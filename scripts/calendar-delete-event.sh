#!/usr/bin/env bash
# Delete a single calendar event via calendar-agent, and prove what happened.
#
# Usage: calendar-delete-event.sh <event_id> [calendar_id]
#
# Environment:
#   CALENDAR_AGENT_URL        base URL of calendar-agent (required)
#   CALENDAR_DELETE_MAX_TIME  per-request curl deadline in seconds (default 90)
#
# Deletes are gated on a human operator, who may take minutes to answer, so the
# response to the DELETE is not evidence of anything: it can be a claim of
# success for work that has not happened, or a timeout for work that is still
# going to happen. This script therefore decides by re-reading the event, and
# reports three distinct outcomes (issue #4):
#
#   exit 0  SUCCESS   the event is gone (404, or status "cancelled")
#   exit 1  FAILURE   the event is still there and nothing is outstanding
#   exit 2  UNKNOWN   the event is still there but the deletion may yet land,
#                     or the re-read could not establish either
#   exit 3  NOT FOUND the DELETE itself answered 404/410 - the event id (or
#                     calendar id) did not exist before this script ran, so
#                     nothing was deleted. A re-read that also 404s is not
#                     evidence of a completed deletion; check the id.
#
# Callers must treat exit 2 as "do not act": in particular, never create a
# replacement event until a delete is observed complete.
#
# Deliberately narrow: it can only ever issue one DELETE and one GET against
# calendar-agent's /calendars/{id}/events/{id} route. It never retries, because
# a retry enqueues a second approval request for the same operation.

set -uo pipefail

EVENT_ID="${1:?usage: calendar-delete-event.sh <event_id> [calendar_id]}"
CAL_ID="${2:-robergb@dm.org}"
: "${CALENDAR_AGENT_URL:?CALENDAR_AGENT_URL is not set}"
MAX_TIME="${CALENDAR_DELETE_MAX_TIME:-90}"

enc() { python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }

URL="$CALENDAR_AGENT_URL/calendars/$(enc "$CAL_ID")/events/$(enc "$EVENT_ID")"

# Read a field out of a JSON body without assuming it parses at all.
json_field() {
    printf '%s' "$1" | python3 -c '
import json, sys
path = sys.argv[1].split(".")
try:
    node = json.load(sys.stdin)
except Exception:
    print("<unparseable>")
    raise SystemExit(0)
for key in path:
    if not isinstance(node, dict) or key not in node:
        print("<absent>")
        raise SystemExit(0)
    node = node[key]
if node is True:
    print("true")
elif node is False:
    print("false")
elif node is None:
    print("<absent>")
else:
    print(node)
' "$2"
}

# ---------------------------------------------------------------------------
# 1. Ask for the deletion. Capture body *and* status; a discarded body is how
#    a 200/success:false got read as success on 2026-08-07.
# ---------------------------------------------------------------------------
resp=$(curl -s --max-time "$MAX_TIME" -w '\n%{http_code}' -X DELETE "$URL" 2>/dev/null)
curl_rc=$?

if [ "$curl_rc" -ne 0 ]; then
    claim="unknown"
    echo "DELETE $CAL_ID event $EVENT_ID -> no response (curl exit $curl_rc after ${MAX_TIME}s)"
else
    code="${resp##*$'\n'}"
    payload="${resp%$'\n'*}"
    body_success=$(json_field "$payload" "success")
    echo "DELETE $CAL_ID event $EVENT_ID -> HTTP $code (body success: $body_success)"

    case "$code" in
        204)                    claim="deleted" ;;
        200) [ "$body_success" = "true" ] && claim="deleted" || claim="failed" ;;
        408|504)                claim="unknown" ;;
        404|410)                claim="absent" ;;
        *)                      claim="failed" ;;
    esac
fi

# ---------------------------------------------------------------------------
# 2. Re-read the event. This, not the answer above, is what decides.
# ---------------------------------------------------------------------------
vresp=$(curl -s --max-time "$MAX_TIME" -w '\n%{http_code}' "$URL" 2>/dev/null)
vcurl_rc=$?

if [ "$vcurl_rc" -ne 0 ]; then
    verify="inconclusive"
    echo "VERIFY $EVENT_ID -> no response (curl exit $vcurl_rc after ${MAX_TIME}s)"
else
    vcode="${vresp##*$'\n'}"
    vpayload="${vresp%$'\n'*}"
    vstatus=$(json_field "$vpayload" "event.status")
    echo "VERIFY $EVENT_ID -> HTTP $vcode (event status: $vstatus)"

    case "$vcode" in
        404|410) verify="gone" ;;
        200)
            case "$vstatus" in
                cancelled)              verify="gone" ;;
                "<absent>"|"<unparseable>") verify="inconclusive" ;;
                *)                      verify="present" ;;
            esac
            ;;
        *) verify="inconclusive" ;;
    esac
fi

# ---------------------------------------------------------------------------
# 3. Reconcile. "Still present" only means failure when nothing is pending.
# ---------------------------------------------------------------------------
case "$verify" in
    gone)
        if [ "$claim" = "absent" ]; then
            echo "RESULT: NOT FOUND - no such event before the delete was" \
                 "attempted (nothing was deleted)"
            exit 3
        fi
        echo "RESULT: SUCCESS - event no longer present"
        exit 0
        ;;
    present)
        if [ "$claim" = "unknown" ]; then
            echo "RESULT: UNKNOWN - event still present, but the deletion had no" \
                 "answer and may still be applied when the operator approves it." \
                 "Re-verify before creating anything in its place."
            exit 2
        fi
        echo "RESULT: FAILURE - event still present (delete reported: $claim)"
        exit 1
        ;;
    *)
        echo "RESULT: UNKNOWN - could not re-read the event, so the outcome is" \
             "unestablished (delete reported: $claim). Re-verify before acting."
        exit 2
        ;;
esac
