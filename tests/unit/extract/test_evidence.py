"""Tests for deterministic evidence override (#308).

Three contract layers:

* ``parse_trace_evidence`` — files touched / read and commands run come
  from tool-call payloads only (Edit/Write shapes, unified-diff hunks),
  never from prose-shaped steps or quoted diffs.
* ``merge_with_evidence`` — evidence values survive verbatim and first;
  supplied values may extend, never displace.
* ``apply_trace_evidence`` — the gate rewrites only the Activity draft,
  demotes extractor-supplied extensions to ``*_unverified`` (and keeps
  them out of ``files_touched`` entirely), is pure and idempotent, and
  runs at the ``extract_trace_batch`` seam.
"""

from __future__ import annotations

from trellis.extract.evidence import (
    COMMANDS_RUN_PROPERTY,
    FILES_READ_PROPERTY,
    FILES_TOUCHED_PROPERTY,
    MAX_COMMAND_CHARS,
    TraceEvidence,
    apply_trace_evidence,
    merge_with_evidence,
    parse_trace_evidence,
    unverified_property_key,
)
from trellis.extract.trace_ingest_hook import extract_trace_batch
from trellis.schemas.extraction import (
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)
from trellis.schemas.trace import Trace


def _trace(steps: list[dict], trace_id: str = "tr_1") -> Trace:
    return Trace.model_validate(
        {
            "trace_id": trace_id,
            "source": "agent",
            "intent": "fix the bug",
            "steps": steps,
            "context": {"agent_id": "a1"},
        }
    )


def _tool(name: str, args: dict, step_type: str = "tool_call") -> dict:
    return {"step_type": step_type, "name": name, "args": args, "result": {}}


def _result(entities: list[EntityDraft], extractor: str = "test") -> ExtractionResult:
    return ExtractionResult(
        entities=entities,
        edges=[],
        extractor_used=extractor,
        tier="llm",
        provenance=ExtractionProvenance(extractor_name=extractor),
    )


def _activity(trace_id: str = "tr_1", properties: dict | None = None) -> EntityDraft:
    return EntityDraft(
        entity_id=f"trace:{trace_id}",
        entity_type="Activity",
        name="fix the bug",
        properties=properties or {},
    )


# ---------------------------------------------------------------------------
# parse_trace_evidence
# ---------------------------------------------------------------------------


class TestParseFilesTouched:
    def test_claude_code_edit_and_write_shapes(self) -> None:
        trace = _trace(
            [
                _tool("Edit", {"file_path": "src/a.py", "old_string": "x"}),
                _tool("Write", {"file_path": "src/b.py", "content": "y"}),
                _tool("MultiEdit", {"file_path": "src/c.py"}),
                _tool("NotebookEdit", {"notebook_path": "nb.ipynb"}),
            ]
        )
        ev = parse_trace_evidence(trace)
        assert ev.files_touched == ["src/a.py", "src/b.py", "src/c.py", "nb.ipynb"]
        assert ev.trace_id == "tr_1"

    def test_docs_worked_example_shape(self) -> None:
        # trace-format.md EXAMPLE_1: ``edit_file`` with a ``file`` arg.
        trace = _trace([_tool("edit_file", {"file": "api/routes.py"})])
        assert parse_trace_evidence(trace).files_touched == ["api/routes.py"]

    def test_order_preserved_and_deduped(self) -> None:
        trace = _trace(
            [
                _tool("Edit", {"file_path": "b.py"}),
                _tool("Edit", {"file_path": "a.py"}),
                _tool("Write", {"file_path": "b.py"}),
            ]
        )
        assert parse_trace_evidence(trace).files_touched == ["b.py", "a.py"]

    def test_non_tool_call_steps_are_ignored(self) -> None:
        trace = _trace([_tool("Edit", {"file_path": "a.py"}, step_type="decision")])
        assert parse_trace_evidence(trace).is_empty()

    def test_unknown_tools_and_blank_paths_contribute_nothing(self) -> None:
        trace = _trace(
            [
                _tool("grep", {"file_path": "a.py"}),
                _tool("Edit", {"file_path": "   "}),
                _tool("Edit", {"file_path": 42}),
            ]
        )
        assert parse_trace_evidence(trace).files_touched == []

    def test_padded_paths_dedupe_against_their_stored_form(self) -> None:
        # TrellisModel stores strings stripped, so the parse must dedupe
        # on the stripped form or the two spellings both survive.
        trace = _trace(
            [
                _tool("Edit", {"file_path": "a.py"}),
                _tool("Write", {"file_path": " a.py"}),
            ]
        )
        assert parse_trace_evidence(trace).files_touched == ["a.py"]


