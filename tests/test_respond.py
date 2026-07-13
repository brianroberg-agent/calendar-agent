"""Tests for the RSVP (respond) endpoint and proxy-client method."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from calendar_agent.calendar_server import respond_to_event  # noqa: F401 (ensures endpoint exists)
from calendar_agent.exceptions import ProxyForbiddenError
from calendar_agent.proxy_client import CalendarProxyClient


class TestProxyClientRespond:
    """CalendarProxyClient.respond_to_event forwards to the proxy /respond route."""

    @pytest.fixture
    def mock_response(self):
        r = AsyncMock(spec=httpx.Response)
        r.status_code = 200
        r.json.return_value = {
            "id": "e1",
            "attendees": [{"email": "me@x", "responseStatus": "accepted", "self": True}],
        }
        return r

    async def test_posts_to_respond_path_with_status(self, mock_response):
        client = CalendarProxyClient(proxy_url="http://proxy", api_key="k")
        with patch("calendar_agent.proxy_client.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await client.respond_to_event("robergb@dm.org", "e1", "accepted")

        mock_client.post.assert_called_once()
        call = mock_client.post.call_args
        assert call.args[0] == "http://proxy/calendar/v3/calendars/robergb@dm.org/events/e1/respond"
        assert call.kwargs["json"] == {"responseStatus": "accepted"}
        assert result["id"] == "e1"


class TestRespondEndpoint:
    """POST /calendars/{calendar_id}/events/{event_id}/respond"""

    def test_respond_success(self, client, mock_proxy_client):
        mock_proxy_client.respond_to_event.return_value = {"id": "e1", "summary": "GMDM"}
        resp = client.post(
            "/calendars/primary/events/e1/respond",
            json={"response_status": "accepted"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["event"]["id"] == "e1"
        mock_proxy_client.respond_to_event.assert_called_once_with("primary", "e1", "accepted")

    def test_respond_forbidden_returns_error(self, client, mock_proxy_client):
        mock_proxy_client.respond_to_event.side_effect = ProxyForbiddenError("blocked")
        resp = client.post(
            "/calendars/primary/events/e1/respond",
            json={"response_status": "accepted"},
        )
        data = resp.json()
        assert data["success"] is False
        assert data["error"]
