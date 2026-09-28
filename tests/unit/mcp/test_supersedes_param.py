"""MCP ``supersedes=`` on ``save_memory`` / ``save_knowledge`` (#613).

The retrieval gate (``trellis.retrieve.lifecycle.partition_superseded``)
withholds an item whose ``lifecycle`` names a successor present in the same
pool, and until #613 no writer produced that stamp. These tests drive the
real tools, read the pack an agent would be served, and pin every refusal to
"nothing was written" — the message has to say so itself, because an
``McpError`` reaching a client through ``call_tool`` keeps only its text.

``trellis.mcp.supersession`` is imported inside the tests that use it
directly, never at module level, so the control arms still run on a tree
that does not have it.
"""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS

import trellis.mcp.auth as auth_mod
import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.mcp.server import MUTATION_FAILED
from trellis.mutate.policy_source import POLICY_FILENAME
from trellis.retrieve.lifecycle import declared_successor
from trellis.schemas.classification import LIFECYCLE_KEY, Lifecycle
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.registry import StoreRegistry

save_memory = unwrap_tool(server_mod.save_memory)
save_knowledge = unwrap_tool(server_mod.save_knowledge)
get_context = unwrap_tool(server_mod.get_context)
execute_mutation = unwrap_tool(server_mod.execute_mutation)

T_TEXT = (
    "zebrafish calibration: settle window is forty milliseconds per the old bench notes"
)
X_TEXT = (
    "revised zebrafish calibration guidance says the settle window should be "
    "twenty-five milliseconds on the new rig"
)
INTENT = "zebrafish calibration settle window"
NAME = "zebrafish settle window"

#: A one-clause revision: near-duplicate of its original by construction,
#: which is exactly the write ``supersedes=`` exists for.
T7_OLD = (
    "zebrafish tank calibration procedure: warm the rig for ten minutes, zero "
    "the sensor array against the reference cell, record three baseline "
    "sweeps, then set the settle window to forty milliseconds before the "
    "first capture run begins"
)
T7_NEW = T7_OLD.replace("forty milliseconds", "twenty-five milliseconds")

#: Resolved lazily by the registry on first use, so a per-test env var is
#: picked up. The counter is read back through the same dotted path the
#: registry imports, so it is the list the embedder actually appends to.
EMBED_PATH = "tests.unit.mcp.test_supersedes_param._fake_embed"
_EMBED_CALLS: list[str] = []


def _fake_embed(text: str) -> list[float]:
    _EMBED_CALLS.append(text)
    return [1.0, 0.0, 0.5]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stamp(successor: str) -> dict[str, Any]:
    return Lifecycle(state="superseded", superseded_by=successor).model_dump(
        mode="json"
    )


def _doc_id(reply: str) -> str:
    """The id a ``save_memory`` reply ends its first line with."""
    return reply.splitlines()[0].rsplit(": ", 1)[1].strip()


def _entity_ids(reply: str) -> tuple[str, str]:
    node = re.search(r"Entity created: (\S+)", reply)
    doc = re.search(r"Evidence document: (\S+)", reply)
    assert node is not None, reply
    assert doc is not None, reply
    return node.group(1), doc.group(1)


def _meta(reg: StoreRegistry, doc_id: str) -> dict[str, Any]:
    stored = reg.knowledge.document_store.get(doc_id)
    assert stored is not None, doc_id
    return dict(stored["metadata"])


def _props(reg: StoreRegistry, node_id: str) -> dict[str, Any]:
    node = reg.knowledge.graph_store.get_node(node_id)
    assert node is not None, node_id
    return dict(node["properties"])


def _events(reg: StoreRegistry, event_type: EventType) -> list[Event]:
    return reg.operational.event_log.get_events(event_type=event_type, limit=1000)


def _event_ids(reg: StoreRegistry, event_type: EventType) -> set[str]:
    return {e.event_id for e in _events(reg, event_type)}


def _new_events(
    reg: StoreRegistry, event_type: EventType, seen: set[str]
) -> list[Event]:
    return [e for e in _events(reg, event_type) if e.event_id not in seen]


def _mutations_on(
    reg: StoreRegistry, seen: set[str], entity_id: str
) -> list[dict[str, Any]]:
    return [
        e.payload
        for e in _new_events(reg, EventType.MUTATION_EXECUTED, seen)
        if e.entity_id == entity_id
    ]


def _last_pack(reg: StoreRegistry) -> dict[str, Any]:
    events = reg.operational.event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, order="desc", limit=1
    )
    assert events, "no pack was assembled"
    return events[0].payload


def _served(ctx: str, item_id: str) -> bool:
    return f"`{item_id}`" in ctx


def _write_policy(
    reg: StoreRegistry, operation: str, scope: PolicyScope | None = None
) -> None:
    """Deny ``operation``, globally unless ``scope`` narrows it.

    Written after seeding, read per build.
    """
    assert reg.stores_dir is not None
    policy = Policy(
        policy_type=PolicyType.MUTATION,
        scope=scope or PolicyScope(level="global", value=None),
        rules=[PolicyRule(operation=operation, action="deny")],
        enforcement=Enforcement.ENFORCE,
    )
    (reg.stores_dir / POLICY_FILENAME).write_text(
        json.dumps({"policies": [policy.model_dump(mode="json")]}),
        encoding="utf-8",
    )


def _clear_policies(reg: StoreRegistry) -> None:
    assert reg.stores_dir is not None
    (reg.stores_dir / POLICY_FILENAME).unlink()


