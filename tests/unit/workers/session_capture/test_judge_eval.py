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
