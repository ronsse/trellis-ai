"""Tests for the memory-path draft policy (#299 / #300)."""

from __future__ import annotations

from trellis.extract.draft_policy import (
    DEFAULT_PARTICIPANT_LABELS,
    apply_memory_draft_policy,
)
from trellis.schemas.extraction import (
    EPISTEMIC_STATUS_MENTIONED,
    EPISTEMIC_STATUS_PROPERTY,
    EXTRACTION_STATUS_PROPERTY,
    EXTRACTION_STATUS_UNCONFIRMED,
    EdgeDraft,
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)

DOC = "conversation:test:abc"


def _result(
    entities: list[EntityDraft],
    edges: list[EdgeDraft] | None = None,
) -> ExtractionResult:
    return ExtractionResult(
        entities=entities,
        edges=edges or [],
        extractor_used="fake",
        tier="llm",
        provenance=ExtractionProvenance(extractor_name="fake"),
    )


class TestParticipantFilter:
    def test_person_speaker_labels_are_dropped(self):
        result = _result(
            [
                EntityDraft(entity_type="Person", name="You"),
                EntityDraft(entity_type="person", name="Claude"),
                EntityDraft(entity_type="Person", name="Mira"),
            ]
        )
        out = apply_memory_draft_policy(result, doc_id=DOC)
        assert [e.name for e in out.entities] == ["Mira"]

    def test_caller_participants_extend_builtin_set(self):
        result = _result(
            [
                EntityDraft(entity_type="Person", name="Nate"),
                EntityDraft(entity_type="Person", name="Mira"),
            ]
        )
        out = apply_memory_draft_policy(result, doc_id=DOC, participant_names=["Nate"])
        assert [e.name for e in out.entities] == ["Mira"]

    def test_matching_is_case_insensitive(self):
        result = _result([EntityDraft(entity_type="PERSON", name="CLAUDE")])
        out = apply_memory_draft_policy(result, doc_id=DOC)
        assert out.entities == []

    def test_non_person_types_named_like_speakers_survive(self):
        # ``Software: Claude`` (the product) is a legitimate entity; only
        # person-shaped drafts are participant-filtered.
        result = _result([EntityDraft(entity_type="Software", name="Claude")])
        out = apply_memory_draft_policy(result, doc_id=DOC)
        assert [e.name for e in out.entities] == ["Claude"]

    def test_edges_referencing_dropped_drafts_are_dropped(self):
        result = _result(
            [
                EntityDraft(entity_type="Person", name="You"),
                EntityDraft(entity_type="Device", name="Garmin Fenix 6"),
            ],
            edges=[
                EdgeDraft(
                    source_id="You", target_id="Garmin Fenix 6", edge_kind="uses"
                ),
                EdgeDraft(
                    source_id=DOC, target_id="Garmin Fenix 6", edge_kind="mentions"
                ),
            ],
        )
        out = apply_memory_draft_policy(result, doc_id=DOC)
        assert [e.edge_kind for e in out.edges] == ["mentions"]

    def test_input_result_is_not_mutated(self):
        original = _result([EntityDraft(entity_type="Person", name="You")])
        apply_memory_draft_policy(original, doc_id=DOC)
        assert len(original.entities) == 1
        assert original.entities[0].properties == {}


