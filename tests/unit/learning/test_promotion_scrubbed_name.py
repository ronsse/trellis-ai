"""A re-run never restores a source-text name over its source's redaction (#1025).

Redacting a precedent's *source document* scrubs the precedent rather than
purging it: the name becomes neutral and ``source_excerpt_status`` reads
``source_redacted``. The ``REDACTION_APPLIED`` event names the source document,
not the precedent, so the #1019 refusal (keyed on the precedent id) does not
fire. A re-run with the candidate captured before the redaction, as the on-disk
candidates file holds it, then wrote a new node version whose name was rebuilt
from the source text. The refusal is ``refused_redacted``, decided in
``_pre_write_outcome`` so the preview and the submission cannot disagree.

The signal that the source was redacted is the redaction's own trace on the
payload's ``source_item_id`` (or its chunk parent), not the node's scrub
markers, which a later promotion of a regenerated candidate overwrites. A
decisions file's ``promotion_name`` is pre-filled with the derived name, so a
name the precedent carried earlier is refused as well."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trellis.learning.scoring import (
    NAME_SOURCE_NEUTRAL,
    NAME_SOURCE_SOURCE_TEXT,
    REFUSED_REDACTED,
    SOURCE_EXCERPT_INCLUDED,
    SOURCE_EXCERPT_REDACTED,
    _apply_readable_name_fallback,
    _earlier_source_names,
    _restores_earlier_name,
    build_learning_promotion_payloads,
    neutral_precedent_name,
    precedent_entity_id_for_candidate,
    prepare_learning_promotions,
    preview_learning_promotion,
    promotion_is_refusal,
    promotion_verdict,
    submit_learning_promotion,
)
from trellis.mutate import Command, CommandStatus, Operation, build_curate_executor
from trellis.retrieve.precedents import list_precedents
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

    def payloads(
        self, candidate: dict[str, Any], promotion_name: str = ""
    ) -> dict[str, Any]:
        return build_learning_promotion_payloads(
            candidate=candidate,
            promotion_name=promotion_name,
            rationale="approved",
            document_store=self.documents,
        )

    def submit(
        self, candidate: dict[str, Any], promotion_name: str = ""
    ) -> dict[str, Any]:
        payloads = self.payloads(candidate, promotion_name)
        return submit_learning_promotion(
            self.executor,
            payloads["entity_payload"],
            payloads["edge_payloads"],
            requested_by="test",
            graph_store=self.graph,
            event_log=self.events,
            document_store=self.documents,
        )

    def submit_planned(
        self, candidate: dict[str, Any], decision_name: str
    ) -> dict[str, Any]:
        """Submit the row the planner builds from a decisions file."""
        plan = prepare_learning_promotions(
            candidates_payload={"candidates": [candidate]},
            decisions_payload={
                "decisions": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "approved": True,
                        "promotion_name": decision_name,
                        "rationale": "approved",
                    }
                ]
            },
            document_store=self.documents,
        )
        row = plan["results"][0]
        assert row["status"] == "ready", row
        return submit_learning_promotion(
            self.executor,
            row["entity_payload"],
            row["edge_payloads"],
            requested_by="test",
            graph_store=self.graph,
            event_log=self.events,
            document_store=self.documents,
        )

    def preview(
        self,
        candidate: dict[str, Any],
        promotion_name: str = "",
        *,
        event_log: Any = "stores",
        document_store: Any = "stores",
    ) -> dict[str, Any] | None:
        """The preview, by default over this fixture's own log and documents."""
        payloads = self.payloads(candidate, promotion_name)
        return preview_learning_promotion(
            payloads["entity_payload"],
            payloads["edge_payloads"],
            graph_store=self.graph,
            event_log=self.events if event_log == "stores" else event_log,
            document_store=(
                self.documents if document_store == "stores" else document_store
            ),
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
    [None, "reviewer", "item_id"],
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


def _regenerated(stores: _Stores) -> dict[str, Any]:
    """The candidate as the analyzer writes it once the source reads as withheld."""
    candidate = _candidate()
    _apply_readable_name_fallback(candidate, stores.documents)
    assert candidate["precedent_name_source"] != NAME_SOURCE_SOURCE_TEXT
    return candidate


def test_the_refusal_outlives_a_regenerated_promotion_that_cleared_the_markers(
    stores: _Stores,
) -> None:
    """The node's scrub markers are not the signal: a regenerated candidate may
    promote (its name holds no source text) and writes a version without them.
    The stale candidate then reaches the same precedent, whose node reads as
    unscrubbed; the redaction's trace on the source still refuses it."""
    entity_id, stale = _promoted_then_source_redacted(stores)
    assert stores.submit(_regenerated(stores))["status"] == "promoted"
    props = stores.graph.get_node(entity_id)["properties"]
    assert props.get("name_source") != NAME_SOURCE_NEUTRAL
    assert props.get("source_excerpt_status") != SOURCE_EXCERPT_REDACTED

    outcome = stores.submit(stale)

    assert outcome["status"] == REFUSED_REDACTED
    assert NAME_TEXT not in stores.everything_stored(entity_id)
    assert COPIED_TEXT not in stores.everything_stored(entity_id)


def test_a_stale_candidate_first_promoted_after_the_redaction_is_refused(
    stores: _Stores,
) -> None:
    """No precedent exists to scrub, so no node carries a marker; the source's
    own trace is all there is."""
    stale = stores.named_from_source()
    stores.redact_source()
    entity_id = precedent_entity_id_for_candidate(stale["candidate_id"])
    assert stores.graph.get_node(entity_id) is None

    outcome = stores.submit(stale)

    assert outcome["status"] == REFUSED_REDACTED
    assert stores.graph.get_node(entity_id) is None
    assert stores.documents.get(entity_id) is None
    assert stores.promote_events(entity_id) == 0


def test_a_decisions_file_kept_from_before_the_redaction_is_refused(
    stores: _Stores,
) -> None:
    """The template pre-fills ``promotion_name`` with the derived name, and a
    name that differs from the regenerated candidate's reads as a reviewer's.
    The precedent carried it before, in a promotion event the log keeps."""
    entity_id, stale = _promoted_then_source_redacted(stores)

    outcome = stores.submit_planned(_regenerated(stores), stale["precedent_name"])

    assert outcome["status"] == REFUSED_REDACTED
    assert NAME_TEXT not in stores.everything_stored(entity_id)
    assert NAME_TEXT not in outcome["message"]


def test_a_touched_up_stale_name_is_refused_too(stores: _Stores) -> None:
    """Stale candidate and stale decisions, the name edited a little."""
    _, stale = _promoted_then_source_redacted(stores)

    outcome = stores.submit_planned(stale, stale["precedent_name"] + " (revised)")

    assert outcome["status"] == REFUSED_REDACTED


def test_a_new_reviewer_title_still_writes_after_the_redaction(
    stores: _Stores,
) -> None:
    entity_id, _ = _promoted_then_source_redacted(stores)
    title = "Reuse the approved opening"

    assert stores.preview(_regenerated(stores), title) is None
    outcome = stores.submit_planned(_regenerated(stores), title)

    assert outcome["status"] == "promoted", outcome
    node = stores.graph.get_node(entity_id)
    assert node["properties"]["name"] == title
    assert node["properties"]["name_source"] == "reviewer"
    assert NAME_TEXT not in stores.everything_stored(entity_id)


def test_the_bare_id_name_after_an_unmarked_promotion_is_not_refused(
    stores: _Stores,
) -> None:
    """An earlier name counts only if it may have held source text: an unmarked
    bare-id title does not, so the same name written again is not refused."""
    stores.redact_source()
    candidate = _regenerated(stores)
    candidate.pop("precedent_name_source")
    assert stores.submit(candidate)["status"] == "promoted"

    outcome = stores.preview(candidate)

    assert outcome is None or not promotion_is_refusal(outcome)


def test_a_chunk_of_the_redacted_document_is_refused_by_its_parent(
    stores: _Stores,
) -> None:
    """A redaction names the document it was asked about; a precedent promoted
    from one of its chunks carries the chunk's id, so the parent is looked up."""
    chunk_id = f"{SOURCE_ID}#chunk-0"
    stale = _candidate(chunk_id)
    stale["precedent_name"] = NAME_TEXT
    stale["precedent_name_source"] = NAME_SOURCE_SOURCE_TEXT
    stores.redact_source()
    assert stores.documents.get(chunk_id) is None

    outcome = stores.preview(stale)

    assert outcome is not None
    assert outcome["status"] == REFUSED_REDACTED


def test_a_document_that_only_looks_like_a_chunk_does_not_name_its_parent(
    stores: _Stores,
) -> None:
    """``chunk_parent_id`` matches the minted shape only; ``<x>#chunk-intro``
    is not a chunk of ``<x>``."""
    stale = _candidate(f"{SOURCE_ID}#chunk-intro")
    stale["precedent_name"] = NAME_TEXT
    stale["precedent_name_source"] = NAME_SOURCE_SOURCE_TEXT
    stores.redact_source()

    assert stores.preview(stale) is None


def test_the_tombstone_alone_marks_the_source_redacted(stores: _Stores) -> None:
    """No event log passed: the source's blank document row is the trace."""
    stale = stores.named_from_source()
    stores.redact_source()

    outcome = stores.preview(stale, event_log=None)

    assert outcome is not None
    assert outcome["status"] == REFUSED_REDACTED


def test_the_event_alone_marks_the_source_redacted(stores: _Stores) -> None:
    """No document store passed: the ``REDACTION_APPLIED`` event is the trace."""
    stale = stores.named_from_source()
    stores.redact_source()

    outcome = stores.preview(stale, document_store=None)

    assert outcome is not None
    assert outcome["status"] == REFUSED_REDACTED


def test_an_earlier_node_version_names_a_source_text_title(stores: _Stores) -> None:
    """The versions count as well as the events: with no log to read, a title
    an earlier version held (marked ``source_text``) refuses a reviewer's
    payload that carries it again."""
    stores.redact_source()
    candidate = _regenerated(stores)
    entity_id = stores.payloads(candidate)["entity_payload"]["entity_id"]
    props = {"intent_family": "asset_generation", "source_item_id": SOURCE_ID}
    stores.graph.upsert_node(
        entity_id,
        "precedent",
        {**props, "name": NAME_TEXT, "name_source": NAME_SOURCE_SOURCE_TEXT},
    )
    stores.graph.upsert_node(
        entity_id,
        "precedent",
        {
            **props,
            "name": "Learning: asset_generation :: later",
            "name_source": "item_id",
        },
    )

    refused = stores.preview(candidate, NAME_TEXT, event_log=None)
    written = stores.preview(candidate, "A different title", event_log=None)

    assert refused is not None
    assert refused["status"] == REFUSED_REDACTED
    assert written is None or not promotion_is_refusal(written)


# --- #1032: a re-ingested source, an edited stale name, an item title ---------

FRESH_TEXT = "Cobalt lantern first line of the rewritten source"


def _redact_id(stores: _Stores, target_id: str) -> None:
    result = stores.executor.execute(
        Command(
            operation=Operation.REDACTION_APPLY,
            args={"target_id": target_id, "reason": "owner request"},
            target_id=target_id,
            target_type="entity",
            requested_by="test",
        )
    )
    assert result.status == CommandStatus.SUCCESS, result.message


def _re_ingest(
    stores: _Stores, item_id: str = SOURCE_ID, first_line: str = FRESH_TEXT
) -> None:
    """Write the redacted id again with new content: ``put_document`` does not
    look for a tombstone, so this is all a re-ingest is."""
    stores.documents.put(item_id, f"{first_line}\n\nNewly written body text", {})


def _named_from_current(stores: _Stores, item_id: str = SOURCE_ID) -> dict[str, Any]:
    """The candidate as the analyzer writes it from the source's current text."""
    candidate = _candidate(item_id)
    _apply_readable_name_fallback(candidate, stores.documents)
    assert candidate["precedent_name_source"] == NAME_SOURCE_SOURCE_TEXT
    assert FRESH_TEXT in candidate["precedent_name"]
    return candidate


def test_a_name_from_the_current_content_promotes_after_a_re_ingest(
    stores: _Stores,
) -> None:
    stores.redact_source()
    _re_ingest(stores)
    candidate = _named_from_current(stores)
    entity_id = precedent_entity_id_for_candidate(candidate["candidate_id"])

    assert stores.preview(candidate) is None
    outcome = stores.submit(candidate)

    assert outcome["status"] == "promoted", outcome
    node = stores.graph.get_node(entity_id)
    assert node is not None
    assert FRESH_TEXT in node["properties"]["name"]
    assert NAME_TEXT not in stores.everything_stored(entity_id)
    # And the regenerated candidate is idempotent: the refusal's own remedy
    # (regenerate) lands on the same name every time.
    assert stores.submit(copy.deepcopy(candidate))["status"] == "already_promoted"


def test_a_name_taken_after_a_re_ingest_is_stale_once_the_source_is_redacted_again(
    stores: _Stores,
) -> None:
    """The cutoff is the latest redaction, not the first."""
    stores.redact_source()
    _re_ingest(stores)
    first = _named_from_current(stores)
    assert stores.submit(first)["status"] == "promoted"
    entity_id = precedent_entity_id_for_candidate(first["candidate_id"])
    # The first redaction deleted the source's graph node, which a second one
    # needs to find.
    stores.graph.upsert_node(SOURCE_ID, "CreativeWork", {"name": SOURCE_ID})
    _redact_id(stores, SOURCE_ID)
    later_text = "Quartz harbor opening of the second rewrite"
    _re_ingest(stores, first_line=later_text)

    stale = stores.submit(copy.deepcopy(first))
    fresh_candidate = _candidate(SOURCE_ID)
    _apply_readable_name_fallback(fresh_candidate, stores.documents)
    fresh = stores.submit(fresh_candidate)

    assert stale["status"] == REFUSED_REDACTED
    assert FRESH_TEXT not in stores.everything_stored(entity_id)
    assert fresh["status"] == "promoted", fresh
    assert later_text in stores.graph.get_node(entity_id)["properties"]["name"]


def test_a_scrubbed_precedent_takes_a_current_content_name_after_a_re_ingest(
    stores: _Stores,
) -> None:
    """The node still carries its scrub markers (``neutral``, ``source_redacted``),
    which are not what refuses once the source reads as written again."""
    entity_id, _ = _promoted_then_source_redacted(stores)
    _re_ingest(stores)

    outcome = stores.submit(_named_from_current(stores))

    assert outcome["status"] == "promoted", outcome
    assert FRESH_TEXT in stores.graph.get_node(entity_id)["properties"]["name"]
    assert NAME_TEXT not in stores.everything_stored(entity_id)


def test_a_reviewer_title_promotes_after_a_re_ingest(stores: _Stores) -> None:
    """A candidate named from source text keeps that marker even when its name
    is edited, so only the source's state tells this title from a stale one."""
    stores.redact_source()
    _re_ingest(stores)
    title = "Reuse the rewritten opening"

    candidate = _named_from_current(stores)

    outcome = stores.submit_planned(candidate, title)

    assert outcome["status"] == "promoted", outcome
    node = stores.graph.get_node(
        precedent_entity_id_for_candidate(candidate["candidate_id"])
    )
    assert node["properties"]["name"] == title


def test_a_name_from_before_the_redaction_is_still_refused_after_a_re_ingest(
    stores: _Stores,
) -> None:
    entity_id, stale = _promoted_then_source_redacted(stores)
    _re_ingest(stores)
    history_before = stores.graph.get_node_history(entity_id)

    by_candidate = stores.submit(stale)
    by_decision = stores.submit_planned(
        _named_from_current(stores), stale["precedent_name"]
    )

    assert by_candidate["status"] == REFUSED_REDACTED
    assert by_decision["status"] == REFUSED_REDACTED
    assert stores.graph.get_node_history(entity_id) == history_before
    assert NAME_TEXT not in stores.everything_stored(entity_id)
    assert NAME_TEXT not in by_decision["message"]


def test_a_redaction_event_with_no_row_and_no_re_ingest_still_refuses(
    stores: _Stores,
) -> None:
    """The event is the only trace: the id never had a document row to blank,
    so there is no live row either, and the first test applies."""
    stores.graph.upsert_node("doc-gone", "CreativeWork", {"name": "doc-gone"})
    _redact_id(stores, "doc-gone")
    assert stores.documents.get("doc-gone") is None
    stale = _candidate("doc-gone")
    stale["precedent_name"] = NAME_TEXT
    stale["precedent_name_source"] = NAME_SOURCE_SOURCE_TEXT

    outcome = stores.preview(stale)

    assert outcome is not None
    assert outcome["status"] == REFUSED_REDACTED


def test_a_chunk_row_beside_a_parent_that_is_still_blank_is_not_a_re_ingest(
    stores: _Stores,
) -> None:
    chunk_id = f"{SOURCE_ID}#chunk-0"
    stores.documents.put(chunk_id, f"{NAME_TEXT}\n\nchunk body", {})
    stale = _candidate(chunk_id)
    stale["precedent_name"] = NAME_TEXT
    stale["precedent_name_source"] = NAME_SOURCE_SOURCE_TEXT
    stores.redact_source()
    _re_ingest(stores, chunk_id)

    outcome = stores.preview(stale)

    assert outcome is not None
    assert outcome["status"] == REFUSED_REDACTED


def test_a_chunk_promotes_once_its_parent_is_written_again(stores: _Stores) -> None:
    chunk_id = f"{SOURCE_ID}#chunk-0"
    stores.documents.put(chunk_id, f"{NAME_TEXT}\n\nchunk body", {})
    stores.redact_source()
    _re_ingest(stores, chunk_id)
    _re_ingest(stores)

    assert stores.preview(_named_from_current(stores, chunk_id)) is None


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda n: n + " (revised)", id="suffix"),
        pytest.param(
            lambda n: f"Reviewer note: {n.split(' :: ')[1].upper()}", id="prefix"
        ),
        pytest.param(lambda n: n.replace(" ", "  "), id="spacing"),
    ],
)
def test_an_edited_stale_name_with_a_regenerated_candidate_is_refused(
    stores: _Stores, edit: Any
) -> None:
    """Neither exact equality nor the candidate's marker sees this one: the
    regenerated candidate is not source-named, and the edited name was never
    promoted itself. It carries the earlier name's text all the same."""
    entity_id, stale = _promoted_then_source_redacted(stores)

    outcome = stores.submit_planned(_regenerated(stores), edit(stale["precedent_name"]))

    assert outcome["status"] == REFUSED_REDACTED
    assert NAME_TEXT not in stores.everything_stored(entity_id)
    assert NAME_TEXT not in outcome["message"]


