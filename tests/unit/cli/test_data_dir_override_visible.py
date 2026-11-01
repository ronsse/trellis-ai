"""A ``data_dir`` in ``config.yaml`` beats ``TRELLIS_DATA_DIR``, and says so.

That precedence is settled (``test_data_dir.py`` pins it). What was missing is
any signal: restore a store, or clone a deployment's ``config.yaml`` for an
"isolated" run, set ``TRELLIS_DATA_DIR`` to the copy, and every write lands in
the original while the copy sits untouched and the command reports success.

The surfaces below each tell the operator which directory is in use, which
one the variable named, and that the key decided:

* the drill itself, end to end, as a warning on **stderr** only;
* ``admin health``, in both formats;
* the "Stores not initialized" line, which names the ``data_dir`` key when
  the key supplied the path.

Directory names carry Rich markup (``[bold]``) where a path is rendered, so
only an escaped render prints them whole.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import structlog
import yaml

from tests.cli_output import plain
from tests.structlog_isolation import (
    IsolatedCliRunner,
    clear_cached_logger_proxies,
)
from trellis.stores import registry as registry_module
from trellis.stores.registry import StoreRegistry
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = IsolatedCliRunner()

_ARMS = pytest.mark.parametrize("arm", [[], ["--format", "json"]], ids=["text", "json"])
_EVENT = "registry_data_dir_env_ignored"


@pytest.fixture(autouse=True)
def _warnings_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI's default level (WARNING), not this package's muted one.

    Also forgets which disagreements this process already warned about, so
    each test starts a fresh process as far as the once-per-process guard goes.
    """
    monkeypatch.setenv("TRELLIS_LOG_LEVEL", "WARNING")
    monkeypatch.setattr(registry_module, "_WARNED_DATA_DIR_ENV", set())
    clear_cached_logger_proxies()
    structlog.reset_defaults()
    yield
    clear_cached_logger_proxies()
    structlog.reset_defaults()


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COLUMNS", "400")  # Rich must not fold a path
    monkeypatch.delenv("TRELLIS_DATA_DIR", raising=False)


def _deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> tuple[Path, Path]:
    """``admin init`` a config dir and data dir, as an operator's first run."""
    config_dir, data_dir = tmp_path / f"{name}-config", tmp_path / f"{name}-data"
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()
    assert runner.invoke(app, ["admin", "init"]).exit_code == 0
    return config_dir, data_dir


def _evidence(tmp_path: Path, evidence_id: str) -> str:
    path = tmp_path / f"{evidence_id}.json"
    path.write_text(
        json.dumps(
            {
                "evidence_id": evidence_id,
                "evidence_type": "snippet",
                "content": "a note filed during the drill",
                "source_origin": "manual",
            }
        )
    )
    return str(path)


def _documents_in(data_dir: Path) -> int:
    """Count a data dir's documents without the config that points elsewhere."""
    registry = StoreRegistry.from_config_dict({}, data_dir=data_dir)
    return registry.knowledge.document_store.count(include_chunks=False)