async def _call(name: str, args: dict[str, Any]) -> str:
    """Call a tool the way a client does: through FastMCP's own dispatch."""
    result = await server_mod.mcp.call_tool(name, args)
    return "".join(getattr(block, "text", "") for block in result.content)


# ---------------------------------------------------------------------------
# save_memory, end to end
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryRun:
    t: str
    x: str
    reply: str
    ctx: str
    withholding: dict[str, Any]
    before: dict[str, Any]
    after: dict[str, Any]
    on_t: list[dict[str, Any]]


async def _memory_run(reg: StoreRegistry, *, declare: bool) -> MemoryRun:
    auth_mod.set_auth_enforced(enforced=False)
    t = _doc_id(await _call("save_memory", {"content": T_TEXT}))
    before = reg.knowledge.document_store.get(t)
    seen = _event_ids(reg, EventType.MUTATION_EXECUTED)
    args: dict[str, Any] = {"content": X_TEXT}
    if declare:
        args["supersedes"] = t
    reply = await _call("save_memory", args)
    ctx = await _call("get_context", {"intent": INTENT})
    return MemoryRun(
        t=t,
        x=_doc_id(reply),
        reply=reply,
        ctx=ctx,
        withholding=_last_pack(reg)["withholding"],
        before=before,
        after=reg.knowledge.document_store.get(t),
        on_t=_mutations_on(reg, seen, t),
    )


async def test_control_save_memory_serves_both_versions(temp_registry):
    run = await _memory_run(temp_registry, declare=False)

    assert run.reply == f"Memory saved: {run.x}"
    assert _served(run.ctx, run.t), run.ctx
    assert _served(run.ctx, run.x), run.ctx
    assert run.withholding["total"] == 0
    assert run.on_t == []
    assert run.after == run.before
    assert declared_successor(run.after["metadata"]) is None


async def test_save_memory_supersedes_withholds_the_old_document(temp_registry):
    run = await _memory_run(temp_registry, declare=True)

    assert run.reply == f"Memory saved (supersedes {run.t}): {run.x}"
    assert not _served(run.ctx, run.t), run.ctx
    assert _served(run.ctx, run.x), run.ctx
    assert run.withholding["withheld_item_ids"] == [run.t]
    assert run.withholding["by_reason"] == {"superseded": 1}
    assert "**Withheld:**" in run.ctx

    # The stamp is the lifecycle key and nothing else: the rest of the bag,
    # the content and both clocks are exactly what the original write left.
    after_meta = dict(run.after["metadata"])
    assert after_meta.pop(LIFECYCLE_KEY) == _stamp(run.x)
    assert after_meta == run.before["metadata"]
    assert run.after["content"] == run.before["content"]
    assert run.after["created_at"] == run.before["created_at"]
    assert run.after["updated_at"] == run.before["updated_at"]

    # Governed: one audited mutation on the target, attributed to the tool.
    assert len(run.on_t) == 1, run.on_t
    assert run.on_t[0]["operation"] == "evidence.ingest"
    assert run.on_t[0]["requested_by"] == "mcp:save_memory"


def test_uri_only_target_is_stamped(temp_registry):
    # evidence.ingest stores a document from a uri alone, with empty content.
    # The stamp re-sends that content, so it must re-send the uri too, or the
    # handler rejects the write as empty.
    reg = temp_registry
    created = execute_mutation(
        "evidence.ingest",
        {
            "evidence": {
                "doc_id": "zf-uri",
                "content": "",
                "uri": "https://example.invalid/zf",
            }
        },
    )
    assert json.loads(created)["status"] == "success", created
    before = reg.knowledge.document_store.get("zf-uri")

    reply = save_memory(X_TEXT, supersedes="zf-uri")

    x = _doc_id(reply)
    assert reply == f"Memory saved (supersedes zf-uri): {x}"
    after = reg.knowledge.document_store.get("zf-uri")
    after_meta = dict(after["metadata"])
    assert after_meta.pop(LIFECYCLE_KEY) == _stamp(x)
    assert after_meta == before["metadata"]
    assert after["content"] == ""


# ---------------------------------------------------------------------------
# save_knowledge, end to end
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KnowledgeRun:
    n_old: str
    t_doc: str
    n_new: str
    z: str
    reply: str
    ctx: str
    withholding: dict[str, Any]
    seen: set[str]

    def served(self) -> dict[str, bool]:
        return {
            "N_old": _served(self.ctx, self.n_old),
            "T_doc": _served(self.ctx, self.t_doc),
            "N_new": _served(self.ctx, self.n_new),
            "Z": _served(self.ctx, self.z),
        }


async def _knowledge_run(reg: StoreRegistry, *, declare: bool) -> KnowledgeRun:
    auth_mod.set_auth_enforced(enforced=False)
    first = await _call("save_knowledge", {"name": NAME, "content": T_TEXT})
    n_old, t_doc = _entity_ids(first)
    seen = _event_ids(reg, EventType.MUTATION_EXECUTED)
    args: dict[str, Any] = {"name": NAME, "content": X_TEXT}
    if declare:
        args["supersedes"] = n_old
    reply = await _call("save_knowledge", args)
    n_new, z = _entity_ids(reply)
    ctx = await _call("get_context", {"intent": INTENT})
    return KnowledgeRun(
        n_old=n_old,
        t_doc=t_doc,
        n_new=n_new,
        z=z,
        reply=reply,
        ctx=ctx,
        withholding=_last_pack(reg)["withholding"],
        seen=seen,
    )


