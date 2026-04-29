"""Multi-backend feedback-loop equivalence scenario.

Runs the same convergence loop scenario 5.4 measures against multiple
backend combinations and diffs the loop outputs — same EventLog-driven
effectiveness + advisory fitness loop, on SQLite vs Postgres vs
Neo4j-knowledge + Postgres-operational.

What "equivalence" means here:

* The scenario corpus, seed, and ``run()`` kwargs are identical across
  backends. ``document_store`` is held at SQLite for every handle so
  ``KeywordSearch`` results don't drift on FTS implementation
  differences — this scenario is about the feedback path, not the
  retrieval path (5.1's job).
* Loop counter outputs (``loops.noise_items_tagged_total``,
  ``loops.advisories_generated_total``,
  ``loops.advisories_suppressed_total``,
  ``loops.advisories_boosted_total``) MUST match exactly across
  backends — they're functions of the EventLog content, which the
  scenario seeds identically.
* Convergence deltas (``convergence.useful_delta``,
  ``convergence.weighted_delta``) must match within a small float
  tolerance — same packs, same scoring formula, only floating-point
  rounding can differ.
* Per-backend ingest wall-time is captured for context but does not
  gate the scenario's pass/fail status.

Surfaced findings:

* ``fail`` — counter mismatch between any backend and the SQLite
  reference. Real equivalence violation; investigate the offending
  backend's ``EventLog.get_events`` ordering / limit semantics.
* ``warn`` — convergence delta drift larger than
  :data:`CONVERGENCE_DELTA_TOLERANCE`. Usually means pack contents
  diverged, which usually means graph_store.upsert_node ordering
  shifted (legitimate cross-backend variance worth flagging).
* ``info`` — per-backend run summary, skipped backends.
"""

from __future__ import annotations

import math
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import structlog

from eval._backends import (
    BackendHandle,
    get_neo4j_config,
    get_postgres_dsn,
    register_handle,
)
from eval._live_wipe import wipe_live_state
from eval.runner import Finding, ScenarioReport, ScenarioStatus
from eval.scenarios.agent_loop_convergence.scenario import (
    run as run_convergence,
)
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)


# Defaults intentionally smaller than 5.4's so a multi-backend pass
# completes in reasonable wall time on free-tier Postgres + AuraDB.
# Scheduled runs dial these up via kwargs.
DEFAULT_ROUNDS = 15
DEFAULT_FEEDBACK_BATCH_SIZE = 5
DEFAULT_TRACES_PER_DOMAIN = 4
DEFAULT_ENTITIES_PER_TRACE = 3

# Convergence deltas are floating-point — tiny rounding differences
# between backends are not equivalence violations. Larger than this
# threshold = warn; smaller = info-level only.
CONVERGENCE_DELTA_TOLERANCE = 0.01

# Loop-counter keys that MUST match exactly across backends. These are
# integer counts derived deterministically from EventLog content.
_EXACT_MATCH_LOOP_KEYS: tuple[str, ...] = (
    "loops.effectiveness_runs",
    "loops.advisory_runs",
    "loops.noise_items_tagged_total",
    "loops.advisories_generated_total",
    "loops.advisories_suppressed_total",
    "loops.advisories_restored_total",
    "loops.advisories_boosted_total",
    "round_success_rate",
    "round_total_items_served",
    "round_total_items_referenced",
)

# Float keys diffed within tolerance.
_TOLERANCE_MATCH_KEYS: tuple[str, ...] = (
    "convergence.useful_delta",
    "convergence.weighted_delta",
    "round_useful_fraction_overall",
)

MIN_BACKENDS_FOR_DIFF = 2


# vector + document pinned to SQLite on every Postgres / Neo4j handle:
# the feedback loop reads neither, so pinning them keeps any
# cross-backend diff attributable to the path under test (event_log +
# trace + graph). Also dodges the pgvector fixed-dimension constraint
# when the test DB has a vectors table from a prior run at a
# different dim.
_NON_FEEDBACK_KNOWLEDGE = {
    "vector": {"backend": "sqlite"},
    "document": {"backend": "sqlite"},
    "blob": {"backend": "local"},
}


