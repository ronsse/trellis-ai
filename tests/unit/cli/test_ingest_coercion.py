"""``trellis ingest trace`` repairs the two commonest trace-shape mistakes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.stores.base.event_log import EventType
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry, get_event_log, get_trace_store

runner = CliRunner()

_NEEDLE = "needle-do-not-log"
_REPO = "secret-org/secret-repo"


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()


def _write(tmp_path: Path, **overrides: object) -> Path:
    data: dict[str, object] = {
        "source": "agent",
        "intent": "probe",
        "steps": [],
        "context": {"agent_id": "a"},
    }
    data.update(overrides)
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(data))
    return path


def _repaired(tmp_path: Path) -> Path:
    return _write(
        tmp_path,
        context={"agent_id": "a", "repo": _REPO},
        steps=[{"step_type": "t", "name": "a", "result": _NEEDLE}],
    )


def _coerced() -> list:
    return get_event_log().get_events(event_type=EventType.WRITE_COERCED, limit=100)


class TestCliRepairs:
    def test_both_mistakes_are_accepted_and_stored_canonically(
        self, tmp_path: Path
    ) -> None:
        result = runner.invoke(
            app, ["ingest", "trace", str(_repaired(tmp_path)), "--format", "json"]
        )

        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout.strip())
        stored = get_trace_store().get(data["trace_id"])
        assert stored is not None
        assert stored.steps[0].result == {"summary": _NEEDLE}
        assert stored.metadata["repo"] == _REPO
        assert "repo" not in stored.context.model_dump()

    def test_json_output_lists_the_repairs_without_content(
        self, tmp_path: Path
    ) -> None:
        result = runner.invoke(
            app, ["ingest", "trace", str(_repaired(tmp_path)), "--format", "json"]
        )

        data = json.loads(result.stdout.strip())
        assert data["status"] == "ingested"
        assert data["repairs"] == [
            {"path": "steps[0].result", "rule": "str_to_summary"},
            {"path": "context.repo", "rule": "moved_to_metadata"},
        ]
        assert _NEEDLE not in result.stdout
        assert _REPO not in result.stdout

    def test_text_output_says_what_was_repaired_and_how_to_send_it_next_time(
        self, tmp_path: Path
    ) -> None:
        result = runner.invoke(app, ["ingest", "trace", str(_repaired(tmp_path))])

        out = plain(result.stdout)
        assert result.exit_code == 0
        assert "repaired 2 fields" in out
        assert "top-level metadata" in out
        assert _NEEDLE not in out

    def test_a_clean_trace_prints_no_repairs_key_and_emits_nothing(
        self, tmp_path: Path
    ) -> None:
        result = runner.invoke(
            app, ["ingest", "trace", str(_write(tmp_path)), "--format", "json"]
        )

        assert result.exit_code == 0
        assert "repairs" not in json.loads(result.stdout.strip())
        assert _coerced() == []

    def test_one_event_names_the_surface_and_carries_no_content(
        self, tmp_path: Path
    ) -> None:
        result = runner.invoke(
            app, ["ingest", "trace", str(_repaired(tmp_path)), "--format", "json"]
        )

        trace_id = json.loads(result.stdout.strip())["trace_id"]
        (event,) = _coerced()
        assert event.source == "cli:ingest-trace"
        assert event.payload["tool"] == "cli:ingest-trace"
        assert event.payload["trace_id"] == trace_id
        assert [r["rule"] for r in event.payload["repairs"]] == [
            "str_to_summary",
            "moved_to_metadata",
        ]
        raw = json.dumps(event.payload)
        assert _NEEDLE not in raw
        assert _REPO not in raw

    @pytest.mark.parametrize("fmt", ["json", "text"])
    @pytest.mark.parametrize(
        "overrides",
        [
            {"steps": [{"step_type": "t", "name": "a", "result": 7}]},
            {"context": {"repo": "a/b"}, "metadata": {"repo": "c/d"}},
            {"context": {"repo": "a/b", "colour": "blue"}},
        ],
        ids=["non-string-result", "conflicting-metadata-repo", "mixed-unrepairable"],
    )
    def test_what_is_not_repairable_still_exits_two_in_either_format(
        self, tmp_path: Path, overrides: dict[str, object], fmt: str
    ) -> None:
        result = runner.invoke(
            app,
            ["ingest", "trace", str(_write(tmp_path, **overrides)), "--format", fmt],
        )

        assert result.exit_code == 2
        assert _coerced() == []

    def test_health_reports_the_coercion_beside_rejections(
        self, tmp_path: Path
    ) -> None:
        runner.invoke(app, ["ingest", "trace", str(_repaired(tmp_path))])

        result = runner.invoke(app, ["analyze", "health", "--format", "json"])

        assert result.exit_code == 0, result.stdout
        text = result.stdout
        assert "coerced_by_rule" in text
        data = json.loads(text)
        writes = data["write"] if "write" in data else data["writes"]
        assert writes["coerced"] == 1
        assert writes["coerced_by_rule"] == {
            "str_to_summary": 1,
            "moved_to_metadata": 1,
        }

    def test_health_text_names_the_coercion(self, tmp_path: Path) -> None:
        runner.invoke(app, ["ingest", "trace", str(_repaired(tmp_path))])

        result = runner.invoke(app, ["analyze", "health"])

        out = plain(result.stdout)
        assert result.exit_code == 0
        assert "repaired at the boundary" in out
        assert "str_to_summary=1" in out
