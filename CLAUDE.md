# Claude Code Guidelines for Calendar Agent

## Project Overview

Calendar Agent is a FastAPI server that provides a privacy-focused interface between AI orchestrators and the Google Calendar API. It processes calendar data locally and returns only metadata and LLM-generated summaries to calling agents.

## Key Architecture Decisions

1. **Minimal Architecture**: Single-module design mirroring the email-agent pattern
2. **Stateless Operations**: No database; all state lives in the Google Calendar backend
3. **LLM Abstraction**: Provider interface allows swapping between local MLX and hosted APIs
4. **Privacy-First**: Event content processed locally; only summaries returned to cloud

## File Structure

- `calendar_agent/calendar_server.py` - Main FastAPI app with all endpoints
- `calendar_agent/proxy_client.py` - HTTP client for api-proxy server
- `calendar_agent/llm_service.py` - LLM provider abstraction
- `calendar_agent/calendar_utils.py` - Utility functions
- `calendar_agent/exceptions.py` - Custom exception classes

## Running the Server

```bash
uv run python -m calendar_agent.calendar_server
```

## Running Tests

```bash
uv run pytest                    # All tests
uv run pytest -v                 # Verbose output
uv run pytest --cov=calendar_agent  # With coverage
```

## Code Style

- Use ruff for linting and formatting
- Follow existing patterns in the codebase
- All async operations use httpx
- Response models follow success/error pattern

## Common Tasks

### Adding a New Endpoint

1. Define request/response Pydantic models in `calendar_server.py`
2. Add the endpoint function with FastAPI decorators
3. Add tests in `test_calendar_server.py`
4. Document in README.md (required by documentation tests)

### Modifying LLM Behavior

- System prompts are in `llm_service.py`
- Always include security warnings about untrusted content
- Use THINKING_PATTERN regex to strip Qwen3 thinking tags

### Updating the api-proxy Contract Snapshot

- `docs/api-proxy-openapi-doc.json` is a stamped snapshot of the api-proxy
  OpenAPI spec (`x-generated-from` records source commit and time) — the
  authoritative spec is generated at runtime by the api-proxy FastAPI app
- `tests/test_proxy_contract.py` fails if `proxy_client.py` calls a route the
  snapshot doesn't contain; refresh the snapshot in the same change that adds
  the client method
- Refresh with `uv run python scripts/refresh_openapi.py --checkout <path>`
  (local api-proxy checkout) or `--url <proxy-url>` (running instance)

### Error Handling

- Use `ProxyAuthError` for 401 responses
- Use `ProxyForbiddenError` for 403 responses (policy blocks and operator
  rejections — the proxy blocks mutations in-line for human approval, so a
  403 means rejected, never "confirmation pending")
- Use `ProxyNotFoundError` for 404 responses (the calendar or event does not
  exist — the *expected* answer when verifying that a delete took effect, so
  it must not be folded into the generic upstream-error bucket)
- Use `ProxyTimeoutError` when the proxy doesn't answer before the client
  timeout (outcome unknown — the mutation may still complete if approved)
- Use `ProxyError` for other proxy errors
- Use `LLMError` for LLM failures
- Always return the `{"success": false, "error": "..."}` envelope via
  `error_response(...)` so the HTTP status agrees with the body (issue #4):
  403 forbidden/rejected, 404 absent, 504 outcome unknown, 502 upstream
  proxy/LLM failure (or a delete the proxy claimed but the re-read
  contradicts), 500 unexpected — never 200 for a failure. Caller errors are
  422 from request validation (no envelope), raised *before* anything is
  sent upstream — e.g. `BulkOperation`'s validator requiring `updates` for
  update/patch. Nothing returns 400
- Every mutation envelope carries an `outcome` (`OperationOutcome`):
  `succeeded` / `failed` / `unknown`, plus `not_attempted` for bulk items.
  Never collapse `unknown` into `failed`: a timed-out mutation may still be
  applied when the operator approves it, and treating that as failure is what
  produced the duplicate-event incident in issue #4
- **Deletes are verified server-side** (`verified_delete`): the proxy's
  answer is a claim; the re-read (`event_presence`: gone / present /
  inconclusive) decides. 200 only after the event is observed gone; a
  success claim with the event still present is 502/`failed`; a timeout with
  it still present is 504/`unknown`; an unreadable re-read is 504/`unknown`.
  403 and 404 from the delete are definitive and skip the re-read
- `/bulk-actions` status is `bulk_status_code(codes)`, ranked by
  `BULK_STATUS_PRECEDENCE` (504 > 403 > 502 > 500 > 404) — never by position.
  The envelope `error` is always set when any item did not succeed
  (`bulk_error_summary`). After the first `unknown` outcome the loop stops
  and the remaining items are `not_attempted` (the operator is not
  answering; each further gated call would hold the connection another full
  `CONFIRM_TIMEOUT` and queue another approval)
- Mutations use `CONFIRM_TIMEOUT` (env `PROXY_CONFIRM_TIMEOUT`, default
  window + 30s = 330s; `resolve_confirm_timeout` refuses `nan`/`inf` and any
  value below `PROXY_CONFIRMATION_WINDOW + CONFIRM_TIMEOUT_MARGIN`); reads
  use `READ_TIMEOUT`
- `PROXY_CONFIRMATION_WINDOW` (env, default 300s) is a *copy* of api-proxy's
  `--confirmation-timeout`, which the proxy does not expose on `/health`. The
  guard is honest only while the two are kept in step by hand; changing one
  without the other re-creates issue #4 silently. Refuses `<= 0` (the proxy
  reads that as "wait forever", which nothing can outlive)

### Wrapper Scripts

- `scripts/calendar-delete-event.sh` is the supported way to delete an event
  from a script. It re-reads the event to decide, and exits `0` success /
  `1` failure / `2` unknown / `3` not found (the DELETE itself 404/410'd —
  the id never existed, nothing was deleted). Exit `2` means do not act —
  never create a replacement event until a deletion has been observed
  complete
- It is covered end to end by `tests/test_delete_event_script.py`, which runs
  the real script against a loopback stub of calendar-agent

## Testing Guidelines

- Mock `get_calendar_client()` and `get_llm_service()` in tests
- Use `subtests` for documentation verification tests
- Test both success and error paths
- Sample data is in `tests/conftest.py`

## Dependencies

Production:
- fastapi, uvicorn - Web framework
- httpx - Async HTTP client
- pydantic - Data validation
- python-dotenv - Environment variables

Development:
- pytest, pytest-asyncio - Testing
- pytest-subtests - Documentation tests
- ruff - Linting and formatting
