"""Tests for ``trellis analyze near-duplicates``.

The command is a measurement: it reads the document and vector stores and, with
``--no-meta-trace``, writes nothing at all. Pinned here: the JSON shape and
counts, that options reach the analyzer, that the text report never prints
document text and escapes ids for Rich, that the exit code does not depend on
``--format``, and that a run leaves every store and the data directory as it
found them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from tests.unit.analyze import _near_duplicates_seed as seed
from trellis.stores.registry import StoreRegistry
from trellis_cli.exit_codes import EXIT_OK
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = CliRunner()

FORMATS = ("text", "json")
ODD_ID = "kb:[bold]odd[/bold]:smile:"


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
    seed.put(_temp_stores, ODD_ID, seed.PARA_A, seed.unit(0))
    seed.put(_temp_stores, "kb:para-b", seed.PARA_B, seed.unit(0, 0.97))
    seed.put(_temp_stores, "mem:lex-a", seed.LEX_A, seed.unit(2))
    seed.put(_temp_stores, "mem:lex-b", seed.LEX_B, seed.unit(2, 0.97))
    seed.put(_temp_stores, "arch", seed.OTHER, seed.unit(0), seed.ARCHIVED)
    return _temp_stores


def _snapshot(registry: StoreRegistry, root: Path) -> dict[str, Any]:
    return {
        "documents": registry.knowledge.document_store.count(),
        "vectors": registry.knowledge.vector_store.count(),
        "nodes": registry.knowledge.graph_store.count_nodes(),
        "traces": registry.operational.trace_store.count(),
        "events": registry.operational.event_log.count(),
        "files": sorted(str(p.relative_to(root)) for p in root.rglob("*")),
    }


def _invoke(*extra: str) -> Any:
    return runner.invoke(app, ["analyze", "near-duplicates", *extra])


class TestJson:
    def test_report_shape_and_counts(self, seeded: StoreRegistry) -> None:
        result = _invoke("--format", "json", "--no-meta-trace")

        assert result.exit_code == EXIT_OK
        data = json.loads(result.output)
        assert set(data) >= {
            "status",
            "basis",
            "documents_considered",
            "skipped",
            "no_embedding",
            "embedded",
            "cap_hit",
            "thresholds",
            "cannot_tell",
            "notes",
        }
        assert data["documents_considered"] == 5
        assert data["skipped"]["archived"] == 1
        assert data["embedded"] == 4
        high = data["thresholds"][1]
        assert high["threshold"] == 0.95
        assert (high["lexical_pairs"], high["residue_pairs"]) == (1, 1)
        assert high["residue_sample"][0]["doc_id"] in {ODD_ID, "kb:para-b"}

    def test_options_reach_the_analyzer(self, seeded: StoreRegistry) -> None:
        data = json.loads(
            _invoke(
                "--format", "json", "--no-meta-trace", "--threshold", "0.99",
                "--max-docs", "2", "--neighbors", "3",
            ).output
        )  # fmt: skip

        assert [row["threshold"] for row in data["thresholds"]] == [0.99]
        assert data["max_docs"] == 2
        assert data["cap_hit"] is True
        assert data["basis"]["neighbors"] == 3


class TestText:
    def test_the_report_names_both_kinds_and_its_limits(
        self, seeded: StoreRegistry
    ) -> None:
        out = plain(_invoke("--no-meta-trace").output)

        assert "Measurement only" in out
        assert "1 lexical (MinHash already catches), 1 paraphrase residue" in out
        assert "1 archived" in out
        assert "uncalibrated" in out

    def test_ids_are_escaped_and_no_text_is_printed(
        self, seeded: StoreRegistry
    ) -> None:
        out = plain(_invoke("--no-meta-trace").output)

        assert ODD_ID in out
        for forbidden in ("signing key", "Key rotation", "nightly export"):
            assert forbidden not in out, forbidden

    def test_an_empty_store_says_so(self) -> None:
        out = plain(_invoke("--no-meta-trace").output)

        assert "empty:" in out


class TestExitParity:
    @pytest.mark.usefixtures("seeded")
    def test_a_populated_store_exits_alike_on_every_format(self) -> None:
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
    def test_a_run_changes_no_store_and_no_file(
        self, seeded: StoreRegistry, tmp_path: Path
    ) -> None:
        root = tmp_path / "data"
        before = _snapshot(seeded, root)

        for fmt in FORMATS:
            assert _invoke("--format", fmt, "--no-meta-trace").exit_code == EXIT_OK

        assert _snapshot(seeded, root) == before


class TestSaturationText:
    @pytest.mark.usefixtures("seeded")
    def test_probe_counters_are_printed(self) -> None:
        out = plain(_invoke("--no-meta-trace", "--neighbors", "1").output)

        assert "probes:" in out
        assert "saturated" in out
        assert "outside the population" in out
