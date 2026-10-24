"""``trellis curate advisory restore`` (#968).

The store half (``restore_many``: one write, all-or-nothing, guarded) is pinned
in ``tests/unit/stores/test_advisory_store.py``; this file pins what the command
adds on top: argument refusals, the rule filter, ``--dry-run``, the audit event,
the degraded/stale refusals, and that no exit code depends on ``--format``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.schemas.advisory import (
    RETIRED_BY_KEY,
    RETIRED_BY_QUERY_FILTER,
    RETIRED_RULE_KEY,
    Advisory,
    AdvisoryCategory,
    AdvisoryEvidence,
    AdvisoryStatus,
)
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli import curate_advisory
from trellis_cli.exit_codes import EXIT_OK, EXIT_STORE, EXIT_VALIDATION
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = CliRunner()

DOC_FREQ = "document_frequency"
STOP_LIST = "stop_list"


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    """Point the CLI at a temp data dir and return a registry over the same files."""
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()
    return StoreRegistry(stores_dir=stores_dir)


@pytest.fixture
def temp_stores(_temp_stores: StoreRegistry) -> StoreRegistry:
    """Expose the autouse registry for tests that read the event log."""
    return _temp_stores


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "stores" / "advisories.json"


def _advisory(message: str = "widget advice", **kwargs: Any) -> Advisory:
    return Advisory(
        category=AdvisoryCategory.QUERY,
        confidence=0.6,
        message=message,
        evidence=AdvisoryEvidence(
            sample_size=10,
            success_rate_with=0.8,
            success_rate_without=0.4,
            effect_size=0.4,
        ),
        scope="global",
        **kwargs,
    )


def _retire(store: AdvisoryStore, advisory: Advisory, rule: str) -> None:
    """Suppress *advisory* the way the generator's rule retirement does."""
    store.suppress(
        advisory.advisory_id,
        reason=f"query keyword excluded by {rule}: 'widget'",
        metadata={RETIRED_BY_KEY: RETIRED_BY_QUERY_FILTER, RETIRED_RULE_KEY: rule},
    )


def _seed(path: Path) -> dict[str, Advisory]:
    """Two doc-frequency rows, one stop-list row, one fitness-suppressed, one active."""
    store = AdvisoryStore(path)
    rows = {
        name: store.put(_advisory(f"{name} advice", metadata={"keyword": name}))
        for name in ("df_a", "df_b", "stop", "fitness", "active")
    }
    _retire(store, rows["df_a"], DOC_FREQ)
    _retire(store, rows["df_b"], DOC_FREQ)
    _retire(store, rows["stop"], STOP_LIST)
    store.suppress(rows["fitness"].advisory_id, reason="confidence 0.05 < 0.1")
    return rows


def _status(path: Path, advisory: Advisory) -> AdvisoryStatus:
    got = AdvisoryStore(path).get(advisory.advisory_id)
    assert got is not None
    return got.status


def _events(registry: StoreRegistry) -> list[Event]:
    return registry.operational.event_log.get_events(
        event_type=EventType.ADVISORY_RESTORED
    )


def _invoke(*args: str, fmt: str = "json") -> Any:
    return runner.invoke(app, ["curate", "advisory", "restore", *args, "--format", fmt])