async def test_control_save_knowledge_serves_both_versions(temp_registry):
    reg = temp_registry
    run = await _knowledge_run(reg, declare=False)

    # The stale evidence is served beside its replacement. (The two entity
    # stubs share one excerpt — the name — so semantic dedup already keeps
    # only one of them; which one is not this test's business.)
    served = run.served()
    assert served["T_doc"], run.ctx
    assert served["Z"], run.ctx
    assert served["N_new"] or served["N_old"], run.ctx
    assert "superseded" not in run.withholding["by_reason"]
    assert run.t_doc not in run.withholding["withheld_item_ids"]
    assert "Superseded:" not in run.reply
    assert declared_successor(_props(reg, run.n_old)) is None
    assert declared_successor(_meta(reg, run.t_doc)) is None
    assert _mutations_on(reg, run.seen, run.n_old) == []
    assert _mutations_on(reg, run.seen, run.t_doc) == []


async def test_save_knowledge_supersedes_withholds_entity_and_evidence(
    temp_registry,
):
    reg = temp_registry
    run = await _knowledge_run(reg, declare=True)

    # The successor is the new evidence document Z, not the new node: Z is
    # what the keyword and semantic axes serve, and N_new resolves through
    # its ``evidence_ref``. A pack withholds nothing if the stamp names N_new.
    assert _props(reg, run.n_old)[LIFECYCLE_KEY] == _stamp(run.z)
    assert _meta(reg, run.t_doc)[LIFECYCLE_KEY] == _stamp(run.z)
    assert f"Superseded: {run.n_old} -> {run.z}" in run.reply.splitlines()
    assert f"Superseded: {run.t_doc} -> {run.z}" in run.reply.splitlines()

    assert run.served() == {"N_old": False, "T_doc": False, "N_new": True, "Z": True}, (
        run.ctx
    )
    assert run.withholding["by_reason"] == {"superseded": 2}
    assert sorted(run.withholding["withheld_item_ids"]) == sorted(
        [run.n_old, run.t_doc]
    )

    node_ops = _mutations_on(reg, run.seen, run.n_old)
    doc_ops = _mutations_on(reg, run.seen, run.t_doc)
    assert [p["operation"] for p in node_ops] == ["entity.update"]
    assert [p["operation"] for p in doc_ops] == ["evidence.ingest"]
    assert {p["requested_by"] for p in node_ops + doc_ops} == {"mcp:save_knowledge"}


def test_entity_without_evidence_is_stamped_alone(temp_registry):
    # An entity saved with no content or evidence_ref has no evidence
    # document, so the node stamp is the whole supersession.
    reg = temp_registry
    first = save_knowledge(name=NAME)
    assert "Evidence document" not in first, first
    created = re.search(r"Entity created: (\S+)", first)
    assert created is not None, first
    n_old = created.group(1)
    seen = _event_ids(reg, EventType.MUTATION_EXECUTED)

    reply = save_knowledge(name=NAME, content=X_TEXT, supersedes=n_old)

    _, z = _entity_ids(reply)
    assert reply.splitlines()[2:] == [f"Superseded: {n_old} -> {z}"]
    assert _props(reg, n_old)[LIFECYCLE_KEY] == _stamp(z)
    node_ops = _mutations_on(reg, seen, n_old)
    assert [p["operation"] for p in node_ops] == ["entity.update"]


@pytest.mark.parametrize(
    ("t_doc_metadata", "reason"),
    [
        pytest.param(None, "no such document", id="missing"),
        pytest.param(
            {LIFECYCLE_KEY: Lifecycle(state="archived").model_dump(mode="json")},
            "it is archived",
            id="archived",
        ),
        pytest.param(
            {LIFECYCLE_KEY: _stamp("zf-other")},
            "it is already superseded by zf-other",
            id="superseded-elsewhere",
        ),
        # One chunk row is still a chunk row, so the boundary is > 0.
        pytest.param(
            {"chunk_count": 1},
            "it is part of a chunked document, which supersedes= does not support",
            id="chunked",
        ),
    ],
)
def test_ineligible_old_evidence_is_left_and_reported(
    temp_registry, t_doc_metadata, reason
):
    # The entity is what the caller named, so it is stamped regardless; its
    # evidence document is stamped only when it can be, and the reply names
    # the one it left, with why.
    reg = temp_registry
    if t_doc_metadata is not None:
        _put(reg, "zf-t-doc", T_TEXT, **t_doc_metadata)
    _node(reg, "zf-node", evidence_ref="zf-t-doc")
    t_doc_before = reg.knowledge.document_store.get("zf-t-doc")
    seen = _event_ids(reg, EventType.MUTATION_EXECUTED)

    reply = save_knowledge(name=NAME, content=X_TEXT, supersedes="zf-node")

    _, z = _entity_ids(reply)
    lines = reply.splitlines()
    assert f"Superseded: zf-node -> {z}" in lines
    assert f"Evidence document zf-t-doc left unchanged ({reason})" in lines
    assert _props(reg, "zf-node")[LIFECYCLE_KEY] == _stamp(z)
    assert reg.knowledge.document_store.get("zf-t-doc") == t_doc_before
    assert _mutations_on(reg, seen, "zf-t-doc") == []
    node_ops = _mutations_on(reg, seen, "zf-node")
    assert [p["operation"] for p in node_ops] == ["entity.update"]


# ---------------------------------------------------------------------------
# Vector mirror (#338)
# ---------------------------------------------------------------------------


