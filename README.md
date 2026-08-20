# Calendar Agent

A privacy-focused FastAPI server that wraps the Google Calendar API for use with AI agents.

## Overview

Calendar Agent acts as an intermediary between AI orchestrators (like Claude Code) and the Google Calendar API via a proxy server. Calendar event details are processed locally; only metadata and LLM-generated summaries are returned to calling agents.

**Key Privacy Features:**
- Event descriptions and details never leave the local server
- Only metadata (IDs, dates, titles, attendee counts) is exposed to cloud services
- LLM processing happens locally via MLX or can use hosted APIs

**Architecture:**
```
Orchestrator Agent <-> Calendar Agent (local) <-> API Proxy <-> Google Calendar API
                            |
                      Local LLM (MLX)
```

## Setup

### Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) package manager
- Access to an api-proxy server instance
- (Optional) Local LLM server (MLX-based) for AI features

### Installation

```bash
# Clone the repository
cd calendar-agent

# Install dependencies with uv
uv sync

# Install dev dependencies
uv sync --dev
```

### Configuration

Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
```

Required environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `PROXY_URL` | URL of the api-proxy server | `http://localhost:8000` |
| `PROXY_API_KEY` | API key for proxy authentication | (required) |
| `LLM_URL` | URL of the local LLM server | `http://localhost:8080/v1/chat/completions` |
| `LLM_MODEL` | Model name for LLM requests | `qwen/qwen3-14b` |
| `CALENDAR_AGENT_PORT` | Port for the calendar agent server | `8082` |
| `PROXY_CONFIRM_TIMEOUT` | Client timeout (seconds) for mutations, which block in the proxy while a human operator approves them; must exceed the proxy's 300s confirmation window | `330` |

### Running the Server

```bash
# Using uv
uv run python -m calendar_agent.calendar_server

# Or with uvicorn directly
uv run uvicorn calendar_agent.calendar_server:app --host 0.0.0.0 --port 8082
```

The server will be available at `http://localhost:8082`. API documentation is at `http://localhost:8082/docs`.

## API Endpoints

### Error Responses

