"""Local-model precedent distill and merge: the tier check, strict parse, judged ops."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from tests.precedent_candidates import SENTINEL, recovering_trace
from trellis.learning import precedent_local as pl
from trellis.llm.egress import EgressGate
from trellis.llm.routing import LLMConsumer
from trellis.llm.types import LLMResponse
from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.precedent_record import (
    MAX_LIST_ITEMS,
    PrecedentAction,
    PrecedentEvidence,
    PrecedentRecord,
    PrecedentSituation,
)
from trellis.schemas.trace import EvidenceRef, Outcome, Trace, TraceContext, TraceStep
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

if TYPE_CHECKING:
    from pathlib import Path

LESSON = {
    "lessons": [
        {
            "kind": "avoid",
            "action": f"do not retry the bulk loader without a lock {SENTINEL}",
            "trigger": "a bulk load stalls on a held lock",
            "because": f"the loader waits forever {SENTINEL}",
            "confidence": 0.9,
        }
    ]
}


class FakeClient:
    """Answers each ``generate`` with the next reply; counts the calls."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.prompts: list[str] = []

    async def generate(self, **kwargs) -> LLMResponse:
        self.calls += 1
        self.prompts.append(kwargs["messages"][-1].content)
        reply = self.replies.pop(0) if self.replies else json.dumps({"lessons": []})
        return LLMResponse(content=reply, model="fake-reported")


def _trace(trace_id: str = "t-smooth", *, evidence: list[str] | None = None) -> Trace:
    """A trace the deterministic tier finds nothing in."""
    return Trace(
        trace_id=trace_id,
        source=TraceSource.AGENT,
        intent="bulk load the nightly ledger",
        steps=[TraceStep(step_type="tool_call", name="load", args={"k": 1})],
        outcome=Outcome(status=OutcomeStatus.SUCCESS, summary=f"done {SENTINEL}"),
        evidence_used=[EvidenceRef(evidence_id=e) for e in evidence or []],
        context=TraceContext(domain="data"),
    )


class _Gate:
    """A gate that withholds the ids it is given."""

    def __init__(self, withheld: set[str]) -> None:
        self.withheld = withheld
        self.local = False

    def stored_verdict(self, doc_id: str):
        from trellis.llm.egress import EgressVerdict

        return (
            EgressVerdict.WITHHELD
            if doc_id in self.withheld
            else EgressVerdict.PERMITTED
        )


def _runner(
    client: FakeClient,
    *,
    local: bool = True,
    event_log=None,
    consumer: LLMConsumer = LLMConsumer.PRECEDENT_DISTILL,
    withheld: set[str] | None = None,
) -> pl.LocalModelRunner:
    return pl.LocalModelRunner(
        consumer=consumer,
        client=client,  # type: ignore[arg-type]
        model="label-m",
        local=local,
        gate=_Gate(withheld or set()),  # type: ignore[arg-type]
        event_log=event_log,
    )


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def _judged(log) -> list[dict]:
    return [
        e.payload
        for e in log.get_events(event_type=EventType.MEMORY_OP_JUDGED, limit=100)
    ]


# --- the tier check -------------------------------------------------------


async def test_cloud_route_is_refused_with_no_call(event_log) -> None:
    client = FakeClient(json.dumps(LESSON))
    runner = _runner(client, local=False, event_log=event_log)
    with pytest.raises(pl.RouteNotLocalError) as caught:
        await pl.distill_trace_local(runner, _trace())
    assert client.calls == 0
    assert runner.calls == 0
    assert runner.refusals == {"route_not_local": 1}
    assert caught.value.as_dict() == {
        "reason": "route_not_local",
        "consumer": "precedent_distill",
    }
    assert _judged(event_log) == []


async def test_merge_on_a_cloud_route_is_refused_with_no_call() -> None:
    client = FakeClient('{"verdict": "same"}')
    runner = _runner(client, local=False, consumer=LLMConsumer.PRECEDENT_MERGE)
    a, b = _pair()
    with pytest.raises(pl.RouteNotLocalError):
        await pl.merge_pair_local(runner, a, b)
    assert client.calls == 0