def test_document_stamp_mirrors_onto_vector_row_without_reembedding(
    temp_registry, monkeypatch
):
    monkeypatch.setenv("TRELLIS_ENABLE_EMBED_ON_INGEST", "1")
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_PATH)
    reg = temp_registry
    embedded = importlib.import_module(EMBED_PATH.rpartition(".")[0])._EMBED_CALLS
    vectors = reg.knowledge.vector_store

    t = _doc_id(save_memory(T_TEXT))
    vector_before = vectors.get(t)
    assert vector_before is not None
    assert vector_before["metadata"].get("created_at")
    embedded.clear()

    x = _doc_id(save_memory(X_TEXT, supersedes=t))

    vector_after = vectors.get(t)
    assert vector_after is not None
    assert vector_after["metadata"][LIFECYCLE_KEY] == _stamp(x)
    assert (
        vector_after["metadata"]["created_at"]
        == vector_before["metadata"]["created_at"]
    )
    # Only the new memory was embedded: the stamp re-embedded nothing.
    assert embedded == [X_TEXT]


# ---------------------------------------------------------------------------
# Refusals: every one writes nothing and says so
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Prepared:
    kwargs: dict[str, Any]
    fragment: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RefusalCase:
    case_id: str
    tool: str
    kind: str
    loc: str
    setup: Callable[[StoreRegistry], Prepared]
    #: The pre-existing ``evidence_ref`` rejection, reached with
    #: ``supersedes`` given: its message and data are today's, unchanged.
    legacy: bool = False


def _put(reg: StoreRegistry, doc_id: str, content: str, **metadata: Any) -> None:
    reg.knowledge.document_store.put(doc_id, content, metadata)


def _node(reg: StoreRegistry, node_id: str, **properties: Any) -> None:
    reg.knowledge.graph_store.upsert_node(
        node_id, "concept", {"name": "zf", **properties}
    )


def _m2_unknown(reg: StoreRegistry) -> Prepared:
    return Prepared(
        {"content": X_TEXT, "supersedes": "doc-missing"},
        "supersedes does not reference an existing document: doc-missing",
    )


def _m2_node(reg: StoreRegistry) -> Prepared:
    _node(reg, "zf-node")
    return Prepared(
        {"content": X_TEXT, "supersedes": "zf-node"},
        "supersedes does not reference an existing document: zf-node",
    )


def _self_doc_id(reg: StoreRegistry) -> Prepared:
    t = _doc_id(save_memory(T_TEXT))
    return Prepared(
        {"content": X_TEXT, "doc_id": t, "supersedes": t},
        f"supersedes={t} names the document this call would write or match",
    )


def _self_identical(reg: StoreRegistry) -> Prepared:
    t = _doc_id(save_memory(T_TEXT))
    return Prepared(
        {"content": T_TEXT, "supersedes": t},
        f"supersedes={t} names the document this call would write or match",
    )


def _m4_chunked_parent(reg: StoreRegistry) -> Prepared:
    _put(reg, "zf-parent", T_TEXT, chunk_count=3)
    return Prepared(
        {"content": X_TEXT, "supersedes": "zf-parent"},
        "supersedes cannot target zf-parent: it is part of a chunked document",
    )


def _m4_chunk_row(reg: StoreRegistry) -> Prepared:
    _put(
        reg,
        "zf-parent#chunk-0",
        T_TEXT,
        parent_doc_id="zf-parent",
        chunk_index=0,
        chunk_count=2,
    )
    return Prepared(
        {"content": X_TEXT, "supersedes": "zf-parent#chunk-0"},
        "supersedes cannot target zf-parent#chunk-0: it is part of a chunked document",
    )


def _m5_archived(reg: StoreRegistry) -> Prepared:
    _put(
        reg,
        "zf-archived",
        T_TEXT,
        lifecycle=Lifecycle(state="archived").model_dump(mode="json"),
    )
    return Prepared(
        {"content": X_TEXT, "supersedes": "zf-archived"},
        "supersedes cannot target zf-archived: it is archived",
    )


def _m6_superseded(reg: StoreRegistry) -> Prepared:
    _put(reg, "zf-old", T_TEXT, lifecycle=_stamp("zf-other"))
    return Prepared(
        {"content": X_TEXT, "supersedes": "zf-old"},
        "supersedes cannot target zf-old: it is already superseded by zf-other",
    )


def _m11_cycle(reg: StoreRegistry) -> Prepared:
    # A revert: the old content E already points at T, so stamping T -> E
    # would form a cycle the gate keeps whole, and the revert would not land.
    _put(reg, "zf-old", T_TEXT)
    _put(reg, "zf-e", X_TEXT, lifecycle=_stamp("zf-old"))
    return Prepared(
        {"content": X_TEXT, "supersedes": "zf-old"},
        "content is identical to zf-e, which is itself superseded by zf-old",
    )


def _m10_near_duplicate(reg: StoreRegistry) -> Prepared:
    w = _doc_id(save_memory(T7_OLD))
    t = _doc_id(save_memory(T_TEXT))
    return Prepared(
        {"content": T7_NEW, "supersedes": t},
        f"of {w}; nothing was written and {t} is unchanged",
        {"duplicate_of": w},
    )


def _k_none_unknown(reg: StoreRegistry) -> Prepared:
    return Prepared(
        {"name": NAME, "content": X_TEXT, "supersedes": "node-missing"},
        "supersedes does not reference an existing entity: node-missing",
    )