class TestFreshMintStamps:
    def test_fresh_mint_gets_doc_link_and_claim_floor(self):
        result = _result([EntityDraft(entity_type="Device", name="Oura ring")])
        out = apply_memory_draft_policy(result, doc_id=DOC)
        (entity,) = out.entities
        assert entity.document_ids == [DOC]
        assert entity.properties[EXTRACTION_STATUS_PROPERTY] == (
            EXTRACTION_STATUS_UNCONFIRMED
        )
        assert entity.properties[EPISTEMIC_STATUS_PROPERTY] == (
            EPISTEMIC_STATUS_MENTIONED
        )

    def test_existing_entity_reference_passes_through_untouched(self):
        # ``entity_id`` set means the draft references an existing node
        # (alias-match doc anchor, resolver hit) — stamping it would open
        # an SCD-2 version that downgrades a confirmed entity.
        draft = EntityDraft(
            entity_id="01EXISTING", entity_type="CreativeWork", name="A memory"
        )
        out = apply_memory_draft_policy(_result([draft]), doc_id=DOC)
        (entity,) = out.entities
        assert entity.document_ids is None
        assert EXTRACTION_STATUS_PROPERTY not in entity.properties
        assert EPISTEMIC_STATUS_PROPERTY not in entity.properties

    def test_explicit_draft_values_are_not_overwritten(self):
        draft = EntityDraft(
            entity_type="Device",
            name="Garmin Fenix 6",
            document_ids=["doc:other"],
            properties={EXTRACTION_STATUS_PROPERTY: "confirmed"},
        )
        out = apply_memory_draft_policy(_result([draft]), doc_id=DOC)
        (entity,) = out.entities
        assert entity.document_ids == ["doc:other"]
        assert entity.properties[EXTRACTION_STATUS_PROPERTY] == "confirmed"
        # The claim floor still fills the field the draft did not set.
        assert entity.properties[EPISTEMIC_STATUS_PROPERTY] == (
            EPISTEMIC_STATUS_MENTIONED
        )

    def test_existing_properties_are_preserved(self):
        draft = EntityDraft(
            entity_type="Device", name="Oura ring", properties={"color": "silver"}
        )
        out = apply_memory_draft_policy(_result([draft]), doc_id=DOC)
        assert out.entities[0].properties["color"] == "silver"


class TestWearablesKnownAnswerFixture:
    """Regression fixture for trellis-ai#300.

    The 2026-08-07 extraction trial ran a conversation *evaluating*
    wearable devices for a project. Correct output asserts
    evaluation-not-ownership: no participant Person nodes, and every
    minted device carries the unconfirmed/mentioned claim floor plus a
    link to the source conversation — so nothing stored can be read as
    "the owner has this device".
    """

    def test_trial_shape_is_contained(self):
        doc = "conversation:claude-ai:b183ecc5"
        raw = _result(
            [
                EntityDraft(entity_type="Person", name="Nate", confidence=0.9),
                EntityDraft(entity_type="Person", name="Claude", confidence=0.9),
                EntityDraft(
                    entity_type="Device", name="Garmin Fenix 6", confidence=0.9
                ),
                EntityDraft(entity_type="Device", name="Oura ring", confidence=0.8),
                EntityDraft(entity_type="Device", name="Whoop strap", confidence=0.9),
                EntityDraft(
                    entity_type="Device", name="Ultrahuman Ring Air", confidence=0.8
                ),
                EntityDraft(
                    entity_type="Software", name="python-garminconnect", confidence=0.9
                ),
            ]
        )
        out = apply_memory_draft_policy(raw, doc_id=doc, participant_names=["Nate"])

        assert not any(e.entity_type.lower() == "person" for e in out.entities)
        assert len(out.entities) == 5
        for entity in out.entities:
            assert entity.document_ids == [doc], entity.name
            assert entity.properties[EXTRACTION_STATUS_PROPERTY] == (
                EXTRACTION_STATUS_UNCONFIRMED
            ), entity.name
            assert entity.properties[EPISTEMIC_STATUS_PROPERTY] == (
                EPISTEMIC_STATUS_MENTIONED
            ), entity.name


class TestDefaultLabels:
    def test_builtin_set_covers_reader_turn_labels(self):
        # The conversation reader renders ``**You:**`` / ``**Claude:**``
        # and ``Unknown`` for unrecognised senders; the builtin set must
        # cover them even when a caller passes no participant_names.
        for label in ("You", "Claude", "Unknown"):
            assert label.lower() in DEFAULT_PARTICIPANT_LABELS
