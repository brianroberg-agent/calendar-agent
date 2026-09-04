"""HTTP client for communicating with the Calendar API proxy server."""

import math
import os
from typing import Any
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

from .exceptions import (
    ProxyAuthError,
    ProxyConfigError,
    ProxyError,
    ProxyForbiddenError,
    ProxyNotFoundError,
    ProxyTimeoutError,
)

load_dotenv()

PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8000")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")

# Read operations answer immediately; mutations block in the proxy while a
# human operator approves them (a 300s window server-side by default). The
# mutation timeout must outlive that window, or the approval/rejection outcome
# is undeliverable and the operation completes unobserved (see issue #4).
READ_TIMEOUT = 30.0

# The proxy's window is api-proxy's ``--confirmation-timeout`` (300s unless
# its operator changed it; ``<= 0`` there means wait forever). api-proxy does
# not publish the value on ``/health``, so this side has to be told: the
# ``PROXY_CONFIRMATION_WINDOW`` env var must be kept in step with the proxy's
# flag by hand. It is a named, checkable number here so the mismatch that
# caused issue #4 is a configuration error rather than folklore.
DEFAULT_PROXY_CONFIRMATION_WINDOW = 300.0

# How far past the window the mutation budget must reach. The proxy's own
# timeout firing, its 403 travelling back, and the network all happen inside
# this gap; a budget that clears the window by a second is not clearing it.
CONFIRM_TIMEOUT_MARGIN = 30.0


def _finite_seconds(raw: str, name: str) -> float:
    """Parse ``raw`` as a finite number of seconds, or raise ProxyConfigError.

    ``float("nan")`` and ``float("inf")`` parse cleanly and compare False
    against every bound, so a plain numeric check lets them through; ``nan``
    then becomes an httpx timeout that is neither bounded nor infinite.
    """
    try:
        value = float(raw)
    except ValueError as e:
        raise ProxyConfigError(
            f"{name} must be a number of seconds, got {raw!r}"
        ) from e
    if not math.isfinite(value):
        raise ProxyConfigError(f"{name} must be a finite number of seconds, got {raw!r}")
    return value


def resolve_confirmation_window(raw: str | None) -> float:
    """Resolve the proxy's approval window from ``PROXY_CONFIRMATION_WINDOW``.

    Refuses ``<= 0``: api-proxy reads that as "wait forever", a window no
    client timeout can outlive, so the guard below could not be honest.
    """
    if raw is None:
        return DEFAULT_PROXY_CONFIRMATION_WINDOW
    window = _finite_seconds(raw, "PROXY_CONFIRMATION_WINDOW")
    if window <= 0:
        raise ProxyConfigError(
            f"PROXY_CONFIRMATION_WINDOW is {raw!r}; api-proxy treats a non-positive "
            "--confirmation-timeout as 'wait forever', which no client timeout can "
            "outlive. Give the proxy a finite window and mirror it here"
        )
    return window


PROXY_CONFIRMATION_WINDOW = resolve_confirmation_window(
    os.environ.get("PROXY_CONFIRMATION_WINDOW")
)
DEFAULT_CONFIRM_TIMEOUT = PROXY_CONFIRMATION_WINDOW + CONFIRM_TIMEOUT_MARGIN


def resolve_confirm_timeout(
    raw: str | None, window: float = PROXY_CONFIRMATION_WINDOW
) -> float:
    """Resolve the mutation timeout from its raw ``PROXY_CONFIRM_TIMEOUT`` value.

    Refuses anything that does not outlive the proxy's approval window by
    ``CONFIRM_TIMEOUT_MARGIN``. A shorter budget is not a tuning choice: it
    makes every approval and rejection undeliverable, so mutations complete
    unobserved (issue #4).
    """
    floor = window + CONFIRM_TIMEOUT_MARGIN
    if raw is None:
        return floor
    timeout = _finite_seconds(raw, "PROXY_CONFIRM_TIMEOUT")
    if timeout < floor:
        raise ProxyConfigError(
            f"PROXY_CONFIRM_TIMEOUT is {timeout:.0f}s, which does not outlive the "
            f"proxy's {window:.0f}s operator-approval window by the required "
            f"{CONFIRM_TIMEOUT_MARGIN:.0f}s margin (minimum {floor:.0f}s); "
            "mutations would time out client-side and complete unobserved"
        )
    return timeout


CONFIRM_TIMEOUT = resolve_confirm_timeout(os.environ.get("PROXY_CONFIRM_TIMEOUT"))