def _k_none_doc(reg: StoreRegistry) -> Prepared:
    _put(reg, "zf-doc", T_TEXT)
    return Prepared(
        {"name": NAME, "content": X_TEXT, "supersedes": "zf-doc"},
        "supersedes does not reference an existing entity: zf-doc",
    )


def _k1_no_content(reg: StoreRegistry) -> Prepared:
    _node(reg, "zf-node")
    return Prepared(
        {"name": NAME, "supersedes": "zf-node"},
        "supersedes requires content or evidence_ref",
    )


def _k1_blank_content(reg: StoreRegistry) -> Prepared:
    _node(reg, "zf-node")
    return Prepared(
        {"name": NAME, "content": "   ", "supersedes": "zf-node"},
        "supersedes requires content or evidence_ref",
    )


def _k2_archived(reg: StoreRegistry) -> Prepared:
    _node(reg, "zf-node", lifecycle=Lifecycle(state="archived").model_dump(mode="json"))
    return Prepared(
        {"name": NAME, "content": X_TEXT, "supersedes": "zf-node"},
        "supersedes cannot target zf-node: it is archived",
    )


def _k3_superseded(reg: StoreRegistry) -> Prepared:
    _node(reg, "zf-node", lifecycle=_stamp("zf-other-doc"))
    return Prepared(
        {"name": NAME, "content": X_TEXT, "supersedes": "zf-node"},
        "supersedes cannot target zf-node: it is already superseded by zf-other-doc",
    )


def _k4_content(reg: StoreRegistry) -> Prepared:
    n_old, t_doc = _entity_ids(save_knowledge(name=NAME, content=T_TEXT))
    return Prepared(
        {"name": NAME, "content": T_TEXT, "supersedes": n_old},
        f"content resolves to {t_doc}, the evidence document of {n_old}",
    )


def _k4_evidence_ref(reg: StoreRegistry) -> Prepared:
    n_old, t_doc = _entity_ids(save_knowledge(name=NAME, content=T_TEXT))
    return Prepared(
        {"name": NAME, "evidence_ref": t_doc, "supersedes": n_old},
        f"evidence_ref resolves to {t_doc}, the evidence document of {n_old}",
    )


def _k6_evidence_ref(reg: StoreRegistry) -> Prepared:
    n_old, _ = _entity_ids(save_knowledge(name=NAME, content=T_TEXT))
    _put(reg, "zf-s", X_TEXT, lifecycle=_stamp("zf-other"))
    return Prepared(
        {"name": NAME, "evidence_ref": "zf-s", "supersedes": n_old},
        "evidence_ref resolves to zf-s, which is itself superseded by zf-other",
    )


def _k6_content(reg: StoreRegistry) -> Prepared:
    n_old, _ = _entity_ids(save_knowledge(name=NAME, content=T_TEXT))
    _put(reg, "zf-s", X_TEXT, lifecycle=_stamp("zf-other"))
    return Prepared(
        {"name": NAME, "content": X_TEXT, "supersedes": n_old},
        "content resolves to zf-s, which is itself superseded by zf-other",
    )


def _dangling_evidence_ref(**properties: Any) -> Callable[[StoreRegistry], Prepared]:
    """The existing ``evidence_ref`` rejection, whatever state the target is in.

    It fires once (``supersedes`` adds no second rejection) and before any
    check on the target, so an archived or superseded target does not change
    which error the caller's own bad ``evidence_ref`` gets.
    """

    def setup(reg: StoreRegistry) -> Prepared:
        _node(reg, "zf-node", **properties)
        return Prepared(
            {"name": NAME, "evidence_ref": "doc-missing", "supersedes": "zf-node"},
            "evidence_ref does not reference an existing document: doc-missing",
            {"evidence_ref": "doc-missing"},
        )

    return setup


_MEMORY, _KNOWLEDGE = "save_memory", "save_knowledge"
_DANGLING, _VALUE = "dangling_reference", "value"

REFUSALS = [
    RefusalCase("m2-unknown", _MEMORY, _DANGLING, "supersedes", _m2_unknown),
    RefusalCase("m2-node", _MEMORY, _DANGLING, "supersedes", _m2_node),
    RefusalCase("self-doc-id", _MEMORY, _VALUE, "supersedes", _self_doc_id),
    RefusalCase("self-identical", _MEMORY, _VALUE, "supersedes", _self_identical),
    RefusalCase("m4-chunked-parent", _MEMORY, _VALUE, "supersedes", _m4_chunked_parent),
    RefusalCase("m4-chunk-row", _MEMORY, _VALUE, "supersedes", _m4_chunk_row),
    RefusalCase("m5-archived", _MEMORY, _VALUE, "supersedes", _m5_archived),
    RefusalCase("m6-superseded", _MEMORY, _VALUE, "supersedes", _m6_superseded),
    RefusalCase("m11-cycle", _MEMORY, _VALUE, "content", _m11_cycle),
    RefusalCase("m10-near-duplicate", _MEMORY, _VALUE, "content", _m10_near_duplicate),
    RefusalCase("k-none-unknown", _KNOWLEDGE, _DANGLING, "supersedes", _k_none_unknown),
    RefusalCase("k-none-doc", _KNOWLEDGE, _DANGLING, "supersedes", _k_none_doc),
    RefusalCase("k1-no-content", _KNOWLEDGE, "missing", "content", _k1_no_content),
    RefusalCase(
        "k1-blank-content", _KNOWLEDGE, "missing", "content", _k1_blank_content
    ),
    RefusalCase("k2-archived", _KNOWLEDGE, _VALUE, "supersedes", _k2_archived),
    RefusalCase("k3-superseded", _KNOWLEDGE, _VALUE, "supersedes", _k3_superseded),
    RefusalCase("k4-content", _KNOWLEDGE, _VALUE, "content", _k4_content),
    RefusalCase(
        "k4-evidence-ref", _KNOWLEDGE, _VALUE, "evidence_ref", _k4_evidence_ref
    ),
    RefusalCase(
        "k6-evidence-ref", _KNOWLEDGE, _VALUE, "evidence_ref", _k6_evidence_ref
    ),
    RefusalCase("k6-content", _KNOWLEDGE, _VALUE, "content", _k6_content),
    RefusalCase(
        "dangling-evidence-ref",
        _KNOWLEDGE,
        _DANGLING,
        "evidence_ref",
        _dangling_evidence_ref(),
        legacy=True,
    ),
    RefusalCase(
        "dangling-evidence-ref-archived",
        _KNOWLEDGE,
        _DANGLING,
        "evidence_ref",
        _dangling_evidence_ref(
            lifecycle=Lifecycle(state="archived").model_dump(mode="json")
        ),
        legacy=True,
    ),
    RefusalCase(
        "dangling-evidence-ref-superseded",
        _KNOWLEDGE,
        _DANGLING,
        "evidence_ref",
        _dangling_evidence_ref(lifecycle=_stamp("zf-other-doc")),
        legacy=True,
    ),
]