def test_a_short_earlier_name_does_not_refuse_an_unrelated_title(
    stores: _Stores,
) -> None:
    stores.documents.put(SOURCE_ID, "Hi\n\nshort opening body", {})
    first = _candidate()
    _apply_readable_name_fallback(first, stores.documents)
    assert first["precedent_name"].endswith(" :: Hi")
    entity_id = precedent_entity_id_for_candidate(first["candidate_id"])
    assert stores.submit(first)["status"] == "promoted"
    stores.redact_source()

    unrelated = stores.submit_planned(_regenerated(stores), "Hi there, a title of mine")
    assert unrelated["status"] == "promoted", unrelated
    assert stores.graph.get_node(entity_id)["properties"]["name_source"] == "reviewer"

    # Equality is still exact, whatever the length.
    exact = stores.submit_planned(_regenerated(stores), first["precedent_name"])
    assert exact["status"] == REFUSED_REDACTED


@pytest.mark.parametrize(
    ("name", "earlier", "refused"),
    [
        (
            "Learning: f :: Quartz heron opening",
            {"Learning: f :: Quartz heron opening"},
            True,
        ),
        ("my QUARTZ  heron opening!", {"Learning: f :: Quartz heron opening"}, True),
        ("Anything at all", {"Learning: f :: "}, False),
        ("Anything at all", {""}, False),
        ("a b c d e f g", {"Learning: f :: abcdefg"}, False),
        ("Learning: f :: Hi", {"Learning: f :: Hi"}, True),
        ("Hi there", {"Learning: f :: Hi"}, False),
        ("see seven7x here", {"Learning: f :: seven7x"}, False),
        ("see eight88x here", {"Learning: f :: eight88x"}, True),
        ("Quartz heron opening", set(), False),
        (
            "Reviewer: Quartz heron opening line, finished",
            {"Learning: f :: Quartz heron opening line…"},
            True,
        ),
        (
            "Reviewer: Quartz heron opening line, finished",
            {"Learning: f :: Quartz heron opening line ..."},
            True,
        ),
        (
            "Reviewer: Quartz heron opening line, finished",
            {"Learning: f :: Quartz heron opening line..."},
            True,
        ),
        (
            "Quartz heron opening line…",
            {"Learning: f :: Quartz heron opening line…"},
            True,
        ),
        ("Quartz", {"Learning: f :: Quartz heron…"}, False),
        ("Anything at all", {"Learning: f :: …"}, False),
        ("Anything at all", {"…"}, False),
    ],
)
def test_restores_earlier_name(name: str, earlier: set[str], refused: bool) -> None:
    assert _restores_earlier_name(name, earlier) is refused


