"""Pins on the class-prior arm that the substance arms reuse.

``replay_substance`` shares ``_tally_class_prior_pack`` with
``replay_class_prior`` and asks it for the ids it removes. Three things must
hold, and a plain seed reaches none of the code that makes them hard:

* the standalone arm's output on a seed that reaches the "still served by
  another axis" and the chunk-scope parent-lookup paths is pinned to a golden
  file captured from ``origin/main``'s code;
* in combined mode the class-prior share of ``removed`` is exactly the
  standalone ``matched_servings - still_served_by_other_axis``;
* asking for the removed ids changes no count.

``replay_substance`` is also checked against a from-the-spec recomputation over
random stores, so a rule that quietly stops holding fails here rather than in a
report.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from tests.unit.retrieve.replay_seeds import (
    AXES,
    class_prior_edge_dump,
    random_parent_docs,
    random_payload,
    seed_class_prior_edges,
)
from trellis.retrieve.class_prior import ClassPriorConfig
from trellis.retrieve.excerpts import count_substance_words, truncate_excerpt
from trellis.retrieve.pack_replay import (
    ReplayPolicy,
    _ClassPriorTally,
    _ParentFormLookup,
    _tally_class_prior_pack,
    replay_class_prior,
    replay_substance,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.graph import SQLiteGraphStore

_GOLDEN = Path(__file__).parent / "golden" / "replay_class_prior_edges.json"
_FORM_NAMES = ["conversation", "note", "runbook"]


def _random_config(rng: random.Random) -> ClassPriorConfig:
    return ClassPriorConfig(
        mode="withhold",
        forms=frozenset(rng.sample(_FORM_NAMES, rng.randint(1, 3))),
        axes=frozenset(rng.sample(list(AXES), rng.randint(1, 3))),
        scope=rng.choice(["parents", "parents_and_chunks"]),
    )


# -- the golden arm set, captured from origin/main ----------------------------


def test_class_prior_edge_seed_replays_as_origin_main_did(tmp_path: Path) -> None:
    """The dedup-loser and parent-lookup paths, pinned.

    ``golden/replay_class_prior_edges.json`` was captured by running
    ``class_prior_edge_dump`` against a ``git archive`` of ``origin/main``
    (3c7e7917), whose ``pack_replay.py`` has no substance arms. The compared
    values are the serialized reports, so a changed count, note or field fails.
    """
    log = SQLiteEventLog(tmp_path / "events.db")
    docs = SQLiteDocumentStore(tmp_path / "documents.db")
    seed_class_prior_edges(log, docs)

    got = class_prior_edge_dump(log, docs)

    golden = json.loads(_GOLDEN.read_text())
    assert set(got) == set(golden)
    for name, expected in golden.items():
        assert json.dumps(got[name], sort_keys=True) == json.dumps(
            expected, sort_keys=True
        ), name
    # The seed does reach the two paths, or the pin proves nothing.
    assert golden["keyword_conversation"]["still_served_by_other_axis"] == 1
    assert golden["chunks_with_store"]["parent_form_resolved"] == 3
    assert golden["chunks_with_store"]["parent_form_unresolved"] == 1
    assert golden["chunks_with_store"]["matched_via_parent"] == 3


def test_combined_share_on_the_edge_seed_is_matched_minus_still_served(
    tmp_path: Path,
) -> None:
    log = SQLiteEventLog(tmp_path / "events.db")
    docs = SQLiteDocumentStore(tmp_path / "documents.db")
    graph = SQLiteGraphStore(tmp_path / "graph.db")
    seed_class_prior_edges(log, docs)
    cases = [
        ({"conversation"}, {"keyword"}, "parents"),
        ({"conversation", "note"}, {"semantic", "keyword"}, "parents_and_chunks"),
        ({"conversation"}, {"keyword", "semantic"}, "parents"),
    ]
    for forms, axes, scope in cases:
        config = ClassPriorConfig(
            mode="withhold", forms=frozenset(forms), axes=frozenset(axes), scope=scope
        )
        alone = replay_class_prior(log, config=config, document_store=docs)
        combined = replay_substance(
            log,
            policy=ReplayPolicy(entity_requires_description=True),
            graph_store=graph,
            document_store=docs,
            class_prior_config=config,
        )
        assert (
            combined.removed_by_class_prior
            == alone.matched_servings - alone.still_served_by_other_axis
        ), (forms, axes, scope)


# -- properties over random stores --------------------------------------------


@pytest.mark.parametrize("seed", range(60))
def test_combined_class_prior_share_equals_standalone_over_seeds(
    tmp_path: Path, seed: int
) -> None:
    rng = random.Random(5000 + seed)  # noqa: S311
    log = SQLiteEventLog(tmp_path / "events.db")
    docs = SQLiteDocumentStore(tmp_path / "documents.db")
    graph = SQLiteGraphStore(tmp_path / "graph.db")
    random_parent_docs(docs, rng)
    for n in range(rng.randint(1, 15)):
        pack_id = f"pk{n}"
        payload = random_payload(rng, pack_id)
        # One row per id: a duplicate row is one serving in the substance arm
        # and two in the class-prior arm, which this property is not about.
        seen: set[str] = set()
        payload["injected_items"] = [
            r
            for r in payload["injected_items"]
            if not (r["item_id"] in seen or seen.add(r["item_id"]))
        ]
        log.emit(
            EventType.PACK_ASSEMBLED,
            source="pack_builder",
            entity_id=pack_id,
            entity_type="pack",
            payload=payload,
        )
        ids = [r["item_id"] for r in payload["injected_items"]]
        log.emit(
            EventType.FEEDBACK_RECORDED,
            source="mcp",
            entity_id=pack_id,
            payload={
                "pack_id": pack_id,
                "helpful_item_ids": ids[:1],
                "unhelpful_item_ids": ids[1:2],
                "rating": 0.5,
                "success": True,
            },
        )
    config = _random_config(rng)

    alone = replay_class_prior(log, config=config, document_store=docs)
    combined = replay_substance(
        log,
        policy=ReplayPolicy(entity_requires_description=True),
        graph_store=graph,
        document_store=docs,
        class_prior_config=config,
    )

    assert (
        combined.removed_by_class_prior
        == alone.matched_servings - alone.still_served_by_other_axis
    )


@pytest.mark.parametrize("seed", range(60))
def test_asking_for_the_removed_ids_changes_no_count(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311
    docs = SQLiteDocumentStore(tmp_path / "documents.db")
    random_parent_docs(docs, rng)
    payload = random_payload(rng, "p")
    rows = [r for r in payload["injected_items"] if r.get("item_id")]
    helpful = {r["item_id"] for r in rows if rng.random() < 0.4}
    unhelpful = {
        r["item_id"] for r in rows if r["item_id"] not in helpful and rng.random() < 0.4
    }
    config = _random_config(rng)
    rejected = payload.get("rejected_items", [])

    def tally(removed_ids: set[str] | None) -> dict[str, Any]:
        t = _ClassPriorTally()
        _tally_class_prior_pack(
            rows,
            rejected_rows=rejected,
            helpful=helpful,
            unhelpful=unhelpful,
            config=config,
            lookup=_ParentFormLookup(docs),
            tally=t,
            removed_ids=removed_ids,
        )
        return vars(t)

    removed: set[str] = set()
    assert tally(removed) == tally(None)
    # Every removed id was matched, and none of them was a still-served one.
    assert removed <= {r["item_id"] for r in rows}


# -- replay_substance against a recomputation from the spec -------------------

_WORDS = [
    "alpha",
    "beta",
    "gamma",
    "delta",
    "epsilon",
    "zeta",
    "eta",
    "theta",
    "iota",
    "kappa",
]
_EXEMPT = "observation"


def _expected(  # noqa: PLR0912
    packs: list[tuple[str, list[dict[str, Any]]]],
    feedback: dict[str, tuple[set[str], set[str]]],
    node_props: dict[str, dict[str, Any]],
    doc_text: dict[str, str],
    policy: ReplayPolicy,
) -> tuple[dict[str, list[int]], dict[str, int], int]:
    """Per-bucket ``[servings, tokens, h, u, j]``, rule counts, overlap."""
    buckets = {k: [0] * 5 for k in ("served", "removed", "unresolved")}
    by_rule = {"entity": 0, "words": 0}
    several = 0
    for pack_id, rows in packs:
        if pack_id not in feedback:
            continue
        helpful, unhelpful = feedback[pack_id]
        seen: set[str] = set()
        for row in rows:
            item_id = row["item_id"]
            if item_id in seen:
                continue
            seen.add(item_id)
            raw = row.get("estimated_tokens")
            tokens = int(raw) if isinstance(raw, int | float) else 0
            slot = 2 if item_id in helpful else 3 if item_id in unhelpful else 4
            item_type = row.get("item_type")
            fired: list[str] = []
            could_not = False
            props = node_props.get(item_id)
            if policy.entity_requires_description and item_type == "entity":
                if props is None:
                    could_not = True
                elif not str(props.get("description") or "").strip():
                    fired.append("entity")
            if policy.min_distinct_words is not None and item_type != _EXEMPT:
                if item_type == "entity":
                    text = (
                        None
                        if props is None
                        else str(
                            props.get(
                                "description", props.get("name", props.get("title", ""))
                            )
                        )
                    )
                else:
                    text = doc_text.get(item_id)
                if text is None:
                    could_not = True
                elif (
                    count_substance_words(truncate_excerpt(text, 500))
                    < policy.min_distinct_words
                ):
                    fired.append("words")
            for name in ["served"] + (
                ["removed"] if fired else ["unresolved"] if could_not else []
            ):
                buckets[name][0] += 1
                buckets[name][1] += tokens
                buckets[name][slot] += 1
            for rule in fired:
                by_rule[rule] += 1
            several += len(fired) > 1
    return buckets, by_rule, several


@pytest.mark.parametrize("seed", range(40))
def test_substance_replay_matches_a_recomputation_from_the_spec(
    tmp_path: Path, seed: int
) -> None:
    rng = random.Random(1000 + seed)  # noqa: S311
    log = SQLiteEventLog(tmp_path / "events.db")
    graph = SQLiteGraphStore(tmp_path / "graph.db")
    docs = SQLiteDocumentStore(tmp_path / "documents.db")
    node_props: dict[str, dict[str, Any]] = {}
    doc_text: dict[str, str] = {}
    for i in range(8):
        draw = rng.random()
        if draw < 0.15:
            continue  # a node the graph never held
        props: dict[str, Any] = {
            "name": " ".join(rng.sample(_WORDS, rng.randint(1, 3)))
        }
        if draw < 0.4:
            props["description"] = rng.choice(
                ["", "   ", " ".join(rng.sample(_WORDS, rng.randint(1, 9)))]
            )
        graph.upsert_node(f"e{i}", "concept", props)
        node_props[f"e{i}"] = props
    for i in range(12):
        if rng.random() < 0.85:
            text = " ".join(
                rng.choice(_WORDS) + rng.choice(["", ",", "."])
                for _ in range(rng.randint(0, 12))
            )
            docs.put(f"d{i}", text, {})
            doc_text[f"d{i}"] = text
    packs: list[tuple[str, list[dict[str, Any]]]] = []
    feedback: dict[str, tuple[set[str], set[str]]] = {}
    for n in range(rng.randint(1, 15)):
        pack_id = f"pk{n}"
        payload = random_payload(rng, pack_id)
        log.emit(
            EventType.PACK_ASSEMBLED,
            source="pack_builder",
            entity_id=pack_id,
            entity_type="pack",
            payload=payload,
        )
        served = [r["item_id"] for r in payload["injected_items"]]
        helpful = {i for i in served if rng.random() < 0.3}
        unhelpful = {i for i in served if i not in helpful and rng.random() < 0.3}
        if helpful or unhelpful:
            log.emit(
                EventType.FEEDBACK_RECORDED,
                source="mcp",
                entity_id=pack_id,
                payload={
                    "pack_id": pack_id,
                    "helpful_item_ids": sorted(helpful),
                    "unhelpful_item_ids": sorted(unhelpful),
                    "rating": 0.5,
                    "success": True,
                },
            )
            # A held-out pack reaches nothing, whatever its feedback says.
            if payload.get("holdout") is not True:
                feedback[pack_id] = (helpful, unhelpful)
        packs.append((pack_id, payload["injected_items"]))
    policy = rng.choice(
        [
            ReplayPolicy(entity_requires_description=True),
            ReplayPolicy(min_distinct_words=rng.randint(1, 8)),
            ReplayPolicy(
                entity_requires_description=True, min_distinct_words=rng.randint(1, 8)
            ),
        ]
    )

    report = replay_substance(
        log, policy=policy, graph_store=graph, document_store=docs
    )

    buckets, by_rule, several = _expected(packs, feedback, node_props, doc_text, policy)
    for name in ("served", "removed", "unresolved"):
        t = getattr(report, name)
        assert [
            t.items,
            t.tokens,
            t.helpful_items,
            t.unhelpful_items,
            t.unjudged_items,
        ] == buckets[name], name
    assert report.removed_by_entity_description == by_rule["entity"]
    assert report.removed_by_min_distinct_words == by_rule["words"]
    assert report.removed_by_several_rules == several
    assert report.attributed_packs == len(feedback)
