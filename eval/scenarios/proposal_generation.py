"""End-to-end eval scenario for Item 7 Phase 1 — ProposalGenerator + CLI.

Seeds a synthetic ``EXTRACTION_FAILED`` cluster directly on the
EventLog, drives :class:`~trellis_workers.code_authoring.ProposalGenerator`,
and asserts the wire contract operators will rely on:

1. The first run returns one :class:`Proposal` whose ``proposal_id`` is
   the deterministic SHA-256 of the cluster signature.
2. The run emits exactly one ``PROPOSAL_DRAFTED`` event.
3. Re-running the generator over the same window produces zero new
   ``PROPOSAL_DRAFTED`` events; the same cluster fires ``PROPOSAL_UPDATED``
   instead — that's the idempotency contract.
4. The CLI ``list-proposals`` subcommand surfaces the proposal.
5. The CLI ``show-proposal`` subcommand returns the rendered markdown.

Backend gating mirrors :mod:`eval.scenarios.observation_retrieval` and
:mod:`eval.scenarios.meta_trace_round_trip`: SQLite is unconditional;
Postgres / Neo4j register only when env credentials are present.

Runnable two ways:

* ``pytest eval/scenarios/proposal_generation.py -v`` — the contract
  the Item 7 Phase 1 brief targets and what CI exercises.
* ``from eval.scenarios.proposal_generation import run`` — minimal dict
  shape for ad-hoc invocations / scheduled multi-backend runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import structlog
from typer.testing import CliRunner

from eval._backends import (
    BackendHandle,
    get_neo4j_config,
    get_postgres_dsn,
    register_handle,
)
from trellis.stores.base.event_log import EventType
from trellis_cli.exit_codes import EXIT_OK
from trellis_workers.code_authoring import ProposalGenerator

if TYPE_CHECKING:
    from trellis_workers.code_authoring import Proposal

logger = structlog.get_logger(__name__)

#: Cluster identity for the seeded failures. The proposal_id is the
#: SHA-256 of the SHA-256 of ``f"{SCENARIO_SOURCE_FILE}|{SCENARIO_FAILURE_KIND}"``
#: (see :func:`trellis_workers.code_authoring.compute_proposal_id`).
SCENARIO_SOURCE_FILE = "src/trellis/eval/proposal_generation/seeded_extractor.py"
SCENARIO_FAILURE_KIND = "parse_error"

#: How many failures we seed per cluster. The Phase 0 generator surfaces
#: any cluster with ≥ 1 event, so a small count is enough — we keep it
#: small so the test stays fast on cloud backends.
SCENARIO_FAILURE_COUNT = 5

#: Cross-backend equivalence requires at least two backends. SQLite is
#: always present; Postgres / Neo4j are env-gated.
_MIN_BACKENDS_FOR_CROSS_CHECK = 2


# ---------------------------------------------------------------------------
# Seed model
# ---------------------------------------------------------------------------


def _expected_proposal_id() -> str:
    """Re-compute the deterministic proposal_id for the seeded cluster.

    Done independently of
    :func:`trellis_workers.code_authoring.compute_proposal_id` so the
    eval asserts the *wire* contract rather than the *implementation*.
    Same hash chain — recomputing it here means a regression in the
    generator's hashing logic would fail this scenario even if its
    own unit tests still pass against itself.
    """
    signature = hashlib.sha256(
        f"{SCENARIO_SOURCE_FILE}|{SCENARIO_FAILURE_KIND}".encode()
    ).hexdigest()
    return hashlib.sha256(signature.encode()).hexdigest()


def _seed_failure_cluster(handle: BackendHandle) -> None:
    """Emit ``SCENARIO_FAILURE_COUNT`` EXTRACTION_FAILED events on this backend.

    Direct EventLog emission rather than running the dispatcher — the
    scenario is exercising the *consumer* (ProposalGenerator + CLI),
    not the producer.
    """
    event_log = handle.registry.operational.event_log
    for i in range(SCENARIO_FAILURE_COUNT):
        event_log.emit(
            EventType.EXTRACTION_FAILED,
            source="eval.proposal_generation",
            payload={
                "source_hint": SCENARIO_SOURCE_FILE,
                "failure_kind": SCENARIO_FAILURE_KIND,
                "extractor_id": "eval.seeded_extractor",
                "extractor_tier": "deterministic",
                "error_class": "ValueError",
                "error_excerpt": f"seeded failure {i}",
            },
        )


def _run_generator(handle: BackendHandle) -> list[Proposal]:
    """Run :class:`ProposalGenerator` against ``handle``."""
    generator = ProposalGenerator(handle.registry)
    return generator.run()


def _count_events(handle: BackendHandle, event_type: EventType) -> int:
    """Count events of ``event_type`` on this backend.

    Reads with a high limit rather than calling ``count()`` so the
    scenario stays portable across backends that implement ``count()``
    differently (in particular, the SCD2 path on the Bolt backends
    has its own filter rules).
    """
    rows = handle.registry.operational.event_log.get_events(
        event_type=event_type,
        limit=10_000,
    )
    return len(rows)


# ---------------------------------------------------------------------------
# Backend wiring (matches observation_retrieval + meta_trace_round_trip)
# ---------------------------------------------------------------------------


_SQLITE_OPERATIONAL = {
    "trace": {"backend": "sqlite"},
    "event_log": {"backend": "sqlite"},
}


def _build_backends(stack: ExitStack, tmp_dir: Path) -> list[BackendHandle]:
    handles: list[BackendHandle] = []

    register_handle(
        stack,
        handles,
        name="sqlite",
        config={
            "knowledge": {
                "graph": {"backend": "sqlite"},
                "vector": {"backend": "sqlite"},
                "document": {"backend": "sqlite"},
                "blob": {"backend": "local"},
            },
            "operational": _SQLITE_OPERATIONAL,
        },
        stores_dir=tmp_dir,
    )

    pg_dsn = get_postgres_dsn()
    if pg_dsn:
        register_handle(
            stack,
            handles,
            name="postgres",
            config={
                "knowledge": {
                    "graph": {"backend": "postgres", "dsn": pg_dsn},
                    "vector": {"backend": "sqlite"},
                    "document": {"backend": "sqlite"},
                    "blob": {"backend": "local"},
                },
                "operational": _SQLITE_OPERATIONAL,
            },
            stores_dir=tmp_dir / "pg",
        )

    neo4j_graph = get_neo4j_config()
    if neo4j_graph:
        register_handle(
            stack,
            handles,
            name="neo4j",
            config={
                "knowledge": {
                    "graph": neo4j_graph,
                    "vector": {"backend": "sqlite"},
                    "document": {"backend": "sqlite"},
                    "blob": {"backend": "local"},
                },
                "operational": _SQLITE_OPERATIONAL,
            },
            stores_dir=tmp_dir / "neo4j",
        )

    return handles


# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_handle(tmp_path: Path) -> Any:
    """Seeded SQLite backend. Always available; no env gating."""
    with ExitStack() as stack:
        handles = _build_backends(stack, tmp_path)
        sqlite = next(h for h in handles if h.name == "sqlite")
        _seed_failure_cluster(sqlite)
        yield sqlite


# ---------------------------------------------------------------------------
# pytest tests — SQLite contract (always exercised)
# ---------------------------------------------------------------------------


def test_first_run_returns_one_proposal_sqlite(
    sqlite_handle: BackendHandle,
) -> None:
    """First generator run surfaces exactly one Proposal with the expected id."""
    proposals = _run_generator(sqlite_handle)
    assert len(proposals) == 1, (
        f"expected 1 proposal, got {len(proposals)}: "
        f"{[p.proposal_id for p in proposals]}"
    )
    proposal = proposals[0]
    expected_id = _expected_proposal_id()
    assert proposal.proposal_id == expected_id, (
        f"proposal_id drift: got {proposal.proposal_id!r}, "
        f"expected {expected_id!r}"
    )
    assert proposal.source_event_ids
    assert len(proposal.source_event_ids) == SCENARIO_FAILURE_COUNT


def test_first_run_emits_exactly_one_proposal_drafted_sqlite(
    sqlite_handle: BackendHandle,
) -> None:
    """First run emits PROPOSAL_DRAFTED for the cluster — exactly once."""
    _run_generator(sqlite_handle)
    drafted = _count_events(sqlite_handle, EventType.PROPOSAL_DRAFTED)
    assert drafted == 1, f"expected 1 PROPOSAL_DRAFTED event, got {drafted}"


def test_rerun_emits_zero_new_drafted_sqlite(
    sqlite_handle: BackendHandle,
) -> None:
    """Re-running over the same cluster fires PROPOSAL_UPDATED, not DRAFTED.

    This is the idempotency contract — operators can re-run the
    generator without spamming the EventLog with duplicate proposals.
    """
    _run_generator(sqlite_handle)
    drafted_after_first = _count_events(
        sqlite_handle, EventType.PROPOSAL_DRAFTED
    )
    updated_after_first = _count_events(
        sqlite_handle, EventType.PROPOSAL_UPDATED
    )

    _run_generator(sqlite_handle)
    drafted_after_second = _count_events(
        sqlite_handle, EventType.PROPOSAL_DRAFTED
    )
    updated_after_second = _count_events(
        sqlite_handle, EventType.PROPOSAL_UPDATED
    )

    assert drafted_after_second == drafted_after_first, (
        "second run emitted a new PROPOSAL_DRAFTED — idempotency broken: "
        f"first={drafted_after_first} second={drafted_after_second}"
    )
    assert updated_after_second == updated_after_first + 1, (
        "second run did not emit PROPOSAL_UPDATED — idempotency broken: "
        f"first={updated_after_first} second={updated_after_second}"
    )


def _seed_via_cli_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> str:
    """Boot the CLI registry, seed the failure cluster, drive the generator.

    The CLI's ``_get_registry`` caches its registry under
    ``TRELLIS_DATA_DIR/stores`` — a different path from where the eval
    fixture seeded its handle. Rather than try to share state across
    two store paths, we run the whole CLI-side pipeline against the
    CLI's own data directory so list-proposals / show-proposal see
    exactly the same SQLite file the seed wrote into.

    Returns the expected proposal_id so callers can assert against it
    without re-deriving the hash chain.
    """
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "cli_config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "cli_data"))

    from trellis_cli.main import app as root_app  # noqa: PLC0415
    from trellis_cli.stores import _get_registry, _reset_registry  # noqa: PLC0415

    _reset_registry()
    runner = CliRunner()
    init_result = runner.invoke(root_app, ["admin", "init"])
    assert init_result.exit_code == EXIT_OK, init_result.output

    # Seed against the CLI's own registry so the data lives where the
    # CLI's subsequent commands will look for it.
    cli_registry = _get_registry()
    event_log = cli_registry.operational.event_log
    for i in range(SCENARIO_FAILURE_COUNT):
        event_log.emit(
            EventType.EXTRACTION_FAILED,
            source="eval.proposal_generation",
            payload={
                "source_hint": SCENARIO_SOURCE_FILE,
                "failure_kind": SCENARIO_FAILURE_KIND,
                "extractor_id": "eval.seeded_extractor",
                "extractor_tier": "deterministic",
                "error_class": "ValueError",
                "error_excerpt": f"seeded failure {i}",
            },
        )
    ProposalGenerator(cli_registry).run()
    return _expected_proposal_id()


def test_cli_list_proposals_surfaces_the_proposal_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``trellis admin list-proposals`` returns the seeded proposal."""
    expected_id = _seed_via_cli_registry(monkeypatch, tmp_path)

    from trellis_cli.main import app as root_app  # noqa: PLC0415

    runner = CliRunner()
    result = runner.invoke(
        root_app, ["admin", "list-proposals", "--format", "json"]
    )
    assert result.exit_code == EXIT_OK, result.output
    data = json.loads(result.stdout.strip())
    assert data["count"] == 1
    assert data["proposals"][0]["proposal_id"] == expected_id