def _build_backends(stack: ExitStack, tmp_dir: Path) -> list[BackendHandle]:
    """Construct every backend handle the env can reach.

    SQLite is always available. Postgres requires
    ``TRELLIS_KNOWLEDGE_PG_DSN`` (or legacy ``TRELLIS_PG_DSN``). Neo4j
    requires ``TRELLIS_NEO4J_URI`` + ``TRELLIS_NEO4J_USER`` +
    ``TRELLIS_NEO4J_PASSWORD``; Neo4j has no EventLog backend so the
    Neo4j handle pairs Neo4j knowledge stores with Postgres
    operational stores — Postgres creds are required for the Neo4j
    handle to be built.

    Each handle gets its own ``stores_dir`` subdirectory so the
    ``AdvisoryStore`` JSON file the scenario creates doesn't collide.
    """
    handles: list[BackendHandle] = []

    register_handle(
        stack,
        handles,
        name="sqlite",
        config={
            "knowledge": {
                "graph": {"backend": "sqlite"},
                **_NON_FEEDBACK_KNOWLEDGE,
            },
            "operational": {
                "trace": {"backend": "sqlite"},
                "event_log": {"backend": "sqlite"},
            },
        },
        stores_dir=tmp_dir / "sqlite",
    )

    pg_dsn = get_postgres_dsn()
    pg_operational = {
        "trace": {"backend": "postgres", "dsn": pg_dsn},
        "event_log": {"backend": "postgres", "dsn": pg_dsn},
    }
    if pg_dsn:
        register_handle(
            stack,
            handles,
            name="postgres",
            config={
                "knowledge": {
                    "graph": {"backend": "postgres", "dsn": pg_dsn},
                    **_NON_FEEDBACK_KNOWLEDGE,
                },
                "operational": pg_operational,
            },
            stores_dir=tmp_dir / "postgres",
        )

    neo4j_graph = get_neo4j_config()
    if neo4j_graph and pg_dsn:
        register_handle(
            stack,
            handles,
            name="neo4j_op_postgres",
            config={
                "knowledge": {
                    "graph": neo4j_graph,
                    **_NON_FEEDBACK_KNOWLEDGE,
                },
                "operational": pg_operational,
            },
            stores_dir=tmp_dir / "neo4j",
        )

    return handles


@dataclass
class _BackendRun:
    name: str
    metrics: dict[str, float]
    duration_seconds: float


def _run_against_backend(
    handle: BackendHandle,
    *,
    seed: int,
    rounds: int,
    feedback_batch_size: int,
    traces_per_domain: int,
    entities_per_trace: int,
) -> _BackendRun:
    """Run scenario 5.4 against ``handle.registry`` and return its metrics.

    Wipes any pre-existing state in the live backends so successive
    runs don't leak across each other. Convergence deltas are
    explicitly NOT gated here — we want the run to produce the same
    metrics regardless of whether the local ``useful_delta`` looks
    healthy in isolation, so set the regress threshold to a very
    permissive value.
    """
    wipe_live_state(handle.registry)

    # ``run_convergence.duration_seconds`` is populated by the runner
    # wrapper at the outermost call site, not inside the function — so
    # when we call it directly here it stays 0.0 unless we time it
    # ourselves. Wall-time per backend matters for context here (PG +
    # Neo4j round-trip cost) so do the timing.
    start = time.perf_counter()
    report = run_convergence(
        handle.registry,
        seed=seed,
        rounds=rounds,
        feedback_batch_size=feedback_batch_size,
        traces_per_domain=traces_per_domain,
        entities_per_trace=entities_per_trace,
        # The 5.4 regress gate compares against a SQLite-only
        # baseline; on this scenario the per-backend status doesn't
        # gate the multi-backend pass, the cross-backend diff does.
        convergence_delta_regress_threshold=-1.0,
    )
    elapsed = time.perf_counter() - start
    return _BackendRun(
        name=handle.name,
        metrics=dict(report.metrics),
        duration_seconds=elapsed,
    )


