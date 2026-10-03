"""F8 schema-trap coverage for the transcript parser.

Sidechains, tool_result content arrays, summaries/compaction, unknown record
types, and malformed lines — each must be tolerated, and raw tool output must
never reach the digest.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from trellis.retrieve.formatters import (
    format_fetched_items_as_markdown,
    format_pack_as_index_markdown,
    format_pack_as_markdown,
    format_sectioned_pack_as_markdown,
)
from trellis.retrieve.withholding import WithholdingSummary
from trellis_workers.session_capture import transcripts
from trellis_workers.session_capture.models import ToolUseRollup
from trellis_workers.session_capture.transcripts import (
    discover_sessions,
    is_ephemeral_project,
    parse_session,
)

from .conftest import (
    CHILD_OUTCOME,
    FAKE_PR_URL,
    PACK_IDS,
    PARENT_OUTCOME,
    WORKED_OUTCOME,
    api_message,
    assistant_tools,
    assistant_turn,
    bash_block,
    delegating_session,
    mcp_envelope,
    pack_markdown,
    pack_result_turn,
    prompt,
    result_block,
    system_note,
    text_block,
    tool_result_turn,
    tool_results,
    tool_use_block,
    usage_block,
    user_turn,
    worked_session,
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


class TestToolErrorAttribution:
    """#306: a ``tool_result`` is joined back to the call it answers.

    ``ToolCall.is_error`` was declared and documented from the start and
    written by nothing — the flag was read off the result and folded
    straight into the session-level ``has_error``, so per-call attribution
    was parsed and then discarded. These pin the join, and pin that it did
    not become a new gate on the flag it replaced.
    """

    def test_error_attributed_to_the_calling_tool_only(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        write_transcript(
            path,
            [
                user_turn("try both"),
                assistant_tools(("Bash", "u-1"), ("Read", "u-2")),
                tool_result_turn(is_error=True, tool_use_id="u-1"),
                tool_result_turn(is_error=False, tool_use_id="u-2"),
            ],
        )
        digest = parse_session(path)
        assert {(c.name, c.is_error) for c in digest.tool_calls} == {
            ("Bash", True),
            ("Read", False),
        }

    def test_rollup_counts_repeats_and_errors_per_tool(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        write_transcript(
            path,
            [
                user_turn("sweep the tree"),
                assistant_tools(("Bash", "u-1"), ("Bash", "u-2"), ("Grep", "u-3")),
                tool_result_turn(is_error=True, tool_use_id="u-2"),
            ],
        )
        rollup = parse_session(path).tool_rollup
        assert [(r.name, r.calls, r.errors) for r in rollup] == [
            ("Bash", 2, 1),
            ("Grep", 1, 0),
        ]

    def test_has_error_survives_an_unjoinable_result(self, tmp_path: Path) -> None:
        """The load-bearing one.

        An errored result whose ``tool_use_id`` names no call in this file
        still means the session hit an error. Routing the existing
        session-level gate through the new join would narrow coverage
        silently — this fails if ``has_error`` is ever derived from the
        join's outcome.
        """
        path = tmp_path / "s.jsonl"
        write_transcript(
            path,
            [
                user_turn("continue from the compaction"),
                assistant_turn("resuming", "Bash", use_id="u-1"),
                tool_result_turn(is_error=True, tool_use_id="u-gone"),
            ],
        )
        digest = parse_session(path)
        assert digest.has_error is True
        # ...and the call it could not be attributed to is not falsely blamed.
        assert [c.is_error for c in digest.tool_calls] == [False]

    def test_has_error_survives_a_result_with_no_id_at_all(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "s.jsonl"
        write_transcript(
            path,
            [
                user_turn("go"),
                assistant_turn("running", "Bash", use_id="u-1"),
                tool_result_turn(is_error=True, tool_use_id=None),
            ],
        )
        digest = parse_session(path)
        assert digest.has_error is True
        assert [c.is_error for c in digest.tool_calls] == [False]

    def test_clean_session_flags_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        write_transcript(
            path,
            [
                user_turn("go"),
                assistant_turn("running", "Bash", use_id="u-1"),
                tool_result_turn(is_error=False, tool_use_id="u-1"),
            ],
        )
        digest = parse_session(path)
        assert digest.has_error is False
        assert digest.tool_rollup == [ToolUseRollup(name="Bash", calls=1, errors=0)]


#: Every Trellis retrieval tool whose result carries the ``**pack_id:**``
#: header, with the title line its formatter prints. Each tool gets its own
#: pack id below, so a parser that reads only some of them cannot match.
_PACK_TOOL_TITLES = (
    ("get_context", "# Context index for: fake intent in index mode"),
    ("search", "# Context for: fake search query"),
    ("get_objective_context", "# Context for: fake objective"),
    ("get_task_context", "# Context for: fake task"),
    ("get_sectioned_context", "# Context for: fake sectioned intent"),
    ("get_items", "# Fetched items"),
)

#: One synthetic item for the tests that run the real formatters.
_ITEM = {
    "item_id": "doc-fake-1",
    "item_type": "document",
    "excerpt": "fake item: the frobnicator boots after migrate",
    "relevance_score": 0.9,
}


def _served(*results: tuple[str, str, object]) -> list[dict]:
    """A transcript serving one tool result per ``(tool, use_id, content)``."""
    records: list[dict] = [user_turn("gather what we know about the widget deploy")]
    for tool, use_id, content in results:
        records.append(assistant_tools((tool, use_id)))
        records.append(pack_result_turn(use_id, content))
    records.append(assistant_turn("done gathering"))
    return records


class TestPackIdExtraction:
    """Which packs a session was served — step 1 of the effectiveness plan.

    A Trellis retrieval tool prints ``**pack_id:** `<id>``` on the line after
    its title. That id is the one thing the digest reads out of a
    ``tool_result``: a generated identifier, kept only once it matches its
    own 26-character alphabet, so no tool output rides along with it.
    """

    def test_a_session_served_two_packs_records_both(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        first, second = PACK_IDS[0], PACK_IDS[1]
        write_transcript(
            path,
            _served(
                (
                    "mcp__trellis__get_context",
                    "u-1",
                    mcp_envelope(pack_markdown(first)),
                ),
                ("mcp__trellis__search", "u-2", mcp_envelope(pack_markdown(second))),
                # Expanding the first pack echoes its id: one pack, not two.
                (
                    "mcp__trellis__get_items",
                    "u-3",
                    mcp_envelope(pack_markdown(first, title="# Fetched items")),
                ),
            ),
        )
        digest = parse_session(path)
        assert digest.pack_ids == [first, second]
        assert digest.retrieval_results == 3
        assert digest.retrieval_errors == 0
        assert digest.pack_ids_unparsed == 0

    def test_every_retrieval_tool_that_prints_the_header_is_read(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "s.jsonl"
        served = [
            (
                f"mcp__trellis__{tool}",
                f"u-{i}",
                mcp_envelope(pack_markdown(pack_id, title=title)),
            )
            for i, ((tool, title), pack_id) in enumerate(
                zip(_PACK_TOOL_TITLES, PACK_IDS, strict=False)
            )
        ]
        write_transcript(path, _served(*served))
        digest = parse_session(path)
        assert digest.pack_ids == list(PACK_IDS[: len(_PACK_TOOL_TITLES)])
        assert digest.retrieval_results == len(_PACK_TOOL_TITLES)

    def test_only_a_trellis_server_counts(self, tmp_path: Path) -> None:
        """The stdio server and the claude.ai connector differ only in the
        server segment of the tool name. A same-named tool on another server,
        or ``Bash`` printing a header, served no Trellis pack."""
        path = tmp_path / "s.jsonl"
        write_transcript(
            path,
            _served(
                (
                    "mcp__trellis__search",
                    "u-1",
                    mcp_envelope(pack_markdown(PACK_IDS[0])),
                ),
                (
                    "mcp__claude_ai_Fake-trellis__get_context",
                    "u-2",
                    mcp_envelope(pack_markdown(PACK_IDS[1])),
                ),
                (
                    "mcp__other-memory__get_context",
                    "u-3",
                    mcp_envelope(pack_markdown(PACK_IDS[2])),
                ),
                ("Bash", "u-4", pack_markdown(PACK_IDS[3])),
            ),
        )
        digest = parse_session(path)
        assert digest.pack_ids == [PACK_IDS[0], PACK_IDS[1]]
        assert digest.retrieval_results == 2

    def test_each_result_shape_is_read(self, tmp_path: Path) -> None:
        """The FastMCP envelope (what Claude Code records today), a bare
        markdown string, and a list of text blocks."""
        path = tmp_path / "s.jsonl"
        write_transcript(
            path,
            _served(
                (
                    "mcp__trellis__get_context",
                    "u-1",
                    mcp_envelope(pack_markdown(PACK_IDS[0])),
                ),
                ("mcp__trellis__get_context", "u-2", pack_markdown(PACK_IDS[1])),
                (
                    "mcp__trellis__get_context",
                    "u-3",
                    [
                        {
                            "type": "text",
                            "text": mcp_envelope(pack_markdown(PACK_IDS[2])),
                        },
                        {"type": "text", "text": "a second fake block"},
                    ],
                ),
            ),
        )
        assert parse_session(path).pack_ids == list(PACK_IDS[:3])

    def test_the_capture_banner_and_a_wrapped_intent_do_not_hide_the_header(
        self, tmp_path: Path
    ) -> None:
        """The banner sits above the title while capture is failing; an
        intent with a newline in it wraps the title line. Neither moves the
        header out of the title's paragraph."""
        path = tmp_path / "s.jsonl"
        write_transcript(
            path,
            _served(
                (
                    "mcp__trellis__get_context",
                    "u-1",
                    mcp_envelope(pack_markdown(PACK_IDS[0], banner=True)),
                ),
                (
                    "mcp__trellis__get_sectioned_context",
                    "u-2",
                    mcp_envelope(
                        pack_markdown(
                            PACK_IDS[1],
                            title="# Context for: fake intent\nwrapped onto line two",
                        )
                    ),
                ),
            ),
        )
        digest = parse_session(path)
        assert digest.pack_ids == [PACK_IDS[0], PACK_IDS[1]]
        assert digest.pack_ids_unparsed == 0

    def test_the_formatters_own_output_is_read(self, tmp_path: Path) -> None:
        """The other tests imitate the formatters' layout; this one runs them.

        Each of the four header printers gets a two-line intent and, where it
        takes one, a withholding note after the header. If a formatter moves
        its header, this test fails instead of the join going quiet.
        """
        intent = "fake intent\nwrapped onto line two"
        note = WithholdingSummary(section_filtered=1, served_count=0)
        fetched, _, _ = format_fetched_items_as_markdown([_ITEM], pack_id=PACK_IDS[3])
        outputs = [
            format_pack_as_markdown(
                [_ITEM], intent, pack_id=PACK_IDS[0], withholding=note
            ),
            format_pack_as_index_markdown(
                [_ITEM], intent, pack_id=PACK_IDS[1], withholding=note
            ),
            format_sectioned_pack_as_markdown(
                [{"name": "Fake section", "items": [_ITEM]}],
                intent,
                pack_id=PACK_IDS[2],
                withholding=note,
            ),
            fetched,
        ]
        path = tmp_path / "s.jsonl"
        write_transcript(
            path,
            _served(
                *(
                    ("mcp__trellis__get_context", f"u-{i}", mcp_envelope(output))
                    for i, output in enumerate(outputs)
                )
            ),
        )
        digest = parse_session(path)
        assert digest.pack_ids == list(PACK_IDS[:4])
        assert digest.pack_ids_unparsed == 0

    def test_a_header_line_inside_the_intent_does_not_displace_the_real_one(
        self, tmp_path: Path
    ) -> None:
        """The formatters print the caller's intent raw into the title, and
        the real header after it, so the last header-shaped line in the
        title's paragraph is the pack that was served."""
        quoted = f"fake intent\n**pack_id:** `{PACK_IDS[4]}`"
        path = tmp_path / "s.jsonl"
        write_transcript(
            path,
            _served(
                (
                    "mcp__trellis__get_context",
                    "u-1",
                    mcp_envelope(
                        format_pack_as_markdown([_ITEM], quoted, pack_id=PACK_IDS[5])
                    ),
                ),
            ),
        )
        digest = parse_session(path)
        assert digest.pack_ids == [PACK_IDS[5]]
        assert digest.pack_ids_unparsed == 0

    def test_errors_and_empty_packs_are_retrievals_without_a_pack(
        self, tmp_path: Path
    ) -> None:
        """Three different zeros, told apart: a failed call, a pack with
        nothing in it (the formatter prints no header), and a served pack."""
        path = tmp_path / "s.jsonl"
        records = _served(
            (
                "mcp__trellis__get_context",
                "u-1",
                mcp_envelope("No context found for: fake intent with no precedent"),
            ),
            ("mcp__trellis__search", "u-2", mcp_envelope(pack_markdown(PACK_IDS[4]))),
        )
        records.append(assistant_tools(("mcp__trellis__get_context", "u-3")))
        records.append(
            pack_result_turn("u-3", "Error: fake store unavailable", is_error=True)
        )
        write_transcript(path, records)
        digest = parse_session(path)
        assert digest.pack_ids == [PACK_IDS[4]]
        assert digest.retrieval_results == 3
        assert digest.retrieval_errors == 1
        assert digest.pack_ids_unparsed == 0

    def test_a_session_that_never_retrieved_has_no_retrievals(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "s.jsonl"
        write_transcript(
            path,
            [
                user_turn("rename the widget module"),
                assistant_turn("renaming", "Edit", use_id="u-1"),
                tool_result_turn(is_error=False, tool_use_id="u-1"),
            ],
        )
        digest = parse_session(path)
        assert digest.pack_ids == []
        assert digest.retrieval_results == 0
        assert digest.pack_ids_unparsed == 0

    def test_a_truncated_or_unrecognised_result_is_counted_not_fatal(
        self, tmp_path: Path
    ) -> None:
        """A result cut off mid-envelope, a header whose id is not a pack id,
        and a content shape the parser has never seen: each is counted as
        unparsed, and the pack served next to them is still recorded."""
        path = tmp_path / "s.jsonl"
        envelope = mcp_envelope(pack_markdown(PACK_IDS[0]))
        # Cut inside the id: the label survives, the envelope does not close.
        truncated = envelope[: envelope.index("**pack_id:**") + 20]
        write_transcript(
            path,
            _served(
                ("mcp__trellis__get_context", "u-1", truncated),
                (
                    "mcp__trellis__get_context",
                    "u-2",
                    mcp_envelope(pack_markdown("not-a-pack-id")),
                ),
                ("mcp__trellis__search", "u-3", {"unexpected": "shape"}),
                (
                    "mcp__trellis__search",
                    "u-4",
                    mcp_envelope(pack_markdown(PACK_IDS[5])),
                ),
            ),
        )
        digest = parse_session(path)
        assert "**pack_id:**" in truncated
        assert digest.pack_ids == [PACK_IDS[5]]
        assert digest.pack_ids_unparsed == 3
        assert digest.retrieval_results == 4
        assert digest.malformed_lines == 0

    def test_a_header_quoted_in_an_item_body_is_not_a_join(
        self, tmp_path: Path
    ) -> None:
        """``get_items`` called without a pack id prints no header, so a
        header-shaped line inside an item belongs to whatever session wrote
        that item. It mentions the label, so it is counted as unparsed."""
        path = tmp_path / "s.jsonl"
        body = "# Fetched items\n\n## fake item\n**pack_id:** `" + PACK_IDS[6] + "`"
        write_transcript(
            path,
            _served(
                ("mcp__trellis__get_items", "u-1", mcp_envelope(body)),
                (
                    "mcp__trellis__get_context",
                    "u-2",
                    mcp_envelope(pack_markdown(PACK_IDS[7])),
                ),
            ),
        )
        digest = parse_session(path)
        assert digest.pack_ids == [PACK_IDS[7]]
        assert digest.pack_ids_unparsed == 1

    def test_no_result_text_reaches_the_digest(self, tmp_path: Path) -> None:
        """F8: the id is read out of the result; the result stays out."""
        path = tmp_path / "s.jsonl"
        write_transcript(
            path,
            _served(
                (
                    "mcp__trellis__get_context",
                    "u-1",
                    mcp_envelope(pack_markdown(PACK_IDS[0])),
                ),
            ),
        )
        digest = parse_session(path)
        assert digest.pack_ids == [PACK_IDS[0]]
        assert "frobnicator" not in repr(digest)
        assert "Context for" not in repr(digest)


class TestParentSessionId:
    """A sub-agent transcript names its parent session by where it lives.

    Claude Code nests sub-agents at
    ``<project>/<parent-session>/subagents/[workflows/<wf>/]agent-*.jsonl``,
    so the parent session is the directory that holds ``subagents``.
    """

    def test_main_and_nested_transcripts(self, tmp_path: Path) -> None:
        project = tmp_path / "-home-me-proj"
        assert (
            transcripts.parent_session_id(project / "sess-main.jsonl", tmp_path) is None
        )
        assert (
            transcripts.parent_session_id(
                project / "sess-main" / "subagents" / "agent-a1.jsonl", tmp_path
            )
            == "sess-main"
        )
        assert (
            transcripts.parent_session_id(
                project
                / "sess-other"
                / "subagents"
                / "workflows"
                / "wf_1"
                / "agent-b2.jsonl",
                tmp_path,
            )
            == "sess-other"
        )

    def test_a_path_outside_the_root_or_off_pattern_has_none(
        self, tmp_path: Path
    ) -> None:
        assert (
            transcripts.parent_session_id(
                Path("/elsewhere/p/s/subagents/a.jsonl"), tmp_path
            )
            is None
        )
        assert (
            transcripts.parent_session_id(
                tmp_path / "p" / "s" / "notes" / "agent-c3.jsonl", tmp_path
            )
            is None
        )


_TOKENS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _tokens(outcome: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(outcome[key] for key in _TOKENS)


def _interrupt(*, at: float) -> dict[str, Any]:
    """The marker Claude Code writes when the person stops a turn."""
    record = prompt("placeholder", at=at)
    record["message"]["content"] = [
        text_block("[Request interrupted by user for tool use]")
    ]
    return record


def _ending(kind: str) -> list[dict[str, Any]]:
    """A session's last records, ending the way *kind* names."""
    call = bash_block("toolu_fake_end", "pytest -q")
    asked = api_message("msg_fake_end", call, at=5, usage=usage_block(1, 2))
    answered = api_message(
        "msg_fake_end", text_block("Fake done."), at=5, usage=usage_block(1, 2)
    )
    failed = tool_results(
        result_block("toolu_fake_end", "fake failure", is_error=True), at=6
    )
    endings = {
        "reply": answered,
        "user": [*answered, prompt("Fake next request.", at=6)],
        "tool_ok": [*asked, tool_results(result_block("toolu_fake_end", "ok"), at=6)],
        "tool_error": [*asked, failed],
        "api_error": [
            *answered,
            *api_message(
                "msg_fake_err",
                text_block("API Error: fake overload"),
                at=7,
                usage=usage_block(0, 0),
                api_error=True,
            ),
        ],
        "pending_tool": asked,
        "interrupt": [*answered, _interrupt(at=6)],
        "interrupted_tool": [*asked, failed, _interrupt(at=6)],
        "tool_error_then_harness": [
            *asked,
            failed,
            prompt("fake caveat", at=7, isMeta=True),
            prompt("Fake compaction summary.", at=8, isCompactSummary=True),
            system_note(at=9),
            {"type": "summary", "summary": "fake summary", "leafUuid": "fake-leaf"},
        ],
    }
    return [prompt("Fake task.", at=0), *endings[kind]]


class TestSessionOutcome:
    """Each transcript's outcome, read from the transcript and no pack.

    The agent's grade of a pack cannot be both the readout and the outcome,
    and traces are nearly all "success", so the outcome is read from what
    the session did. These are the candidates a pre-registration chooses
    among; none is chosen here. Counts, one duration and two flags, so no
    transcript string can ride along.
    """

    def _parse(self, tmp_path: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
        path = tmp_path / "proj" / "sess-fake-0001.jsonl"
        write_transcript(path, records)
        return parse_session(path).outcome.to_payload()

    def test_each_counter_on_a_worked_session(self, tmp_path: Path) -> None:
        assert self._parse(tmp_path, worked_session()) == WORKED_OUTCOME

    def test_a_message_split_over_records_is_one_turn_and_its_usage_once(
        self, tmp_path: Path
    ) -> None:
        """Claude Code writes one record per content block, each carrying
        the message's usage so far: input and cache repeat on every record
        and the last record's output is the message's total."""
        outcome = self._parse(
            tmp_path,
            api_message(
                "msg_fake_split",
                text_block("Reading two fake files."),
                tool_use_block("toolu_fake_r1", "Read", file_path="/fake/a.md"),
                tool_use_block("toolu_fake_r2", "Read", file_path="/fake/b.md"),
                at=0,
                usage=usage_block(7, 90, cache_read=500, cache_creation=60),
            ),
        )
        assert (outcome["assistant_turns"], outcome["tool_calls"]) == (1, 2)
        assert _tokens(outcome) == (7, 90, 500, 60)

    def test_a_split_message_keeps_each_fields_largest_snapshot(
        self, tmp_path: Path
    ) -> None:
        """A snapshot only grows while the message streams, so the largest
        value each field reached is the message's total, whichever record
        carries it and wherever that record sits in the file."""
        records = api_message(
            "msg_fake_split",
            text_block("Fake."),
            tool_use_block("toolu_fake_r1", "Read", file_path="/fake/a.md"),
            at=0,
            usage=usage_block(7, 90, cache_read=500, cache_creation=60),
        )
        outcome = self._parse(tmp_path, records[::-1])
        assert outcome["assistant_turns"] == 1
        assert _tokens(outcome) == (7, 90, 500, 60)

    def test_a_transcript_without_usage_has_null_tokens_not_zero(
        self, tmp_path: Path
    ) -> None:
        """ "Not recorded" and "none" are different answers: a zero would
        read as a session that cost nothing. A record with no message id
        (the older shape) is a turn of its own."""
        outcome = self._parse(
            tmp_path,
            [
                prompt("Fake question.", at=0),
                *api_message("msg_fake_a", text_block("Fake answer."), at=1),
                assistant_turn("Fake answer without a message id."),
            ],
        )
        assert (outcome["assistant_turns"], outcome["assistant_turns_with_usage"]) == (
            2,
            0,
        )
        assert _tokens(outcome) == (None, None, None, None)

    def test_a_recorded_zero_stays_zero(self, tmp_path: Path) -> None:
        outcome = self._parse(
            tmp_path,
            api_message(
                "msg_fake_z", text_block("Fake."), at=0, usage=usage_block(0, 0)
            ),
        )
        assert outcome["assistant_turns_with_usage"] == 1
        assert _tokens(outcome) == (0, 0, 0, 0)

    def test_tokens_sum_only_the_messages_that_recorded_them(
        self, tmp_path: Path
    ) -> None:
        outcome = self._parse(
            tmp_path,
            [
                *api_message(
                    "msg_fake_u",
                    text_block("Fake."),
                    at=0,
                    usage=usage_block(5, 6, cache_read=7, cache_creation=8),
                ),
                *api_message("msg_fake_n", text_block("Fake again."), at=1),
            ],
        )
        assert (outcome["assistant_turns"], outcome["assistant_turns_with_usage"]) == (
            2,
            1,
        )
        assert _tokens(outcome) == (5, 6, 7, 8)

    def test_a_token_field_missing_or_not_a_count_is_null_alone(
        self, tmp_path: Path
    ) -> None:
        outcome = self._parse(
            tmp_path,
            api_message(
                "msg_fake_p",
                text_block("Fake."),
                at=0,
                usage={
                    "input_tokens": 5,
                    "output_tokens": 6,
                    "cache_read_input_tokens": "12",
                    "cache_creation_input_tokens": True,
                },
            ),
        )
        assert _tokens(outcome) == (5, 6, None, None)

    def test_an_api_error_is_an_ending_not_a_turn(self, tmp_path: Path) -> None:
        """Claude Code writes a record of its own, in the model's place,
        when the API call fails or there is nothing to answer; neither is a
        turn the model took."""
        outcome = self._parse(
            tmp_path,
            [
                *api_message(
                    "msg_fake_ok", text_block("Fake."), at=0, usage=usage_block(3, 4)
                ),
                *api_message(
                    "msg_fake_note",
                    text_block("fake synthetic note"),
                    at=1,
                    usage=usage_block(0, 0),
                    model="<synthetic>",
                ),
                *api_message(
                    "msg_fake_err",
                    text_block("API Error: fake overload"),
                    at=2,
                    usage=usage_block(0, 0),
                    api_error=True,
                ),
            ],
        )
        assert (outcome["assistant_turns"], outcome["assistant_turns_with_usage"]) == (
            1,
            1,
        )
        assert _tokens(outcome) == (3, 4, 0, 0)
        assert outcome["ended_on_error"] is True

    def test_the_wall_clock_is_null_without_a_stamp_and_zero_with_one(
        self, tmp_path: Path
    ) -> None:
        unstamped = [prompt("Fake.", at=None)]
        unstamped += api_message("msg_fake_t", text_block("Fake."), at=None)
        assert self._parse(tmp_path, unstamped)["wall_clock_seconds"] is None
        stamped_once = [prompt("Fake.", at=12.5)]
        stamped_once += api_message("msg_fake_t", text_block("Fake."), at=None)
        assert self._parse(tmp_path, stamped_once)["wall_clock_seconds"] == 0.0

    def test_the_wall_clock_spans_every_stamp_it_can_read(self, tmp_path: Path) -> None:
        """Stamps run backwards in real transcripts, so the span is the
        latest less the earliest, not the last less the first. A stamp with
        no zone is read as UTC; one that does not parse is passed over
        without costing its record."""
        naive = prompt("Fake late prompt.", at=None)
        naive["timestamp"] = "2026-09-30T09:01:30"
        garbled = prompt("Fake garbled prompt.", at=None)
        garbled["timestamp"] = "not a fake time"
        outcome = self._parse(
            tmp_path,
            [
                prompt("Fake start.", at=10),
                naive,
                garbled,
                system_note(at=-5),
                *api_message("msg_fake_w", text_block("Fake."), at=30),
            ],
        )
        assert outcome["wall_clock_seconds"] == 95.0
        assert outcome["user_turns"] == 3

    def test_a_user_turn_is_a_persons_text_not_the_harness(
        self, tmp_path: Path
    ) -> None:
        """Claude Code writes a background task's notification and a local
        command's output as plain user text, on its own account. A slash
        command and an interrupt marker are a person's doing."""
        outcome = self._parse(
            tmp_path,
            [
                prompt("Fake task.", at=0),
                prompt("<command-name>/fake</command-name>", at=1),
                prompt("<local-command-stdout>fake</local-command-stdout>", at=2),
                prompt("<local-command-stderr>fake</local-command-stderr>", at=3),
                prompt("<task-notification>fake</task-notification>", at=4),
                _interrupt(at=5),
            ],
        )
        assert outcome["user_turns"] == 3

    def test_a_record_written_again_counts_once(self, tmp_path: Path) -> None:
        """A resumed session writes its history into the file again, each
        record under the ``uuid`` it had and some harness fields changed.
        The copy is not new work; a record under a fresh ``uuid`` is, even
        one saying the same thing."""
        history = [
            prompt("Fake task.", at=0),
            *api_message(
                "msg_fake_d1",
                bash_block("toolu_fake_d1", "git commit -m fake"),
                at=1,
                usage=usage_block(5, 10),
            ),
            tool_results(
                result_block("toolu_fake_d1", "fake hook refusal", is_error=True),
                at=2,
            ),
            *api_message(
                "msg_fake_d2",
                bash_block("toolu_fake_d2", "git commit -m fake && gh pr create"),
                at=3,
                usage=usage_block(6, 20),
            ),
            tool_results(result_block("toolu_fake_d2", FAKE_PR_URL.format(4)), at=4),
        ]
        again = [{**record, "version": "2.0.1"} for record in history]
        outcome = self._parse(tmp_path, [*history, *again, prompt("Fake task.", at=5)])
        assert (
            outcome["tool_calls"],
            outcome["tool_errors"],
            outcome["user_turns"],
            outcome["commits"],
            outcome["prs_created"],
        ) == (2, 1, 2, 1, 1)
        assert outcome["assistant_turns"] == 2
        assert _tokens(outcome) == (11, 30, 0, 0)

    @pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset")
    def test_a_stamp_without_a_zone_is_utc_whatever_the_host_zone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read as the host's local time, the stamp below would move the
        span by the host's offset. CI runs in UTC, where the two readings
        agree, so the zone is set here, five hours west, not trusted."""
        naive = prompt("Fake late prompt.", at=None)
        naive["timestamp"] = "2026-09-30T09:01:30"
        records = [prompt("Fake start.", at=0), naive]
        try:
            with monkeypatch.context() as patch:
                patch.setenv("TZ", "EST+5")
                time.tzset()
                outcome = self._parse(tmp_path, records)
        finally:
            time.tzset()
        assert outcome["wall_clock_seconds"] == 90.0

    def test_a_failed_commit_is_not_a_commit(self, tmp_path: Path) -> None:
        outcome = self._parse(
            tmp_path,
            [
                *api_message(
                    "msg_fake_c", bash_block("toolu_fake_c", "git commit -m fake"), at=0
                ),
                tool_results(
                    result_block("toolu_fake_c", "fake hook refusal", is_error=True),
                    at=1,
                ),
            ],
        )
        assert (outcome["commits"], outcome["tool_errors"]) == (0, 1)

    def test_an_unanswered_commit_is_not_a_commit(self, tmp_path: Path) -> None:
        outcome = self._parse(
            tmp_path,
            api_message(
                "msg_fake_c", bash_block("toolu_fake_c", "git commit -m fake"), at=0
            ),
        )
        assert outcome["commits"] == 0
        assert outcome["ended_interrupted"] is True

    def test_a_failed_pr_create_counts_neither_the_pr_nor_the_url_it_prints(
        self, tmp_path: Path
    ) -> None:
        """``gh pr create`` on a branch that already has a PR fails, and
        prints the URL of the PR that branch already has."""
        outcome = self._parse(
            tmp_path,
            [
                *api_message(
                    "msg_fake_pr",
                    bash_block("toolu_fake_pr", "gh pr create --fill"),
                    at=0,
                ),
                tool_results(
                    result_block(
                        "toolu_fake_pr",
                        "fake: a PR already exists " + FAKE_PR_URL.format(7),
                        is_error=True,
                    ),
                    at=1,
                ),
            ],
        )
        assert (outcome["prs_created"], outcome["pr_urls"]) == (0, 0)

    def test_a_pr_url_anywhere_else_is_not_counted(self, tmp_path: Path) -> None:
        """Only the result of a commit, ``gh pr create`` or ``gh pr merge``
        that succeeded is read for a URL, and only a ``Bash`` call is read
        for a command at all."""
        outcome = self._parse(
            tmp_path,
            [
                prompt("Fake: look at " + FAKE_PR_URL.format(9), at=0),
                *api_message(
                    "msg_fake_v",
                    bash_block("toolu_fake_v", "gh pr view 7 --json url"),
                    tool_use_block("toolu_fake_rd", "Read", file_path="/fake/notes.md"),
                    tool_use_block(
                        "toolu_fake_sh",
                        "mcp__fake__shell",
                        command="gh pr create --fill",
                    ),
                    at=1,
                ),
                tool_results(result_block("toolu_fake_v", FAKE_PR_URL.format(7)), at=2),
                tool_results(
                    result_block("toolu_fake_rd", "fake " + FAKE_PR_URL.format(8)), at=2
                ),
                tool_results(
                    result_block("toolu_fake_sh", FAKE_PR_URL.format(6)), at=2
                ),
            ],
        )
        assert outcome["pr_urls"] == 0
        assert (outcome["commits"], outcome["prs_created"], outcome["prs_merged"]) == (
            0,
            0,
            0,
        )

    @pytest.mark.parametrize(
        ("command", "made"),
        [
            ("git commit -m fake", (1, 0, 0)),
            ("cd /fake/repo && git commit -am fake", (1, 0, 0)),
            ("GIT_EDITOR=true git -C /fake/repo commit --amend", (1, 0, 0)),
            ("git -c user.name=fake -c user.email=fake commit -m fake", (1, 0, 0)),
            ("/usr/bin/git commit -m fake", (1, 0, 0)),
            ("git add -A\ngit commit -m fake", (1, 0, 0)),
            # One call that commits twice is one call that committed.
            ("git commit -m one; git commit --amend --no-edit", (1, 0, 0)),
            ("echo git commit", (0, 0, 0)),
            ("git log --grep commit", (0, 0, 0)),
            ("git commit-tree fake", (0, 0, 0)),
            ("grep -rn 'git commit' /fake/docs", (0, 0, 0)),
            ("gh pr create --fill", (0, 1, 0)),
            ("gh pr merge 7 --squash", (0, 0, 1)),
            ("gh pr view 7", (0, 0, 0)),
            ("gh pr create --fill && gh pr merge --auto --squash", (0, 1, 1)),
            ("git commit -am fake && git push && gh pr create --fill", (1, 1, 0)),
        ],
    )
    def test_what_a_bash_call_made(
        self, tmp_path: Path, command: str, made: tuple[int, int, int]
    ) -> None:
        """A step counts where the shell would run it: first in a command,
        after any ``NAME=value`` and, for ``git``, its global options."""
        outcome = self._parse(
            tmp_path,
            [
                *api_message(
                    "msg_fake_cmd", bash_block("toolu_fake_cmd", command), at=0
                ),
                tool_results(result_block("toolu_fake_cmd", "fake ok"), at=1),
            ],
        )
        assert (
            outcome["commits"],
            outcome["prs_created"],
            outcome["prs_merged"],
        ) == made

    @pytest.mark.parametrize(
        ("kind", "ended"),
        [
            ("reply", (False, False)),
            ("user", (False, False)),
            ("tool_ok", (False, False)),
            ("tool_error", (True, False)),
            ("api_error", (True, False)),
            ("pending_tool", (False, True)),
            ("interrupt", (False, True)),
            ("interrupted_tool", (False, True)),
            ("tool_error_then_harness", (True, False)),
        ],
    )
    def test_how_the_session_ended(
        self, tmp_path: Path, kind: str, ended: tuple[bool, bool]
    ) -> None:
        """Read off the last record of the conversation; what the harness
        writes after it (a caveat, a compaction summary, a system note) is
        not the conversation."""
        outcome = self._parse(tmp_path, _ending(kind))
        assert (outcome["ended_on_error"], outcome["ended_interrupted"]) == ended

    def test_an_empty_transcript_has_an_all_zero_outcome(self, tmp_path: Path) -> None:
        outcome = self._parse(tmp_path, [])
        assert outcome == {
            **dict.fromkeys(WORKED_OUTCOME, 0),
            **dict.fromkeys(_TOKENS, None),
            "wall_clock_seconds": None,
            "ended_on_error": False,
            "ended_interrupted": False,
        }

    def test_a_sub_agent_and_its_parent_count_apart(self, tmp_path: Path) -> None:
        """The sub-agent's transcript records its own work. The parent's
        ``Agent`` result reports the child's totals and prints the PR the
        child opened; the parent reads neither, so a rollup of parent and
        children counts each step once."""
        parent, child = delegating_session("sess-parent-0001")
        root = tmp_path / "projects"
        parent_path = root / "proj" / "sess-parent-0001.jsonl"
        child_path = (
            root / "proj" / "sess-parent-0001" / "subagents" / "agent-fk1.jsonl"
        )
        write_transcript(parent_path, parent)
        write_transcript(child_path, child)

        assert parse_session(parent_path).outcome.to_payload() == PARENT_OUTCOME
        assert parse_session(child_path).outcome.to_payload() == CHILD_OUTCOME

    @pytest.mark.parametrize("records", ["worked", "empty", "garbled"])
    def test_every_value_is_a_number_a_flag_or_null(
        self, tmp_path: Path, records: str
    ) -> None:
        """The content rule: transcripts are private and the repo is public,
        so the outcome holds no string. Values that are not counts, where a
        count belongs, are dropped rather than carried."""
        garbled = api_message(
            "msg_fake_g",
            bash_block("toolu_fake_g", "git commit -m 'fake secret-shaped text'"),
            at=0,
            usage={"input_tokens": "fake", "output_tokens": [1], "service_tier": "x"},
        )
        garbled.append(
            tool_results(result_block("toolu_fake_g", FAKE_PR_URL.format(3)), at=1)
        )
        transcript = {"worked": worked_session(), "empty": [], "garbled": garbled}
        outcome = self._parse(tmp_path, transcript[records])

        assert list(outcome) == list(WORKED_OUTCOME)
        for key, value in outcome.items():
            assert value is None or type(value) in (int, float, bool), key
        assert json.loads(json.dumps(outcome)) == outcome

    def test_no_command_or_result_text_reaches_the_digest(self, tmp_path: Path) -> None:
        """F8: the outcome reads a ``Bash`` command and a result's text in
        memory and keeps neither, on the outcome or anywhere on the digest."""
        path = tmp_path / "proj" / "sess-worked-0001.jsonl"
        write_transcript(path, worked_session())
        digest = parse_session(path)
        for fragment in ("git commit", "fake change", "fake-owner", "pull/101"):
            assert fragment not in repr(digest)
