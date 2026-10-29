"""A re-run never writes a source-text name back onto a scrubbed precedent (#1025).

Redacting a precedent's *source document* scrubs the precedent rather than
purging it: the name becomes neutral and ``source_excerpt_status`` reads
``source_redacted``. The ``REDACTION_APPLIED`` event names the source document,
not the precedent, so the #1019 refusal (keyed on the precedent id) does not
fire. A re-run with the candidate captured before the redaction, as the on-disk
candidates file holds it, then wrote a new node version whose name was rebuilt
from the source text. The refusal is ``refused_redacted``, decided in
``_pre_write_outcome`` so the preview and the submission cannot disagree.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from trellis.learning.scoring import (
    NAME_SOURCE_NEUTRAL,
    NAME_SOURCE_SOURCE_TEXT,
    REFUSED_REDACTED,
    SOURCE_EXCERPT_REDACTED,
    _apply_readable_name_fallback,
    build_learning_promotion_payloads,
    precedent_entity_id_for_candidate,
    preview_learning_promotion,
    promotion_is_refusal,
    promotion_verdict,
    submit_learning_promotion,
)
from trellis.mutate import Command, CommandStatus, Operation, build_curate_executor
from trellis.schemas.classification import LIFECYCLE_KEY
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

SOURCE_ID = "doc-source"
NAME_TEXT = "Quartz heron opening line of the source"
COPIED_TEXT = "Amber ledger sentence copied from the source body"


def _candidate(item_id: str = SOURCE_ID) -> dict[str, Any]:
    """A title-less candidate as the analyzer writes it, before any fallback name."""
    return {
        "candidate_id": f"cand:{item_id}",
        "intent_family": "asset_generation",
        "recommendation_type": "promote_guidance",
        "item_id": item_id,
        "title": None,
        "precedent_name": f"Learning: asset_generation :: {item_id}",
        "precedent_name_source": "item_id",
        "precedent_properties": {
            "intent_family": "asset_generation",
            "source_item_id": item_id,
        },
        "target_entity_ids": ["entity://shared"],
        "supporting_run_ids": ["run-1"],
        "phases": ["GENERATE"],
    }


class _Stores:
    def __init__(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir()
        self.registry = StoreRegistry(stores_dir=stores_dir)
        self.executor = build_curate_executor(self.registry)
        self.graph = self.registry.knowledge.graph_store
        self.events = self.registry.operational.event_log
        self.documents = self.registry.knowledge.document_store
        self.graph.upsert_node("entity://shared", "entity", {"name": "shared"})
        self.documents.put(SOURCE_ID, f"{NAME_TEXT}\n\n{COPIED_TEXT}", {})
        self.graph.upsert_node(SOURCE_ID, "CreativeWork", {"name": SOURCE_ID})

    def named_from_source(self) -> dict[str, Any]:
        candidate = _candidate()
        _apply_readable_name_fallback(candidate, self.documents)
        assert candidate["precedent_name_source"] == NAME_SOURCE_SOURCE_TEXT
        assert NAME_TEXT in candidate["precedent_name"]
        return candidate

    def payloads(self, candidate: dict[str, Any]) -> dict[str, Any]:
        return build_learning_promotion_payloads(
            candidate=candidate,
            promotion_name="",
            rationale="approved",
            document_store=self.documents,
        )

    def submit(self, candidate: dict[str, Any]) -> dict[str, Any]:
        payloads = self.payloads(candidate)
        return submit_learning_promotion(
            self.executor,
            payloads["entity_payload"],
            payloads["edge_payloads"],
            requested_by="test",
            graph_store=self.graph,
            event_log=self.events,
            document_store=self.documents,
        )

    def preview(self, candidate: dict[str, Any]) -> dict[str, Any] | None:
        payloads = self.payloads(candidate)
        return preview_learning_promotion(
            payloads["entity_payload"],
            payloads["edge_payloads"],
            graph_store=self.graph,
            event_log=self.events,
            document_store=self.documents,
        )

    def redact_source(self) -> None:
        result = self.executor.execute(
            Command(
                operation=Operation.REDACTION_APPLY,
                args={"target_id": SOURCE_ID, "reason": "owner request"},
                target_id=SOURCE_ID,
                target_type="entity",
                requested_by="test",
            )
        )
        assert result.status == CommandStatus.SUCCESS, result.message

    def promote_events(self, entity_id: str) -> int:
        return len(
            self.events.get_events(
                event_type=EventType.PRECEDENT_PROMOTED, entity_id=entity_id
            )
        )

    def executed_events(self) -> int:
        return len(self.events.get_events(event_type=EventType.MUTATION_EXECUTED))

    def everything_stored(self, entity_id: str) -> str:
        """Every place the precedent's name or text is kept, as one string."""
        return repr(
            [
                self.graph.get_node(entity_id),
                self.graph.get_node_history(entity_id),
                self.documents.get(entity_id),
            ]
        )


@pytest.fixture
def stores(tmp_path: Path):
    s = _Stores(tmp_path)
    yield s
    s.registry.close()


def _promoted_then_source_redacted(stores: _Stores) -> tuple[str, dict[str, Any]]:
    """Promote a source-named precedent, redact its source; return its id and the
    candidate as it stood before the redaction."""
    candidate = stores.named_from_source()
    stale = copy.deepcopy(candidate)
    first = stores.submit(candidate)
    assert first["status"] == "promoted", first
    entity_id = precedent_entity_id_for_candidate(candidate["candidate_id"])
    assert NAME_TEXT in stores.everything_stored(entity_id)

    stores.redact_source()

    # The precedent survived, scrubbed: the state #1019's id lookup cannot see.
    node = stores.graph.get_node(entity_id)
    assert node is not None
    assert node["properties"]["name_source"] == NAME_SOURCE_NEUTRAL
    assert node["properties"]["source_excerpt_status"] == SOURCE_EXCERPT_REDACTED
    assert NAME_TEXT not in stores.everything_stored(entity_id)
    assert COPIED_TEXT not in stores.everything_stored(entity_id)
    return entity_id, stale


