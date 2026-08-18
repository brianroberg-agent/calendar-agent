"""Contract tests against the vendored api-proxy OpenAPI snapshot.

Every proxy route CalendarProxyClient calls must exist — path and method — in
docs/api-proxy-openapi-doc.json, the stamped snapshot of the proxy contract
this agent was built against. When a test here fails after adding a client
method, refresh the snapshot from the current proxy with
scripts/refresh_openapi.py (and confirm the route actually exists in api-proxy
before shipping the client change).
"""

import inspect
import json
from pathlib import Path
from unittest.mock import patch

import httpx

from calendar_agent.proxy_client import CalendarProxyClient

SNAPSHOT_PATH = Path(__file__).parent.parent / "docs" / "api-proxy-openapi-doc.json"

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}

# One representative call per public client method. The coverage test below
# fails when a new client method is added without an entry here, so every new
# proxy route gets checked against the snapshot.
CLIENT_CALLS = {
    "list_calendars": lambda c: c.list_calendars(),
    "get_calendar": lambda c: c.get_calendar("primary"),
    "list_events": lambda c: c.list_events("primary"),
    "get_event": lambda c: c.get_event("primary", "event123"),
    "create_event": lambda c: c.create_event("primary", {"summary": "s"}),
    "update_event": lambda c: c.update_event("primary", "event123", {"summary": "s"}),
    "patch_event": lambda c: c.patch_event("primary", "event123", {"summary": "s"}),
    "delete_event": lambda c: c.delete_event("primary", "event123"),
    "respond_to_event": lambda c: c.respond_to_event("primary", "event123", "accepted"),
}


class _RecordingClient:
    """Stands in for httpx.AsyncClient; records each request's method and path."""

    def __init__(self, recorded: list[tuple[str, str]]):
        self._recorded = recorded

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def _respond(self, method: str, url: str) -> httpx.Response:
        self._recorded.append((method, httpx.URL(url).path))
        return httpx.Response(200, json={}, request=httpx.Request(method, url))

    async def get(self, url, **kwargs):
        return self._respond("GET", url)

    async def post(self, url, **kwargs):
        return self._respond("POST", url)

    async def put(self, url, **kwargs):
        return self._respond("PUT", url)

    async def patch(self, url, **kwargs):
        return self._respond("PATCH", url)

    async def delete(self, url, **kwargs):
        return self._respond("DELETE", url)


async def _record_request(call) -> tuple[str, str]:
    """Run one client call against a recording fake; return (method, path)."""
    recorded: list[tuple[str, str]] = []
    client = CalendarProxyClient(proxy_url="http://proxy", api_key="test-key")
    with patch(
        "calendar_agent.proxy_client.httpx.AsyncClient",
        lambda **kwargs: _RecordingClient(recorded),
    ):
        await call(client)
    assert len(recorded) == 1, f"Expected exactly one request, got {recorded}"
    return recorded[0]


def _load_snapshot_spec() -> dict:
    return json.loads(SNAPSHOT_PATH.read_text())


def _snapshot_routes() -> dict[str, set[str]]:
    """Map each path template in the snapshot to its allowed HTTP methods."""
    spec = _load_snapshot_spec()
    return {
        path: {method.upper() for method in operations if method.upper() in HTTP_METHODS}
        for path, operations in spec["paths"].items()
    }


def _matches_template(template: str, path: str) -> bool:
    """Match a concrete request path against an OpenAPI path template.

    Segments are compared without normalizing slashes: a trailing or doubled
    slash, or an empty path parameter, would 307/404 against the real proxy,
    so it must not match here either.
    """
    template_parts = template.split("/")
    path_parts = path.split("/")
    if len(template_parts) != len(path_parts):
        return False
    return all(
        (part.startswith("{") and part.endswith("}") and actual != "") or part == actual
        for part, actual in zip(template_parts, path_parts, strict=True)
    )


async def test_client_routes_exist_in_snapshot(subtests):
    """Each route the client calls must exist in the snapshot with its method."""
    routes = _snapshot_routes()

    for name, call in CLIENT_CALLS.items():
        with subtests.test(client_method=name):
            method, path = await _record_request(call)
            matching = [t for t in routes if _matches_template(t, path)]
            assert matching, (
                f"{name} calls {method} {path}, which matches no path in "
                f"{SNAPSHOT_PATH.name}. If the proxy contract has changed, refresh "
                "the snapshot with scripts/refresh_openapi.py; otherwise the route "
                "does not exist in api-proxy."
            )
            assert any(method in routes[t] for t in matching), (
                f"{name} calls {method} {path}, but the snapshot only allows "
                f"{sorted(set().union(*(routes[t] for t in matching)))} there. "
                "Refresh the snapshot with scripts/refresh_openapi.py if the proxy "
                "contract has changed."
            )


def test_matcher_rejects_malformed_paths():
    """Slash variants the real proxy would 307/404 must not match a template."""
    template = "/calendar/v3/calendars/{calendar_id}/events"
    assert _matches_template(template, "/calendar/v3/calendars/primary/events")
    assert not _matches_template(template, "/calendar/v3/calendars/primary/events/")
    assert not _matches_template(template, "//calendar/v3/calendars/primary/events")
    assert not _matches_template(template, "/calendar/v3/calendars//events")


def test_every_client_method_has_a_contract_entry():
    """Adding a client method without a CLIENT_CALLS entry must fail loudly."""
    public_methods = {
        name
        for name, _ in inspect.getmembers(CalendarProxyClient, inspect.iscoroutinefunction)
        if not name.startswith("_")
    }
    assert public_methods == CLIENT_CALLS.keys(), (
        "CLIENT_CALLS is out of sync with CalendarProxyClient's public methods. "
        "Add a representative call for each new method so its route is checked "
        "against the snapshot."
    )


def test_snapshot_has_provenance_stamp():
    """The snapshot must say what it was generated from and when."""
    spec = _load_snapshot_spec()
    provenance = spec.get("x-generated-from")
    assert provenance, (
        f"{SNAPSHOT_PATH.name} has no x-generated-from stamp. Regenerate it with "
        "scripts/refresh_openapi.py instead of copying the spec in by hand."
    )
    for field in ("source", "commit", "generated_at"):
        assert field in provenance, f"x-generated-from is missing '{field}'"
