"""Tests for the DocumentMetadata model and the content_type reconciliation."""

from __future__ import annotations

import pytest

from trellis.retrieve.evaluate import BreadthScorer, EvaluationScenario
from trellis.retrieve.tier_mapping import TierMapper
from trellis.schemas.classification import CONTENT_TYPE_VALUES
from trellis.schemas.document_metadata import DocumentMetadata, document_form_of
from trellis.schemas.pack import Pack, PackItem, SectionRequest


class TestRoundTrip:
    """Valid metadata survives the model unchanged."""

    def test_core_keys_round_trip(self) -> None:
        stored = {
            "title": "Note A",
            "source_system": "obsidian",
            "source_path": "sub/note-a.md",
            "content_tags": {"content_type": "decision", "signal_quality": "high"},
            "auto_importance": 0.62,
            "domain": "personal",
            "chunk_count": 3,
        }
        meta = DocumentMetadata.from_mapping(stored)

        assert meta.title == "Note A"
        assert meta.source_system == "obsidian"
        assert meta.auto_importance == pytest.approx(0.62)
        assert meta.custom == {}
        assert meta.to_metadata() == stored

    def test_chunk_keys_round_trip(self) -> None:
        stored = {
            "source_system": "corpus",
            "source_path": "note.md",
            "parent_doc_id": "corpus:obsidian:abc",
            "chunk_index": 2,
            "chunk_count": 4,
            "char_span": [120, 480],
        }
        assert DocumentMetadata.from_mapping(stored).to_metadata() == stored

    def test_empty_metadata_is_valid(self) -> None:
        assert DocumentMetadata.from_mapping({}).to_metadata() == {}
        assert DocumentMetadata.from_mapping(None).to_metadata() == {}


class TestCustomBag:
    """Unrecognised keys land in ``custom`` — never dropped, never fatal."""

    def test_unknown_key_lands_in_custom(self) -> None:
        stored = {"title": "Note", "tags": ["a", "b"], "sprint": 14}
        meta = DocumentMetadata.from_mapping(stored)

        assert meta.custom == {"tags": ["a", "b"], "sprint": 14}
        assert meta.to_metadata() == stored

    def test_arbitrary_frontmatter_does_not_raise(self) -> None:
        # The markdown handler stamps every frontmatter key top-level; a
        # caller's odd note must not make its document unwritable.
        stored = {"aliases": ["x"], "cssclass": "wide", "rating": 4.5}
        assert DocumentMetadata.from_mapping(stored).to_metadata() == stored

    def test_core_key_with_wrong_type_is_demoted_not_dropped(self) -> None:
        # ``title: 2026`` in YAML frontmatter parses as an int.
        stored = {"title": 2026, "source_system": "obsidian"}
        meta = DocumentMetadata.from_mapping(stored)

        assert meta.title is None
        assert meta.custom == {"title": 2026}
        assert meta.to_metadata() == stored

    def test_custom_keys_are_not_whitespace_stripped(self) -> None:
        # TrellisModel sets str_strip_whitespace, which pydantic applies to
        # dict *keys* too. Renaming a caller's key would break every
        # json_extract filter pointed at it.
        stored = {" spacey ": 1, "title": "  padded  "}
        assert DocumentMetadata.from_mapping(stored).to_metadata() == stored

    def test_non_string_key_does_not_raise(self) -> None:
        # YAML parses a bare `2026:` key as an int. from_mapping is total for
        # JSON-shaped mappings; _apply_record does not wrap it, so a raise
        # here would abort a whole corpus sync after partial writes.
        assert DocumentMetadata.from_mapping({2026: "x"}).to_metadata() == {"2026": "x"}

    def test_explicit_none_core_value_survives(self) -> None:
        # `content_tags: null` gates classify-on-write in sync._apply_record
        # (`"content_tags" not in metadata`); dropping the key at the seam
        # would classify a document deliberately left untagged.
        stored = {"content_tags": None, "title": None}
        assert DocumentMetadata.from_mapping(stored).to_metadata() == stored

    def test_direct_construction_still_forbids_extras(self) -> None:
        # extra="forbid" is the project rule; from_mapping is the only
        # sanctioned way to build one from a stored dict.
        with pytest.raises(ValueError, match="sprint"):
            DocumentMetadata(title="Note", sprint=14)  # type: ignore[call-arg]


class TestContentTypeReconciliation:
    """A foreign flat ``content_type`` is a document *form*, not a facet."""

    def test_foreign_flat_value_becomes_document_form(self) -> None:
        meta = DocumentMetadata.from_mapping({"content_type": "conversation"})

        assert meta.document_form == "conversation"
        assert meta.content_type is None
        assert meta.to_metadata() == {"document_form": "conversation"}

    def test_in_vocabulary_flat_value_stays_a_facet(self) -> None:
        stored = {"content_type": "decision"}
        meta = DocumentMetadata.from_mapping(stored)

        assert meta.content_type == "decision"
        assert meta.document_form is None
        assert meta.to_metadata() == stored

    def test_explicit_document_form_wins_over_foreign_flat_value(self) -> None:
        meta = DocumentMetadata.from_mapping(
            {"document_form": "entity_summary", "content_type": "conversation"}
        )

        assert meta.document_form == "entity_summary"
        assert meta.to_metadata() == {"document_form": "entity_summary"}

    def test_non_string_document_form_is_not_destroyed_by_the_rename(self) -> None:
        # The incumbent wins whatever its type; a non-string one is demoted to
        # `custom` and still round-trips. Only the losing flat content_type is
        # dropped — the model's one documented lossy path.
        meta = DocumentMetadata.from_mapping(
            {"content_type": "conversation", "document_form": 5}
        )

        assert meta.document_form is None
        assert meta.to_metadata() == {"document_form": 5}

    def test_blank_document_form_does_not_block_the_rename(self) -> None:
        meta = DocumentMetadata.from_mapping(
            {"content_type": "conversation", "document_form": "  "}
        )

        assert meta.to_metadata() == {"document_form": "conversation"}

    def test_content_tags_facet_is_untouched(self) -> None:
        stored = {
            "document_form": "conversation",
            "content_tags": {"content_type": "decision"},
        }
        meta = DocumentMetadata.from_mapping(stored)

        assert meta.content_tags == {"content_type": "decision"}
        assert meta.to_metadata() == stored


