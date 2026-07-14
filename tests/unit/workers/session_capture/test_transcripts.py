"""F8 schema-trap coverage for the transcript parser.

Sidechains, tool_result content arrays, summaries/compaction, unknown record
types, and malformed lines — each must be tolerated, and raw tool output must
never reach the digest.
"""

from __future__ import annotations

from pathlib import Path

from trellis_workers.session_capture.transcripts import (
    discover_sessions,
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


def test_sidechain_records_excluded_from_salient_text(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    side = assistant_turn("subagent internal reasoning")
    side["isSidechain"] = True
    write_transcript(path, [side, assistant_turn("main thread reply")])
    digest = parse_session(path)
    assert digest.sidechain_records == 1
    assert digest.assistant_texts == ["main thread reply"]


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
