"""Shared environment for full-loop end-to-end tests.

Each loop test proves *the next pack reflects the signal we wrote* —
through real public surfaces, not the internal ``MutationExecutor``.
The inside-out version of these loops already exists in
``eval/scenarios/agent_loop_convergence/``; the loop tests under this
directory port the assertions to the user-facing surface.

Today's REST API exposes ``POST /api/v1/packs/{pack_id}/feedback``
with ``success`` / ``notes`` only — no per-item ``helpful_item_ids``
support. The MCP ``record_feedback`` tool already accepts those
fields and emits a ``FEEDBACK_RECORDED`` event with them in the
payload, which is exactly what
:func:`trellis.retrieve.effectiveness.analyze_effectiveness` reads.
The cleanest *outside-in* path that doesn't add new product surface
is to drive feedback through the MCP client and everything else
through REST. Both are real public surfaces; both are spawned against
the same live Neon + AuraDB backend so they observe the same events.

Skipped when ``TRELLIS_TEST_NEO4J_URI`` *or* ``TRELLIS_TEST_PG_DSN``
isn't set — same gating as the API + SDK suites.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from tests.integration._live_server import (
    NEO4J_URI,
    PG_DSN,
    build_subprocess_env,
    find_console_script,
    free_port,
    spawn_uvicorn,
    terminate_subprocess,
    wait_for_healthz,
    wipe_live_state_for_config,
    write_cloud_config,
)


@dataclass(frozen=True)
class LoopEnvironment:
    """Both public surfaces wired to the same live backend.

    ``api_url`` is the base URL of a running uvicorn; ``mcp`` is a
    ``fastmcp.Client`` connected to a separate ``trellis-mcp``
    subprocess. Both processes share the same ``config_dir`` (and
    therefore the same Neon + AuraDB backend), so an event one writes
    is visible to the other.

    ``data_dir`` is the SQLite/JSON-store root the subprocesses point
    at via ``TRELLIS_DATA_DIR`` — exposed so loop tests can reach into
    file-backed stores (advisory JSON, parameter overrides) without
    re-deriving the path.
    """

    api_url: str
    mcp: Client
    config_dir: Path
    data_dir: Path


@pytest_asyncio.fixture
async def loop_env(tmp_path: Path) -> AsyncIterator[LoopEnvironment]:
    """Spawn both uvicorn and trellis-mcp against the same live backend.

    Wipes persistent state once before yielding. Both subprocesses
    point at the same ``config_dir``, so they share a registry view
    over the live Neon Postgres + AuraDB Neo4j cluster. Tests can
    drive ingest + retrieval via the REST URL and per-item feedback
    via the MCP client without worrying about state divergence.
    """
    if not NEO4J_URI or not PG_DSN:
        pytest.skip(
            "TRELLIS_TEST_NEO4J_URI and TRELLIS_TEST_PG_DSN must be set for loop tests"
        )

    mcp_bin = find_console_script(
        "trellis-mcp", install_hint="install with `pip install -e .`"
    )

    config_dir = tmp_path / ".trellis"
    data_dir = tmp_path / "data"
    write_cloud_config(config_dir)
    subprocess_env = build_subprocess_env(config_dir, data_dir)

    # Wipe Neon + AuraDB before either subprocess opens its connections,
    # so the registry instances they construct never see stale rows.
    wipe_live_state_for_config(
        config_dir,
        env={
            "TRELLIS_CONFIG_DIR": str(config_dir),
            "TRELLIS_DATA_DIR": str(data_dir),
            "TRELLIS_KNOWLEDGE_PG_DSN": PG_DSN,
            "TRELLIS_OPERATIONAL_PG_DSN": PG_DSN,
        },
    )

    port = free_port()
    api_url = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "uvicorn.log"
    api_proc = spawn_uvicorn(subprocess_env, port, log_path=log_path)

    try:
        wait_for_healthz(api_proc, api_url, log_path=log_path)

        transport = StdioTransport(
            command=mcp_bin,
            args=[],
            env=subprocess_env,
        )
        async with Client(transport, timeout=30.0) as mcp_client:
            yield LoopEnvironment(
                api_url=api_url,
                mcp=mcp_client,
                config_dir=config_dir,
                data_dir=data_dir,
            )
    finally:
        terminate_subprocess(api_proc)


# ── Shared seed/build helpers used across loop tests ──────────────────


def seed_distractor_corpus(
    api_url: str,
    *,
    domain: str = "loop-test",
    distractor_id: str = "loop:doc:distractor",
    helpful_ids: tuple[str, ...] = (
        "loop:doc:helpful-1",
        "loop:doc:helpful-2",
        "loop:doc:helpful-3",
        "loop:doc:helpful-4",
    ),
    intent_token: str = "noisedemote",  # noqa: S107 — token marker, not a credential
) -> tuple[str, list[str]]:
    """Seed a 5-document corpus with one designated distractor.

    All documents share an ``intent_token`` in their content so that
    ``GET /api/v1/search`` and ``POST /api/v1/packs`` both retrieve
    them as a set. The distractor's content is the same length as the
    helpful docs so token-budget pressure isn't biased toward keeping
    or dropping it for irrelevant reasons.

    Returns ``(distractor_id, [helpful_ids…])`` for the test to assert
    against.
    """
    with httpx.Client(base_url=api_url, timeout=15.0) as client:
        for doc_id in helpful_ids:
            resp = client.post(
                "/api/v1/documents",
                json={
                    "doc_id": doc_id,
                    "content": (
                        f"{intent_token} reference document {doc_id}. "
                        f"This document covers the canonical {intent_token} "
                        "workflow and should appear in helpful packs."
                    ),
                    "metadata": {"domain": domain, "source": "loop-test"},
                },
            )
            assert resp.status_code == 200, resp.text

        resp = client.post(
            "/api/v1/documents",
            json={
                "doc_id": distractor_id,
                "content": (
                    f"{intent_token} distractor document. Mentions "
                    f"{intent_token} but contains no canonical workflow."
                ),
                "metadata": {"domain": domain, "source": "loop-test"},
            },
        )
        assert resp.status_code == 200, resp.text

    return distractor_id, list(helpful_ids)


def build_pack(
    api_url: str,
    *,
    intent: str,
    domain: str = "loop-test",
    max_items: int = 10,
    max_tokens: int = 2000,
    tag_filters: dict[str, list[str]] | None = None,
) -> dict:
    """Assemble a context pack via the live REST API.

    Returns the parsed JSON body. Caller asserts on ``pack_id``,
    ``items``, etc.

    Pass ``tag_filters={}`` to opt in to the default
    ``signal_quality=["high","standard","low"]`` filter that excludes
    noise-tagged items. ``PackBuilder._build_filters`` only applies
    that default when ``tag_filters`` is non-None — passing ``None``
    (the default) leaves noise items in the candidate set.
    """
    body: dict = {
        "intent": intent,
        "domain": domain,
        "max_items": max_items,
        "max_tokens": max_tokens,
    }
    if tag_filters is not None:
        body["tag_filters"] = tag_filters
    with httpx.Client(base_url=api_url, timeout=15.0) as client:
        resp = client.post("/api/v1/packs", json=body)
        assert resp.status_code == 200, resp.text
        return resp.json()


def trigger_apply_noise_tags(api_url: str, *, days: int = 30) -> dict:
    """Run the effectiveness → noise-tag pipeline via REST.

    Returns the report body. Caller can read
    ``noise_candidates_tagged`` to verify the loop fired.
    """
    with httpx.Client(base_url=api_url, timeout=30.0) as client:
        resp = client.post(
            "/api/v1/effectiveness/apply-noise-tags",
            params={"days": days, "min_appearances": 1},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()