def _item_titled(title: str) -> dict[str, Any]:
    candidate = _candidate()
    candidate.update(
        title=title,
        precedent_name=f"Learning: asset_generation :: {title}",
        precedent_name_source="item_title",
    )
    return candidate


def test_an_item_title_name_is_refused_over_a_redacted_source(
    stores: _Stores,
) -> None:
    """The redaction blanks the document row's title, so a name copied from the
    item's title is the redacted document's text; a reviewer's own title is not."""
    stores.redact_source()
    titled = _item_titled("Harbor ledger title of the source")

    refused = stores.preview(titled)
    assert refused is not None
    assert refused["status"] == REFUSED_REDACTED
    assert "Harbor ledger" not in refused["message"]
    assert stores.preview(titled, "Reuse the approved opening") is None


def test_an_earlier_item_title_name_is_refused_after_a_re_ingest(
    stores: _Stores,
) -> None:
    title = "Harbor ledger title of the source"
    titled = _item_titled(title)
    entity_id = precedent_entity_id_for_candidate(titled["candidate_id"])
    assert stores.submit(titled)["status"] == "promoted"
    stores.redact_source()
    _re_ingest(stores)
    node = stores.graph.get_node(entity_id)
    assert node["properties"]["name_source"] == NAME_SOURCE_NEUTRAL
    assert title not in stores.everything_stored(entity_id)

    outcome = stores.submit(copy.deepcopy(titled))
    assert outcome["status"] == REFUSED_REDACTED

    assert stores.submit(titled, "Reuse the approved opening")["status"] == "promoted"
    assert title not in stores.everything_stored(entity_id)


