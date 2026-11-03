"""``classification.tag_regulatory``: tag a document without changing who may read it.

The handler writes one field, ``DataClassification.regulatory_tags``. The hard
requirement is that no caller's access to the document moves because of it, so
the serving tests here compare every clearance's verdict before and after, and
the refusals (a ``public`` document, a corrupt record) are the guard that keeps
that true for the one label where it would not be. It also never claims a person
labelled the document: ``classified_by`` is left alone on a record that exists and
is ``["regulatory-tag"]`` on one it creates, which the label proposer still scans.
"""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

import pytest

from tests.unit.retrieve.test_sensitivity import label
from trellis.classify.label_proposer import scan_for_label_candidates
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandResult, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.handlers import (
    MAX_REGULATORY_TAG_LENGTH,
    MAX_REGULATORY_TAGS,
    create_curate_handlers,
    normalize_regulatory_tags,
)
from trellis.mutate.policy_gate import DefaultPolicyGate
from trellis.retrieve.public_rank import read_public_documents
from trellis.retrieve.sensitivity import DocumentClearance
from trellis.schemas.classification import (
    DATA_CLASSIFICATION_KEY,
    REGULATORY_TAG_CLASSIFIER,
    SENSITIVITY_ORDER,
    Sensitivity,
    is_public_label,
    public_label_record,
    sensitivity_of,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

BODY = "rotate the vault seal keys every quarter"
NON_PUBLIC: tuple[Sensitivity, ...] = ("internal", "confidential", "restricted")


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[StoreRegistry]:
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    yield reg
    reg.close()


def _executor(
    registry: StoreRegistry, clearance: Sensitivity = "restricted"
) -> MutationExecutor:
    """The handler behind an empty, plain gate, so its own refusals are the test."""
    return MutationExecutor(
        event_log=registry.operational.event_log,
        handlers=create_curate_handlers(
            registry, evidence_embed="none", clearance=clearance
        ),
        policy_gate=DefaultPolicyGate(),
    )


def _tag(
    registry: StoreRegistry,
    doc_id: str,
    *tags: str,
    clear: bool = False,
    clearance: Sensitivity = "restricted",
) -> CommandResult:
    return _executor(registry, clearance).execute(
        Command(
            operation=Operation.CLASSIFICATION_TAG_REGULATORY,
            args={"doc_id": doc_id, "tags": list(tags), "clear": clear},
            requested_by="tester",
        )
    )


def _put(registry: StoreRegistry, doc_id: str, level: str | None) -> None:
    registry.knowledge.document_store.put(
        doc_id, BODY, label(level) if level else {"title": "t"}
    )


def _row(registry: StoreRegistry, doc_id: str) -> dict[str, Any]:
    row = registry.knowledge.document_store.get(doc_id)
    assert row is not None
    return row


def _record(registry: StoreRegistry, doc_id: str) -> dict[str, Any]:
    return _row(registry, doc_id)["metadata"][DATA_CLASSIFICATION_KEY]


def _verdicts(registry: StoreRegistry, doc_id: str) -> dict[str, object]:
    """Everything a read gate decides about ``doc_id``, for every clearance."""
    store = registry.knowledge.document_store
    metadata = _row(registry, doc_id)["metadata"]
    return {
        "sensitivity": sensitivity_of(metadata),
        "is_public_label": is_public_label(metadata),
        "permits": {
            level: DocumentClearance(store, level).permits(doc_id, metadata)
            for level in SENSITIVITY_ORDER
        },
        "public_listing": [d["doc_id"] for d in read_public_documents(store)],
    }


class TestNormalisation:
    def test_lowercases_strips_dedupes_and_sorts(self) -> None:
        assert normalize_regulatory_tags(["PHI", " pii ", "Pii", "gdpr"]) == [
            "gdpr",
            "phi",
            "pii",
        ]

    def test_the_vocabulary_is_open(self) -> None:
        assert normalize_regulatory_tags(["pci-dss", "my own tag"]) == [
            "my own tag",
            "pci-dss",
        ]

    @pytest.mark.parametrize(
        "bad",
        [
            "pii",
            None,
            [1],
            [None],
            [""],
            ["   "],
            ["x" * (MAX_REGULATORY_TAG_LENGTH + 1)],
            ["a\x00b"],
            ["a\nb"],
            [f"t{i}" for i in range(MAX_REGULATORY_TAGS + 1)],
        ],
    )
    def test_refuses_what_is_not_a_bounded_list_of_printable_strings(
        self, bad: object
    ) -> None:
        with pytest.raises(ValueError, match=r"tag"):
            normalize_regulatory_tags(bad)

    def test_the_bounds_are_inclusive(self) -> None:
        assert normalize_regulatory_tags(["x" * MAX_REGULATORY_TAG_LENGTH]) != []
        many = [f"t{i:02d}" for i in range(MAX_REGULATORY_TAGS)]
        assert len(normalize_regulatory_tags(many)) == MAX_REGULATORY_TAGS


class TestSetsAndClears:
    def test_sets_normalised_tags_on_an_internal_document(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", "internal")
        result = _tag(registry, "doc", "PHI", "pii", " PII ")
        assert result.status == CommandStatus.SUCCESS, result.message
        assert _record(registry, "doc")["regulatory_tags"] == ["phi", "pii"]
        assert "none -> phi, pii" in result.message

    def test_a_new_set_replaces_the_old_one(self, registry: StoreRegistry) -> None:
        _put(registry, "doc", "confidential")
        _tag(registry, "doc", "pii", "phi")
        result = _tag(registry, "doc", "gdpr")
        assert result.status == CommandStatus.SUCCESS, result.message
        assert _record(registry, "doc")["regulatory_tags"] == ["gdpr"]
        assert "phi, pii -> gdpr" in result.message

    def test_clear_removes_the_set(self, registry: StoreRegistry) -> None:
        _put(registry, "doc", "restricted")
        _tag(registry, "doc", "pii")
        result = _tag(registry, "doc", clear=True)
        assert result.status == CommandStatus.SUCCESS, result.message
        assert _record(registry, "doc")["regulatory_tags"] == []
        assert _record(registry, "doc")["sensitivity"] == "restricted"

    def test_reapplying_the_stored_set_writes_nothing(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", "internal")
        _tag(registry, "doc", "pii", "phi")
        before = _row(registry, "doc")
        result = _tag(registry, "doc", "PHI", "pii")
        assert result.status == CommandStatus.SUCCESS
        assert "unchanged" in result.message
        assert _row(registry, "doc") == before

    def test_clearing_an_untagged_document_writes_nothing(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", "internal")
        before = _row(registry, "doc")
        result = _tag(registry, "doc", clear=True)
        assert result.status == CommandStatus.SUCCESS
        assert "unchanged" in result.message
        assert _row(registry, "doc") == before

    def test_clearing_an_unlabelled_document_creates_no_record(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", None)
        before = _row(registry, "doc")
        result = _tag(registry, "doc", clear=True)
        assert result.status == CommandStatus.SUCCESS
        assert _row(registry, "doc") == before
        assert DATA_CLASSIFICATION_KEY not in _row(registry, "doc")["metadata"]

    def test_an_unlabelled_document_gets_a_record_saying_internal(
        self, registry: StoreRegistry
    ) -> None:
        """The level it already read, and ``classified_by`` names the tag write."""
        _put(registry, "doc", None)
        assert sensitivity_of(_row(registry, "doc")["metadata"]) == "internal"
        result = _tag(registry, "doc", "pii")
        assert result.status == CommandStatus.SUCCESS, result.message
        assert _record(registry, "doc") == {
            "sensitivity": "internal",
            "regulatory_tags": ["pii"],
            "jurisdiction": [],
            "classified_by": [REGULATORY_TAG_CLASSIFIER],
            "classification_version": "1",
        }
        assert REGULATORY_TAG_CLASSIFIER == "regulatory-tag"
        assert _row(registry, "doc")["metadata"]["title"] == "t"

    def test_a_metadata_only_write_keeps_content_and_the_clock(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", "internal")
        before = _row(registry, "doc")
        _tag(registry, "doc", "pii")
        after = _row(registry, "doc")
        assert after["content"] == before["content"]
        assert after["updated_at"] == before["updated_at"]
        assert after["created_at"] == before["created_at"]
        assert after["metadata"].get("title") == before["metadata"].get("title")

    def test_mirrors_the_record_onto_the_vector_row(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", "confidential")
        vectors = registry.knowledge.vector_store
        vectors.upsert(
            item_id="doc",
            vector=[0.1, 0.2, 0.3],
            metadata={"title": "t", **label("confidential")},
        )
        _tag(registry, "doc", "pii")
        row = vectors.get("doc")
        assert row is not None
        assert row["metadata"][DATA_CLASSIFICATION_KEY]["regulatory_tags"] == ["pii"]
        assert sensitivity_of(row["metadata"]) == "confidential"


class TestWritesOnlyTheTagField:
    @pytest.mark.parametrize(
        "prior",
        [
            {
                "sensitivity": "confidential",
                "regulatory_tags": ["gdpr"],
                "jurisdiction": ["eu", "us-ca"],
                "classified_by": ["import", "caller"],
                "classification_version": "1",
            },
            {
                "sensitivity": "internal",
                "regulatory_tags": [],
                "jurisdiction": ["uk"],
                "classified_by": ["import"],
                "classification_version": "2",
                "note": "kept verbatim",
            },
            {"sensitivity": "restricted"},
        ],
        ids=["tagged", "version-2-and-extra-key", "bare"],
    )
    def test_every_other_field_survives_byte_identical(
        self, registry: StoreRegistry, prior: dict[str, Any]
    ) -> None:
        registry.knowledge.document_store.put(
            "doc", BODY, {"title": "t", DATA_CLASSIFICATION_KEY: copy.deepcopy(prior)}
        )
        result = _tag(registry, "doc", "pii", "phi")
        assert result.status == CommandStatus.SUCCESS, result.message

        new = _record(registry, "doc")
        assert new["regulatory_tags"] == ["phi", "pii"]
        assert new == {**prior, "regulatory_tags": ["phi", "pii"]}
        for field in ("sensitivity", "jurisdiction", "classification_version"):
            assert json.dumps(new.get(field)) == json.dumps(prior.get(field)), field
        assert _row(registry, "doc")["metadata"]["title"] == "t"

    @pytest.mark.parametrize(
        "classified_by",
        [
            ["import"],
            ["import", "caller"],
            [],
            [REGULATORY_TAG_CLASSIFIER],
            ["llm_facet", "regulatory-tag"],
        ],
        ids=["import", "import-caller", "empty", "tag-only", "tag-among-others"],
    )
    def test_classified_by_is_left_byte_identical_on_an_existing_record(
        self, registry: StoreRegistry, classified_by: list[str]
    ) -> None:
        """A tag write makes no sensitivity call, so it never says a person did."""
        registry.knowledge.document_store.put(
            "doc",
            BODY,
            {
                DATA_CLASSIFICATION_KEY: {
                    "sensitivity": "internal",
                    "classified_by": list(classified_by),
                }
            },
        )
        _tag(registry, "doc", "pii")
        _tag(registry, "doc", "phi")
        _tag(registry, "doc", clear=True)
        assert json.dumps(_record(registry, "doc")["classified_by"]) == json.dumps(
            classified_by
        )

    def test_a_record_with_no_classified_by_key_gains_none(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put(
            "doc", BODY, {DATA_CLASSIFICATION_KEY: {"sensitivity": "confidential"}}
        )
        _tag(registry, "doc", "pii")
        assert "classified_by" not in _record(registry, "doc")


def _relabel(registry: StoreRegistry, doc_id: str, sensitivity: str) -> CommandResult:
    return _executor(registry).execute(
        Command(
            operation=Operation.CLASSIFICATION_SET,
            args={"doc_id": doc_id, "sensitivity": sensitivity},
            requested_by="tester",
        )
    )


PII_BODY = "contact alice@example.com about the vault seal rotation"


class TestTaggingIsNotLabelling:
    """A tag write must not hide a document from ``label-candidates``."""

    @staticmethod
    def _scan(registry: StoreRegistry) -> dict[str, object]:
        report = scan_for_label_candidates(registry.knowledge.document_store)
        return {
            "documents_flagged": report.documents_flagged,
            "skipped_labelled": report.skipped_labelled,
            "flagged": sorted({f.doc_id for f in report.findings}),
        }

    def test_tagging_an_unlabelled_pii_document_leaves_it_flagged(
        self, registry: StoreRegistry
    ) -> None:
        store = registry.knowledge.document_store
        store.put("doc-open", PII_BODY, {"title": "t"})
        store.put("doc-labelled", PII_BODY, label("internal"))
        before = self._scan(registry)
        assert before == {
            "documents_flagged": 1,
            "skipped_labelled": 1,
            "flagged": ["doc-open"],
        }

        result = _tag(registry, "doc-open", "pii")
        assert result.status == CommandStatus.SUCCESS, result.message
        assert DATA_CLASSIFICATION_KEY in _row(registry, "doc-open")["metadata"]

        assert self._scan(registry) == before

    def test_tagging_a_labelled_document_leaves_it_skipped(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put("doc", PII_BODY, label("internal"))
        before = self._scan(registry)
        _tag(registry, "doc", "pii")
        assert self._scan(registry) == before
        assert before["skipped_labelled"] == 1

    def test_clearing_the_tags_still_leaves_it_flagged(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put("doc", PII_BODY, {"title": "t"})
        before = self._scan(registry)
        _tag(registry, "doc", "pii")
        _tag(registry, "doc", clear=True)
        assert self._scan(registry) == before

    @pytest.mark.parametrize("target", ["confidential", "restricted"])
    def test_a_relabel_afterwards_is_a_persons_call(
        self, registry: StoreRegistry, target: str
    ) -> None:
        """``_relabelled_record`` appends ``caller``; the scan then skips it."""
        registry.knowledge.document_store.put("doc", PII_BODY, {"title": "t"})
        _tag(registry, "doc", "pii", "phi")
        assert _record(registry, "doc")["classified_by"] == [REGULATORY_TAG_CLASSIFIER]
        assert self._scan(registry)["documents_flagged"] == 1

        result = _relabel(registry, "doc", target)

        assert result.status == CommandStatus.SUCCESS, result.message
        record = _record(registry, "doc")
        assert record["sensitivity"] == target
        assert record["regulatory_tags"] == ["phi", "pii"]
        assert record["classified_by"] == [REGULATORY_TAG_CLASSIFIER, "caller"]
        assert self._scan(registry) == {
            "documents_flagged": 0,
            "skipped_labelled": 1,
            "flagged": [],
        }

    def test_confirming_internal_on_a_tag_only_record_is_a_persons_call(
        self, registry: StoreRegistry
    ) -> None:
        """The record states ``internal``, but nobody chose it, so this writes."""
        registry.knowledge.document_store.put("doc", PII_BODY, {"title": "t"})
        _tag(registry, "doc", "pii")
        assert self._scan(registry)["documents_flagged"] == 1

        result = _relabel(registry, "doc", "internal")

        assert result.status == CommandStatus.SUCCESS, result.message
        assert "unchanged" not in result.message
        record = _record(registry, "doc")
        assert record["sensitivity"] == "internal"
        assert record["regulatory_tags"] == ["pii"]
        assert record["classified_by"] == [REGULATORY_TAG_CLASSIFIER, "caller"]
        assert self._scan(registry)["documents_flagged"] == 0

        again = _relabel(registry, "doc", "internal")
        assert again.status == CommandStatus.SUCCESS
        assert "unchanged" in again.message

    def test_a_tagged_document_cannot_be_relabelled_public_until_cleared(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put("doc", PII_BODY, {"title": "t"})
        _tag(registry, "doc", "pii")
        before = _row(registry, "doc")

        refused = _relabel(registry, "doc", "public")
        assert refused.status == CommandStatus.REJECTED
        assert "regulatory_tags" in refused.message
        assert _row(registry, "doc") == before

        assert _tag(registry, "doc", clear=True).status == CommandStatus.SUCCESS
        assert _relabel(registry, "doc", "public").status == CommandStatus.SUCCESS
        assert is_public_label(_row(registry, "doc")["metadata"])


class TestServingIsUnchanged:
    @pytest.mark.parametrize("level", [*NON_PUBLIC, None])
    def test_every_clearance_reads_the_document_as_before(
        self, registry: StoreRegistry, level: str | None
    ) -> None:
        _put(registry, "doc", level)
        _put(registry, "bystander", "internal")
        before = _verdicts(registry, "doc")
        before_other = _verdicts(registry, "bystander")

        result = _tag(registry, "doc", "pii", "phi")
        assert result.status == CommandStatus.SUCCESS, result.message
        assert _verdicts(registry, "doc") == before
        assert _verdicts(registry, "bystander") == before_other
        # And again after a clear: the record that remains reads the same.
        assert _tag(registry, "doc", clear=True).status == CommandStatus.SUCCESS
        assert _verdicts(registry, "doc") == before

    def test_the_stored_label_level_is_the_one_it_had(
        self, registry: StoreRegistry
    ) -> None:
        for level in NON_PUBLIC:
            _put(registry, f"doc-{level}", level)
            _tag(registry, f"doc-{level}", "pii")
            assert _record(registry, f"doc-{level}")["sensitivity"] == level


class TestRefusesWhatCouldChangeServing:
    @pytest.mark.parametrize(
        "record",
        [
            public_label_record(),
            {"sensitivity": "public"},
            {**public_label_record(), "classified_by": ["import"]},
            {**public_label_record(), "regulatory_tags": ["gdpr"]},
        ],
        ids=["exact", "bare", "imported", "already-tagged"],
    )
    def test_refuses_a_public_document_and_writes_nothing(
        self, registry: StoreRegistry, record: dict[str, Any]
    ) -> None:
        registry.knowledge.document_store.put(
            "doc", BODY, {DATA_CLASSIFICATION_KEY: dict(record)}
        )
        before = _row(registry, "doc")
        result = _tag(registry, "doc", "pii")
        assert result.status == CommandStatus.REJECTED
        assert "labelled public" in result.message
        assert "Nothing was written" in result.message
        assert _row(registry, "doc") == before

    def test_a_public_document_is_still_served_to_an_external_key(
        self, registry: StoreRegistry
    ) -> None:
        """The reason for the refusal, asserted end to end."""
        registry.knowledge.document_store.put(
            "doc", BODY, {DATA_CLASSIFICATION_KEY: public_label_record()}
        )
        before = _verdicts(registry, "doc")
        assert before["permits"]["public"] is True  # type: ignore[index]
        result = _tag(registry, "doc", "pii")
        # Serving first: if the refusal is ever removed, this is the line that
        # shows the external key losing the document, not a status mismatch.
        assert _verdicts(registry, "doc") == before
        assert before["public_listing"] == ["doc"]
        assert result.status == CommandStatus.REJECTED

    def test_clearing_a_public_document_is_refused_too(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put(
            "doc", BODY, {DATA_CLASSIFICATION_KEY: public_label_record()}
        )
        assert _tag(registry, "doc", clear=True).status == CommandStatus.REJECTED

    @pytest.mark.parametrize(
        "record",
        ["restricted", ["sensitivity", "internal"], 7, None, True, {}, []],
        ids=["string", "list", "number", "null", "bool", "empty-dict", "empty-list"],
    )
    def test_refuses_a_record_that_is_not_an_object_with_a_level(
        self, registry: StoreRegistry, record: object
    ) -> None:
        registry.knowledge.document_store.put(
            "doc", BODY, {DATA_CLASSIFICATION_KEY: record}
        )
        before = _row(registry, "doc")
        result = _tag(registry, "doc", "pii")
        assert result.status == CommandStatus.REJECTED
        assert "Nothing was written" in result.message
        assert _row(registry, "doc") == before

    @pytest.mark.parametrize(
        "record",
        [{"sensitivity": "top-secret"}, {"sensitivity": 3}, {"classified_by": []}],
        ids=["unknown-level", "non-string-level", "no-level"],
    )
    def test_refuses_a_record_with_no_valid_level(
        self, registry: StoreRegistry, record: dict[str, Any]
    ) -> None:
        registry.knowledge.document_store.put(
            "doc", BODY, {DATA_CLASSIFICATION_KEY: record}
        )
        before = _row(registry, "doc")
        assert _tag(registry, "doc", "pii").status == CommandStatus.REJECTED
        assert _row(registry, "doc") == before

    @pytest.mark.parametrize("raw", ["[1,2]", '"str"', "5", "true"])
    def test_refuses_metadata_that_is_not_an_object(
        self, registry: StoreRegistry, raw: str
    ) -> None:
        docs = registry.knowledge.document_store
        docs.put("doc", BODY, {})
        conn = docs._get_conn()
        conn.execute("UPDATE documents SET metadata_json=? WHERE doc_id='doc'", (raw,))
        conn.commit()
        before = docs.get("doc")
        result = _tag(registry, "doc", "pii")
        assert result.status == CommandStatus.REJECTED
        assert docs.get("doc") == before

    def test_refuses_a_chunk(self, registry: StoreRegistry) -> None:
        _put(registry, "doc", "internal")
        cid = chunk_doc_id("doc", 0)
        _put(registry, cid, "internal")
        before = _row(registry, cid)
        result = _tag(registry, cid, "pii")
        assert result.status == CommandStatus.REJECTED
        assert "chunk" in result.message
        assert _row(registry, cid) == before

    def test_a_missing_document_is_not_found(self, registry: StoreRegistry) -> None:
        result = _tag(registry, "nope", "pii")
        assert result.status == CommandStatus.REJECTED
        assert "not found" in result.message

    def test_a_document_above_the_clearance_answers_as_a_missing_one(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", "confidential")
        before = _row(registry, "doc")
        result = _tag(registry, "doc", "pii", clearance="internal")
        assert result.status == CommandStatus.REJECTED
        assert "not found" in result.message
        assert _row(registry, "doc") == before

    def test_a_document_within_the_clearance_can_be_tagged(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", "confidential")
        result = _tag(registry, "doc", "pii", clearance="confidential")
        assert result.status == CommandStatus.SUCCESS, result.message


class TestArgumentShape:
    def _run(self, registry: StoreRegistry, args: dict[str, Any]) -> CommandResult:
        _put(registry, "doc", "internal")
        return _executor(registry).execute(
            Command(
                operation=Operation.CLASSIFICATION_TAG_REGULATORY,
                args=args,
                requested_by="tester",
            )
        )

    @pytest.mark.parametrize(
        "args",
        [
            {"doc_id": "doc", "tags": []},
            {"doc_id": "doc", "tags": ["pii"], "clear": True},
            {"doc_id": "doc", "tags": "pii"},
            {"doc_id": "doc", "tags": [3]},
            {"doc_id": "doc", "tags": ["pii"], "clear": "yes"},
            {"doc_id": "", "tags": ["pii"]},
            {"doc_id": 5, "tags": ["pii"]},
        ],
        ids=[
            "neither",
            "both",
            "string-tags",
            "non-string-tag",
            "non-bool-clear",
            "empty-id",
            "non-string-id",
        ],
    )
    def test_a_malformed_command_is_rejected_and_writes_nothing(
        self, registry: StoreRegistry, args: dict[str, Any]
    ) -> None:
        result = self._run(registry, args)
        assert result.status == CommandStatus.REJECTED
        assert _record(registry, "doc")["regulatory_tags"] == []

    def test_a_missing_tags_arg_fails_the_operation_schema(
        self, registry: StoreRegistry
    ) -> None:
        result = self._run(registry, {"doc_id": "doc"})
        assert result.status == CommandStatus.REJECTED


class TestThroughTheCuratePipeline:
    def test_runs_behind_the_real_policy_gate_and_audits(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", "internal")
        executor = build_curate_executor(registry, clearance="restricted")
        result = executor.execute(
            Command(
                operation=Operation.CLASSIFICATION_TAG_REGULATORY,
                args={"doc_id": "doc", "tags": ["pii"], "clear": False},
                target_id="doc",
                target_type="document",
                requested_by="tester",
            )
        )
        assert result.status == CommandStatus.SUCCESS, result.message
        assert _record(registry, "doc")["regulatory_tags"] == ["pii"]
        events = registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_EXECUTED, limit=10
        )
        assert any(
            e.payload.get("operation") == "classification.tag_regulatory"
            for e in events
        )