class TestParseStrReplaceEditor:
    """The ``command`` discriminator routes read vs write."""

    def test_view_command_is_a_read_not_a_touch(self) -> None:
        trace = _trace(
            [_tool("str_replace_editor", {"command": "view", "path": "src/foo.py"})]
        )
        ev = parse_trace_evidence(trace)
        assert ev.files_read == ["src/foo.py"]
        assert ev.files_touched == []

    def test_edit_commands_are_touches(self) -> None:
        trace = _trace(
            [
                _tool("str_replace_editor", {"command": "create", "path": "a.py"}),
                _tool("str_replace_editor", {"command": "str_replace", "path": "b.py"}),
                _tool("str_replace_editor", {"command": "insert", "path": "c.py"}),
                _tool("str_replace_editor", {"command": "undo_edit", "path": "d.py"}),
            ]
        )
        ev = parse_trace_evidence(trace)
        assert ev.files_touched == ["a.py", "b.py", "c.py", "d.py"]
        assert ev.files_read == []

    def test_absent_or_unknown_discriminator_contributes_nothing(self) -> None:
        trace = _trace(
            [
                _tool("str_replace_editor", {"path": "a.py"}),
                _tool("str_replace_editor", {"command": "wat", "path": "b.py"}),
                _tool("str_replace_editor", {"command": {"n": 1}, "path": "c.py"}),
            ]
        )
        assert parse_trace_evidence(trace).is_empty()


class TestParsePatchShapes:
    DIFF = "--- a/src/old.py\n+++ b/src/new.py\n@@ -1 +1 @@\n-x\n+y\n"

    def test_patch_arg_key_on_any_tool(self) -> None:
        trace = _trace([_tool("some_tool", {"patch": self.DIFF})])
        assert parse_trace_evidence(trace).files_touched == [
            "src/old.py",
            "src/new.py",
        ]

    def test_patch_named_tool_scans_all_string_args(self) -> None:
        trace = _trace([_tool("apply_patch", {"input": self.DIFF})])
        assert parse_trace_evidence(trace).files_touched == [
            "src/old.py",
            "src/new.py",
        ]

    def test_deletion_hunk_keeps_old_side_and_drops_dev_null(self) -> None:
        diff = "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
        trace = _trace([_tool("git_apply", {"patch": diff})])
        assert parse_trace_evidence(trace).files_touched == ["gone.py"]

    def test_plain_modification_dedupes_both_sides(self) -> None:
        diff = "--- a/same.py\n+++ b/same.py\n@@ -1 +1 @@\n-x\n+y\n"
        trace = _trace([_tool("some_tool", {"diff": diff})])
        assert parse_trace_evidence(trace).files_touched == ["same.py"]

    def test_diff_u_tab_timestamp_suffix_is_stripped(self) -> None:
        diff = "--- same.py\t2026-01-01\n+++ same.py\t2026-01-02\n@@ -1 +1 @@\n"
        trace = _trace([_tool("some_tool", {"unified_diff": diff})])
        assert parse_trace_evidence(trace).files_touched == ["same.py"]

    def test_hunk_body_lines_are_not_read_as_file_headers(self) -> None:
        # A removed SQL comment carries the hunk's own ``-`` prefix and
        # arrives as ``--- drop the temp table``; an added C-ish ``++``
        # line arrives as ``+++ ...``.  Neither is a filename.
        diff = (
            "--- a/models/orders.sql\n"
            "+++ b/models/orders.sql\n"
            "@@ -1,3 +1,3 @@\n"
            " select 1\n"
            "--- drop the temp table before rebuild\n"
            "+++ OTHER.md is quoted here\n"
            " select 2\n"
        )
        trace = _trace([_tool("apply_patch", {"patch": diff})])
        assert parse_trace_evidence(trace).files_touched == ["models/orders.sql"]

    def test_header_pair_without_a_hunk_marker_is_not_evidence(self) -> None:
        prose = "--- not/a/file.py\n+++ also/not/a/file.py\nsome prose\n"
        trace = _trace([_tool("apply_patch", {"patch": prose})])
        assert parse_trace_evidence(trace).files_touched == []

    def test_multi_file_patch_records_every_header(self) -> None:
        diff = (
            "--- a/one.py\n+++ b/one.py\n@@ -1 +1 @@\n-x\n+y\n"
            "--- a/two.py\n+++ b/two.py\n@@ -1 +1 @@\n-p\n+q\n"
        )
        trace = _trace([_tool("apply_patch", {"patch": diff})])
        assert parse_trace_evidence(trace).files_touched == ["one.py", "two.py"]

    def test_quoted_diff_in_write_content_is_not_evidence(self) -> None:
        # A Write whose *content* quotes a diff touches only its own
        # file_path — the quoted paths must not leak into evidence.
        trace = _trace(
            [_tool("Write", {"file_path": "notes.md", "content": self.DIFF})]
        )
        assert parse_trace_evidence(trace).files_touched == ["notes.md"]


