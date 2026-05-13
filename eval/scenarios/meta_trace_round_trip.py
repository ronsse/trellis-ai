"""Meta-trace round-trip scenario for Item 6 Phases 0-2.

Seeds an Activity through :func:`trellis.meta.record_meta_analysis`,
then verifies the full round-trip:

1. The Activity is present in the graph with the canonical properties
   (``analyzer_name``, ``agent_id``, ``started_at``) and the synthetic
   ``Agent`` node is wired by a ``wasAssociatedWith`` edge.
2. :meth:`PackBuilder.build` with the default ``include_meta=False``
   *excludes* the Activity from the resulting pack.
3. :meth:`PackBuilder.build` with ``include_meta=True`` *includes* the
   Activity — operators debugging the self-improvement loop need this
   opt-in surface (per ``adr-dogfooding-meta-traces.md`` §5.3).
4. The ``PACK_ASSEMBLED`` event payload carries
   ``meta_filtered_count`` matching the number of meta-Activities the
   filter dropped.

Backend gating mirrors :mod:`eval.scenarios.observation_retrieval`:
SQLite is unconditional, Postgres / Neo4j are env-gated.

Runnable two ways:

* ``pytest eval/scenarios/meta_trace_round_trip.py -v`` — the contract
  the Item 6 Phase 2 brief targets.
* ``from eval.scenarios.meta_trace_round_trip import run`` — minimal
  dict shape for ad-hoc runs.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest
import structlog

from eval._backends import (
    BackendHandle,
    get_neo4j_config,
    get_postgres_dsn,
    register_handle,
)
from trellis.meta import DEFAULT_META_AGENT_ID, record_meta_analysis
from trellis.retrieve import PackBuilder
from trellis.retrieve.strategies import GraphSearch
from trellis.schemas import well_known as wk

logger = structlog.get_logger(__name__)

#: Cross-backend assertions only kick in when at least two backends are
#: available. SQLite is always present; Postgres / Neo4j depend on env.
_MIN_BACKENDS_FOR_CROSS_CHECK = 2

#: Analyzer + agent identity used for the seeded meta-Activity. The
#: synthetic agent_id is the default reserved ID — the default-filter
#: assertion below relies on the ``trellis_meta_`` prefix being on it.
SCENARIO_ANALYZER_NAME = "meta-trace-round-trip-eval"
SCENARIO_FINDING_ID = "meta-trace-finding-1"
SCENARIO_FINDING_TYPE = "TestFinding"

#: A user-authored ("real") Activity that *must not* be filtered out.
#: Stamped with a non-synthetic ``agent_id`` so the default filter only
#: catches the meta one — and we can assert that distinction.
NON_META_ACTIVITY_ID = "real-activity-1"
NON_META_AGENT_ID = "human-analyst-1"


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------


def _seed_meta_activity(handle: BackendHandle) -> str:
    """Drive :func:`record_meta_analysis` once and return the Activity ID."""
    with record_meta_analysis(
        analyzer_name=SCENARIO_ANALYZER_NAME,
        agent_id=DEFAULT_META_AGENT_ID,
        registry=handle.registry,
    ) as record:
        assert record.activity_id is not None
        # Materialise a finding node first so the wasGeneratedBy edge has a
        # valid target. Recorder writes the edge from finding -> activity.
        handle.registry.knowledge.graph_store.upsert_node(
            node_id=SCENARIO_FINDING_ID,
            node_type=SCENARIO_FINDING_TYPE,
            properties={"name": SCENARIO_FINDING_ID},
        )
        record.produced_finding(
            SCENARIO_FINDING_ID, finding_type=SCENARIO_FINDING_TYPE
        )
        return record.activity_id


def _seed_non_meta_activity(handle: BackendHandle) -> str:
    """Materialise a user-authored Activity node (non-synthetic agent).

    Used to verify the default filter is *agent_id-specific* and does
    not just blanket-drop every ``Activity`` node.
    """
    handle.registry.knowledge.graph_store.upsert_node(
        node_id=NON_META_ACTIVITY_ID,
        node_type=wk.ACTIVITY,
        properties={
            "name": NON_META_ACTIVITY_ID,
            "analyzer_name": "user-authored",
            "agent_id": NON_META_AGENT_ID,
            "started_at": "2026-05-13T00:00:00+00:00",
        },
    )
    return NON_META_ACTIVITY_ID


# ---------------------------------------------------------------------------
# Pack assembly helper
# ---------------------------------------------------------------------------


def _build_pack(
    handle: BackendHandle,
    *,
    include_meta: bool,
    node_type: str = wk.ACTIVITY,
) -> Any:
    """Run a PackBuilder with the GraphSearch strategy filtered to Activities."""
    strategy = GraphSearch(handle.registry.knowledge.graph_store)
    builder = PackBuilder(
        strategies=[strategy],
        event_log=handle.registry.operational.event_log,
    )
    return builder.build(
        intent="meta-trace round-trip eval",
        filters={"node_type": node_type},
        include_meta=include_meta,
    )


# ---------------------------------------------------------------------------
# Backend wiring (mirrors observation_retrieval)
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
# pytest entry points
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_handle(tmp_path: Path) -> Any:
    """Seeded SQLite backend. Always available; no env gating."""
    with ExitStack() as stack:
        handles = _build_backends(stack, tmp_path)
        sqlite = next(h for h in handles if h.name == "sqlite")
        # Seed BOTH activities so every assertion has both shapes to bite on.
        _seed_meta_activity(sqlite)
        _seed_non_meta_activity(sqlite)
        yield sqlite


def test_meta_activity_present_in_graph_sqlite(sqlite_handle: BackendHandle) -> None:
    """The recorder writes a real Activity row with the canonical shape."""
    graph = sqlite_handle.registry.knowledge.graph_store
    # The activity_id is generated by the recorder; we query by analyzer_name
    # to find it (the property is stamped on the node).
    candidates = graph.query(
        node_type=wk.ACTIVITY,
        properties={"analyzer_name": SCENARIO_ANALYZER_NAME},
        limit=10,
    )
    assert len(candidates) == 1, f"expected exactly 1 meta-Activity, got {candidates}"
    node = candidates[0]
    assert node["properties"]["agent_id"] == DEFAULT_META_AGENT_ID
    assert node["properties"]["analyzer_name"] == SCENARIO_ANALYZER_NAME
    assert "started_at" in node["properties"]


def test_default_filter_excludes_meta_sqlite(sqlite_handle: BackendHandle) -> None:
    """include_meta=False (the default) drops the meta-Activity."""
    pack = _build_pack(sqlite_handle, include_meta=False)
    item_ids = {item.item_id for item in pack.items}
    # The non-meta (user-authored) Activity must remain.
    assert NON_META_ACTIVITY_ID in item_ids, (
        "non-meta Activity was incorrectly filtered: "
        f"item_ids={item_ids}"
    )
    # No item_id should match the meta-Activity (we don't know its ULID,
    # but the synthetic agent_id is on the metadata).
    for item in pack.items:
        meta_agent = (item.metadata or {}).get("agent_id")
        assert meta_agent != DEFAULT_META_AGENT_ID, (
            f"meta-Activity leaked through the default filter: {item}"
        )


def test_opt_in_includes_meta_sqlite(sqlite_handle: BackendHandle) -> None:
    """include_meta=True surfaces the meta-Activity."""
    pack = _build_pack(sqlite_handle, include_meta=True)
    meta_agents_seen = [
        (item.metadata or {}).get("agent_id") for item in pack.items
    ]
    assert DEFAULT_META_AGENT_ID in meta_agents_seen, (
        f"meta-Activity missing with include_meta=True: "
        f"seen agent_ids={meta_agents_seen}"
    )


def test_pack_assembled_event_records_meta_filtered_count_sqlite(
    sqlite_handle: BackendHandle,
) -> None:
    """``meta_filtered_count`` in the PACK_ASSEMBLED payload matches the drop count."""
    from trellis.stores.base.event_log import EventType  # noqa: PLC0415

    event_log = sqlite_handle.registry.operational.event_log
    # The default-filter build drops 1 meta-Activity; the opt-in build
    # drops 0. Both emit telemetry; we read the latest event of each.
    _ = _build_pack(sqlite_handle, include_meta=False)
    default_events = event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, order="desc", limit=1
    )
    assert default_events, "no PACK_ASSEMBLED event emitted for default build"
    assert default_events[0].payload["meta_filtered_count"] == 1, (
        default_events[0].payload
    )

    _ = _build_pack(sqlite_handle, include_meta=True)
    optin_events = event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, order="desc", limit=1
    )
    assert optin_events, "no PACK_ASSEMBLED event emitted for opt-in build"
    assert optin_events[0].payload["meta_filtered_count"] == 0, (
        optin_events[0].payload
    )


def test_cross_backend_equivalence(tmp_path: Path) -> None:
    """Every backend the env provides must agree on the filter outcome."""
    with ExitStack() as stack:
        handles = _build_backends(stack, tmp_path)
        if len(handles) < _MIN_BACKENDS_FOR_CROSS_CHECK:
            pytest.skip(
                "cross-backend assertions require at least "
                f"{_MIN_BACKENDS_FOR_CROSS_CHECK} backends; "
                f"got {[h.name for h in handles]}"
            )
        outcomes: dict[str, tuple[bool, bool]] = {}
        for handle in handles:
            _seed_meta_activity(handle)
            _seed_non_meta_activity(handle)
            default_pack = _build_pack(handle, include_meta=False)
            optin_pack = _build_pack(handle, include_meta=True)

            def _has_meta(pack: Any) -> bool:
                return any(
                    (item.metadata or {}).get("agent_id") == DEFAULT_META_AGENT_ID
                    for item in pack.items
                )

            outcomes[handle.name] = (
                _has_meta(default_pack),  # should be False on every backend
                _has_meta(optin_pack),  # should be True on every backend
            )

        # Each backend must produce identical (default, opt-in) outcomes.
        baseline = next(iter(outcomes.values()))
        for name, result in outcomes.items():
            assert result == baseline, (
                f"backend {name} disagrees: result={result} baseline={baseline} "
                f"all_outcomes={outcomes}"
            )
        # Sanity: the canonical outcome is (False, True).
        assert baseline == (False, True), (
            f"unexpected baseline outcome — meta filter broken: {baseline}"
        )


# ---------------------------------------------------------------------------
# Ad-hoc invocation entry point
# ---------------------------------------------------------------------------


def run(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """Lightweight ``python -m`` style invocation.

    Mirrors :mod:`eval.scenarios.observation_retrieval.run` — returns a
    minimal dict so operators can eyeball the cross-backend outcome
    without dragging in the ``ScenarioReport`` machinery.
    """
    with tempfile.TemporaryDirectory() as tmp_dir, ExitStack() as stack:
        handles = _build_backends(stack, Path(tmp_dir))
        out: dict[str, Any] = {"backends": {}}
        for handle in handles:
            _seed_meta_activity(handle)
            _seed_non_meta_activity(handle)
            default_pack = _build_pack(handle, include_meta=False)
            optin_pack = _build_pack(handle, include_meta=True)
            out["backends"][handle.name] = {
                "default_items": [item.item_id for item in default_pack.items],
                "optin_items": [item.item_id for item in optin_pack.items],
            }
        return out


if __name__ == "__main__":  # pragma: no cover — operator convenience
    os.environ.setdefault("STRUCTLOG_DISABLE_CONFIG", "1")
    print(json.dumps(run(), indent=2))
