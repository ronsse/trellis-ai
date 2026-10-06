"""``trellis extract traces`` and ``extract refresh`` report what their batch answered.

Both commands submit a governed batch, and the executor answers each command
SUCCESS, DUPLICATE, REJECTED or FAILED. Their summaries count those answers
by status, under the names the REST extract route uses, in JSON and text
alike. The exit follows ``trellis ingest dbt-manifest`` (#687): a run whose
every command was refused or failed exits by the first such result (``3``
for a policy, ``2`` for another refusal, ``5`` for a failure) with
``"status": "error"``, and a run that wrote anything exits ``0`` with its
failures counted and the first one named.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner, Result

from tests.cli_output import plain
from trellis.core.error_sanitize import SUPPRESSED_MARKER
from trellis.mutate.commands import CommandBatch, CommandResult, CommandStatus
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.policy_source import POLICY_FILENAME
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis_cli import extract_refresh as extract_cli
from trellis_cli.exit_codes import EXIT_POLICY, EXIT_STORE, EXIT_VALIDATION
from trellis_cli.main import app
from trellis_cli.stores import get_graph_store

runner = CliRunner()

#: A closing tag with no opener. Printed as markup, Rich raises
#: ``MarkupError`` and the command dies with exit 1 instead of reporting.
_MESSAGE = "boom [/x]"

_FORMATS = pytest.mark.parametrize("fmt", ["json", "text"])

_POLICY = {"rejection_reason": "policy_violation"}

#: Every command in the run answers *status*; the run exits *code*. The
#: policy refusal (``3``) runs through the real executor in the denied tests.
_WROTE_NOTHING = pytest.mark.parametrize(
    ("status", "metadata", "code"),
    [
        (CommandStatus.REJECTED, {"rejection_reason": "orphan_edge"}, EXIT_VALIDATION),
        (CommandStatus.FAILED, {}, EXIT_STORE),
    ],
    ids=["validation", "failed"],
)

#: The answers of a run with one refusal among writes that landed.
_MIXED = [
    (CommandStatus.SUCCESS, {}),
    (CommandStatus.REJECTED, _POLICY),
    (CommandStatus.SUCCESS, {}),
]

#: The domain is printed per trace; Rich would delete its ``[ops]``.
_TRACE: dict = {
    "source": "agent",
    "intent": "fix the import",
    "steps": [{"step_type": "tool_call", "name": "grep"}],
    "context": {"agent_id": "a1", "domain": "data[ops]"},
}

#: Three dbt models: three entity writes, no edges.
_MANIFEST: dict = {
    "metadata": {"adapter_type": "snowflake"},
    "nodes": {
        f"model.p.m{i}": {
            "unique_id": f"model.p.m{i}",
            "resource_type": "model",
            "name": f"m{i}",
            "schema": "marts",
            "database": "analytics",
            "config": {"materialized": "table"},
        }
        for i in range(3)
    },
}


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CLI stores at a temp directory, with ingest-time extraction off."""
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    monkeypatch.delenv("TRELLIS_ENABLE_TRACE_EXTRACTION", raising=False)


def _invoke(args: list[str], fmt: str) -> Result:
    """Run the CLI, adding ``--format json`` when *fmt* asks for it."""
    return runner.invoke(app, [*args, "--format", "json"] if fmt == "json" else args)


def _ingest_trace() -> None:
    """Store one trace. Extraction is off, so the graph starts empty."""
    result = runner.invoke(
        app, ["ingest", "trace", "-", "--format", "json"], input=json.dumps(_TRACE)
    )
    assert result.exit_code == 0, result.output


def _refresh_args(tmp_path: Path) -> list[str]:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_MANIFEST), encoding="utf-8")
    return ["extract", "refresh", "--type", "dbt-manifest", "--path", str(path)]


def _deny_writes(tmp_path: Path) -> None:
    """Deny entity and edge writes at Stage 2, with a condition Rich would eat."""
    policy = Policy(
        policy_id="pol-extract",
        policy_type=PolicyType.MUTATION,
        scope=PolicyScope(level="global"),
        rules=[
            PolicyRule(operation=op, condition="frozen [x]", action="deny")
            for op in ("entity.create", "link.create")
        ],
        enforcement=Enforcement.ENFORCE,
    )
    (tmp_path / "data" / "stores" / POLICY_FILENAME).write_text(
        json.dumps({"policies": [policy.model_dump(mode="json")]}), encoding="utf-8"
    )