async def test_a_local_route_is_asked() -> None:
    client = FakeClient(json.dumps(LESSON))
    result = await pl.distill_trace_local(_runner(client), _trace())
    assert client.calls == 1
    assert result.outcome == "lesson"


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"provider": "openai", "model": "m"}, False),
        (
            {
                "provider": "openai",
                "model": "m",
                "local": True,
                "base_url": "http://localhost:11434/v1",
            },
            True,
        ),
        # An unrouted consumer inherits the parent block: cloud stays cloud.
        (
            {
                "provider": "openai",
                "model": "m",
                "tiers": {"alt": {"model": "other"}},
                "routes": {"enrichment": "alt"},
            },
            False,
        ),
        (
            {
                "provider": "openai",
                "model": "m",
                "local": True,
                "base_url": "http://localhost:11434/v1",
                "tiers": {"alt": {"model": "other"}},
                "routes": {"enrichment": "alt"},
            },
            True,
        ),
        # The documented stanza: a tier on the same endpoint inherits the flag.
        (
            {
                "provider": "openai",
                "model": "placeholder-model",
                "base_url": "http://localhost:11434/v1",
                "api_key_env": "LOCAL_LLM_API_KEY",
                "local": True,
                "tiers": {"local-judge": {"model": "placeholder-larger-model"}},
                "routes": {"precedent_distill": "local-judge"},
            },
            True,
        ),
        # A tier that moves the endpoint is cloud again unless it says local.
        (
            {
                "provider": "openai",
                "model": "m",
                "local": True,
                "base_url": "http://localhost:11434/v1",
                "tiers": {"far": {"base_url": "http://example.invalid/v1"}},
                "routes": {"precedent_distill": "far"},
            },
            False,
        ),
        (
            {
                "provider": "openai",
                "model": "m",
                "local": True,
                "base_url": "http://localhost:11434/v1",
                "tiers": {"near": {"model": "other", "local": True}},
                "routes": {"precedent_distill": "near"},
            },
            True,
        ),
    ],
)
def test_for_consumer_reads_the_resolved_local_flag(config, expected) -> None:
    from trellis.llm.routing import resolve_llm_route

    registry = SimpleNamespace(
        llm_route=lambda consumer=None: resolve_llm_route(config, consumer)
    )
    runner = pl.LocalModelRunner.for_consumer(
        registry,  # type: ignore[arg-type]
        LLMConsumer.PRECEDENT_DISTILL,
        client=FakeClient(),  # type: ignore[arg-type]
        model="m",
        document_store=None,
        event_log=None,
    )
    assert runner.local is expected
    assert isinstance(runner.gate, EgressGate)


def test_an_unresolvable_routing_block_reads_as_not_local() -> None:
    from trellis.llm.routing import LLMRoutingError

    def broken(_consumer=None):
        msg = "bad block"
        raise LLMRoutingError(msg)

    runner = pl.LocalModelRunner.for_consumer(
        SimpleNamespace(llm_route=broken),  # type: ignore[arg-type]
        LLMConsumer.PRECEDENT_MERGE,
        client=FakeClient(),  # type: ignore[arg-type]
        model="m",
        document_store=None,
        event_log=None,
    )
    assert runner.local is False


# --- the egress gate -------------------------------------------------------


async def test_a_withheld_stored_document_keeps_the_trace_off_the_model() -> None:
    client = FakeClient(json.dumps(LESSON))
    runner = _runner(client, withheld={"doc-secret"})
    result = await pl.distill_trace_local(
        runner, _trace(evidence=["doc-ok", "doc-secret"])
    )
    assert client.calls == 0
    assert result.skips == ("egress_withheld",)