class TestRestoreOne:
    def test_restores_the_row_and_clears_its_retirement(
        self, path: Path, temp_stores: StoreRegistry
    ) -> None:
        rows = _seed(path)
        target = rows["df_a"]

        result = _invoke(target.advisory_id, "--reason", "word is a domain noun")

        assert result.exit_code == EXIT_OK, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "restored"
        assert payload["count"] == 1
        assert payload["audit_events_failed"] == 0
        row = payload["advisories"][0]
        assert row["advisory_id"] == target.advisory_id
        assert row["previous_retired_rule"] == DOC_FREQ
        assert "document_frequency" in row["previous_suppression_reason"]
        restored = AdvisoryStore(path).get(target.advisory_id)
        assert restored is not None
        assert restored.status == AdvisoryStatus.ACTIVE
        assert restored.metadata == {"keyword": "df_a"}
        assert _status(path, rows["df_b"]) == AdvisoryStatus.SUPPRESSED

    def test_emits_one_audit_event_naming_what_was_undone(
        self, path: Path, temp_stores: StoreRegistry
    ) -> None:
        rows = _seed(path)
        target = rows["df_a"]

        _invoke(target.advisory_id, "--reason", "word is a domain noun")

        (event,) = _events(temp_stores)
        assert event.source == "cli:curate"
        assert event.entity_id == target.advisory_id
        assert event.entity_type == "advisory"
        assert event.payload["advisory_id"] == target.advisory_id
        assert event.payload["previous_retired_rule"] == DOC_FREQ
        assert event.payload["reason"] == "word is a domain noun"
        assert "new_confidence" not in event.payload

    def test_restores_a_row_the_fitness_loop_suppressed_too(
        self, path: Path, temp_stores: StoreRegistry
    ) -> None:
        """The single-id form undoes any suppression, not only a rule retirement."""
        rows = _seed(path)

        result = _invoke(rows["fitness"].advisory_id)

        assert result.exit_code == EXIT_OK, result.output
        assert _status(path, rows["fitness"]) == AdvisoryStatus.ACTIVE
        (event,) = _events(temp_stores)
        assert event.payload["previous_retired_rule"] is None

    def test_an_active_advisory_is_a_no_op(
        self, path: Path, temp_stores: StoreRegistry
    ) -> None:
        rows = _seed(path)
        before = path.read_bytes()

        result = _invoke(rows["active"].advisory_id)

        assert result.exit_code == EXIT_OK, result.output
        assert json.loads(result.stdout)["status"] == "noop"
        assert path.read_bytes() == before
        assert _events(temp_stores) == []

    def test_an_unknown_id_exits_validation_in_both_formats(
        self, path: Path, temp_stores: StoreRegistry
    ) -> None:
        _seed(path)
        before = path.read_bytes()

        as_json = _invoke("no-such-id")
        as_text = _invoke("no-such-id", fmt="text")

        assert as_json.exit_code == as_text.exit_code == EXIT_VALIDATION
        assert json.loads(as_json.stdout)["status"] == "not_found"
        assert "No advisory no-such-id" in plain(as_text.stdout)
        assert path.read_bytes() == before
        assert _events(temp_stores) == []

    def test_text_output_survives_markup_in_the_message(
        self, path: Path, temp_stores: StoreRegistry
    ) -> None:
        """A free-text message with Rich markup must print, not raise MarkupError."""
        store = AdvisoryStore(path)
        row = store.put(_advisory("keep [/oops] and [bold]this[/bold] literal"))
        store.suppress(row.advisory_id, reason="r")

        result = _invoke(row.advisory_id, fmt="text")

        assert result.exit_code == EXIT_OK, result.output
        assert "[/oops]" in plain(result.stdout)
        assert "[bold]this[/bold]" in plain(result.stdout)
        # The category's own brackets are markup too, not just the values.
        assert "[query]" in plain(result.stdout)


class TestRestoreByRule:
    def test_restores_only_the_rows_that_rule_retired(
        self, path: Path, temp_stores: StoreRegistry
    ) -> None:
        rows = _seed(path)

        result = _invoke("--rule", DOC_FREQ, "--all")

        assert result.exit_code == EXIT_OK, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "restored"
        assert payload["rule"] == DOC_FREQ
        assert {a["advisory_id"] for a in payload["advisories"]} == {
            rows["df_a"].advisory_id,
            rows["df_b"].advisory_id,
        }
        assert _status(path, rows["df_a"]) == AdvisoryStatus.ACTIVE
        assert _status(path, rows["df_b"]) == AdvisoryStatus.ACTIVE
        # Another rule's retirement and the fitness loop's suppression are untouched.
        assert _status(path, rows["stop"]) == AdvisoryStatus.SUPPRESSED
        assert _status(path, rows["fitness"]) == AdvisoryStatus.SUPPRESSED
        assert {e.entity_id for e in _events(temp_stores)} == {
            rows["df_a"].advisory_id,
            rows["df_b"].advisory_id,
        }

    def test_a_rule_with_nothing_retired_is_a_clean_zero(
        self, path: Path, temp_stores: StoreRegistry
    ) -> None:
        AdvisoryStore(path).put(_advisory())
        before = path.read_bytes()

        result = _invoke("--rule", DOC_FREQ, "--all")

        assert result.exit_code == EXIT_OK, result.output
        assert json.loads(result.stdout)["count"] == 0
        assert path.read_bytes() == before

    def test_dry_run_lists_the_rows_and_writes_nothing(
        self, path: Path, temp_stores: StoreRegistry
    ) -> None:
        rows = _seed(path)
        before = path.read_bytes()

        as_json = _invoke("--rule", DOC_FREQ, "--all", "--dry-run")
        as_text = _invoke("--rule", DOC_FREQ, "--all", "--dry-run", fmt="text")

        assert as_json.exit_code == as_text.exit_code == EXIT_OK
        payload = json.loads(as_json.stdout)
        assert payload["status"] == "dry_run"
        assert payload["dry_run"] is True
        assert payload["count"] == 2
        assert "Would restore 2 advisories" in plain(as_text.stdout)
        assert rows["df_a"].advisory_id in plain(as_text.stdout)
        assert path.read_bytes() == before
        assert _events(temp_stores) == []

    def test_dry_run_on_one_id_writes_nothing(
        self, path: Path, temp_stores: StoreRegistry
    ) -> None:
        rows = _seed(path)
        before = path.read_bytes()

        result = _invoke(rows["df_a"].advisory_id, "--dry-run")

        assert result.exit_code == EXIT_OK, result.output
        assert json.loads(result.stdout)["status"] == "dry_run"
        assert path.read_bytes() == before