def test_a_stale_candidate_is_refused_after_its_source_was_redacted(
    stores: _Stores,
) -> None:
    entity_id, stale = _promoted_then_source_redacted(stores)
    history_before = stores.graph.get_node_history(entity_id)
    promoted_before = stores.promote_events(entity_id)
    executed_before = stores.executed_events()

    outcome = stores.submit(stale)

    assert outcome["status"] == REFUSED_REDACTED
    assert outcome["entity_id"] == entity_id
    assert entity_id in outcome["message"]
    assert NAME_TEXT not in outcome["message"]
    assert promotion_is_refusal(outcome)
    assert promotion_verdict([outcome]) == "refused"
    # Nothing was written: no new node version, no name, no event, no command.
    assert stores.graph.get_node_history(entity_id) == history_before
    assert NAME_TEXT not in stores.everything_stored(entity_id)
    assert COPIED_TEXT not in stores.everything_stored(entity_id)
    assert stores.promote_events(entity_id) == promoted_before
    assert stores.executed_events() == executed_before


def test_the_preview_reports_the_refusal_the_submission_answers(
    stores: _Stores,
) -> None:
    _, stale = _promoted_then_source_redacted(stores)

    preview = stores.preview(stale)

    assert preview is not None
    assert preview["status"] == REFUSED_REDACTED
    assert preview == stores.submit(stale)


def test_a_candidate_regenerated_after_the_redaction_still_goes_through(
    stores: _Stores,
) -> None:
    """Its source is blank now, so the fallback finds no text to name it from."""
    entity_id, _ = _promoted_then_source_redacted(stores)
    regenerated = _candidate()
    _apply_readable_name_fallback(regenerated, stores.documents)
    assert regenerated["precedent_name_source"] != NAME_SOURCE_SOURCE_TEXT

    assert stores.preview(regenerated) is None
    outcome = stores.submit(regenerated)

    assert outcome["status"] == "promoted", outcome
    assert NAME_TEXT not in stores.everything_stored(entity_id)
    assert COPIED_TEXT not in stores.everything_stored(entity_id)


def test_the_same_candidate_re_run_on_an_unscrubbed_precedent_is_not_refused(
    stores: _Stores,
) -> None:
    """The check keys on a scrub, not on a source-text name."""
    candidate = stores.named_from_source()
    assert stores.submit(candidate)["status"] == "promoted"

    outcome = stores.submit(copy.deepcopy(candidate))

    assert outcome["status"] == "already_promoted"


def _precedent_node(stores: _Stores, properties: dict[str, Any]) -> str:
    entity_id = stores.payloads(stores.named_from_source())["entity_id"]
    stores.graph.upsert_node(
        entity_id,
        "precedent",
        {"name": "Learning: asset_generation :: held", **properties},
    )
    return entity_id


@pytest.mark.parametrize(
    "scrub",
    [
        pytest.param({"name_source": NAME_SOURCE_NEUTRAL}, id="neutral-name"),
        pytest.param(
            {
                "name_source": "item_title",
                "source_excerpt_status": SOURCE_EXCERPT_REDACTED,
            },
            id="excerpt-redacted",
        ),
    ],
)
def test_either_scrub_marker_refuses_a_source_text_name(
    stores: _Stores, scrub: dict[str, Any]
) -> None:
    _precedent_node(stores, scrub)

    outcome = stores.preview(stores.named_from_source())

    assert outcome is not None
    assert outcome["status"] == REFUSED_REDACTED


def test_a_scrubbed_precedent_is_refused_before_its_lifecycle_state(
    stores: _Stores,
) -> None:
    """Reinstating it first would only reach this refusal on the next run."""
    _precedent_node(
        stores,
        {
            "name_source": NAME_SOURCE_NEUTRAL,
            LIFECYCLE_KEY: {"state": "demoted"},
        },
    )

    outcome = stores.preview(stores.named_from_source())

    assert outcome is not None
    assert outcome["status"] == REFUSED_REDACTED


@pytest.mark.parametrize(
    "marker",
    [None, "reviewer", "item_title", "item_id"],
)
def test_a_name_with_no_source_text_is_not_refused_on_a_scrubbed_precedent(
    stores: _Stores, marker: str | None
) -> None:
    """Only a name that may hold the source's text is refused; the rest write."""
    _precedent_node(
        stores,
        {
            "name_source": NAME_SOURCE_NEUTRAL,
            "source_excerpt_status": SOURCE_EXCERPT_REDACTED,
        },
    )
    candidate = _candidate()
    if marker is None:
        candidate.pop("precedent_name_source")
    else:
        candidate["precedent_name_source"] = marker
    if marker == "item_title":
        candidate["precedent_name"] = "Learning: asset_generation :: A real title"

    outcome = stores.preview(candidate)

    assert outcome is None or not promotion_is_refusal(outcome)


def test_an_unmarked_candidate_that_holds_source_text_is_refused(
    stores: _Stores,
) -> None:
    """A candidates file written before names were marked names no source, and
    its name is source text all the same: the scrub's own test refuses it."""
    _precedent_node(stores, {"name_source": NAME_SOURCE_NEUTRAL})
    unmarked = stores.named_from_source()
    unmarked.pop("precedent_name_source")

    outcome = stores.preview(unmarked)

    assert outcome is not None
    assert outcome["status"] == REFUSED_REDACTED
