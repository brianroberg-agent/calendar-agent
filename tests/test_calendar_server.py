"""Tests for Calendar Agent server endpoints."""


from unittest.mock import AsyncMock, patch

import httpx
import pytest

from calendar_agent.calendar_server import EventSummary, app, event_to_summary
from calendar_agent.exceptions import (
    ProxyAuthError,
    ProxyConfigError,
    ProxyError,
    ProxyForbiddenError,
    ProxyNotFoundError,
    ProxyRequestError,
    ProxyTimeoutError,
)
from calendar_agent.proxy_client import (
    CONFIRM_TIMEOUT,
    CONFIRM_TIMEOUT_MARGIN,
    PROXY_CONFIRMATION_WINDOW,
    READ_TIMEOUT,
    CalendarProxyClient,
    resolve_confirm_timeout,
    resolve_confirmation_window,
)
from tests.factories import (
    AUTH_USER_EMAIL,
    CANCELLED_STUB,
    COLLEAGUE_EMAIL,
    GROUP_CALENDAR_ID,
    SAMPLE_EVENTS,
    colleague_copy,
    get_sample_event,
)

# ============================================================================
# Health Endpoint Tests
# ============================================================================


class TestHealthEndpoint:
    """Tests for the /health endpoint."""

    def test_health_returns_ok(self, client):
        """Health endpoint returns status ok."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "version" in data

    def test_health_returns_version(self, client):
        """Health endpoint returns correct version."""
        response = client.get("/health")
        data = response.json()
        assert data["version"] == "1.0.0"


# ============================================================================
# Calendar Endpoint Tests
# ============================================================================


class TestCalendarsEndpoint:
    """Tests for the /calendars endpoint."""

    def test_list_calendars_success(self, client, mock_proxy_client):
        """List calendars returns all calendars."""
        response = client.get("/calendars")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["calendars"]) == 3
        assert data["calendars"][0]["id"] == AUTH_USER_EMAIL

    def test_list_calendars_empty(self, client, mock_proxy_client):
        """List calendars handles empty list."""
        mock_proxy_client.list_calendars.return_value = {"items": []}
        response = client.get("/calendars")
        data = response.json()
        assert data["success"] is True
        assert len(data["calendars"]) == 0

    def test_list_calendars_with_pagination(self, client, mock_proxy_client):
        """List calendars supports pagination parameters."""
        response = client.get("/calendars?max_results=10&page_token=abc123")
        assert response.status_code == 200
        mock_proxy_client.list_calendars.assert_called_with(
            max_results=10,
            page_token="abc123",
        )

    def test_list_calendars_error(self, client, mock_proxy_client):
        """List calendars handles proxy errors."""
        mock_proxy_client.list_calendars.side_effect = ProxyError("Connection failed")
        response = client.get("/calendars")
        data = response.json()
        assert data["success"] is False
        assert "error" in data
        assert "Proxy error" in data["error"]

    def test_get_calendar_success(self, client, mock_proxy_client):
        """Get specific calendar returns calendar details."""
        response = client.get("/calendars/primary")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["calendar"]["id"] == AUTH_USER_EMAIL

    def test_get_calendar_not_found(self, client, mock_proxy_client):
        """Get calendar handles not found."""
        mock_proxy_client.get_calendar.side_effect = ProxyError("Calendar not found")
        response = client.get("/calendars/nonexistent")
        data = response.json()
        assert data["success"] is False


# ============================================================================
# Event CRUD Endpoint Tests
# ============================================================================


class TestEventToSummary:
    """event_to_summary(): organizer exposure and RSVP state (issue #9).

    Google defines ``attendees[].self`` / ``organizer.self`` relative to the
    calendar the event copy sits on, so every ``calendar_*`` field describes
    the calendar named by ``calendar_id`` -- which is the authenticated user
    only when that calendar is the user's own.
    """

    def test_self_flags_describe_the_calendar_being_read(self):
        summary = event_to_summary(colleague_copy(), COLLEAGUE_EMAIL)
        assert summary.calendar_id == COLLEAGUE_EMAIL
        assert summary.calendar_is_organizer is False
        assert summary.calendar_rsvp_state == "accepted"  # Carol's RSVP, not john.doe@'s
        assert summary.organizer_email == "dave@example.com"
        assert summary.creator_email == "dave@example.com"

    def test_summary_field_set_is_exactly_the_documented_one(self):
        """Pins the README's Key Privacy Features claim for list/search rows:
        these fields and no others -- in particular no description and no
        attendee list. Adding a field here is a documentation change too."""
        expected = {
            "id",
            "calendar_id",
            "summary",
            "start",
            "end",
            "location",
            "attendee_count",
            "is_all_day",
            "status",
            "html_link",
            "organizer_email",
            "creator_email",
            "calendar_is_organizer",
            "calendar_rsvp_state",
        }
        assert set(EventSummary.model_fields) == expected

    def test_no_field_claims_to_be_the_authenticated_users_rsvp(self):
        """The old contract (is_organizer / response_status "of the
        authenticated user") is gone: nothing on the wire is named as if it
        described the caller rather than the calendar."""
        dumped = event_to_summary(colleague_copy(), COLLEAGUE_EMAIL).model_dump()
        assert "is_organizer" not in dumped
        assert "response_status" not in dumped
        assert "calendar_response_status" not in dumped  # dropped in round 3 (redundant)
        assert not any(k.startswith("user_") for k in dumped)

    def test_perspective_fields_are_required_in_the_openapi_schema(self):
        """Both perspective fields are always emitted, so the schema must not
        mark either optional (finding 15, round 3)."""
        schema = app.openapi()["components"]["schemas"]["EventSummary"]
        assert {"calendar_is_organizer", "calendar_rsvp_state"} <= set(schema["required"])
        assert "calendar_response_status" not in schema["properties"]

    def test_malformed_organizer_or_attendees_degrade_instead_of_raising(self):
        """Finding 8 (round 3): a non-dict organizer/creator or attendee row
        reads as absent instead of raising. Only these fields are guarded;
        a malformed start/end or summary is not."""
        event = get_sample_event()
        event["organizer"] = "dave@example.com"
        event["creator"] = ["dave@example.com"]
        event["attendees"] = [None, "bob@example.com"]
        summary = event_to_summary(event, "primary")
        assert summary.organizer_email is None
        assert summary.creator_email is None
        assert summary.calendar_is_organizer is False
        assert summary.calendar_rsvp_state == "unknown"
        # attendee_count agrees with the perspective: the non-dict rows are
        # not attendees, so the count is 0, not 2 (finding 7, round 4).
        assert summary.attendee_count == 0
        event["attendees"] = "not-a-list"
        assert event_to_summary(event, "primary").attendee_count == 0

    def test_pending_invitation_on_own_calendar(self):
        event = get_sample_event(
            organizer={"email": "dave@example.com", "self": False},
            attendees=[{"email": AUTH_USER_EMAIL, "self": True, "responseStatus": "needsAction"}],
        )
        summary = event_to_summary(event, "primary")
        assert summary.calendar_is_organizer is False
        assert summary.calendar_rsvp_state == "needsAction"

    def test_own_event_with_attendees_but_no_own_entry(self):
        """The dangerous null from issue #9: the calendar organizes, others
        are invited, the calendar has no attendee entry of its own. Reads as
        the calendar's own event, not an unanswered invitation."""
        event = get_sample_event(
            organizer={"email": AUTH_USER_EMAIL, "self": True},
            creator={"email": AUTH_USER_EMAIL, "self": True},
            attendees=[
                {"email": "alice@example.com", "responseStatus": "accepted"},
                {"email": "bob@example.com", "responseStatus": "needsAction"},
            ],
        )
        summary = event_to_summary(event, "primary")
        assert summary.attendee_count == 2
        assert summary.calendar_is_organizer is True
        assert summary.calendar_rsvp_state == "organizer_no_rsvp"

    def test_group_calendar_native_event_names_the_calendar_and_the_creator(self):
        """An event created directly on a group calendar: Google makes the
        calendar itself the organizer (email == calendar id, self:true); the
        person who created it is only in ``creator``."""
        group = "abc123@group.calendar.google.com"
        event = get_sample_event(
            organizer={"email": group, "displayName": "Team Calendar", "self": True},
            creator={"email": AUTH_USER_EMAIL},
            attendees=None,
        )
        summary = event_to_summary(event, group)
        assert summary.attendee_count == 0
        assert summary.calendar_is_organizer is True
        assert summary.calendar_rsvp_state == "organizer_no_rsvp"
        assert summary.organizer_email == group
        assert summary.creator_email == AUTH_USER_EMAIL

    def test_calendar_neither_organizes_nor_attends(self):
        event = get_sample_event(
            organizer={"email": "dave@example.com", "self": False},
            attendees=[{"email": "alice@example.com", "responseStatus": "accepted"}],
        )
        summary = event_to_summary(event, "primary")
        assert summary.calendar_is_organizer is False
        assert summary.calendar_rsvp_state == "not_attendee"

    def test_cancelled_recurring_stub_is_unknown_not_own_event(self):
        event = get_sample_event(status="cancelled", with_times=False)
        summary = event_to_summary(event, "primary")
        assert summary.status == "cancelled"
        assert summary.attendee_count == 0
        assert summary.calendar_is_organizer is False
        assert summary.calendar_rsvp_state == "unknown"
        assert summary.organizer_email is None
        assert summary.creator_email is None

    def test_bare_cancelled_stub_with_no_start_or_end(self):
        """The real shape of a deleted recurring instance: no start, no end,
        no summary (finding 5, round 4). Empty times, not all-day, not a
        crash."""
        summary = event_to_summary(CANCELLED_STUB, "primary")
        assert summary.id == CANCELLED_STUB["id"]
        assert summary.status == "cancelled"
        assert summary.start == ""
        assert summary.end == ""
        assert summary.is_all_day is False
        assert summary.summary == "Untitled Event"
        assert summary.calendar_rsvp_state == "unknown"

    def test_attendee_addresses_are_not_on_the_wire(self):
        """Brian's 2026-09-03 decision: organizer and creator addresses are
        exposed; the attendee list is still only a count."""
        event = colleague_copy()
        # Guard the assertion below against a fixture drift that would make
        # it vacuous: the address must really be in the attendee list.
        assert any(a["email"] == AUTH_USER_EMAIL for a in event["attendees"])
        dumped = event_to_summary(event, COLLEAGUE_EMAIL).model_dump_json()
        assert COLLEAGUE_EMAIL in dumped  # it is the calendar_id
        assert AUTH_USER_EMAIL not in dumped
        assert "attendees" not in dumped


class TestEventsListEndpoint:
    """Tests for GET /calendars/{calendar_id}/events."""

    def test_list_events_success(self, client, mock_proxy_client):
        """List events returns event summaries."""
        response = client.get("/calendars/primary/events")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["events"]) == 1
        assert data["events"][0]["summary"] == "Team Standup"

    def test_list_events_with_filters(self, client, mock_proxy_client):
        """List events accepts filter parameters."""
        response = client.get(
            "/calendars/primary/events",
            params={
                "time_min": "2024-01-01T00:00:00Z",
                "time_max": "2024-01-31T23:59:59Z",
                "q": "meeting",
                "max_results": 50,
            }
        )
        assert response.status_code == 200
        mock_proxy_client.list_events.assert_called_once()
        call_kwargs = mock_proxy_client.list_events.call_args.kwargs
        assert call_kwargs["time_min"] == "2024-01-01T00:00:00Z"
        assert call_kwargs["q"] == "meeting"

    def test_list_events_single_events_default_true(self, client, mock_proxy_client):
        """List events defaults to singleEvents=true for recurring expansion."""
        client.get("/calendars/primary/events")
        call_kwargs = mock_proxy_client.list_events.call_args.kwargs
        assert call_kwargs["single_events"] is True

    def test_list_event_without_attendees_counts_zero(self, client, mock_proxy_client):
        """An event with no attendees key is a count of 0 on the wire, not an
        error and not a phantom count (finding 9, round 3)."""
        mock_proxy_client.list_events.return_value = {"items": [SAMPLE_EVENTS["all_day_event"]]}
        response = client.get("/calendars/primary/events")
        assert response.status_code == 200
        assert response.json()["events"][0]["attendee_count"] == 0

    def test_list_survives_a_malformed_event_row(self, client, mock_proxy_client):
        """A row with a non-dict organizer and a non-dict attendee entry is
        served as null/unknown rather than 500ing the page (finding 8,
        round 3). Only organizer, creator and attendee rows are guarded."""
        bad = {
            **SAMPLE_EVENTS["basic_meeting"],
            "organizer": "dave@example.com",
            "attendees": [None],
        }
        mock_proxy_client.list_events.return_value = {"items": [bad]}
        response = client.get("/calendars/primary/events")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["events"]) == 1
        assert data["events"][0]["calendar_rsvp_state"] == "unknown"
        assert data["events"][0]["attendee_count"] == 0

    def test_list_serves_a_bare_cancelled_stub(self, client, mock_proxy_client):
        """A plain GET with single_events=false returns stubs without
        start/end; the page must still be 200 (finding 5, round 4)."""
        mock_proxy_client.list_events.return_value = {"items": [CANCELLED_STUB]}
        response = client.get("/calendars/primary/events", params={"single_events": "false"})
        assert response.status_code == 200
        row = response.json()["events"][0]
        assert row["status"] == "cancelled"
        assert row["start"] == "" and row["end"] == ""
        assert row["is_all_day"] is False

    def test_list_events_empty(self, client, mock_proxy_client):
        """List events handles empty results."""
        mock_proxy_client.list_events.return_value = {"items": []}
        response = client.get("/calendars/primary/events")
        data = response.json()
        assert data["success"] is True
        assert len(data["events"]) == 0

    def test_list_events_with_pagination_token(self, client, mock_proxy_client):
        """List events returns pagination token when available."""
        mock_proxy_client.list_events.return_value = {
            "items": [],
            "nextPageToken": "next_page_123",
        }
        response = client.get("/calendars/primary/events")
        data = response.json()
        assert data["next_page_token"] == "next_page_123"


