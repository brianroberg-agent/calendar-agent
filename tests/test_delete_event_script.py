"""Tests for scripts/calendar-delete-event.sh.

The script is the last hop before a human believes a calendar event is gone,
so it is exercised end to end: a real bash process, a real curl, against a
throwaway HTTP server standing in for calendar-agent. Nothing here touches a
live calendar or proxy.

Issue #4: the wrapper must distinguish three outcomes — the deletion happened,
it definitely did not happen, or nobody knows yet — and must decide by
re-reading the event rather than by trusting the delete's own answer.
"""

import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "calendar-delete-event.sh"

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_UNKNOWN = 2
EXIT_NOT_FOUND = 3


class _FakeAgentHandler(BaseHTTPRequestHandler):
    """Serves whatever ``server.script`` says, and records what was asked."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: A003 - silence stderr noise
        pass

    def handle_error(self, *args):
        """A client that timed out and hung up is the scenario, not an error."""

    def _respond(self, spec: dict) -> None:
        if spec.get("delay"):
            time.sleep(spec["delay"])
        payload = json.dumps(spec.get("body", {})).encode()
        try:
            self.send_response(spec["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except BrokenPipeError:
            # curl hit --max-time and closed the socket: exactly what the
            # timeout scenarios are simulating.
            self.close_connection = True

    def do_DELETE(self):  # noqa: N802 - BaseHTTPRequestHandler naming
        self.server.calls.append(("DELETE", self.path))
        self._respond(self.server.script["delete"])

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler naming
        self.server.calls.append(("GET", self.path))
        self._respond(self.server.script["get"])


@pytest.fixture
def fake_agent():
    """Start a stub calendar-agent on loopback; yield a configurable handle."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAgentHandler)
    server.daemon_threads = True
    server.calls = []
    server.script = {
        "delete": {"status": 200, "body": {"success": True, "message": "ok"}},
        "get": {"status": 404, "body": {"success": False, "event": None}},
    }
    server.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def run_script(fake_agent, *, event_id="event_1", max_time="5"):
    return subprocess.run(
        [str(SCRIPT), event_id, "primary"],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "CALENDAR_AGENT_URL": fake_agent.url,
            "CALENDAR_DELETE_MAX_TIME": max_time,
        },
        timeout=60,
    )


def confirmed_event(event_id="event_1"):
    return {
        "status": 200,
        "body": {"success": True, "event": {"id": event_id, "status": "confirmed"}},
    }


class TestVerifiedSuccess:
    """Exit 0 requires evidence the event is gone, never the delete's claim."""

    def test_deleted_event_gone_on_re_read_is_success(self, fake_agent):
        result = run_script(fake_agent)
        assert result.returncode == EXIT_SUCCESS
        assert "RESULT: SUCCESS" in result.stdout

    def test_cancelled_status_on_re_read_is_success(self, fake_agent):
        fake_agent.script["get"] = {
            "status": 200,
            "body": {"success": True, "event": {"id": "event_1", "status": "cancelled"}},
        }
        result = run_script(fake_agent)
        assert result.returncode == EXIT_SUCCESS
        assert "RESULT: SUCCESS" in result.stdout

    def test_re_read_happens_after_the_delete(self, fake_agent):
        run_script(fake_agent)
        methods = [method for method, _ in fake_agent.calls]
        assert methods == ["DELETE", "GET"]


class TestDefiniteFailure:
    """Exit 1 means the event demonstrably survived and nothing is pending."""

    def test_success_claim_contradicted_by_re_read_is_failure(self, fake_agent):
        """The 2026-08-07 incident: HTTP 200 for an event still confirmed."""
        fake_agent.script["get"] = confirmed_event()
        result = run_script(fake_agent)
        assert result.returncode == EXIT_FAILURE
        assert "RESULT: FAILURE" in result.stdout

    def test_success_false_in_body_is_not_success(self, fake_agent):
        """A 200 whose body says success:false must not be read as deleted."""
        fake_agent.script["delete"] = {
            "status": 200,
            "body": {"success": False, "message": "Failed to delete event"},
        }
        fake_agent.script["get"] = confirmed_event()
        result = run_script(fake_agent)
        assert result.returncode == EXIT_FAILURE

    def test_operator_rejection_is_failure(self, fake_agent):
        fake_agent.script["delete"] = {
            "status": 403,
            "body": {"success": False, "message": "rejected by operator"},
        }
        fake_agent.script["get"] = confirmed_event()
        result = run_script(fake_agent)
        assert result.returncode == EXIT_FAILURE
        assert "RESULT: FAILURE" in result.stdout


