"""The token check in front of the real app (issue #15).

These tests drive ``calendar_agent.calendar_server.app`` through a test
client, with the proxy and the LLM mocked. Every token here is synthetic.
"""

import re

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route

from calendar_agent import auth
from calendar_agent.calendar_server import app
from tests.conftest import TEST_API_TOKEN

WRONG_TOKEN = "synthetic-token-wrong-0123456789abcdef"
RIGHT = {"Authorization": f"Bearer {TEST_API_TOKEN}"}
WRONG = {"Authorization": f"Bearer {WRONG_TOKEN}"}


def route_requests() -> list[tuple[str, str, str]]:
    """(method, concrete path, route template) for every route the app has
    registered, derived from the app itself. A route type this walk does not
    understand fails it, rather than being skipped."""
    requests = []
    for route in app.routes:
        assert isinstance(route, Route), (
            f"{route!r} is not a plain HTTP route: teach this walk how to call it, "
            "so it cannot go unchecked"
        )
        path = re.sub(r"\{[^}]+\}", "placeholder", route.path)
        for method in sorted(route.methods):
            requests.append((method, path, route.path))
    assert len(requests) > 18, "the walk found suspiciously few routes"
    return requests


def routes_not_answering_401(client: TestClient, headers: dict[str, str]) -> set[str]:
    return {
        template
        for method, path, template in route_requests()
        if client.request(method, path, headers=headers).status_code != 401
    }


@pytest.fixture
def anonymous_client(mock_proxy_client, mock_llm_service):
    """A client that sends no Authorization header unless a test adds one."""
    return TestClient(app)