# --- strict parse ----------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "not json at all",
        "```json\n" + json.dumps(LESSON) + "\n```",
        "Here you go: " + json.dumps(LESSON),
        json.dumps({"lessons": [{**LESSON["lessons"][0], "extra": 1}]}),
        json.dumps({"lessons": LESSON["lessons"], "note": "x"}),
        json.dumps({"lessons": [{**LESSON["lessons"][0], "confidence": "0.9"}]}),
        json.dumps({"lessons": [{**LESSON["lessons"][0], "kind": "maybe"}]}),
        json.dumps({"lessons": [{**LESSON["lessons"][0], "confidence": 1.5}]}),
        json.dumps({"lessons": LESSON["lessons"] * 4}),
        json.dumps([]),
        "",
    ],
)
async def test_anything_but_the_exact_shape_is_unparsed(reply, event_log) -> None:
    runner = _runner(FakeClient(reply), event_log=event_log)
    result = await pl.distill_trace_local(runner, _trace())
    assert result.outcome == "unparsed"
    assert result.records == ()
    assert [p["decision"] for p in _judged(event_log)] == ["unparsed"]


async def test_a_valid_lesson_becomes_a_local_model_record() -> None:
    result = await pl.distill_trace_local(
        _runner(FakeClient(json.dumps(LESSON))), _trace(evidence=["doc-1"])
    )
    (record,) = result.records
    assert record.provenance_tier == "local_model"
    assert record.rule == pl.RULE_LOCAL_LESSON
    assert record.distiller_version == pl.DISTILL_PROMPT_VERSION
    assert record.confidence == pl.CONFIDENCE_CAP  # the model said 0.9
    assert record.evidence.trace_ids == ["t-smooth"]
    assert record.situation.entity_ids == ["doc-1"]
    assert record.candidate_id == record.expected_candidate_id()


async def test_no_lesson_is_a_valid_answer(event_log) -> None:
    runner = _runner(FakeClient('{"lessons": []}'), event_log=event_log)
    result = await pl.distill_trace_local(runner, _trace())
    assert (result.outcome, result.records, result.called) == ("no_lesson", (), True)
    assert [p["decision"] for p in _judged(event_log)] == ["no_lesson"]


async def test_a_lesson_that_restates_the_intent_is_dropped_and_counted() -> None:
    restating = {
        "lessons": [{**LESSON["lessons"][0], "action": "bulk load the nightly ledger"}]
    }
    result = await pl.distill_trace_local(
        _runner(FakeClient(json.dumps(restating))), _trace()
    )
    assert result.records == ()
    assert result.skips == ("restates_intent",)


# --- the deterministic tier wins -----------------------------------------


async def test_a_trace_the_deterministic_tier_covers_is_skipped_with_no_call() -> None:
    client = FakeClient(json.dumps(LESSON))
    result = await pl.distill_trace_local(_runner(client), recovering_trace())
    assert client.calls == 0
    assert result.skips == ("deterministic_covered",)


async def test_a_trace_with_nothing_beyond_its_intent_is_skipped() -> None:
    bare = Trace(
        trace_id="t-bare",
        source=TraceSource.AGENT,
        intent="do a thing",
        context=TraceContext(),
    )
    client = FakeClient(json.dumps(LESSON))
    result = await pl.distill_trace_local(_runner(client), bare)
    assert client.calls == 0
    assert result.skips == ("no_content",)


# --- the judged op and its sentinel --------------------------------------


async def test_one_leak_safe_judged_event_per_call(event_log) -> None:
    trace = _trace(evidence=["doc-1"])
    reply = json.dumps(LESSON)
    runner = _runner(FakeClient(reply), event_log=event_log)
    await pl.distill_trace_local(runner, trace)
    rows = event_log.get_events(event_type=EventType.MEMORY_OP_JUDGED, limit=10)
    assert len(rows) == 1
    event = rows[0]
    assert event.source == "learning:precedent-distill/v1"
    payload = event.payload
    assert payload["op_type"] == "distillation"
    assert payload["decision"] == "lesson"
    assert payload["model_id"] == "fake-reported"
    assert payload["subject_ref"] == {"ref_type": "trace", "ref_id": "t-smooth"}
    assert payload["input_digest"]["source_refs"] == ["t-smooth"]
    assert payload["input_digest"]["length"] > 0
    # Sentinel: neither the trace's text nor the model's reply is in the event.
    blob = json.dumps(payload) + event.source + (event.entity_id or "")
    assert SENTINEL not in blob
    assert "bulk load the nightly ledger" not in blob
    assert "held lock" not in blob
    assert runner.judged == {"distillation": 1}


