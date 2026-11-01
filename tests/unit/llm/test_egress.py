"""The egress predicate, and the senders that call it.

Content labelled above ``internal`` must not reach an LLM route that is not
declared local. The predicate lives in :mod:`trellis.llm.egress`; these tests
pin it, and the two senders built on it that need no CLI harness (the
enrichment batch and the MCP reconcile outcome). The shadow pass, the
capture reconcile pass, corpus sync and the worker CLI have their tests beside
their own suites.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

import trellis.mcp.server as server_mod
from tests.llm_routes import per_consumer_config, route_from
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.llm.egress import (
    EGRESS_WITHHELD_REASON,
    EgressGate,
    EgressVerdict,
    may_reach_route,
)
from trellis.llm.routing import (
    LLMConsumer,
    LLMRoutingError,
    resolve_llm_route,
    validate_llm_routing,
)
from trellis.llm.types import LLMResponse
from trellis.mcp.reconcile import (
    JUDGE_FAILURE_REASONS,
    ReconcileCandidate,
    ReconcileDecision,
    ReconcileFallbackReason,
)
from trellis.stores.registry import StoreRegistry
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis_cli import worker


def _label(sensitivity: str) -> dict[str, Any]:
    return {"data_classification": {"sensitivity": sensitivity}}


@pytest.fixture
def store(tmp_path: Path):
    s = SQLiteDocumentStore(tmp_path / "docs.db")
    yield s
    s.close()


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def _registry(store: SQLiteDocumentStore, *, local: bool) -> MagicMock:
    """A registry whose reconcile / enrichment route is (not) declared local."""
    registry = MagicMock()
    registry.knowledge.document_store = store
    config: dict[str, Any] = {"local": True} if local else {}
    registry.llm_route.side_effect = lambda consumer=None: resolve_llm_route(
        config, consumer
    )
    return registry


class TestMayReachRoute:
    @pytest.mark.parametrize("label", ["public", "internal"])
    def test_public_and_internal_reach_any_route(self, label: str) -> None:
        assert may_reach_route(label, local=False) is True  # type: ignore[arg-type]
        assert may_reach_route(label, local=True) is True  # type: ignore[arg-type]

    @pytest.mark.parametrize("label", ["confidential", "restricted"])
    def test_a_label_above_internal_reaches_only_a_local_route(
        self, label: str
    ) -> None:
        assert may_reach_route(label, local=False) is False  # type: ignore[arg-type]
        assert may_reach_route(label, local=True) is True  # type: ignore[arg-type]


class TestEgressGate:
    def test_unlabelled_and_internal_documents_are_permitted(
        self, store: SQLiteDocumentStore
    ) -> None:
        gate = EgressGate(store, local=False)
        assert gate.permits("a", {}) is True
        assert gate.permits("a", None) is True
        assert gate.permits("a", _label("internal")) is True
        assert gate.permits("a", _label("public")) is True

    @pytest.mark.parametrize("label", ["confidential", "restricted"])
    def test_a_label_above_internal_is_refused(
        self, store: SQLiteDocumentStore, label: str
    ) -> None:
        assert EgressGate(store, local=False).permits("a", _label(label)) is False

    def test_a_label_that_cannot_be_read_is_refused(
        self, store: SQLiteDocumentStore
    ) -> None:
        gate = EgressGate(store, local=False)
        assert gate.permits("a", {"data_classification": "oops"}) is False
        assert gate.permits("a", {"data_classification": {"sensitivity": "x"}}) is False
        assert gate.permits("a", ["not", "a", "mapping"]) is False  # type: ignore[arg-type]

    def test_a_chunk_takes_its_parents_label(self, store: SQLiteDocumentStore) -> None:
        store.put("parent", "body", _label("confidential"))
        chunk = chunk_doc_id("parent", 0)
        store.put(chunk, "fragment", {})
        assert EgressGate(store, local=False).permits(chunk, {}) is False

    def test_an_orphan_chunk_is_withheld_whatever_its_own_label(
        self, store: SQLiteDocumentStore
    ) -> None:
        """No parent row means no parent label to inherit, so nothing is sent.

        Retrieval falls back to the chunk's own label here; egress does not,
        because an unlabelled orphan would otherwise go out.
        """
        chunk = chunk_doc_id("missing-parent", 0)
        gate = EgressGate(store, local=False)
        assert gate.permits(chunk, {}) is False
        assert gate.permits(chunk, _label("public")) is False
        assert gate.permits(chunk, _label("confidential")) is False
        assert EgressGate(store, local=True).permits(chunk, {}) is True

    def test_a_chunk_shaped_id_is_read_as_a_chunk_whatever_the_row_is(
        self, store: SQLiteDocumentStore
    ) -> None:
        """A plain document whose id resembles a chunk id is judged by shape.

        As in retrieval: the id is the signal, the row is not consulted. With
        a parent row the stricter of its own label and the parent's applies;
        with none it is an orphan and is withheld.
        """
        lookalike = chunk_doc_id("host-doc", 3)
        store.put(lookalike, "body", {})
        assert EgressGate(store, local=False).permits(lookalike, {}) is False
        store.put("host-doc", "parent body", {})
        assert EgressGate(store, local=False).permits(lookalike, {}) is True
        own = _label("confidential")
        assert EgressGate(store, local=False).permits(lookalike, own) is False
        store.put("host-doc", "parent body", _label("restricted"))
        assert EgressGate(store, local=False).permits(lookalike, {}) is False

    def test_a_store_that_raises_is_refused_not_propagated(self) -> None:
        broken = MagicMock()
        broken.get.side_effect = RuntimeError("store is down")
        chunk = chunk_doc_id("parent", 0)
        assert EgressGate(broken, local=False).permits(chunk, {}) is False

    def test_a_local_route_reads_nothing_and_permits_everything(self) -> None:
        broken = MagicMock()
        broken.get.side_effect = AssertionError("a local gate must not read")
        gate = EgressGate(broken, local=True)
        assert gate.permits("a", _label("restricted")) is True
        assert gate.permits("a", {"data_classification": "oops"}) is True
        assert gate.permits_metadata(_label("restricted")) is True

    def test_metadata_only_check_uses_the_content_own_label(self) -> None:
        gate = EgressGate(None, local=False)
        assert gate.permits_metadata({}) is True
        assert gate.permits_metadata(_label("confidential")) is False
        assert gate.permits_metadata({"data_classification": 3}) is False

    def test_for_route_follows_the_routes_own_declaration(
        self, store: SQLiteDocumentStore
    ) -> None:
        undeclared = _registry(store, local=False)
        declared = _registry(store, local=True)
        for consumer in LLMConsumer:
            assert EgressGate.for_route(undeclared, consumer, store).local is False
            assert EgressGate.for_route(declared, consumer, store).local is True

    def test_an_unresolvable_route_reads_as_not_local(
        self, store: SQLiteDocumentStore
    ) -> None:
        registry = MagicMock()
        registry.llm_route.side_effect = LLMRoutingError("llm.tiers is malformed")
        gate = EgressGate.for_route(registry, LLMConsumer.ENRICHMENT, store)
        assert gate.local is False
        assert gate.permits("a", _label("restricted")) is False

    def test_a_clearance_that_raises_is_refused_not_propagated(
        self, store: SQLiteDocumentStore
    ) -> None:
        class _Raises:
            _cache = MagicMock()

            def permits(self, *_args: Any, **_kwargs: Any) -> bool:
                msg = "clearance is down"
                raise RuntimeError(msg)

        _Raises._cache.resolve.side_effect = RuntimeError("cache is down")
        gate = EgressGate(store, local=False)
        gate._clearance = _Raises()  # type: ignore[assignment]
        assert gate.permits("plain-doc", {}) is False
        assert gate.permits(chunk_doc_id("parent", 0), {}) is False


class _PromptLog:
    """A model double that records every prompt it is sent."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(self, **kwargs: Any) -> LLMResponse:
        self.prompts.append(kwargs["messages"][1].content)
        return LLMResponse(
            content=json.dumps(
                {
                    "tags": ["alpha"],
                    "class": "reference",
                    "summary": "Summary.",
                    "importance": 0.6,
                    "class_confidence": 0.9,
                }
            ),
            model="test-model",
        )