Every endpoint returns a body with a `success` field, and on failure an
`error` message; the HTTP status code always agrees with the body
([issue #4](https://github.com/brianroberg/calendar-agent/issues/4)):

| Status | Meaning |
|--------|---------|
| `200` | The operation succeeded (`success: true`) |
| `400` | The request was rejected before anything was sent upstream (e.g. a bulk `update` with no `updates` payload) — nothing happened |
| `403` | The proxy blocked the operation by policy, or the human operator rejected it (mutations block in the proxy until an operator approves them) |
| `404` | The calendar or event does not exist. When verifying a deletion this is the *expected* answer: the event is gone |
| `422` | Request validation failed (FastAPI's standard `detail` body, no envelope) |
| `502` | The proxy or LLM backend failed |
| `504` | No response before this server's timeout — **the outcome is unknown**: a confirmation-gated mutation may still complete if approved later. Verify by re-reading the resource; never issue a compensating mutation on the strength of a `504` |
| `500` | Unexpected internal error |

### GET /health

Health check endpoint. Returns server status and version.

```bash
curl http://localhost:8082/health
```

Response:
```json
{
  "status": "ok",
  "version": "1.0.0"
}
```

---

## Calendar Endpoints

### GET /calendars

List all calendars for the authenticated user.

```bash
curl http://localhost:8082/calendars
```

Response:
```json
{
  "success": true,
  "calendars": [
    {
      "id": "primary",
      "summary": "john.doe@example.com",
      "description": "Primary calendar",
      "timeZone": "America/New_York",
      "primary": true
    }
  ],
  "error": null
}
```

### GET /calendars/{calendar_id}

Get metadata for a specific calendar.

```bash
curl http://localhost:8082/calendars/primary
```

Response:
```json
{
  "success": true,
  "calendar": {
    "id": "primary",
    "summary": "john.doe@example.com",
    "timeZone": "America/New_York"
  }
}
```

---

## Event CRUD Endpoints

### GET /calendars/{calendar_id}/events

List events in a calendar. By default, recurring events are expanded to individual instances.

Query parameters:
- `max_results` (int): Maximum number of events to return (default: 100)
- `page_token` (string): Token for pagination
- `time_min` (string): Start of time range (RFC3339)
- `time_max` (string): End of time range (RFC3339)
- `q` (string): Free text search query
- `single_events` (bool): Expand recurring events (default: true)
- `order_by` (string): "startTime" or "updated"

```bash
curl "http://localhost:8082/calendars/primary/events?time_min=2024-01-01T00:00:00Z&max_results=10"
```

Response:
```json
{
  "success": true,
  "events": [
    {
      "id": "event123",
      "calendar_id": "primary",
      "summary": "Team Meeting",
      "start": "2024-01-15T10:00:00Z",
      "end": "2024-01-15T11:00:00Z",
      "location": "Conference Room A",
      "attendee_count": 5,
      "is_all_day": false,
      "status": "confirmed"
    }
  ],
  "next_page_token": null,
  "error": null
}
```

### POST /calendars/{calendar_id}/events

Create a new event in a calendar.

```bash
curl -X POST http://localhost:8082/calendars/primary/events \
  -H "Content-Type: application/json" \
  -d '{
    "summary": "Project Review",
    "description": "Quarterly project review meeting",
    "location": "Board Room",
    "start": {"dateTime": "2024-01-20T14:00:00Z"},
    "end": {"dateTime": "2024-01-20T15:00:00Z"},
    "attendees": [
      {"email": "alice@example.com"},
      {"email": "bob@example.com"}
    ]
  }'
```

Response:
```json
{
  "success": true,
  "event": {
    "id": "newEvent123",
    "summary": "Project Review",
    "status": "confirmed"
  },
  "error": null
}
```

### GET /calendars/{calendar_id}/events/{event_id}

Get a specific event by ID.

```bash
curl http://localhost:8082/calendars/primary/events/event123
```

Response:
```json
{
  "success": true,
  "event": {
    "id": "event123",
    "summary": "Team Meeting",
    "description": "Weekly team sync",
    "start": {"dateTime": "2024-01-15T10:00:00Z", "timeZone": "UTC"},
    "end": {"dateTime": "2024-01-15T11:00:00Z", "timeZone": "UTC"},
    "attendees": [
      {"email": "alice@example.com", "responseStatus": "accepted"}
    ]
  },
  "error": null
}
```

### PUT /calendars/{calendar_id}/events/{event_id}

Update an event (full replacement).

```bash
curl -X PUT http://localhost:8082/calendars/primary/events/event123 \
  -H "Content-Type: application/json" \
  -d '{
    "summary": "Updated Meeting Title",
    "start": {"dateTime": "2024-01-15T14:00:00Z"},
    "end": {"dateTime": "2024-01-15T15:00:00Z"}
  }'
```

Response:
```json
{
  "success": true,
  "event": {
    "id": "event123",
    "summary": "Updated Meeting Title"
  },
  "error": null
}
```

### PATCH /calendars/{calendar_id}/events/{event_id}

Partially update an event.

```bash
curl -X PATCH http://localhost:8082/calendars/primary/events/event123 \
  -H "Content-Type: application/json" \
  -d '{"location": "New Conference Room"}'
```

Response:
```json
{
  "success": true,
  "event": {
    "id": "event123",
    "location": "New Conference Room"
  },
  "error": null
}
```

### DELETE /calendars/{calendar_id}/events/{event_id}

Delete an event. The proxy requires operator confirmation for deletes: the
request blocks while a human approves it, then returns `200` on approval,
`403` if the operator rejects (or never answers), or `504` if this server
times out first — in which case the outcome is unknown and the event should
be re-read before assuming failure.

```bash
curl -X DELETE http://localhost:8082/calendars/primary/events/event123
```

Response:
```json
{
  "success": true,
  "message": "Event deleted successfully",
  "error": null
}
```

If the operator rejects the deletion (HTTP `403`):
```json
{
  "success": false,
  "message": "Deletion blocked or rejected by operator",
  "error": "Operation blocked: Request rejected by operator"
}
```

If no response arrives before the timeout (HTTP `504` — outcome unknown,
re-read the event before assuming failure):
```json
{
  "success": false,
  "message": "Deletion outcome unknown: no response before timeout",
  "error": "Outcome unknown: No response from proxy after 330s; ..."
}
```

#### Deleting from a script: `scripts/calendar-delete-event.sh`

A raw `curl -X DELETE` cannot tell these three cases apart, and a caller that
reads only the status code will believe a deletion that has not happened.
The wrapper script decides by **re-reading the event** rather than by trusting
the delete's own answer, and reports three outcomes through its exit status:

| Exit | Result | Meaning |
|------|--------|---------|
| `0` | `SUCCESS` | The event is gone — re-read returned `404`, or `status: cancelled` |
| `1` | `FAILURE` | The event is still there and nothing is outstanding (rejected, or `success: false`) |
| `2` | `UNKNOWN` | The event is still there but the deletion may yet be applied, or the re-read established nothing |

```bash
CALENDAR_AGENT_URL=http://localhost:8082 \
  scripts/calendar-delete-event.sh event123 primary
```

```
DELETE primary event event123 -> HTTP 200 (body success: true)
VERIFY event123 -> HTTP 404 (event status: <absent>)
RESULT: SUCCESS - event no longer present
```

`CALENDAR_DELETE_MAX_TIME` (default `90`) sets the per-request `curl` deadline.
Keep it below whatever timeout the *calling* harness imposes: if the caller
kills `curl`, the script never reaches its verification step, which is the one
part that establishes anything. The script never retries — a retry enqueues a
second operator approval for the same operation.

**Exit `2` means do not act.** In particular, never create a replacement event
until a deletion has been observed complete.

### POST /calendars/{calendar_id}/events/{event_id}/respond

RSVP to an event by setting **only your own** response status. Forwards to the
proxy's dedicated respond route, which changes only the `self` attendee's status
and sends no invitations or notifications.

Request body:
- `response_status` (string): one of `accepted`, `declined`, `tentative`

```bash
curl -X POST http://localhost:8082/calendars/primary/events/event123/respond \
  -H "Content-Type: application/json" \
  -d '{"response_status": "accepted"}'
```

Response:
```json
{
  "success": true,
  "event": {
    "id": "event123",
    "attendees": [
      {"email": "you@example.com", "responseStatus": "accepted", "self": true}
    ]
  },
  "error": null
}
```

---

## LLM-Powered Endpoints

These endpoints use a local LLM to process calendar data and generate insights.

### POST /summarize

Summarize a calendar event using AI.

```bash
curl -X POST http://localhost:8082/summarize \
  -H "Content-Type: application/json" \
  -d '{
    "calendar_id": "primary",
    "event_id": "event123",
    "format": "brief"
  }'
```

Response:
```json
{
  "success": true,
  "data": {
    "event_id": "event123",
    "summary": "This is a weekly team sync meeting scheduled for Monday at 10 AM with 5 attendees. The meeting is held in Conference Room A and typically covers project updates and blockers."
  },
  "error": null
}
```

### POST /ask-about

Ask a question about a specific calendar event.

```bash
curl -X POST http://localhost:8082/ask-about \
  -H "Content-Type: application/json" \
  -d '{
    "calendar_id": "primary",
    "event_id": "event123",
    "question": "Who are the attendees and what are their response statuses?"
  }'
```

Response:
```json
{
  "success": true,
  "data": {
    "event_id": "event123",
    "question": "Who are the attendees and what are their response statuses?",
    "answer": "There are 5 attendees: Alice Smith (accepted), Bob Jones (tentative), Carol White (accepted), David Brown (needs action), and Eve Davis (declined)."
  },
  "error": null
}
```

### POST /batch-summarize

Summarize multiple events, optionally with triage classification.

```bash
curl -X POST http://localhost:8082/batch-summarize \
  -H "Content-Type: application/json" \
  -d '{
    "calendar_id": "primary",
    "event_ids": ["event1", "event2", "event3"],
    "triage": true
  }'
```

Response:
```json
{
  "success": true,
  "data": {
    "results": [
      {
        "event_id": "event1",
        "summary": "Quarterly planning meeting with leadership team",
        "action_type": "meeting",
        "deadline": null
      },
      {
        "event_id": "event2",
        "summary": "Project deadline reminder",
        "action_type": "deadline",
        "deadline": "2024-01-20"
      }
    ],
    "total": 2
  },
  "error": null
}
```

### POST /find-free-time

Find available time slots and get AI suggestions for scheduling.

```bash
curl -X POST http://localhost:8082/find-free-time \
  -H "Content-Type: application/json" \
  -d '{
    "calendar_id": "primary",
    "time_min": "2024-01-15T09:00:00Z",
    "time_max": "2024-01-15T17:00:00Z",
    "duration_minutes": 30,
    "working_hours_only": true,
    "prefer_morning": true
  }'
```

Response:
```json
{
  "success": true,
  "data": {
    "available_slots": [
      {
        "start": "2024-01-15T09:00:00Z",
        "end": "2024-01-15T10:00:00Z",
        "duration_minutes": 60
      },
      {
        "start": "2024-01-15T14:00:00Z",
        "end": "2024-01-15T15:30:00Z",
        "duration_minutes": 90
      }
    ],
    "suggestions": "Based on your preference for morning meetings, I recommend the 9:00 AM slot. It gives you a full hour before your first scheduled meeting and allows time for preparation.",
    "duration_requested": 30
  },
  "error": null
}
```

### POST /analyze-schedule

Analyze schedule patterns and get AI-powered insights.

```bash
curl -X POST http://localhost:8082/analyze-schedule \
  -H "Content-Type: application/json" \
  -d '{
    "calendar_id": "primary",
    "time_min": "2024-01-15T00:00:00Z",
    "time_max": "2024-01-22T00:00:00Z",
    "analysis_type": "overview"
  }'
```

Response:
```json
{
  "success": true,
  "data": {
    "time_range": "2024-01-15T00:00:00Z to 2024-01-22T00:00:00Z",
    "metrics": {
      "total_events": 15,
      "total_hours": 22.5
    },
    "analysis_type": "overview",
    "insights": "Your week has 15 scheduled events totaling 22.5 hours. Key observations:\n\n1. Meeting load is moderate at ~4.5 hours/day\n2. Tuesday and Thursday are meeting-heavy days\n3. You have good focus time blocks on Monday and Friday mornings\n\nRecommendations:\n- Consider consolidating Tuesday meetings to create longer focus blocks\n- The back-to-back meetings on Thursday afternoon may cause fatigue"
  },
  "error": null
}
```

### POST /prepare-briefing

Generate an AI-powered schedule briefing.

```bash
curl -X POST http://localhost:8082/prepare-briefing \
  -H "Content-Type: application/json" \
  -d '{
    "calendar_id": "primary",
    "briefing_type": "daily"
  }'
```

Response:
```json
{
  "success": true,
  "data": {
    "briefing_type": "daily",
    "period": "daily schedule",
    "event_count": 5,
    "briefing": "Today's Schedule Overview:\n\nYou have 5 events scheduled today:\n\n1. 9:00 AM - Team Standup (15 min)\n   Quick daily sync with your team\n\n2. 10:00 AM - Project Review (1 hour)\n   Prepare: Review Q4 metrics document\n\n3. 12:00 PM - Lunch with Client (1.5 hours)\n   Location: Restaurant downtown\n\n4. 3:00 PM - 1:1 with Manager (30 min)\n   Prepare: Weekly status update\n\n5. 4:30 PM - Tech Talk (1 hour)\n   Optional attendance\n\nKey Preparation:\n- Review Q4 metrics before the Project Review\n- Allow 30 min travel time to lunch location"
  },
  "error": null
}
```

---

## Operations Endpoints

### POST /search

Search events in a calendar with structured filters.

```bash
curl -X POST http://localhost:8082/search \
  -H "Content-Type: application/json" \
  -d '{
    "calendar_id": "primary",
    "filters": {
      "query": "project review",
      "time_min": "2024-01-01T00:00:00Z",
      "time_max": "2024-03-31T23:59:59Z",
      "max_results": 20,
      "order_by": "startTime"
    }
  }'
```

Response:
```json
{
  "success": true,
  "events": [
    {
      "id": "event456",
      "calendar_id": "primary",
      "summary": "Q1 Project Review",
      "start": "2024-01-20T14:00:00Z",
      "end": "2024-01-20T15:00:00Z",
      "attendee_count": 8,
      "is_all_day": false
    }
  ],
  "next_page_token": null,
  "error": null
}
```

### POST /bulk-actions

Execute multiple operations on events in a single request.

Supported operations:
- `update`: Full event replacement
- `patch`: Partial event update
- `delete`: Delete event

Each result carries an `outcome` of `succeeded`, `failed` or `unknown`.
`unknown` means the mutation timed out and **may still be applied** when the
operator approves it — re-read the event before doing anything about it.

The envelope's `success` is true only when every operation succeeded, and the
status code agrees with it: `200` when all succeeded, `504` if any outcome is
unknown (this outranks a definite failure, because the unknown operation is
the one that can still change the calendar), otherwise `403`/`404`/`502`/`400`
from the first failure.

```bash
curl -X POST http://localhost:8082/bulk-actions \
  -H "Content-Type: application/json" \
  -d '{
    "operations": [
      {
        "operation": "patch",
        "event_id": "event1",
        "calendar_id": "primary",
        "updates": {"location": "Room A"}
      },
      {
        "operation": "delete",
        "event_id": "event2",
        "calendar_id": "primary"
      }
    ]
  }'
```

Response:
```json
{
  "success": true,
  "results": [
    {
      "event_id": "event1",
      "operation": "patch",
      "success": true,
      "outcome": "succeeded",
      "error": null
    },
    {
      "event_id": "event2",
      "operation": "delete",
      "success": true,
      "outcome": "succeeded",
      "error": null
    }
  ],
  "success_count": 2,
  "error_count": 0,
  "unknown_count": 0,
  "error": null
}
```

---

## Development

### Running Tests

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=calendar_agent

# Run specific test file
uv run pytest tests/test_calendar_server.py

# Run documentation tests
uv run pytest tests/test_readme_documentation.py
```

### Linting

```bash
# Check linting
uv run ruff check .

# Fix linting issues
uv run ruff check --fix .

# Format code
uv run ruff format .
```

### Refreshing the API Proxy Spec Snapshot

`docs/api-proxy-openapi-doc.json` is a snapshot of the
[api-proxy](https://github.com/brianroberg/api-proxy) OpenAPI spec, stamped
with the commit and time it was generated from (`x-generated-from`). It records
the proxy contract this agent was built against; `tests/test_proxy_contract.py`
fails if `proxy_client.py` calls a route the snapshot doesn't contain, so
refresh it whenever the proxy contract changes:

```bash
# From a local api-proxy checkout
uv run python scripts/refresh_openapi.py --checkout ../api-proxy

# From a running proxy instance
uv run python scripts/refresh_openapi.py --url http://localhost:8000
```

### Project Structure

```
calendar-agent/
├── calendar_agent/
│   ├── __init__.py
│   ├── calendar_server.py    # FastAPI application with all endpoints
│   ├── proxy_client.py       # HTTP client for api-proxy
│   ├── calendar_utils.py     # Utility functions
│   ├── llm_service.py        # LLM provider abstraction and implementation
│   └── exceptions.py         # Custom exceptions
├── tests/
│   ├── conftest.py           # Test fixtures
│   ├── test_calendar_server.py
│   ├── test_delete_event_script.py  # End-to-end tests for the delete wrapper
│   ├── test_proxy_contract.py    # Client routes vs. the api-proxy spec snapshot
│   └── test_readme_documentation.py
├── docs/
│   └── api-proxy-openapi-doc.json    # Stamped snapshot of the api-proxy spec
├── scripts/
│   ├── calendar-delete-event.sh  # Verified delete wrapper (success/failure/unknown)
│   └── refresh_openapi.py    # Regenerates the api-proxy spec snapshot
├── pyproject.toml
├── README.md
└── .env.example
```

## LLM Provider Extensibility

The calendar agent is designed to support multiple LLM providers. Currently, it uses a local MLX-based server, but the architecture allows easy swapping to hosted APIs.

To add a new LLM provider:

1. Create a new class implementing `LLMProvider` in `llm_service.py`:

```python
class AnthropicProvider(LLMProvider):
    async def generate(self, system_prompt, user_content, max_tokens=1024, temperature=0.3):
        # Implementation using Anthropic API
        ...
```

2. Update the `get_llm_service()` function to use your provider based on configuration.

## License

MIT