def test_the_refusal_names_the_id_and_is_true_with_no_precedent_to_scrub(
    stores: _Stores,
) -> None:
    stale = stores.named_from_source()
    stores.redact_source()
    entity_id = precedent_entity_id_for_candidate(stale["candidate_id"])
    assert stores.graph.get_node(entity_id) is None

    message = stores.submit(stale)["message"]

    assert entity_id in message
    assert "its source was redacted" in message
    assert "scrubbed" not in message
    assert NAME_TEXT not in message


# --- The listing serves a row per promotion, not per node ---------------------


@pytest.mark.parametrize(
    "reviewer_title",
    [None, "Reuse the rewritten opening"],
    ids=["current-content-name", "reviewer-title"],
)
def test_the_listing_does_not_re_serve_an_older_events_text_after_a_re_ingest(
    stores: _Stores, reviewer_title: str | None
) -> None:
    """Promote with a source-text name, redact, write the id again, promote again.

    The node now reads ``included`` over the new content, but the older
    ``PRECEDENT_PROMOTED`` event still holds the old title and excerpt, and the
    log is immutable. A node vouches only for the event whose text it holds."""
    entity_id, _ = _promoted_then_source_redacted(stores)
    first_event, *_ = stores.events.get_events(
        event_type=EventType.PRECEDENT_PROMOTED, entity_id=entity_id, order="asc"
    )
    assert NAME_TEXT in first_event.payload["title"]
    assert COPIED_TEXT in first_event.payload["description"]
    _re_ingest(stores)
    candidate = _named_from_current(stores)
    outcome = (
        stores.submit(candidate)
        if reviewer_title is None
        else stores.submit_planned(candidate, reviewer_title)
    )
    assert outcome["status"] == "promoted", outcome
    node = stores.graph.get_node(entity_id)
    assert node is not None
    assert node["properties"]["source_excerpt_status"] == SOURCE_EXCERPT_INCLUDED
    second_event = next(
        e
        for e in stores.events.get_events(
            event_type=EventType.PRECEDENT_PROMOTED, entity_id=entity_id, order="asc"
        )
        if e.event_id != first_event.event_id
    )

    rows = {
        r["event_id"]: r
        for r in list_precedents(stores.events, graph_store=stores.graph)
    }

    assert set(rows) == {first_event.event_id, second_event.event_id}
    assert NAME_TEXT not in repr(rows)
    assert COPIED_TEXT not in repr(rows)
    older = rows[first_event.event_id]
    assert older["title"] == neutral_precedent_name(entity_id, "asset_generation")
    assert "Source excerpt" not in older["description"]
    # The new event is served as promoted: its own title and its own excerpt.
    newer = rows[second_event.event_id]
    assert newer["title"] == node["properties"]["name"]
    assert newer["title"] == (reviewer_title or newer["title"])
    assert FRESH_TEXT in newer["title" if reviewer_title is None else "description"]
    assert "Newly written body text" in newer["description"]


