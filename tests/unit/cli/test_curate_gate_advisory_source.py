"""``evaluate-precedent-gate`` Stage 1: the advisory source beside the trace source."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from tests.advisory_candidates import (
    ADVISORY_SENTINEL,
    anti_pattern,
    approach,
    retired,
    store_advisories,
)
from tests.cli_output import plain
from tests.precedent_candidates import recovering_trace
from trellis.learning.precedent_distill import DISTILLER_VERSION
from trellis.stores.advisory_source import resolve_advisory_path
from trellis.stores.registry import StoreRegistry
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

runner = CliRunner()

#: The trace source's Stage 1 keys before the advisory source existed.
TRACE_STAGE1_KEYS = frozenset(
    {
        "distiller_version",
        "traces_read",
        "trace_limit",
        "truncated",
        "candidates",
        "would_mint",
        "duplicates",
        "by_rule",
        "skipped",
        "dropped",
    }
)


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()
    reg = StoreRegistry(stores_dir=stores_dir)
    yield reg
    reg.close()


@pytest.fixture
def candidates(tmp_path: Path) -> Path:
    """Stage 2 needs a candidates file; an empty one leaves Stage 1 the subject."""
    path = tmp_path / "intent_learning_candidates.json"
    path.write_text(json.dumps({"candidates": []}), encoding="utf-8")
    return path


def _run(candidates: Path, *args: str) -> Result:
    return runner.invoke(
        app,
        ["curate", "evaluate-precedent-gate", "--candidates", str(candidates), *args],
    )


def _report(candidates: Path) -> dict:
    result = _run(candidates, "--format", "json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _events(stores: StoreRegistry) -> int:
    return stores.operational.event_log.count()


def test_the_trace_source_keys_and_values_are_unchanged_with_no_advisories(
    stores: StoreRegistry, candidates: Path
) -> None:
    stores.operational.trace_store.append(recovering_trace())

    stage1 = _report(candidates)["stage1"]
    trace_source = {k: v for k, v in stage1.items() if k != "advisory_source"}

    assert set(trace_source) == TRACE_STAGE1_KEYS
    assert trace_source["distiller_version"] == DISTILLER_VERSION
    assert trace_source["traces_read"] == 1
    assert trace_source["truncated"] is False
    assert trace_source["duplicates"] == 0
    assert trace_source["would_mint"] == trace_source["candidates"] > 0
    assert trace_source["skipped"] == trace_source["dropped"] == {}


def test_the_advisory_source_is_an_all_zero_block_with_no_advisories(
    stores: StoreRegistry, candidates: Path
) -> None:
    block = _report(candidates)["stage1"]["advisory_source"]

    assert block == {
        "distiller_version": "advisory-deterministic-1",
        "advisories_read": 0,
        "candidates": 0,
        "would_mint": 0,
        "duplicates": 0,
        "finding_recorded": 0,
        "by_rule": {},
        "skipped": {},
        "store_degraded": False,
    }


def test_adding_advisories_changes_only_the_advisory_source(
    stores: StoreRegistry, candidates: Path
) -> None:
    stores.operational.trace_store.append(recovering_trace())
    without = _report(candidates)

    store_advisories(
        stores.stores_dir,
        [anti_pattern("doc-bad"), approach("graph"), retired(anti_pattern("doc-old"))],
    )
    with_advisories = _report(candidates)

    # Every other key, at every depth, is what it was. The window opens at "now",
    # so its start moves between two runs and is not the subject.
    for report in (without, with_advisories):
        report["window"].pop("since")
    without["stage1"].pop("advisory_source")
    block = with_advisories["stage1"].pop("advisory_source")
    assert with_advisories == without
    assert block["advisories_read"] == 3
    assert block["would_mint"] == block["candidates"] == 1
    assert block["by_rule"] == {"advisory_anti_pattern": 1}
    assert block["skipped"] == {"category_not_lesson_shaped": 1, "retired": 1}


def test_the_advisory_source_reads_and_writes_nothing_to_the_event_log(
    stores: StoreRegistry, candidates: Path
) -> None:
    store_advisories(
        stores.stores_dir, [anti_pattern("doc-bad"), anti_pattern("doc-worse")]
    )
    before = _events(stores)

    _report(candidates)

    assert _events(stores) == before


def test_recorded_advisory_candidates_are_duplicates_not_would_mint(
    stores: StoreRegistry, candidates: Path
) -> None:
    store_advisories(
        stores.stores_dir, [anti_pattern("doc-bad"), anti_pattern("doc-worse")]
    )
    applied = runner.invoke(
        app, ["worker", "distill-advisory-precedents", "--apply", "--format", "json"]
    )
    assert json.loads(applied.stdout)["recorded"] == 2

    block = _report(candidates)["stage1"]["advisory_source"]

    assert block["would_mint"] == 0
    assert block["duplicates"] == 2


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_no_advisory_text_reaches_the_report(
    stores: StoreRegistry, candidates: Path, fmt: str
) -> None:
    store_advisories(
        stores.stores_dir,
        [
            anti_pattern(
                ADVISORY_SENTINEL,
                advisory_id="adv-s1",
                message=f"{ADVISORY_SENTINEL} correlates with failure",
            )
        ],
    )

    result = _run(candidates, "--format", fmt)

    assert result.exit_code == 0
    assert ADVISORY_SENTINEL not in result.output


def test_the_text_report_names_the_advisory_source(
    stores: StoreRegistry, candidates: Path
) -> None:
    store_advisories(
        stores.stores_dir, [anti_pattern("doc-bad"), anti_pattern("doc-worse")]
    )

    text = plain(_run(candidates).output)

    assert "stage 1 (mint)" in text
    assert "stage 1 (mint, advisory source): read 2 advisory(ies), would mint 2" in text
    assert "rule advisory_anti_pattern: 2" in text


def test_the_text_report_says_the_advisory_source_ignores_the_window(
    stores: StoreRegistry, candidates: Path
) -> None:
    store_advisories(stores.stores_dir, [anti_pattern("doc-bad")])

    text = plain(_run(candidates, "--days", "1").output)

    assert "every stored advisory is read: --days does not bound this source" in text
    # Said once, beside the advisory block, and not for the trace source.
    assert text.count("--days does not bound") == 1


def test_a_degraded_advisory_file_is_warned_about_in_the_text_report(
    stores: StoreRegistry, candidates: Path
) -> None:
    resolve_advisory_path(stores.stores_dir).write_text("{not json", encoding="utf-8")

    as_json = _report(candidates)["stage1"]["advisory_source"]
    text = plain(_run(candidates).output)

    assert as_json["store_degraded"] is True
    assert "the advisory file loaded only in part" in text


def test_a_healthy_advisory_file_prints_no_degradation_warning(
    stores: StoreRegistry, candidates: Path
) -> None:
    store_advisories(stores.stores_dir, [anti_pattern("doc-bad")])

    text = plain(_run(candidates).output)

    assert "loaded only in part" not in text


def test_an_approach_advisory_alone_mints_nothing(
    stores: StoreRegistry, candidates: Path
) -> None:
    store_advisories(stores.stores_dir, [approach("graph")])

    block = _report(candidates)["stage1"]["advisory_source"]

    assert block["advisories_read"] == 1
    assert block["would_mint"] == block["candidates"] == 0
    assert block["by_rule"] == {}
    assert block["skipped"] == {"category_not_lesson_shaped": 1}