class TestRsvpFieldsOnTheWire:
    """The organizer/RSVP fields as list and search actually emit them."""

    def test_list_on_primary(self, client, mock_proxy_client):
        mock_proxy_client.list_events.return_value = {"items": [SAMPLE_EVENTS["invitation"]]}
        response = client.get("/calendars/primary/events")
        assert response.status_code == 200
        event = response.json()["events"][0]
        assert event["organizer_email"] == "dave@example.com"
        assert event["creator_email"] == "dave@example.com"
        assert event["calendar_is_organizer"] is False
        assert event["calendar_rsvp_state"] == "needsAction"
        assert event["status"] == "confirmed"
        assert "is_organizer" not in event
        assert "response_status" not in event
        assert "calendar_response_status" not in event

    def test_list_on_colleague_calendar_reports_the_colleagues_rsvp(
        self, client, mock_proxy_client
    ):
        mock_proxy_client.list_events.return_value = {"items": [SAMPLE_EVENTS["colleague_copy"]]}
        response = client.get("/calendars/carol@example.com/events")
        assert response.status_code == 200
        event = response.json()["events"][0]
        assert event["calendar_id"] == "carol@example.com"
        assert event["calendar_rsvp_state"] == "accepted"  # Carol's, not john.doe's "declined"
        assert not any(k.startswith("user_") for k in event)

    def test_search_on_colleague_calendar_reports_the_colleagues_rsvp(
        self, client, mock_proxy_client
    ):
        mock_proxy_client.list_events.return_value = {"items": [SAMPLE_EVENTS["colleague_copy"]]}
        response = client.post(
            "/search", json={"calendar_id": "carol@example.com", "filters": {"query": "Budget"}}
        )
        assert response.status_code == 200
        event = response.json()["events"][0]
        assert event["calendar_is_organizer"] is False
        assert event["calendar_rsvp_state"] == "accepted"

    def test_read_needs_no_extra_proxy_call(self, client, mock_proxy_client):
        """Deriving the calendar's perspective is local: one proxy call per
        list, nothing to resolve the caller's identity."""
        mock_proxy_client.list_events.return_value = {"items": [SAMPLE_EVENTS["colleague_copy"]]}
        assert client.get("/calendars/carol@example.com/events").status_code == 200
        assert mock_proxy_client.list_events.await_count == 1
        assert mock_proxy_client.get_calendar.await_count == 0


class TestEventCreateEndpoint:
    """Tests for POST /calendars/{calendar_id}/events."""

    def test_create_event_success(self, client, mock_proxy_client):
        """Create event returns created event."""
        event_data = {
            "summary": "New Meeting",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T11:00:00Z"},
        }
        response = client.post("/calendars/primary/events", json=event_data)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["event"] is not None

    def test_create_event_minimal(self, client, mock_proxy_client):
        """Create event with minimal data."""
        event_data = {
            "summary": "Quick Note",
        }
        response = client.post("/calendars/primary/events", json=event_data)
        assert response.status_code == 200

    def test_create_event_with_attendees(self, client, mock_proxy_client):
        """Create event with attendees."""
        event_data = {
            "summary": "Team Meeting",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T11:00:00Z"},
            "attendees": [
                {"email": "alice@example.com"},
                {"email": "bob@example.com", "optional": True},
            ],
        }
        response = client.post("/calendars/primary/events", json=event_data)
        assert response.status_code == 200

    def test_create_event_with_send_updates(self, client, mock_proxy_client):
        """Create event with sendUpdates parameter."""
        event_data = {"summary": "Meeting"}
        client.post(
            "/calendars/primary/events?send_updates=all",
            json=event_data,
        )
        mock_proxy_client.create_event.assert_called_once()