@pytest.mark.parametrize("case", REFUSALS, ids=[c.case_id for c in REFUSALS])
def test_refusal_writes_nothing_and_says_so(temp_registry, case):
    reg = temp_registry
    prepared = case.setup(reg)
    tool = save_memory if case.tool == _MEMORY else save_knowledge
    target = prepared.kwargs["supersedes"]
    docs = reg.knowledge.document_store
    graph = reg.knowledge.graph_store
    doc_count, node_count = docs.count(), graph.count_nodes()
    target_doc, target_node = docs.get(target), graph.get_node(target)
    mutations = _event_ids(reg, EventType.MUTATION_EXECUTED)
    rejections = _event_ids(reg, EventType.WRITE_REJECTED)

    with pytest.raises(McpError) as excinfo:
        tool(**prepared.kwargs)

    error = excinfo.value.error
    assert error.code == INVALID_PARAMS
    assert prepared.fragment in error.message, error.message
    assert error.data["field"] == case.loc
    if not case.legacy:
        assert "nothing was written" in error.message, error.message
        assert error.data["supersedes"] == target
    for key, value in prepared.data.items():
        assert error.data[key] == value, key

    assert docs.count() == doc_count
    assert graph.count_nodes() == node_count
    assert docs.get(target) == target_doc
    assert graph.get_node(target) == target_node
    assert _new_events(reg, EventType.MUTATION_EXECUTED, mutations) == []
    rejected = _new_events(reg, EventType.WRITE_REJECTED, rejections)
    assert len(rejected) == 1, [e.payload for e in rejected]
    assert rejected[0].payload["tool"] == case.tool
    assert rejected[0].payload["rejections"][0]["kind"] == case.kind
    assert rejected[0].payload["rejections"][0]["loc"] == case.loc


# ---------------------------------------------------------------------------
# Resends, near-revisions, partial failure
# ---------------------------------------------------------------------------


def test_save_memory_resend_is_answered_without_a_second_write(temp_registry):
    reg = temp_registry
    t = _doc_id(save_memory(T_TEXT))
    x = _doc_id(save_memory(X_TEXT, supersedes=t))
    seen = _event_ids(reg, EventType.MUTATION_EXECUTED)

    reply = save_memory(X_TEXT, supersedes=t)

    assert reply == f"Memory already exists (supersedes {t}): {x}"
    assert _mutations_on(reg, seen, t) == []
    assert declared_successor(_meta(reg, t)) == x


def test_exact_hit_first_declaration_stamps_the_match(temp_registry):
    # The content is already stored, undeclared: nothing new is written, the
    # matching document is the successor, and the stamp is the one write.
    reg = temp_registry
    t = _doc_id(save_memory(T_TEXT))
    e = _doc_id(save_memory(X_TEXT))
    doc_count = reg.knowledge.document_store.count()
    seen = _event_ids(reg, EventType.MUTATION_EXECUTED)

    reply = save_memory(X_TEXT, supersedes=t)

    assert reply == f"Memory already exists (supersedes {t}): {e}"
    assert _meta(reg, t)[LIFECYCLE_KEY] == _stamp(e)
    assert reg.knowledge.document_store.count() == doc_count
    assert [
        (p["operation"], p["requested_by"]) for p in _mutations_on(reg, seen, t)
    ] == [("evidence.ingest", "mcp:save_memory")]


def test_save_knowledge_resend_does_not_restamp(temp_registry):
    reg = temp_registry
    n_old, t_doc = _entity_ids(save_knowledge(name=NAME, content=T_TEXT))
    _, z = _entity_ids(save_knowledge(name=NAME, content=X_TEXT, supersedes=n_old))
    seen = _event_ids(reg, EventType.MUTATION_EXECUTED)

    reply = save_knowledge(name=NAME, content=X_TEXT, supersedes=n_old)

    assert _mutations_on(reg, seen, n_old) == []
    assert _mutations_on(reg, seen, t_doc) == []
    assert f"Superseded: {n_old} -> {z}" in reply.splitlines()
    assert f"Superseded: {t_doc} -> {z}" in reply.splitlines()