class TestArgumentRefusals:
    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(["--all"], id="all-without-rule"),
            pytest.param(["--rule", DOC_FREQ], id="rule-without-all"),
            pytest.param([], id="nothing-named"),
            pytest.param(["some-id", "--rule", DOC_FREQ, "--all"], id="id-and-all"),
            pytest.param(["some-id", "--all"], id="id-and-all-no-rule"),
            pytest.param(["--rule", "document_freq", "--all"], id="unknown-rule"),
        ],
    )
    def test_refused_with_exit_validation_and_nothing_written(
        self, path: Path, temp_stores: StoreRegistry, args: list[str]
    ) -> None:
        _seed(path)
        before = path.read_bytes()

        as_json = _invoke(*args)
        as_text = _invoke(*args, fmt="text")

        # Same code either way: the exit sits below the format branch.
        assert as_json.exit_code == as_text.exit_code == EXIT_VALIDATION
        assert json.loads(as_json.stdout)["status"] == "invalid"
        assert path.read_bytes() == before
        assert _events(temp_stores) == []

    def test_all_without_rule_says_why(self, path: Path) -> None:
        _seed(path)

        result = _invoke("--all", fmt="text")

        flat = plain(result.stdout).replace("\n", " ")
        assert "--all requires --rule" in flat
        assert DOC_FREQ in flat

    def test_the_group_is_reachable_from_curate(self) -> None:
        result = runner.invoke(app, ["curate", "advisory", "--help"])

        assert result.exit_code == EXIT_OK, result.output
        assert "restore" in plain(result.stdout)


class TestStoreRefusals:
    def test_a_degraded_store_exits_store_and_writes_nothing(
        self, path: Path, temp_stores: StoreRegistry
    ) -> None:
        rows = _seed(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["advisories"].append({"advisory_id": "broken", "renamed": 1})
        path.write_text(json.dumps(raw), encoding="utf-8")
        before = path.read_bytes()

        for extra in ([], ["--dry-run"]):
            as_json = _invoke(rows["df_a"].advisory_id, *extra)
            as_text = _invoke(rows["df_a"].advisory_id, *extra, fmt="text")

            assert as_json.exit_code == as_text.exit_code == EXIT_STORE
            assert json.loads(as_json.stdout)["status"] == "degraded"
        assert path.read_bytes() == before
        assert _events(temp_stores) == []

    def test_a_stale_store_exits_store_and_keeps_the_other_writers_row(
        self,
        path: Path,
        temp_stores: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Another process writes between the command's load and its save."""
        rows = _seed(path)
        theirs = _advisory("written by the other process")

        def _racing_store(store_path: Path) -> AdvisoryStore:
            store = AdvisoryStore(store_path)
            AdvisoryStore(path).put(theirs)
            return store

        monkeypatch.setattr(curate_advisory, "AdvisoryStore", _racing_store)

        result = _invoke("--rule", DOC_FREQ, "--all")

        assert result.exit_code == EXIT_STORE, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "refused"
        assert payload["code"] == "STALE_STORE_WRITE"
        assert _status(path, rows["df_a"]) == AdvisoryStatus.SUPPRESSED
        assert AdvisoryStore(path).get(theirs.advisory_id) is not None
        assert _events(temp_stores) == []

    def test_a_stale_refusal_exits_alike_in_text(
        self, path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = _seed(path)

        def _racing_store(store_path: Path) -> AdvisoryStore:
            store = AdvisoryStore(store_path)
            AdvisoryStore(path).put(_advisory("theirs"))
            return store

        monkeypatch.setattr(curate_advisory, "AdvisoryStore", _racing_store)

        result = _invoke(rows["df_a"].advisory_id, fmt="text")

        assert result.exit_code == EXIT_STORE, result.output
        assert "ADVISORY WRITE REFUSED" in plain(result.stdout)


class TestAuditIsFailSoft:
    def test_a_failed_event_does_not_undo_or_fail_the_restore(
        self, path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = _seed(path)
        broken = MagicMock()
        broken.emit.side_effect = RuntimeError("event log down")
        monkeypatch.setattr(curate_advisory, "get_event_log", lambda: broken)

        as_json = _invoke(rows["df_a"].advisory_id)
        as_text = _invoke(rows["df_b"].advisory_id, fmt="text")

        assert as_json.exit_code == as_text.exit_code == EXIT_OK
        assert json.loads(as_json.stdout)["audit_events_failed"] == 1
        assert "were not recorded" in plain(as_text.stdout)
        assert _status(path, rows["df_a"]) == AdvisoryStatus.ACTIVE
        assert _status(path, rows["df_b"]) == AdvisoryStatus.ACTIVE