# --- #1032 review: boundaries of the earlier-name window, a truncated name -----


class _VersionedNode:
    """A graph store that holds one node, with the given version stamps."""

    def __init__(self, stamps: list[Any]) -> None:
        self._stamps = stamps

    def get_node_history(self, entity_id: str) -> list[dict[str, Any]]:
        return [
            {
                "valid_from": stamp,
                "properties": {
                    "name": f"Learning: f :: Name at {stamp}",
                    "name_source": NAME_SOURCE_SOURCE_TEXT,
                },
            }
            for stamp in self._stamps
        ]


def _names_before(stamps: list[Any], before: datetime) -> set[str]:
    return _earlier_source_names(
        {"entity_id": "prec-x", "properties": {}},
        graph_store=_VersionedNode(stamps),  # type: ignore[arg-type]
        event_log=None,
        before=before,
    )


def test_a_version_stamped_at_the_redaction_time_counts_as_earlier() -> None:
    """``before`` is inclusive: a scrub and a promotion can share a timestamp,
    and the only safe reading is that the name came first."""
    redacted_at = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
    at = redacted_at.isoformat()
    after = (redacted_at + timedelta(microseconds=1)).isoformat()
    before = (redacted_at - timedelta(microseconds=1)).isoformat()

    names = _names_before([before, at, after], redacted_at)

    assert names == {f"Learning: f :: Name at {before}", f"Learning: f :: Name at {at}"}


