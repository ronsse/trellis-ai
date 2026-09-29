"""One data dir: ``admin init``, the stores guard, ``admin health`` and the registry.

``admin init --data-dir X`` writes ``data_dir: X`` into ``config.yaml``.
``StoreRegistry.from_config_dir`` (MCP, the REST API, the capture sweep and
every CLI store command) reads that key ahead of ``TRELLIS_DATA_DIR``, but
the CLI's ``get_data_dir`` did not. So the stores guard checked another
directory, and the ``admin init`` it advised found ``config.yaml`` and
repaired nothing. The directory names below carry Rich markup, which only
an escaped render prints.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.mutate import resolve_policy_path
from trellis.stores.registry import StoreRegistry
from trellis_cli.config import get_data_dir
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = CliRunner()

_ARMS = pytest.mark.parametrize("arm", [[], ["--format", "json"]], ids=["text", "json"])


def _isolate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data_env: Path | None = None
) -> Path:
    """Scratch HOME and config dir; ``TRELLIS_DATA_DIR`` only when given."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("COLUMNS", "400")  # Rich must not fold a path
    if data_env is None:
        monkeypatch.delenv("TRELLIS_DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_env))
    return tmp_path / "config"


def _write_config(config_dir: Path, document: dict[str, str]) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.yaml"
    path.write_text(yaml.safe_dump(document))
    return path


def _invoke(*args: str):
    _reset_registry()
    return runner.invoke(app, list(args))


def _keyed_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """``config.yaml`` names one data dir and ``TRELLIS_DATA_DIR`` another."""
    keyed, data_env = tmp_path / "keyed[bold]", tmp_path / "env-data"
    config_dir = _isolate(tmp_path, monkeypatch, data_env)
    _write_config(config_dir, {"data_dir": str(keyed)})
    return keyed, data_env


@_ARMS
def test_a_store_command_finds_the_stores_init_made_under_data_dir(
    tmp_path, monkeypatch, arm
):
    config_dir = _isolate(tmp_path, monkeypatch)
    data_dir = tmp_path / "custom"

    assert _invoke("admin", "init", "--data-dir", str(data_dir)).exit_code == 0
    result = _invoke("retrieve", "search", "anything", *arm)

    assert result.exit_code == 0, result.output
    assert (data_dir / "stores" / "documents.db").is_file()
    assert not (config_dir / "data").exists()


@_ARMS
def test_init_recreates_a_missing_stores_dir_and_leaves_config_yaml_alone(
    tmp_path, monkeypatch, arm
):
    config_dir = _isolate(tmp_path, monkeypatch)
    data_dir = tmp_path / "custom[dim]"
    assert _invoke("admin", "init", "--data-dir", str(data_dir)).exit_code == 0
    config_bytes = (config_dir / "config.yaml").read_bytes()
    shutil.rmtree(data_dir)  # the data dir too, not only stores/

    first = _invoke("admin", "init", *arm)
    second = _invoke("admin", "init", *arm)

    assert (first.exit_code, second.exit_code) == (0, 0)
    assert (data_dir / "stores").is_dir()
    assert (config_dir / "config.yaml").read_bytes() == config_bytes
    assert _invoke("retrieve", "search", "anything").exit_code == 0
    if arm:
        assert json.loads(first.stdout) == {
            "status": "exists",
            "config_dir": str(config_dir),
            "data_dir": str(data_dir),
            "stores_created": True,
        }
        assert json.loads(second.stdout)["stores_created"] is False
    else:
        created = f"Created the missing stores directory: {data_dir / 'stores'}"
        assert "Config already exists at" in plain(first.stdout)
        assert created in plain(first.stdout)
        assert "Config already exists at" in plain(second.stdout)
        assert "Created the missing" not in plain(second.stdout)


@_ARMS
def test_the_stores_guard_checks_and_names_the_registry_s_directory(
    tmp_path, monkeypatch, arm
):
    keyed, data_env = _keyed_and_env(tmp_path, monkeypatch)
    (data_env / "stores").mkdir(parents=True)  # present, but not the one in use

    result = _invoke("retrieve", "search", "anything", *arm)

    assert result.exit_code == 1
    assert result.stdout == ""
    message = f"Stores not initialized at {keyed / 'stores'}. Run 'trellis admin init'"
    assert message in plain(result.stderr)
    assert not keyed.exists()  # stopped before the registry could create it


@pytest.mark.parametrize("present", ["keyed", "env"])
def test_health_and_the_stores_guard_read_the_same_directory(
    tmp_path, monkeypatch, present
):
    keyed, data_env = _keyed_and_env(tmp_path, monkeypatch)
    ((keyed if present == "keyed" else data_env) / "stores").mkdir(parents=True)
    in_use = present == "keyed"

    health = json.loads(_invoke("admin", "health", "--format", "json").stdout)
    text = plain(_invoke("admin", "health").stdout)
    search = _invoke("retrieve", "search", "anything", "--format", "json")

    assert (health["data_dir"], health["stores_dir"]) == (in_use, in_use)
    rows = {
        cells[0]: cells[1]
        for line in text.splitlines()
        if len(cells := [c.strip() for c in line.split("│")[1:-1]]) == 2
    }
    status = "OK" if in_use else "MISSING"
    assert (rows["data_dir"], rows["stores_dir"]) == (status, status)
    assert (search.exit_code == 0) is in_use
    assert keyed.exists() is in_use


def test_config_yaml_data_dir_beats_trellis_data_dir(tmp_path, monkeypatch):
    keyed, _ = _keyed_and_env(tmp_path, monkeypatch)

    assert get_data_dir() == keyed
    assert StoreRegistry.from_config_dir().stores_dir == keyed / "stores"


def test_trellis_data_dir_beats_the_config_dir_default(tmp_path, monkeypatch):
    data_env = tmp_path / "env-data"
    config_dir = _isolate(tmp_path, monkeypatch, data_env)
    _write_config(config_dir, {"default_domain": "ops"})  # no data_dir key

    assert get_data_dir() == data_env
    assert StoreRegistry.from_config_dir().stores_dir == data_env / "stores"
    monkeypatch.delenv("TRELLIS_DATA_DIR")
    assert get_data_dir() == config_dir / "data"
    assert StoreRegistry.from_config_dir().stores_dir == config_dir / "data" / "stores"


def test_a_new_config_s_default_data_dir_ignores_the_one_it_replaces(
    tmp_path, monkeypatch
):
    from trellis_cli.config import get_default_data_dir

    _, data_env = _keyed_and_env(tmp_path, monkeypatch)

    assert get_default_data_dir() == data_env


def test_init_force_still_rewrites_a_config_yaml_that_does_not_parse(
    tmp_path, monkeypatch
):
    data_env = tmp_path / "env-data"
    config_dir = _isolate(tmp_path, monkeypatch, data_env)
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("data_dir: [unclosed\n")

    plain_init = _invoke("admin", "init", "--format", "json")
    forced = _invoke("admin", "init", "--force", "--format", "json")

    # Without --force, init has no data dir to repair: the config says
    # where it is and cannot be read. --force replaces the file unread.
    assert plain_init.exit_code == 5
    assert forced.exit_code == 0, forced.output
    assert json.loads(forced.stdout)["data_dir"] == str(data_env)
    assert (data_env / "stores").is_dir()


def test_policy_add_writes_the_file_the_registry_s_gate_reads(tmp_path, monkeypatch):
    keyed, data_env = _keyed_and_env(tmp_path, monkeypatch)
    (keyed / "stores").mkdir(parents=True)

    result = _invoke(
        "policy", "add", "--operation", "*", "--action", "warn", "--format", "json"
    )

    assert result.exit_code == 0, result.output
    gate_reads = resolve_policy_path(StoreRegistry.from_config_dir().stores_dir)
    assert gate_reads == keyed / "stores" / "policies.json"
    assert gate_reads.is_file()
    assert not data_env.exists()


@pytest.mark.parametrize("config_yaml", [True, False], ids=["config", "no-config"])
def test_a_store_command_and_health_each_build_one_registry(
    tmp_path, monkeypatch, config_yaml
):
    # Each registry reads config.yaml and logs its warnings again (one per
    # read is pinned in tests/unit/stores/test_registry_config_literals.py),
    # so a second one doubles every warning an operator sees. With no
    # config.yaml, TrellisConfig.load() must not build a second one either.
    data_env = tmp_path / "env-data"
    (data_env / "stores").mkdir(parents=True)
    config_dir = _isolate(tmp_path, monkeypatch, data_env)
    if config_yaml:
        _write_config(config_dir, {"default_domain": "ops"})  # health's no-key path
    assert (config_dir / "config.yaml").exists() is config_yaml
    reads: list[object] = []
    original = StoreRegistry.from_config_dir.__func__

    def counting(cls, *args, **kwargs):
        reads.append(args or kwargs)
        return original(cls, *args, **kwargs)

    monkeypatch.setattr(StoreRegistry, "from_config_dir", classmethod(counting))

    for command in (["retrieve", "search", "anything"], ["admin", "health"]):
        reads.clear()
        assert _invoke(*command, "--format", "json").exit_code == 0
        assert len(reads) == 1, (command, reads)