def test_near_revision_of_the_target_is_stored_not_deduplicated(temp_registry):
    reg = temp_registry
    t = _doc_id(save_memory(T7_OLD))
    match = server_mod._get_minhash_index(reg).find_duplicate(T7_NEW)
    assert match is not None
    assert match[0] == t
    assert match[1] >= 0.85, match

    reply = save_memory(T7_NEW, supersedes=t)

    x = _doc_id(reply)
    assert reply == f"Memory saved (supersedes {t}): {x}"
    assert x != t
    assert declared_successor(_meta(reg, t)) == x


def test_create_failure_reports_applied_stamps_and_resend_finishes(temp_registry):
    reg = temp_registry
    graph = reg.knowledge.graph_store
    n_old, t_doc = _entity_ids(save_knowledge(name=NAME, content=T_TEXT))
    nodes_before = graph.count_nodes()
    _write_policy(reg, "entity.create")

    with pytest.raises(McpError) as excinfo:
        save_knowledge(name=NAME, content=X_TEXT, supersedes=n_old)

    error = excinfo.value.error
    assert error.code == MUTATION_FAILED
    z = error.data["evidence_ref"]
    assert error.data["supersession_applied"] == [n_old, t_doc]
    assert f"supersession already applied: {n_old} -> {z}, {t_doc} -> {z}" in (
        error.message
    )
    assert declared_successor(_props(reg, n_old)) == z
    assert declared_successor(_meta(reg, t_doc)) == z
    assert graph.count_nodes() == nodes_before

    _clear_policies(reg)
    seen = _event_ids(reg, EventType.MUTATION_EXECUTED)
    reply = save_knowledge(name=NAME, content=X_TEXT, supersedes=n_old)

    _, z_again = _entity_ids(reply)
    assert z_again == z
    assert graph.count_nodes() == nodes_before + 1
    assert _mutations_on(reg, seen, n_old) == []
    assert _mutations_on(reg, seen, t_doc) == []


def test_post_store_stamp_failure_warns_and_the_resend_heals(
    temp_registry, monkeypatch
):
    reg = temp_registry
    supersession = importlib.import_module("trellis.mcp.supersession")
    real = supersession.supersede_document
    attempts: list[str] = []

    def flaky(registry: StoreRegistry, **kwargs: Any) -> str | None:
        attempts.append(kwargs["doc_id"])
        if len(attempts) == 1:
            return "store unavailable"
        return real(registry, **kwargs)

    monkeypatch.setattr(supersession, "supersede_document", flaky)
    t = _doc_id(save_memory(T_TEXT))
    t_before = reg.knowledge.document_store.get(t)

    reply = save_memory(X_TEXT, supersedes=t)

    x = _doc_id(reply)
    assert reply == (
        f"Memory saved: {x}\nWarning: supersession NOT applied — {t} is "
        "unchanged (store unavailable). Re-send the same call to retry; it "
        "will not store a duplicate."
    )
    stored = [e for e in _events(reg, EventType.MEMORY_STORED) if e.entity_id == x]
    assert len(stored) == 1
    assert reg.knowledge.document_store.get(t) == t_before

    again = save_memory(X_TEXT, supersedes=t)

    assert again == f"Memory already exists (supersedes {t}): {x}"
    assert declared_successor(_meta(reg, t)) == x


def test_exact_hit_stamp_failure_is_an_error_not_a_success(temp_registry):
    reg = temp_registry
    t = _doc_id(save_memory(T_TEXT))
    e = _doc_id(save_memory(X_TEXT))
    t_before = reg.knowledge.document_store.get(t)
    _write_policy(reg, "evidence.ingest")

    with pytest.raises(McpError) as excinfo:
        save_memory(X_TEXT, supersedes=t)

    error = excinfo.value.error
    assert error.code == MUTATION_FAILED
    assert error.data["stage"] == "supersede"
    assert error.data["supersedes"] == t
    assert error.data["existing"] == e
    assert f"failed to supersede {t}" in error.message
    assert "nothing was written" in error.message
    assert reg.knowledge.document_store.get(t) == t_before


def test_node_stamp_failure_creates_no_entity(temp_registry):
    reg = temp_registry
    graph = reg.knowledge.graph_store
    n_old, t_doc = _entity_ids(save_knowledge(name=NAME, content=T_TEXT))
    nodes_before = graph.count_nodes()
    _write_policy(reg, "entity.update")

    with pytest.raises(McpError) as excinfo:
        save_knowledge(name=NAME, content=X_TEXT, supersedes=n_old)

    error = excinfo.value.error
    assert error.code == MUTATION_FAILED
    assert error.data["stage"] == "supersede"
    assert error.data["supersession_applied"] == []
    assert "supersession applied: none" in error.message
    assert "no entity was created" in error.message
    assert graph.count_nodes() == nodes_before
    assert declared_successor(_props(reg, n_old)) is None
    assert declared_successor(_meta(reg, t_doc)) is None


def test_node_stamp_is_governed_by_a_policy_on_the_node_type(temp_registry):
    # The stamp is an entity.update on the old node, so a policy scoped to
    # that node's type governs it exactly as it governs a direct update.
    reg = temp_registry
    graph = reg.knowledge.graph_store
    n_old, t_doc = _entity_ids(
        save_knowledge(name=NAME, content=T_TEXT, entity_type="zf-rig")
    )
    nodes_before = graph.count_nodes()
    _write_policy(
        reg, "entity.update", PolicyScope(level="entity_type", value="zf-rig")
    )

    with pytest.raises(McpError) as excinfo:
        save_knowledge(name=NAME, content=X_TEXT, supersedes=n_old)

    assert excinfo.value.error.code == MUTATION_FAILED
    assert excinfo.value.error.data["supersession_applied"] == []
    assert graph.count_nodes() == nodes_before
    assert declared_successor(_props(reg, n_old)) is None
    assert declared_successor(_meta(reg, t_doc)) is None


