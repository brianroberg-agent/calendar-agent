"""Bearer-token authentication for every route but /health (issue #15).

Pure settings logic first (no I/O, no framework), so it is unit-testable on
its own.
"""

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

KEYS_ENV = "CALENDAR_AGENT_API_KEYS"
MODE_ENV = "CALENDAR_AGENT_AUTH"
MIN_TOKEN_LENGTH = 32
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