def _diff_runs(
    reference: _BackendRun,
    other: _BackendRun,
    *,
    tolerance: float,
) -> tuple[list[Finding], dict[str, float]]:
    findings: list[Finding] = []
    metrics: dict[str, float] = {}

    for key in _EXACT_MATCH_LOOP_KEYS:
        ref_val = reference.metrics.get(key)
        oth_val = other.metrics.get(key)
        if ref_val is None or oth_val is None:
            findings.append(
                Finding(
                    severity="fail",
                    message=(
                        f"loop metric {key!r} missing on "
                        f"{reference.name if ref_val is None else other.name}"
                    ),
                )
            )
            continue
        if ref_val != oth_val:
            findings.append(
                Finding(
                    severity="fail",
                    message=(
                        f"loop counter {key} differs: {reference.name}="
                        f"{ref_val} vs {other.name}={oth_val}"
                    ),
                    detail={reference.name: ref_val, other.name: oth_val},
                )
            )

    for key in _TOLERANCE_MATCH_KEYS:
        ref_val = reference.metrics.get(key)
        oth_val = other.metrics.get(key)
        if ref_val is None or oth_val is None:
            continue
        delta = abs(ref_val - oth_val)
        metrics[f"diff.{key}.{reference.name}_vs_{other.name}"] = round(delta, 6)
        if math.isnan(delta) or delta > tolerance:
            findings.append(
                Finding(
                    severity="warn",
                    message=(
                        f"convergence drift on {key}: {reference.name}="
                        f"{ref_val:.4f} vs {other.name}={oth_val:.4f} "
                        f"(|Δ|={delta:.4f} > {tolerance})"
                    ),
                    detail={reference.name: ref_val, other.name: oth_val},
                )
            )

    return findings, metrics


def run(
    registry: StoreRegistry,  # noqa: ARG001 — scenario builds its own registries
    *,
    seed: int = 0,
    rounds: int = DEFAULT_ROUNDS,
    feedback_batch_size: int = DEFAULT_FEEDBACK_BATCH_SIZE,
    traces_per_domain: int = DEFAULT_TRACES_PER_DOMAIN,
    entities_per_trace: int = DEFAULT_ENTITIES_PER_TRACE,
    tolerance: float = CONVERGENCE_DELTA_TOLERANCE,
) -> ScenarioReport:
    """Execute the multi-backend feedback-loop scenario.

    The runner-supplied ``registry`` is intentionally ignored: this
    scenario builds its own registries because comparing backends is
    the entire point. See the README in this directory.
    """
    findings: list[Finding] = []
    metrics: dict[str, float] = {
        "rounds": float(rounds),
        "feedback_batch_size": float(feedback_batch_size),
    }
    configured: list[str] = []

    with ExitStack() as stack:
        tmp_dir = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        handles = _build_backends(stack, tmp_dir)

        configured = sorted(h.name for h in handles)
        metrics["backends_compared"] = float(len(handles))
        findings.append(
            Finding(
                severity="info",
                message=f"compared backends: {', '.join(configured)}",
            )
        )
        findings.extend(
            Finding(
                severity="info",
                message=f"{missing} backend skipped — credentials not in env",
            )
            for missing in sorted({"postgres", "neo4j_op_postgres"} - set(configured))
        )

        runs: list[_BackendRun] = []
        for handle in handles:
            backend_run = _run_against_backend(
                handle,
                seed=seed,
                rounds=rounds,
                feedback_batch_size=feedback_batch_size,
                traces_per_domain=traces_per_domain,
                entities_per_trace=entities_per_trace,
            )
            runs.append(backend_run)
            metrics[f"duration_seconds.{backend_run.name}"] = round(
                backend_run.duration_seconds, 4
            )
            for key in _EXACT_MATCH_LOOP_KEYS + _TOLERANCE_MATCH_KEYS:
                value = backend_run.metrics.get(key)
                if value is not None:
                    metrics[f"per_backend.{backend_run.name}.{key}"] = value

        if len(runs) >= MIN_BACKENDS_FOR_DIFF:
            reference = runs[0]
            for other in runs[1:]:
                pair_findings, pair_metrics = _diff_runs(
                    reference, other, tolerance=tolerance
                )
                findings.extend(pair_findings)
                metrics.update(pair_metrics)

    failed = any(f.severity == "fail" for f in findings)
    regressed = any(f.severity == "warn" for f in findings)
    status: ScenarioStatus
    if failed:
        status = "fail"
    elif regressed:
        status = "regress"
    else:
        status = "pass"

    if len(configured) >= MIN_BACKENDS_FOR_DIFF:
        decision = (
            "``loops.*`` counters matching across SQLite / Postgres / "
            "Neo4j-op-Postgres confirms the EventLog query layer "
            "(``get_events`` ordering + limit) is equivalent across "
            "backends. Drift here = a real bug in the offending "
            "backend's EventLog implementation."
        )
    else:
        decision = (
            "Single-backend run only — re-run with TRELLIS_KNOWLEDGE_PG_DSN "
            "set (and optionally TRELLIS_NEO4J_*) to get a real "
            "equivalence signal. Without ≥2 backends this is just a 5.4 "
            "smoke."
        )

    return ScenarioReport(
        name="multi_backend_feedback",
        status=status,
        metrics=metrics,
        findings=findings,
        decision=decision,
    )
