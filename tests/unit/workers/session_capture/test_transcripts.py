"""F8 schema-trap coverage for the transcript parser.

Sidechains, tool_result content arrays, summaries/compaction, unknown record
types, and malformed lines — each must be tolerated, and raw tool output must
never reach the digest.
"""

from __future__ import annotations

from pathlib import Path

from trellis_workers.session_capture.transcripts import (
    discover_sessions,
    is_ephemeral_project,
    parse_session,
)

from .conftest import (
    assistant_turn,
    tool_result_turn,
    user_turn,
    write_transcript,
)


def test_discover_missing_root_is_empty(tmp_path: Path) -> None:
    assert discover_sessions(tmp_path / "does-not-exist") == []


def test_discover_finds_nested_jsonl(tmp_path: Path) -> None:
    write_transcript(tmp_path / "projA" / "s1.jsonl", [user_turn("hi")])
    write_transcript(tmp_path / "projB" / "s2.jsonl", [user_turn("yo")])
    found = discover_sessions(tmp_path)
    assert [p.name for p in found] == ["s1.jsonl", "s2.jsonl"]


def test_basic_turns_and_session_id(tmp_path: Path) -> None:
    path = tmp_path / "sess-fake-0001.jsonl"
    write_transcript(
        path,
        [user_turn("please fix the deploy"), assistant_turn("on it", "Bash")],
    )
    digest = parse_session(path)
    assert digest.session_id == "sess-fake-0001"
    assert digest.user_texts == ["please fix the deploy"]
    assert digest.assistant_texts == ["on it"]
    assert [c.name for c in digest.tool_calls] == ["Bash"]


