"""MCP ``execute_mutation`` honours the bearer's clearance (#194, #1023)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.test_sensitivity_surfaces import as_http, as_stdio
from tests.unit.retrieve.test_sensitivity import label
from trellis.core.hashing import content_hash
from trellis.schemas.classification import LIFECYCLE_KEY, sensitivity_of

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

execute_mutation = unwrap_tool(server_mod.execute_mutation)
save_memory = unwrap_tool(server_mod.save_memory)
save_knowledge = unwrap_tool(server_mod.save_knowledge)

BODY = "failover runbook zebra ledger: rotate the vault seal keys. " * 4
OPEN = "unrelated planting text about tide charts and lighthouse lamps. " * 4
MUTATE = ["mutate"]
MUTATE_RESTRICTED = ["mutate", "restricted"]


def _ingest(doc_id: str, level: str | None = None) -> dict[str, Any]:
    return {
        "evidence": {
            "doc_id": doc_id,
            "content": "replacement",
            "metadata": label(level) if level else {},
        }
    }


def _run(operation: str, args: dict[str, Any]) -> dict[str, Any]:
    return json.loads(execute_mutation(operation=operation, args=args))


def _level(registry: StoreRegistry, doc_id: str) -> str:
    row = registry.knowledge.document_store.get(doc_id)
    assert row is not None
    return sensitivity_of(row["metadata"])


class TestEvidenceIngest:
    def test_a_token_below_the_label_cannot_overwrite(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put("doc", BODY, label("restricted"))
        with as_http(MUTATE):
            out = _run("evidence.ingest", _ingest("doc"))
        assert out["status"] == "rejected"
        stored = temp_registry.knowledge.document_store.get("doc")
        assert stored is not None
        assert stored["content"] == BODY
        assert _level(temp_registry, "doc") == "restricted"

    def test_a_token_below_the_label_cannot_overwrite_at_the_same_label(
        self, temp_registry: StoreRegistry
    ) -> None:
        """No downgrade here: refused because the label is out of reach."""
        temp_registry.knowledge.document_store.put("doc", BODY, label("confidential"))
        with as_http(MUTATE):
            out = _run("evidence.ingest", _ingest("doc", "confidential"))
        assert out["status"] == "rejected"
        stored = temp_registry.knowledge.document_store.get("doc")
        assert stored is not None
        assert stored["content"] == BODY

    def test_a_cleared_token_cannot_lower_the_label(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put("doc", BODY, label("restricted"))
        with as_http(MUTATE_RESTRICTED):
            out = _run("evidence.ingest", _ingest("doc", "confidential"))
        assert out["status"] == "rejected"
        assert _level(temp_registry, "doc") == "restricted"

    @pytest.mark.parametrize("written", ["confidential", "restricted"])
    def test_a_cleared_token_may_rewrite_at_the_same_or_a_higher_label(
        self, temp_registry: StoreRegistry, written: str
    ) -> None:
        temp_registry.knowledge.document_store.put("doc", BODY, label("confidential"))
        with as_http(MUTATE_RESTRICTED):
            out = _run("evidence.ingest", _ingest("doc", written))
        assert out["status"] == "success", out
        assert _level(temp_registry, "doc") == written

    def test_a_mutate_key_cannot_raise_a_label_above_its_own_clearance(
        self, temp_registry: StoreRegistry
    ) -> None:
        """One rule: ``classification.set`` and ``evidence.ingest`` agree."""
        temp_registry.knowledge.document_store.put("doc", BODY, {"title": "t"})
        with as_http(MUTATE):
            relabel = _run(
                "classification.set", {"doc_id": "doc", "sensitivity": "restricted"}
            )
            ingest = _run(
                "evidence.ingest",
                {
                    "evidence": {
                        "doc_id": "doc",
                        "content": BODY,
                        "metadata": label("restricted"),
                    }
                },
            )
        assert relabel["status"] == "rejected"
        assert ingest["status"] == "rejected"
        assert ingest["message"] == relabel["message"]
        assert _level(temp_registry, "doc") == "internal"

    def test_a_mutate_key_cannot_create_a_document_above_its_clearance(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_http(MUTATE):
            out = _run("evidence.ingest", _ingest("fresh", "restricted"))
        assert out["status"] == "rejected"
        assert temp_registry.knowledge.document_store.get("fresh") is None

    def test_a_restricted_mutate_key_may_create_one(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_http(MUTATE_RESTRICTED):
            out = _run("evidence.ingest", _ingest("fresh", "restricted"))
        assert out["status"] == "success", out
        assert _level(temp_registry, "fresh") == "restricted"

    def test_a_trusted_stdio_caller_still_may_not_lower_a_label(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put("doc", BODY, label("restricted"))
        with as_stdio():
            out = _run("evidence.ingest", _ingest("doc"))
        assert out["status"] == "rejected"
        assert _level(temp_registry, "doc") == "restricted"


class TestClassificationSet:
    def test_a_token_without_restricted_cannot_relabel_a_restricted_doc(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put("doc", BODY, label("restricted"))
        with as_http(["mutate", "confidential"]):
            out = _run(
                "classification.set", {"doc_id": "doc", "sensitivity": "internal"}
            )
        assert out["status"] == "rejected"
        assert _level(temp_registry, "doc") == "restricted"

    def test_a_document_above_the_token_answers_as_a_missing_one(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put(
            "hr/layoffs.md", BODY, label("restricted")
        )
        with as_http(MUTATE):
            present = _run(
                "classification.set",
                {"doc_id": "hr/layoffs.md", "sensitivity": "internal"},
            )
            missing = _run(
                "classification.set",
                {"doc_id": "hr/nothing.md", "sensitivity": "internal"},
            )
        assert present["status"] == missing["status"] == "rejected"
        assert present.get("rejection_reason") == missing.get("rejection_reason")
        assert present["message"].replace("layoffs", "nothing") == missing["message"]
        assert _level(temp_registry, "hr/layoffs.md") == "restricted"

    def test_a_restricted_token_relabels_and_a_read_token_loses_the_document(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put("doc", BODY, {"title": "t"})
        get_context = unwrap_tool(server_mod.get_context)
        with as_http(["read"]):
            assert "zebra ledger" in get_context("failover runbook")
        with as_http(MUTATE_RESTRICTED):
            out = _run(
                "classification.set", {"doc_id": "doc", "sensitivity": "restricted"}
            )
        assert out["status"] == "success", out
        with as_http(["read"]):
            assert "zebra ledger" not in get_context("failover runbook")
        with as_http(["read", "restricted"]):
            assert "zebra ledger" in get_context("failover runbook")


def _same_but_for_the_id(a: str, b: str, first: str, second: str) -> bool:
    return a.replace(first, second) == b


class TestSupersessionHonoursClearance:
    """``supersedes=`` stamps the target, so it is a write to that document."""

    def test_save_memory_cannot_stamp_a_document_above_the_key(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put("secret", BODY, label("restricted"))
        before = temp_registry.knowledge.document_store.get("secret")
        with as_http(["ingest"]), pytest.raises(Exception, match="supersedes") as err:
            save_memory(content=OPEN, supersedes="secret")
        assert "existing document" in str(err.value)
        assert temp_registry.knowledge.document_store.get("secret") == before
        assert LIFECYCLE_KEY not in (before or {}).get("metadata", {})

    def test_save_memory_answers_a_hidden_target_as_a_missing_one(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put("secret", BODY, label("restricted"))
        errors: dict[str, str] = {}
        for target in ("secret", "no-such-doc"):
            with as_http(["ingest"]), pytest.raises(Exception, match="supersedes") as e:
                save_memory(content=OPEN + target, supersedes=target)
            errors[target] = str(e.value)
        assert _same_but_for_the_id(
            errors["secret"], errors["no-such-doc"], "secret", "no-such-doc"
        ), errors
        stored = temp_registry.knowledge.document_store.get_by_hash(
            content_hash(OPEN + "secret")
        )
        assert stored is None, "a refused call stores nothing"

    def test_save_memory_still_supersedes_a_document_within_the_key(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put("old", BODY, label("internal"))
        with as_http(["ingest"]):
            reply = save_memory(content=OPEN, supersedes="old")
        assert "supersedes old" in reply
        row = temp_registry.knowledge.document_store.get("old")
        assert row is not None
        assert LIFECYCLE_KEY in row["metadata"]

    def test_a_restricted_key_may_supersede_a_restricted_document(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put("secret", BODY, label("restricted"))
        with as_http(["ingest", "restricted"]):
            reply = save_memory(content=OPEN, supersedes="secret")
        assert "supersedes secret" in reply
        row = temp_registry.knowledge.document_store.get("secret")
        assert row is not None
        assert LIFECYCLE_KEY in row["metadata"]
        assert _level(temp_registry, "secret") == "restricted", "the label survives"

    def test_save_knowledge_leaves_a_restricted_evidence_document_unstamped(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_stdio():
            created = save_knowledge(
                name="vault seals", content=BODY, sensitivity="restricted"
            )
        node_id = created.split("Entity created: ")[1].split(" ")[0]
        evidence_id = created.split("Evidence document: ")[1].split("\n")[0].strip()
        assert _level(temp_registry, evidence_id) == "restricted"

        with as_http(["mutate", "ingest"]):
            reply = save_knowledge(
                name="vault seals v2", content=OPEN, supersedes=node_id
            )

        row = temp_registry.knowledge.document_store.get(evidence_id)
        assert row is not None
        assert LIFECYCLE_KEY not in row["metadata"]
        assert f"Evidence document {evidence_id} left unchanged (no such document)" in (
            reply
        )

    def test_save_knowledge_stamps_a_restricted_evidence_document_for_a_cleared_key(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_stdio():
            created = save_knowledge(
                name="vault seals", content=BODY, sensitivity="restricted"
            )
        node_id = created.split("Entity created: ")[1].split(" ")[0]
        evidence_id = created.split("Evidence document: ")[1].split("\n")[0].strip()

        with as_http(["mutate", "ingest", "restricted"]):
            save_knowledge(name="vault seals v2", content=OPEN, supersedes=node_id)

        row = temp_registry.knowledge.document_store.get(evidence_id)
        assert row is not None
        assert LIFECYCLE_KEY in row["metadata"]
        assert _level(temp_registry, evidence_id) == "restricted"


class TestSupersessionHelpersHideAboveClearance:
    """The helpers refuse on their own, whatever the tool checked before them."""

    def test_supersede_document_reports_a_hidden_target_as_gone(
        self, temp_registry: StoreRegistry
    ) -> None:
        from trellis.mcp import supersession

        temp_registry.knowledge.document_store.put("secret", BODY, label("restricted"))
        before = temp_registry.knowledge.document_store.get("secret")

        error = supersession.supersede_document(
            temp_registry,
            doc_id="secret",
            successor="newer",
            requested_by="tester",
            clearance="confidential",
        )

        assert error == "document secret no longer exists"
        assert temp_registry.knowledge.document_store.get("secret") == before

    def test_supersede_document_stamps_a_target_within_clearance(
        self, temp_registry: StoreRegistry
    ) -> None:
        from trellis.mcp import supersession

        temp_registry.knowledge.document_store.put("secret", BODY, label("restricted"))
        error = supersession.supersede_document(
            temp_registry,
            doc_id="secret",
            successor="newer",
            requested_by="tester",
            clearance="restricted",
        )
        assert error is None
        row = temp_registry.knowledge.document_store.get("secret")
        assert row is not None
        assert LIFECYCLE_KEY in row["metadata"]
        assert _level(temp_registry, "secret") == "restricted"

    def test_the_stamp_is_refused_by_the_executor_even_past_the_read(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Third layer: were the target read as visible, the handler still refuses
        a stamp over a document labelled above the caller's clearance."""
        from trellis.mcp import supersession

        temp_registry.knowledge.document_store.put("secret", BODY, label("restricted"))
        before = temp_registry.knowledge.document_store.get("secret")
        monkeypatch.setattr(
            supersession,
            "visible_document",
            lambda store, doc_id, *, clearance: store.get(doc_id),
        )

        error = supersession.supersede_document(
            temp_registry,
            doc_id="secret",
            successor="newer",
            requested_by="tester",
            clearance="confidential",
        )

        assert error is not None
        assert "above this credential's clearance" in error
        assert temp_registry.knowledge.document_store.get("secret") == before

    def test_the_plan_leaves_a_hidden_evidence_document_alone(
        self, temp_registry: StoreRegistry
    ) -> None:
        from trellis.mcp import supersession

        temp_registry.knowledge.document_store.put("ev", BODY, label("restricted"))
        node_id = temp_registry.knowledge.graph_store.upsert_node(
            None, "concept", {"name": "n", "evidence_ref": "ev"}
        )
        plan = supersession.plan_entity_supersession(
            temp_registry,
            node_id,
            content=OPEN,
            evidence_ref=None,
            clearance="confidential",
        )
        assert isinstance(plan, supersession.EntityPlan)
        assert plan.t_doc == "ev"
        assert plan.t_doc_skip == "no such document"

        cleared = supersession.plan_entity_supersession(
            temp_registry,
            node_id,
            content=OPEN,
            evidence_ref=None,
            clearance="restricted",
        )
        assert isinstance(cleared, supersession.EntityPlan)
        assert cleared.t_doc_skip is None

    def test_the_plan_does_not_confirm_a_hidden_successor(
        self, temp_registry: StoreRegistry
    ) -> None:
        """A successor above the key reads as absent, whichever way it is named.

        Were it read as present, the plan would refuse with "itself superseded
        by ...", telling a key that cannot read the document that it exists and
        what became of it.
        """
        from trellis.mcp import supersession

        retired = {**label("restricted"), LIFECYCLE_KEY: supersession._stamp("later")}
        temp_registry.knowledge.document_store.put("hid", BODY, retired)
        node_id = temp_registry.knowledge.graph_store.upsert_node(
            None, "concept", {"name": "n"}
        )

        def plan(clearance: Any, **named: Any) -> Any:
            return supersession.plan_entity_supersession(
                temp_registry, node_id, clearance=clearance, **named
            )

        for named in (
            {"content": None, "evidence_ref": "hid"},
            {"content": BODY, "evidence_ref": None},
        ):
            hidden = plan("confidential", **named)
            assert not isinstance(hidden, supersession.Refusal), named
            seen = plan("restricted", **named)
            assert isinstance(seen, supersession.Refusal), named
            assert "itself superseded" in seen.msg