class TestUnknownOutcome:
    """Exit 2 means the deletion may still land; do not act on it either way."""

    def test_server_reported_unknown_outcome_is_unknown(self, fake_agent):
        """504 from calendar-agent: it gave up waiting for the operator."""
        fake_agent.script["delete"] = {
            "status": 504,
            "body": {"success": False, "message": "Deletion outcome unknown"},
        }
        fake_agent.script["get"] = confirmed_event()
        result = run_script(fake_agent)
        assert result.returncode == EXIT_UNKNOWN
        assert "RESULT: UNKNOWN" in result.stdout

    def test_client_timeout_with_event_still_present_is_unknown(self, fake_agent):
        """curl's own deadline expiring is not evidence the delete failed."""
        fake_agent.script["delete"] = {"status": 200, "body": {"success": True}, "delay": 3}
        fake_agent.script["get"] = confirmed_event()
        result = run_script(fake_agent, max_time="1")
        assert result.returncode == EXIT_UNKNOWN
        assert "RESULT: UNKNOWN" in result.stdout

    def test_client_timeout_still_verifies(self, fake_agent):
        """The verify GET must run even when the delete never answered."""
        fake_agent.script["delete"] = {"status": 200, "body": {"success": True}, "delay": 3}
        run_script(fake_agent, max_time="1")
        assert ("GET", "/calendars/primary/events/event_1") in fake_agent.calls

    def test_client_timeout_but_event_gone_is_success(self, fake_agent):
        """Approved late but before the re-read: the deletion did happen."""
        fake_agent.script["delete"] = {"status": 200, "body": {"success": True}, "delay": 3}
        result = run_script(fake_agent, max_time="1")
        assert result.returncode == EXIT_SUCCESS

    def test_unreadable_verification_is_unknown(self, fake_agent):
        """If the re-read itself fails, nothing has been established."""
        fake_agent.script["get"] = {"status": 502, "body": {"success": False}}
        result = run_script(fake_agent)
        assert result.returncode == EXIT_UNKNOWN
        assert "RESULT: UNKNOWN" in result.stdout


class TestNotFound:
    """Exit 3 means the id never existed; a 404 delete is not evidence of work.

    A DELETE that itself 404s is indistinguishable, by exit-0 SUCCESS alone,
    from a completed deletion of a real event: both re-reads come back 404.
    A mistyped or already-gone event id must not be reported as success.
    """

    def test_delete_404_with_get_404_is_not_found(self, fake_agent):
        fake_agent.script["delete"] = {
            "status": 404,
            "body": {"success": False, "message": "Event not found"},
        }
        result = run_script(fake_agent)
        assert result.returncode == EXIT_NOT_FOUND
        assert "RESULT: NOT FOUND" in result.stdout
        assert "RESULT: SUCCESS" not in result.stdout

    def test_delete_410_with_get_404_is_not_found(self, fake_agent):
        fake_agent.script["delete"] = {
            "status": 410,
            "body": {"success": False, "message": "Event gone"},
        }
        result = run_script(fake_agent)
        assert result.returncode == EXIT_NOT_FOUND
        assert "RESULT: NOT FOUND" in result.stdout

    def test_delete_404_but_get_still_present_is_failure(self, fake_agent):
        """A 404 delete followed by a present re-read is the FAILURE path,
        not NOT FOUND — the claim only matters when verify says gone."""
        fake_agent.script["delete"] = {
            "status": 404,
            "body": {"success": False, "message": "Event not found"},
        }
        fake_agent.script["get"] = confirmed_event()
        result = run_script(fake_agent)
        assert result.returncode == EXIT_FAILURE
        assert "RESULT: FAILURE" in result.stdout


class TestNoRetryOnItsOwn:
    """Enqueuing a second approval is a side effect; the script must not."""

    def test_exactly_one_delete_is_issued(self, fake_agent):
        fake_agent.script["delete"] = {"status": 504, "body": {"success": False}}
        fake_agent.script["get"] = confirmed_event()
        run_script(fake_agent)
        assert [m for m, _ in fake_agent.calls].count("DELETE") == 1


class TestUsage:
    """Guard rails that keep the whitelisted script narrowly scoped."""

    def test_missing_event_id_exits_nonzero(self, fake_agent):
        result = subprocess.run(
            [str(SCRIPT)],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "CALENDAR_AGENT_URL": fake_agent.url},
            timeout=30,
        )
        assert result.returncode != EXIT_SUCCESS
        assert "usage" in result.stderr.lower()

    def test_missing_agent_url_exits_nonzero(self):
        result = subprocess.run(
            [str(SCRIPT), "event_1"],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin"},
            timeout=30,
        )
        assert result.returncode != EXIT_SUCCESS
        assert "CALENDAR_AGENT_URL" in result.stderr
