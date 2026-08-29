"""Shared environment for full-loop end-to-end tests.

Each loop test proves *the next pack reflects the signal we wrote* —
through real public surfaces, not the internal ``MutationExecutor``.
The inside-out version of these loops already exists in
``eval/scenarios/agent_loop_convergence/``; the loop tests under this
directory port the assertions to the user-facing surface.

The REST API exposes ``POST /api/v1/packs/{pack_id}/feedback`` with a
``PackFeedbackRequest`` body carrying ``success`` plus per-item
``helpful_item_ids`` / ``unhelpful_item_ids`` / ``followed_advisory_ids``;
the MCP ``record_feedback`` tool accepts the same fields. Both surfaces
emit a ``FEEDBACK_RECORDED`` event with those ids in the payload, which
is exactly what
:func:`trellis.retrieve.effectiveness.analyze_effectiveness` reads.
Some loop tests still drive feedback through the MCP client and
everything else through REST — both are real public surfaces, and both
are spawned against the same live Neon + AuraDB backend so they observe
the same events.

**Single-token intents.** Loop tests use single-token ``_INTENT``
markers (e.g. ``"reconcile"``, ``"learnpromote"``) rather than
human-readable phrases. Postgres FTS tokenizes a multi-word intent
into terms that all have to appear in document content — so a probe
like ``"noise demote loop"`` retrieves nothing once the seeded docs
only contain a single shared token. Each loop test re-uses this
convention; no need to re-explain at every call site.

Skipped when ``TRELLIS_TEST_NEO4J_URI`` *or* ``TRELLIS_TEST_PG_DSN``
isn't set — same gating as the API + SDK suites.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
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
from trellis.classify.demotion_gate import (
    MIN_ATTRIBUTED_PACKS,
    MIN_UNHELPFUL_CITATIONS,
    REFUSED_THIN_CORPUS,
)
from trellis.stores.registry import StoreRegistry

#: Graded rounds a demote loop drives before the evidence gate will
#: admit anything.
#:
#: Since #380 a demotion needs two independent things: at least
#: ``MIN_ATTRIBUTED_PACKS`` packs in the window carrying a per-item
#: verdict, **and** at least ``MIN_UNHELPFUL_CITATIONS`` explicit
#: ``unhelpful_item_ids`` citations naming the item. One round of
#: serve-then-grade supplies one of each, so the coverage floor is the
#: binding one and the round count follows it.
#:
#: Derived from the constants rather than written as a literal so the
#: fixtures track the policy. The constants themselves are pinned by
#: value in ``tests/unit/classify/test_demotion_gate.py`` — that is
#: where a change to the *policy* has to be argued, not here.
DEMOTION_ROUNDS = max(MIN_ATTRIBUTED_PACKS, MIN_UNHELPFUL_CITATIONS)


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


def build_pack_with_distractor(
    api_url: str,
    *,
    intent: str,
    distractor_id: str,
    helpful_ids: list[str],
) -> tuple[dict, list[str]]:
    """Assemble a pack and check it carries the contrast the loop needs.

    A demote loop is only meaningful while the distractor is still being
    served *and* at least one helpful doc rides alongside it — without
    the second, the feedback signal has nothing to contrast against and
    a green test would prove nothing. Both loops re-check this every
    round rather than once, because a round that quietly stopped
    serving the distractor would silently stop contributing evidence.

    Returns ``(pack, helpful_ids_present_in_it)``.
    """
    pack = build_pack(api_url, intent=intent, tag_filters={})
    served = item_ids(pack)
    assert distractor_id in served, (
        f"the loop's value depends on the distractor being served; "
        f"got items={sorted(served)}"
    )
    helpful_present = sorted(served.intersection(helpful_ids))
    assert helpful_present, (
        "expected at least one helpful doc alongside the distractor — "
        "without one, the feedback signal carries no contrast"
    )
    return pack, helpful_present


def assert_demotion_withheld_below_floor(
    report: dict, *, attributed_packs: int
) -> None:
    """Assert the gate refused this batch on corpus coverage, and said so.

    The refusal is as much the governed behaviour as the demotion is
    (#380 — demote on evidence of unhelpfulness, never on absence of
    praise), so both loops pin it before they clear the floor. Reading
    ``attributed_packs`` back off the screen is what makes this a
    measurement rather than a restatement: it fails both if the gate
    stops refusing and if the loop miscounts the evidence it supplied.
    """
    assert report["noise_candidates_tagged"] == 0, report
    assert report["noise_candidates_proposed"] >= 1, (
        f"the usage-rate rule should still *propose* the distractor — "
        f"without a proposal the gate is never consulted: {report}"
    )
    screen = report["demotion_screen"]
    assert screen["suppressed"] is True, screen
    assert screen["suppressed_reason"] == REFUSED_THIN_CORPUS, screen
    assert screen["attributed_packs"] == attributed_packs, screen
    assert screen["min_attributed_packs"] == MIN_ATTRIBUTED_PACKS, screen


def assert_demotion_admitted(report: dict, *, item_id: str) -> None:
    """Assert the gate admitted ``item_id`` on the evidence supplied.

    Asserts on the screen's own decision row, not just on the tag count:
    the point of the loop is that the *evidence path* carried the
    citations through to the gate, and a count alone cannot distinguish
    that from a gate that stopped reading them.
    """
    screen = report["demotion_screen"]
    assert screen["suppressed"] is False, screen
    assert item_id in screen["admitted"], screen
    # Looked up defensively: a bare ``next(...)`` raises StopIteration,
    # which pytest reports without ever naming the id that was missing.
    decision = next((d for d in screen["decisions"] if d["item_id"] == item_id), None)
    assert decision is not None, (
        f"{item_id!r} is in `admitted` but has no decision row — the screen "
        f"contradicts itself: {screen}"
    )
    assert decision["admitted"] is True, decision
    assert decision["unhelpful_count"] >= MIN_UNHELPFUL_CITATIONS, decision
    assert decision["helpful_count"] < decision["unhelpful_count"], decision


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


def item_ids(pack: dict) -> set[str]:
    """Pull ``item_id`` out of every entry in a pack response body."""
    return {item["item_id"] for item in pack["items"]}


@contextmanager
def live_registry(config_dir: Path, data_dir: Path) -> Iterator[StoreRegistry]:
    """Yield a ``StoreRegistry`` pointing at the loop-test backends.

    Mirrors the env-var dance ``wipe_live_state_for_config`` performs:
    plane-aware DSN resolution reads ``TRELLIS_KNOWLEDGE_PG_DSN`` /
    ``TRELLIS_OPERATIONAL_PG_DSN`` from the process env, so we set
    them just for the registry's lifetime and restore on exit. The
    Neo4j credentials live in the cloud-config YAML, so they don't
    need an env-var bridge.
    """
    env_overrides = {
        "TRELLIS_CONFIG_DIR": str(config_dir),
        "TRELLIS_DATA_DIR": str(data_dir),
        "TRELLIS_KNOWLEDGE_PG_DSN": PG_DSN or "",
        "TRELLIS_OPERATIONAL_PG_DSN": PG_DSN or "",
    }
    saved = {k: os.environ.get(k) for k in env_overrides}
    try:
        os.environ.update(env_overrides)
        registry = StoreRegistry.from_config_dir(config_dir=config_dir)
        try:
            yield registry
        finally:
            registry.close()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