async def test_the_prompt_marks_cuts_and_bounds_its_input() -> None:
    long_intent = "migrate " + "x" * 5000
    trace = Trace(
        trace_id="t-long",
        source=TraceSource.AGENT,
        intent=long_intent,
        steps=[
            TraceStep(step_type="tool_call", name=f"s{i}", error="e" * 900)
            for i in range(30)
        ],
        outcome=Outcome(status=OutcomeStatus.FAILURE, summary="s" * 3000),
        context=TraceContext(domain="data"),
    )
    client = FakeClient('{"lessons": []}')
    await pl.distill_trace_local(
        _runner(client), trace, deterministic=pl.DistillResult()
    )
    (prompt,) = client.prompts
    assert long_intent not in prompt
    assert "more step(s) not shown" in prompt
    assert len(prompt) < 8000


# --- merge ----------------------------------------------------------------


def _record(
    suffix: str,
    *,
    kind="avoid",
    entity_ids=None,
    trigger="a bulk load stalls on a held lock",
    trace_id=None,
    confidence=0.4,
    family="load data",
) -> PrecedentRecord:
    return PrecedentRecord.derive(
        rule=pl.RULE_LOCAL_LESSON,
        situation=PrecedentSituation(
            intent_family=family, entity_ids=entity_ids or [], trigger=trigger
        ),
        action=PrecedentAction(kind=kind, text=f"act {suffix}"),
        because=f"why {suffix}",
        evidence=PrecedentEvidence(
            trace_ids=[trace_id or f"trace-{suffix}"], step_indexes=[0]
        ),
        confidence=confidence,
        provenance_tier="local_model",
        distiller_version=pl.DISTILL_PROMPT_VERSION,
    )


def _pair() -> tuple[PrecedentRecord, PrecedentRecord]:
    return _record("a", entity_ids=["doc-1"]), _record("b", entity_ids=["doc-1"])


def test_pairing_is_deterministic_and_needs_family_kind_and_overlap() -> None:
    a, b = _pair()
    other_family = _record("c", entity_ids=["doc-1"], family="other")
    other_kind = _record("d", entity_ids=["doc-1"], kind="do")
    no_overlap = _record("e", trigger="completely unrelated words here")
    pool = [no_overlap, other_kind, b, other_family, a]
    pairs = pl.candidate_pairs(pool, limit=10)
    assert [(x.candidate_id, y.candidate_id) for x, y in pairs] == [
        tuple(sorted((a.candidate_id, b.candidate_id)))
    ]
    assert pl.candidate_pairs(list(reversed(pool)), limit=10) == pairs


def test_a_shared_trigger_pairs_without_a_shared_entity() -> None:
    a = _record("a", trigger="the bulk load stalls on a held lock")
    b = _record("b", trigger="bulk load stalls on held lock again")
    assert len(pl.candidate_pairs([a, b], limit=5)) == 1


def test_the_pair_limit_bounds_the_calls() -> None:
    pool = [_record(str(i), entity_ids=["doc-1"]) for i in range(6)]
    assert len(pl.candidate_pairs(pool, limit=2)) == 2
    assert pl.candidate_pairs(pool, limit=0) == []


def test_a_candidate_a_merge_already_cites_leaves_the_pool() -> None:
    a, b = _pair()
    merged = pl.merge_records(a, b)
    assert merged is not None
    assert pl.candidate_pairs([a, b, merged], limit=5) == []