def _fake_batch(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[tuple[CommandStatus, dict[str, str]]],
    message: str = _MESSAGE,
) -> MagicMock:
    """Answer each batch's commands with *outcomes* in order; the last repeats.

    Nothing is written. A refused or failed command carries *message*.
    """
    executor = MagicMock(spec=MutationExecutor)

    def _execute_batch(batch: CommandBatch) -> list[CommandResult]:
        results = []
        for i, cmd in enumerate(batch.commands):
            status, metadata = outcomes[min(i, len(outcomes) - 1)]
            refused = status in (CommandStatus.REJECTED, CommandStatus.FAILED)
            results.append(
                CommandResult(
                    command_id=f"cmd-{i}",
                    status=status,
                    operation=cmd.operation,
                    message=message if refused else "done",
                    metadata=dict(metadata),
                )
            )
        return results

    executor.execute_batch.side_effect = _execute_batch
    monkeypatch.setattr(extract_cli, "build_curate_executor", lambda _reg: executor)
    return executor


def _drafts(payload: dict) -> int:
    return payload["total_entities"] + payload["total_edges"]


def _text_drafts(out: str) -> int:
    """The draft total the text output's ``Drafts:`` line reports."""
    match = re.search(r"Drafts: +(\d+) entities, (\d+) edges", out)
    assert match, out
    return int(match.group(1)) + int(match.group(2))


def _counts(
    succeeded: int = 0, failed: int = 0, rejected: int = 0, duplicates: int = 0
) -> str:
    """The text output's ``Commands:`` counts."""
    return (
        f"{succeeded} succeeded, {failed} failed, {rejected} rejected, "
        f"{duplicates} duplicates"
    )


def _named(result: Result) -> str:
    """The failure a text run names: what follows ``first:`` on its failures line."""
    assert result.exit_code == 0, result.output
    match = re.search(r"; first: (.*)$", plain(result.output), re.MULTILINE)
    assert match, result.output
    return match.group(1)


