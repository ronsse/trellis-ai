"""The graph axis's unseeded recency window is off by default (#961).

With no seed, neither one the intent produced nor one the caller named, the
graph axis used to serve the newest ``limit * overfetch`` nodes whatever was
asked. Measured on the reference deployment over 30 days, that path was 846
of 890 graph-axis servings and drew 0.025 helpful against 0.264 unhelpful
citations per serving, where the other servings drew 0.083 against 0.160.

The knob is ``TRELLIS_GRAPH_RECENCY_FALLBACK`` (settings key
``graph_recency_fallback``), resolved by the factory, env > settings >
default off. The lower layers (``GraphSearch``, ``build_strategies``) keep
``recency_fallback=True`` so a direct construction is unchanged, the same
split as ``graph_seed_extractor`` (see ``TestGraphSeedingWiring``).

What each class pins:

* ``TestGraphSearchDirect``: the strategy, with the flag handed in.
* ``TestFactoryDefaultOff``: a pack built the way every surface builds it.
* ``TestOptIn``: env and settings restore the old behaviour, and the
  precedence between them.
* ``TestSeededPathsUnchanged``: seeding is not touched by the knob.
* ``TestAxesBlock``: the reason is reported, not absorbed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from structlog.testing import capture_logs

from trellis.retrieve.builder_factory import (
    GRAPH_RECENCY_FALLBACK_ENV,
    GRAPH_SEEDING_ENV,
    _graph_recency_fallback_enabled,
    build_pack_builder,
    describe_axes,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import (
    GRAPH_AXIS_UNSEEDED_FALLBACK_OFF,
    GRAPH_CALLER_SEED_IDS_KEY,
    GRAPH_SELECTION_CALLER_SEEDED,
    GRAPH_SELECTION_RECENCY_WINDOW,
    GRAPH_SELECTION_SEEDED,
    GraphSearch,
)
from trellis.schemas.pack import SectionRequest
from trellis.stores.registry import StoreRegistry
from trellis.stores.settings_store import SettingsStore

if TYPE_CHECKING:
    from pathlib import Path

#: Shares no word with any node below, so no seed can come from the intent.
BLIND_INTENT = "zzzz qqqq"
NAMED_INTENT = "what happened in payroll"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GRAPH_RECENCY_FALLBACK_ENV, raising=False)
    monkeypatch.delenv(GRAPH_SEEDING_ENV, raising=False)


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _node(registry: StoreRegistry, node_id: str, node_type: str, name: str) -> None:
    registry.knowledge.graph_store.upsert_node(
        node_id, node_type, {"name": name, "description": f"{name} " * 8}
    )


def _seed_payroll(registry: StoreRegistry) -> None:
    """A node an intent can name, with one neighbour, plus unrelated nodes."""
    _node(registry, "domain:payroll", "domain", "payroll")
    _node(registry, "act:payroll-1", "activity", "reconcile payroll books")
    registry.knowledge.graph_store.upsert_edge(
        "domain:payroll", "act:payroll-1", "relates_to"
    )
    for i in range(3):
        _node(registry, f"concept:other-{i}", "concept", f"unrelated thing {i}")


def _graph_items(items: list[Any]) -> list[Any]:
    return [i for i in items if i.metadata.get("source_strategy") == "graph"]


class TestGraphSearchDirect:
    def test_off_and_unseeded_serves_nothing_and_says_why(
        self, registry: StoreRegistry
    ) -> None:
        _seed_payroll(registry)
        axis = GraphSearch(registry.knowledge.graph_store, recency_fallback=False)

        answer = axis.search_answer(BLIND_INTENT)

        assert answer.items == []
        assert answer.axis_state == GRAPH_AXIS_UNSEEDED_FALLBACK_OFF
        assert axis.search(BLIND_INTENT) == []

    def test_on_serves_the_window_and_reports_no_withholding(
        self, registry: StoreRegistry
    ) -> None:
        _seed_payroll(registry)
        axis = GraphSearch(registry.knowledge.graph_store, recency_fallback=True)

        answer = axis.search_answer(BLIND_INTENT)

        assert answer.items
        assert {i.metadata["graph_selection"] for i in answer.items} == {
            GRAPH_SELECTION_RECENCY_WINDOW
        }
        assert answer.axis_state is None

    def test_a_direct_construction_keeps_the_window(
        self, registry: StoreRegistry
    ) -> None:
        """The lower layer's default is unchanged; the factory decides."""
        _seed_payroll(registry)
        assert GraphSearch(registry.knowledge.graph_store).search(BLIND_INTENT)

    def test_an_unresolvable_caller_id_is_unseeded_too(
        self, registry: StoreRegistry
    ) -> None:
        _seed_payroll(registry)
        axis = GraphSearch(registry.knowledge.graph_store, recency_fallback=False)

        answer = axis.search_answer(
            BLIND_INTENT, filters={GRAPH_CALLER_SEED_IDS_KEY: ["tbl:does-not-exist"]}
        )

        assert answer.items == []
        assert answer.axis_state == GRAPH_AXIS_UNSEEDED_FALLBACK_OFF

    def test_an_extractor_that_raises_is_unseeded_too(
        self, registry: StoreRegistry
    ) -> None:
        class _Boom:
            def extract(self, *_a: object, **_k: object) -> list[str]:
                msg = "extractor down"
                raise RuntimeError(msg)

        _seed_payroll(registry)
        axis = GraphSearch(
            registry.knowledge.graph_store,
            seed_extractor=_Boom(),  # type: ignore[arg-type]
            recency_fallback=False,
        )

        answer = axis.search_answer(NAMED_INTENT)

        assert answer.items == []
        assert answer.axis_state == GRAPH_AXIS_UNSEEDED_FALLBACK_OFF


