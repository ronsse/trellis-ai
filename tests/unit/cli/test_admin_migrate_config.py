"""Tests for ``trellis admin migrate-config``.

Covers the ADR planes-and-substrates Phase 2 CLI: rewriting a legacy
flat ``stores:`` block into ``knowledge:`` + ``operational:`` blocks,
with backup, dry-run, and idempotency handling.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import yaml
from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from trellis_cli.main import app

runner = CliRunner()


def _write_config(dir_path: Path, data: dict) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    cfg = dir_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(data, sort_keys=False))
    return cfg


class TestMigrateConfig:
    def test_migrates_flat_to_plane_split(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "config"
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
        _write_config(
            config_dir,
            {
                "data_dir": "preserved-test-value",
                "stores": {
                    "graph": {"backend": "sqlite"},
                    "vector": {"backend": "sqlite"},
                    "document": {"backend": "sqlite"},
                    "blob": {"backend": "local"},
                    "trace": {"backend": "sqlite"},
                    "event_log": {"backend": "sqlite"},
                },
            },
        )

        result = runner.invoke(app, ["admin", "migrate-config"])
        assert result.exit_code == 0, result.stdout

        migrated = yaml.safe_load((config_dir / "config.yaml").read_text())
        assert "stores" not in migrated
        assert set(migrated["knowledge"].keys()) == {
            "graph",
            "vector",
            "document",
            "blob",
        }
        assert set(migrated["operational"].keys()) == {"trace", "event_log"}
        # Non-store keys preserved
        assert migrated["data_dir"] == "preserved-test-value"

    def test_creates_backup_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "config"
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
        _write_config(
            config_dir,
            {"stores": {"graph": {"backend": "sqlite"}}},
        )

        result = runner.invoke(app, ["admin", "migrate-config"])
        assert result.exit_code == 0

        backups = list(config_dir.glob("config.yaml.bak.*"))
        assert len(backups) == 1
        backup_content = yaml.safe_load(backups[0].read_text())
        assert "stores" in backup_content  # pre-migration shape preserved

    def test_force_skips_backup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "config"
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
        _write_config(
            config_dir,
            {"stores": {"graph": {"backend": "sqlite"}}},
        )

        result = runner.invoke(app, ["admin", "migrate-config", "--force"])
        assert result.exit_code == 0
        assert not list(config_dir.glob("config.yaml.bak.*"))

    def test_dry_run_does_not_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "config"
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
        original = {"stores": {"graph": {"backend": "sqlite"}}}
        _write_config(config_dir, original)

        result = runner.invoke(app, ["admin", "migrate-config", "--dry-run"])
        assert result.exit_code == 0

        # File unchanged
        current = yaml.safe_load((config_dir / "config.yaml").read_text())
        assert current == original
        # No backup files created
        assert not list(config_dir.glob("config.yaml.bak.*"))

    def test_missing_config_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True)
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))

        result = runner.invoke(app, ["admin", "migrate-config"])
        assert result.exit_code == 1

    def test_already_migrated_is_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "config"
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
        _write_config(
            config_dir,
            {
                "knowledge": {"graph": {"backend": "sqlite"}},
                "operational": {"trace": {"backend": "sqlite"}},
            },
        )

        result = runner.invoke(app, ["admin", "migrate-config"])
        assert result.exit_code == 0
        assert "already-migrated" in result.stdout or "already" in result.stdout

    def test_nothing_to_do_when_no_stores_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "config"
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
        _write_config(config_dir, {"data_dir": "preserved-test-value"})

        result = runner.invoke(app, ["admin", "migrate-config"])
        assert result.exit_code == 0
        assert "nothing-to-do" in result.stdout or "No" in result.stdout

    def test_json_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "config"
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
        _write_config(
            config_dir,
            {"stores": {"graph": {"backend": "sqlite"}}},
        )

        result = runner.invoke(
            app, ["admin", "migrate-config", "--format", "json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "migrated"
        assert "backup" in data

    def test_unknown_store_type_is_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "config"
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
        _write_config(
            config_dir,
            {
                "stores": {
                    "graph": {"backend": "sqlite"},
                    "whatever": {"backend": "x"},
                }
            },
        )

        result = runner.invoke(app, ["admin", "migrate-config"])
        assert result.exit_code == 1