class TestLegacyDocumentsStillRead:
    """Documents written before the reconciliation keep working."""

    def test_pre_change_conversation_metadata_reads_as_a_form(self) -> None:
        # Exactly what trellis.ingest_corpus.conversations wrote before this
        # change, as it still sits in the store today.
        legacy = {
            "conversation_id": "conv-1",
            "title": "Retirement planning",
            "content_type": "conversation",
            "message_count": 12,
            "source_system": "claude-ai",
            "source_path": "Retirement planning",
        }
        assert document_form_of(legacy) == "conversation"

    def test_post_change_metadata_reads_as_the_same_form(self) -> None:
        current = {"document_form": "conversation", "title": "Retirement planning"}
        assert document_form_of(current) == "conversation"

    def test_legacy_read_ignores_a_real_content_type_facet(self) -> None:
        for value in sorted(CONTENT_TYPE_VALUES):
            assert document_form_of({"content_type": value}) is None

    def test_no_form_at_all(self) -> None:
        assert document_form_of({}) is None
        assert document_form_of(None) is None
        assert document_form_of({"content_type": 7}) is None

    def test_legacy_metadata_round_trips_every_other_key(self) -> None:
        legacy = {
            "conversation_id": "conv-1",
            "title": "Retirement planning",
            "content_type": "conversation",
            "message_count": 12,
            "created_at": "2026-06-01T10:00:00Z",
        }
        migrated = DocumentMetadata.from_mapping(legacy).to_metadata()

        assert migrated["document_form"] == "conversation"
        assert "content_type" not in migrated
        assert {k: v for k, v in migrated.items() if k != "document_form"} == {
            k: v for k, v in legacy.items() if k != "content_type"
        }


class TestLegacyStoredDictsStillScore:
    """Hand-built metadata dicts of both eras score the same.

    The composed property — ingest a document, read it back, score it —
    is pinned end-to-end in
    ``tests/unit/ingest_corpus/test_sync.py::TestMetadataValidationSeam``;
    these are the unit-level halves of it.

    Neither reader is narrowed to the :data:`ContentType` vocabulary: eval
    scenarios legitimately declare ``expected_categories`` outside it (e.g.
    ``"tutorial"``), so a vocabulary guard on the *read* side would change
    scores. The guard belongs on the write side, which is where it now is —
    and because the write side *renames* the key, the read side had to learn
    the new name to keep those scores identical.
    """

    @staticmethod
    def _item(metadata: dict[str, object]) -> PackItem:
        return PackItem(
            item_id="doc-1",
            item_type="document",
            excerpt="text",
            relevance_score=0.5,
            metadata=metadata,
        )

    def test_nested_facet_still_matches_a_section(self) -> None:
        item = self._item({"content_tags": {"content_type": "decision"}})
        match = SectionRequest(name="s", content_types=["decision"])
        miss = SectionRequest(name="s", content_types=["pattern"])

        assert TierMapper().matches_section(item, match)
        assert not TierMapper().matches_section(item, miss)

    @pytest.mark.parametrize(
        "metadata",
        [
            pytest.param({"content_type": "conversation"}, id="legacy-flat-key"),
            pytest.param({"document_form": "conversation"}, id="reconciled-key"),
        ],
    )
    def test_both_eras_score_breadth_identically(
        self, metadata: dict[str, object]
    ) -> None:
        # ``_item_content_type`` falls back to the flat key and then to
        # ``document_form``, so rewriting a document at the ingest seam does
        # not change the categories it contributes.
        scenario = EvaluationScenario(
            name="s", intent="i", expected_categories=["conversation"]
        )
        pack = Pack(pack_id="p", intent="i", items=[self._item(metadata)])

        assert BreadthScorer().score(pack, scenario) == 1.0

    def test_facet_still_wins_over_the_document_form_fallback(self) -> None:
        # The fallback is last: a real content-type facet is what the
        # dimension is about, and provenance must not shadow it.
        item = self._item(
            {
                "content_tags": {"content_type": "decision"},
                "document_form": "conversation",
            }
        )
        scenario = EvaluationScenario(
            name="s", intent="i", expected_categories=["decision", "conversation"]
        )
        pack = Pack(pack_id="p", intent="i", items=[item])

        assert BreadthScorer().score(pack, scenario) == 0.5

    def test_conversation_documents_are_untyped_for_section_filters(self) -> None:
        # True before *and* after: ``TierMapper._get_content_type`` reads
        # ``metadata.get("content_tags", {})`` — the ``{}`` default makes its
        # flat-key fallback unreachable whenever ``content_tags`` is absent,
        # which is exactly the case for a conversation document. So moving the
        # key changes nothing here.
        legacy = self._item({"content_type": "conversation"})
        current = self._item({"document_form": "conversation"})
        section = SectionRequest(name="s", content_types=["decision"])

        assert TierMapper().matches_section(legacy, section)
        assert TierMapper().matches_section(current, section)
