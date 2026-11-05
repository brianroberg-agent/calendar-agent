"""Bearer-token authentication for every route but /health (issue #15).

Two halves: the settings (pure parsing and matching, no framework), and one
ASGI middleware that sits in front of the whole application. The check is
default-deny: it exempts an explicit list of paths and refuses everything
else, so a route added later is covered without anyone protecting it.
"""

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

KEYS_ENV = "CALENDAR_AGENT_API_KEYS"
MODE_ENV = "CALENDAR_AGENT_AUTH"
MIN_TOKEN_LENGTH = 32
# The only paths served without a token. Compared with the request path
# exactly; tests/test_auth_routes.py fails if this grows.
EXEMPT_PATHS = frozenset({"/health"})
UNAUTHORIZED_MESSAGE = "Missing or invalid bearer token"
NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
# RFC 6750 section 2.1, the b64token a Bearer header carries.
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9\-._~+/]+=*")

# The one comparison every presented token goes through.
_constant_time_equal = hmac.compare_digest


class AuthConfigError(ValueError):
    """The authentication settings cannot be used; the service must not start."""


class AuthMode(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    OFF = "off"


def _digest(token: bytes) -> bytes:
    return hashlib.sha256(token).digest()


@dataclass(frozen=True)
class ApiKey:
    """One caller's token. Only its SHA-256 digest is kept: a request is
    checked by comparing digests, and nothing that prints these settings can
    print a token."""

    name: str
    digest: bytes


def _bad_keys(position: int, problem: str) -> AuthConfigError:
    # Position only. Nothing from the value is repeated: a token pasted into
    # the name slot, or an entry with no colon, would otherwise be printed by
    # the very error that reports it.
    return AuthConfigError(f"{KEYS_ENV}: entry {position} {problem}")


def parse_api_keys(raw: str | None) -> tuple[ApiKey, ...]:
    """Parse ``name:token`` pairs, comma-separated. Unset or blank is no keys.

    Raises AuthConfigError for an entry that is not ``name:token``, a name
    outside letters/digits/``.``/``_``/``-``, a token that is empty, shorter
    than MIN_TOKEN_LENGTH or not sendable in a Bearer header, and for a name
    or a token that appears twice.
    """
    keys: list[ApiKey] = []
    positions: dict[str, int] = {}
    digests: dict[bytes, int] = {}
    entries = [entry.strip() for entry in (raw or "").split(",")]
    for position, entry in enumerate((e for e in entries if e), start=1):
        name, colon, token = (part.strip() for part in entry.partition(":"))
        if not colon:
            raise _bad_keys(position, "is not in name:token form")
        if not name:
            raise _bad_keys(position, "has no name before the colon")
        if not NAME_PATTERN.fullmatch(name):
            raise _bad_keys(
                position, "has a name with characters other than letters, digits, '.', '_', '-'"
            )
        if not token:
            raise _bad_keys(position, "has an empty token")
        if not TOKEN_PATTERN.fullmatch(token):
            raise _bad_keys(
                position,
                "has a token a Bearer header cannot carry (allowed: letters, digits, "
                "'-', '.', '_', '~', '+', '/', and trailing '='; no quotes or spaces)",
            )
        if len(token) < MIN_TOKEN_LENGTH:
            raise _bad_keys(position, f"has a token shorter than {MIN_TOKEN_LENGTH} characters")
        digest = _digest(token.encode("ascii"))
        if name in positions:
            raise _bad_keys(position, f"repeats the name of entry {positions[name]}")
        if digest in digests:
            raise _bad_keys(position, f"repeats the token of entry {digests[digest]}")
        positions[name] = position
        digests[digest] = position
        keys.append(ApiKey(name=name, digest=digest))
    return tuple(keys)


@dataclass(frozen=True)
class AuthSettings:
    mode: AuthMode
    keys: tuple[ApiKey, ...] = ()


def resolve_auth_mode(raw: str | None) -> AuthMode:
    """Unset or blank is ``required``. An unknown value is an error, never
    read as one of the modes; it is not repeated in the message."""
    value = (raw or "").strip().lower()
    if not value:
        return AuthMode.REQUIRED
    try:
        return AuthMode(value)
    except ValueError:
        allowed = ", ".join(mode.value for mode in AuthMode)
        raise AuthConfigError(
            f"{MODE_ENV} is set to a value that is not one of: {allowed}"
        ) from None


def load_auth_settings(environ: Mapping[str, str]) -> AuthSettings:
    """Read the two settings. Raises AuthConfigError when the service must not
    start: an unknown mode, a malformed keys value, or ``required`` with no
    tokens. ``off`` does not read the keys at all."""
    mode = resolve_auth_mode(environ.get(MODE_ENV))
    if mode is AuthMode.OFF:
        return AuthSettings(mode=mode)
    keys = parse_api_keys(environ.get(KEYS_ENV))
    if mode is AuthMode.REQUIRED and not keys:
        raise AuthConfigError(
            f"{MODE_ENV} is 'required' (the default when it is unset) but {KEYS_ENV} "
            f"holds no tokens. Set {KEYS_ENV} to comma-separated name:token pairs, or "
            f"set {MODE_ENV} to 'optional' or 'off' to start without them."
        )
    return AuthSettings(mode=mode, keys=keys)


def match_token(keys: tuple[ApiKey, ...], presented: bytes) -> str | None:
    """The name of the key ``presented`` belongs to, or None.

    Compares against every configured key and does not stop at a match.
    """
    presented_digest = _digest(presented)
    caller = None
    for key in keys:
        if _constant_time_equal(key.digest, presented_digest):
            caller = key.name
    return caller


# What applies when nothing has loaded the settings (the start-up hook did not
# run): `required` with no keys, so every non-exempt request is refused.
FAIL_CLOSED = AuthSettings(mode=AuthMode.REQUIRED)


def bearer_token(authorization: list[bytes]) -> bytes | None:
    """The token from a request's Authorization header values, or None.

    None unless there is exactly one header and it is ``Bearer <token>``
    (scheme case-insensitive, one token, nothing after it).
    """
    if len(authorization) != 1:
        return None
    parts = authorization[0].split()
    if len(parts) != 2 or parts[0].lower() != b"bearer":
        return None
    return parts[1]


class BearerAuthMiddleware:
    """Refuse any request that does not carry a configured token.

    Sits in front of the whole application, so the check runs before
    routing, request validation and the route itself: a refused request
    reads no body and reaches no upstream call. ``get_settings`` is called
    per request and returns what the start-up hook loaded, or None if it
    never ran, in which case everything but the exempt paths is refused.
    """

    def __init__(self, app: ASGIApp, get_settings, exempt_paths=EXEMPT_PATHS) -> None:
        self.app = app
        self.get_settings = get_settings
        self.exempt_paths = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.exempt_paths:
            await self.app(scope, receive, send)
            return
        settings: AuthSettings = self.get_settings() or FAIL_CLOSED
        authorization = [value for name, value in scope["headers"] if name == b"authorization"]
        token = bearer_token(authorization)
        if token is not None and match_token(settings.keys, token) is not None:
            await self.app(scope, receive, send)
            return
        refusal = JSONResponse(
            {"success": False, "error": UNAUTHORIZED_MESSAGE},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await refusal(scope, receive, send)
