"""The mutation side of the sensitivity floor (#194, #1023).

Each layer is exercised alone, so a test fails when *its* layer is removed
rather than being covered for by the other: handlers run behind an empty gate,
and the Stage-2 floor runs in front of a stub handler that records whether it
was reached.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

import pytest

from tests.unit.retrieve.test_sensitivity import label
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.mcp import supersession
from trellis.mutate import build_curate_executor, ensure_evidence_document
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.handlers import create_curate_handlers
from trellis.mutate.policy_gate import DefaultPolicyGate
from trellis.mutate.policy_source import build_policy_gate
from trellis.mutate.sensitivity_floor import (
    SensitivityFloor,
    refuse_written_label,
    targeted_document_ids,
    visible_document,
)
from trellis.retrieve.sensitivity import OverwriteRefusedError
from trellis.schemas.classification import (
    DATA_CLASSIFICATION_KEY,
    Sensitivity,
    sensitivity_of,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.routes.curate import _execute_command

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

BODY = "rotate the vault seal keys every quarter"


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[StoreRegistry]:
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    yield reg
    reg.close()


def _handlers_only(registry: StoreRegistry, clearance: Sensitivity) -> MutationExecutor:
    """Handlers with the floor's Stage-2 half absent: an empty, plain gate."""
    return MutationExecutor(
        event_log=registry.operational.event_log,
        handlers=create_curate_handlers(
            registry, evidence_embed="none", clearance=clearance
        ),
        policy_gate=DefaultPolicyGate(),
    )


class _Recorder:
    def __init__(self) -> None:
        self.calls = 0

    def handle(self, command: Command) -> tuple[str | None, str]:
        self.calls += 1
        return None, "reached"


def _gate_only(
    registry: StoreRegistry, operation: Operation, clearance: Sensitivity
) -> tuple[MutationExecutor, _Recorder]:
    """Stage 2 with the floor, in front of a handler that only counts."""
    recorder = _Recorder()
    executor = MutationExecutor(
        event_log=registry.operational.event_log,
        handlers={operation: recorder},
        policy_gate=DefaultPolicyGate(
            sensitivity_floor=SensitivityFloor(
                registry.knowledge.document_store, clearance
            )
        ),
    )
    return executor, recorder


def _put(registry: StoreRegistry, doc_id: str, level: str | None) -> None:
    registry.knowledge.document_store.put(
        doc_id, BODY, label(level) if level else {"title": "t"}
    )


def _ingest(
    doc_id: str, level: str | None, content: str = "replacement body"
) -> Command:
    metadata = label(level) if level else {}
    return Command(
        operation=Operation.EVIDENCE_INGEST,
        args={"evidence": {"doc_id": doc_id, "content": content, "metadata": metadata}},
        requested_by="tester",
    )


def _set(doc_id: str, level: str) -> Command:
    return Command(
        operation=Operation.CLASSIFICATION_SET,
        args={"doc_id": doc_id, "sensitivity": level},
        requested_by="tester",
    )


def _stored_level(registry: StoreRegistry, doc_id: str) -> Sensitivity:
    row = registry.knowledge.document_store.get(doc_id)
    assert row is not None
    return sensitivity_of(row["metadata"])