class TestRequiredMode:
    """The conftest default: ``required`` with one configured token."""

    def test_every_route_but_health_refuses_a_request_with_no_token(
        self, anonymous_client, mock_proxy_client, mock_llm_service
    ):
        """Walks the app's own route list. The only route left open is
        ``/health``: a new route is covered without being listed anywhere,
        and adding a path to the exempt list fails this test."""
        assert routes_not_answering_401(anonymous_client, {}) == {"/health"}
        assert mock_proxy_client.mock_calls == []
        assert mock_llm_service.mock_calls == []

    def test_every_route_but_health_refuses_a_wrong_token(
        self, anonymous_client, mock_proxy_client, mock_llm_service
    ):
        assert routes_not_answering_401(anonymous_client, WRONG) == {"/health"}
        assert mock_proxy_client.mock_calls == []
        assert mock_llm_service.mock_calls == []

    def test_the_exempt_list_is_health_alone(self):
        assert set(auth.EXEMPT_PATHS) == {"/health"}

    def test_no_route_refuses_the_right_token(self, anonymous_client):
        every_route = {template for _, _, template in route_requests()}
        assert routes_not_answering_401(anonymous_client, RIGHT) == every_route

    def test_the_right_token_reaches_the_route(self, anonymous_client, mock_proxy_client):
        response = anonymous_client.get("/calendars", headers=RIGHT)
        assert response.status_code == 200
        assert response.json()["success"] is True
        mock_proxy_client.list_calendars.assert_awaited_once()

    def test_a_path_or_method_the_app_does_not_have_is_refused_too(self, anonymous_client):
        """Default deny: without a token the answer is 401 whether or not the
        route exists, so the 401 says nothing about which paths are real."""
        assert anonymous_client.get("/no-such-route").status_code == 401
        assert anonymous_client.options("/calendars").status_code == 401
        assert anonymous_client.post("/health/extra").status_code == 401
        assert anonymous_client.get("/health/").status_code == 401

    @pytest.mark.parametrize("headers", [{}, WRONG], ids=["no token", "wrong token"])
    def test_a_refusal_is_401_with_the_envelope_and_a_bearer_challenge(
        self, anonymous_client, headers
    ):
        response = anonymous_client.get("/calendars", headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.headers["content-type"] == "application/json"
        assert response.json() == {"success": False, "error": "Missing or invalid bearer token"}

    def test_the_body_does_not_say_whether_the_token_was_missing_or_wrong(self, anonymous_client):
        missing = anonymous_client.get("/calendars")
        wrong = anonymous_client.get("/calendars", headers=WRONG)
        assert missing.content == wrong.content
        assert missing.headers["www-authenticate"] == wrong.headers["www-authenticate"]

    @pytest.mark.parametrize(
        "value",
        [
            TEST_API_TOKEN,  # no scheme
            f"Basic {TEST_API_TOKEN}",
            f"Token {TEST_API_TOKEN}",
            "Bearer",
            "Bearer ",
            f"Bearer {TEST_API_TOKEN} extra",
            f"Bearer {TEST_API_TOKEN},{TEST_API_TOKEN}",
            f"Bearer{TEST_API_TOKEN}",
            f"Bearer {TEST_API_TOKEN[:-1]}",
            "",
        ],
    )
    def test_an_authorization_header_that_is_not_bearer_plus_the_token_is_refused(
        self, anonymous_client, value
    ):
        response = anonymous_client.get("/calendars", headers={"Authorization": value})
        assert response.status_code == 401

    @pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
    def test_the_scheme_name_is_case_insensitive(self, anonymous_client, scheme):
        response = anonymous_client.get(
            "/calendars", headers={"Authorization": f"{scheme} {TEST_API_TOKEN}"}
        )
        assert response.status_code == 200

    def test_two_authorization_headers_are_refused_even_if_one_is_right(self, anonymous_client):
        response = anonymous_client.get(
            "/calendars",
            headers=[
                ("Authorization", f"Bearer {WRONG_TOKEN}"),
                ("Authorization", f"Bearer {TEST_API_TOKEN}"),
            ],
        )
        assert response.status_code == 401

    def test_the_token_in_the_query_string_or_another_header_is_not_accepted(
        self, anonymous_client
    ):
        assert anonymous_client.get(f"/calendars?token={TEST_API_TOKEN}").status_code == 401
        assert anonymous_client.get(f"/calendars?access_token={TEST_API_TOKEN}").status_code == 401
        assert (
            anonymous_client.get("/calendars", headers={"X-API-Key": TEST_API_TOKEN}).status_code
            == 401
        )

    def test_the_token_is_checked_before_the_request_is_validated(self, anonymous_client):
        """A request that would be a 422 with a token is a 401 without one:
        an unauthenticated caller learns nothing about field names."""
        bad_body = {"no_such_field": 1}
        assert anonymous_client.post("/search", json=bad_body, headers=RIGHT).status_code == 422
        assert anonymous_client.post("/search", json=bad_body).status_code == 401
        assert anonymous_client.get("/calendars?bogus=1", headers=RIGHT).status_code == 422
        assert anonymous_client.get("/calendars?bogus=1").status_code == 401

    def test_health_needs_no_token_and_ignores_a_wrong_one(self, anonymous_client):
        for headers in ({}, WRONG, RIGHT, {"Authorization": "garbage"}):
            response = anonymous_client.get("/health", headers=headers)
            assert response.status_code == 200
            assert response.json()["status"] == "ok"


class TestSettingsNeverLoaded:
    """If the start-up hook has not run (so nothing read the settings), the
    check fails closed rather than open or with a 500."""

    def test_everything_but_health_is_refused_even_with_a_plausible_token(self, anonymous_client):
        del app.state.auth_settings
        try:
            assert routes_not_answering_401(anonymous_client, {}) == {"/health"}
            assert routes_not_answering_401(anonymous_client, RIGHT) == {"/health"}
        finally:
            app.state.auth_settings = None  # the conftest fixture deletes it


# Requests that, sent with a token, go on to a call the proxy holds open while
# a human operator approves it (for minutes). Bodies are valid, so the only
# thing standing between the request and that wait is the token check.
GATED_REQUESTS = [
    ("POST", "/calendars/primary/events", {"summary": "Synthetic event"}, "create_event"),
    ("PUT", "/calendars/primary/events/event_1", {"summary": "Synthetic event"}, "update_event"),
    ("PATCH", "/calendars/primary/events/event_1", {"summary": "Synthetic event"}, "patch_event"),
    ("DELETE", "/calendars/primary/events/event_1", None, "delete_event"),
    (
        "POST",
        "/calendars/primary/events/event_1/respond",
        {"response_status": "accepted"},
        "respond_to_event",
    ),
    (
        "POST",
        "/bulk-actions",
        {"operations": [{"operation": "delete", "event_id": "event_1", "calendar_id": "primary"}]},
        "delete_event",
    ),
]


class TestRefusalComesBeforeTheOperatorApprovalWait:
    @pytest.mark.parametrize(
        "method, path, body, gated_call",
        GATED_REQUESTS,
        ids=[f"{m} {p}" for m, p, _, _ in GATED_REQUESTS],
    )
    @pytest.mark.parametrize("headers", [{}, WRONG], ids=["no token", "wrong token"])
    def test_an_unauthenticated_mutation_is_refused_without_reaching_the_proxy(
        self, anonymous_client, mock_proxy_client, method, path, body, gated_call, headers
    ):
        response = anonymous_client.request(method, path, json=body, headers=headers)
        assert response.status_code == 401
        assert mock_proxy_client.mock_calls == [], "the request got as far as the proxy client"

    @pytest.mark.parametrize(
        "method, path, body, gated_call",
        GATED_REQUESTS,
        ids=[f"{m} {p}" for m, p, _, _ in GATED_REQUESTS],
    )
    def test_control_the_same_request_with_the_token_does_reach_the_gated_call(
        self, anonymous_client, mock_proxy_client, method, path, body, gated_call
    ):
        """Without this the test above could pass for a request that would
        never have reached the proxy anyway (a 422, a wrong path)."""
        anonymous_client.request(method, path, json=body, headers=RIGHT)
        assert getattr(mock_proxy_client, gated_call).await_count == 1

    def test_a_401_on_a_mutation_route_carries_no_outcome(self, anonymous_client):
        """Like a request-validation 422: nothing was attempted, so there is
        no outcome to report."""
        response = anonymous_client.delete("/calendars/primary/events/event_1")
        assert response.status_code == 401
        assert "outcome" not in response.json()