class TestEventGetEndpoint:
    """Tests for GET /calendars/{calendar_id}/events/{event_id}."""

    def test_get_event_success(self, client, mock_proxy_client):
        """Get event returns full event details."""
        response = client.get("/calendars/primary/events/event_123")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["event"]["id"] == "meeting_001"

    def test_get_event_returns_the_full_google_event(self, client, mock_proxy_client):
        """The detail route is not a summary: description and the attendee
        list with addresses come back as the proxy sent them (the README's
        privacy section says exactly this)."""
        mock_proxy_client.get_event.return_value = SAMPLE_EVENTS["invitation"]
        event = client.get("/calendars/primary/events/invite_001").json()["event"]
        assert event["description"] == "Quarterly budget walkthrough"
        assert any(a["email"] == AUTH_USER_EMAIL for a in event["attendees"])

    def test_get_event_detail_has_empty_warnings(self, client, mock_proxy_client):
        """`warnings` is on every EventDetailResponse (added for /respond);
        the other detail routes emit an empty list."""
        response = client.get("/calendars/primary/events/event_123")
        assert response.status_code == 200
        assert response.json()["warnings"] == []

    def test_get_event_with_timezone(self, client, mock_proxy_client):
        """Get event with timezone parameter."""
        client.get("/calendars/primary/events/event_123?time_zone=America/New_York")
        call_kwargs = mock_proxy_client.get_event.call_args.kwargs
        assert call_kwargs["time_zone"] == "America/New_York"

    def test_get_event_not_found(self, client, mock_proxy_client):
        """Get event handles not found."""
        mock_proxy_client.get_event.side_effect = ProxyError("Event not found")
        response = client.get("/calendars/primary/events/nonexistent")
        data = response.json()
        assert data["success"] is False


class TestEventUpdateEndpoint:
    """Tests for PUT /calendars/{calendar_id}/events/{event_id}."""

    def test_update_event_success(self, client, mock_proxy_client):
        """Update event returns updated event."""
        event_data = {
            "summary": "Updated Meeting",
            "start": {"dateTime": "2024-01-15T14:00:00Z"},
            "end": {"dateTime": "2024-01-15T15:00:00Z"},
        }
        response = client.put("/calendars/primary/events/event_123", json=event_data)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True


class TestEventPatchEndpoint:
    """Tests for PATCH /calendars/{calendar_id}/events/{event_id}."""

    def test_patch_event_success(self, client, mock_proxy_client):
        """Patch event with partial update."""
        patch_data = {"summary": "Renamed Meeting"}
        response = client.patch("/calendars/primary/events/event_123", json=patch_data)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_patch_event_only_changed_fields(self, client, mock_proxy_client):
        """Patch sends only the changed fields."""
        patch_data = {"location": "New Room"}
        client.patch("/calendars/primary/events/event_123", json=patch_data)
        call_kwargs = mock_proxy_client.patch_event.call_args.kwargs
        assert "location" in call_kwargs["event_data"]


class TestEventDeleteEndpoint:
    """Tests for DELETE /calendars/{calendar_id}/events/{event_id}."""

    def test_delete_event_success(self, client, mock_proxy_client):
        """Delete event returns success once the re-read shows it gone."""
        mock_proxy_client.get_event.side_effect = ProxyNotFoundError("Not Found")
        response = client.delete("/calendars/primary/events/event_123")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["outcome"] == "succeeded"
        assert "deleted" in data["message"].lower()

    def test_delete_is_verified_by_re_reading_the_event(self, client, mock_proxy_client):
        """The proxy's answer is a claim; the re-read is the evidence (F4)."""
        mock_proxy_client.get_event.side_effect = ProxyNotFoundError("Not Found")
        client.delete("/calendars/primary/events/event_123")
        assert [c[0] for c in mock_proxy_client.mock_calls] == ["delete_event", "get_event"]
        assert mock_proxy_client.get_event.call_args.args[:2] == ("primary", "event_123")

    def test_cancelled_status_on_re_read_is_success(self, client, mock_proxy_client):
        mock_proxy_client.get_event.return_value = {"id": "event_123", "status": "cancelled"}
        response = client.delete("/calendars/primary/events/event_123")
        assert response.status_code == 200
        assert response.json()["outcome"] == "succeeded"

    def test_success_claim_contradicted_by_re_read_is_failure(self, client, mock_proxy_client):
        """The 2026-08-07 incident: the proxy said 200 for an event still
        confirmed. Fixture default: get_event returns a confirmed event."""
        response = client.delete("/calendars/primary/events/event_123")
        assert response.status_code == 502
        data = response.json()
        assert data["success"] is False
        assert data["outcome"] == "failed"
        assert "still present" in data["error"]

    def test_delete_event_rejected_by_operator(self, client, mock_proxy_client):
        """An operator rejection surfaces as 403 with the rejection message."""
        mock_proxy_client.delete_event.side_effect = ProxyForbiddenError(
            "Request rejected by operator"
        )
        response = client.delete("/calendars/primary/events/event_123")
        assert response.status_code == 403
        data = response.json()
        assert data["success"] is False
        assert "rejected" in data["message"].lower()
        assert "rejected by operator" in data["error"]

    def test_delete_event_timeout_outcome_unknown(self, client, mock_proxy_client):
        """A timed-out delete with the event still present is 504/unknown."""
        mock_proxy_client.delete_event.side_effect = ProxyTimeoutError(
            "No response from proxy after 330s"
        )
        response = client.delete("/calendars/primary/events/event_123")
        assert response.status_code == 504
        data = response.json()
        assert data["success"] is False
        assert data["outcome"] == "unknown"
        assert "unknown" in data["message"].lower()

    def test_delete_timeout_but_event_gone_is_success(self, client, mock_proxy_client):
        """Approved after this server gave up, but before the re-read: the
        deletion did happen, and saying 504 would invite a compensating
        mutation against an event that no longer exists."""
        mock_proxy_client.delete_event.side_effect = ProxyTimeoutError("no response")
        mock_proxy_client.get_event.side_effect = ProxyNotFoundError("Not Found")
        response = client.delete("/calendars/primary/events/event_123")
        assert response.status_code == 200
        assert response.json()["outcome"] == "succeeded"

    def test_delete_timeout_and_unreadable_re_read_is_unknown(
        self, client, mock_proxy_client
    ):
        mock_proxy_client.delete_event.side_effect = ProxyTimeoutError("no response")
        mock_proxy_client.get_event.side_effect = ProxyError("proxy down")
        response = client.delete("/calendars/primary/events/event_123")
        assert response.status_code == 504
        assert response.json()["outcome"] == "unknown"

    def test_rejection_is_failed_without_a_re_read(self, client, mock_proxy_client):
        """A 403 is the proxy saying it dropped the request: definitive."""
        mock_proxy_client.delete_event.side_effect = ProxyForbiddenError("rejected")
        response = client.delete("/calendars/primary/events/event_123")
        assert response.status_code == 403
        assert response.json()["outcome"] == "failed"
        mock_proxy_client.get_event.assert_not_called()

    def test_nonexistent_event_is_404_failed(self, client, mock_proxy_client):
        mock_proxy_client.delete_event.side_effect = ProxyNotFoundError("Not Found")
        response = client.delete("/calendars/primary/events/event_123")
        assert response.status_code == 404
        data = response.json()
        assert data["success"] is False
        assert data["outcome"] == "failed"
        mock_proxy_client.get_event.assert_not_called()


class TestEventRespondEndpoint:
    """Tests for POST /calendars/{calendar_id}/events/{event_id}/respond."""

    def test_respond_success(self, client, mock_proxy_client):
        """RSVP forwards to the proxy client and returns the updated event."""
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

    def test_respond_on_primary_carries_no_warning(self, client, mock_proxy_client):
        resp = client.post(
            "/calendars/primary/events/e1/respond",
            json={"response_status": "accepted"},
        )
        assert resp.status_code == 200
        assert resp.json()["warnings"] == []

    def test_respond_error_envelope_has_no_warnings(self, client, mock_proxy_client):
        mock_proxy_client.respond_to_event.side_effect = ProxyForbiddenError("blocked")
        resp = client.post(
            "/calendars/primary/events/e1/respond",
            json={"response_status": "accepted"},
        )
        assert resp.status_code == 403
        assert resp.json()["warnings"] == []