class CalendarProxyClient:
    """Client for making authenticated requests to the Calendar API proxy."""

    def __init__(self, proxy_url: str | None = None, api_key: str | None = None):
        self.proxy_url = (proxy_url or PROXY_URL).rstrip("/")
        self.api_key = api_key or PROXY_API_KEY
        if not self.api_key:
            raise ProxyAuthError("PROXY_API_KEY environment variable is not set")

    def _get_headers(self) -> dict[str, str]:
        """Return headers for proxy requests."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _parse_error_message(self, response: httpx.Response, default: str) -> str:
        """Extract error message from response, with fallback to default."""
        try:
            data = response.json()
            return data.get("detail", data.get("message", default))
        except (ValueError, KeyError):
            return default

    def _handle_response(self, response: httpx.Response) -> dict[str, Any]:
        """Handle response and raise appropriate exceptions for errors."""
        if response.status_code == 401:
            message = self._parse_error_message(
                response, "Invalid or missing API key"
            )
            raise ProxyAuthError(message)

        if response.status_code == 403:
            message = self._parse_error_message(
                response, "Operation forbidden or rejected by operator"
            )
            raise ProxyForbiddenError(message)

        if response.status_code in (404, 410):
            # 410 Gone is Google's answer for an event that has been deleted:
            # "absent", the expected result when verifying a delete.
            message = self._parse_error_message(response, "Not found")
            raise ProxyNotFoundError(message)

        if response.status_code >= 500:
            message = self._parse_error_message(response, "Proxy server error")
            raise ProxyError(f"Proxy server error: {message}")

        if response.status_code >= 400:
            message = self._parse_error_message(response, "Bad request")
            raise ProxyError(f"Proxy error ({response.status_code}): {message}")

        return response.json()

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = READ_TIMEOUT,
    ) -> httpx.Response:
        """Send one request to the proxy, mapping timeouts to ProxyTimeoutError."""
        kwargs: dict[str, Any] = {"headers": self._get_headers()}
        if params is not None:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
        try:
            # The long CONFIRM_TIMEOUT budget is for the proxy's human-approval
            # window, which only starts once a request reaches it; connecting
            # gets the ordinary budget so an unreachable proxy fails fast.
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=READ_TIMEOUT)
            ) as client:
                return await getattr(client, method)(url, **kwargs)
        except httpx.ConnectTimeout as e:
            # The request never reached the proxy: definitive failure, not an
            # unknown outcome.
            raise ProxyError(
                f"Could not connect to proxy within {READ_TIMEOUT:.0f}s"
            ) from e
        except httpx.TimeoutException as e:
            raise ProxyTimeoutError(
                f"No response from proxy after {timeout:.0f}s; the operation's "
                "outcome is unknown — a confirmation-gated mutation may still "
                "complete if approved later. Verify by re-reading the resource "
                "before retrying."
            ) from e
        except httpx.HTTPError as e:
            raise ProxyError(f"Proxy connection failed: {e}") from e

    # ========== URL construction ==========

    def _calendar_url(self, calendar_id: str) -> str:
        # Calendar IDs may contain characters like '#' (Google's built-in
        # calendars: '#contacts@group.v.calendar.google.com') that would
        # otherwise truncate the URL as a fragment and hit a different route.
        return f"{self.proxy_url}/calendar/v3/calendars/{quote(calendar_id, safe='@')}"

    def _event_url(self, calendar_id: str, event_id: str) -> str:
        return f"{self._calendar_url(calendar_id)}/events/{quote(event_id, safe='')}"

    # ========== Calendar Operations ==========

    async def list_calendars(
        self,
        max_results: int | None = None,
        page_token: str | None = None,
        show_deleted: bool | None = None,
        show_hidden: bool | None = None,
    ) -> dict[str, Any]:
        """List all calendars for the authenticated user."""
        url = f"{self.proxy_url}/calendar/v3/users/me/calendarList"
        params: dict[str, Any] = {}

        if max_results is not None:
            params["maxResults"] = max_results
        if page_token is not None:
            params["pageToken"] = page_token
        if show_deleted is not None:
            params["showDeleted"] = show_deleted
        if show_hidden is not None:
            params["showHidden"] = show_hidden

        response = await self._send("get", url, params=params or None)
        return self._handle_response(response)

    async def get_calendar(self, calendar_id: str) -> dict[str, Any]:
        """Get metadata for a specific calendar."""
        url = self._calendar_url(calendar_id)

        response = await self._send("get", url)
        return self._handle_response(response)

    # ========== Event Operations ==========

    async def list_events(
        self,
        calendar_id: str,
        max_results: int | None = None,
        page_token: str | None = None,
        time_min: str | None = None,
        time_max: str | None = None,
        q: str | None = None,
        single_events: bool = True,
        order_by: str | None = None,
        show_deleted: bool | None = None,
        updated_min: str | None = None,
        sync_token: str | None = None,
    ) -> dict[str, Any]:
        """List events in a calendar."""
        url = f"{self._calendar_url(calendar_id)}/events"
        params: dict[str, Any] = {"singleEvents": single_events}

        if max_results is not None:
            params["maxResults"] = max_results
        if page_token is not None:
            params["pageToken"] = page_token
        if time_min is not None:
            params["timeMin"] = time_min
        if time_max is not None:
            params["timeMax"] = time_max
        if q is not None:
            params["q"] = q
        if order_by is not None:
            params["orderBy"] = order_by
        if show_deleted is not None:
            params["showDeleted"] = show_deleted
        if updated_min is not None:
            params["updatedMin"] = updated_min
        if sync_token is not None:
            params["syncToken"] = sync_token

        response = await self._send("get", url, params=params)
        return self._handle_response(response)

    async def get_event(
        self,
        calendar_id: str,
        event_id: str,
        time_zone: str | None = None,
    ) -> dict[str, Any]:
        """Get a specific event by ID."""
        url = self._event_url(calendar_id, event_id)
        params: dict[str, Any] = {}

        if time_zone is not None:
            params["timeZone"] = time_zone

        response = await self._send("get", url, params=params or None)
        return self._handle_response(response)

    async def create_event(
        self,
        calendar_id: str,
        event_data: dict[str, Any],
        send_updates: str | None = None,
        conference_data_version: int | None = None,
    ) -> dict[str, Any]:
        """Create a new event in a calendar."""
        url = f"{self._calendar_url(calendar_id)}/events"
        params: dict[str, Any] = {}

        if send_updates is not None:
            params["sendUpdates"] = send_updates
        if conference_data_version is not None:
            params["conferenceDataVersion"] = conference_data_version

        response = await self._send(
            "post", url, params=params or None, json_body=event_data,
            timeout=CONFIRM_TIMEOUT,
        )
        return self._handle_response(response)

    async def respond_to_event(
        self,
        calendar_id: str,
        event_id: str,
        response_status: str,
    ) -> dict[str, Any]:
        """RSVP to an event by setting the owner's own responseStatus.

        Forwards to the proxy's dedicated ``/respond`` route, which changes only
        the self attendee's status and sends no notifications.
        """
        url = f"{self._event_url(calendar_id, event_id)}/respond"
        response = await self._send(
            "post", url, json_body={"responseStatus": response_status},
            timeout=CONFIRM_TIMEOUT,
        )
        return self._handle_response(response)

    async def update_event(
        self,
        calendar_id: str,
        event_id: str,
        event_data: dict[str, Any],
        send_updates: str | None = None,
        conference_data_version: int | None = None,
    ) -> dict[str, Any]:
        """Update an event (full replacement)."""
        url = self._event_url(calendar_id, event_id)
        params: dict[str, Any] = {}

        if send_updates is not None:
            params["sendUpdates"] = send_updates
        if conference_data_version is not None:
            params["conferenceDataVersion"] = conference_data_version

        response = await self._send(
            "put", url, params=params or None, json_body=event_data,
            timeout=CONFIRM_TIMEOUT,
        )
        return self._handle_response(response)

    async def patch_event(
        self,
        calendar_id: str,
        event_id: str,
        event_data: dict[str, Any],
        send_updates: str | None = None,
        conference_data_version: int | None = None,
    ) -> dict[str, Any]:
        """Partially update an event."""
        url = self._event_url(calendar_id, event_id)
        params: dict[str, Any] = {}

        if send_updates is not None:
            params["sendUpdates"] = send_updates
        if conference_data_version is not None:
            params["conferenceDataVersion"] = conference_data_version

        response = await self._send(
            "patch", url, params=params or None, json_body=event_data,
            timeout=CONFIRM_TIMEOUT,
        )
        return self._handle_response(response)

    async def delete_event(
        self,
        calendar_id: str,
        event_id: str,
        send_updates: str | None = None,
    ) -> dict[str, Any]:
        """Delete an event."""
        url = self._event_url(calendar_id, event_id)
        params: dict[str, Any] = {}

        if send_updates is not None:
            params["sendUpdates"] = send_updates

        response = await self._send(
            "delete", url, params=params or None, timeout=CONFIRM_TIMEOUT
        )
        # DELETE may return empty response on success
        if response.status_code == 204:
            return {"success": True}
        return self._handle_response(response)


# Singleton pattern for easy access
_client: CalendarProxyClient | None = None


def get_calendar_client() -> CalendarProxyClient:
    """Get or create the singleton CalendarProxyClient instance."""
    global _client
    if _client is None:
        _client = CalendarProxyClient()
    return _client
