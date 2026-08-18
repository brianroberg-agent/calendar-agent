"""Refresh docs/api-proxy-openapi-doc.json from the authoritative api-proxy spec.

The snapshot records the proxy contract this agent was built against. api-proxy
has no checked-in spec file (FastAPI generates it at runtime), so this script
regenerates the snapshot and stamps it with provenance (source, commit,
timestamp) so staleness is diagnosable. Run it whenever the proxy contract
changes:

    # From a local api-proxy checkout (imports the app via `uv run --project`)
    uv run python scripts/refresh_openapi.py --checkout ../api-proxy

    # From a running proxy instance
    uv run python scripts/refresh_openapi.py --url http://localhost:8000

tests/test_proxy_contract.py verifies that every route CalendarProxyClient
calls exists in the snapshot, so a stale snapshot fails the test suite rather
than silently drifting.
"""

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "docs" / "api-proxy-openapi-doc.json"

API_PROXY_REPO = "https://github.com/brianroberg/api-proxy"

_DUMP_SPEC_CODE = "import json; from api_proxy.main import app; print(json.dumps(app.openapi()))"


def spec_from_checkout(checkout: Path) -> dict[str, Any]:
    """Generate the spec by importing the api-proxy app from a local checkout."""
    try:
        result = subprocess.run(
            ["uv", "run", "--quiet", "--project", str(checkout), "python", "-c", _DUMP_SPEC_CODE],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        sys.exit("Failed to generate spec: 'uv' not found on PATH (install uv or use --url)")
    if result.returncode != 0:
        sys.exit(f"Failed to generate spec from {checkout}:\n{result.stderr}")
    return json.loads(result.stdout)


def commit_of_checkout(checkout: Path) -> str | None:
    """Return the checkout's HEAD commit, suffixed with -dirty if the working
    tree has uncommitted changes (the generated spec reflects the working tree,
    not HEAD). Returns None if the checkout isn't a git repo."""
    head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if head.returncode != 0:
        return None
    commit = head.stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(checkout), "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        print(
            f"warning: {checkout} has uncommitted changes; stamping commit as {commit}-dirty",
            file=sys.stderr,
        )
        commit += "-dirty"
    return commit


def spec_from_url(url: str) -> dict[str, Any]:
    """Fetch the spec from a running proxy instance's /openapi.json."""
    spec_url = f"{url.rstrip('/')}/openapi.json"
    try:
        response = httpx.get(spec_url, timeout=30.0)
        response.raise_for_status()
    except httpx.HTTPError as e:
        sys.exit(f"Failed to fetch spec from {spec_url}: {e}")
    return response.json()


def with_provenance(spec: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    """Insert the x-generated-from stamp right after "info" so it's visible near
    the top of the file."""
    stamped: dict[str, Any] = {}
    for key, value in spec.items():
        stamped[key] = value
        if key == "info":
            stamped["x-generated-from"] = provenance
    stamped.setdefault("x-generated-from", provenance)
    return stamped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--checkout",
        type=Path,
        help="Path to a local api-proxy checkout to generate the spec from",
    )
    source.add_argument(
        "--url",
        help="Base URL of a running api-proxy instance (e.g. http://localhost:8000)",
    )
    args = parser.parse_args()

    if args.checkout:
        spec = spec_from_checkout(args.checkout)
        provenance = {
            "source": API_PROXY_REPO,
            "commit": commit_of_checkout(args.checkout),
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    else:
        spec = spec_from_url(args.url)
        provenance = {
            "source": args.url,
            "commit": None,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }

    SNAPSHOT_PATH.write_text(json.dumps(with_provenance(spec, provenance), indent=2) + "\n")
    print(f"Wrote {SNAPSHOT_PATH} ({len(spec.get('paths', {}))} paths)")


if __name__ == "__main__":
    main()