def _docs(store: SQLiteDocumentStore) -> list[dict[str, Any]]:
    store.put("plain", "plain body", {"title": "Plain"})
    store.put("internal", "internal body", {"title": "Internal", **_label("internal")})
    store.put("secret", "secret body", {"title": "Secret", **_label("confidential")})
    store.put("locked", "locked body", {"title": "Locked", **_label("restricted")})
    store.put("garbled", "garbled body", {"data_classification": "oops"})
    return [store.get(i) for i in ("plain", "internal", "secret", "locked", "garbled")]


class TestEnrichmentEgress:
    def test_scan_withholds_labelled_documents_and_counts_them(
        self, store: SQLiteDocumentStore
    ) -> None:
        _docs(store)
        selection = worker._scan_enrichment_candidates(store, limit=10)
        assert {d["doc_id"] for d in selection.candidates} == {"plain", "internal"}
        assert selection.egress_withheld == 3

    def test_a_withheld_document_takes_no_seat_under_limit(
        self, store: SQLiteDocumentStore
    ) -> None:
        store.put("old", "old body", {"title": "Old"})
        store.put("new-secret", "newer body", _label("confidential"))
        selection = worker._scan_enrichment_candidates(store, limit=1)
        assert [d["doc_id"] for d in selection.candidates] == ["old"]
        assert selection.egress_withheld == 1

    def test_a_route_declared_local_withholds_nothing(
        self, store: SQLiteDocumentStore
    ) -> None:
        _docs(store)
        selection = worker._scan_enrichment_candidates(
            store, limit=10, egress=EgressGate(store, local=True)
        )
        assert len(selection.candidates) == 5
        assert selection.egress_withheld == 0

    def test_the_send_refuses_what_the_scan_would_have_withheld(
        self, store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        """A caller that hands the sender labelled candidates directly is gated too."""
        llm = _PromptLog()
        result = worker._run_batch_enrichment(
            llm,
            store,
            _docs(store),
            concurrency=1,
            event_log=event_log,
            vector_store=None,
        )
        assert result.egress_withheld == 3
        assert result.enriched == 2
        assert len(llm.prompts) == 2
        assert not any(
            word in prompt
            for prompt in llm.prompts
            for word in ("secret", "locked", "garbled")
        )
        for doc_id in ("secret", "locked", "garbled"):
            assert "content_tags" not in store.get(doc_id)["metadata"]

    def test_unlabelled_and_internal_prompts_are_exactly_what_they_were(
        self, store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        """The gate changes who is sent, never what is sent."""
        from trellis_workers.enrichment.service import EnrichmentService

        docs = _docs(store)[:2]
        gated = _PromptLog()
        worker._run_batch_enrichment(
            gated,
            store,
            docs,
            concurrency=1,
            event_log=event_log,
            vector_store=None,
        )
        opened = _PromptLog()
        worker._run_batch_enrichment(
            opened,
            store,
            docs,
            concurrency=1,
            event_log=event_log,
            vector_store=None,
            egress=EgressGate(store, local=True),
        )
        # What the sender sent before the gate existed: the service, directly.
        legacy = _PromptLog()
        asyncio.run(
            EnrichmentService(legacy, event_log=event_log).batch_enrich(
                [
                    {
                        "content": d["content"],
                        "title": d["metadata"].get("title", ""),
                        "tags": [],
                    }
                    for d in docs
                ],
                concurrency=1,
            )
        )
        assert len(gated.prompts) == 2
        assert gated.prompts == opened.prompts == legacy.prompts

    def test_a_route_declared_local_sends_labelled_documents(
        self, store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        llm = _PromptLog()
        result = worker._run_batch_enrichment(
            llm,
            store,
            _docs(store),
            concurrency=1,
            event_log=event_log,
            vector_store=None,
            egress=EgressGate(store, local=True),
        )
        assert result.egress_withheld == 0
        assert len(llm.prompts) == 5


def _reconcile_registry(
    store: SQLiteDocumentStore, monkeypatch: pytest.MonkeyPatch, *, local: bool
) -> tuple[MagicMock, list[LLMConsumer]]:
    built: list[LLMConsumer] = []

    class _Judge:
        async def generate(self, **_kwargs: Any) -> LLMResponse:
            return LLMResponse(
                content='{"decision": "noop", "confidence": 0.9}', model="m"
            )

    def _build(_registry: Any, consumer: LLMConsumer) -> Any:
        built.append(consumer)
        return _Judge()

    monkeypatch.setattr(server_mod, "_build_llm_client", _build)
    monkeypatch.setattr(server_mod, "configured_model_id", lambda _r: "m")
    monkeypatch.setattr(server_mod, "reconcile_timeout_seconds", lambda _r: 5.0)
    return _registry(store, local=local), built


class TestReconcileEgress:
    def _outcome(
        self,
        registry: MagicMock,
        store: SQLiteDocumentStore,
        *,
        candidate_label: dict[str, Any],
        new_label: dict[str, Any],
    ) -> Any:
        store.put("cand", "candidate text", candidate_label)
        candidate = ReconcileCandidate(
            doc_id="cand", content="candidate text", similarity=0.9
        )
        return server_mod._compute_reconcile_outcome(
            registry, "new text", candidate, metadata=new_label
        )

    def test_a_labelled_candidate_is_not_sent_and_builds_no_client(
        self, store: SQLiteDocumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry, built = _reconcile_registry(store, monkeypatch, local=False)
        outcome = self._outcome(
            registry, store, candidate_label=_label("confidential"), new_label={}
        )
        assert outcome.decision is ReconcileDecision.ADD
        assert outcome.fallback is True
        assert outcome.fallback_reason is ReconcileFallbackReason.EGRESS_WITHHELD
        assert built == []

    def test_a_labelled_incoming_memory_is_not_sent_either(
        self, store: SQLiteDocumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry, built = _reconcile_registry(store, monkeypatch, local=False)
        outcome = self._outcome(
            registry, store, candidate_label={}, new_label=_label("restricted")
        )
        assert outcome.fallback_reason is ReconcileFallbackReason.EGRESS_WITHHELD
        assert built == []

    def test_a_candidate_that_vanished_is_withheld_not_sent(
        self, store: SQLiteDocumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry, built = _reconcile_registry(store, monkeypatch, local=False)
        outcome = server_mod._compute_reconcile_outcome(
            registry,
            "new text",
            ReconcileCandidate(doc_id="gone", content="x", similarity=0.9),
            metadata={},
        )
        assert outcome.fallback_reason is ReconcileFallbackReason.EGRESS_WITHHELD
        assert built == []

    def test_unlabelled_pair_is_judged_as_before(
        self, store: SQLiteDocumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry, built = _reconcile_registry(store, monkeypatch, local=False)
        outcome = self._outcome(registry, store, candidate_label={}, new_label={})
        assert outcome.fallback is False
        assert outcome.decision is ReconcileDecision.NOOP
        assert built == [LLMConsumer.RECONCILE]

    def test_a_route_declared_local_judges_a_labelled_pair(
        self, store: SQLiteDocumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry, built = _reconcile_registry(store, monkeypatch, local=True)
        outcome = self._outcome(
            registry,
            store,
            candidate_label=_label("restricted"),
            new_label=_label("confidential"),
        )
        assert outcome.fallback is False
        assert built == [LLMConsumer.RECONCILE]

    def test_a_withheld_pair_is_not_a_judge_failure(self) -> None:
        """The skip is a decision about the route, never counted as a failure."""
        assert ReconcileFallbackReason.EGRESS_WITHHELD not in JUDGE_FAILURE_REASONS
        assert ReconcileFallbackReason.EGRESS_WITHHELD.value == EGRESS_WITHHELD_REASON

    @pytest.mark.parametrize("local", [False, True])
    def test_the_gate_reads_the_reconcile_route_not_another_consumers(
        self, store: SQLiteDocumentStore, monkeypatch: pytest.MonkeyPatch, local: bool
    ) -> None:
        """Every other consumer sits on the opposite tier, so a wrong read flips."""
        registry, built = _reconcile_registry(store, monkeypatch, local=False)
        registry.llm_route.side_effect = route_from(
            per_consumer_config(LLMConsumer.RECONCILE, local=local)
        )
        outcome = self._outcome(
            registry, store, candidate_label=_label("confidential"), new_label={}
        )
        assert (outcome.fallback_reason is None) is local
        if not local:
            assert outcome.fallback_reason is ReconcileFallbackReason.EGRESS_WITHHELD
        assert built == ([LLMConsumer.RECONCILE] if local else [])


class TestClientFallbackRespectsALocalDeclaration:
    """``_build_llm_client`` takes the env key only for a route not declared local."""

    @staticmethod
    def _registry(config: dict[str, Any]) -> Any:
        return SimpleNamespace(
            llm_route=route_from(config), build_llm_client=lambda *, consumer: None
        )

    def test_a_local_route_never_falls_back_to_a_hosted_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unbuildable local client must not become ``OPENAI_API_KEY``."""
        reached: list[str] = []
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
        monkeypatch.setattr(
            server_mod, "_build_llm_client_from_env", lambda: reached.append("env")
        )
        client = server_mod._build_llm_client(
            self._registry({"local": True}),
            LLMConsumer.RECONCILE,  # type: ignore[arg-type]
        )
        assert client is None
        assert reached == []

    def test_an_undeclared_route_still_falls_back_to_the_env_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The contrast: the same unbuildable client, no declaration."""
        sentinel = object()
        reached: list[str] = []

        def _from_env() -> object:
            reached.append("env")
            return sentinel

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
        monkeypatch.setattr(server_mod, "_build_llm_client_from_env", _from_env)
        client = server_mod._build_llm_client(
            self._registry({}),
            LLMConsumer.RECONCILE,  # type: ignore[arg-type]
        )
        assert client is sentinel
        assert reached == ["env"]


_OPERATIONS_GUIDE = (
    Path(__file__).resolve().parents[3] / "docs" / "agent-guide" / "operations.md"
)


class TestOperationsGuideExample:
    """The ``llm:`` example the guide shows an operator must load."""

    @staticmethod
    def _example() -> dict[str, Any]:
        text = _OPERATIONS_GUIDE.read_text(encoding="utf-8")
        section = text.split("## Keeping labelled content off a hosted model", 1)[1]
        block = section.split("```yaml\n", 1)[1].split("\n```", 1)[0]
        return yaml.safe_load(block)["llm"]

    def test_the_example_passes_routing_validation(self) -> None:
        validate_llm_routing(self._example())

    def test_the_hosted_tier_is_buildable_and_not_local(self) -> None:
        route = resolve_llm_route(self._example(), LLMConsumer.ENRICHMENT)
        assert route.tier == "hosted"
        assert route.local is False
        assert route.block["api_key_env"]

    def test_an_unrouted_consumer_keeps_the_parents_declaration(self) -> None:
        route = resolve_llm_route(self._example(), LLMConsumer.MEMORY_EXTRACTION)
        assert route.local is True

    def test_every_consumer_builds_a_client_from_the_example(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A copied example must yield a client, not a silent ``None``.

        The credential names come from the example itself, so an example that
        names none leaves the variable unset and its consumers unbuilt.
        """
        example = self._example()
        built: list[dict[str, Any]] = []

        class _Fake:
            def __init__(self, **kwargs: Any) -> None:
                built.append(kwargs)

        monkeypatch.setattr("trellis.llm.providers.openai.OpenAIClient", _Fake)
        names = [example.get("api_key_env")] + [
            tier.get("api_key_env") for tier in example["tiers"].values()
        ]
        for name in names:
            if name:
                monkeypatch.setenv(name, "placeholder")
        config_dir = tmp_path / ".trellis"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            yaml.safe_dump({"knowledge": {}, "llm": example})
        )
        registry = StoreRegistry.from_config_dir(
            config_dir=config_dir, data_dir=tmp_path / "data"
        )
        for consumer in LLMConsumer:
            before = len(built)
            assert registry.build_llm_client(consumer=consumer) is not None, (
                consumer.value
            )
            assert len(built) == before + 1
            hosted = consumer is LLMConsumer.ENRICHMENT
            expected = example["tiers"]["hosted"] if hosted else example
            assert built[-1]["base_url"] == expected["base_url"], consumer.value


class TestPerConsumerConfig:
    """The helper the route-selection tests rely on must answer as designed.

    A config that fails validation reads as not local for every consumer,
    which would make every "hosted" arm of those tests pass for no reason.
    """

    @pytest.mark.parametrize("local", [False, True])
    @pytest.mark.parametrize("target", list(LLMConsumer))
    def test_each_consumer_gets_the_answer_its_tier_gives(
        self, target: LLMConsumer, local: bool
    ) -> None:
        config = per_consumer_config(target, local=local)
        for member in LLMConsumer:
            route = resolve_llm_route(config, member)
            assert route.local is ((member is target) == local)


class _FlakyStore:
    """A real store that raises for chosen ids and delegates for the rest."""

    def __init__(self, inner: SQLiteDocumentStore, failing: dict[str, Exception]):
        self._inner = inner
        self._failing = failing

    def get(self, doc_id: str) -> Any:
        if doc_id in self._failing:
            raise self._failing[doc_id]
        return self._inner.get(doc_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class TestStoredVerdict:
    """``stored_verdict``: a gate over a document named by id, failing closed.

    WITHHELD is a property of the row and holds on a retry; UNREADABLE is a
    store that could not answer. Both keep the content off the route.
    """

    def test_the_label_on_the_stored_row_decides(
        self, store: SQLiteDocumentStore
    ) -> None:
        store.put("secret", "body", _label("confidential"))
        store.put("plain", "body", {})
        store.put("shared", "body", _label("public"))
        gate = EgressGate(store, local=False)
        assert gate.stored_verdict("secret") is EgressVerdict.WITHHELD
        assert gate.stored_verdict("plain") is EgressVerdict.PERMITTED
        assert gate.stored_verdict("shared") is EgressVerdict.PERMITTED

    def test_an_id_with_no_row_names_nothing_that_carries_a_label(
        self, store: SQLiteDocumentStore
    ) -> None:
        gate = EgressGate(store, local=False)
        assert gate.stored_verdict("entity:x") is EgressVerdict.PERMITTED

    def test_a_chunk_shaped_id_with_no_parent_row_is_withheld_not_unreadable(
        self, store: SQLiteDocumentStore
    ) -> None:
        orphan = chunk_doc_id("gone", 0)
        gate = EgressGate(store, local=False)
        assert gate.stored_verdict(orphan) is EgressVerdict.WITHHELD

    def test_a_chunk_of_a_labelled_parent_is_withheld_and_of_a_plain_one_is_not(
        self, store: SQLiteDocumentStore
    ) -> None:
        store.put("secret-parent", "body", _label("restricted"))
        store.put("plain-parent", "body", {})
        store.put(chunk_doc_id("secret-parent", 0), "c", {})
        store.put(chunk_doc_id("plain-parent", 0), "c", {})
        gate = EgressGate(store, local=False)
        assert (
            gate.stored_verdict(chunk_doc_id("secret-parent", 0))
            is EgressVerdict.WITHHELD
        )
        assert (
            gate.stored_verdict(chunk_doc_id("plain-parent", 0))
            is EgressVerdict.PERMITTED
        )

    def test_a_chunk_whose_parent_fetch_fails_is_unreadable_not_withheld(
        self, store: SQLiteDocumentStore
    ) -> None:
        store.put(chunk_doc_id("parent", 0), "c", {})
        flaky = _FlakyStore(store, {"parent": RuntimeError("store down")})
        gate = EgressGate(flaky, local=False)  # type: ignore[arg-type]
        assert (
            gate.stored_verdict(chunk_doc_id("parent", 0)) is EgressVerdict.UNREADABLE
        )

    def test_a_store_that_cannot_answer_is_unreadable_not_withheld(self) -> None:
        broken = MagicMock()
        broken.get.side_effect = RuntimeError("store down")
        assert EgressGate(broken, local=False).stored_verdict("any") is (
            EgressVerdict.UNREADABLE
        )

    def test_a_row_whose_own_bytes_do_not_parse_is_withheld(self) -> None:
        bad = MagicMock()
        bad.get.side_effect = json.JSONDecodeError("bad", "{", 0)
        assert EgressGate(bad, local=False).stored_verdict("any") is (
            EgressVerdict.WITHHELD
        )

    def test_a_row_with_non_mapping_metadata_is_withheld(self) -> None:
        odd = MagicMock()
        odd.get.return_value = {"metadata": ["not", "a", "mapping"]}
        assert EgressGate(odd, local=False).stored_verdict("any") is (
            EgressVerdict.WITHHELD
        )

    def test_a_local_gate_reads_nothing(self) -> None:
        untouched = MagicMock()
        gate = EgressGate(untouched, local=True)
        assert gate.stored_verdict("any") is EgressVerdict.PERMITTED
        untouched.get.assert_not_called()

    def test_the_lazy_gate_opens_the_store_only_on_a_route_that_reads_it(
        self,
    ) -> None:
        opened: list[str] = []

        def open_store() -> Any:
            opened.append("opened")
            return MagicMock()

        local = MagicMock()
        local.llm_route.return_value = SimpleNamespace(local=True)
        cloud = MagicMock()
        cloud.llm_route.return_value = SimpleNamespace(local=False)
        consumer = LLMConsumer.PRECEDENT_MINING

        assert EgressGate.for_route_lazy(local, consumer, open_store).local is True
        assert opened == []
        assert EgressGate.for_route_lazy(cloud, consumer, open_store).local is False
        assert opened == ["opened"]