def test_cli_show_proposal_returns_markdown_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``trellis admin show-proposal`` returns the rendered markdown."""
    expected_id = _seed_via_cli_registry(monkeypatch, tmp_path)

    from trellis_cli.main import app as root_app  # noqa: PLC0415

    runner = CliRunner()
    result = runner.invoke(
        root_app,
        ["admin", "show-proposal", expected_id, "--format", "json"],
    )
    assert result.exit_code == EXIT_OK, result.output
    data = json.loads(result.stdout.strip())
    assert data["proposal_id"] == expected_id
    assert data["markdown"].startswith(
        f"# Proposal: address {SCENARIO_FAILURE_KIND} in {SCENARIO_SOURCE_FILE}"
    )


# ---------------------------------------------------------------------------
# Cross-backend equivalence
# ---------------------------------------------------------------------------


def test_cross_backend_equivalence(tmp_path: Path) -> None:
    """Every backend the env provides must agree on the wire contract.

    Drives the seed + generator on each registered backend and asserts
    that the proposal_id + drafted/updated counts match across all of
    them. SQLite-only environments skip — the assertion is meaningless
    with one backend.
    """
    with ExitStack() as stack:
        handles = _build_backends(stack, tmp_path)
        if len(handles) < _MIN_BACKENDS_FOR_CROSS_CHECK:
            pytest.skip(
                "cross-backend assertions require at least "
                f"{_MIN_BACKENDS_FOR_CROSS_CHECK} backends; "
                f"got {[h.name for h in handles]}"
            )

        outcomes: dict[str, dict[str, Any]] = {}
        expected_id = _expected_proposal_id()
        for handle in handles:
            _seed_failure_cluster(handle)
            first = _run_generator(handle)
            second = _run_generator(handle)
            outcomes[handle.name] = {
                "first_count": len(first),
                "first_id": first[0].proposal_id if first else None,
                "second_count": len(second),
                "second_id": second[0].proposal_id if second else None,
                "drafted": _count_events(
                    handle, EventType.PROPOSAL_DRAFTED
                ),
                "updated": _count_events(
                    handle, EventType.PROPOSAL_UPDATED
                ),
            }

        baseline = next(iter(outcomes.values()))
        for name, result in outcomes.items():
            assert result == baseline, (
                f"backend {name} diverged from baseline: "
                f"result={result} baseline={baseline}"
            )
        assert baseline["first_id"] == expected_id
        assert baseline["second_id"] == expected_id
        assert baseline["drafted"] == 1
        assert baseline["updated"] == 1


# ---------------------------------------------------------------------------
# Ad-hoc ``python -m`` style invocation
# ---------------------------------------------------------------------------


def run(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """Lightweight invocation for operator dry-runs.

    Mirrors :mod:`eval.scenarios.observation_retrieval.run` — returns a
    minimal dict that records the proposal_id + drafted/updated counts
    on every backend the local env can reach.
    """
    with tempfile.TemporaryDirectory() as tmp_dir, ExitStack() as stack:
        handles = _build_backends(stack, Path(tmp_dir))
        out: dict[str, Any] = {"backends": {}}
        for handle in handles:
            _seed_failure_cluster(handle)
            proposals = _run_generator(handle)
            _run_generator(handle)  # idempotency check
            out["backends"][handle.name] = {
                "proposal_ids": [p.proposal_id for p in proposals],
                "drafted": _count_events(handle, EventType.PROPOSAL_DRAFTED),
                "updated": _count_events(handle, EventType.PROPOSAL_UPDATED),
            }
        return out


if __name__ == "__main__":  # pragma: no cover — operator convenience
    os.environ.setdefault("STRUCTLOG_DISABLE_CONFIG", "1")
    print(json.dumps(run(), indent=2))