def test_malformed_line_skipped_and_counted(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    write_transcript(
        path,
        [
            user_turn("valid one"),
            "{ this is not valid json",
            assistant_turn("still parsed"),
        ],
    )
    digest = parse_session(path)
    assert digest.malformed_lines == 1
    assert digest.user_texts == ["valid one"]
    assert digest.assistant_texts == ["still parsed"]


def test_unknown_record_type_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    write_transcript(
        path,
        [
            {"type": "file-history-snapshot", "snapshot": {"any": "shape"}},
            user_turn("after the unknown record"),
        ],
    )
    digest = parse_session(path)
    assert digest.unknown_records == 1
    assert digest.user_texts == ["after the unknown record"]


def test_summary_records_counted_not_treated_as_turns(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    write_transcript(
        path,
        [
            {"type": "summary", "summary": "a compaction summary", "leafUuid": "x"},
            user_turn("real turn"),
        ],
    )
    digest = parse_session(path)
    assert digest.summary_records == 1
    assert "a compaction summary" not in digest.user_texts


def test_sidechain_records_excluded_when_a_main_thread_exists(tmp_path: Path) -> None:
    """The original rule, unchanged: a MIXED file keeps only its main thread."""
    path = tmp_path / "s.jsonl"
    side = assistant_turn("subagent internal reasoning")
    side["isSidechain"] = True
    write_transcript(path, [side, assistant_turn("main thread reply")])
    digest = parse_session(path)
    assert digest.sidechain_records == 1
    assert digest.assistant_texts == ["main thread reply"]
    assert digest.is_subagent is False


class TestDedicatedSubAgentTranscripts:
    """A file that is *only* sidechain is that sub-agent's conversation (#332).

    The exclusion rule was written for sidechain records interleaved into a
    main session's file, where dropping them keeps the digest from reading as
    one linear conversation. Claude Code now writes each sub-agent thread to
    its own ``agent-*.jsonl``, where every record is sidechain — measured on a
    real corpus, 158 of 257 transcripts were pure sidechain and **0 were
    mixed**, so a blanket skip discarded 61% of the corpus (and its largest
    files) to guard against a shape that no longer occurs.
    """

    def _subagent_transcript(self, path: Path) -> None:
        turns = [
            user_turn("Analyze the store layer and report back."),
            assistant_turn("Reading the store layer", "Grep"),
            assistant_turn("The pool is opened per call, not reused."),
        ]
        for turn in turns:
            turn["isSidechain"] = True
        write_transcript(path, turns)

    def test_pure_sidechain_file_is_captured_and_flagged(self, tmp_path: Path) -> None:
        path = tmp_path / "agent-a1b2c3.jsonl"
        self._subagent_transcript(path)
        digest = parse_session(path)

        assert not digest.is_empty
        assert digest.is_subagent is True
        assert digest.assistant_texts == [
            "Reading the store layer",
            "The pool is opened per call, not reused.",
        ]
        assert digest.user_texts == ["Analyze the store layer and report back."]

    def test_sub_agent_turns_keep_chronological_order(self, tmp_path: Path) -> None:
        path = tmp_path / "agent-a1b2c3.jsonl"
        self._subagent_transcript(path)
        salient = parse_session(path).salient_text
        assert salient.splitlines() == [
            "USER: Analyze the store layer and report back.",
            "ASSISTANT: Reading the store layer",
            "ASSISTANT: The pool is opened per call, not reused.",
        ]

    def test_signals_are_detected_on_the_resolved_thread(self, tmp_path: Path) -> None:
        """Error/correction detection must read the turns actually kept.

        If ``resolve_thread`` ran after the detectors, a sub-agent file would
        score its signals against an empty turn list and never be
        capture-mandatory.
        """
        path = tmp_path / "agent-a1b2c3.jsonl"
        turns = [
            user_turn("no, that's wrong - the pool is per-call"),
            assistant_turn("Corrected: reusing the pool now."),
        ]
        for turn in turns:
            turn["isSidechain"] = True
        write_transcript(path, turns)

        digest = parse_session(path)
        assert digest.is_subagent is True
        assert digest.has_correction is True

    def test_empty_file_is_not_flagged_as_subagent(self, tmp_path: Path) -> None:
        write_transcript(tmp_path / "agent-empty.jsonl", [])
        digest = parse_session(tmp_path / "agent-empty.jsonl")
        assert digest.is_empty
        assert digest.is_subagent is False


def test_tool_result_content_array_error_sets_flag_but_drops_output(
    tmp_path: Path,
) -> None:
    path = tmp_path / "s.jsonl"
    write_transcript(
        path,
        [
            user_turn("run the tests"),
            assistant_turn("running", "Bash"),
            tool_result_turn(is_error=True),
        ],
    )
    digest = parse_session(path)
    assert digest.has_error is True
    # The raw tool output ("raw tool output here") must never reach the digest.
    assert all("raw tool output" not in t for t in digest.user_texts)
    assert digest.salient_text.count("raw tool output") == 0


def test_correction_detected(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    write_transcript(
        path,
        [user_turn("actually, the config lives in settings.toml, not env vars")],
    )
    digest = parse_session(path)
    assert digest.has_correction is True


def test_unreadable_file_yields_empty_digest(tmp_path: Path) -> None:
    # A directory with a .jsonl name cannot be opened as a file.
    weird = tmp_path / "dir.jsonl"
    weird.mkdir()
    digest = parse_session(weird)
    assert digest.malformed_lines == 1
    assert digest.is_empty


def test_salient_text_preserves_chronological_interleaving(tmp_path: Path) -> None:
    """A conversation is one ordered stream, not a user block then an assistant block.

    The digest used to hold two independent lists and join them
    all-users-then-all-assistants. On a long session that put every user turn
    in the head and every assistant turn in the tail, so the elided window the
    judge sees never contained an adjacent pair — a correction was separated
    from the thing it corrected by the whole rest of the session. Measured on
    a real 51k-char transcript, restoring order took one session from 0 to 3
    distilled candidates at an unchanged cap.
    """
    path = tmp_path / "sess-fake-0002.jsonl"
    write_transcript(
        path,
        [
            user_turn("add the retry"),
            assistant_turn("added a retry with backoff", "Edit"),
            user_turn("no, that is wrong - it must be idempotent first"),
            assistant_turn("reverted; making the write idempotent", "Edit"),
        ],
    )
    salient = parse_session(path).salient_text

    assert salient.splitlines() == [
        "USER: add the retry",
        "ASSISTANT: added a retry with backoff",
        "USER: no, that is wrong - it must be idempotent first",
        "ASSISTANT: reverted; making the write idempotent",
    ]
    # The correction and the response it provoked stay adjacent — this is the
    # property the blocked ordering destroyed.
    correction = salient.index("no, that is wrong")
    response = salient.index("reverted; making the write idempotent")
    assert 0 < response - correction < 120


def test_role_views_stay_ordered_and_filtered(tmp_path: Path) -> None:
    """``user_texts`` / ``assistant_texts`` remain usable role-filtered views."""
    path = tmp_path / "sess-fake-0003.jsonl"
    write_transcript(
        path,
        [
            user_turn("first ask"),
            assistant_turn("first answer", "Bash"),
            user_turn("second ask"),
        ],
    )
    digest = parse_session(path)
    assert digest.user_texts == ["first ask", "second ask"]
    assert digest.assistant_texts == ["first answer"]
    assert not digest.is_empty


class TestEphemeralProjectSkip:
    """A session run in a throwaway directory has no durable project.

    Claude Code names each project directory after the session's working
    directory with separators flattened, so ``/tmp/tmpa1b2c3`` becomes
    ``-tmp-tmpa1b2c3``. Tooling that shells out to Claude in a scratch
    directory produces transcripts whose subject is whatever was pasted in.
    Measured on a real corpus, every memory distilled from these directories
    was third-party document content — 29% of a first capture run.
    """

    def test_temp_root_projects_are_ephemeral(self, tmp_path: Path) -> None:
        for project in (
            "-tmp-tmpa1b2c3",
            "-tmp",
            "-var-tmp-scratch",
            "-private-var-tmp-x",
        ):
            path = tmp_path / project / "s.jsonl"
            assert is_ephemeral_project(path, tmp_path), project

    def test_real_projects_are_not_ephemeral(self, tmp_path: Path) -> None:
        for project in (
            "-home-nronsse-projects-trellis-ai",
            "-home-nronsse",
            "-srv-tmpl-app",
            "-opt-tmpfiles",
        ):
            path = tmp_path / project / "s.jsonl"
            assert not is_ephemeral_project(path, tmp_path), project

    def test_prefix_match_does_not_catch_a_lookalike(self, tmp_path: Path) -> None:
        """``-tmpl-...`` is ``/tmpl``, a real directory — not ``/tmp``."""
        assert not is_ephemeral_project(
            tmp_path / "-tmpl-project" / "s.jsonl", tmp_path
        )

    def test_nested_subagent_transcript_inherits_its_project(
        self, tmp_path: Path
    ) -> None:
        """A sub-agent transcript is judged by its project, not its parent dir.

        Sub-agents nest at ``<project>/<parent-session>/subagents/agent-*``,
        so the immediate parent is ``subagents`` and carries no cwd at all.
        Reading the parent would exempt every sub-agent transcript from the
        rule — silently, and only once #332 made them capturable.
        """
        nested = "sess-uuid/subagents/workflows/wf_1/agent-a1.jsonl"
        assert is_ephemeral_project(tmp_path / "-tmp-tmpa1b2c3" / nested, tmp_path)
        assert not is_ephemeral_project(tmp_path / "-home-me-proj" / nested, tmp_path)

    def test_discovery_stays_unfiltered(self, tmp_path: Path) -> None:
        """The skip belongs to the sweep, so it can be counted in the report.

        Filtering inside discovery would make the gap invisible — which is
        how a capture gap gets reported as a sampling decision.
        """
        write_transcript(tmp_path / "-tmp-tmpxyz" / "s1.jsonl", [user_turn("hi")])
        write_transcript(tmp_path / "-home-me-proj" / "s2.jsonl", [user_turn("yo")])
        assert len(discover_sessions(tmp_path)) == 2
