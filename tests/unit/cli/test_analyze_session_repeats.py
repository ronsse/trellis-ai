"""Tests for ``trellis analyze session-repeats``.

The command is a measurement: it reads ``PACK_ASSEMBLED`` and the document store
and, with ``--no-meta-trace``, writes nothing at all. Pinned here: the JSON
shape, that the text report never prints a session id, an item id or document
text, that the exit code does not depend on ``--format``, and that a run leaves
every store and the data directory exactly as it found them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from tests.unit.analyze import _session_repeats_seed as seed
from trellis.analyze.session_repeats import session_hash
from trellis.stores.registry import StoreRegistry
from trellis_cli.exit_codes import EXIT_OK
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = CliRunner()

SESSION = "sess:smile: [red]raw[/red]"
FORMATS = ("text", "json")
ITEM_IDS = ("doc-a", "doc-a-copy", "doc-x", "[bold]odd[/bold]-id")


def _recent(minutes: int) -> datetime:
    """A moment inside the default 30-day window, ``minutes`` after a fixed base."""
    return datetime.now(UTC) - timedelta(days=1) + timedelta(minutes=minutes)


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    (tmp_path / "config").mkdir()
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()
    return StoreRegistry(stores_dir=stores_dir)


@pytest.fixture
def seeded(_temp_stores: StoreRegistry) -> StoreRegistry:
    seed.put_doc(_temp_stores, "doc-a", seed.BASE)
    seed.put_doc(_temp_stores, "doc-a-copy", seed.NEAR)
    seed.put_doc(_temp_stores, "doc-x", seed.FRESH)
    seed.put_doc(_temp_stores, "[bold]odd[/bold]-id", seed.THIRD)
    log = _temp_stores.operational.event_log
    seed.pack(log, "p1", SESSION, _recent(0), [("doc-a", 10), ("doc-x", 8)])
    seed.pack(
        log,
        "p2",
        SESSION,
        _recent(10),
        [("doc-a-copy", 12), ("[bold]odd[/bold]-id", 6)],
    )
    return _temp_stores


def _snapshot(registry: StoreRegistry, root: Path) -> dict[str, Any]:
    """Row counts across the stores plus the set of files under the data root."""
    return {
        "documents": registry.knowledge.document_store.count(),
        "vectors": registry.knowledge.vector_store.count(),
        "nodes": registry.knowledge.graph_store.count_nodes(),
        "edges": registry.knowledge.graph_store.count_edges(),
        "traces": registry.operational.trace_store.count(),
        "events": registry.operational.event_log.count(),
        "files": sorted(str(p.relative_to(root)) for p in root.rglob("*")),
    }


def _invoke(*extra: str) -> Any:
    return runner.invoke(app, ["analyze", "session-repeats", *extra])


class TestJson:
    def test_report_shape_and_counts(self, seeded: StoreRegistry) -> None:
        result = _invoke("--format", "json", "--no-meta-trace")

        assert result.exit_code == EXIT_OK
        data = json.loads(result.output)
        assert set(data) >= {
            "status",
            "window_days",
            "window_minutes",
            "basis",
            "sessions_multi_pack",
            "later_pack_items",
            "exact_repeat",
            "near_duplicate",
            "chunk_sibling",
            "fresh",
            "unreadable",
            "near_duplicate_share",
            "top_sessions",
            "scan",
            "notes",
        }
        assert data["status"] == "ok"
        assert (data["window_days"], data["window_minutes"]) == (30, 60)
        assert data["sessions_multi_pack"] == 1
        assert data["later_pack_items"] == 2
        assert data["near_duplicate"] == {"items": 1, "estimated_tokens": 12}
        assert data["fresh"] == {"items": 1, "estimated_tokens": 6}
        assert data["near_duplicate_share"] == 0.5
        assert data["basis"]["threshold"] == 0.85

    def test_json_may_carry_item_ids_but_the_session_stays_hashed(
        self, seeded: StoreRegistry
    ) -> None:
        data = json.loads(_invoke("--format", "json", "--no-meta-trace").output)

        [row] = data["top_sessions"]
        assert row["session"] == session_hash(SESSION)
        assert row["pairs"][0]["item_id"] == "doc-a-copy"
        assert row["pairs"][0]["sibling_item_id"] == "doc-a"
        assert SESSION not in json.dumps(data)

    def test_options_reach_the_analyzer(self, seeded: StoreRegistry) -> None:
        data = json.loads(
            _invoke(
                "--format", "json", "--no-meta-trace", "--days", "7",
                "--window-minutes", "5",
            ).output
        )  # fmt: skip

        assert (data["window_days"], data["window_minutes"]) == (7, 5)
        # The two packs are ten minutes apart, so a five-minute window skips them.
        assert data["later_pack_items"] == 0
        assert data["outside_window_items"] == 2


class TestText:
    def test_the_report_names_counts_and_the_predicate(
        self, seeded: StoreRegistry
    ) -> None:
        out = plain(_invoke("--no-meta-trace").output)

        assert "near-duplicate sibling: 1 items, 12 estimated tokens" in out
        assert "exact repeat: 0 items" in out
        assert "near-duplicate share of later-pack items: 50.0%" in out
        assert "MinHash threshold 0.85" in out
        assert "Measurement only" in out

    def test_no_session_id_item_id_or_text_is_printed(
        self, seeded: StoreRegistry
    ) -> None:
        out = plain(_invoke("--no-meta-trace").output)

        assert session_hash(SESSION) in out
        for forbidden in (
            SESSION,
            "sess:",
            "[red]",
            "raw",
            *ITEM_IDS,
            "failover runbook",
            "Quarterly budget",
            "Capacity planning",
            "\N{GRINNING FACE WITH SMILING EYES}",
        ):
            assert forbidden not in out, forbidden

    def test_an_empty_store_says_so(self) -> None:
        out = plain(_invoke("--no-meta-trace").output)

        assert "empty:" in out
        assert "0 sessions with 2 or more packs" in out
        assert "near-duplicate share of later-pack items: -" in out


class TestExitParity:
    def test_a_populated_store_exits_alike_on_every_format(
        self, seeded: StoreRegistry
    ) -> None:
        codes = {
            fmt: _invoke("--format", fmt, "--no-meta-trace").exit_code
            for fmt in FORMATS
        }

        assert set(codes.values()) == {EXIT_OK}

    def test_an_empty_store_exits_alike_on_every_format(self) -> None:
        codes = {
            fmt: _invoke("--format", fmt, "--no-meta-trace").exit_code
            for fmt in FORMATS
        }

        assert set(codes.values()) == {EXIT_OK}


class TestReadOnly:
    def test_no_meta_trace_leaves_every_store_and_file_unchanged(
        self, seeded: StoreRegistry, tmp_path: Path
    ) -> None:
        root = tmp_path / "data"
        before = _snapshot(seeded, root)

        for fmt in FORMATS:
            assert _invoke("--format", fmt, "--no-meta-trace").exit_code == EXIT_OK
        after = _snapshot(seeded, root)

        assert after == before
        assert before["events"] == 2  # the two seeded packs, nothing else

    def test_the_default_run_is_what_the_flag_switches_off(
        self, seeded: StoreRegistry, tmp_path: Path
    ) -> None:
        """Without the flag the meta-Activity is written, so the flag test bites."""
        before = _snapshot(seeded, tmp_path / "data")

        assert _invoke("--format", "json").exit_code == EXIT_OK
        after = _snapshot(seeded, tmp_path / "data")

        assert (after["nodes"], after["events"]) != (before["nodes"], before["events"])