def _clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Copy a deployment to new paths and aim both environment variables there.

    The copied ``config.yaml`` still names the original ``data_dir``.
    """
    config_dir, data_dir = _deployment(tmp_path, monkeypatch, "orig")
    restored_config, restored_data = tmp_path / "copy-config", tmp_path / "copy-data"
    shutil.copytree(config_dir, restored_config)
    shutil.copytree(data_dir, restored_data)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(restored_config))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(restored_data))
    _reset_registry()
    return data_dir, restored_data


def _shadow_events(stderr: str) -> list[str]:
    return [line for line in plain(stderr).splitlines() if _EVENT in line]


# -- The drill ---------------------------------------------------------------


@_ARMS
def test_the_restore_drill_writes_to_the_original_and_warns_on_stderr(
    tmp_path, monkeypatch, arm
):
    _isolate(tmp_path, monkeypatch)
    original, restored = _clone(tmp_path, monkeypatch)
    before = (_documents_in(original), _documents_in(restored))

    result = runner.invoke(
        app, ["ingest", "evidence", _evidence(tmp_path, "drill-note"), *arm]
    )

    # The precedence is settled and stays: the write went to the original.
    assert result.exit_code == 0, result.output
    assert (_documents_in(original), _documents_in(restored)) == (
        before[0] + 1,
        before[1],
    )
    # What was missing: the process says which directory it used and why.
    [line] = _shadow_events(result.stderr)
    for needle in ("data_dir", "TRELLIS_DATA_DIR", str(original), str(restored)):
        assert needle in line, line
    if arm:
        assert json.loads(result.stdout)["status"] == "ingested"
    else:
        assert _EVENT not in plain(result.stdout)


def test_the_warning_is_the_only_difference_the_variable_makes_to_the_exit(
    tmp_path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    _clone(tmp_path, monkeypatch)
    evidence = _evidence(tmp_path, "drill-note")

    warned = runner.invoke(app, ["ingest", "evidence", evidence, "--format", "json"])
    monkeypatch.delenv("TRELLIS_DATA_DIR")
    _reset_registry()
    silent = runner.invoke(app, ["ingest", "evidence", evidence, "--format", "json"])

    assert (warned.exit_code, silent.exit_code) == (0, 0)
    assert _shadow_events(warned.stderr)
    assert not _shadow_events(silent.stderr)


# -- admin health ------------------------------------------------------------


def _config_naming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: Path | str | None
) -> Path:
    """A ``config.yaml`` whose ``data_dir`` is created; ``env`` is the variable."""
    _isolate(tmp_path, monkeypatch)
    config_dir, keyed = tmp_path / "config", tmp_path / "keyed[bold]"
    (keyed / "stores").mkdir(parents=True)
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(yaml.safe_dump({"data_dir": str(keyed)}))
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
    if env is not None:
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(env))
    _reset_registry()
    return keyed


def test_health_json_shows_both_directories_when_they_disagree(tmp_path, monkeypatch):
    env = tmp_path / "env-data"
    keyed = _config_naming(tmp_path, monkeypatch, env)

    result = runner.invoke(app, ["admin", "health", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data_dir_disagreement"] == {
        "in_use": str(keyed),
        "in_use_from": "data_dir in config.yaml",
        "ignored": str(env),
        "ignored_from": "TRELLIS_DATA_DIR",
    }


def test_health_text_shows_both_directories_when_they_disagree(tmp_path, monkeypatch):
    env = tmp_path / "env-data[dim]"
    keyed = _config_naming(tmp_path, monkeypatch, env)

    result = runner.invoke(app, ["admin", "health"])

    text = plain(result.stdout)
    assert result.exit_code == 0, result.output
    assert "data_dir in config.yaml overrides TRELLIS_DATA_DIR" in text
    assert f"in use:  {keyed}" in text
    assert f"ignored: {env}" in text


@_ARMS
def test_health_is_silent_about_the_variable_when_it_agrees_or_is_unset(
    tmp_path, monkeypatch, arm
):
    keyed = _config_naming(tmp_path, monkeypatch, None)
    unset = runner.invoke(app, ["admin", "health", *arm])
    monkeypatch.setenv("TRELLIS_DATA_DIR", f"{keyed}/")  # same place, new spelling
    _reset_registry()
    same = runner.invoke(app, ["admin", "health", *arm])

    for result in (unset, same):
        assert result.exit_code == 0, result.output
        assert "overrides_env" not in result.stdout
        assert "ignored" not in plain(result.stdout)


# -- "Stores not initialized" ------------------------------------------------


def test_the_guard_names_the_data_dir_key_when_the_key_supplied_the_path(
    tmp_path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    config_dir, stale = tmp_path / "config", tmp_path / "old-host[bold]" / "data"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    config_path.write_text(yaml.safe_dump({"data_dir": str(stale)}))
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "env-data"))

    result = runner.invoke(app, ["retrieve", "search", "anything", "--format", "json"])

    assert result.exit_code == 1
    assert result.stdout == ""
    text = " ".join(plain(result.stderr).split())
    assert f"Stores not initialized at {stale / 'stores'}." in text
    assert f"'data_dir' in {config_path}" in text
    assert "TRELLIS_DATA_DIR does not override it" in text
    # init is one remedy, for a store that does not exist yet; not the only one.
    assert "moved or restored" in text
    assert "to create it" in text
    assert not stale.exists()  # the guard stopped before anything was created


@pytest.mark.parametrize("source", ["env", "default"])
def test_the_guard_keeps_the_init_advice_when_no_key_supplied_the_path(
    tmp_path, monkeypatch, source
):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    expected = tmp_path / "config" / "data"
    if source == "env":
        expected = tmp_path / "env-data"
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(expected))

    result = runner.invoke(app, ["retrieve", "search", "anything"])

    assert result.exit_code == 1
    text = " ".join(plain(result.stderr).split())
    assert (
        f"Stores not initialized at {expected / 'stores'}. Run 'trellis admin init'"
        in text
    )
    assert "config.yaml" not in text


def test_admin_init_in_that_state_creates_the_configured_dir_and_nothing_else(
    tmp_path, monkeypatch
):
    """What the guard's advice does: recreate ``stores/`` at the stale key's path.

    ``init`` without ``--force`` leaves ``config.yaml`` alone, so it makes an
    empty store where the key points. That is right for a store that never
    existed and wrong for a restore, which is why the message says both.
    """
    _isolate(tmp_path, monkeypatch)
    config_dir, stale = tmp_path / "config", tmp_path / "old-host" / "data"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    config_path.write_text(yaml.safe_dump({"data_dir": str(stale)}))
    before = config_path.read_bytes()
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "restored-data"))

    result = runner.invoke(app, ["admin", "init", "--format", "json"])

    assert json.loads(result.stdout)["data_dir"] == str(stale)
    assert (stale / "stores").is_dir()
    assert config_path.read_bytes() == before
    assert not (tmp_path / "restored-data").exists()