class TestParseReadsAndCommands:
    def test_read_tool_populates_files_read(self) -> None:
        trace = _trace(
            [
                _tool("Read", {"file_path": "src/a.py"}),
                _tool("Edit", {"file_path": "src/a.py"}),
            ]
        )
        ev = parse_trace_evidence(trace)
        assert ev.files_read == ["src/a.py"]
        assert ev.files_touched == ["src/a.py"]

    def test_shell_command_recorded_verbatim(self) -> None:
        trace = _trace([_tool("Bash", {"command": "make test"})])
        assert parse_trace_evidence(trace).commands_run == ["make test"]

    def test_long_command_truncated_with_marker(self) -> None:
        long_cmd = "x" * (MAX_COMMAND_CHARS + 50)
        trace = _trace([_tool("bash", {"command": long_cmd})])
        (recorded,) = parse_trace_evidence(trace).commands_run
        assert recorded == "x" * MAX_COMMAND_CHARS + "…"

    def test_shell_files_are_not_inferred_from_commands(self) -> None:
        trace = _trace([_tool("Bash", {"command": "echo hi > /tmp/out.txt"})])
        assert parse_trace_evidence(trace).files_touched == []


# ---------------------------------------------------------------------------
# merge_with_evidence
# ---------------------------------------------------------------------------


class TestMergeWithEvidence:
    def test_evidence_only(self) -> None:
        merge = merge_with_evidence(["a.py", "b.py"], [])
        assert merge.values == ["a.py", "b.py"]
        assert merge.evidence_values == ["a.py", "b.py"]
        assert merge.unverified_values == []

    def test_supplied_extends_after_evidence(self) -> None:
        merge = merge_with_evidence(["a.py"], ["c.py", "a.py"])
        assert merge.values == ["a.py", "c.py"]
        assert merge.unverified_values == ["c.py"]

    def test_supplied_cannot_displace_evidence(self) -> None:
        # A conflicting claim never replaces the parsed value — it can
        # only trail it as an unverified extension.
        merge = merge_with_evidence(["real.py"], ["hallucinated.py"])
        assert merge.values[0] == "real.py"
        assert merge.unverified_values == ["hallucinated.py"]

    def test_no_evidence_means_all_supplied_is_unverified(self) -> None:
        merge = merge_with_evidence([], ["a.py", "a.py", "", 3])  # type: ignore[list-item]
        assert merge.values == ["a.py"]
        assert merge.evidence_values == []
        assert merge.unverified_values == ["a.py"]

    def test_padding_does_not_hide_an_attested_value(self) -> None:
        merge = merge_with_evidence(["a.py"], [" a.py "])
        assert merge.values == ["a.py"]
        assert merge.unverified_values == []


# ---------------------------------------------------------------------------
# apply_trace_evidence
# ---------------------------------------------------------------------------


