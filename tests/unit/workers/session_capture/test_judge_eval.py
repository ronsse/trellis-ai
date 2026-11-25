"""The offline judge eval: production judge and gates, rows on disk, no store.

Every transcript, candidate and endpoint here is synthetic; no client talks to
a network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from trellis.llm.types import LLMResponse, TokenUsage
from trellis_workers.session_capture import judge_eval
from trellis_workers.session_capture.__main__ import main as worker_main
from trellis_workers.session_capture.judge_eval import (
    EvalConfigError,
    GoldMemory,
    JudgeArm,
    load_arms,
    run_eval,
)

from .conftest import (
    PACK_IDS,
    BrokenLLMClient,
    assistant_tools,
    assistant_turn,
    candidates_json,
    good_candidate,
    mcp_envelope,
    pack_markdown,
    pack_result_turn,
    user_turn,
    write_transcript,
)

_QUOTE = "the boot probe failed with a missing-table error"


class UsageClient:
    """Returns *responses* in order (last repeats) and reports token usage."""

    def __init__(self, responses: list[str], *, delay: float = 0.0) -> None:
        self._responses = responses
        self._delay = delay
        self.calls: list[list[Any]] = []

    async def generate(self, *, messages: list[Any], **_kwargs: Any) -> LLMResponse:
        self.calls.append(messages)
        if self._delay:
            await asyncio.sleep(self._delay)
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        return LLMResponse(
            content=self._responses[idx],
            model="fake-judge",
            usage=TokenUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
        )


@pytest.fixture(autouse=True)
def _no_operator_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRELLIS_CAPTURE_MAX_SALIENT_CHARS", raising=False)
    monkeypatch.delenv("TRELLIS_CAPTURE_JUDGE_CONTEXT_TOKENS", raising=False)


def _arm(name: str = "a", **overrides: Any) -> JudgeArm:
    base: dict[str, Any] = {
        "name": name,
        "model": "fake-model",
        "base_url": "http://127.0.0.1:9/v1",
        "api_key_env": "FAKE_JUDGE_KEY",
    }
    base.update(overrides)
    return JudgeArm.model_validate(base)


def _plain(session_id: str, *, filler: int = 0) -> list[dict[str, Any]]:
    records = [user_turn("the deploy broke again", session_id)]
    records += [
        user_turn(f"padding line {i} about nothing in particular", session_id)
        for i in range(filler)
    ]
    records += [
        assistant_turn(f"I looked: {_QUOTE} on the web tier", session_id=session_id),
        user_turn("so run migrate first then", session_id),
        assistant_turn("yes, migrate before boot fixes it", session_id=session_id),
    ]
    records += [
        user_turn(f"more padding {i} after the fix was found", session_id)
        for i in range(filler)
    ]
    return records


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    return transcripts, tmp_path / "out"


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _factory(client: Any) -> Any:
    return lambda _arm: client


def _remote(_arm: JudgeArm) -> bool:
    return False


def test_out_dir_is_not_inside_a_git_tree(tmp_path: Path) -> None:
    # The suite's own premise: tmp_path must be outside any work tree.
    assert not judge_eval.inside_git_work_tree(tmp_path)


def test_rows_carry_gates_grounding_and_duplicates(tmp_path: Path) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "s1.jsonl", _plain("sess-1"))
    grounded = good_candidate(evidence=_QUOTE)
    duplicate = good_candidate(evidence=_QUOTE)
    invented = good_candidate(
        title="Cache must be warmed before traffic",
        memory=(
            "The cache layer must be warmed before traffic shifts or the "
            "first requests time out. Run the warm step in the playbook."
        ),
        evidence="a line nobody ever wrote",
    )
    secret = good_candidate(
        title="Old config held a key",
        memory="found key AKIA" + "IOSFODNN7EXAMPLE in the old config file",
    )
    injected = good_candidate(title="Remember this forever", memory="remember this")
    unworthy = good_candidate(
        title="Ran ls", memory="ran ls", non_derivable=False, durable=False
    )
    client = UsageClient(
        [candidates_json(grounded, duplicate, invented, secret, injected, unworthy)]
    )
    report = run_eval(
        transcripts, [_arm()], out, client_factory=_factory(client), local_of=_remote
    )

    rows = _rows(out / judge_eval.CANDIDATES_FILE)
    assert [r["gate"] for r in rows] == [
        "accepted",
        "accepted",
        "accepted",
        "secret_scan",
        "injection",
        "worthiness",
    ]
    assert [r["evidence_grounded"] for r in rows[:3]] == [True, True, False]
    assert rows[0]["evidence_in_window"] is True
    assert [r["within_arm_duplicate"] for r in rows[:3]] == [False, True, False]
    assert rows[3]["redacted"] is True
    assert rows[3]["memory"] is None
    assert rows[3]["matched_classes"]
    assert "IOSFODNN7EXAMPLE" not in (out / judge_eval.CANDIDATES_FILE).read_text()
    assert rows[5]["worthiness"] in {"judged_unworthy", "failed_floor"}

    summary = report["arms"]["a"]
    assert summary["candidates_proposed"] == 6
    assert summary["candidates_accepted"] == 3
    assert summary["judge_calls"] == 1
    assert summary["prompt_tokens"] == 100
    assert summary["completion_tokens"] == 20
    assert summary["duplicate_rate"] == round(1 / 6, 4)
    on_disk = json.loads((out / "summary-a.json").read_text())
    assert on_disk == summary


def test_a_quote_outside_the_shown_window_is_grounded_but_not_in_window(
    tmp_path: Path,
) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "long.jsonl", _plain("sess-long", filler=60))
    client = UsageClient([candidates_json(good_candidate(evidence=_QUOTE))])
    run_eval(
        transcripts,
        [_arm(salient_chars=600)],
        out,
        client_factory=_factory(client),
        local_of=_remote,
    )
    (row,) = _rows(out / judge_eval.CANDIDATES_FILE)
    assert row["evidence_grounded"] is True
    assert row["evidence_in_window"] is False


def test_failures_and_timeouts_are_recorded_and_the_run_goes_on(
    tmp_path: Path,
) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "s1.jsonl", _plain("sess-1"))
    write_transcript(transcripts / "s2.jsonl", _plain("sess-2"))
    slow = UsageClient([candidates_json(good_candidate())], delay=1.0)
    clients = {"broken": BrokenLLMClient(), "slow": slow}
    report = run_eval(
        transcripts,
        [_arm("broken"), _arm("slow")],
        out,
        timeout=0.05,
        client_factory=lambda arm: clients[arm.name],
        local_of=_remote,
    )
    calls = _rows(out / judge_eval.CALLS_FILE)
    assert len(calls) == 4
    assert report["arms"]["broken"]["failures"] == 2
    assert report["arms"]["slow"]["timeouts"] == 2
    assert report["arms"]["slow"]["candidates_proposed"] == 0


def test_a_rerun_skips_pairs_already_recorded(tmp_path: Path) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "s1.jsonl", _plain("sess-1"))
    write_transcript(transcripts / "s2.jsonl", _plain("sess-2"))
    client = UsageClient([candidates_json(good_candidate(evidence=_QUOTE))])
    kwargs: dict[str, Any] = {"client_factory": _factory(client), "local_of": _remote}
    run_eval(transcripts, [_arm()], out, max_transcripts=1, **kwargs)
    assert len(client.calls) == 1
    report = run_eval(transcripts, [_arm()], out, **kwargs)
    assert len(client.calls) == 2
    assert len(_rows(out / judge_eval.CALLS_FILE)) == 2
    assert report["arms"]["a"]["transcripts"] == 2
    run_eval(transcripts, [_arm()], out, **kwargs)
    assert len(client.calls) == 2


def test_an_output_dir_inside_a_git_tree_is_refused(tmp_path: Path) -> None:
    transcripts, _ = _dirs(tmp_path)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(EvalConfigError, match="git work tree"):
        run_eval(
            transcripts,
            [_arm()],
            repo / "nested" / "out",
            client_factory=_factory(UsageClient(["[]"])),
            local_of=_remote,
        )
    assert not (repo / "nested").exists()


def test_a_transcript_that_named_a_pack_stays_off_a_route_not_declared_local(
    tmp_path: Path,
) -> None:
    transcripts, out = _dirs(tmp_path)
    records = [
        *_plain("sess-pack"),
        assistant_tools(("mcp__trellis__get_context", "u-1")),
        pack_result_turn("u-1", mcp_envelope(pack_markdown(PACK_IDS[0]))),
    ]
    write_transcript(transcripts / "p.jsonl", records)
    client = UsageClient([candidates_json(good_candidate())])
    arms = [_arm("remote"), _arm("onhost", local=True)]
    report = run_eval(
        transcripts,
        arms,
        out,
        client_factory=_factory(client),
        local_of=lambda arm: arm.local,
    )
    by_arm = {c["arm"]: c for c in _rows(out / judge_eval.CALLS_FILE)}
    assert by_arm["remote"]["outcome"] == judge_eval.OUTCOME_EGRESS_WITHHELD
    assert by_arm["onhost"]["outcome"] == "candidates"
    assert len(client.calls) == 1
    assert report["arms"]["remote"]["judge_calls"] == 0


def test_gold_recall_precision_and_unmatched_files(tmp_path: Path) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "s1.jsonl", _plain("sess-1"))
    by_quote = good_candidate(evidence=_QUOTE)
    by_words = good_candidate(
        title="Cache must be warmed before traffic",
        memory="The cache must be warmed before traffic shifts to the new tier.",
        evidence="warm step logged",
    )
    stray = good_candidate(
        title="Pager rota lives in the wiki",
        memory="The pager rota is kept in the team wiki under on-call.",
        evidence="wiki page on-call",
    )
    client = UsageClient([candidates_json(by_quote, by_words, stray)])
    gold = {
        "s1.jsonl": [
            GoldMemory(transcript_id="s1.jsonl", text="x", evidence=_QUOTE),
            GoldMemory(
                transcript_id="s1.jsonl",
                text="cache must be warmed before traffic shifts to the new tier",
            ),
            GoldMemory(transcript_id="s1.jsonl", text="an unrelated lesson"),
        ]
    }
    report = run_eval(
        transcripts,
        [_arm()],
        out,
        gold=gold,
        client_factory=_factory(client),
        local_of=_remote,
    )
    block = report["arms"]["a"]["gold"]
    assert block["matched"] == 2
    assert block["precision"] == round(2 / 3, 4)
    assert block["recall"] == round(2 / 3, 4)
    (lone_candidate,) = _rows(out / "unmatched-candidates-a.jsonl")
    assert lone_candidate["title"] == stray["title"]
    (lone_gold,) = _rows(out / "unmatched-gold-a.jsonl")
    assert lone_gold["text"] == "an unrelated lesson"


def test_arm_files_load_from_yaml_and_json_and_refuse_bad_ones(
    tmp_path: Path,
) -> None:
    yaml_file = tmp_path / "arms.yaml"
    yaml_file.write_text(
        "arms:\n"
        "  - name: small\n    model: m\n    base_url: http://127.0.0.1:9/v1\n"
        "    api_key_env: K\n    salient_chars: 4000\n    context_tokens: 8192\n"
    )
    (arm,) = load_arms(yaml_file)
    assert (arm.salient_chars, arm.context_tokens, arm.prompt_variant) == (
        4000,
        8192,
        "default",
    )
    json_file = tmp_path / "arms.json"
    json_file.write_text(json.dumps({"arms": [_arm().model_dump()]}))
    assert load_arms(json_file) == [_arm()]
    for bad in (
        {"arms": [_arm().model_dump(), _arm().model_dump()]},
        {"arms": [{**_arm().model_dump(), "prompt_variant": "nope"}]},
        {"arms": [{**_arm().model_dump(), "api_key": "inline"}]},
        {"arms": [{**_arm().model_dump(), "name": "../escape"}]},
    ):
        json_file.write_text(json.dumps(bad))
        with pytest.raises(EvalConfigError):
            load_arms(json_file)


def test_the_client_and_route_come_from_the_registry_and_the_key_stays_put(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("openai")
    sentinel = "fake-sentinel-key-value-7f3a"
    monkeypatch.delenv("FAKE_JUDGE_KEY", raising=False)
    assert judge_eval.build_arm_client(_arm()) is None
    monkeypatch.setenv("FAKE_JUDGE_KEY", sentinel)
    assert judge_eval.build_arm_client(_arm()) is not None
    assert judge_eval.arm_route_is_local(_arm()) is False
    assert judge_eval.arm_route_is_local(_arm(local=True)) is True

    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "s1.jsonl", _plain("sess-1"))
    arms_file = tmp_path / "arms.json"
    arms_file.write_text(json.dumps({"arms": [_arm().model_dump()]}))
    client = UsageClient([candidates_json(good_candidate(evidence=_QUOTE))])
    monkeypatch.setattr(judge_eval, "build_arm_client", lambda _arm: client)
    code = worker_main(
        [
            "eval-judge",
            "--transcripts",
            str(transcripts),
            "--arms",
            str(arms_file),
            "--output",
            str(out),
        ]
    )
    assert code == 0
    assert len(client.calls) == 1
    for produced in out.iterdir():
        assert sentinel not in produced.read_text()


def test_the_cli_exits_one_on_a_refused_output_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transcripts, _ = _dirs(tmp_path)
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    arms_file = tmp_path / "arms.json"
    arms_file.write_text(json.dumps({"arms": [_arm().model_dump()]}))
    code = worker_main(
        [
            "eval-judge",
            "--transcripts",
            str(transcripts),
            "--arms",
            str(arms_file),
            "--output",
            str(tmp_path / "repo" / "out"),
        ]
    )
    assert code == judge_eval.EXIT_CONFIG
    assert "git work tree" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Fix round: redaction, resume, denominators, egress modes
# ---------------------------------------------------------------------------

# Obviously fake, assembled so no scanner flags this file.
_FAKE_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


def _pack_records(session_id: str, pack_id: str) -> list[dict[str, Any]]:
    return [
        *_plain(session_id),
        assistant_tools(("mcp__trellis__get_context", "u-1")),
        pack_result_turn("u-1", mcp_envelope(pack_markdown(pack_id))),
    ]


@pytest.mark.parametrize("field", ["signal", "memory_type"])
def test_a_secret_in_any_model_field_nulls_every_model_string(
    tmp_path: Path, field: str
) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "s1.jsonl", _plain("sess-1"))
    leaky = good_candidate(**{field: f"note {_FAKE_KEY} here"})
    client = UsageClient([candidates_json(leaky)])
    run_eval(
        transcripts, [_arm()], out, client_factory=_factory(client), local_of=_remote
    )
    (row,) = _rows(out / judge_eval.CANDIDATES_FILE)
    assert row["redacted"] is True
    for key in ("title", "memory", "evidence", "memory_type", "signal"):
        assert row[key] is None, key
    for produced in out.iterdir():
        assert "IOSFODNN7EXAMPLE" not in produced.read_text()


def test_candidate_rows_of_a_crashed_attempt_are_not_counted_after_a_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "s1.jsonl", _plain("sess-1"))
    real_append = judge_eval._append_jsonl

    def crash_on_call_row(path: Path, rows: Any) -> None:
        if path.name == judge_eval.CALLS_FILE:
            msg = "simulated crash before the call row"
            raise RuntimeError(msg)
        real_append(path, rows)

    monkeypatch.setattr(judge_eval, "_append_jsonl", crash_on_call_row)
    three = [
        good_candidate(title=f"Distinct lesson number {i} about deploys")
        for i in range(3)
    ]
    first = UsageClient([candidates_json(*three)])
    with pytest.raises(RuntimeError):
        run_eval(
            transcripts, [_arm()], out, client_factory=_factory(first), local_of=_remote
        )
    assert len(_rows(out / judge_eval.CANDIDATES_FILE)) == 3

    monkeypatch.setattr(judge_eval, "_append_jsonl", real_append)
    second = UsageClient([candidates_json(good_candidate(evidence=_QUOTE))])
    report = run_eval(
        transcripts, [_arm()], out, client_factory=_factory(second), local_of=_remote
    )
    assert report["arms"]["a"]["candidates_proposed"] == 1


def test_a_torn_tail_line_does_not_swallow_the_next_row(tmp_path: Path) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "s1.jsonl", _plain("sess-1"))
    out.mkdir()
    (out / judge_eval.CALLS_FILE).write_text('{"transcript_id": "torn', "utf-8")
    client = UsageClient([candidates_json(good_candidate(evidence=_QUOTE))])
    report = run_eval(
        transcripts, [_arm()], out, client_factory=_factory(client), local_of=_remote
    )
    assert report["arms"]["a"]["transcripts"] == 1
    assert report["arms"]["a"]["candidates_proposed"] == 1


def _gold_for(*transcript_ids: str) -> dict[str, list[GoldMemory]]:
    return {
        tid: [GoldMemory(transcript_id=tid, text="x", evidence=_QUOTE)]
        for tid in transcript_ids
    }


def test_gold_denominators_exclude_transcripts_the_judge_never_answered(
    tmp_path: Path,
) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "s1.jsonl", _plain("sess-1"))
    write_transcript(transcripts / "s2.jsonl", _pack_records("sess-2", PACK_IDS[0]))
    client = UsageClient([candidates_json(good_candidate(evidence=_QUOTE))])
    report = run_eval(
        transcripts,
        [_arm("remote"), _arm("onhost", local=True)],
        out,
        gold=_gold_for("s1.jsonl", "s2.jsonl"),
        client_factory=_factory(client),
        local_of=lambda arm: arm.local,
    )
    remote = report["arms"]["remote"]["gold"]
    assert remote["recall"] == 1.0
    assert remote["gold_rows"] == 1
    assert remote["transcripts_not_judged_by_outcome"] == {"egress_withheld": 1}
    assert remote["gold_rows_excluded_not_judged"] == 1
    assert report["arms"]["onhost"]["gold"]["gold_rows"] == 2
    inter = report["intersection"]
    assert inter["transcripts"] == 1
    assert inter["arms"]["remote"]["recall"] == 1.0
    assert inter["arms"]["onhost"]["gold_rows"] == 1


def test_trivial_evidence_is_too_short_not_grounded_and_matches_no_gold(
    tmp_path: Path,
) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "s1.jsonl", _plain("sess-1"))
    trivial = good_candidate(
        title="Pager rota lives in the wiki",
        memory="The pager rota is kept in the team wiki under on-call.",
        evidence="boot",
    )
    client = UsageClient([candidates_json(trivial)])
    report = run_eval(
        transcripts,
        [_arm()],
        out,
        gold=_gold_for("s1.jsonl"),
        client_factory=_factory(client),
        local_of=_remote,
    )
    (row,) = _rows(out / judge_eval.CANDIDATES_FILE)
    assert row["evidence_check"] == judge_eval.EVIDENCE_TOO_SHORT
    assert row["evidence_grounded"] is None
    assert row["evidence_in_window"] is None
    assert report["arms"]["a"]["evidence_too_short"] == 1
    assert report["arms"]["a"]["gold"]["matched"] == 0


def test_ephemeral_projects_are_skipped_on_the_record_unless_included(
    tmp_path: Path,
) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "-tmp-tmpa1b2c3" / "s.jsonl", _plain("sess-e"))
    client = UsageClient([candidates_json(good_candidate(evidence=_QUOTE))])
    kwargs: dict[str, Any] = {"client_factory": _factory(client), "local_of": _remote}
    report = run_eval(transcripts, [_arm()], out, **kwargs)
    assert client.calls == []
    assert report["arms"]["a"]["outcomes"] == {judge_eval.OUTCOME_SKIPPED_EPHEMERAL: 1}
    run_eval(transcripts, [_arm()], tmp_path / "out2", include_ephemeral=True, **kwargs)
    assert len(client.calls) == 1


def test_resuming_with_a_changed_arm_config_is_refused_naming_the_arm(
    tmp_path: Path,
) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "s1.jsonl", _plain("sess-1"))
    client = UsageClient([candidates_json(good_candidate(evidence=_QUOTE))])
    kwargs: dict[str, Any] = {"client_factory": _factory(client), "local_of": _remote}
    run_eval(transcripts, [_arm()], out, **kwargs)
    (call,) = _rows(out / judge_eval.CALLS_FILE)
    assert call["arm_fingerprint"] == judge_eval.arm_fingerprint(_arm())
    assert "FAKE_JUDGE_KEY" not in json.dumps(call)
    run_eval(transcripts, [_arm(api_key_env="OTHER_KEY")], out, **kwargs)
    with pytest.raises(EvalConfigError, match=r"'a'.*new arm name"):
        run_eval(transcripts, [_arm(salient_chars=600)], out, **kwargs)


def _label_stores(tmp_path: Path, *, labelled: bool) -> Any:
    from tests.unit.workers.session_capture.test_capture import _registry
    from trellis.stores.base.event_log import EventType

    stores = _registry(tmp_path)
    metadata = (
        {"data_classification": {"sensitivity": "confidential"}} if labelled else {}
    )
    stores.knowledge.document_store.put("doc-served", "body", metadata)
    stores.operational.event_log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id=PACK_IDS[0],
        entity_type="pack",
        payload={"injected_item_ids": ["doc-served"]},
    )
    return stores


@pytest.mark.parametrize(
    ("labelled", "outcome", "reason"),
    [
        (False, "candidates", None),
        (True, "egress_withheld", "label_withheld"),
    ],
)
def test_the_labels_rule_reads_what_the_packs_served(
    tmp_path: Path, labelled: bool, outcome: str, reason: str | None
) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "p.jsonl", _pack_records("sess-p", PACK_IDS[0]))
    stores = _label_stores(tmp_path, labelled=labelled)
    client = UsageClient([candidates_json(good_candidate(evidence=_QUOTE))])
    run_eval(
        transcripts,
        [_arm("remote")],
        out,
        client_factory=_factory(client),
        local_of=_remote,
        stores=stores,
    )
    (call,) = _rows(out / judge_eval.CALLS_FILE)
    assert call["egress_rule"] == judge_eval.EGRESS_RULE_LABELS
    assert call["outcome"] == outcome
    assert call["egress_reason"] == reason
    assert len(client.calls) == (0 if labelled else 1)


def test_the_labels_rule_fails_closed_on_a_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "p.jsonl", _pack_records("sess-p", PACK_IDS[0]))
    stores = _label_stores(tmp_path, labelled=False)

    def down(*_args: Any, **_kwargs: Any) -> Any:
        msg = "log down"
        raise RuntimeError(msg)

    monkeypatch.setattr(stores.operational.event_log, "get_events", down)
    client = UsageClient([candidates_json(good_candidate())])
    run_eval(
        transcripts,
        [_arm("remote")],
        out,
        client_factory=_factory(client),
        local_of=_remote,
        stores=stores,
    )
    (call,) = _rows(out / judge_eval.CALLS_FILE)
    assert call["outcome"] == judge_eval.OUTCOME_EGRESS_WITHHELD
    assert call["egress_reason"] == judge_eval.EGRESS_REASON_UNREADABLE
    assert client.calls == []


def test_the_strict_rule_is_the_default_and_says_so(tmp_path: Path) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "p.jsonl", _pack_records("sess-p", PACK_IDS[0]))
    run_eval(
        transcripts,
        [_arm("remote")],
        out,
        client_factory=_factory(UsageClient(["[]"])),
        local_of=_remote,
    )
    (call,) = _rows(out / judge_eval.CALLS_FILE)
    assert call["egress_rule"] == judge_eval.EGRESS_RULE_STRICT
    assert call["egress_reason"] == judge_eval.EGRESS_REASON_PACK_NAMED


def _cli(tmp_path: Path, transcripts: Path, out: Path, *extra: str) -> int:
    arms_file = tmp_path / "arms.json"
    arms_file.write_text(json.dumps({"arms": [_arm().model_dump()]}))
    return worker_main(
        [
            "eval-judge",
            "--transcripts",
            str(transcripts),
            "--arms",
            str(arms_file),
            "--output",
            str(out),
            *extra,
        ]
    )


def test_the_cli_egress_labels_flag_opens_stores_and_closes_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "p.jsonl", _pack_records("sess-p", PACK_IDS[0]))
    stores = _label_stores(tmp_path, labelled=True)
    monkeypatch.setattr(judge_eval, "open_label_stores", lambda: stores)
    monkeypatch.setattr(judge_eval, "build_arm_client", lambda _arm: UsageClient([]))
    assert _cli(tmp_path, transcripts, out, "--egress-labels") == 0
    (call,) = _rows(out / judge_eval.CALLS_FILE)
    assert call["egress_reason"] == judge_eval.EGRESS_REASON_LABEL
    stores.close.assert_called_once()


def test_the_cli_exits_one_on_unusable_inputs_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from trellis.errors import BackendNotInstalledError

    transcripts, out = _dirs(tmp_path)
    assert _cli(tmp_path, tmp_path / "missing", out) == judge_eval.EXIT_CONFIG
    assert "does not exist" in capsys.readouterr().err

    write_transcript(transcripts / "s1.jsonl", _plain("sess-1"))

    def no_backend(_arm: JudgeArm) -> Any:
        raise BackendNotInstalledError(backend_name="openai", extra="llm-openai")

    monkeypatch.setattr(judge_eval, "build_arm_client", no_backend)
    assert _cli(tmp_path, transcripts, out) == judge_eval.EXIT_CONFIG
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "llm-openai" in err

    def no_stores() -> Any:
        msg = "stores could not be opened"
        raise EvalConfigError(msg)

    monkeypatch.setattr(judge_eval, "open_label_stores", no_stores)
    assert _cli(tmp_path, transcripts, out, "--egress-labels") == 1


def test_outputs_are_owner_only(tmp_path: Path) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "s1.jsonl", _plain("sess-1"))
    client = UsageClient([candidates_json(good_candidate(evidence=_QUOTE))])
    run_eval(
        transcripts,
        [_arm()],
        out,
        gold=_gold_for("s1.jsonl"),
        client_factory=_factory(client),
        local_of=_remote,
    )
    assert out.stat().st_mode & 0o777 == 0o700
    produced = list(out.iterdir())
    assert len(produced) == 5
    for path in produced:
        assert path.stat().st_mode & 0o777 == 0o600, path.name


def test_mean_latency_covers_completed_calls_only(tmp_path: Path) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "s1.jsonl", _plain("sess-1"))
    report = run_eval(
        transcripts,
        [_arm("broken")],
        out,
        client_factory=_factory(BrokenLLMClient()),
        local_of=_remote,
    )
    summary = report["arms"]["broken"]
    assert summary["mean_latency_s"] is None
    assert summary["failed_calls_mean_latency_s"] is not None


# ---------------------------------------------------------------------------
# Second fix round: labels mode fails closed, modes, resume settings
# ---------------------------------------------------------------------------


def test_the_labels_rule_withholds_a_pack_the_event_log_does_not_know(
    tmp_path: Path,
) -> None:
    from tests.unit.workers.session_capture.test_capture import _registry

    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "p.jsonl", _pack_records("sess-p", PACK_IDS[0]))
    stores = _registry(tmp_path)  # empty: not the deployment that served it
    client = UsageClient([candidates_json(good_candidate(evidence=_QUOTE))])
    run_eval(
        transcripts,
        [_arm("remote"), _arm("onhost", local=True)],
        out,
        client_factory=_factory(client),
        local_of=lambda arm: arm.local,
        stores=stores,
    )
    calls = _rows(out / judge_eval.CALLS_FILE)
    assert {c["egress_reason"] for c in calls} == {
        judge_eval.EGRESS_REASON_PACK_UNRECORDED
    }
    assert {c["outcome"] for c in calls} == {judge_eval.OUTCOME_EGRESS_WITHHELD}
    assert client.calls == []


def test_the_labels_rule_withholds_a_transcript_with_an_unparsed_pack_line(
    tmp_path: Path,
) -> None:
    transcripts, _ = _dirs(tmp_path)
    write_transcript(transcripts / "p.jsonl", _pack_records("sess-p", PACK_IDS[0]))
    stores = _label_stores(tmp_path, labelled=False)
    digest = judge_eval.parse_session(transcripts / "p.jsonl")
    assert (
        judge_eval._eval_egress_permits(
            digest, _arm("remote"), local=False, stores=stores
        )
        is None
    )
    digest.pack_ids_unparsed = 1
    assert (
        judge_eval._eval_egress_permits(
            digest, _arm("remote"), local=False, stores=stores
        )
        == judge_eval.EGRESS_REASON_PACK_UNPARSED
    )


def test_label_stores_refuse_a_config_dir_with_no_config_yaml_creating_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "cfg"
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(EvalConfigError, match=r"no config\.yaml"):
        judge_eval.open_label_stores()
    assert sorted(p.name for p in tmp_path.iterdir()) == []

    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("{}\n")
    stores = judge_eval.open_label_stores()
    stores.close()


def test_an_existing_output_dir_and_files_are_tightened(tmp_path: Path) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "s1.jsonl", _plain("sess-1"))
    out.mkdir(mode=0o755)
    out.chmod(0o755)
    loose = out / judge_eval.CANDIDATES_FILE
    loose.write_text("")
    loose.chmod(0o644)
    client = UsageClient([candidates_json(good_candidate(evidence=_QUOTE))])
    run_eval(
        transcripts, [_arm()], out, client_factory=_factory(client), local_of=_remote
    )
    assert out.stat().st_mode & 0o777 == 0o700
    assert loose.stat().st_mode & 0o777 == 0o600


def test_a_dangling_link_at_output_is_a_one_line_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transcripts, _ = _dirs(tmp_path)
    write_transcript(transcripts / "s1.jsonl", _plain("sess-1"))
    link = tmp_path / "out-link"
    link.symlink_to(tmp_path / "nowhere")
    assert _cli(tmp_path, transcripts, link) == judge_eval.EXIT_CONFIG
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "not a directory" in err


@pytest.mark.parametrize("setting", ["egress_rule", "include_ephemeral"])
def test_resuming_under_a_different_run_setting_is_refused_naming_it(
    tmp_path: Path, setting: str
) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "p.jsonl", _pack_records("sess-p", PACK_IDS[0]))
    client = UsageClient([candidates_json(good_candidate(evidence=_QUOTE))])
    kwargs: dict[str, Any] = {"client_factory": _factory(client), "local_of": _remote}
    run_eval(transcripts, [_arm()], out, **kwargs)
    (call,) = _rows(out / judge_eval.CALLS_FILE)
    assert call["egress_rule"] == judge_eval.EGRESS_RULE_STRICT
    assert call["include_ephemeral"] is False
    run_eval(transcripts, [_arm()], out, **kwargs)  # same settings resume
    changed: dict[str, Any] = (
        {"stores": _label_stores(tmp_path, labelled=False)}
        if setting == "egress_rule"
        else {"include_ephemeral": True}
    )
    with pytest.raises(EvalConfigError, match=setting):
        run_eval(transcripts, [_arm()], out, **kwargs, **changed)


def test_an_empty_intersection_says_why(tmp_path: Path) -> None:
    transcripts, out = _dirs(tmp_path)
    write_transcript(transcripts / "p.jsonl", _pack_records("sess-p", PACK_IDS[0]))
    client = UsageClient([candidates_json(good_candidate(evidence=_QUOTE))])
    report = run_eval(
        transcripts,
        [_arm("remote"), _arm("onhost", local=True)],
        out,
        gold=_gold_for("p.jsonl"),
        client_factory=_factory(client),
        local_of=lambda arm: arm.local,
    )
    inter = report["intersection"]
    assert inter["transcripts"] == 0
    assert "remote" in inter["reason"]
    assert "onhost" not in inter["reason"]


def test_an_egress_read_failure_logs_the_class_name_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        judge_eval.logger, "warning", lambda event, **kw: seen.append(kw)
    )
    transcripts, _ = _dirs(tmp_path)
    write_transcript(transcripts / "p.jsonl", _pack_records("sess-p", PACK_IDS[0]))
    stores = _label_stores(tmp_path, labelled=False)

    def down(*_args: Any, **_kwargs: Any) -> Any:
        msg = "stored secret text"
        raise RuntimeError(msg)

    monkeypatch.setattr(stores.operational.event_log, "get_events", down)
    digest = judge_eval.parse_session(transcripts / "p.jsonl")
    reason = judge_eval._eval_egress_permits(
        digest, _arm("remote"), local=False, stores=stores
    )
    assert reason == judge_eval.EGRESS_REASON_UNREADABLE
    assert seen == [{"arm": "remote", "error": "RuntimeError"}]