class TestExtractTracesReportsTheBatch:
    @_FORMATS
    def test_a_denied_backfill_exits_3_and_claims_no_extraction(
        self, tmp_path: Path, fmt: str
    ) -> None:
        """The real executor with every write denied: nothing lands, and it says so."""
        _ingest_trace()
        _deny_writes(tmp_path)
        result = _invoke(["extract", "traces"], fmt)
        assert result.exit_code == EXIT_POLICY, result.output
        assert get_graph_store().count_nodes() == 0
        if fmt == "json":
            payload = json.loads(result.stdout)
            assert payload["status"] == "error"
            assert "frozen [x]" in payload["message"]
            assert _drafts(payload) > 0
            assert (payload["succeeded"], payload["rejected"]) == (0, _drafts(payload))
        else:
            out = plain(result.output)
            assert "Trace backfill failed (7 days)" in out
            assert "Extracted" not in out
            drafts = _text_drafts(out)
            assert drafts > 0
            assert _counts(rejected=drafts) in out
            assert "frozen [x]" in out

    @_WROTE_NOTHING
    @_FORMATS
    def test_a_backfill_that_wrote_nothing_exits_by_its_refusal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fmt: str,
        status: CommandStatus,
        metadata: dict[str, str],
        code: int,
    ) -> None:
        _ingest_trace()
        _fake_batch(monkeypatch, [(status, metadata)])
        result = _invoke(["extract", "traces"], fmt)
        assert result.exit_code == code, result.output
        key = "failed" if status == CommandStatus.FAILED else "rejected"
        if fmt == "json":
            payload = json.loads(result.stdout)
            assert (payload["status"], payload["message"]) == ("error", _MESSAGE)
            assert (payload["succeeded"], payload[key]) == (0, _drafts(payload))
        else:
            out = plain(result.output)
            assert "Extracted" not in out
            assert _counts(**{key: _text_drafts(out)}) in out
            assert _MESSAGE in out

    def test_the_first_refusal_sets_the_exit_and_a_sanitized_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JSON output is an artifact, so a store error's credentials stay out."""
        _ingest_trace()
        _fake_batch(
            monkeypatch,
            [(CommandStatus.FAILED, {}), (CommandStatus.REJECTED, _POLICY)],
            message="no route to postgres://ops:pw@db:5432/kb",
        )
        result = _invoke(["extract", "traces"], "json")
        assert result.exit_code == EXIT_STORE, result.output
        payload = json.loads(result.stdout)
        assert payload["message"] == SUPPRESSED_MARKER
        assert (payload["failed"], payload["rejected"]) == (1, _drafts(payload) - 1)

    @_FORMATS
    def test_a_clean_backfill_reports_backfilled_and_its_writes(self, fmt: str) -> None:
        _ingest_trace()
        result = _invoke(["extract", "traces"], fmt)
        assert result.exit_code == 0, result.output
        if fmt == "json":
            payload = json.loads(result.stdout)
            assert payload["status"] == "backfilled"
            assert payload["succeeded"] == _drafts(payload) > 0
            assert (payload["failed"], payload["rejected"], payload["duplicates"]) == (
                0,
                0,
                0,
            )
        else:
            out = plain(result.output)
            assert "Trace backfill (7 days)" in out
            drafts = _text_drafts(out)
            assert drafts > 0
            assert _counts(succeeded=drafts) in out
            assert "(data[ops]): " in out

    @_FORMATS
    def test_a_mixed_backfill_reports_both_counts_and_exits_0(
        self, monkeypatch: pytest.MonkeyPatch, fmt: str
    ) -> None:
        """One refusal among writes that landed is counted and named, not fatal."""
        _ingest_trace()
        _fake_batch(monkeypatch, _MIXED)
        result = _invoke(["extract", "traces"], fmt)
        assert result.exit_code == 0, result.output
        if fmt == "json":
            payload = json.loads(result.stdout)
            assert payload["status"] == "backfilled"
            assert _drafts(payload) >= 2
            assert (payload["succeeded"], payload["rejected"]) == (
                _drafts(payload) - 1,
                1,
            )
        else:
            out = plain(result.output)
            drafts = _text_drafts(out)
            assert drafts >= 2
            assert _counts(succeeded=drafts - 1, rejected=1) in out
            assert _MESSAGE in out

    def test_a_mixed_backfill_names_in_json_the_failure_its_text_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The payload carries the message top-level, as ``ingest``'s does."""
        _ingest_trace()
        _fake_batch(monkeypatch, _MIXED)
        named = _named(_invoke(["extract", "traces"], "text"))
        result = _invoke(["extract", "traces"], "json")
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "backfilled"
        assert payload["message"] == named == _MESSAGE

    @_FORMATS
    def test_duplicates_are_not_failures(
        self, monkeypatch: pytest.MonkeyPatch, fmt: str
    ) -> None:
        _ingest_trace()
        _fake_batch(monkeypatch, [(CommandStatus.DUPLICATE, {})])
        result = _invoke(["extract", "traces"], fmt)
        assert result.exit_code == 0, result.output
        if fmt == "json":
            payload = json.loads(result.stdout)
            assert payload["status"] == "backfilled"
            assert payload["duplicates"] == _drafts(payload) > 0
            assert (payload["failed"], payload["rejected"]) == (0, 0)
        else:
            out = plain(result.output)
            drafts = _text_drafts(out)
            assert _counts(duplicates=drafts) in out

    @_FORMATS
    def test_a_dry_run_executes_nothing_and_reports_as_before(
        self, monkeypatch: pytest.MonkeyPatch, fmt: str
    ) -> None:
        _ingest_trace()
        executor = _fake_batch(monkeypatch, [(CommandStatus.FAILED, {})])
        result = _invoke(["extract", "traces", "--dry-run"], fmt)
        assert result.exit_code == 0, result.output
        executor.execute_batch.assert_not_called()
        if fmt == "json":
            payload = json.loads(result.stdout)
            assert payload["status"] == "backfilled"
            assert set(payload) == {
                "status",
                "traces_scanned",
                "total_entities",
                "total_edges",
                "dry_run",
                "per_trace",
            }
        else:
            out = plain(result.output)
            assert re.search(r"Would extract: +[1-9]\d* entities", out), out
            assert "dry-run -- no mutations executed" in out
            assert "succeeded" not in out