async def test_same_produces_one_merged_record_with_the_evidence_union(
    event_log,
) -> None:
    a, b = _pair()
    before = (a.model_dump(), b.model_dump())
    runner = _runner(
        FakeClient('{"verdict": "same"}'),
        event_log=event_log,
        consumer=LLMConsumer.PRECEDENT_MERGE,
    )
    result = await pl.merge_pair_local(runner, b, a)
    merged = result.merged
    assert merged is not None
    assert result.verdict == "same"
    assert merged.evidence.merged_from == sorted([a.candidate_id, b.candidate_id])
    assert merged.evidence.trace_ids == ["trace-a", "trace-b"]
    assert merged.situation.entity_ids == ["doc-1"]
    assert merged.provenance_tier == "local_model"
    assert merged.rule == pl.RULE_LOCAL_MERGE
    assert merged.candidate_id not in {a.candidate_id, b.candidate_id}
    assert merged.candidate_id == merged.expected_candidate_id()
    # A merge never rewrites a recorded candidate.
    assert (a.model_dump(), b.model_dump()) == before
    (payload,) = _judged(event_log)
    assert payload["op_type"] == "reconciliation"
    assert payload["decision"] == "same"
    assert payload["subject_ref"]["ref_type"] == "precedent_pair"
    assert "act a" not in json.dumps(payload)


async def test_merge_takes_the_higher_confidence_parents_text() -> None:
    low = _record("low", confidence=0.2)
    high = _record("high", confidence=0.45)
    merged = pl.merge_records(low, high)
    assert merged is not None
    assert merged.action.text == "act high"
    assert merged.confidence == 0.45


@pytest.mark.parametrize(
    ("reply", "verdict"),
    [
        ('{"verdict": "different"}', "different"),
        ("same", "unparsed"),
        ('{"verdict": "Same"}', "unparsed"),
        ('{"verdict": "same", "why": "x"}', "unparsed"),
        ('```json\n{"verdict": "same"}\n```', "unparsed"),
    ],
)
async def test_only_a_same_verdict_merges(reply, verdict, event_log) -> None:
    a, b = _pair()
    runner = _runner(
        FakeClient(reply), event_log=event_log, consumer=LLMConsumer.PRECEDENT_MERGE
    )
    result = await pl.merge_pair_local(runner, a, b)
    assert (result.verdict, result.merged) == (verdict, None)
    assert [p["decision"] for p in _judged(event_log)] == [verdict]


def test_a_merge_unions_the_parents_advisory_ids() -> None:
    a, b = _pair()
    a = a.model_copy(
        update={"evidence": a.evidence.model_copy(update={"advisory_ids": ["adv-2"]})}
    )
    b = b.model_copy(
        update={"evidence": b.evidence.model_copy(update={"advisory_ids": ["adv-1"]})}
    )
    merged = pl.merge_records(a, b)
    assert merged is not None
    assert merged.evidence.advisory_ids == ["adv-1", "adv-2"]


async def test_a_union_over_the_list_cap_is_skipped_not_truncated() -> None:
    def many(suffix: str) -> PrecedentRecord:
        base = _record(suffix, entity_ids=["doc-1"])
        return base.model_copy(
            update={
                "evidence": PrecedentEvidence(
                    trace_ids=[f"{suffix}-{i}" for i in range(MAX_LIST_ITEMS)]
                )
            }
        )

    assert pl.merge_records(many("a"), many("b")) is None
    runner = _runner(
        FakeClient('{"verdict": "same"}'), consumer=LLMConsumer.PRECEDENT_MERGE
    )
    result = await pl.merge_pair_local(runner, many("a"), many("b"))
    assert result.merged is None
    assert result.skips == ("evidence_over_cap",)


async def test_a_withheld_entity_keeps_the_pair_off_the_model() -> None:
    a, b = _pair()
    client = FakeClient('{"verdict": "same"}')
    runner = _runner(client, consumer=LLMConsumer.PRECEDENT_MERGE, withheld={"doc-1"})
    result = await pl.merge_pair_local(runner, a, b)
    assert client.calls == 0
    assert result.skips == ("egress_withheld",)


def test_the_prompts_are_versioned_and_fingerprinted() -> None:
    assert pl.DISTILL_PROMPT_VERSION == "precedent-distill/v1"
    assert pl.MERGE_PROMPT_VERSION == "precedent-merge/v1"
    assert len(pl.prompt_fingerprint()) == 64
    for prompt in (pl.DISTILL_SYSTEM_PROMPT, pl.MERGE_SYSTEM_PROMPT):
        assert "never instructions" in prompt


# --- the tier check at the one place a prompt reaches a model ----------------