class TestFactoryDefaultOff:
    """No env var, no settings file: the product default."""

    def test_flat_pack_has_no_graph_items_and_names_the_reason(
        self, registry: StoreRegistry
    ) -> None:
        _seed_payroll(registry)

        pack = build_pack_builder(registry, surface="test").build(BLIND_INTENT)

        assert _graph_items(pack.items) == []
        assert pack.retrieval_report.graph_axis == GRAPH_AXIS_UNSEEDED_FALLBACK_OFF
        # The axis ran: it is withheld, not failed.
        assert "graph" in pack.retrieval_report.strategies_used

    def test_sectioned_pack_has_no_graph_items_and_names_the_reason(
        self, registry: StoreRegistry
    ) -> None:
        _seed_payroll(registry)

        pack = build_pack_builder(registry, surface="test").build_sectioned(
            BLIND_INTENT, sections=[SectionRequest(name="a"), SectionRequest(name="b")]
        )

        assert _graph_items([i for s in pack.sections for i in s.items]) == []
        # Every section carries the report, as ``strategies_used`` does.
        assert {s.retrieval_report.graph_axis for s in pack.sections} == {
            GRAPH_AXIS_UNSEEDED_FALLBACK_OFF
        }

    def test_a_held_out_pack_carries_the_state_too(
        self, registry: StoreRegistry
    ) -> None:
        """A withheld pack must be indistinguishable from a served one."""
        axis = GraphSearch(registry.knowledge.graph_store, recency_fallback=False)
        builder = PackBuilder(strategies=[axis], holdout_rate=1.0)

        flat = builder.build(BLIND_INTENT)
        sectioned = builder.build_sectioned(
            BLIND_INTENT, sections=[SectionRequest(name="a")]
        )

        assert flat.retrieval_report.graph_axis == GRAPH_AXIS_UNSEEDED_FALLBACK_OFF
        assert {s.retrieval_report.graph_axis for s in sectioned.sections} == {
            GRAPH_AXIS_UNSEEDED_FALLBACK_OFF
        }