def test_a_version_with_an_unreadable_timestamp_counts_as_earlier() -> None:
    """Fail closed: a version whose time cannot be read might be from before the
    redaction, so its name stays refused."""
    redacted_at = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)

    names = _names_before(
        ["not a timestamp", None, "2030-01-01T00:00:00+00:00"], redacted_at
    )

    assert names == {
        "Learning: f :: Name at not a timestamp",
        "Learning: f :: Name at None",
    }


def test_a_truncated_stale_name_the_reviewer_completed_is_still_refused(
    stores: _Stores,
) -> None:
    """A name derived from a long first line is cut with an ellipsis; a reviewer
    who finishes the sentence has still carried the text the redaction removed."""
    long_line = "Quartz heron opening line that runs on well past the cap " * 3
    stores.documents.put(SOURCE_ID, f"{long_line}\n\nbody", {})
    first = _candidate()
    _apply_readable_name_fallback(first, stores.documents)
    truncated = first["precedent_name"]
    assert truncated.endswith("…")
    assert stores.submit(first)["status"] == "promoted"
    stores.redact_source()
    kept = truncated.partition(" :: ")[2].removesuffix("…").rstrip()
    completed = f"Reviewer: {kept} and then some words the reviewer added"

    outcome = stores.submit_planned(_regenerated(stores), completed)

    assert outcome["status"] == REFUSED_REDACTED