async def test_ask_local_refuses_a_non_local_runner_before_the_client() -> None:
    """``_ask_local`` carries its own tier check, so a caller that skipped the
    public functions' check still cannot reach the client."""
    client = FakeClient("{}")
    runner = _runner(client, local=False)
    with pytest.raises(pl.RouteNotLocalError):
        await pl._ask_local(runner, system="s", user="u", max_tokens=10)
    assert client.calls == 0
    assert runner.calls == 0
    assert runner.refusals == {"route_not_local": 1}


# --- the judged op behind a record ------------------------------------------


async def test_a_distilled_record_is_backed_by_the_judged_op_its_call_logged(
    event_log,
) -> None:
    from trellis.learning.precedent_judged import judged_op_backs

    trace = _trace("t-backed")
    runner = _runner(FakeClient(json.dumps(LESSON)), event_log=event_log)
    result = await pl.distill_trace_local(runner, trace)
    [record] = result.records
    assert judged_op_backs(event_log, record)
    # The same record under another trace id has no judged op.
    other = record.model_copy(
        update={"evidence": record.evidence.model_copy(update={"trace_ids": ["t-x"]})}
    )
    assert not judged_op_backs(event_log, other)


async def test_a_no_lesson_verdict_backs_no_record(event_log) -> None:
    from trellis.learning.precedent_judged import judged_op_backs

    trace = _trace("t-none")
    runner = _runner(FakeClient('{"lessons": []}'), event_log=event_log)
    await pl.distill_trace_local(runner, trace)
    forged = _record("forged", trace_id="t-none")
    assert not judged_op_backs(event_log, forged)


async def test_a_merged_record_is_backed_by_the_pair_verdict_its_call_logged(
    event_log,
) -> None:
    from trellis.learning.precedent_judged import judged_op_backs

    a, b = _pair()
    runner = _runner(
        FakeClient('{"verdict": "same"}'),
        event_log=event_log,
        consumer=LLMConsumer.PRECEDENT_MERGE,
    )
    result = await pl.merge_pair_local(runner, b, a)
    assert result.merged is not None
    assert judged_op_backs(event_log, result.merged)
    # A merge of another pair is not backed by this pair's verdict.
    c = _record("c", entity_ids=["doc-1"])
    other = pl.merge_records(a, c)
    assert other is not None
    assert not judged_op_backs(event_log, other)


async def test_a_different_verdict_backs_no_merged_record(event_log) -> None:
    from trellis.learning.precedent_judged import judged_op_backs

    a, b = _pair()
    runner = _runner(
        FakeClient('{"verdict": "different"}'),
        event_log=event_log,
        consumer=LLMConsumer.PRECEDENT_MERGE,
    )
    await pl.merge_pair_local(runner, a, b)
    merged = pl.merge_records(a, b)
    assert merged is not None
    assert not judged_op_backs(event_log, merged)


# --- the judged rows read as unservable --------------------------------------


async def test_a_merge_call_lands_as_unservable_not_never_served(event_log) -> None:
    from trellis.learning.judged_outcomes import summarize_judged_outcomes

    a, b = _pair()
    runner = _runner(
        FakeClient('{"verdict": "different"}'),
        event_log=event_log,
        consumer=LLMConsumer.PRECEDENT_MERGE,
    )
    await pl.merge_pair_local(runner, a, b)

    report = summarize_judged_outcomes(event_log, days=30, limit=100)

    assert report.totals.judged == 1
    assert report.totals.unservable == 1
    assert report.totals.never_served == 0
    assert report.ref_types == {"precedent_pair": 1}


async def test_a_distill_call_lands_as_unservable_not_never_served(event_log) -> None:
    from trellis.learning.judged_outcomes import summarize_judged_outcomes

    runner = _runner(FakeClient(json.dumps(LESSON)), event_log=event_log)
    await pl.distill_trace_local(runner, _trace())

    report = summarize_judged_outcomes(event_log, days=30, limit=100)

    assert report.totals.judged == 1
    assert report.totals.unservable == 1
    assert report.totals.never_served == 0
    assert report.ref_types == {"trace": 1}