class TestEventRespondRefusesForeignCalendars:
    """POST .../respond refuses any calendar_id that is not the authenticated
    user's own (Brian's decision, 2026-09-04).

    The read fields describe the calendar being read; /respond always writes
    the authenticated user's entry. Refusing keeps the two on the same
    calendar, where they agree.
    """

    def test_refuses_a_group_calendar_id(self, client, mock_proxy_client):
        """A group calendar is never the authenticated account's own."""
        mock_proxy_client.get_calendar.return_value = {"id": AUTH_USER_EMAIL}
        resp = client.post(
            f"/calendars/{GROUP_CALENDAR_ID}/events/invite_001/respond",
            json={"response_status": "accepted"},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["success"] is False
        assert GROUP_CALENDAR_ID in data["error"]
        assert "primary" in data["error"]
        mock_proxy_client.respond_to_event.assert_not_called()

    def test_refuses_a_colleagues_address(self, client, mock_proxy_client):
        mock_proxy_client.get_calendar.return_value = {"id": AUTH_USER_EMAIL}
        resp = client.post(
            f"/calendars/{COLLEAGUE_EMAIL}/events/invite_001/respond",
            json={"response_status": "declined"},
        )
        assert resp.status_code == 400
        assert COLLEAGUE_EMAIL in resp.json()["error"]
        mock_proxy_client.respond_to_event.assert_not_called()

    def test_refusal_envelope_has_no_event_and_no_warnings(self, client, mock_proxy_client):
        mock_proxy_client.get_calendar.return_value = {"id": AUTH_USER_EMAIL}
        resp = client.post(
            f"/calendars/{COLLEAGUE_EMAIL}/events/invite_001/respond",
            json={"response_status": "declined"},
        )
        assert resp.json()["event"] is None
        assert resp.json()["warnings"] == []

    def test_allows_the_literal_primary_without_resolving_an_address(
        self, client, mock_proxy_client
    ):
        """'primary' needs no identity lookup, so the common path costs no
        extra proxy call."""
        resp = client.post(
            "/calendars/primary/events/e1/respond",
            json={"response_status": "accepted"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert mock_proxy_client.get_calendar.await_count == 0

    def test_literal_primary_is_matched_case_insensitively(self, client, mock_proxy_client):
        """'Primary' takes the same no-lookup fast path as 'primary': the
        address comparison already casefolds, so the literal must too
        (Opus delta review, finding 2)."""
        mock_proxy_client.get_calendar.return_value = {"id": AUTH_USER_EMAIL}
        resp = client.post(
            "/calendars/Primary/events/e1/respond",
            json={"response_status": "accepted"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert mock_proxy_client.get_calendar.await_count == 0

    def test_allows_the_authenticated_users_own_address(self, client, mock_proxy_client):
        mock_proxy_client.get_calendar.return_value = {"id": AUTH_USER_EMAIL}
        resp = client.post(
            f"/calendars/{AUTH_USER_EMAIL}/events/invite_001/respond",
            json={"response_status": "accepted"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        mock_proxy_client.respond_to_event.assert_called_once_with(
            AUTH_USER_EMAIL, "invite_001", "accepted"
        )

    def test_own_address_matches_case_insensitively(self, client, mock_proxy_client):
        """Google lowercases calendar ids; a caller's capitalisation must not
        turn an allowed RSVP into a refusal."""
        mock_proxy_client.get_calendar.return_value = {"id": AUTH_USER_EMAIL}
        resp = client.post(
            f"/calendars/{AUTH_USER_EMAIL.upper()}/events/invite_001/respond",
            json={"response_status": "accepted"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_resolves_the_authenticated_address_once_per_process(
        self, client, mock_proxy_client
    ):
        """The identity lookup is cached: two RSVPs, one GET /calendars/primary."""
        mock_proxy_client.get_calendar.return_value = {"id": AUTH_USER_EMAIL}
        for _ in range(2):
            client.post(
                f"/calendars/{AUTH_USER_EMAIL}/events/invite_001/respond",
                json={"response_status": "accepted"},
            )
        assert mock_proxy_client.get_calendar.await_count == 1

    def test_unresolvable_identity_is_an_upstream_failure_not_a_refusal(
        self, client, mock_proxy_client
    ):
        """If the proxy's primary calendar carries no id there is nothing to
        compare against: 502, and the RSVP is not forwarded."""
        mock_proxy_client.get_calendar.return_value = {"summary": "no id here"}
        resp = client.post(
            f"/calendars/{AUTH_USER_EMAIL}/events/invite_001/respond",
            json={"response_status": "accepted"},
        )
        assert resp.status_code == 502
        assert resp.json()["success"] is False
        mock_proxy_client.respond_to_event.assert_not_called()

    def test_identity_lookup_timeout_is_an_upstream_failure_not_an_unknown_outcome(
        self, client, mock_proxy_client
    ):
        """A read timeout on GET /calendars/primary happens before anything is
        sent, so it must not be reported as a mutation whose outcome is
        unknown (Opus delta review, finding 1): 502, the message says the
        ownership check could not be performed, and no RSVP is forwarded."""
        mock_proxy_client.get_calendar.side_effect = ProxyTimeoutError(
            "No response from proxy after 30s"
        )
        resp = client.post(
            f"/calendars/{AUTH_USER_EMAIL}/events/invite_001/respond",
            json={"response_status": "accepted"},
        )
        assert resp.status_code == 502
        data = resp.json()
        assert data["success"] is False
        assert "Outcome unknown" not in data["error"]
        assert "ownership check" in data["error"]
        assert "nothing was sent" in data["error"]
        mock_proxy_client.respond_to_event.assert_not_awaited()

    def test_identity_lookup_not_found_is_an_upstream_failure_not_a_404(
        self, client, mock_proxy_client
    ):
        """A 404 on GET /calendars/primary is not "no such event": the URL
        names an event this route never looked at. 502, nothing sent."""
        mock_proxy_client.get_calendar.side_effect = ProxyNotFoundError(
            "Calendar not found"
        )
        resp = client.post(
            f"/calendars/{AUTH_USER_EMAIL}/events/invite_001/respond",
            json={"response_status": "accepted"},
        )
        assert resp.status_code == 502
        assert resp.json()["success"] is False
        assert "ownership check" in resp.json()["error"]
        mock_proxy_client.respond_to_event.assert_not_awaited()

    @pytest.mark.parametrize(
        "lookup_failure",
        [
            ProxyForbiddenError("blocked"),
            ProxyRequestError(429, "slow down"),
            ProxyAuthError("bad key"),
            ProxyError("connection refused"),
        ],
        ids=["forbidden", "other-4xx", "auth", "generic"],
    )
    def test_any_identity_lookup_failure_is_502_with_nothing_sent(
        self, client, mock_proxy_client, lookup_failure
    ):
        """Pins the documented guarantee: whatever the proxy does to the
        identity read, the caller sees 502 and no RSVP was attempted. A
        forbidden here would otherwise read as "the operator rejected your
        RSVP" for a read no operator ever saw."""
        mock_proxy_client.get_calendar.side_effect = lookup_failure
        resp = client.post(
            f"/calendars/{AUTH_USER_EMAIL}/events/invite_001/respond",
            json={"response_status": "accepted"},
        )
        assert resp.status_code == 502
        assert resp.json()["success"] is False
        assert "nothing was sent" in resp.json()["error"]
        mock_proxy_client.respond_to_event.assert_not_awaited()

    def test_respond_invalid_status_rejected(self, client, mock_proxy_client):
        """Values outside accepted/declined/tentative are rejected with 422."""
        resp = client.post(
            "/calendars/primary/events/e1/respond",
            json={"response_status": "maybe"},
        )
        assert resp.status_code == 422
        mock_proxy_client.respond_to_event.assert_not_called()

    def test_respond_forbidden_returns_error(self, client, mock_proxy_client):
        """Proxy 403 surfaces as success=false with an error message."""
        mock_proxy_client.respond_to_event.side_effect = ProxyForbiddenError("blocked")
        resp = client.post(
            "/calendars/primary/events/e1/respond",
            json={"response_status": "accepted"},
        )
        assert resp.status_code == 403
        data = resp.json()
        assert data["success"] is False
        assert data["error"]

    def test_respond_timeout_returns_504(self, client, mock_proxy_client):
        """A timed-out RSVP surfaces as 504 with outcome-unknown error text."""
        mock_proxy_client.respond_to_event.side_effect = ProxyTimeoutError(
            "No response from proxy after 330s"
        )
        resp = client.post(
            "/calendars/primary/events/e1/respond",
            json={"response_status": "accepted"},
        )
        assert resp.status_code == 504
        data = resp.json()
        assert data["success"] is False
        assert "Outcome unknown" in data["error"]

    def test_respond_not_an_attendee_passes_the_proxys_400_through(self, client, mock_proxy_client):
        """The proxy's 400 ("not an attendee") reaches the caller as 400 with
        the proxy's message in ``error`` (finding 3, round 4) -- not 502."""
        mock_proxy_client.respond_to_event.side_effect = ProxyRequestError(
            400, "You are not an attendee of this event; cannot RSVP."
        )
        resp = client.post(
            "/calendars/primary/events/e1/respond",
            json={"response_status": "accepted"},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["success"] is False
        assert "You are not an attendee of this event; cannot RSVP." in data["error"]


# ============================================================================
# Proxy Client Tests
# ============================================================================


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

    async def _call(self, mock_response, calendar_id, event_id, status):
        client = CalendarProxyClient(proxy_url="http://proxy", api_key="k")
        with patch("calendar_agent.proxy_client.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await client.respond_to_event(calendar_id, event_id, status)
        return result, mock_client

    async def test_posts_to_respond_path_with_status(self, mock_response):
        result, mock_client = await self._call(mock_response, "robergb@dm.org", "e1", "accepted")

        mock_client.post.assert_called_once()
        call = mock_client.post.call_args
        assert call.args[0] == "http://proxy/calendar/v3/calendars/robergb@dm.org/events/e1/respond"
        assert call.kwargs["json"] == {"responseStatus": "accepted"}
        assert result["id"] == "e1"

    async def test_encodes_special_characters_in_path(self, mock_response):
        """A '#' in the calendar ID must be percent-encoded, not left as a fragment."""
        _, mock_client = await self._call(
            mock_response, "en.usa#holiday@group.v.calendar.google.com", "e1", "declined"
        )

        url = mock_client.post.call_args.args[0]
        assert "%23" in url
        assert "#" not in url
        assert url == (
            "http://proxy/calendar/v3/calendars/"
            "en.usa%23holiday@group.v.calendar.google.com/events/e1/respond"
        )


class TestProxyClientPathQuoting:
    """Every event route must percent-encode its path segments (F9).

    Google's built-in calendar ids contain ``#`` (``#contacts@group.v...``,
    ``en.usa#holiday@...``). Unquoted, httpx reads the ``#`` as a fragment and
    the request goes to ``/calendars/`` — a different route, whose 404 now maps
    to "the event does not exist" instead of a recognisable upstream error.
    """

    CAL = "#contacts@group.v.calendar.google.com"
    EXPECTED = (
        "http://proxy/calendar/v3/calendars/"
        "%23contacts@group.v.calendar.google.com/events/e%2F1"
    )

    @pytest.fixture
    def ok_response(self):
        r = AsyncMock(spec=httpx.Response)
        r.status_code = 200
        r.json.return_value = {"id": "e/1"}
        return r

    @pytest.mark.parametrize(
        ("method", "call"),
        [
            ("get", lambda c, cal: c.get_event(cal, "e/1")),
            ("put", lambda c, cal: c.update_event(cal, "e/1", {"summary": "x"})),
            ("patch", lambda c, cal: c.patch_event(cal, "e/1", {"summary": "x"})),
            ("delete", lambda c, cal: c.delete_event(cal, "e/1")),
        ],
    )
    async def test_event_routes_quote_both_segments(self, ok_response, method, call):
        client = CalendarProxyClient(proxy_url="http://proxy", api_key="k")
        with patch("calendar_agent.proxy_client.httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            getattr(mock_http, method).return_value = ok_response
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await call(client, self.CAL)
        url = getattr(mock_http, method).call_args.args[0]
        assert url == self.EXPECTED
        assert httpx.URL(url).raw_path.decode().endswith("/events/e%2F1")

    async def test_proxy_410_is_not_found(self):
        """Google answers 410 Gone for a deleted event; that is "absent", the
        expected answer when verifying a delete, not a generic proxy error."""
        client = CalendarProxyClient(proxy_url="http://proxy", api_key="k")
        response = AsyncMock(spec=httpx.Response)
        response.status_code = 410
        response.json.return_value = {"detail": "Resource has been deleted"}
        with pytest.raises(ProxyNotFoundError):
            client._handle_response(response)


class TestProxyClientTimeouts:
    """Mutations must outlive the proxy's 300s confirmation window (issue #4)."""

    def _mock_http(self, mock_cls, method: str, response=None, side_effect=None):
        mock_http = AsyncMock()
        if response is not None:
            getattr(mock_http, method).return_value = response
        if side_effect is not None:
            getattr(mock_http, method).side_effect = side_effect
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        return mock_http

    @pytest.fixture
    def ok_response(self):
        r = AsyncMock(spec=httpx.Response)
        r.status_code = 200
        r.json.return_value = {"id": "e1"}
        return r

    def test_confirm_timeout_outlives_confirmation_window(self):
        """The mutation timeout must exceed the proxy's 300s approval window."""
        assert CONFIRM_TIMEOUT > 300

    async def test_mutating_call_uses_confirm_timeout(self, ok_response):
        client = CalendarProxyClient(proxy_url="http://proxy", api_key="k")
        with patch("calendar_agent.proxy_client.httpx.AsyncClient") as mock_cls:
            self._mock_http(mock_cls, "post", response=ok_response)
            await client.respond_to_event("primary", "e1", "accepted")
        assert mock_cls.call_args.kwargs["timeout"] == httpx.Timeout(
            CONFIRM_TIMEOUT, connect=READ_TIMEOUT
        )

    async def test_read_call_uses_read_timeout(self, ok_response):
        client = CalendarProxyClient(proxy_url="http://proxy", api_key="k")
        with patch("calendar_agent.proxy_client.httpx.AsyncClient") as mock_cls:
            self._mock_http(mock_cls, "get", response=ok_response)
            await client.get_event("primary", "e1")
        assert mock_cls.call_args.kwargs["timeout"] == httpx.Timeout(
            READ_TIMEOUT, connect=READ_TIMEOUT
        )

    async def test_timeout_raises_proxy_timeout_error(self):
        """An httpx timeout surfaces as ProxyTimeoutError with unknown-outcome text."""
        client = CalendarProxyClient(proxy_url="http://proxy", api_key="k")
        with patch("calendar_agent.proxy_client.httpx.AsyncClient") as mock_cls:
            self._mock_http(mock_cls, "delete", side_effect=httpx.ReadTimeout("timed out"))
            with pytest.raises(ProxyTimeoutError) as exc_info:
                await client.delete_event("primary", "e1")
        assert "outcome is unknown" in str(exc_info.value)

    async def test_connect_timeout_is_definitive_failure(self):
        """A connect-phase timeout never reached the proxy: ProxyError, not
        outcome-unknown ProxyTimeoutError."""
        client = CalendarProxyClient(proxy_url="http://proxy", api_key="k")
        with patch("calendar_agent.proxy_client.httpx.AsyncClient") as mock_cls:
            self._mock_http(mock_cls, "post", side_effect=httpx.ConnectTimeout("connect"))
            with pytest.raises(ProxyError) as exc_info:
                await client.create_event("primary", {"summary": "s"})
        assert not isinstance(exc_info.value, ProxyTimeoutError)
        assert "Could not connect" in str(exc_info.value)

    async def test_connection_error_is_proxy_error(self):
        """A down/unreachable proxy surfaces as ProxyError (502), not a raw
        httpx exception (500)."""
        client = CalendarProxyClient(proxy_url="http://proxy", api_key="k")
        with patch("calendar_agent.proxy_client.httpx.AsyncClient") as mock_cls:
            self._mock_http(
                mock_cls, "get", side_effect=httpx.ConnectError("All connection attempts failed")
            )
            with pytest.raises(ProxyError) as exc_info:
                await client.get_event("primary", "e1")
        assert not isinstance(exc_info.value, ProxyTimeoutError)
        assert "Proxy connection failed" in str(exc_info.value)


class TestProxyClientRequestErrors:
    """A proxy 4xx other than 401/403/404/410 raises ProxyRequestError carrying
    the upstream status and message (finding 3, round 4). 404 and 410 stay
    ProxyNotFoundError, which delete verification depends on (issue #4)."""

    def _response(self, status: int, detail: str):
        r = AsyncMock(spec=httpx.Response)
        r.status_code = status
        r.json.return_value = {"detail": detail}
        return r

    def test_400_carries_status_and_message(self):
        client = CalendarProxyClient(proxy_url="http://proxy", api_key="k")
        with pytest.raises(ProxyRequestError) as exc_info:
            client._handle_response(
                self._response(400, "You are not an attendee of this event; cannot RSVP.")
            )
        assert exc_info.value.status_code == 400
        assert str(exc_info.value) == "You are not an attendee of this event; cannot RSVP."

    def test_404_is_not_found_error_not_request_error(self):
        client = CalendarProxyClient(proxy_url="http://proxy", api_key="k")
        with pytest.raises(ProxyNotFoundError) as exc_info:
            client._handle_response(self._response(404, "Not Found"))
        assert not isinstance(exc_info.value, ProxyRequestError)
        assert str(exc_info.value) == "Not Found"

    def test_other_4xx_is_still_a_request_error(self):
        client = CalendarProxyClient(proxy_url="http://proxy", api_key="k")
        with pytest.raises(ProxyRequestError) as exc_info:
            client._handle_response(self._response(409, "conflict"))
        assert exc_info.value.status_code == 409
        assert isinstance(exc_info.value, ProxyError)

    def test_401_and_403_keep_their_own_types(self):
        client = CalendarProxyClient(proxy_url="http://proxy", api_key="k")
        with pytest.raises(ProxyAuthError):
            client._handle_response(self._response(401, "bad key"))
        with pytest.raises(ProxyForbiddenError):
            client._handle_response(self._response(403, "rejected"))


# ============================================================================
# LLM Endpoint Tests - Basic
# ============================================================================


class TestSummarizeEndpoint:
    """Tests for POST /summarize."""

    def test_summarize_success(self, client, mock_proxy_client, mock_llm_service):
        """Summarize event returns AI summary."""
        request_data = {
            "calendar_id": "primary",
            "event_id": "event_123",
            "format": "brief",
        }
        response = client.post("/summarize", json=request_data)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "summary" in data["data"]

    def test_summarize_detailed_format(self, client, mock_proxy_client, mock_llm_service):
        """Summarize with detailed format."""
        request_data = {
            "calendar_id": "primary",
            "event_id": "event_123",
            "format": "detailed",
        }
        client.post("/summarize", json=request_data)
        mock_llm_service.summarize_event.assert_called_once()
        call_kwargs = mock_llm_service.summarize_event.call_args.kwargs
        assert call_kwargs["format"] == "detailed"

    def test_summarize_event_not_found(self, client, mock_proxy_client, mock_llm_service):
        """Summarize handles event not found."""
        mock_proxy_client.get_event.side_effect = ProxyError("Event not found")
        request_data = {
            "calendar_id": "primary",
            "event_id": "nonexistent",
        }
        response = client.post("/summarize", json=request_data)
        data = response.json()
        assert data["success"] is False


class TestAskAboutEndpoint:
    """Tests for POST /ask-about."""

    def test_ask_about_success(self, client, mock_proxy_client, mock_llm_service):
        """Ask about event returns AI answer."""
        request_data = {
            "calendar_id": "primary",
            "event_id": "event_123",
            "question": "What time is the meeting?",
        }
        response = client.post("/ask-about", json=request_data)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "answer" in data["data"]

    def test_ask_about_requires_question(self, client):
        """Ask about requires question field."""
        request_data = {
            "calendar_id": "primary",
            "event_id": "event_123",
        }
        response = client.post("/ask-about", json=request_data)
        assert response.status_code == 422  # Validation error


class TestBatchSummarizeEndpoint:
    """Tests for POST /batch-summarize."""

    def test_batch_summarize_success(self, client, mock_proxy_client, mock_llm_service):
        """Batch summarize returns summaries for multiple events."""
        request_data = {
            "calendar_id": "primary",
            "event_ids": ["event_1", "event_2", "event_3"],
            "triage": False,
        }
        response = client.post("/batch-summarize", json=request_data)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_batch_summarize_with_triage(self, client, mock_proxy_client, mock_llm_service):
        """Batch summarize with triage classification."""
        request_data = {
            "calendar_id": "primary",
            "event_ids": ["event_1"],
            "triage": True,
        }
        client.post("/batch-summarize", json=request_data)
        call_kwargs = mock_llm_service.batch_summarize.call_args.kwargs
        assert call_kwargs["triage"] is True

    def test_batch_summarize_continues_on_fetch_error(
        self, client, mock_proxy_client, mock_llm_service
    ):
        """Batch summarize continues if individual event fetch fails."""
        # First call succeeds, second fails
        mock_proxy_client.get_event.side_effect = [
            {"id": "event_1", "summary": "Event 1"},
            ProxyError("Event not found"),
        ]
        request_data = {
            "calendar_id": "primary",
            "event_ids": ["event_1", "event_2"],
        }
        response = client.post("/batch-summarize", json=request_data)
        data = response.json()
        assert data["success"] is True


# ============================================================================
# LLM Endpoint Tests - Calendar-Specific
# ============================================================================


class TestFindFreeTimeEndpoint:
    """Tests for POST /find-free-time."""

    def test_find_free_time_success(self, client, mock_proxy_client, mock_llm_service):
        """Find free time returns available slots and suggestions."""
        request_data = {
            "calendar_id": "primary",
            "time_min": "2024-01-15T09:00:00Z",
            "time_max": "2024-01-15T17:00:00Z",
            "duration_minutes": 30,
        }
        response = client.post("/find-free-time", json=request_data)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "suggestions" in data["data"]

    def test_find_free_time_with_preferences(
        self, client, mock_proxy_client, mock_llm_service
    ):
        """Find free time with scheduling preferences."""
        request_data = {
            "calendar_id": "primary",
            "time_min": "2024-01-15T09:00:00Z",
            "time_max": "2024-01-15T17:00:00Z",
            "duration_minutes": 60,
            "working_hours_only": True,
            "prefer_morning": True,
            "buffer_minutes": 15,
        }
        response = client.post("/find-free-time", json=request_data)
        assert response.status_code == 200

    def test_find_free_time_validation(self, client):
        """Find free time validates duration."""
        request_data = {
            "calendar_id": "primary",
            "time_min": "2024-01-15T09:00:00Z",
            "time_max": "2024-01-15T17:00:00Z",
            "duration_minutes": 0,  # Invalid
        }
        response = client.post("/find-free-time", json=request_data)
        assert response.status_code == 422


class TestAnalyzeScheduleEndpoint:
    """Tests for POST /analyze-schedule."""

    def test_analyze_schedule_success(self, client, mock_proxy_client, mock_llm_service):
        """Analyze schedule returns insights and metrics."""
        request_data = {
            "calendar_id": "primary",
            "time_min": "2024-01-15T00:00:00Z",
            "time_max": "2024-01-22T00:00:00Z",
            "analysis_type": "overview",
        }
        response = client.post("/analyze-schedule", json=request_data)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "insights" in data["data"]

    @pytest.mark.parametrize("analysis_type", ["overview", "workload", "patterns", "conflicts"])
    def test_analyze_schedule_types(
        self, client, mock_proxy_client, mock_llm_service, analysis_type
    ):
        """Analyze schedule supports different analysis types."""
        request_data = {
            "calendar_id": "primary",
            "time_min": "2024-01-15T00:00:00Z",
            "time_max": "2024-01-22T00:00:00Z",
            "analysis_type": analysis_type,
        }
        response = client.post("/analyze-schedule", json=request_data)
        assert response.status_code == 200


class TestPrepareBriefingEndpoint:
    """Tests for POST /prepare-briefing."""

    def test_prepare_briefing_daily(self, client, mock_proxy_client, mock_llm_service):
        """Prepare daily briefing returns schedule overview."""
        request_data = {
            "calendar_id": "primary",
            "briefing_type": "daily",
        }
        response = client.post("/prepare-briefing", json=request_data)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "briefing" in data["data"]

    def test_prepare_briefing_weekly(self, client, mock_proxy_client, mock_llm_service):
        """Prepare weekly briefing."""
        request_data = {
            "calendar_id": "primary",
            "briefing_type": "weekly",
        }
        response = client.post("/prepare-briefing", json=request_data)
        assert response.status_code == 200

    def test_prepare_briefing_custom_range(
        self, client, mock_proxy_client, mock_llm_service
    ):
        """Prepare briefing with custom time range."""
        request_data = {
            "calendar_id": "primary",
            "briefing_type": "daily",
            "time_min": "2024-01-15T00:00:00Z",
            "time_max": "2024-01-15T23:59:59Z",
        }
        response = client.post("/prepare-briefing", json=request_data)
        assert response.status_code == 200


# ============================================================================
# Operations Endpoint Tests
# ============================================================================


class TestSearchEndpoint:
    """Tests for POST /search."""

    def test_search_success(self, client, mock_proxy_client):
        """Search events with filters."""
        request_data = {
            "calendar_id": "primary",
            "filters": {
                "query": "meeting",
                "time_min": "2024-01-01T00:00:00Z",
                "time_max": "2024-01-31T23:59:59Z",
            },
        }
        response = client.post("/search", json=request_data)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "events" in data

    def test_search_with_all_filters(self, client, mock_proxy_client):
        """Search with all available filters."""
        request_data = {
            "calendar_id": "primary",
            "filters": {
                "query": "project",
                "time_min": "2024-01-01T00:00:00Z",
                "time_max": "2024-12-31T23:59:59Z",
                "max_results": 50,
                "order_by": "startTime",
                "show_deleted": False,
            },
        }
        response = client.post("/search", json=request_data)
        assert response.status_code == 200

    def test_search_show_deleted_forwards_and_classifies_cancelled_rows(
        self, client, mock_proxy_client
    ):
        """show_deleted is forwarded with single_events fixed to True, and a
        cancelled row that carries organizer/attendees is classified like
        any other row -- not blanked to 'unknown' (finding 2, round 4)."""
        cancelled = colleague_copy()
        cancelled["status"] = "cancelled"
        mock_proxy_client.list_events.return_value = {"items": [cancelled]}
        response = client.post(
            "/search",
            json={"calendar_id": COLLEAGUE_EMAIL, "filters": {"show_deleted": True}},
        )
        assert response.status_code == 200
        kwargs = mock_proxy_client.list_events.call_args.kwargs
        assert kwargs["show_deleted"] is True
        assert kwargs["single_events"] is True
        row = response.json()["events"][0]
        assert row["status"] == "cancelled"
        assert row["organizer_email"] == "dave@example.com"
        assert row["calendar_rsvp_state"] == "accepted"
        assert row["start"] != ""

    def test_search_default_filters(self, client, mock_proxy_client):
        """Search with default filters."""
        request_data = {
            "calendar_id": "primary",
            "filters": {},
        }
        response = client.post("/search", json=request_data)
        assert response.status_code == 200


class TestBulkActionsEndpoint:
    """Tests for POST /bulk-actions."""

    def test_bulk_delete_success(self, client, mock_proxy_client):
        """Bulk delete events."""
        mock_proxy_client.get_event.side_effect = ProxyNotFoundError("Not Found")
        request_data = {
            "operations": [
                {"operation": "delete", "event_id": "event_1", "calendar_id": "primary"},
                {"operation": "delete", "event_id": "event_2", "calendar_id": "primary"},
            ]
        }
        response = client.post("/bulk-actions", json=request_data)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["success_count"] == 2
        assert data["error_count"] == 0

    def test_bulk_patch_success(self, client, mock_proxy_client):
        """Bulk patch events."""
        request_data = {
            "operations": [
                {
                    "operation": "patch",
                    "event_id": "event_1",
                    "calendar_id": "primary",
                    "updates": {"summary": "Updated Title"},
                },
            ]
        }
        response = client.post("/bulk-actions", json=request_data)
        data = response.json()
        assert data["success"] is True

    def test_bulk_mixed_operations(self, client, mock_proxy_client):
        """Bulk operations with mixed types."""
        mock_proxy_client.get_event.side_effect = ProxyNotFoundError("Not Found")
        request_data = {
            "operations": [
                {"operation": "delete", "event_id": "event_1", "calendar_id": "primary"},
                {
                    "operation": "patch",
                    "event_id": "event_2",
                    "calendar_id": "primary",
                    "updates": {"location": "New Room"},
                },
            ]
        }
        response = client.post("/bulk-actions", json=request_data)
        assert response.status_code == 200

    def test_bulk_partial_failure(self, client, mock_proxy_client):
        """Bulk operations continue on individual failures."""
        # First succeeds, second fails
        mock_proxy_client.delete_event.side_effect = [
            {"success": True},
            ProxyError("Event not found"),
        ]
        mock_proxy_client.get_event.side_effect = [
            ProxyNotFoundError("Not Found"),
            {"id": "event_2", "status": "confirmed"},
        ]
        request_data = {
            "operations": [
                {"operation": "delete", "event_id": "event_1", "calendar_id": "primary"},
                {"operation": "delete", "event_id": "event_2", "calendar_id": "primary"},
            ]
        }
        response = client.post("/bulk-actions", json=request_data)
        data = response.json()
        assert data["success"] is False  # one operation did not happen
        assert data["success_count"] == 1
        assert data["error_count"] == 1

    def test_bulk_update_without_data(self, client, mock_proxy_client):
        """Bulk update without update data is a validation error (422)."""
        request_data = {
            "operations": [
                {"operation": "update", "event_id": "event_1", "calendar_id": "primary"},
            ]
        }
        response = client.post("/bulk-actions", json=request_data)
        assert response.status_code == 422
        assert "updates" in response.text
        mock_proxy_client.update_event.assert_not_called()


# ============================================================================
# Error Handling Tests
# ============================================================================


class TestProxyErrorHandling:
    """Tests for proxy error handling across endpoints."""

    def test_auth_error_handling(self, client, mock_proxy_client):
        """Authentication errors are properly formatted."""
        mock_proxy_client.list_calendars.side_effect = ProxyAuthError("Invalid API key")
        response = client.get("/calendars")
        assert response.status_code == 502
        data = response.json()
        assert data["success"] is False
        assert "Authentication error" in data["error"]

    def test_forbidden_error_handling(self, client, mock_proxy_client):
        """Forbidden errors are properly formatted."""
        mock_proxy_client.delete_event.side_effect = ProxyForbiddenError(
            "Request rejected by operator"
        )
        response = client.delete("/calendars/primary/events/event_123")
        assert response.status_code == 403
        data = response.json()
        assert data["success"] is False
        assert "blocked" in data["error"].lower()

    def test_generic_proxy_error_handling(self, client, mock_proxy_client):
        """Generic proxy errors are properly formatted."""
        mock_proxy_client.list_events.side_effect = ProxyError("Connection timeout")
        response = client.get("/calendars/primary/events")
        assert response.status_code == 502
        data = response.json()
        assert data["success"] is False
        assert "Proxy error" in data["error"]

    def test_proxy_404_passes_message_through_as_404(self, client, mock_proxy_client):
        mock_proxy_client.get_event.side_effect = ProxyNotFoundError("Not Found")
        response = client.get("/calendars/primary/events/nope")
        assert response.status_code == 404
        data = response.json()
        assert data["success"] is False
        assert "Not Found" in data["error"]

    def test_proxy_400_passes_through_as_400(self, client, mock_proxy_client):
        mock_proxy_client.list_events.side_effect = ProxyRequestError(400, "Bad Request")
        response = client.get("/calendars/primary/events")
        assert response.status_code == 400
        assert response.json()["success"] is False

    def test_other_proxy_4xx_still_maps_to_502(self, client, mock_proxy_client):
        """Only 400 passes through (404 is ProxyNotFoundError); any other proxy
        4xx is still 502."""
        mock_proxy_client.get_event.side_effect = ProxyRequestError(409, "conflict")
        response = client.get("/calendars/primary/events/e1")
        assert response.status_code == 502
        assert "conflict" in response.json()["error"]

    def test_proxy_401_maps_to_502(self, client, mock_proxy_client):
        """A proxy 401 (this service's own key rejected) is an upstream
        failure from the caller's point of view: 502, not 401."""
        mock_proxy_client.get_event.side_effect = ProxyAuthError("Invalid API key")
        response = client.get("/calendars/primary/events/e1")
        assert response.status_code == 502


# ============================================================================
# Validation Tests
# ============================================================================


class TestRequestValidation:
    """Tests for request validation."""

    def test_create_event_validates_attendee_email(self, client):
        """Event creation validates attendee email format."""
        event_data = {
            "summary": "Meeting",
            "attendees": [{"email": "not-an-email"}],
        }
        # Note: Pydantic doesn't validate email format by default
        response = client.post("/calendars/primary/events", json=event_data)
        # Should succeed (email format not strictly validated)
        assert response.status_code == 200

    def test_bulk_actions_requires_operations(self, client):
        """Bulk actions requires non-empty operations list."""
        request_data = {"operations": []}
        response = client.post("/bulk-actions", json=request_data)
        assert response.status_code == 422

    def test_find_free_time_requires_positive_duration(self, client):
        """Find free time requires positive duration."""
        request_data = {
            "calendar_id": "primary",
            "time_min": "2024-01-15T00:00:00Z",
            "time_max": "2024-01-16T00:00:00Z",
            "duration_minutes": -30,
        }
        response = client.post("/find-free-time", json=request_data)
        assert response.status_code == 422


class TestConfirmTimeoutResolution:
    """The confirm timeout is env-overridable; it must never silently drop
    back below the proxy's approval window (issue #4, item 1)."""

    def test_default_outlives_the_proxy_confirmation_window(self):
        """With nothing configured, the mutation budget clears the window."""
        assert resolve_confirm_timeout(None) > PROXY_CONFIRMATION_WINDOW

    def test_override_above_the_window_is_honoured(self):
        assert resolve_confirm_timeout("400") == 400.0

    def test_override_below_the_window_is_rejected(self):
        """A 30s override is the original incident's configuration; accepting
        it silently makes every approval outcome undeliverable again."""
        with pytest.raises(ProxyConfigError) as exc_info:
            resolve_confirm_timeout("30")
        assert "PROXY_CONFIRM_TIMEOUT" in str(exc_info.value)
        assert "300" in str(exc_info.value)

    def test_non_numeric_override_is_rejected(self):
        """A typo must fail loudly rather than fall back to a short default."""
        with pytest.raises(ProxyConfigError):
            resolve_confirm_timeout("5 minutes")

    @pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "NaN", "Infinity"])
    def test_non_finite_override_is_rejected(self, raw):
        """float('nan') parses and compares False against everything, so the
        old ``<= window`` guard let it through; ``inf`` disables the timeout.
        Neither is a budget that outlives anything."""
        with pytest.raises(ProxyConfigError):
            resolve_confirm_timeout(raw)

    @pytest.mark.parametrize("raw", ["301", "329.9"])
    def test_override_inside_the_margin_is_rejected(self, raw):
        """Clearing the window by a second is not clearing it: the proxy's own
        timeout, its response, and the network all sit inside that gap."""
        with pytest.raises(ProxyConfigError) as exc_info:
            resolve_confirm_timeout(raw)
        assert str(int(PROXY_CONFIRMATION_WINDOW + CONFIRM_TIMEOUT_MARGIN)) in str(
            exc_info.value
        )

    def test_override_at_window_plus_margin_is_accepted(self):
        assert resolve_confirm_timeout("330") == 330.0

    def test_guard_follows_an_overridden_confirmation_window(self):
        """The window is api-proxy's ``--confirmation-timeout``, not a law of
        nature; an operator who raises it there must be able to keep this
        guard honest, or it passes while the invariant is violated (F5)."""
        with pytest.raises(ProxyConfigError):
            resolve_confirm_timeout("330", window=600.0)
        assert resolve_confirm_timeout("630", window=600.0) == 630.0


class TestConfirmationWindowResolution:
    """``PROXY_CONFIRMATION_WINDOW`` mirrors api-proxy's confirmation timeout."""

    def test_default_matches_api_proxy_default(self):
        assert resolve_confirmation_window(None) == 300.0

    def test_override_is_honoured(self):
        assert resolve_confirmation_window("600") == 600.0

    @pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "five"])
    def test_unbounded_or_invalid_window_is_rejected(self, raw):
        """api-proxy treats ``<= 0`` as "wait forever" — a window no client
        timeout can outlive, so the guard cannot be honest and must say so."""
        with pytest.raises(ProxyConfigError) as exc_info:
            resolve_confirmation_window(raw)
        assert "PROXY_CONFIRMATION_WINDOW" in str(exc_info.value)


class TestMissingEventStatus:
    """A resource the proxy says is gone must surface as 404, not 502.

    Item 3's verify-by-re-read (issue #4) is specified as "expect 404 or
    status: cancelled"; collapsing 404 into the generic upstream-error bucket
    leaves a caller unable to tell "deleted" from "proxy is broken"."""

    async def test_proxy_404_raises_not_found(self):
        client = CalendarProxyClient(proxy_url="http://proxy", api_key="k")
        response = AsyncMock(spec=httpx.Response)
        response.status_code = 404
        response.json.return_value = {"detail": "Not Found"}
        with pytest.raises(ProxyNotFoundError):
            client._handle_response(response)

    def test_get_deleted_event_returns_404(self, client, mock_proxy_client):
        mock_proxy_client.get_event.side_effect = ProxyNotFoundError("Event not found")
        response = client.get("/calendars/primary/events/gone_123")
        assert response.status_code == 404
        data = response.json()
        assert data["success"] is False
        assert data["event"] is None


class TestBulkActionsOutcomeHonesty:
    """/bulk-actions must not answer 200/success:true for work that failed or
    whose outcome is unknown (issue #4, item 4)."""

    def _delete_ops(self, *event_ids: str) -> dict:
        return {
            "operations": [
                {"operation": "delete", "event_id": e, "calendar_id": "primary"}
                for e in event_ids
            ]
        }

    def test_all_operations_failing_is_not_reported_as_success(
        self, client, mock_proxy_client
    ):
        mock_proxy_client.delete_event.side_effect = ProxyError("proxy exploded")
        response = client.post("/bulk-actions", json=self._delete_ops("e1", "e2"))
        assert response.status_code == 502
        data = response.json()
        assert data["success"] is False
        assert data["error_count"] == 2
        assert data["error"]  # F7: the envelope must say so, not just the items

    def test_partial_failure_is_not_reported_as_success(self, client, mock_proxy_client):
        mock_proxy_client.delete_event.side_effect = [
            {"success": True},
            ProxyError("proxy exploded"),
        ]
        mock_proxy_client.get_event.side_effect = [
            ProxyNotFoundError("Not Found"),
            {"id": "e2", "status": "confirmed"},
        ]
        response = client.post("/bulk-actions", json=self._delete_ops("e1", "e2"))
        data = response.json()
        assert data["success"] is False
        assert response.status_code == 502
        assert "1 of 2" in data["error"]
        assert "1 failed" in data["error"]

    def test_timed_out_operation_is_unknown_not_failed(self, client, mock_proxy_client):
        """A mutation that timed out may still be applied after later approval;
        recording it as a plain failure is the incident's error inverted."""
        mock_proxy_client.delete_event.side_effect = ProxyTimeoutError("no response")
        response = client.post("/bulk-actions", json=self._delete_ops("e1"))
        assert response.status_code == 504
        data = response.json()
        assert data["success"] is False
        assert data["unknown_count"] == 1
        assert data["error_count"] == 0
        assert data["results"][0]["outcome"] == "unknown"
        assert "unknown" in data["error"]  # F7

    def test_unknown_outcome_outranks_a_definite_failure(self, client, mock_proxy_client):
        """504 (verify before acting) must win over 502, because the unknown
        operation is the one that can still change the calendar."""
        mock_proxy_client.delete_event.side_effect = [
            ProxyError("proxy exploded"),
            ProxyTimeoutError("no response"),
        ]
        response = client.post("/bulk-actions", json=self._delete_ops("e1", "e2"))
        assert response.status_code == 504

    def test_rejected_operation_propagates_403(self, client, mock_proxy_client):
        mock_proxy_client.delete_event.side_effect = ProxyForbiddenError("rejected")
        response = client.post("/bulk-actions", json=self._delete_ops("e1"))
        assert response.status_code == 403
        assert response.json()["error"]  # F7

    def test_nonexistent_event_propagates_404(self, client, mock_proxy_client):
        """The README's 404 bulk contract (F10): a proxy 404 is "absent",
        not a 502 upstream failure."""
        mock_proxy_client.delete_event.side_effect = ProxyNotFoundError("Not Found")
        response = client.post("/bulk-actions", json=self._delete_ops("e1"))
        assert response.status_code == 404
        data = response.json()
        assert data["results"][0]["outcome"] == "failed"
        assert data["error"]

    def test_all_succeeding_stays_200_and_true(self, client, mock_proxy_client):
        mock_proxy_client.get_event.side_effect = ProxyNotFoundError("Not Found")
        response = client.post("/bulk-actions", json=self._delete_ops("e1", "e2"))
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["unknown_count"] == 0
        assert [r["outcome"] for r in data["results"]] == ["succeeded", "succeeded"]

    @pytest.mark.parametrize("operation", ["update", "patch"])
    def test_missing_update_payload_rejects_the_whole_batch(
        self, client, mock_proxy_client, operation
    ):
        """F3: a malformed operation anywhere in the batch is a 422 before any
        operation runs — never a 400 that claims "nothing happened" after
        earlier operations already did."""
        request_data = {
            "operations": [
                {"operation": "delete", "event_id": "e1", "calendar_id": "primary"},
                {"operation": operation, "event_id": "e2", "calendar_id": "primary"},
            ]
        }
        response = client.post("/bulk-actions", json=request_data)
        assert response.status_code == 422
        mock_proxy_client.delete_event.assert_not_called()

    @pytest.mark.parametrize("order", ["not_found_first", "rejected_first"])
    def test_status_code_is_ranked_by_severity_not_position(
        self, client, mock_proxy_client, order
    ):
        """F3: the envelope's status must not depend on operation order."""
        errors = [ProxyNotFoundError("Not Found"), ProxyForbiddenError("rejected")]
        if order == "rejected_first":
            errors.reverse()
        mock_proxy_client.delete_event.side_effect = errors
        response = client.post("/bulk-actions", json=self._delete_ops("e1", "e2"))
        assert response.status_code == 403

    def test_bulk_delete_is_verified_by_re_read(self, client, mock_proxy_client):
        """F4: bulk deletes get the same after-write check as single deletes."""
        # Fixture default: get_event returns a confirmed event.
        response = client.post("/bulk-actions", json=self._delete_ops("e1"))
        assert response.status_code == 502
        data = response.json()
        assert data["results"][0]["outcome"] == "failed"
        assert "still present" in data["results"][0]["error"]

    def test_stops_after_the_first_unknown_outcome(self, client, mock_proxy_client):
        """F8: once one gated mutation has timed out, the operator is not
        answering; each further operation would hold the connection another
        full CONFIRM_TIMEOUT and enqueue another approval. Stop, and say so."""
        mock_proxy_client.delete_event.side_effect = [
            {"success": True},
            ProxyTimeoutError("no response"),
            {"success": True},
        ]
        mock_proxy_client.get_event.side_effect = [
            ProxyNotFoundError("Not Found"),
            {"id": "e2", "status": "confirmed"},
        ]
        response = client.post("/bulk-actions", json=self._delete_ops("e1", "e2", "e3"))
        assert response.status_code == 504
        data = response.json()
        assert [r["outcome"] for r in data["results"]] == [
            "succeeded", "unknown", "not_attempted"
        ]
        assert data["results"][2]["success"] is False
        assert "not attempted" in data["results"][2]["error"].lower()
        assert data["success_count"] == 1
        assert data["unknown_count"] == 1
        assert data["error_count"] == 0
        assert data["not_attempted_count"] == 1
        assert mock_proxy_client.delete_event.call_count == 2
        assert "1 not attempted" in data["error"]


class TestBulkStatusCode:
    """Pure ranking of per-operation status codes into one envelope status."""

    @pytest.mark.parametrize(
        ("codes", "expected"),
        [
            ([200, 200], 200),
            ([404, 403], 403),
            ([403, 404], 403),
            ([502, 403], 403),
            ([404, 502], 502),
            ([403, 504], 504),
            ([200, 404], 404),
            ([500, 404], 500),
        ],
    )
    def test_precedence(self, codes, expected):
        from calendar_agent.calendar_server import bulk_status_code

        assert bulk_status_code(codes) == expected
