"""The pack holdout on the CLI: ``retrieve pack``, ``analyze pack-quality``
and ``admin write-config``.

``trellis retrieve pack`` and ``trellis analyze pack-quality`` build through
:func:`~trellis.retrieve.builder_factory.build_pack_builder` like every
other pack surface, so the process's own rate applies to them: at rate 1 the
operator's preview is an empty pack in the normal JSON shape, and its
``PACK_ASSEMBLED`` row keeps the would-be items apart. ``trellis admin
write-config`` reports the rate in force as it reports every
write-behaviour knob.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from trellis.core.write_config import ENV_VAR_BY_FIELD
from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryEvidence
from trellis.schemas.pack import Pack
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import EventType
from trellis_cli.main import app
from trellis_cli.stores import _get_registry, get_document_store, get_event_log

runner = CliRunner()

RATE_ENV = "TRELLIS_PACK_HOLDOUT_RATE"
INTENT = "failover runbook"

_SUBJECTS = (
    "replication lag",
    "connection pooling",
    "vacuum scheduling",
    "wal shipping",
)


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    for name in ENV_VAR_BY_FIELD.values():
        monkeypatch.delenv(name, raising=False)


def _seed() -> None:
    docs = get_document_store()
    for i, subject in enumerate(_SUBJECTS):
        docs.put(
            f"doc-{i}",
            (
                f"failover runbook for {subject}: drain the write queue, "
                f"promote the replica, then restart the {subject} sidecar. "
            )
            * 6,
            {"title": f"Runbook {i}"},
        )
    advisories = AdvisoryStore(_get_registry().stores_dir / "advisories.json")
    for advisory_id, confidence, category in [
        ("adv-entity", 0.82, AdvisoryCategory.ENTITY),
        ("adv-approach", 0.61, AdvisoryCategory.APPROACH),
    ]:
        advisories.put(
            Advisory(
                advisory_id=advisory_id,
                category=category,
                confidence=confidence,
                message=f"Synthetic advisory {advisory_id}",
                evidence=AdvisoryEvidence(
                    sample_size=9,
                    success_rate_with=0.7,
                    success_rate_without=0.4,
                    effect_size=0.3,
                    evidence_confidence=1.0,
                ),
                scope="global",
            )
        )


def _pack() -> dict[str, Any]:
    result = runner.invoke(
        app, ["retrieve", "pack", "--intent", INTENT, "--format", "json", "--quiet"]
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout.strip())


def _assembled(pack_id: str) -> dict[str, Any]:
    events = [
        event
        for event in get_event_log().get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=100
        )
        if event.entity_id == pack_id
    ]
    assert len(events) == 1, events
    return events[0].payload


def _without_pack_id(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key != "pack_id"}


def _mask_duration(node: Any) -> Any:
    """``node`` with every ``duration_ms`` masked.

    It is real wall-clock elapsed time for that call's build, so two
    separately-invoked CLI builds (a withheld preview vs. a greenfield one,
    run as two different process-in-process ``trellis retrieve pack``
    invocations) are not expected to report the same value.
    """
    if isinstance(node, dict):
        return {
            key: "<elapsed>" if key == "duration_ms" else _mask_duration(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_mask_duration(value) for value in node]
    return node


class TestRetrievePack:
    def test_a_withheld_preview_is_an_empty_pack(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(RATE_ENV, "1e-12")
        greenfield = _pack()
        _seed()
        monkeypatch.setenv(RATE_ENV, "0")
        control = _pack()
        monkeypatch.setenv(RATE_ENV, "1")
        withheld = _pack()

        assert sorted(item["item_id"] for item in control["items"]) == [
            "doc-0",
            "doc-1",
            "doc-2",
            "doc-3",
        ]
        assert control["advisories"]
        assert _mask_duration(_without_pack_id(withheld)) == _mask_duration(
            _without_pack_id(greenfield)
        )
        assert withheld["count"] == 0
        assert len(withheld["pack_id"]) == 26

        held = _assembled(withheld["pack_id"])
        assert (held["holdout"], held["holdout_rate"]) == (True, 1.0)
        assert held["injected_items"] == []
        assert sorted(row["item_id"] for row in held["holdout_items"]) == sorted(
            item["item_id"] for item in control["items"]
        )
        served = _assembled(control["pack_id"])
        assert (served["holdout"], served["holdout_rate"]) == (False, 0.0)
        assert "holdout_items" not in served

    def test_a_settings_store_override_withholds_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``pack_holdout_rate`` is the one knob a *settings* row, with no
        env var set at all, actually changes at its real enforcement site
        (see ``SettingSpec(name="pack_holdout_rate").settings_live`` in
        ``trellis.core.settings_registry``) — proven end to end here, not
        just at the ``from_env_and_settings`` unit level.
        """
        from trellis.stores.settings_store import SettingsStore

        _seed()
        monkeypatch.delenv(RATE_ENV, raising=False)
        SettingsStore(_get_registry().stores_dir / "settings.json").set(
            "pack_holdout_rate", 1.0
        )
        withheld = _pack()
        assert withheld["count"] == 0
        held = _assembled(withheld["pack_id"])
        assert (held["holdout"], held["holdout_rate"]) == (True, 1.0)

    def test_env_still_wins_over_a_settings_override_here(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The env > settings precedence holds at this call site too, not
        just in ``WriteBehaviourConfig.from_env_and_settings``'s own
        tests: a settings row saying "withhold everything" must not
        override an explicit env var saying "withhold nothing".
        """
        from trellis.stores.settings_store import SettingsStore

        _seed()
        SettingsStore(_get_registry().stores_dir / "settings.json").set(
            "pack_holdout_rate", 1.0
        )
        monkeypatch.setenv(RATE_ENV, "0")
        served = _pack()
        assert served["count"] > 0
        row = _assembled(served["pack_id"])
        assert (row["holdout"], row["holdout_rate"]) == (False, 0.0)


class TestPackQuality:
    """``analyze pack-quality`` assembles through the same seam.

    Its packs land in ``PACK_ASSEMBLED`` like any other surface's, so the
    rate in force applies to them too: no surface's rows read as served
    while the draw was running.
    """

    def test_a_scenario_pack_is_drawn_like_any_other(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from trellis.retrieve.evaluate import EvaluationScenario
        from trellis_cli.analyze import _assemble_pack_for_scenario

        _seed()
        scenario = EvaluationScenario(name="holdout-scenario", intent=INTENT)
        monkeypatch.setenv(RATE_ENV, "0")
        control = _assemble_pack_for_scenario(scenario)
        monkeypatch.setenv(RATE_ENV, "1")
        withheld = _assemble_pack_for_scenario(scenario)
        assert isinstance(control, Pack)
        assert isinstance(withheld, Pack)

        assert sorted(item.item_id for item in control.items) == [
            "doc-0",
            "doc-1",
            "doc-2",
            "doc-3",
        ]
        assert control.advisories
        assert (withheld.items, withheld.advisories) == ([], [])

        held = _assembled(withheld.pack_id)
        assert (held["holdout"], held["holdout_rate"]) == (True, 1.0)
        assert held["injected_items"] == []
        assert sorted(row["item_id"] for row in held["holdout_items"]) == sorted(
            item.item_id for item in control.items
        )
        assert sorted(held["holdout_advisory_ids"]) == sorted(
            advisory.advisory_id for advisory in control.advisories
        )


class TestWriteConfig:
    @staticmethod
    def _report() -> dict[str, Any]:
        result = runner.invoke(app, ["admin", "write-config", "--format", "json"])
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    def test_the_rate_in_force_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(RATE_ENV, "0.25")
        report = self._report()
        (row,) = [r for r in report["knobs"] if r["name"] == "pack_holdout_rate"]
        assert row == {
            "name": "pack_holdout_rate",
            "env_var": RATE_ENV,
            "value": 0.25,
            "default": 0.0,
            "overridden": True,
            "overridden_by": "env",
        }
        assert report["write_provenance"]["env_flags"]["pack_holdout_rate"] == 0.25

    @pytest.mark.parametrize("raw", ["2", "-0.1", "lots"])
    def test_a_refused_value_reports_the_default(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(RATE_ENV, raw)
        report = self._report()
        (row,) = [r for r in report["knobs"] if r["name"] == "pack_holdout_rate"]
        assert (row["value"], row["overridden"]) == (0.0, False)
        assert report["write_provenance"]["env_flags"]["pack_holdout_rate"] == 0.0