class TestStampLayersHoldWhenThePreCheckIsBypassed:
    """The tools check the target first; the stamp refuses on its own as well.

    The check and the stamp are two reads, so a label raised in between must not
    let the stamp through. Each test removes the pre-check and expects the
    stamp layer to refuse.
    """

    def test_save_memory_new_content(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        temp_registry.knowledge.document_store.put("secret", BODY, label("restricted"))
        monkeypatch.setattr(
            server_mod, "_check_memory_supersedes", lambda *a, **k: None
        )
        with as_http(["ingest"]):
            reply = save_memory(content=OPEN, supersedes="secret")
        assert "supersession NOT applied" in reply
        row = temp_registry.knowledge.document_store.get("secret")
        assert row is not None
        assert LIFECYCLE_KEY not in row["metadata"]

    def test_save_memory_existing_content(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        temp_registry.knowledge.document_store.put("secret", BODY, label("restricted"))
        with as_http(["ingest"]):
            save_memory(content=OPEN)
        monkeypatch.setattr(
            server_mod, "_check_memory_supersedes", lambda *a, **k: None
        )
        with as_http(["ingest"]), pytest.raises(Exception, match="failed to supersede"):
            save_memory(content=OPEN, supersedes="secret")
        row = temp_registry.knowledge.document_store.get("secret")
        assert row is not None
        assert LIFECYCLE_KEY not in row["metadata"]

    def test_save_knowledge_evidence_document(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from trellis.mcp import supersession

        with as_stdio():
            created = save_knowledge(
                name="vault seals", content=BODY, sensitivity="restricted"
            )
        node_id = created.split("Entity created: ")[1].split(" ")[0]
        evidence_id = created.split("Evidence document: ")[1].split("\n")[0].strip()
        monkeypatch.setattr(
            server_mod,
            "_plan_knowledge_supersedes",
            lambda *a, **k: supersession.EntityPlan(t_doc=evidence_id, t_doc_skip=None),
        )
        with as_http(["mutate", "ingest"]), pytest.raises(Exception, match="supersede"):
            save_knowledge(name="vault seals v2", content=OPEN, supersedes=node_id)
        row = temp_registry.knowledge.document_store.get(evidence_id)
        assert row is not None
        assert LIFECYCLE_KEY not in row["metadata"]
