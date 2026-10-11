"""Tests for ``SettingsStore`` — operator setting overrides.

The shared cross-store contract (degenerate-shape matrix, the AST walk
pinning every write path's guard order, the stale/degraded write refusal
primitives) is asserted once, generically, over all three
``DegradableJsonStore`` subclasses in
``tests/unit/stores/test_degradable_json_store.py`` (the ``"settings"``
``StoreCase``). What lives here is ``SettingsStore``-specific: its own
public shape (``as_values()``), and the same per-store degradation-
visibility and round-trip checks ``test_policy_store.py`` pins for
``PolicyStore`` — mirrored, not re-derived, so the two stay comparable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from tests.recovery_command import expected_recovery
from trellis.errors import DegradedStoreWriteError, StaleStoreWriteError
from trellis.stores.settings_store import (
    SETTINGS_FILENAME,
    SettingsStore,
    load_settings_values,
    resolve_settings_path,
)


def _damaged(tmp_path: Path, text: str) -> Path:
    path = tmp_path / SETTINGS_FILENAME
    path.write_text(text, encoding="utf-8")
    return path


class TestResolveSettingsPath:
    def test_none_stores_dir_is_no_overrides_not_an_error(self) -> None:
        assert resolve_settings_path(None) is None

    def test_joins_the_filename_onto_stores_dir(self, tmp_path: Path) -> None:
        assert resolve_settings_path(tmp_path) == tmp_path / "settings.json"


class TestRoundTrip:
    """The write-then-read round trip on a fresh file (PR 1's failing-before test)."""

    def test_a_fresh_file_starts_empty(self, tmp_path: Path) -> None:
        store = SettingsStore(tmp_path / SETTINGS_FILENAME)
        assert store.list() == []
        assert store.get("pack_holdout_rate") is None
        assert store.as_values() == {}

    def test_set_then_get_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / SETTINGS_FILENAME
        store = SettingsStore(path)
        row = store.set("pack_holdout_rate", 0.25)
        assert row.name == "pack_holdout_rate"
        assert row.value == 0.25

        reloaded = SettingsStore(path)
        assert reloaded.get("pack_holdout_rate").value == 0.25
        assert reloaded.as_values() == {"pack_holdout_rate": 0.25}

    def test_set_persists_every_value_type_the_schema_allows(
        self, tmp_path: Path
    ) -> None:
        store = SettingsStore(tmp_path / SETTINGS_FILENAME)
        store.set("memory_extraction", True)
        store.set("minhash_seed_max_docs", 500)
        store.set("pack_holdout_rate", 0.1)
        store.set("reconcile_model", "llama3.2:3b")

        reloaded = SettingsStore(tmp_path / SETTINGS_FILENAME)
        assert reloaded.as_values() == {
            "memory_extraction": True,
            "minhash_seed_max_docs": 500,
            "pack_holdout_rate": 0.1,
            "reconcile_model": "llama3.2:3b",
        }

    def test_set_replaces_an_existing_override(self, tmp_path: Path) -> None:
        store = SettingsStore(tmp_path / SETTINGS_FILENAME)
        store.set("pack_holdout_rate", 0.1)
        store.set("pack_holdout_rate", 0.9)
        assert store.get("pack_holdout_rate").value == 0.9
        assert len(store.list()) == 1

    def test_remove_reports_whether_it_found_one(self, tmp_path: Path) -> None:
        store = SettingsStore(tmp_path / SETTINGS_FILENAME)
        store.set("pack_holdout_rate", 0.1)
        assert store.remove("pack_holdout_rate") is True
        assert store.remove("pack_holdout_rate") is False
        assert store.as_values() == {}


class TestLoadSettingsValues:
    """The convenience wrapper ``build_pack_builder`` and ``admin.py`` call."""

    def test_none_stores_dir_is_an_empty_mapping(self) -> None:
        assert load_settings_values(None) == {}

    def test_reads_through_to_the_store(self, tmp_path: Path) -> None:
        SettingsStore(tmp_path / SETTINGS_FILENAME).set("pack_holdout_rate", 0.3)
        assert load_settings_values(tmp_path) == {"pack_holdout_rate": 0.3}

    def test_absent_file_is_an_empty_mapping_not_an_error(self, tmp_path: Path) -> None:
        assert load_settings_values(tmp_path) == {}


class TestCorruptFileDegradesNotRaises:
    """The second failing-before test: corrupt-file read degrades."""

    def test_malformed_json_degrades_rather_than_raising(self, tmp_path: Path) -> None:
        path = _damaged(tmp_path, "{ not json")
        store = SettingsStore(path)  # must not raise
        assert store.is_degraded is True
        assert store.list() == []
        assert store.as_values() == {}

    def test_one_bad_row_costs_that_row_not_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / SETTINGS_FILENAME
        path.write_text(
            json.dumps(
                {
                    "settings": [
                        {"name": "pack_holdout_rate", "value": 0.2},
                        {"name": "broken"},  # missing required "value"
                    ]
                }
            ),
            encoding="utf-8",
        )
        store = SettingsStore(path)
        assert store.is_degraded is True
        assert store.as_values() == {"pack_holdout_rate": 0.2}

    def test_an_explicitly_empty_list_is_not_degradation(self, tmp_path: Path) -> None:
        store = SettingsStore(_damaged(tmp_path, '{"settings": []}'))
        assert store.is_degraded is False
        assert store.as_values() == {}
        store.set("pack_holdout_rate", 0.1)  # a clean store still writes


class TestWriteWhileDegradedRefuses:
    """The third failing-before test, pinned against the shared primitive."""

    def test_set_raises_degraded_store_write_error(self, tmp_path: Path) -> None:
        store = SettingsStore(_damaged(tmp_path, "{ not json"))
        with pytest.raises(DegradedStoreWriteError):
            store.set("pack_holdout_rate", 0.1)

    def test_remove_raises_degraded_store_write_error(self, tmp_path: Path) -> None:
        store = SettingsStore(_damaged(tmp_path, "{ not json"))
        with pytest.raises(DegradedStoreWriteError):
            store.remove("pack_holdout_rate")

    def test_the_damaged_file_is_left_untouched(self, tmp_path: Path) -> None:
        path = _damaged(tmp_path, "{ not json")
        before = path.read_text(encoding="utf-8")
        store = SettingsStore(path)
        with pytest.raises(DegradedStoreWriteError):
            store.set("pack_holdout_rate", 0.1)
        assert path.read_text(encoding="utf-8") == before

    def test_a_stale_store_also_refuses(self, tmp_path: Path) -> None:
        path = tmp_path / SETTINGS_FILENAME
        store = SettingsStore(path)
        store.set("pack_holdout_rate", 0.1)
        # A second handle on the same file, then the first writes again —
        # the second is now stale relative to what's on disk.
        other = SettingsStore(path)
        other.set("pack_holdout_rate", 0.2)
        with pytest.raises(StaleStoreWriteError):
            store.set("pack_holdout_rate", 0.3)


class TestDegradationIsLoggedWhereOperatorsSee:
    """Mirrors ``test_policy_store.py``'s pinned log-level test exactly."""

    def test_the_line_is_error_not_info(self, tmp_path: Path) -> None:
        path = _damaged(tmp_path, "{ broken")

        with capture_logs() as logs:
            SettingsStore(path)

        lines = [e for e in logs if e["event"] == "settings_load_degraded"]
        assert len(lines) == 1
        assert lines[0]["log_level"] == "error"
        assert lines[0]["reason"] == "malformed_json"
        assert lines[0]["path"] == str(path)
        assert lines[0]["recovery"] == expected_recovery(path)

    def test_a_clean_load_says_nothing_alarming(self, tmp_path: Path) -> None:
        path = tmp_path / SETTINGS_FILENAME
        SettingsStore(path).set("pack_holdout_rate", 0.1)

        with capture_logs() as logs:
            SettingsStore(path)

        assert not [e for e in logs if e["event"] == "settings_load_degraded"]
