"""The substance arms of ``trellis.retrieve.pack_replay`` (``replay_substance``).

Every expected count below is computed by hand from the seeded rows in
``_seed``, not read back from the code under test. The edges that matter:

* an entity with a description, an empty one, a whitespace one and none;
* distinct-word counts at N-1, N and N+1, with case and punctuation;
* a row the stores cannot answer for is ``unresolved`` and is neither removed
  nor kept;
* a serving two rules both remove counts once;
* a held-out pack never reaches a count;
* an entity is judged as the graph held it when the pack was assembled
  (``get_node(..., as_of=...)``), a document as it is now.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from trellis.retrieve.class_prior import ClassPriorConfig
from trellis.retrieve.excerpts import (
    EXCERPT_MAX_CHARS,
    count_substance_words,
    truncate_excerpt,
)
from trellis.retrieve.pack_replay import (
    SUBSTANCE_BASIS_AS_SERVED,
    SUBSTANCE_BASIS_CURRENT_STATE,
    ReplayPolicy,
    _entity_served_text,
    replay_class_prior,
    replay_pack_value,
    replay_substance,
)
from trellis.retrieve.strategies import GRAPH_CALLER_SEED_IDS_KEY, GraphSearch
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.graph import SQLiteGraphStore

SENTENCE = "Restart the api container before running admin init"  # 8 distinct words

# item_id, item_type, tokens, verdict
PACK_1 = [
    ("e_desc", "entity", 100, "helpful"),
    ("e_empty", "entity", 20, "unhelpful"),
    ("e_blank", "entity", 20, "unhelpful"),
    ("e_nodesc", "entity", 20, "unjudged"),
    ("e_gone", "entity", 30, "helpful"),
    ("d_six", "document", 50, "helpful"),
]
PACK_2 = [
    ("d_five", "document", 40, "helpful"),
    ("d_four", "document", 40, "unhelpful"),
    ("d_case", "document", 40, "unjudged"),
    ("d_gone", "document", 10, "unhelpful"),
]


class _Stores:
    def __init__(self, tmp_path: Path) -> None:
        self.log = SQLiteEventLog(tmp_path / "events.db")
        self.graph = SQLiteGraphStore(tmp_path / "graph.db")
        self.docs = SQLiteDocumentStore(tmp_path / "documents.db")

    def emit_pack(
        self,
        pack_id: str,
        rows: list[tuple[str, str, int, str]],
        *,
        extra: dict[str, dict[str, Any]] | None = None,
        holdout: bool = False,
    ) -> None:
        extra = extra or {}
        payload: dict[str, Any] = {
            "pack_id": pack_id,
            "injected_items": [
                {
                    "item_id": item_id,
                    "item_type": item_type,
                    "rank": rank,
                    "estimated_tokens": tokens,
                    "strategy_source": "graph" if item_type == "entity" else "keyword",
                    **extra.get(item_id, {}),
                }
                for rank, (item_id, item_type, tokens, _) in enumerate(rows, 1)
            ],
        }
        if holdout:
            payload["holdout"] = True
        self.log.emit(
            EventType.PACK_ASSEMBLED,
            source="pack_builder",
            entity_id=pack_id,
            entity_type="pack",
            payload=payload,
        )
        self.log.emit(
            EventType.FEEDBACK_RECORDED,
            source="mcp",
            entity_id=pack_id,
            payload={
                "pack_id": pack_id,
                "helpful_item_ids": [r[0] for r in rows if r[3] == "helpful"],
                "unhelpful_item_ids": [r[0] for r in rows if r[3] == "unhelpful"],
                "rating": 0.5,
                "success": True,
            },
        )

    def replay(self, policy: ReplayPolicy, **kwargs: Any):
        return replay_substance(
            self.log,
            policy=policy,
            graph_store=self.graph,
            document_store=self.docs,
            **kwargs,
        )


def _seed(tmp_path: Path) -> _Stores:
    s = _Stores(tmp_path)
    s.graph.upsert_node(
        "e_desc", "concept", {"name": "Runbook", "description": SENTENCE}
    )
    s.graph.upsert_node("e_empty", "concept", {"name": "Empty Stub", "description": ""})
    s.graph.upsert_node(
        "e_blank", "concept", {"name": "Blank Stub", "description": "   "}
    )
    s.graph.upsert_node("e_nodesc", "concept", {"name": "Trellis ingest"})
    s.docs.put("d_six", "alpha beta gamma delta epsilon zeta", {})
    s.docs.put("d_five", "alpha beta gamma delta epsilon", {})
    s.docs.put("d_four", "alpha beta gamma delta", {})
    s.docs.put("d_case", "Alpha alpha ALPHA, beta! beta. gamma", {})
    s.emit_pack("pack-1", PACK_1)
    s.emit_pack("pack-2", PACK_2)
    return s


@pytest.fixture
def stores(tmp_path: Path) -> _Stores:
    return _seed(tmp_path)


def _seed_walk(s: _Stores) -> None:
    """Six packs carrying both halves of the budget walk, for the default pin."""
    for n in range(6):
        admitted = [(f"w{n}_a", 60), (f"w{n}_b", 40), (f"w{n}_c", 30)]
        turned_away = [(f"w{n}_d", 50), (f"w{n}_e", 20)]
        trace = [
            {"item_id": i, "item_tokens": t, "running_total": 0, "included": True}
            for i, t in admitted
        ] + [
            {"item_id": i, "item_tokens": t, "running_total": 0, "included": False}
            for i, t in turned_away
        ]
        pack_id = f"walk-{n}"
        s.log.emit(
            EventType.PACK_ASSEMBLED,
            source="pack_builder",
            entity_id=pack_id,
            entity_type="pack",
            payload={
                "pack_id": pack_id,
                "budget_max_tokens": 140,
                "injected_items": [
                    {
                        "item_id": i,
                        "item_type": "document",
                        "rank": rank,
                        "estimated_tokens": t,
                        "strategy_source": "semantic",
                    }
                    for rank, (i, t) in enumerate(admitted, 1)
                ],
                "budget_trace": trace,
            },
        )
        s.log.emit(
            EventType.FEEDBACK_RECORDED,
            source="mcp",
            entity_id=pack_id,
            payload={
                "pack_id": pack_id,
                "helpful_item_ids": [f"w{n}_a"],
                "unhelpful_item_ids": [f"w{n}_b"] if n % 2 else [],
                "rating": 0.5,
                "success": True,
            },
        )


def test_entity_requires_description_at_each_description_edge(stores: _Stores) -> None:
    report = stores.replay(ReplayPolicy(entity_requires_description=True))

    # Served: 10 servings, 370 tokens = helpful 4/220 + unhelpful 4/90 + unjudged 2/60.
    assert (report.served.items, report.served.tokens) == (10, 370)
    assert (report.served.helpful_items, report.served.helpful_tokens) == (4, 220)
    assert (report.served.unhelpful_items, report.served.unhelpful_tokens) == (4, 90)
    assert (report.served.unjudged_items, report.served.unjudged_tokens) == (2, 60)
    # Removed: the empty, whitespace and absent descriptions. The described
    # entity stays, and documents are not this arm's business.
    r = report.removed
    assert (r.items, r.tokens) == (3, 60)
    assert (r.helpful_items, r.helpful_tokens) == (0, 0)
    assert (r.unhelpful_items, r.unhelpful_tokens) == (2, 40)
    assert (r.unjudged_items, r.unjudged_tokens) == (1, 20)
    # The entity the graph no longer holds is neither removed nor kept.
    u = report.unresolved
    assert (u.items, u.helpful_items, u.helpful_tokens) == (1, 1, 30)
    assert report.unresolved_entity_description == 1
    assert report.removed_by_entity_description == 3
    assert report.removed_by_min_distinct_words == 0
    assert report.judged_on == {
        "entity_requires_description": SUBSTANCE_BASIS_AS_SERVED
    }
    assert any("Graph entities are judged as served" in n for n in report.notes)
    # Only the word arm reads a document, so the entity arm alone says nothing
    # about documents being on current state.
    assert not any("Documents are judged on current state" in n for n in report.notes)


@pytest.mark.parametrize(
    ("n", "removed"),
    # e_empty 0 and e_blank 0 words; e_nodesc 2 ("Trellis ingest"); d_case 3
    # (case and punctuation fold); d_four 4; d_five 5; d_six 6; e_desc 8.
    [(1, 2), (2, 2), (3, 3), (4, 4), (5, 5), (6, 6), (7, 7), (8, 7), (9, 8)],
)
def test_min_distinct_words_at_n_minus_one_n_and_n_plus_one(
    stores: _Stores, n: int, removed: int
) -> None:
    report = stores.replay(ReplayPolicy(min_distinct_words=n))

    assert report.removed.items == removed
    assert report.removed_by_min_distinct_words == removed
    # e_gone and d_gone cannot be read, at every N.
    assert report.unresolved.items == 2
    assert report.unresolved_min_distinct_words == 2


def test_min_distinct_words_hand_computed_verdict_counts(stores: _Stores) -> None:
    report = stores.replay(ReplayPolicy(min_distinct_words=5))

    r = report.removed  # e_empty, e_blank, e_nodesc, d_four, d_case
    assert (r.items, r.tokens) == (5, 140)
    assert (r.helpful_items, r.unhelpful_items, r.unhelpful_tokens) == (0, 3, 80)
    assert (r.unjudged_items, r.unjudged_tokens) == (2, 60)
    u = report.unresolved  # e_gone (helpful 30), d_gone (unhelpful 10)
    assert (u.items, u.helpful_tokens, u.unhelpful_tokens) == (2, 30, 10)
    assert report.judged_on == {
        "min_distinct_words.entity": SUBSTANCE_BASIS_AS_SERVED,
        "min_distinct_words.document": SUBSTANCE_BASIS_CURRENT_STATE,
    }
    assert any("Documents are judged on current state" in n for n in report.notes)


def test_both_arms_count_a_serving_both_remove_once(stores: _Stores) -> None:
    report = stores.replay(
        ReplayPolicy(entity_requires_description=True, min_distinct_words=5)
    )

    assert report.removed.items == 5
    assert report.removed_by_entity_description == 3
    assert report.removed_by_min_distinct_words == 5
    assert report.removed_by_several_rules == 3
    # e_gone is unresolved for both rules and d_gone for one: two servings.
    assert report.unresolved.items == 2
    assert report.unresolved_entity_description == 1
    assert report.unresolved_min_distinct_words == 2


def test_a_missing_store_leaves_every_dependent_serving_unresolved(
    stores: _Stores,
) -> None:
    report = replay_substance(stores.log, policy=ReplayPolicy(min_distinct_words=5))

    assert report.removed.items == 0
    assert report.unresolved.items == report.served.items == 10


def test_a_raising_store_is_unresolved_not_an_abort(stores: _Stores) -> None:
    class _Broken:
        def get_node(self, *_: Any, **__: Any) -> None:
            msg = "boom"
            raise RuntimeError(msg)

    report = replay_substance(
        stores.log,
        policy=ReplayPolicy(entity_requires_description=True),
        graph_store=_Broken(),  # type: ignore[arg-type]
    )

    assert report.removed.items == 0
    assert report.unresolved.items == 5  # the five entity servings


def test_observation_items_are_exempt_from_the_word_arm(tmp_path: Path) -> None:
    s = _Stores(tmp_path)
    s.emit_pack("pack-obs", [("obs-1", "observation", 12, "unhelpful")])

    report = s.replay(ReplayPolicy(min_distinct_words=5))

    assert report.removed.items == 0
    assert report.unresolved.items == 0
    assert report.exempt_from_min_distinct_words == 1


def test_a_held_out_pack_and_its_feedback_reach_no_count(stores: _Stores) -> None:
    stores.emit_pack(
        "pack-held", [("d_four", "document", 999, "unhelpful")], holdout=True
    )

    report = stores.replay(ReplayPolicy(min_distinct_words=5))

    assert report.attributed_packs == 2
    assert report.served.items == 10
    assert report.served.tokens == 370


def test_default_policy_is_not_a_substance_arm_and_the_walk_refuses_one(
    stores: _Stores,
) -> None:
    assert not ReplayPolicy().has_substance_arms
    with pytest.raises(ValueError, match="needs entity_requires_description"):
        stores.replay(ReplayPolicy())
    with pytest.raises(ValueError, match="priced by replay_substance"):
        replay_pack_value(stores.log, policy=ReplayPolicy(min_distinct_words=3))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"excerpt_max_chars": 100},
        {"body_items": 3},
        {"max_items": 3},
        {"refill": False},
    ],
)
def test_substance_arms_refuse_the_walk_levers(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="removal-only"):
        ReplayPolicy(min_distinct_words=3, **kwargs)
    with pytest.raises(ValueError, match="removal-only"):
        ReplayPolicy(entity_requires_description=True, **kwargs)
    with pytest.raises(ValueError, match="min_distinct_words must be >= 1"):
        ReplayPolicy(min_distinct_words=0)


def test_substance_policy_describes_itself_without_the_refill_knob() -> None:
    both = ReplayPolicy(entity_requires_description=True, min_distinct_words=5)
    assert both.describe() == "entity_requires_description, min_distinct_words=5"
    assert ReplayPolicy().describe() == "refill=on"


# -- combined with the class prior -------------------------------------------

_PRIOR = ClassPriorConfig(
    mode="withhold", forms=frozenset({"conversation"}), axes=frozenset({"keyword"})
)


def _seed_conversations(s: _Stores) -> None:
    s.docs.put("conv_1", "hi there", {})  # 2 words
    s.docs.put("conv_2", "one two three four five six seven", {})  # 7 words
    s.docs.put("conv_3", "hi there", {})
    form = {"document_form": "conversation"}
    s.emit_pack(
        "pack-conv",
        [
            ("conv_1", "document", 25, "helpful"),
            ("conv_2", "document", 30, "unhelpful"),
            ("conv_3", "document", 10, "unjudged"),
        ],
        extra={
            "conv_1": form,
            "conv_2": form,
            # Same form, but on an axis outside the configured one.
            "conv_3": {**form, "strategy_source": "semantic"},
        },
    )


def test_combined_with_the_class_prior_counts_each_serving_once(
    tmp_path: Path,
) -> None:
    s = _Stores(tmp_path)
    _seed_conversations(s)

    report = s.replay(ReplayPolicy(min_distinct_words=5), class_prior_config=_PRIOR)

    # conv_1: both rules. conv_2: class prior only. conv_3: words only.
    assert report.removed.items == 3
    assert (report.removed.helpful_items, report.removed.helpful_tokens) == (1, 25)
    assert (report.removed.unhelpful_items, report.removed.unhelpful_tokens) == (1, 30)
    assert (report.removed.unjudged_items, report.removed.unjudged_tokens) == (1, 10)
    assert report.removed_by_class_prior == 2
    assert report.removed_by_min_distinct_words == 2
    assert report.removed_by_several_rules == 1
    assert "withhold" in report.class_prior

    # The class-prior share agrees with the standalone class-prior replay.
    alone = replay_class_prior(s.log, config=_PRIOR)
    assert alone.matched_servings == report.removed_by_class_prior
    assert alone.helpful_lost == 1
    assert alone.unhelpful_removed == 1
    assert alone.tokens_freed == 25 + 30


def test_a_class_prior_in_off_mode_removes_nothing_and_says_so(
    tmp_path: Path,
) -> None:
    s = _Stores(tmp_path)
    _seed_conversations(s)

    report = s.replay(
        ReplayPolicy(min_distinct_words=5),
        class_prior_config=ClassPriorConfig(mode="off"),
    )

    assert report.removed_by_class_prior == 0
    assert report.removed.items == 2
    assert any('mode is "off"' in n for n in report.notes)


# -- the mirror of the graph strategy's served text --------------------------


def test_the_replayed_entity_text_is_what_the_graph_strategy_serves(
    tmp_path: Path,
) -> None:
    store = SQLiteGraphStore(tmp_path / "graph.db")
    shapes: dict[str, dict[str, Any]] = {
        "a": {"name": "Alpha", "description": SENTENCE},
        "b": {"name": "Beta", "description": ""},
        "c": {"name": "Gamma", "description": "   "},
        "d": {"name": "Delta"},
        "e": {"title": "Epsilon title only"},
    }
    for node_id, props in shapes.items():
        store.upsert_node(node_id, "concept", props)

    items = GraphSearch(store).search(
        "q", filters={GRAPH_CALLER_SEED_IDS_KEY: list(shapes)}
    )

    served = {item.item_id: item.excerpt for item in items}
    assert set(served) >= set(shapes)
    for node_id in shapes:
        node = store.get_node(node_id)
        assert node is not None
        # The strategy cuts the text with ``truncate_excerpt`` as it serves
        # it, and ``PackItem`` (a ``TrellisModel``) strips the result. The
        # replay cuts the same way and does not strip: whitespace is not a
        # word, so the distinct-word count agrees either way.
        text = truncate_excerpt(
            _entity_served_text(node["properties"]), limit=EXCERPT_MAX_CHARS
        )
        assert text.strip() == served[node_id]
        assert count_substance_words(text) == count_substance_words(served[node_id])


# -- the default policy replays exactly as it did before the arms existed -----

_GOLDEN = Path(__file__).parent / "golden" / "replay_default_policy.json"


def test_default_policy_replays_byte_identically_to_before_the_substance_arms(
    tmp_path: Path,
) -> None:
    """Pin the pre-existing arms' output on a fixed store.

    ``golden/replay_default_policy.json`` was captured from ``origin/main``
    (00bd0b49), the source tree before the substance arms existed, by running
    the same seed through ``replay_pack_value`` (default, ``max_items=2`` and
    ``excerpt_max_chars=50, refill=False``) and ``replay_class_prior``. The
    comparison is on the serialized report, so any new field, reordered key or
    changed number in an arm this change did not mean to touch fails here.
    """
    s = _seed(tmp_path)
    _seed_conversations(s)
    _seed_walk(s)
    golden = json.loads(_GOLDEN.read_text())

    got = {
        "default": replay_pack_value(s.log, policy=ReplayPolicy()).model_dump(),
        "max2": replay_pack_value(s.log, policy=ReplayPolicy(max_items=2)).model_dump(),
        "norefill": replay_pack_value(
            s.log, policy=ReplayPolicy(refill=False, excerpt_max_chars=50)
        ).model_dump(),
        "prior": replay_class_prior(s.log, config=_PRIOR).model_dump(),
        "describe": ReplayPolicy().describe(),
    }

    for name, expected in golden.items():
        assert json.dumps(got[name], sort_keys=True) == json.dumps(
            expected, sort_keys=True
        ), name


# -- entities are judged as served --------------------------------------------

_PAUSE = 0.02  # the graph and the event log stamp wall-clock instants


def _pack_rows(item_id: str, *, tokens: int = 10) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "item_type": "entity",
        "rank": 1,
        "estimated_tokens": tokens,
        "strategy_source": "graph",
    }


def _emit_entity_pack(
    log: SQLiteEventLog,
    pack_id: str,
    item_ids: list[str],
    *,
    unhelpful: list[str] | None = None,
    occurred_at: datetime | None = None,
) -> None:
    """One pack serving ``item_ids``; ``occurred_at`` overrides the stamp.

    Every id is cited, helpful unless listed in ``unhelpful``: a pack whose
    feedback names no item is not attributed and reaches no count.
    """
    unhelpful = unhelpful or []
    stamp = {"occurred_at": occurred_at} if occurred_at is not None else {}
    log.append(
        Event(
            event_type=EventType.PACK_ASSEMBLED,
            source="pack_builder",
            entity_id=pack_id,
            entity_type="pack",
            payload={
                "pack_id": pack_id,
                "injected_items": [_pack_rows(i) for i in item_ids],
            },
            **stamp,
        )
    )
    log.emit(
        EventType.FEEDBACK_RECORDED,
        source="mcp",
        entity_id=pack_id,
        payload={
            "pack_id": pack_id,
            "helpful_item_ids": [i for i in item_ids if i not in unhelpful],
            "unhelpful_item_ids": unhelpful,
            "rating": 0.5,
            "success": True,
        },
    )


def test_a_stub_that_gained_a_description_after_it_was_served_is_removed(
    tmp_path: Path,
) -> None:
    s = _Stores(tmp_path)
    s.graph.upsert_node("e1", "concept", {"name": "Stub"})  # served as a stub
    time.sleep(_PAUSE)
    _emit_entity_pack(s.log, "p1", ["e1"], unhelpful=["e1"])
    time.sleep(_PAUSE)
    s.graph.upsert_node(
        "e1", "concept", {"name": "Stub", "description": "a real description now"}
    )
    # The precondition: today's node would pass both arms.
    assert s.graph.get_node("e1")["properties"]["description"]  # type: ignore[index]

    by_description = s.replay(ReplayPolicy(entity_requires_description=True))
    by_words = s.replay(ReplayPolicy(min_distinct_words=3))

    for report in (by_description, by_words):
        assert report.removed.items == 1
        assert report.removed.unhelpful_items == 1
        assert report.unresolved.items == 0
    assert by_description.removed_by_entity_description == 1
    assert by_words.removed_by_min_distinct_words == 1  # "Stub" is one word


def test_a_description_dropped_after_the_serving_does_not_remove_it(
    tmp_path: Path,
) -> None:
    s = _Stores(tmp_path)
    s.graph.upsert_node("e1", "concept", {"name": "N", "description": "described"})
    time.sleep(_PAUSE)
    _emit_entity_pack(s.log, "p1", ["e1"])
    time.sleep(_PAUSE)
    s.graph.upsert_node("e1", "concept", {"name": "N"})

    report = s.replay(ReplayPolicy(entity_requires_description=True))

    assert report.removed.items == 0
    assert report.served.items == 1


def test_a_node_minted_after_the_serving_is_unresolved(tmp_path: Path) -> None:
    s = _Stores(tmp_path)
    _emit_entity_pack(s.log, "p1", ["e1"], unhelpful=["e1"])
    time.sleep(_PAUSE)
    s.graph.upsert_node("e1", "concept", {"name": "Late"})

    report = s.replay(ReplayPolicy(entity_requires_description=True))

    assert report.removed.items == 0
    assert report.unresolved.items == 1
    assert report.unresolved_entity_description == 1


def test_a_hard_deleted_node_stays_unresolved(tmp_path: Path) -> None:
    """``delete_node`` purges history, so there is no as-served version to read."""
    s = _Stores(tmp_path)
    s.graph.upsert_node("e1", "concept", {"name": "Stub"})
    time.sleep(_PAUSE)
    _emit_entity_pack(s.log, "p1", ["e1"], unhelpful=["e1"])
    time.sleep(_PAUSE)
    assert s.graph.delete_node("e1")

    report = s.replay(ReplayPolicy(entity_requires_description=True))

    assert report.removed.items == 0
    assert report.unresolved.items == 1
    assert report.unresolved.unhelpful_items == 1


def test_one_node_is_read_at_each_packs_own_time(tmp_path: Path) -> None:
    """The cache is keyed on ``(item_id, served_at)``, not on the id alone."""
    s = _Stores(tmp_path)
    s.graph.upsert_node("e1", "concept", {"name": "Stub"})
    time.sleep(_PAUSE)
    _emit_entity_pack(s.log, "before", ["e1"])
    time.sleep(_PAUSE)
    s.graph.upsert_node("e1", "concept", {"name": "Stub", "description": "described"})
    time.sleep(_PAUSE)
    _emit_entity_pack(s.log, "after", ["e1"])

    report = s.replay(ReplayPolicy(entity_requires_description=True))

    assert report.served.items == 2
    assert report.removed.items == 1  # the serving before the description
    assert report.unresolved.items == 0


class _RecordingGraph:
    """A graph stand-in that records every ``get_node`` and answers from a dict."""

    def __init__(self, nodes: dict[str, dict[str, Any]]) -> None:
        self.nodes = nodes
        self.calls: list[tuple[str, datetime | None]] = []

    def get_node(self, node_id: str, as_of: datetime | None = None):
        self.calls.append((node_id, as_of))
        props = self.nodes.get(node_id)
        return None if props is None else {"node_id": node_id, "properties": props}


def test_a_node_two_rules_need_is_read_once_per_pack(tmp_path: Path) -> None:
    s = _Stores(tmp_path)
    graph = _RecordingGraph({"e1": {"name": "N", "description": "one two three"}})
    _emit_entity_pack(s.log, "p1", ["e1"])
    _emit_entity_pack(s.log, "p2", ["e1"])

    replay_substance(
        s.log,
        policy=ReplayPolicy(entity_requires_description=True, min_distinct_words=2),
        graph_store=graph,  # type: ignore[arg-type]
    )

    # Two packs, two instants, two reads; the second rule reuses the first's.
    assert len(graph.calls) == 2
    assert len({as_of for _, as_of in graph.calls}) == 2


@pytest.mark.parametrize(
    "to_stamp",
    [
        pytest.param(lambda t: t, id="aware-utc"),
        pytest.param(lambda t: t.replace(tzinfo=None), id="naive"),
        pytest.param(
            lambda t: t.astimezone(timezone(timedelta(hours=5, minutes=30))),
            id="offset",
        ),
    ],
)
def test_the_as_of_instant_is_aware_utc_whatever_the_event_stamp(
    tmp_path: Path, to_stamp: Any
) -> None:
    s = _Stores(tmp_path)
    instant = datetime.now(UTC) - timedelta(hours=1)
    graph = _RecordingGraph({"e1": {"name": "N", "description": "d"}})
    _emit_entity_pack(s.log, "p1", ["e1"], occurred_at=to_stamp(instant))

    replay_substance(
        s.log,
        policy=ReplayPolicy(entity_requires_description=True),
        graph_store=graph,  # type: ignore[arg-type]
    )

    ((_, as_of),) = graph.calls
    assert as_of is not None
    assert as_of.utcoffset() == timedelta(0)
    assert as_of == instant


def test_a_document_is_judged_on_current_state_not_as_served(tmp_path: Path) -> None:
    s = _Stores(tmp_path)
    s.docs.put("d1", "two words", {})
    s.emit_pack("p1", [("d1", "document", 10, "unhelpful")])
    s.docs.put("d1", "alpha beta gamma delta epsilon zeta", {})

    report = s.replay(ReplayPolicy(min_distinct_words=5))

    # Six words today, so it is kept, though it served two.
    assert report.removed.items == 0
    assert report.judged_on["min_distinct_words.document"] == (
        SUBSTANCE_BASIS_CURRENT_STATE
    )


def test_a_truncated_event_scan_is_stated_in_a_note(stores: _Stores) -> None:
    capped = stores.replay(ReplayPolicy(min_distinct_words=5), limit=2)
    complete = stores.replay(ReplayPolicy(min_distinct_words=5))

    truncated = [n for n in capped.notes if "TRUNCATED" in n]
    assert len(truncated) == 1
    assert "newer part of the window only" in truncated[0]
    assert not any("TRUNCATED" in n for n in complete.notes)
    # Only the note differs: the counts are of what was read.
    assert capped.notes[: len(complete.notes)] == complete.notes