class TestApplyTraceEvidence:
    def test_populates_activity_from_evidence(self) -> None:
        evidence = TraceEvidence(
            trace_id="tr_1",
            files_touched=["a.py"],
            commands_run=["make test"],
        )
        out = apply_trace_evidence(_result([_activity()]), evidence)
        props = out.entities[0].properties
        assert props[FILES_TOUCHED_PROPERTY] == ["a.py"]
        assert props[COMMANDS_RUN_PROPERTY] == ["make test"]
        assert FILES_READ_PROPERTY not in props
        assert unverified_property_key(FILES_TOUCHED_PROPERTY) not in props

    def test_files_touched_is_evidence_only(self) -> None:
        # #308's reference behaviour: the model's claim about what it
        # modified never joins the attested key — it survives only under
        # the companion, where a reviewer can see it is a claim.
        activity = _activity(
            properties={FILES_TOUCHED_PROPERTY: ["a.py", "hallucinated.py"]}
        )
        evidence = TraceEvidence(trace_id="tr_1", files_touched=["a.py", "b.py"])
        out = apply_trace_evidence(_result([activity]), evidence)
        props = out.entities[0].properties
        assert props[FILES_TOUCHED_PROPERTY] == ["a.py", "b.py"]
        assert props[unverified_property_key(FILES_TOUCHED_PROPERTY)] == [
            "hallucinated.py"
        ]

    def test_files_touched_claim_without_evidence_leaves_the_key_absent(self) -> None:
        activity = _activity(properties={FILES_TOUCHED_PROPERTY: ["hallucinated.py"]})
        out = apply_trace_evidence(_result([activity]), TraceEvidence(trace_id="tr_1"))
        props = out.entities[0].properties
        assert FILES_TOUCHED_PROPERTY not in props
        assert props[unverified_property_key(FILES_TOUCHED_PROPERTY)] == [
            "hallucinated.py"
        ]

    def test_files_read_and_commands_take_the_union(self) -> None:
        # The issue grants a union for the fields that are not a
        # possession claim — marked, but not withheld.
        activity = _activity(
            properties={
                FILES_READ_PROPERTY: ["claimed.py"],
                COMMANDS_RUN_PROPERTY: ["make lint"],
            }
        )
        evidence = TraceEvidence(
            trace_id="tr_1",
            files_read=["seen.py"],
            commands_run=["make test"],
        )
        out = apply_trace_evidence(_result([activity]), evidence)
        props = out.entities[0].properties
        assert props[FILES_READ_PROPERTY] == ["seen.py", "claimed.py"]
        assert props[unverified_property_key(FILES_READ_PROPERTY)] == ["claimed.py"]
        assert props[COMMANDS_RUN_PROPERTY] == ["make test", "make lint"]
        assert props[unverified_property_key(COMMANDS_RUN_PROPERTY)] == ["make lint"]

    def test_supplied_without_evidence_survives_as_unverified(self) -> None:
        activity = _activity(properties={FILES_READ_PROPERTY: ["seen.py"]})
        out = apply_trace_evidence(_result([activity]), TraceEvidence(trace_id="tr_1"))
        props = out.entities[0].properties
        assert props[FILES_READ_PROPERTY] == ["seen.py"]
        assert props[unverified_property_key(FILES_READ_PROPERTY)] == ["seen.py"]

    def test_idempotent(self) -> None:
        activity = _activity(properties={FILES_TOUCHED_PROPERTY: ["x.py"]})
        evidence = TraceEvidence(trace_id="tr_1", files_touched=["a.py"])
        once = apply_trace_evidence(_result([activity]), evidence)
        twice = apply_trace_evidence(once, evidence)
        assert twice.entities[0].properties == once.entities[0].properties

    def test_other_drafts_and_input_result_untouched(self) -> None:
        other = EntityDraft(
            entity_id="tool:bash",
            entity_type="SoftwareApplication",
            name="bash",
            properties={FILES_TOUCHED_PROPERTY: ["not-governed.py"]},
        )
        activity = _activity()
        result = _result([activity, other])
        evidence = TraceEvidence(trace_id="tr_1", files_touched=["a.py"])
        out = apply_trace_evidence(result, evidence)
        assert out.entities[1] is other
        # Purity: the input drafts were not mutated in place.
        assert activity.properties == {}
        assert FILES_TOUCHED_PROPERTY in out.entities[0].properties

    def test_missing_activity_draft_passes_through(self) -> None:
        result = _result([])
        evidence = TraceEvidence(trace_id="tr_1", files_touched=["a.py"])
        assert apply_trace_evidence(result, evidence) is result

    def test_no_evidence_and_no_claims_leaves_no_residue(self) -> None:
        out = apply_trace_evidence(
            _result([_activity()]), TraceEvidence(trace_id="tr_1")
        )
        props = out.entities[0].properties
        for key in (
            FILES_TOUCHED_PROPERTY,
            FILES_READ_PROPERTY,
            COMMANDS_RUN_PROPERTY,
        ):
            assert key not in props
            assert unverified_property_key(key) not in props


# ---------------------------------------------------------------------------
# The seam: extract_trace_batch applies the gate
# ---------------------------------------------------------------------------


class TestSeamIntegration:
    def test_activity_draft_carries_parsed_evidence(self) -> None:
        trace = _trace(
            [
                _tool("Edit", {"file_path": "src/a.py"}),
                _tool("Bash", {"command": "pytest -q"}),
            ]
        )
        result, batch = extract_trace_batch(trace, requested_by="test")
        assert batch is not None
        activity = next(
            e for e in result.entities if e.entity_id == f"trace:{trace.trace_id}"
        )
        assert activity.properties[FILES_TOUCHED_PROPERTY] == ["src/a.py"]
        assert activity.properties[COMMANDS_RUN_PROPERTY] == ["pytest -q"]
        # Deterministic path supplies nothing beyond evidence — no
        # unverified companion keys on a pure-deterministic run.
        assert unverified_property_key(FILES_TOUCHED_PROPERTY) not in (
            activity.properties
        )