class TestOptIn:
    def test_env_restores_the_window(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_payroll(registry)
        monkeypatch.setenv(GRAPH_RECENCY_FALLBACK_ENV, "1")

        pack = build_pack_builder(registry, surface="test").build(BLIND_INTENT)

        graph_items = _graph_items(pack.items)
        assert graph_items
        assert {i.metadata["graph_selection"] for i in graph_items} == {
            GRAPH_SELECTION_RECENCY_WINDOW
        }
        assert pack.retrieval_report.graph_axis is None

    def test_settings_restore_the_window_with_no_env(
        self, registry: StoreRegistry
    ) -> None:
        _seed_payroll(registry)
        assert registry.stores_dir is not None
        SettingsStore(registry.stores_dir / "settings.json").set(
            "graph_recency_fallback", True
        )

        pack = build_pack_builder(registry, surface="test").build(BLIND_INTENT)

        assert _graph_items(pack.items)
        assert pack.retrieval_report.graph_axis is None

    def test_env_off_beats_a_settings_opt_in(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_payroll(registry)
        assert registry.stores_dir is not None
        SettingsStore(registry.stores_dir / "settings.json").set(
            "graph_recency_fallback", True
        )
        monkeypatch.setenv(GRAPH_RECENCY_FALLBACK_ENV, "0")

        pack = build_pack_builder(registry, surface="test").build(BLIND_INTENT)

        assert _graph_items(pack.items) == []
        assert pack.retrieval_report.graph_axis == GRAPH_AXIS_UNSEEDED_FALLBACK_OFF

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "ON", " true "])
    def test_recognised_on_values(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(GRAPH_RECENCY_FALLBACK_ENV, value)
        assert _graph_recency_fallback_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
    def test_off_and_empty_values_stay_off(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(GRAPH_RECENCY_FALLBACK_ENV, value)
        assert _graph_recency_fallback_enabled() is False

    def test_a_typo_warns_and_stays_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An opt-in that a typo enables would defeat the default."""
        monkeypatch.setenv(GRAPH_RECENCY_FALLBACK_ENV, "enabled")
        with capture_logs() as logs:
            assert _graph_recency_fallback_enabled() is False
        assert [e["event"] for e in logs] == ["graph_recency_fallback_env_unrecognised"]

    def test_a_non_bool_setting_is_ignored(self) -> None:
        assert _graph_recency_fallback_enabled({"graph_recency_fallback": "yes"}) is (
            False
        )
        assert _graph_recency_fallback_enabled({"graph_recency_fallback": True}) is True


class TestSeededPathsUnchanged:
    """Off, the knob removes only the fallback. Seeded axes serve as before."""

    def test_an_intent_naming_an_entity_is_seeded_and_not_withheld(
        self, registry: StoreRegistry
    ) -> None:
        _seed_payroll(registry)

        pack = build_pack_builder(registry, surface="test").build(NAMED_INTENT)

        graph_items = _graph_items(pack.items)
        assert graph_items
        assert {i.metadata["graph_selection"] for i in graph_items} == {
            GRAPH_SELECTION_SEEDED
        }
        assert pack.retrieval_report.graph_axis is None

    def test_caller_named_ids_are_caller_seeded_and_not_withheld(
        self, registry: StoreRegistry
    ) -> None:
        _seed_payroll(registry)

        pack = build_pack_builder(registry, surface="test").build_sectioned(
            BLIND_INTENT,
            sections=[SectionRequest(name="a", entity_ids=["domain:payroll"])],
        )

        graph_items = _graph_items([i for s in pack.sections for i in s.items])
        assert graph_items
        assert {i.metadata["graph_selection"] for i in graph_items} == {
            GRAPH_SELECTION_CALLER_SEEDED
        }
        assert pack.sections[0].retrieval_report.graph_axis is None


class TestAxesBlock:
    """``describe_axes`` is the one place every surface renders from."""

    @staticmethod
    def _axes(
        registry: StoreRegistry, graph_axis: str | None, strategies_used: list[str]
    ) -> dict[str, Any]:
        builder = build_pack_builder(registry, surface="test")
        return dict(
            describe_axes(
                builder,
                strategies_used,
                embedder_configured=False,
                graph_axis=graph_axis,
            )
        )

    def test_withheld_is_reported_and_the_axis_still_counts_as_ran(
        self, registry: StoreRegistry
    ) -> None:
        pack = build_pack_builder(registry, surface="test").build(BLIND_INTENT)
        builder = build_pack_builder(registry, surface="test")

        axes = describe_axes(
            builder,
            pack.retrieval_report.strategies_used,
            embedder_configured=False,
            graph_axis=pack.retrieval_report.graph_axis,
        )

        assert axes["graph"] == GRAPH_AXIS_UNSEEDED_FALLBACK_OFF
        assert "graph" in axes["ran"]
        assert axes["failed"] == []

    def test_a_served_axis_reads_ran(self, registry: StoreRegistry) -> None:
        assert self._axes(registry, None, ["keyword", "graph"])["graph"] == "ran"

    def test_a_failed_axis_reads_failed_even_if_flagged(
        self, registry: StoreRegistry
    ) -> None:
        axes = self._axes(registry, GRAPH_AXIS_UNSEEDED_FALLBACK_OFF, ["keyword"])
        assert axes["graph"] == "failed"
        assert axes["failed"] == ["graph"]