class TestClassificationSetHandler:
    def test_relabels_without_touching_content_or_the_clock(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", None)
        before = registry.knowledge.document_store.get("doc")
        result = _handlers_only(registry, "restricted").execute(
            _set("doc", "restricted")
        )

        assert result.status == CommandStatus.SUCCESS, result.message
        after = registry.knowledge.document_store.get("doc")
        assert after is not None
        assert before is not None
        assert sensitivity_of(after["metadata"]) == "restricted"
        assert after["content"] == before["content"]
        assert after["updated_at"] == before["updated_at"]
        assert after["metadata"]["title"] == "t", "other metadata keys survive"

    def test_keeps_the_other_fields_of_a_readable_record(
        self, registry: StoreRegistry
    ) -> None:
        record = {
            "sensitivity": "confidential",
            "regulatory_tags": ["gdpr"],
            "jurisdiction": ["eu"],
            "classified_by": ["test"],
            "classification_version": "1",
        }
        registry.knowledge.document_store.put(
            "doc", BODY, {DATA_CLASSIFICATION_KEY: record}
        )
        result = _handlers_only(registry, "restricted").execute(
            _set("doc", "restricted")
        )
        assert result.status == CommandStatus.SUCCESS, result.message
        stored = registry.knowledge.document_store.get("doc")
        assert stored is not None
        new = stored["metadata"][DATA_CLASSIFICATION_KEY]
        assert new["sensitivity"] == "restricted"
        assert new["regulatory_tags"] == ["gdpr"]
        assert new["jurisdiction"] == ["eu"]
        assert new["classified_by"] == ["test", "caller"]

    def test_mirrors_the_label_onto_the_vector_row(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", None)
        vectors = registry.knowledge.vector_store
        vectors.upsert(item_id="doc", vector=[0.1, 0.2, 0.3], metadata={"title": "t"})

        _handlers_only(registry, "restricted").execute(_set("doc", "restricted"))

        row = vectors.get("doc")
        assert row is not None
        assert sensitivity_of(row["metadata"]) == "restricted"

    def test_reapplying_the_stored_label_writes_nothing(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", "confidential")
        before = registry.knowledge.document_store.get("doc")
        result = _handlers_only(registry, "restricted").execute(
            _set("doc", "confidential")
        )
        assert result.status == CommandStatus.SUCCESS
        assert "unchanged" in result.message
        assert registry.knowledge.document_store.get("doc") == before

    @pytest.mark.parametrize(
        ("doc_id", "level", "code"),
        [
            ("ghost", "restricted", "target_not_found"),
            ("doc", "top-secret", "classification_set_shape"),
            (chunk_doc_id("doc", 0), "restricted", "classification_set_chunk"),
        ],
        ids=["missing", "outside-vocabulary", "chunk"],
    )
    def test_refuses_a_bad_target_or_value(
        self, registry: StoreRegistry, doc_id: str, level: str, code: str
    ) -> None:
        _put(registry, "doc", None)
        _put(registry, chunk_doc_id("doc", 0), None)
        result = _handlers_only(registry, "restricted").execute(_set(doc_id, level))
        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == code
        assert _stored_level(registry, "doc") == "internal"

    def test_a_caller_below_the_current_label_cannot_relabel(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", "restricted")
        result = _handlers_only(registry, "confidential").execute(
            _set("doc", "internal")
        )
        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == "target_not_found"
        assert _stored_level(registry, "doc") == "restricted"

    def test_a_document_above_clearance_answers_exactly_as_a_missing_one(
        self, registry: StoreRegistry
    ) -> None:
        """No reason, message or event tells the two apart (modulo the id)."""
        _put(registry, "hidden", "restricted")
        executor = _handlers_only(registry, "confidential")

        present = executor.execute(_set("hidden", "internal"))
        missing = executor.execute(_set("absent", "internal"))

        assert present.status == missing.status == CommandStatus.REJECTED
        assert present.metadata["rejection_reason"] == "target_not_found"
        assert present.metadata == missing.metadata
        assert present.message.replace("hidden", "absent") == missing.message
        assert present.warnings == missing.warnings
        events = registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_REJECTED, limit=10
        )
        assert len(events) == 2
        assert {e.payload["message"].replace("hidden", "absent") for e in events} == {
            missing.message
        }

    def test_a_chunk_of_a_parent_above_clearance_also_answers_as_missing(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "parent", "restricted")
        chunk = chunk_doc_id("parent", 0)
        registry.knowledge.document_store.put(chunk, BODY, {})
        result = _handlers_only(registry, "internal").execute(_set(chunk, "internal"))
        assert result.metadata["rejection_reason"] == "target_not_found"

    def test_a_caller_cannot_assign_a_label_above_its_own_clearance(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", None)
        result = _handlers_only(registry, "confidential").execute(
            _set("doc", "restricted")
        )
        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == "policy_violation"
        assert _stored_level(registry, "doc") == "internal"

    def test_a_cleared_caller_may_lower_the_label(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", "confidential")
        result = _handlers_only(registry, "confidential").execute(
            _set("doc", "internal")
        )
        assert result.status == CommandStatus.SUCCESS, result.message
        assert _stored_level(registry, "doc") == "internal"

    def test_a_malformed_record_can_only_be_repaired_by_a_restricted_caller(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put(
            "doc", BODY, {DATA_CLASSIFICATION_KEY: "garbage"}
        )
        low = _handlers_only(registry, "confidential").execute(_set("doc", "internal"))
        assert low.status == CommandStatus.REJECTED
        assert low.metadata["rejection_reason"] == "target_not_found"
        high = _handlers_only(registry, "restricted").execute(_set("doc", "internal"))
        assert high.status == CommandStatus.SUCCESS, high.message
        assert _stored_level(registry, "doc") == "internal"

    def test_refuse_written_label_names_no_label(self) -> None:
        with pytest.raises(OverwriteRefusedError) as refused:
            refuse_written_label("doc", "restricted", clearance="internal")
        assert "restricted" not in str(refused.value)
        assert "internal" not in str(refused.value)

    def test_refuse_written_label_permits_a_label_within_clearance(self) -> None:
        refuse_written_label("doc", "confidential", clearance="confidential")
        refuse_written_label("doc", "internal", clearance="confidential")

    def test_visible_document_hides_above_clearance_and_missing_alike(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "hidden", "restricted")
        _put(registry, "open", "internal")
        store = registry.knowledge.document_store
        assert visible_document(store, "hidden", clearance="confidential") is None
        assert visible_document(store, "absent", clearance="confidential") is None
        assert visible_document(store, "open", clearance="internal") is not None
        assert visible_document(store, "hidden", clearance="restricted") is not None


class TestEvidenceIngestHandlerRefusesOverwrite:
    """#1023: the rule runs in the handler, so no door can skip it."""

    def test_a_caller_below_the_stored_label_cannot_overwrite(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", "restricted")
        result = _handlers_only(registry, "internal").execute(_ingest("doc", None))
        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == "policy_violation"
        stored = registry.knowledge.document_store.get("doc")
        assert stored is not None
        assert stored["content"] == BODY
        assert _stored_level(registry, "doc") == "restricted"

    def test_a_caller_below_the_stored_label_cannot_rewrite_at_the_same_label(
        self, registry: StoreRegistry
    ) -> None:
        """No downgrade here: refused because the label is out of reach."""
        _put(registry, "doc", "confidential")
        result = _handlers_only(registry, "internal").execute(
            _ingest("doc", "confidential")
        )
        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == "policy_violation"
        stored = registry.knowledge.document_store.get("doc")
        assert stored is not None
        assert stored["content"] == BODY

    def test_a_cleared_caller_may_not_lower_the_label(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", "restricted")
        for clearance in ("restricted", "confidential"):
            result = _handlers_only(registry, clearance).execute(  # type: ignore[arg-type]
                _ingest("doc", "confidential")
            )
            assert result.status == CommandStatus.REJECTED, clearance
        assert _stored_level(registry, "doc") == "restricted"

    @pytest.mark.parametrize("written", ["confidential", "restricted"])
    def test_a_cleared_caller_may_rewrite_at_the_same_or_a_higher_label(
        self, registry: StoreRegistry, written: str
    ) -> None:
        _put(registry, "doc", "confidential")
        result = _handlers_only(registry, "restricted").execute(_ingest("doc", written))
        assert result.status == CommandStatus.SUCCESS, result.message
        assert _stored_level(registry, "doc") == written

    def test_the_written_label_cannot_exceed_clearance_on_an_existing_id(
        self, registry: StoreRegistry
    ) -> None:
        """The stored label is within reach, so only the written-label arm refuses."""
        _put(registry, "doc", "internal")
        result = _handlers_only(registry, "confidential").execute(
            _ingest("doc", "restricted")
        )
        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == "policy_violation"
        stored = registry.knowledge.document_store.get("doc")
        assert stored is not None
        assert stored["content"] == BODY
        assert _stored_level(registry, "doc") == "internal"

    def test_the_written_label_cannot_exceed_clearance_on_a_new_id(
        self, registry: StoreRegistry
    ) -> None:
        result = _handlers_only(registry, "confidential").execute(
            _ingest("fresh", "restricted")
        )
        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == "policy_violation"
        assert registry.knowledge.document_store.get("fresh") is None

    def test_a_label_within_clearance_is_written_on_a_new_id(
        self, registry: StoreRegistry
    ) -> None:
        result = _handlers_only(registry, "confidential").execute(
            _ingest("fresh", "confidential")
        )
        assert result.status == CommandStatus.SUCCESS, result.message
        assert _stored_level(registry, "fresh") == "confidential"

    def test_ingest_and_classification_set_refuse_a_label_in_the_same_words(
        self, registry: StoreRegistry
    ) -> None:
        """One rule, one message, whichever operation carries the label."""
        _put(registry, "doc", "internal")
        executor = _handlers_only(registry, "confidential")
        via_ingest = executor.execute(_ingest("doc", "restricted"))
        via_set = executor.execute(_set("doc", "restricted"))
        assert via_ingest.message == via_set.message
        assert via_ingest.message == (
            "this credential's clearance does not cover the label requested "
            "for document 'doc'; nothing was written"
        )

    def test_a_trusted_caller_may_write_any_label(
        self, registry: StoreRegistry
    ) -> None:
        result = _handlers_only(registry, "restricted").execute(
            _ingest("fresh", "restricted")
        )
        assert result.status == CommandStatus.SUCCESS, result.message

    def test_a_new_document_id_is_written(self, registry: StoreRegistry) -> None:
        result = _handlers_only(registry, "internal").execute(_ingest("fresh", None))
        assert result.status == CommandStatus.SUCCESS, result.message

    def test_an_unlabelled_store_is_unchanged_for_the_lowest_clearance(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", None)
        result = _handlers_only(registry, "internal").execute(_ingest("doc", None))
        assert result.status == CommandStatus.SUCCESS, result.message


class TestStageTwoFloor:
    @pytest.mark.parametrize(
        "command",
        [
            _ingest("doc", None),
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={"item_ids": ["doc"], "reason": "r"},
                requested_by="tester",
            ),
            Command(
                operation=Operation.REDACTION_APPLY,
                args={"target_id": "doc", "reason": "r"},
                requested_by="tester",
            ),
            Command(
                operation=Operation.PRECEDENT_DEMOTE,
                args={"target_id": "doc", "reason": "r"},
                requested_by="tester",
            ),
            Command(
                operation=Operation.PRECEDENT_REINSTATE,
                args={"target_id": "doc", "reason": "r"},
                requested_by="tester",
            ),
        ],
        ids=lambda c: str(c.operation),
    )
    def test_a_command_aimed_at_a_document_above_clearance_is_denied(
        self, registry: StoreRegistry, command: Command
    ) -> None:
        _put(registry, "doc", "restricted")
        executor, recorder = _gate_only(registry, command.operation, "confidential")

        result = executor.execute(command)

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == "policy_violation"
        assert recorder.calls == 0, "the handler must never be reached"
        rejected = registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_REJECTED, limit=10
        )
        assert len(rejected) == 1
        assert "restricted" not in result.message

    def test_classification_set_is_left_to_its_handler(
        self, registry: StoreRegistry
    ) -> None:
        """Stage 2 would answer ``policy_violation`` for a present document and
        the handler ``target_not_found`` for a missing one; the handler alone
        decides, so both read as not found."""
        _put(registry, "doc", "restricted")
        assert targeted_document_ids(_set("doc", "internal")) == []
        executor, recorder = _gate_only(
            registry, Operation.CLASSIFICATION_SET, "confidential"
        )
        assert executor.execute(_set("doc", "internal")).status == (
            CommandStatus.SUCCESS
        )
        assert recorder.calls == 1

    def test_a_cleared_caller_reaches_the_handler(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", "restricted")
        executor, recorder = _gate_only(
            registry, Operation.EVIDENCE_INGEST, "restricted"
        )
        assert executor.execute(_ingest("doc", None)).status == CommandStatus.SUCCESS
        assert recorder.calls == 1

    @pytest.mark.parametrize("level", [None, "public", "internal"])
    def test_an_unlabelled_or_open_document_is_not_denied(
        self, registry: StoreRegistry, level: str | None
    ) -> None:
        _put(registry, "doc", level)
        executor, recorder = _gate_only(registry, Operation.EVIDENCE_INGEST, "internal")
        assert executor.execute(_ingest("doc", None)).status == CommandStatus.SUCCESS
        assert recorder.calls == 1

    def test_a_missing_document_is_no_verdict(self, registry: StoreRegistry) -> None:
        executor, recorder = _gate_only(registry, Operation.EVIDENCE_INGEST, "internal")
        assert executor.execute(_ingest("ghost", None)).status == CommandStatus.SUCCESS
        assert recorder.calls == 1

    def test_a_chunk_is_denied_on_its_parents_label(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "parent", "restricted")
        chunk = chunk_doc_id("parent", 0)
        registry.knowledge.document_store.put(chunk, BODY, {})
        command = Command(
            operation=Operation.RETENTION_RESTORE,
            args={"item_ids": [chunk], "reason": "r"},
            requested_by="tester",
        )
        executor, recorder = _gate_only(
            registry, Operation.RETENTION_RESTORE, "internal"
        )
        assert executor.execute(command).status == CommandStatus.REJECTED
        assert recorder.calls == 0

    def test_a_restricted_clearance_never_reads_the_store(self) -> None:
        class Exploding:
            def get(self, doc_id: str) -> Any:
                msg = "a trusted caller must not pay for the floor"
                raise AssertionError(msg)

        floor = SensitivityFloor(Exploding(), "restricted")  # type: ignore[arg-type]
        assert floor.refusal(_ingest("doc", None)) is None

    def test_a_label_read_that_fails_is_not_permission(self) -> None:
        class Down:
            def get(self, doc_id: str) -> Any:
                msg = "store down"
                raise OSError(msg)

        floor = SensitivityFloor(Down(), "internal")  # type: ignore[arg-type]
        with pytest.raises(OSError, match="store down"):
            floor.refusal(_ingest("doc", None))

    def test_operations_that_write_no_caller_named_document_name_no_target(
        self,
    ) -> None:
        for operation, args in (
            (
                Operation.ENTITY_CREATE,
                {"entity_type": "t", "name": "n", "document_ids": ["doc"]},
            ),
            (
                Operation.LINK_CREATE,
                {"source_id": "doc", "target_id": "x", "edge_kind": "k"},
            ),
            (Operation.LABEL_ADD, {"target_id": "doc", "label": "l"}),
        ):
            assert targeted_document_ids(Command(operation=operation, args=args)) == []


PRECEDENT_ID = "precedent://learning/p1"


def _promote(
    target_id: str | None = PRECEDENT_ID, entity_type: str = "precedent"
) -> Command:
    return Command(
        operation=Operation.PRECEDENT_PROMOTE,
        target_id=target_id,
        target_type="precedent",
        args={"title": "t", "description": "d", "entity_type": entity_type},
        requested_by="tester",
    )


def _seed_precedent(registry: StoreRegistry, level: str | None) -> None:
    registry.knowledge.graph_store.upsert_node(
        PRECEDENT_ID, "precedent", {"name": "seal keys", "description": "rotate"}
    )
    _put(registry, PRECEDENT_ID, level)


class TestPrecedentPromoteTargetsItsRow:
    """``precedent.promote`` rewrites the promoted precedent's document row (the
    label is kept, the content is not), so the row is a write target like any
    other."""

    def test_the_learning_path_names_the_precedent_row(self) -> None:
        assert targeted_document_ids(_promote()) == [PRECEDENT_ID]

    @pytest.mark.parametrize(
        "command",
        [
            # A trace-sourced promotion writes no row.
            _promote(entity_type="trace"),
            # Outside the namespace the handler writes no row either.
            _promote(target_id="entity-1"),
            _promote(target_id=None),
        ],
        ids=["trace_sourced", "outside_namespace", "no_target"],
    )
    def test_a_promotion_that_writes_no_row_names_no_target(
        self, command: Command
    ) -> None:
        assert targeted_document_ids(command) == []

    def test_a_caller_below_the_label_is_denied_before_the_handler(
        self, registry: StoreRegistry
    ) -> None:
        _seed_precedent(registry, "restricted")
        executor, recorder = _gate_only(
            registry, Operation.PRECEDENT_PROMOTE, "confidential"
        )

        result = executor.execute(_promote())

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == "policy_violation"
        assert recorder.calls == 0, "the handler must never be reached"
        assert "restricted" not in result.message

    def test_the_label_survives_a_refused_promotion(
        self, registry: StoreRegistry
    ) -> None:
        """End to end, with the real handler behind the real gate."""
        _seed_precedent(registry, "restricted")
        before = registry.knowledge.document_store.get(PRECEDENT_ID)

        result = build_curate_executor(registry, clearance="confidential").execute(
            _promote()
        )

        assert result.status == CommandStatus.REJECTED
        assert registry.knowledge.document_store.get(PRECEDENT_ID) == before
        assert _stored_level(registry, PRECEDENT_ID) == "restricted"
        assert not registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_PROMOTED, limit=10
        )

    def test_a_cleared_caller_may_promote(self, registry: StoreRegistry) -> None:
        _seed_precedent(registry, "restricted")

        result = build_curate_executor(registry, clearance="restricted").execute(
            _promote()
        )

        assert result.status == CommandStatus.SUCCESS, result.message

    def test_an_unlabelled_precedent_is_promoted_at_the_default_clearance(
        self, registry: StoreRegistry
    ) -> None:
        """An unlabelled row reads as ``internal``, the floor of a credentialed key."""
        _seed_precedent(registry, None)

        result = build_curate_executor(registry, clearance="internal").execute(
            _promote()
        )

        assert result.status == CommandStatus.SUCCESS, result.message


class TestClearanceHasNoDefault:
    """A caller that forgets to name its clearance fails at the call.

    A default of ``"restricted"`` would make every surface that never thought
    about the floor run at full clearance, silently. ``PackBuilder.build`` pins
    the same shape for the read side.
    """

    @pytest.mark.parametrize(
        "function",
        [
            build_curate_executor,
            ensure_evidence_document,
            _execute_command,
            supersession.supersede_document,
            supersession.supersede_entity,
            supersession.check_memory_target,
            supersession.plan_entity_supersession,
        ],
        ids=lambda f: f.__name__,
    )
    def test_clearance_is_a_required_keyword(self, function: Any) -> None:
        parameter = inspect.signature(function).parameters["clearance"]
        assert parameter.default is inspect.Parameter.empty
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


class TestDefaultPostureIsTransparent:
    def test_the_default_executor_carries_no_floor(
        self, registry: StoreRegistry
    ) -> None:
        assert build_policy_gate(registry)._sensitivity_floor is None
        assert build_policy_gate(registry, clearance="internal")._sensitivity_floor

    def test_a_labelless_store_gives_every_clearance_the_same_result(
        self, registry: StoreRegistry
    ) -> None:
        _put(registry, "doc", None)
        outcomes = []
        for clearance in ("internal", "confidential", "restricted"):
            executor = build_curate_executor(
                registry,
                evidence_embed="none",
                clearance=clearance,  # type: ignore[arg-type]
            )
            result = executor.execute(_ingest("doc", None, content=f"v-{clearance}"))
            outcomes.append((result.status, result.warnings, result.created_id))
        assert len(set(map(str, outcomes))) == 1
        assert outcomes[0][0] == CommandStatus.SUCCESS


class TestRelabelWithholdsOnBothAxes:
    """Relabelling to ``restricted`` hides the document from a ``read`` caller."""

    def test_keyword_and_semantic_axes_stop_serving_the_document(
        self, registry: StoreRegistry
    ) -> None:
        from trellis.retrieve.pack_builder import PackBuilder
        from trellis.retrieve.strategies import (
            KeywordSearch,
            SemanticSearch,
        )

        text = "failover runbook zebra ledger: rotate the vault seal keys"
        registry.knowledge.document_store.put("doc", text, {"title": "t"})
        vectors = registry.knowledge.vector_store
        vectors.upsert(item_id="doc", vector=[0.1, 0.2, 0.3], metadata={"title": "t"})
        builder = PackBuilder(
            strategies=[
                KeywordSearch(registry.knowledge.document_store),
                SemanticSearch(vectors, lambda _text: [0.1, 0.2, 0.3]),
            ]
        )

        def served(clearance: Sensitivity) -> dict[str, set[str]]:
            pack = builder.build("failover runbook", clearance=clearance)
            by_axis: dict[str, set[str]] = {}
            for item in pack.items:
                by_axis.setdefault(item.item_id, set()).add(item.item_id)
            return by_axis

        assert "doc" in served("internal"), "unlabelled, so served before the relabel"

        result = build_curate_executor(registry, clearance="restricted").execute(
            _set("doc", "restricted")
        )
        assert result.status == CommandStatus.SUCCESS, result.message

        assert "doc" not in served("internal")
        assert "doc" not in served("confidential")
        assert "doc" in served("restricted")
        # The semantic axis reads the vector row's snapshot, which the relabel
        # re-synced; assert it directly so a keyword-only hit cannot hide a stale one.
        row = vectors.get("doc")
        assert row is not None
        assert sensitivity_of(row["metadata"]) == "restricted"
