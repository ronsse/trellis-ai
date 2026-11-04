"""``trellis worker distill-advisory-precedents``: dry-run first, counts only."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from tests.advisory_candidates import (
    ADVISORY_SENTINEL,
    anti_pattern,
    approach,
    make_advisory,
    retired,
    store_advisories,
)
from tests.cli_output import plain
from trellis.mutate.policy_source import resolve_policy_path
from trellis.schemas.advisory import AdvisoryCategory
from trellis.schemas.enums import PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.advisory_source import resolve_advisory_path
from trellis.stores.base.event_log import EventType
from trellis.stores.policy_store import PolicyStore
from trellis.stores.registry import StoreRegistry
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

runner = CliRunner()


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


def _seed(stores: StoreRegistry) -> Path:
    """Two ANTI_PATTERN advisories, one retired, two of other categories."""
    return store_advisories(
        stores.stores_dir,
        [
            anti_pattern(
                "doc-bad",
                message=f"{ADVISORY_SENTINEL} correlates with failure",
                scope="data",
            ),
            anti_pattern("doc-worse"),
            approach("graph"),
            retired(anti_pattern("doc-old")),
            make_advisory(AdvisoryCategory.QUERY, advisory_id="adv-query"),
        ],
    )


def _run(*args: str) -> Result:
    return runner.invoke(app, ["worker", "distill-advisory-precedents", *args])


def _total_events(stores: StoreRegistry) -> int:
    return stores.operational.event_log.count()


def _candidates(stores: StoreRegistry) -> int:
    return stores.operational.event_log.count(
        event_type=EventType.PRECEDENT_CANDIDATE_MINED
    )


def test_a_dry_run_reports_counts_and_writes_nothing(stores: StoreRegistry) -> None:
    path = _seed(stores)
    advisories_before = path.read_bytes()
    before = _total_events(stores)

    result = _run("--format", "json")

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["mode"] == "dry-run"
    assert payload["status"] == "ok"
    assert payload["advisories_read"] == 5
    assert payload["candidates"] == payload["would_record"] == 2
    assert payload["by_rule"] == {"advisory_anti_pattern": 2}
    assert payload["skipped"] == {"category_not_lesson_shaped": 2, "retired": 1}
    assert payload["duplicates"] == payload["finding_recorded"] == 0
    assert payload["store_degraded"] is False
    assert len(payload["candidate_ids"]) == 2
    assert _total_events(stores) == before
    assert path.read_bytes() == advisories_before


def test_apply_records_each_candidate_once_and_a_rerun_records_none(
    stores: StoreRegistry,
) -> None:
    path = _seed(stores)
    advisories_before = path.read_bytes()

    first = json.loads(_run("--apply", "--format", "json").stdout)
    second = json.loads(_run("--apply", "--format", "json").stdout)
    dry = json.loads(_run("--format", "json").stdout)

    assert first["mode"] == "apply"
    assert first["recorded"] == 2
    assert _candidates(stores) == 2
    assert second["recorded"] == 0
    assert second["duplicates"] == 2
    assert dry["would_record"] == 0
    assert dry["duplicates"] == 2
    assert _candidates(stores) == 2
    # Reading advisories changes no advisory.
    assert path.read_bytes() == advisories_before


def test_a_recorded_candidate_cites_its_advisory_and_packs(
    stores: StoreRegistry,
) -> None:
    _seed(stores)
    _run("--apply")

    records = [
        e.payload["record"]
        for e in stores.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_CANDIDATE_MINED, limit=10
        )
    ]

    assert {r["rule"] for r in records} == {"advisory_anti_pattern"}
    anti = next(
        r for r in records if r["evidence"]["advisory_ids"] == ["adv-anti-doc-bad"]
    )
    assert anti["provenance_tier"] == "deterministic"
    assert anti["action"]["kind"] == "avoid"
    assert anti["evidence"]["pack_ids"] == ["pack-1", "pack-2"]


def test_the_same_finding_with_moved_counts_is_not_recorded_again(
    stores: StoreRegistry,
) -> None:
    store_advisories(stores.stores_dir, [anti_pattern("doc-bad", sample_size=8)])
    assert json.loads(_run("--apply", "--format", "json").stdout)["recorded"] == 1

    # The generator keys an advisory on the finding, so tomorrow it is the same
    # advisory with new numbers, and a record that hashes them has a new id.
    store_advisories(
        stores.stores_dir,
        [anti_pattern("doc-bad", sample_size=11, rate_with=0.2, rate_without=0.7)],
    )
    again = json.loads(_run("--apply", "--format", "json").stdout)

    assert again["recorded"] == 0
    assert again["finding_recorded"] == 1
    assert again["duplicates"] == 0
    assert again["candidate_ids"] == []
    assert _candidates(stores) == 1


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_the_output_carries_no_advisory_text(stores: StoreRegistry, fmt: str) -> None:
    store_advisories(
        stores.stores_dir,
        [
            anti_pattern(
                ADVISORY_SENTINEL,
                advisory_id="adv-s1",
                message=f"{ADVISORY_SENTINEL} correlates with failure",
                scope=ADVISORY_SENTINEL,
                packs=(f"pack-{ADVISORY_SENTINEL}",),
            ),
            approach(ADVISORY_SENTINEL, advisory_id="adv-s2"),
        ],
    )

    for args in ([], ["--apply"]):
        result = _run(*args, "--format", fmt)
        assert result.exit_code == 0
        assert ADVISORY_SENTINEL not in result.stdout
        assert "correlates with failure" not in result.stdout
    assert _candidates(stores) == 1


def test_the_exit_code_does_not_depend_on_the_format(stores: StoreRegistry) -> None:
    _seed(stores)

    ok = [_run("--format", f).exit_code for f in ("json", "text")]
    bad = [_run("--bogus", "--format", f).exit_code for f in ("json", "text")]

    assert ok == [0, 0]
    assert bad[0] == bad[1] != 0


def test_a_policy_refusal_exits_the_same_in_both_formats(
    stores: StoreRegistry, tmp_path: Path
) -> None:
    _seed(stores)
    deny = Policy(
        policy_type=PolicyType.MUTATION,
        scope=PolicyScope(level="global", value=None),
        rules=[PolicyRule(operation="precedent.candidate", action="deny")],
    )
    PolicyStore(resolve_policy_path(tmp_path / "data" / "stores")).add(deny)

    results = [_run("--apply", "--format", f) for f in ("json", "text")]

    assert results[0].exit_code == results[1].exit_code != 0
    payload = json.loads(results[0].stdout)
    assert payload["status"] == "error"
    assert payload["rejected"] == 2
    assert payload["recorded"] == 0
    assert _candidates(stores) == 0


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_no_advisory_file_reports_zeros_and_exits_clean(
    stores: StoreRegistry, fmt: str
) -> None:
    result = _run("--apply", "--format", fmt)

    assert result.exit_code == 0
    if fmt == "json":
        payload = json.loads(result.stdout)
        assert payload["advisories_read"] == payload["candidates"] == 0
        assert payload["recorded"] == 0
    assert _candidates(stores) == 0


def test_a_corrupt_advisory_file_is_reported_degraded_not_fatal(
    stores: StoreRegistry,
) -> None:
    resolve_advisory_path(stores.stores_dir).write_text("{not json", encoding="utf-8")

    result = _run("--apply", "--format", "json")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["store_degraded"] is True
    assert payload["recorded"] == 0
    assert _candidates(stores) == 0


def test_the_text_report_names_the_mode_rules_and_skips(stores: StoreRegistry) -> None:
    _seed(stores)

    text = plain(_run().stdout)

    assert "(dry-run)" in text
    assert "read 5 advisory(ies)" in text
    assert "rule advisory_anti_pattern: 2" in text
    assert "skipped category_not_lesson_shaped: 2" in text
    assert "skipped retired: 1" in text
    assert "nothing was written" in text
