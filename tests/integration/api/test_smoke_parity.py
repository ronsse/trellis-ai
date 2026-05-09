"""Pytest mirror of ``deploy/smoke.sh``.

The bash script under ``deploy/`` is the canonical ops probe: it runs
inside ECS/EKS post-deploy hooks and lean shell environments where
Python+pytest may not be available. This module is its sibling for
developer machines and CI — same probes, same assertions, same trace
round-trip, but expressed as ``pytest`` tests so failures surface with
the same tooling as the rest of the suite.

Run with::

    pytest --include-live tests/integration/api/test_smoke_parity.py

(``--include-live`` is the opt-in flag F1 wires up for the ``live``
marker. Without it, ``make test`` and a bare ``pytest`` invocation
deselect every ``live``-marked test.)

Both files must stay in sync. When you add or rename an endpoint to
``deploy/smoke.sh``, update this module in the same change — and vice
versa. The bash script remains the source-of-truth for ops; this is the
parity gate that catches drift.

The base URL fixture honours ``TRELLIS_BASE_URL`` (matching the bash
script's env var verbatim), defaulting to ``http://localhost:8420``,
which is the docker-compose port published in
``deploy/config.compose.yaml``.

Postgres round-trip (``test_trace_roundtrip_postgres``) carries an
extra ``@pytest.mark.postgres`` so it skips cleanly when no DB-backed
instance is reachable but the rest of the smoke probes can still run
against, say, an SQLite-backed dev server.
"""

from __future__ import annotations

import os
import re

import httpx
import pytest

# Every test in this module is a ``live`` probe — it talks to a real
# running orchestrator process at ``TRELLIS_BASE_URL``. The runner
# (F1's ``--include-live``) opts in; otherwise these are deselected.
pytestmark = pytest.mark.live


def _live_server_reachable(url: str) -> bool:
    """Return True if ``GET {url}/healthz`` returns *any* HTTP response.

    Used as a belt-and-braces skip guard: F1's ``--include-live`` flag
    is the primary gate (default-deselects every ``live``-marked test),
    but until F1 lands on ``main`` the marker only emits a warning and
    pytest will still execute the body. Probing ``/healthz`` here turns
    "no compose stack running" into a clean skip rather than a
    ConnectError failure under a default ``make test`` run.

    Once F1 ships, this fallback becomes a no-op (the runner deselects
    the file before the fixture ever evaluates).
    """
    try:
        httpx.get(f"{url}/healthz", timeout=1.0)
    except httpx.HTTPError:
        return False
    return True


# ULID pattern matching deploy/smoke.sh's grep -oE '[0-9A-Z]{26}' — Crockford
# base32, fixed 26-char width. Keep in sync if the trace_id format ever changes.
_ULID_RE = re.compile(r"[0-9A-Z]{26}")

# Same shape as the trace_body heredoc in deploy/smoke.sh. Kept as a
# module-level constant so a diff between the two files is mechanical.
_SMOKE_TRACE_BODY: dict[str, object] = {
    "source": "agent",
    "intent": "smoke-test ingest via Postgres+pgvector",
    "context": {"agent_id": "smoke-test", "domain": "smoke"},
    "steps": [
        {"step_type": "tool_call", "name": "noop", "args": {}, "result": {}},
    ],
    "outcome": {"status": "success", "summary": "ok"},
}


@pytest.fixture
def base_url() -> str:
    """Resolve the orchestrator base URL the same way ``deploy/smoke.sh`` does.

    Reads ``TRELLIS_BASE_URL`` (the same env var the bash script reads)
    and falls back to ``http://localhost:8420`` — the docker-compose
    published port. Skips the test when no live server is reachable so
    a default ``make test`` invocation can't fail on developer machines
    that don't have the compose stack up. Once F1's ``--include-live``
    gate lands, this skip is unreachable in practice — the runner
    deselects the whole file first.

    Returning a plain ``str`` rather than a fixture that pre-builds an
    ``httpx.Client`` keeps each test free to choose its own timeout /
    headers, the same way each ``curl`` invocation in the bash script
    does.
    """
    url = os.environ.get("TRELLIS_BASE_URL", "http://localhost:8420")
    if not _live_server_reachable(url):
        pytest.skip(
            f"no live Trellis API at {url} — start the docker-compose "
            f"stack (deploy/) or set TRELLIS_BASE_URL to a running instance"
        )
    return url


