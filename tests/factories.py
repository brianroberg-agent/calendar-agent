"""Sample calendar data and event factories shared by the test modules.

Import from here (``from tests.factories import ...``) rather than from
``conftest.py``: conftest is pytest's fixture hook module and importing it
directly is the one pattern the repo otherwise avoids.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

# ============================================================================
# Sample Calendar Data
# ============================================================================


# The authenticated user the sample data is built around (the proxy's
# credentials belong to this account) and the colleague whose calendar the
# colleague_copy fixture sits on.
AUTH_USER_EMAIL = "john.doe@example.com"
COLLEAGUE_EMAIL = "carol@example.com"

SAMPLE_CALENDARS = {
    "primary": {
        "id": "primary",
        "summary": AUTH_USER_EMAIL,
        "description": "Primary calendar",
        "timeZone": "America/New_York",
        "primary": True,
    },
    "work": {
        "id": "work_calendar_123",
        "summary": "Work Calendar",
        "description": "Work meetings and deadlines",
        "timeZone": "America/New_York",
        "primary": False,
    },
    "personal": {
        "id": "personal_calendar_456",
        "summary": "Personal",
        "description": None,
        "timeZone": "America/Los_Angeles",
        "primary": False,
    },
}


def get_sample_event(
    event_id: str = "event_123",
    summary: str = "Team Meeting",
    description: str = "Weekly team sync",
    location: str | None = "Conference Room A",
    start_hours_from_now: int = 1,
    duration_hours: int = 1,
    attendees: list[dict[str, Any]] | None = None,
    is_all_day: bool = False,
    organizer: dict[str, Any] | None = None,
    creator: dict[str, Any] | None = None,
    status: str | None = "confirmed",
) -> dict[str, Any]:
    """Generate a sample event with customizable properties.

    ``attendees`` / ``organizer`` / ``creator`` are omitted from the dict when
    None, and so is ``status``, matching what Google returns for events that
    carry none of them (e.g. cancelled recurring-instance stubs).
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    start = now + timedelta(hours=start_hours_from_now)
    end = start + timedelta(hours=duration_hours)

    if is_all_day:
        start_obj = {"date": start.strftime("%Y-%m-%d")}
        end_obj = {"date": end.strftime("%Y-%m-%d")}
    else:
        start_obj = {
            "dateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timeZone": "UTC",
        }
        end_obj = {
            "dateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timeZone": "UTC",
        }

    event = {
        "id": event_id,
        "summary": summary,
        "description": description,
        "location": location,
        "start": start_obj,
        "end": end_obj,
        "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
        "created": (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    if status is not None:
        event["status"] = status

    if attendees is not None:
        event["attendees"] = attendees

    if organizer is not None:
        event["organizer"] = organizer

    if creator is not None:
        event["creator"] = creator

    return event


SAMPLE_EVENTS = {
    "basic_meeting": get_sample_event(
        event_id="meeting_001",
        summary="Team Standup",
        description="Daily standup meeting",
        location="Zoom",
        start_hours_from_now=1,
        duration_hours=0.5,
        attendees=[
            {
                "email": "alice@example.com",
                "displayName": "Alice Smith",
                "responseStatus": "accepted",
            },
            {"email": "bob@example.com", "displayName": "Bob Jones", "responseStatus": "tentative"},
        ],
    ),
    "all_day_event": get_sample_event(
        event_id="allday_001",
        summary="Company Holiday",
        description="Office closed",
        location=None,
        start_hours_from_now=24,
        duration_hours=24,
        is_all_day=True,
    ),
    "no_description": get_sample_event(
        event_id="nodesc_001",
        summary="Quick Chat",
        description="",
        location=None,
        start_hours_from_now=2,
        duration_hours=0.25,
    ),
    "long_description": get_sample_event(
        event_id="longdesc_001",
        summary="Project Review",
        description="A" * 5000,  # Very long description
        location="Board Room",
        start_hours_from_now=3,
        duration_hours=2,
    ),
    "with_recurrence": {
        **get_sample_event(
            event_id="recurring_001",
            summary="Weekly 1:1",
            description="Weekly check-in with manager",
            location=None,
            start_hours_from_now=24,
            duration_hours=0.5,
        ),
        "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO"],
    },
    "with_conference": {
        **get_sample_event(
            event_id="conf_001",
            summary="Remote Meeting",
            description="Discussion of Q4 goals",
            location=None,
            start_hours_from_now=4,
            duration_hours=1,
        ),
        "conferenceData": {
            "entryPoints": [
                {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"},
            ],
        },
    },
    "many_attendees": get_sample_event(
        event_id="many_att_001",
        summary="All Hands",
        description="Company-wide meeting",
        location="Main Auditorium",
        start_hours_from_now=48,
        duration_hours=2,
        attendees=[
            {"email": f"employee{i}@example.com", "responseStatus": "needsAction"}
            for i in range(50)
        ],
    ),
    # The authenticated user's own copy of an invitation from Dave: their
    # entry carries self:true. See colleague_copy() for Carol's copy of it.
    "invitation": get_sample_event(
        event_id="invite_001",
        summary="Budget Review",
        description="Quarterly budget walkthrough",
        location=None,
        start_hours_from_now=5,
        duration_hours=1,
        organizer={"email": "dave@example.com", "displayName": "Dave Organizer", "self": False},
        creator={"email": "dave@example.com"},
        attendees=[
            {"email": "dave@example.com", "organizer": True, "responseStatus": "accepted"},
            {"email": AUTH_USER_EMAIL, "self": True, "responseStatus": "needsAction"},
            {"email": "alice@example.com", "responseStatus": "accepted"},
        ],
    ),
    "past_event": get_sample_event(
        event_id="past_001",
        summary="Yesterday's Meeting",
        description="This meeting already happened",
        start_hours_from_now=-25,
        duration_hours=1,
    ),
}


def colleague_copy(calendar_entry_status: Any = "accepted") -> dict[str, Any]:
    """Carol's calendar's copy of ``SAMPLE_EVENTS["invitation"]``.

    Identical to the authenticated user's copy except for the attendee list,
    which is how Google renders the same event on another calendar: Carol's
    entry is the ``self`` entry (with ``calendar_entry_status``) and the
    authenticated user is a plain attendee with no ``self`` flag.
    """
    return {
        **SAMPLE_EVENTS["invitation"],
        "attendees": [
            {"email": "dave@example.com", "organizer": True, "responseStatus": "accepted"},
            {"email": COLLEAGUE_EMAIL, "self": True, "responseStatus": calendar_entry_status},
            {"email": AUTH_USER_EMAIL, "responseStatus": "declined"},
        ],
    }


SAMPLE_EVENTS["colleague_copy"] = colleague_copy()