class TestExtractRefreshReportsTheBatch:
    @_FORMATS
    def test_a_denied_refresh_exits_3(self, tmp_path: Path, fmt: str) -> None:
        """The real executor with every write denied: a refusal, not "unchanged"."""
        _deny_writes(tmp_path)
        result = _invoke(_refresh_args(tmp_path), fmt)
        assert result.exit_code == EXIT_POLICY, result.output
        if fmt == "json":
            payload = json.loads(result.stdout)
            assert payload["status"] == "error"
            assert "frozen [x]" in payload["message"]
            assert (payload["succeeded"], payload["rejected"]) == (0, 3)
            assert payload["new_entities"] == 0
        else:
            out = plain(result.output)
            assert "Refreshed" not in out
            assert _counts(rejected=3) in out
            assert "frozen [x]" in out

    @_WROTE_NOTHING
    @_FORMATS
    def test_a_refresh_that_wrote_nothing_exits_by_its_refusal(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fmt: str,
        status: CommandStatus,
        metadata: dict[str, str],
        code: int,
    ) -> None:
        _fake_batch(monkeypatch, [(status, metadata)])
        result = _invoke(_refresh_args(tmp_path), fmt)
        assert result.exit_code == code, result.output
        key = "failed" if status == CommandStatus.FAILED else "rejected"
        if fmt == "json":
            payload = json.loads(result.stdout)
            assert (payload["status"], payload["message"]) == ("error", _MESSAGE)
            assert (payload["succeeded"], payload[key]) == (0, 3)
        else:
            out = plain(result.output)
            assert _counts(**{key: 3}) in out
            assert _MESSAGE in out

    @_FORMATS
    def test_a_clean_refresh_reports_refreshed_and_its_writes(
        self, tmp_path: Path, fmt: str
    ) -> None:
        result = _invoke(_refresh_args(tmp_path), fmt)
        assert result.exit_code == 0, result.output
        if fmt == "json":
            payload = json.loads(result.stdout)
            assert (payload["status"], payload["new_entities"]) == ("refreshed", 3)
            assert (
                payload["succeeded"],
                payload["failed"],
                payload["rejected"],
                payload["duplicates"],
            ) == (3, 0, 0, 0)
        else:
            out = plain(result.output)
            assert "Refreshed" in out
            assert _counts(succeeded=3) in out

    @_FORMATS
    def test_a_mixed_refresh_reports_both_counts_and_exits_0(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str
    ) -> None:
        _fake_batch(monkeypatch, _MIXED)
        result = _invoke(_refresh_args(tmp_path), fmt)
        assert result.exit_code == 0, result.output
        if fmt == "json":
            payload = json.loads(result.stdout)
            assert payload["status"] == "refreshed"
            assert (payload["succeeded"], payload["rejected"]) == (2, 1)
        else:
            out = plain(result.output)
            assert _counts(succeeded=2, rejected=1) in out
            assert _MESSAGE in out

    def test_a_mixed_refresh_names_in_json_the_failure_its_text_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The payload carries the message top-level, as ``ingest``'s does."""
        _fake_batch(monkeypatch, _MIXED)
        named = _named(_invoke(_refresh_args(tmp_path), "text"))
        result = _invoke(_refresh_args(tmp_path), "json")
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "refreshed"
        assert payload["message"] == named == _MESSAGE

    @_FORMATS
    def test_duplicates_are_not_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str
    ) -> None:
        _fake_batch(monkeypatch, [(CommandStatus.DUPLICATE, {})])
        result = _invoke(_refresh_args(tmp_path), fmt)
        assert result.exit_code == 0, result.output
        if fmt == "json":
            payload = json.loads(result.stdout)
            assert payload["status"] == "refreshed"
            assert (payload["duplicates"], payload["failed"], payload["rejected"]) == (
                3,
                0,
                0,
            )
        else:
            assert _counts(duplicates=3) in plain(result.output)

    def test_diff_lines_print_types_keys_and_values_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rich would delete each ``[ops]`` and die on ``[/x]``."""
        run_refresh = extract_cli._run_refresh

        def _bracketed(*args: object, **kwargs: object) -> tuple[dict, list]:
            summary, results = run_refresh(*args, **kwargs)
            diff = {
                "added": {"a[ops]": 1},
                "removed": {"r[ops]": 1},
                "changed": {"c[ops]": ["v[ops]", "see [/x]"]},
            }
            summary["diffs"] = [
                {"entity_id": "model.p.m0", "entity_type": "t[ops]", "diff": diff}
            ]
            return summary, results

        monkeypatch.setattr(extract_cli, "_run_refresh", _bracketed)
        result = _invoke(_refresh_args(tmp_path), "text")
        assert result.exit_code == 0, result.output
        out = plain(result.output)
        assert "model.p.m0 (t[ops])" in out
        assert "+ a[ops]" in out
        assert "- r[ops]" in out
        assert "~ c[ops]: 'v[ops]' -> 'see [/x]'" in out
