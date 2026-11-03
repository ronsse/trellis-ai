"""``classification.tag_regulatory``: tag a document without changing what it serves.

The handler writes one field, ``DataClassification.regulatory_tags``. The hard
requirement is that no caller's access to the document moves because of it, so
the serving tests here compare every clearance's verdict before and after, and
the refusals (a ``public`` document, a corrupt record) are the guard that keeps
that true for the one label where it would not be.
"""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

import pytest

from tests.unit.retrieve.test_sensitivity import label
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
        """The level it already read; ``classified_by`` records the person."""
        _put(registry, "doc", None)
        assert sensitivity_of(_row(registry, "doc")["metadata"]) == "internal"
        result = _tag(registry, "doc", "pii")
        assert result.status == CommandStatus.SUCCESS, result.message
        assert _record(registry, "doc") == {
            "sensitivity": "internal",
            "regulatory_tags": ["pii"],
            "jurisdiction": [],
            "classified_by": ["caller"],
            "classification_version": "1",
        }
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
        expected = {**prior, "regulatory_tags": ["phi", "pii"]}
        if isinstance(prior.get("classified_by"), list):
            expected["classified_by"] = [
                *prior["classified_by"],
                *([] if "caller" in prior["classified_by"] else ["caller"]),
            ]
        assert new == expected
        for field in ("sensitivity", "jurisdiction", "classification_version"):
            assert json.dumps(new.get(field)) == json.dumps(prior.get(field)), field
        assert _row(registry, "doc")["metadata"]["title"] == "t"

    def test_classified_by_gains_caller_once_as_a_relabel_records_it(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put(
            "doc",
            BODY,
            {
                DATA_CLASSIFICATION_KEY: {
                    "sensitivity": "internal",
                    "classified_by": ["import"],
                }
            },
        )
        _tag(registry, "doc", "pii")
        _tag(registry, "doc", "phi")
        assert _record(registry, "doc")["classified_by"] == ["import", "caller"]


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
