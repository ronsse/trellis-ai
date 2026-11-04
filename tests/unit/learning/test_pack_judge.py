"""The shadow pack judge: what it writes, what it will not, and how it is measured.

Every test runs on a synthetic SQLite event log and document store in
``tmp_path`` and a scripted fake :class:`LLMClient`. No test reaches a model or a
network. The calibration numbers are computed by hand in
:data:`_WORLD`'s comment, not by the code under test.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from trellis.core.pack_holdout import HOLDOUT_KEY
from trellis.learning import pack_judge
from trellis.learning.pack_judge import (
    MAX_CONSECUTIVE_LLM_ERRORS,
    MIN_GRADED_ITEMS,
    PROMPT_VERSION,
    REASON_PACK_EGRESS_WITHHELD,
    JudgedRow,
    JudgeLabel,
    PackJudgeError,
    PackJudgeRefusal,
    PackWindow,
    ServedItem,
    calibrate,
    cohens_kappa,
    judge_window,
    load_window,
    parse_instant,
    parse_label,
    prompt_fingerprint,
    read_judged_rows,
)
from trellis.llm.egress import EgressGate
from trellis.llm.types import LLMResponse, Message
from trellis.stores.base.event_log import Event, EventType, ScanCoverage
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.event_log import SQLiteEventLog

INTENT_A = "roll out the billing service to staging"
INTENT_B = "diagnose the slow checkout query"
INTENT_H = "held-out intent that must never be judged"
BODY_MARK = "DOC::"
SECRET_PHRASE = "tangerine-wombat-protocol"  # noqa: S105 - marker text, not a secret
TOKENS = 10
SINCE = datetime.now(tz=UTC) - timedelta(days=2)

# -- the hand-computed world ------------------------------------------------------
#
# Eleven served items across two served packs (10 tokens each), plus one
# held-out pack. Agents graded nine of them:
#
#   agent helpful   h1 h2 h3 h4 h5
#   agent unhelpful u1 u2 u3 u4
#   ungraded        g1 g2
#
# The scripted judge answers (the first column of each pair is the agent):
#
#   h1 helpful   h2 helpful   h3 helpful ("Helpful.")   h4 unhelpful   h5 unsure
#   u1 unhelpful u2 unhelpful u3 helpful                 u4 unsure
#   g1 helpful   g2 helpful
#
#                    judge helpful  judge unhelpful  judge unsure
#   agent helpful          3               1               1        (5)
#   agent unhelpful        1               2               1        (4)
#
#   graded overlap          9
#   agreement               (3 + 2) / 9            = 5/9
#   unhelpful-side          2 / 4                  = 0.5
#   helpful-side            3 / 5                  = 0.6
#   kappa (a=3 b=1 c=1 d=2, n=7, unsure left out):
#       observed = 5/7, expected = (4*4 + 3*3)/49 = 25/49
#       kappa = (35/49 - 25/49) / (24/49) = 10/24
#   judged tokens           110 (all eleven answered), coverage 110/110
#   judge-implied useful    helpful on h1 h2 h3 u3 g1 g2 = 6 items = 60/110
#   agent lower bound       helpful on h1..h5            = 5 items = 50/110
#   agent graded share      90/110
_ANSWERS: dict[str, str] = {
    "h1": "helpful",
    "h2": "helpful",
    "h3": "Helpful.",
    "h4": "unhelpful",
    "h5": "unsure",
    "u1": "unhelpful",
    "u2": "unhelpful",
    "u3": "helpful",
    "u4": "unsure",
    "g1": "helpful",
    "g2": "helpful",
}
_PACK_ONE = ("h1", "h2", "h3", "h4", "h5", "u1", "g1")
_PACK_TWO = ("u2", "u3", "u4", "g2")
_HELD_OUT = ("x1", "x2")


class FakeClient:
    """Scripted ``LLMClient``: answers by the item id named in the prompt."""

    def __init__(
        self, answers: dict[str, str], *, fail_on: frozenset[str] = frozenset()
    ):
        self.answers = answers
        self.fail_on = fail_on
        self.calls: list[list[Message]] = []
        self.kwargs: list[dict[str, Any]] = []

    async def generate(
        self,
        *,
        messages: list[Message],
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> LLMResponse:
        self.calls.append(messages)
        self.kwargs.append({"temperature": temperature, "max_tokens": max_tokens})
        match = re.search(rf"{BODY_MARK}(\w+)", messages[-1].content)
        assert match, "the prompt carried no item text"
        item_id = match.group(1)
        if item_id in self.fail_on:
            message = f"endpoint refused {item_id}"
            raise RuntimeError(message)
        return LLMResponse(content=self.answers[item_id], model="fake-model-x")

    def asked(self) -> list[str]:
        found = []
        for messages in self.calls:
            match = re.search(rf"{BODY_MARK}(\w+)", messages[-1].content)
            assert match
            found.append(match.group(1))
        return found


def _append(
    log: SQLiteEventLog,
    event_type: EventType,
    *,
    entity_id: str,
    payload: dict[str, Any],
    at: datetime | None = None,
) -> None:
    moment = at or datetime.now(tz=UTC) - timedelta(hours=1)
    log.append(
        Event(
            event_type=event_type,
            source="test",
            entity_id=entity_id,
            occurred_at=moment,
            recorded_at=moment,
            payload=payload,
        )
    )


def seed_pack(
    log: SQLiteEventLog,
    pack_id: str,
    *,
    intent: str | None,
    item_ids: tuple[str, ...],
    held_out: bool = False,
    at: datetime | None = None,
) -> None:
    payload: dict[str, Any] = {
        "intent": intent,
        "injected_items": [
            {"item_id": item, "rank": rank, "estimated_tokens": TOKENS}
            for rank, item in enumerate(item_ids, start=1)
        ],
    }
    if held_out:
        payload[HOLDOUT_KEY] = True
    _append(log, EventType.PACK_ASSEMBLED, entity_id=pack_id, payload=payload, at=at)


def seed_feedback(
    log: SQLiteEventLog,
    pack_id: str,
    *,
    helpful: tuple[str, ...] = (),
    unhelpful: tuple[str, ...] = (),
) -> None:
    _append(
        log,
        EventType.FEEDBACK_RECORDED,
        entity_id=pack_id,
        payload={
            "pack_id": pack_id,
            "feedback_id": f"fb-{pack_id}",
            "rating": 0.5,
            "success": True,
            "helpful_item_ids": list(helpful),
            "unhelpful_item_ids": list(unhelpful),
        },
    )


def seed_doc(docs: SQLiteDocumentStore, item_id: str, **metadata: Any) -> None:
    docs.put(
        item_id,
        f"{BODY_MARK}{item_id} {SECRET_PHRASE} body text for {item_id}",
        metadata or None,
    )


@pytest.fixture
def log(tmp_path: Path) -> SQLiteEventLog:
    return SQLiteEventLog(tmp_path / "events.db")


@pytest.fixture
def docs(tmp_path: Path) -> SQLiteDocumentStore:
    return SQLiteDocumentStore(tmp_path / "docs.db")


@pytest.fixture
def world(log: SQLiteEventLog, docs: SQLiteDocumentStore) -> None:
    seed_pack(log, "pack-one", intent=INTENT_A, item_ids=_PACK_ONE)
    seed_pack(log, "pack-two", intent=INTENT_B, item_ids=_PACK_TWO)
    seed_pack(log, "pack-held", intent=INTENT_H, item_ids=_HELD_OUT, held_out=True)
    seed_feedback(
        log, "pack-one", helpful=("h1", "h2", "h3", "h4", "h5"), unhelpful=("u1",)
    )
    seed_feedback(log, "pack-two", unhelpful=("u2", "u3", "u4"))
    seed_feedback(log, "pack-held", helpful=("x1",))
    for item in (*_PACK_ONE, *_PACK_TWO, *_HELD_OUT):
        seed_doc(docs, item)


LOCAL = EgressGate(None, local=True)


def run_judge(
    log: SQLiteEventLog,
    docs: SQLiteDocumentStore,
    client: FakeClient,
    out: Path,
    *,
    model: str = "judge-a",
    limit: int = 0,
    gate: EgressGate = LOCAL,
) -> pack_judge.JudgeRunSummary:
    import asyncio

    window = load_window(log, since=SINCE)
    return asyncio.run(
        judge_window(
            window,
            document_store=docs,
            client=client,
            gate=gate,
            model=model,
            out_path=out,
            limit=limit,
        )
    )


# -- the answer ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "label"),
    [
        ("helpful", JudgeLabel.HELPFUL),
        ("Helpful.", JudgeLabel.HELPFUL),
        ("  UNHELPFUL\n", JudgeLabel.UNHELPFUL),
        ("**unsure**", JudgeLabel.UNSURE),
        ('"helpful"', JudgeLabel.HELPFUL),
        ("not helpful", JudgeLabel.UNPARSED),
        ("helpful, because it names the service", JudgeLabel.UNPARSED),
        ("helpful unhelpful", JudgeLabel.UNPARSED),
        ("maybe", JudgeLabel.UNPARSED),
        ("", JudgeLabel.UNPARSED),
    ],
)
def test_parse_label_is_strict(answer: str, label: JudgeLabel) -> None:
    assert parse_label(answer) is label


def test_the_prompt_cannot_change_without_a_version_bump() -> None:
    pinned = {
        "pack-judge/v1": (
            "d8bd629dd73e0fc01e08160f30b28e7c5e22c2acd8576744aa8e8a1c53f7855c"
        ),
    }
    assert PROMPT_VERSION in pinned, "a new PROMPT_VERSION needs its fingerprint pinned"
    assert prompt_fingerprint() == pinned[PROMPT_VERSION], (
        "the prompt, a truncation limit or the label set changed: bump "
        "PROMPT_VERSION and pin the new fingerprint"
    )


# -- the judge phase -----------------------------------------------------------------


@pytest.mark.usefixtures("world")
def test_judge_writes_one_labelled_row_per_served_pair(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    out = tmp_path / "judged.jsonl"
    client = FakeClient(_ANSWERS)
    summary = run_judge(log, docs, client, out)

    rows = read_judged_rows(out)
    by_item = {row.item_id: row for row in rows}
    assert set(by_item) == set(_ANSWERS)
    assert {row.item_id: row.label.value for row in rows} == {
        "h1": "helpful",
        "h2": "helpful",
        "h3": "helpful",
        "h4": "unhelpful",
        "h5": "unsure",
        "u1": "unhelpful",
        "u2": "unhelpful",
        "u3": "helpful",
        "u4": "unsure",
        "g1": "helpful",
        "g2": "helpful",
    }
    first = by_item["h1"]
    assert first.pack_id == "pack-one"
    assert first.model == "judge-a"
    assert first.reported_model == "fake-model-x"
    assert first.prompt_version == PROMPT_VERSION
    assert first.item_tokens == TOKENS
    assert first.judged_at.tzinfo is not None
    assert summary.status == "ok"
    assert (summary.helpful, summary.unhelpful, summary.unsure) == (6, 3, 2)
    assert summary.rows_written == 11
    assert summary.window_items == 11


@pytest.mark.usefixtures("world")
def test_held_out_packs_are_dropped_and_never_asked_about(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    out = tmp_path / "judged.jsonl"
    client = FakeClient({**_ANSWERS, "x1": "helpful", "x2": "helpful"})
    summary = run_judge(log, docs, client, out)

    assert summary.held_out_packs == 1
    assert summary.packs == 2
    assert not set(client.asked()) & set(_HELD_OUT)
    assert not {row.item_id for row in read_judged_rows(out)} & set(_HELD_OUT)


@pytest.mark.usefixtures("world")
def test_the_question_is_asked_with_the_pinned_settings(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    client = FakeClient(_ANSWERS)
    run_judge(log, docs, client, tmp_path / "judged.jsonl")

    assert {tuple(sorted(kw.items())) for kw in client.kwargs} == {
        (
            ("max_tokens", pack_judge.MAX_ANSWER_TOKENS),
            ("temperature", pack_judge.JUDGE_TEMPERATURE),
        )
    }
    system, user = client.calls[0]
    assert system.content == pack_judge.JUDGE_SYSTEM_PROMPT
    assert INTENT_A in user.content or INTENT_B in user.content


def test_a_long_item_is_cut_before_it_is_sent(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    seed_pack(log, "p", intent=INTENT_A, item_ids=("long1",))
    docs.put("long1", f"{BODY_MARK}long1 " + "x" * 50_000)
    client = FakeClient({"long1": "helpful"})
    run_judge(log, docs, client, tmp_path / "judged.jsonl")

    sent = client.calls[0][-1].content
    assert len(sent) < pack_judge.MAX_ITEM_CHARS + pack_judge.MAX_INTENT_CHARS + 400


def test_a_missing_item_is_recorded_as_unjudgeable_not_skipped(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    seed_pack(log, "p", intent=INTENT_A, item_ids=("here", "gone"))
    seed_doc(docs, "here")
    out = tmp_path / "judged.jsonl"
    summary = run_judge(log, docs, FakeClient({"here": "helpful"}), out)

    rows = {row.item_id: row for row in read_judged_rows(out)}
    assert rows["gone"].label is JudgeLabel.UNJUDGEABLE
    assert rows["gone"].reason == "missing"
    assert rows["here"].label is JudgeLabel.HELPFUL
    assert summary.unjudgeable == {"missing": 1}


def test_an_unparsed_answer_is_kept_as_unparsed_and_counted(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    seed_pack(log, "p", intent=INTENT_A, item_ids=("chatty", "terse"))
    seed_doc(docs, "chatty")
    seed_doc(docs, "terse")
    out = tmp_path / "judged.jsonl"
    client = FakeClient(
        {
            "chatty": "I think this is probably helpful because of the title",
            "terse": "unhelpful",
        }
    )
    summary = run_judge(log, docs, client, out)

    rows = {row.item_id: row for row in read_judged_rows(out)}
    assert rows["chatty"].label is JudgeLabel.UNPARSED
    assert rows["terse"].label is JudgeLabel.UNHELPFUL
    assert summary.unparsed == 1
    assert summary.unhelpful == 1
    assert summary.helpful == 0  # never coerced to the label it mentions


def test_an_item_with_no_intent_or_no_text_is_unjudgeable(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    seed_pack(log, "no-intent", intent=None, item_ids=("a",))
    seed_pack(log, "no-text", intent=INTENT_A, item_ids=("b",))
    seed_doc(docs, "a")
    docs.put("b", "   ")
    out = tmp_path / "judged.jsonl"
    client = FakeClient({})
    summary = run_judge(log, docs, client, out)

    assert client.calls == []
    assert summary.unjudgeable == {"empty": 1, "no_intent": 1}


def test_a_document_above_internal_is_withheld_from_a_remote_route(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    seed_pack(log, "p", intent=INTENT_A, item_ids=("open", "secret"))
    seed_doc(docs, "open")
    seed_doc(docs, "secret", data_classification={"sensitivity": "confidential"})
    client = FakeClient({"open": "helpful", "secret": "helpful"})
    out = tmp_path / "remote.jsonl"
    run_judge(log, docs, client, out, gate=EgressGate(docs, local=False))

    # The pack served a confidential document, so its intent stays home too.
    assert client.calls == []
    rows = {row.item_id: row for row in read_judged_rows(out)}
    assert rows["secret"].label is JudgeLabel.UNJUDGEABLE
    assert rows["secret"].reason == REASON_PACK_EGRESS_WITHHELD
    assert rows["open"].label is JudgeLabel.UNJUDGEABLE

    local = FakeClient({"open": "helpful", "secret": "helpful"})
    run_judge(
        log, docs, local, tmp_path / "local.jsonl", gate=EgressGate(docs, local=True)
    )
    assert sorted(local.asked()) == ["open", "secret"]


@pytest.mark.usefixtures("world")
def test_a_run_resumes_by_pair_prompt_version_and_model(
    log: SQLiteEventLog,
    docs: SQLiteDocumentStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "judged.jsonl"
    first = FakeClient(_ANSWERS)
    one = run_judge(log, docs, first, out, limit=4)
    assert one.attempted == 4
    assert one.limit_reached is True
    assert len(read_judged_rows(out)) == 4

    second = FakeClient(_ANSWERS)
    two = run_judge(log, docs, second, out)
    assert two.already_judged == 4
    assert two.attempted == 7
    assert not set(first.asked()) & set(second.asked())
    rows = read_judged_rows(out)
    assert len(rows) == 11
    assert len({row.key() for row in rows}) == 11

    # Nothing left: a third run asks nothing.
    third = FakeClient(_ANSWERS)
    assert run_judge(log, docs, third, out).attempted == 0
    assert third.calls == []

    # Another model is another judge: everything is asked again.
    other = FakeClient(_ANSWERS)
    run_judge(log, docs, other, out, model="judge-b")
    assert len(other.calls) == 11

    # So is another prompt version.
    monkeypatch.setattr(pack_judge, "PROMPT_VERSION", "pack-judge/v0-test")
    newer = FakeClient(_ANSWERS)
    run_judge(log, docs, newer, out)
    assert len(newer.calls) == 11
    assert len(read_judged_rows(out)) == 33


@pytest.mark.usefixtures("world")
def test_a_limited_run_spends_its_calls_on_items_an_agent_graded(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    client = FakeClient(_ANSWERS)
    run_judge(log, docs, client, tmp_path / "judged.jsonl", limit=9)

    assert not {"g1", "g2"} & set(client.asked())


@pytest.mark.usefixtures("world")
def test_a_failed_call_writes_nothing_and_the_next_run_retries_it(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    out = tmp_path / "judged.jsonl"
    flaky = FakeClient(_ANSWERS, fail_on=frozenset({"h2"}))
    summary = run_judge(log, docs, flaky, out)

    assert summary.status == "partial"
    assert summary.llm_errors == 1
    assert "h2" not in {row.item_id for row in read_judged_rows(out)}
    assert len(read_judged_rows(out)) == 10

    healed = FakeClient(_ANSWERS)
    again = run_judge(log, docs, healed, out)
    assert healed.asked() == ["h2"]
    assert again.status == "ok"
    assert len(read_judged_rows(out)) == 11


@pytest.mark.usefixtures("world")
def test_a_dead_endpoint_stops_the_run_after_a_few_calls(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    out = tmp_path / "judged.jsonl"
    dead = FakeClient(_ANSWERS, fail_on=frozenset(_ANSWERS))
    summary = run_judge(log, docs, dead, out)

    assert summary.status == "aborted"
    assert len(dead.calls) == MAX_CONSECUTIVE_LLM_ERRORS
    assert read_judged_rows(out) == []


@pytest.mark.usefixtures("world")
def test_the_output_carries_ids_and_counts_never_text(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    out = tmp_path / "judged.jsonl"
    run_judge(log, docs, FakeClient(_ANSWERS), out)

    raw = out.read_text(encoding="utf-8")
    for text in (INTENT_A, INTENT_B, BODY_MARK, SECRET_PHRASE, "body text"):
        assert text not in raw
    allowed = set(JudgedRow.model_fields)
    for line in raw.splitlines():
        assert set(json.loads(line)) <= allowed
    # The fields the brief names are all there.
    assert {
        "pack_id",
        "item_id",
        "label",
        "model",
        "prompt_version",
        "judged_at",
    } <= allowed
    assert "item_tokens" in allowed


@pytest.mark.usefixtures("world")
def test_the_judge_writes_nothing_to_any_trellis_store(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    events_before = log.count()
    snapshot = {item: docs.get(item) for item in (*_PACK_ONE, *_PACK_TWO, *_HELD_OUT)}

    window = load_window(ReadOnly(log), since=SINCE)  # type: ignore[arg-type]
    import asyncio

    asyncio.run(
        judge_window(
            window,
            document_store=ReadOnly(docs),  # type: ignore[arg-type]
            client=FakeClient(_ANSWERS),
            gate=LOCAL,
            model="judge-a",
            out_path=tmp_path / "judged.jsonl",
        )
    )

    assert log.count() == events_before
    assert {item: docs.get(item) for item in snapshot} == snapshot


class ReadOnly:
    """Raises on any call that is not a read, so a write cannot pass unseen."""

    _READS = ("get", "count", "list", "scan", "query", "search")

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if not name.startswith(self._READS):
            message = f"the judge called {name}(), which is not a read"
            raise AssertionError(message)
        return getattr(self._inner, name)


# -- the window ---------------------------------------------------------------------


def test_the_window_is_bounded_and_validated(
    log: SQLiteEventLog,
) -> None:
    old = datetime.now(tz=UTC) - timedelta(days=30)
    seed_pack(log, "old", intent=INTENT_A, item_ids=("o1",), at=old)
    seed_pack(log, "new", intent=INTENT_A, item_ids=("n1",))

    window = load_window(log, since=SINCE)
    assert [item.item_id for item in window.items] == ["n1"]
    wide = load_window(log, since=old - timedelta(days=1))
    assert {item.item_id for item in wide.items} == {"o1", "n1"}
    with pytest.raises(PackJudgeError):
        load_window(log, since=SINCE, until=SINCE - timedelta(hours=1))
    with pytest.raises(PackJudgeError):
        parse_instant("last tuesday", option="--since")
    assert parse_instant("2026-11-01", option="--since") == datetime(
        2026, 11, 1, tzinfo=UTC
    )
    assert parse_instant(None, option="--until") is None


# -- calibration ----------------------------------------------------------------------


def _rows_from_answers(model: str = "judge-a", **overrides: str) -> list[JudgedRow]:
    packs = dict.fromkeys(_PACK_ONE, "pack-one") | dict.fromkeys(_PACK_TWO, "pack-two")
    answers = {**_ANSWERS, **overrides}
    return [
        JudgedRow(
            pack_id=packs[item],
            item_id=item,
            label=parse_label(answer),
            model=model,
            prompt_version=PROMPT_VERSION,
            judged_at=datetime.now(tz=UTC),
            item_tokens=TOKENS,
        )
        for item, answer in answers.items()
    ]


@pytest.mark.usefixtures("world")
def test_calibration_matches_the_hand_computed_figures(
    log: SQLiteEventLog,
) -> None:
    report = calibrate(
        load_window(log, since=SINCE),
        _rows_from_answers(),
        min_graded=7,  # kappa is over the 7 committed items and has the same floor
        min_unhelpful=4,
    )

    assert report.status == "ok"
    assert report.refusal is None
    assert report.model == "judge-a"
    matrix = report.matrix
    assert (
        matrix.agent_helpful.helpful,
        matrix.agent_helpful.unhelpful,
        matrix.agent_helpful.unsure,
    ) == (3, 1, 1)
    assert (
        matrix.agent_unhelpful.helpful,
        matrix.agent_unhelpful.unhelpful,
        matrix.agent_unhelpful.unsure,
    ) == (1, 2, 1)
    assert report.graded_overlap == 9
    assert report.agreement == pytest.approx(5 / 9)
    assert report.unhelpful_agreement == pytest.approx(0.5)
    assert report.helpful_agreement == pytest.approx(0.6)
    assert report.kappa == pytest.approx(10 / 24)
    assert report.kappa_n == 7

    assert report.coverage.injected_items == 11
    assert report.coverage.injected_tokens == 110
    assert report.coverage.judged_tokens == 110
    assert report.coverage.token_coverage == pytest.approx(1.0)
    assert report.useful.judge_useful_fraction == pytest.approx(60 / 110)
    assert report.useful.agent_lower_bound == pytest.approx(50 / 110)
    assert report.useful.agent_graded_token_share == pytest.approx(90 / 110)
    # The held-out pack and its feedback are not in the join.
    assert report.coverage.injected_items == 11


@pytest.mark.usefixtures("world")
def test_the_end_to_end_run_calibrates_to_the_same_figures(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    out = tmp_path / "judged.jsonl"
    run_judge(log, docs, FakeClient(_ANSWERS), out)
    report = pack_judge.calibrate_from_log(
        log, out, since=SINCE, min_graded=7, min_unhelpful=4
    )

    assert report.status == "ok"
    assert report.agreement == pytest.approx(5 / 9)
    assert report.kappa == pytest.approx(10 / 24)


@pytest.mark.usefixtures("world")
def test_too_few_graded_items_refuses_rather_than_printing_a_kappa(
    log: SQLiteEventLog,
) -> None:
    report = calibrate(load_window(log, since=SINCE), _rows_from_answers())

    assert MIN_GRADED_ITEMS == 30
    assert report.status == "refused"
    assert report.refusal is PackJudgeRefusal.INSUFFICIENT_GRADED_OVERLAP
    assert report.graded_overlap == 9
    assert report.kappa is None
    assert report.agreement is None
    assert report.unhelpful_agreement is None
    assert "30" in (report.message or "")


@pytest.mark.usefixtures("world")
def test_a_thin_unhelpful_side_is_withheld_not_estimated(
    log: SQLiteEventLog,
) -> None:
    report = calibrate(
        load_window(log, since=SINCE), _rows_from_answers(), min_graded=9
    )

    assert report.status == "ok"
    assert report.unhelpful_agreement is None
    assert report.unhelpful_agreement_withheld is not None
    assert "4" in report.unhelpful_agreement_withheld
    assert report.agreement == pytest.approx(5 / 9)


@pytest.mark.usefixtures("world")
def test_calibration_refuses_with_no_rows_for_the_version_or_model(
    log: SQLiteEventLog,
) -> None:
    window = load_window(log, since=SINCE)

    none = calibrate(window, [], min_graded=1)
    assert none.refusal is PackJudgeRefusal.NO_JUDGED_ROWS

    other_model = calibrate(window, _rows_from_answers(), model="nope", min_graded=1)
    assert other_model.refusal is PackJudgeRefusal.NO_JUDGED_ROWS

    stale = [
        row.model_copy(update={"prompt_version": "pack-judge/v0"})
        for row in _rows_from_answers()
    ]
    assert (
        calibrate(window, stale, min_graded=1).refusal
        is PackJudgeRefusal.NO_JUDGED_ROWS
    )


@pytest.mark.usefixtures("world")
def test_two_judges_in_one_file_are_not_averaged(log: SQLiteEventLog) -> None:
    window = load_window(log, since=SINCE)
    rows = [
        *_rows_from_answers("judge-a"),
        *_rows_from_answers("judge-b", h1="unhelpful"),
    ]

    ambiguous = calibrate(window, rows, min_graded=1)
    assert ambiguous.refusal is PackJudgeRefusal.AMBIGUOUS_JUDGE
    assert "judge-a" in (ambiguous.message or "")

    chosen = calibrate(window, rows, model="judge-b", min_graded=9, min_unhelpful=4)
    assert chosen.status == "ok"
    assert chosen.matrix.agent_helpful.helpful == 2  # h1 flipped for judge-b


@pytest.mark.usefixtures("world")
def test_unparsed_and_unjudgeable_graded_items_are_counted_not_joined(
    log: SQLiteEventLog,
) -> None:
    rows = _rows_from_answers(h1="I am not sure what to say")
    rows = [
        row.model_copy(update={"label": JudgeLabel.UNJUDGEABLE, "reason": "missing"})
        if row.item_id == "u1"
        else row
        for row in rows
    ]
    report = calibrate(
        load_window(log, since=SINCE), rows, min_graded=1, min_unhelpful=1
    )

    assert report.graded_overlap == 7
    assert report.graded_unusable == 2
    assert report.unparsed_rows == 1
    assert report.unjudgeable_rows == {"missing": 1}
    # Neither bad row is a judged token.
    assert report.coverage.judged_tokens == 90


@pytest.mark.usefixtures("world")
def test_a_judge_that_says_helpful_to_everything_has_zero_kappa(
    log: SQLiteEventLog,
) -> None:
    rows = _rows_from_answers(**dict.fromkeys(_ANSWERS, "helpful"))
    report = calibrate(
        load_window(log, since=SINCE), rows, min_graded=1, min_unhelpful=1
    )

    assert report.helpful_agreement == pytest.approx(1.0)
    assert report.unhelpful_agreement == pytest.approx(0.0)
    assert report.kappa == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("a", "b", "c", "d", "expected"),
    [
        (3, 1, 1, 2, 10 / 24),
        (10, 0, 0, 10, 1.0),
        (0, 5, 5, 0, -1.0),
        (5, 0, 4, 0, 0.0),
    ],
)
def test_cohens_kappa_matches_hand_values(
    a: int, b: int, c: int, d: int, expected: float
) -> None:
    kappa, why = cohens_kappa(a, b, c, d)
    assert kappa == pytest.approx(expected)
    assert why is None


@pytest.mark.parametrize(("cells"), [(0, 0, 0, 0), (5, 0, 0, 0), (0, 0, 0, 5)])
def test_cohens_kappa_is_undefined_not_zero_when_chance_is_total(
    cells: tuple[int, int, int, int],
) -> None:
    kappa, why = cohens_kappa(*cells)
    assert kappa is None
    assert why


def test_a_damaged_judged_file_is_named_not_skipped(tmp_path: Path) -> None:
    path = tmp_path / "judged.jsonl"
    path.write_text('{"pack_id": "p"}\n', encoding="utf-8")
    with pytest.raises(PackJudgeError, match="line 1"):
        read_judged_rows(path)
    assert read_judged_rows(tmp_path / "absent.jsonl") == []


# -- the pack is the unit of egress ----------------------------------------------


class _FlakyStore:
    """A document store that cannot answer for the ids it is told to fail on."""

    def __init__(self, inner: SQLiteDocumentStore, fail: frozenset[str]) -> None:
        self._inner = inner
        self._fail = fail

    def get(self, doc_id: str) -> Any:
        if doc_id in self._fail:
            message = "store unavailable"
            raise RuntimeError(message)
        return self._inner.get(doc_id)


def _mixed_world(log: SQLiteEventLog, docs: SQLiteDocumentStore) -> None:
    seed_pack(log, "mixed", intent=INTENT_A, item_ids=("open1", "secret", "open2"))
    seed_pack(log, "clean", intent=INTENT_B, item_ids=("fine1", "fine2"))
    for item in ("open1", "open2", "fine1", "fine2"):
        seed_doc(docs, item)
    seed_doc(docs, "secret", data_classification={"sensitivity": "restricted"})


_MIXED_ANSWERS = dict.fromkeys(
    ("open1", "secret", "open2", "fine1", "fine2"), "helpful"
)


def test_a_pack_that_served_a_withheld_document_sends_none_of_its_items_remotely(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    _mixed_world(log, docs)
    client = FakeClient(_MIXED_ANSWERS)
    out = tmp_path / "remote.jsonl"

    summary = run_judge(log, docs, client, out, gate=EgressGate(docs, local=False))

    # Only the clean pack reaches the model: the mixed pack's intent would name
    # the restricted document beside every permitted item.
    assert sorted(client.asked()) == ["fine1", "fine2"]
    rows = {row.item_id: row for row in read_judged_rows(out)}
    for item in ("open1", "secret", "open2"):
        assert rows[item].label is JudgeLabel.UNJUDGEABLE
        assert rows[item].reason == REASON_PACK_EGRESS_WITHHELD
    assert rows["fine1"].label is JudgeLabel.HELPFUL
    assert summary.unjudgeable == {REASON_PACK_EGRESS_WITHHELD: 3}
    assert INTENT_A not in out.read_text(encoding="utf-8")


def test_a_pack_with_an_unreadable_document_is_withheld_from_a_remote_route(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    _mixed_world(log, docs)
    client = FakeClient(_MIXED_ANSWERS)
    # The gate's store cannot answer for "open2": no label is read, so the pack
    # is held back rather than guessed to be unlabelled.
    gate = EgressGate(_FlakyStore(docs, frozenset({"open2"})), local=False)  # type: ignore[arg-type]
    out = tmp_path / "remote.jsonl"

    run_judge(log, docs, client, out, gate=gate)

    assert sorted(client.asked()) == ["fine1", "fine2"]
    reasons = {row.item_id: row.reason for row in read_judged_rows(out)}
    assert reasons["open1"] == REASON_PACK_EGRESS_WITHHELD


def test_a_local_route_still_judges_every_item_of_the_mixed_pack(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    _mixed_world(log, docs)
    client = FakeClient(_MIXED_ANSWERS)

    summary = run_judge(
        log, docs, client, tmp_path / "local.jsonl", gate=EgressGate(docs, local=True)
    )

    assert sorted(client.asked()) == sorted(_MIXED_ANSWERS)
    assert summary.unjudgeable == {}


# -- an unjudgeable pair is asked about again -----------------------------------------


def test_an_unjudgeable_pair_is_retried_and_its_row_replaced(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    seed_pack(log, "p", intent=INTENT_A, item_ids=("here", "late"))
    seed_doc(docs, "here")
    out = tmp_path / "judged.jsonl"
    first = run_judge(log, docs, FakeClient({"here": "helpful"}), out)
    assert first.unjudgeable == {"missing": 1}

    seed_doc(docs, "late")  # the document appears after the first run
    client = FakeClient({"late": "unhelpful"})
    second = run_judge(log, docs, client, out)

    assert client.asked() == ["late"]  # "here" was answered and stays done
    assert second.already_judged == 1
    rows = {row.item_id: row for row in read_judged_rows(out)}
    assert len(rows) == 2  # one row per key, the newest
    assert rows["late"].label is JudgeLabel.UNHELPFUL
    assert rows["late"].reason is None


def test_an_unchanged_unjudgeable_pair_adds_no_row(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    seed_pack(log, "p", intent=INTENT_A, item_ids=("gone",))
    out = tmp_path / "judged.jsonl"
    run_judge(log, docs, FakeClient({}), out)
    before = out.read_text(encoding="utf-8")

    again = run_judge(log, docs, FakeClient({}), out)

    assert again.attempted == 1  # it was checked again
    assert again.unjudgeable == {"missing": 1}
    assert again.rows_written == 0
    assert out.read_text(encoding="utf-8") == before


def test_a_pack_withheld_from_a_remote_route_is_judged_once_the_route_is_local(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    _mixed_world(log, docs)
    out = tmp_path / "judged.jsonl"
    run_judge(
        log,
        docs,
        FakeClient(_MIXED_ANSWERS),
        out,
        gate=EgressGate(docs, local=False),
    )

    client = FakeClient(_MIXED_ANSWERS)
    summary = run_judge(log, docs, client, out, gate=EgressGate(docs, local=True))

    assert sorted(client.asked()) == ["open1", "open2", "secret"]
    assert summary.unjudgeable == {}
    rows = read_judged_rows(out)
    assert len(rows) == 5
    assert all(row.label is JudgeLabel.HELPFUL for row in rows)


def test_a_limited_run_tries_new_pairs_before_rechecking_unjudgeable_ones(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    seed_pack(log, "old", intent=INTENT_A, item_ids=("gone",))
    out = tmp_path / "judged.jsonl"
    run_judge(log, docs, FakeClient({}), out)

    seed_pack(log, "new", intent=INTENT_B, item_ids=("fresh",))
    seed_doc(docs, "fresh")
    client = FakeClient({"fresh": "helpful"})
    run_judge(log, docs, client, out, limit=1)

    assert client.asked() == ["fresh"]  # the one attempt went to new ground


# -- failure logs name the cause, never the text ------------------------------------


@pytest.mark.usefixtures("world")
def test_a_failed_call_is_logged_with_its_type_and_never_its_message(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    flaky = FakeClient(_ANSWERS, fail_on=frozenset({"h2"}))

    with capture_logs() as logs:
        run_judge(log, docs, flaky, tmp_path / "judged.jsonl")

    failed = [e for e in logs if e["event"] == "pack_judge_llm_call_failed"]
    assert len(failed) == 1
    assert failed[0]["error_type"] == "RuntimeError"
    assert failed[0]["item_id"] == "h2"
    assert "endpoint refused" not in json.dumps(logs, default=str)


def test_a_failed_document_read_is_logged_with_its_type_and_never_its_message(
    log: SQLiteEventLog, docs: SQLiteDocumentStore, tmp_path: Path
) -> None:
    seed_pack(log, "p", intent=INTENT_A, item_ids=("ok", "broken"))
    seed_doc(docs, "ok")
    store = _FlakyStore(docs, frozenset({"broken"}))

    import asyncio

    with capture_logs() as logs:
        summary = asyncio.run(
            judge_window(
                load_window(log, since=SINCE),
                document_store=store,  # type: ignore[arg-type]
                client=FakeClient({"ok": "helpful"}),
                gate=LOCAL,
                model="judge-a",
                out_path=tmp_path / "judged.jsonl",
            )
        )

    assert summary.read_errors == 1
    failed = [e for e in logs if e["event"] == "pack_judge_document_read_failed"]
    assert [e["error_type"] for e in failed] == ["RuntimeError"]
    assert "store unavailable" not in json.dumps(logs, default=str)


# -- kappa has its own floor -----------------------------------------------------


def _window_of(items: list[ServedItem]) -> PackWindow:
    return PackWindow(
        items=tuple(items),
        packs=1,
        held_out_packs=0,
        coverage=ScanCoverage.model_validate({}),
        since=SINCE,
    )


def _judged(item_id: str, answer: JudgeLabel) -> JudgedRow:
    return JudgedRow(
        pack_id="p",
        item_id=item_id,
        label=answer,
        model="m",
        prompt_version=PROMPT_VERSION,
        judged_at=datetime.now(tz=UTC),
        item_tokens=TOKENS,
    )


def test_a_kappa_over_two_committed_items_is_withheld_though_thirty_were_graded() -> (
    None
):
    # 30 agent-graded items clear the overlap floor, but the judge committed on
    # two of them: it answered ``unsure`` to the other 28.
    items, rows = [], []
    for n in range(30):
        verdict = "helpful" if n % 2 == 0 else "unhelpful"
        items.append(ServedItem("p", f"i{n}", "intent", TOKENS, verdict))  # type: ignore[arg-type]
        committed = {0: JudgeLabel.HELPFUL, 1: JudgeLabel.UNHELPFUL}.get(n)
        rows.append(_judged(f"i{n}", committed or JudgeLabel.UNSURE))

    report = calibrate(_window_of(items), rows)

    assert report.status == "ok"
    assert report.graded_overlap == 30
    assert report.kappa_n == 2
    assert report.kappa is None
    assert report.kappa_unavailable
    assert "2" in report.kappa_unavailable
    assert "30" in report.kappa_unavailable
    assert report.agreement == pytest.approx(2 / 30)  # the other figures stand


@pytest.mark.usefixtures("world")
def test_kappa_prints_at_exactly_the_floor_and_not_one_below(
    log: SQLiteEventLog,
) -> None:
    window = load_window(log, since=SINCE)
    rows = _rows_from_answers()  # the hand-computed world: kappa over 7 items

    at_floor = calibrate(window, rows, min_graded=7, min_unhelpful=4)
    below = calibrate(window, rows, min_graded=8, min_unhelpful=4)

    assert at_floor.kappa == pytest.approx(10 / 24)
    assert at_floor.kappa_unavailable is None
    assert below.status == "ok"  # nine graded items clear 8
    assert below.kappa_n == 7
    assert below.kappa is None
    assert below.kappa_unavailable