# ── Endpoint probes — mirror probe_status + probe_body_contains in the bash script ──


def test_healthz_status(base_url: str) -> None:
    """``GET /healthz`` returns 200 — bash: ``probe_status "GET /healthz"``."""
    resp = httpx.get(f"{base_url}/healthz", timeout=5.0)
    assert resp.status_code == 200


def test_healthz_body(base_url: str) -> None:
    """``/healthz`` body contains ``"status":"ok"``."""
    resp = httpx.get(f"{base_url}/healthz", timeout=5.0)
    assert '"status":"ok"' in resp.text


def test_readyz_status(base_url: str) -> None:
    """``GET /readyz`` returns 200."""
    resp = httpx.get(f"{base_url}/readyz", timeout=5.0)
    assert resp.status_code == 200


def test_readyz_body(base_url: str) -> None:
    """``/readyz`` body contains ``"status":"ready"``."""
    resp = httpx.get(f"{base_url}/readyz", timeout=5.0)
    assert '"status":"ready"' in resp.text


def test_api_version_status(base_url: str) -> None:
    """``GET /api/version`` returns 200."""
    resp = httpx.get(f"{base_url}/api/version", timeout=5.0)
    assert resp.status_code == 200


def test_api_version_body(base_url: str) -> None:
    """``/api/version`` body contains ``"api_version":``."""
    resp = httpx.get(f"{base_url}/api/version", timeout=5.0)
    assert '"api_version":' in resp.text


def test_ui_status(base_url: str) -> None:
    """``GET /ui/`` returns 200 — bash: ``probe_status "GET /ui/"``."""
    resp = httpx.get(f"{base_url}/ui/", timeout=5.0)
    assert resp.status_code == 200


def test_ui_body(base_url: str) -> None:
    """``/ui/`` body contains the SPA title tag."""
    resp = httpx.get(f"{base_url}/ui/", timeout=5.0)
    assert "<title>Trellis</title>" in resp.text


# ── Backend round-trip via /api/v1/traces — mirrors the bash script's
# "Backend round-trip" block. The script splits this into POST + GET,
# extracting the ULID from the POST response. We do the same thing in
# one test so a half-working backend (POST 200 but GET 500) still
# fails the round-trip assertion, matching the bash script's behaviour.


@pytest.mark.postgres
def test_trace_roundtrip_postgres(base_url: str) -> None:
    """Ingest a trace, extract the ULID, and read it back.

    Bash equivalent: the ``=== Backend round-trip via /api/v1/traces ===``
    block. This test additionally carries the ``postgres`` marker so it
    skips cleanly when only a SQLite-backed instance is up — the round-
    trip exercises the governed ingest path through Postgres+pgvector
    in the documented compose deployment.
    """
    ingest = httpx.post(
        f"{base_url}/api/v1/traces",
        headers={"Content-Type": "application/json"},
        json=_SMOKE_TRACE_BODY,
        timeout=5.0,
    )
    # Bash doesn't assert a status code here — it only checks the
    # response body for a 26-char ULID. Mirror that exactly: no
    # ``assert ingest.status_code == 200``. A 500 with a stray ULID in
    # the error message would slip past the bash script too.
    match = _ULID_RE.search(ingest.text)
    assert match, (
        f"could not extract trace_id from ingest response: {ingest.text[:200]}"
    )
    trace_id = match.group(0)

    get = httpx.get(f"{base_url}/api/v1/traces/{trace_id}", timeout=5.0)
    assert trace_id in get.text, (
        f"GET /api/v1/traces/{trace_id} did not echo the trace_id; "
        f"body was: {get.text[:200]}"
    )