def test_a_stamp_that_raises_is_reported_not_raised(temp_registry, monkeypatch):
    # Both stamps promise never to raise. One escaping after the store would
    # leave the memory saved and indexed, skip MEMORY_STORED, and report the
    # saved write as an error.
    supersession = importlib.import_module("trellis.mcp.supersession")
    reg = temp_registry
    t = _doc_id(save_memory(T_TEXT))
    n_old, _ = _entity_ids(save_knowledge(name=NAME, content=T7_OLD))
    t_before = reg.knowledge.document_store.get(t)

    def broken(*args: Any, **kwargs: Any) -> Any:
        msg = "executor unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(supersession, "build_curate_executor", broken)

    node_error = supersession.supersede_entity(
        reg, node_id=n_old, successor="zf-z", requested_by="mcp:save_knowledge"
    )
    reply = save_memory(X_TEXT, supersedes=t)

    assert node_error == "RuntimeError: executor unavailable"
    x = _doc_id(reply)
    assert reply == (
        f"Memory saved: {x}\nWarning: supersession NOT applied — {t} is "
        "unchanged (RuntimeError: executor unavailable). Re-send the same call "
        "to retry; it will not store a duplicate."
    )
    stored = [e for e in _events(reg, EventType.MEMORY_STORED) if e.entity_id == x]
    assert len(stored) == 1
    assert reg.knowledge.document_store.get(t) == t_before
    assert declared_successor(_props(reg, n_old)) is None


def test_stamp_names_a_target_removed_after_the_check(temp_registry):
    # The check and the stamp read the target separately. One removed in
    # between is reported by name, not as a TypeError from a missing row.
    supersession = importlib.import_module("trellis.mcp.supersession")
    reg = temp_registry
    seen = _event_ids(reg, EventType.MUTATION_EXECUTED)

    doc_error = supersession.supersede_document(
        reg, doc_id="doc-gone", successor="zf-z", requested_by="mcp:save_memory"
    )
    node_error = supersession.supersede_entity(
        reg, node_id="node-gone", successor="zf-z", requested_by="mcp:save_knowledge"
    )

    assert doc_error == "document doc-gone no longer exists"
    assert node_error == "entity node-gone no longer exists"
    assert _new_events(reg, EventType.MUTATION_EXECUTED, seen) == []


def test_reconcile_tier_is_bypassed_only_when_supersedes_is_given(
    temp_registry, monkeypatch
):
    reg = temp_registry
    t = _doc_id(save_memory(T_TEXT))
    monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
    reconciled: list[str] = []

    def sentinel(*args: Any, **kwargs: Any) -> str:
        reconciled.append("called")
        return "sentinel"

    monkeypatch.setattr(server_mod, "_save_memory_reconciled", sentinel)

    assert save_memory(X_TEXT) == "sentinel"
    assert reconciled == ["called"]

    reply = save_memory(X_TEXT, supersedes=t)

    x = _doc_id(reply)
    assert reply == f"Memory saved (supersedes {t}): {x}"
    assert reconciled == ["called"]
    assert declared_successor(_meta(reg, t)) == x


# ---------------------------------------------------------------------------
# Undo: the documented operator routes invert the stamp, and a resend
# re-applies it (a derived idempotency key would make it a silent DUPLICATE)
# ---------------------------------------------------------------------------


def test_document_undo_then_resend_restamps(temp_registry):
    reg = temp_registry
    t = _doc_id(save_memory(T_TEXT))
    x = _doc_id(save_memory(X_TEXT, supersedes=t))
    stored = reg.knowledge.document_store.get(t)
    bag = dict(stored["metadata"])
    bag[LIFECYCLE_KEY] = Lifecycle().model_dump(mode="json")

    undo = execute_mutation(
        "evidence.ingest",
        {
            "evidence": {
                "doc_id": t,
                "content": stored["content"],
                "metadata": bag,
                "preserve_updated_at": True,
            }
        },
        idempotency_key="undo-fresh-1",
    )

    assert json.loads(undo)["status"] == "success", undo
    assert _meta(reg, t)[LIFECYCLE_KEY]["state"] == "current"
    assert _served(get_context(INTENT), t)

    seen = _event_ids(reg, EventType.MUTATION_EXECUTED)
    again = save_memory(X_TEXT, supersedes=t)

    assert again == f"Memory already exists (supersedes {t}): {x}"
    assert len(_mutations_on(reg, seen, t)) == 1
    assert declared_successor(_meta(reg, t)) == x


def test_entity_undo_restores_current(temp_registry):
    reg = temp_registry
    n_old, _ = _entity_ids(save_knowledge(name=NAME, content=T_TEXT))
    save_knowledge(name=NAME, content=X_TEXT, supersedes=n_old)

    undo = execute_mutation(
        "entity.update",
        {"entity_id": n_old, "properties": {LIFECYCLE_KEY: {"state": "current"}}},
    )

    assert json.loads(undo)["status"] == "success", undo
    assert _props(reg, n_old)[LIFECYCLE_KEY] == {"state": "current"}
