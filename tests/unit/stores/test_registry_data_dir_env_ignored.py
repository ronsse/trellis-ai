"""``config.yaml``'s ``data_dir`` beats ``TRELLIS_DATA_DIR``, and the registry says so.

The precedence is settled and pinned in ``tests/unit/cli/test_data_dir.py``.
What these tests pin is the one warning that makes it visible: logged once per
process, only when the variable names a *different* directory, and never able
to fail the write that built the registry. The CLI surfaces (the drill, health,
the stores guard) are in ``tests/unit/cli/test_data_dir_override_visible.py``.

Fixture words are generic; every path is under ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from structlog.testing import capture_logs

from trellis.stores import registry as registry_module
from trellis.stores.registry import StoreRegistry

_EVENT = "registry_data_dir_env_ignored"


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRELLIS_DATA_DIR", raising=False)
    monkeypatch.setattr(registry_module, "_WARNED_DATA_DIR_ENV", set())


def _config(tmp_path: Path, data_dir: str | None) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    document = {} if data_dir is None else {"data_dir": data_dir}
    (config_dir / "config.yaml").write_text(yaml.safe_dump(document))
    return config_dir


def _open(config_dir: Path) -> tuple[StoreRegistry, list[dict[str, object]]]:
    with capture_logs() as logs:
        registry = StoreRegistry.from_config_dir(config_dir=config_dir)
    return registry, [e for e in logs if e["event"] == _EVENT]


def test_a_variable_naming_another_directory_warns_with_both_paths_and_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyed, env = tmp_path / "keyed", tmp_path / "env"
    config_dir = _config(tmp_path, str(keyed))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(env))

    registry, [warning] = _open(config_dir)

    assert warning["log_level"] == "warning"
    assert warning["config_key"] == "data_dir"
    assert warning["config_data_dir"] == str(keyed)
    assert warning["env_var"] == "TRELLIS_DATA_DIR"
    assert warning["env_data_dir"] == str(env)
    assert warning["config_file"] == str(config_dir / "config.yaml")
    hint = str(warning["hint"])
    assert all(part in hint for part in (str(keyed), str(env), "'data_dir'"))
    # The precedence is unchanged: the key wins.
    assert registry.stores_dir == keyed / "stores"
    assert registry.data_dir_origin == "config.yaml"
    assert registry.ignored_env_data_dir == env


def test_it_warns_once_per_process_however_many_registries_are_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _config(tmp_path, str(tmp_path / "keyed"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "env"))

    with capture_logs() as logs:
        registries = [
            StoreRegistry.from_config_dir(config_dir=config_dir) for _ in range(3)
        ]

    assert len([e for e in logs if e["event"] == _EVENT]) == 1
    # Health reads the registry, not the log, so each one still knows.
    assert all(r.ignored_env_data_dir == tmp_path / "env" for r in registries)


def test_a_different_pair_of_directories_is_a_different_disagreement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _config(tmp_path, str(tmp_path / "keyed"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "env-a"))
    assert len(_open(config_dir)[1]) == 1

    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "env-b"))

    assert len(_open(config_dir)[1]) == 1


@pytest.mark.parametrize("env", [None, ""], ids=["unset", "empty"])
def test_an_unset_or_empty_variable_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str | None
) -> None:
    config_dir = _config(tmp_path, str(tmp_path / "keyed"))
    if env is not None:
        monkeypatch.setenv("TRELLIS_DATA_DIR", env)

    registry, warnings = _open(config_dir)

    assert warnings == []
    assert registry.ignored_env_data_dir is None


def test_without_a_data_dir_key_the_variable_is_what_decides_and_nothing_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _config(tmp_path, None)
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "env"))

    registry, warnings = _open(config_dir)

    assert warnings == []
    assert registry.stores_dir == tmp_path / "env" / "stores"
    assert (registry.data_dir_origin, registry.ignored_env_data_dir) == (
        "TRELLIS_DATA_DIR",
        None,
    )


def test_no_config_yaml_at_all_is_silent_and_defaults_to_the_config_dir(
    tmp_path: Path,
) -> None:
    registry, warnings = _open(tmp_path / "config")

    assert warnings == []
    assert registry.stores_dir == tmp_path / "config" / "data" / "stores"
    assert registry.data_dir_origin == "default"


# -- the same directory spelled another way is not a disagreement ---------------


def _spellings(tmp_path: Path) -> dict[str, tuple[str, str]]:
    """``config.yaml`` value and variable value that name one directory."""
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    other = tmp_path / "other"
    other.mkdir()
    return {
        "identical": (str(real), str(real)),
        "trailing-slash": (str(real), f"{real}/"),
        "dot-segments": (str(real), f"{other}/../real/."),
        "symlink-in-config": (str(tmp_path / "link"), str(real)),
        "symlink-in-env": (str(real), str(tmp_path / "link")),
        "not-created-yet": (str(tmp_path / "new"), f"{tmp_path}/new/"),
    }


@pytest.mark.parametrize(
    "spelling",
    [
        "identical",
        "trailing-slash",
        "dot-segments",
        "symlink-in-config",
        "symlink-in-env",
        "not-created-yet",
    ],
)
def test_one_directory_spelled_two_ways_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    keyed, env = _spellings(tmp_path)[spelling]
    config_dir = _config(tmp_path, keyed)
    monkeypatch.setenv("TRELLIS_DATA_DIR", env)

    registry, warnings = _open(config_dir)

    assert warnings == []
    assert registry.ignored_env_data_dir is None
    assert registry.data_dir_origin == "config.yaml"


def test_a_relative_key_is_compared_where_the_stores_would_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config_dir = _config(tmp_path, "rel-data")
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "rel-data"))
    assert _open(config_dir)[1] == []

    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "elsewhere"))

    assert len(_open(config_dir)[1]) == 1


def test_a_tilde_is_compared_as_the_registry_opens_it_which_is_literally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing that opens the stores expands ``~``, so neither does the comparison.

    ``data_dir: ~/data`` is a directory named ``~`` under the working directory.
    Calling it the home directory's ``data`` would be silent exactly where the
    process writes somewhere the variable did not name.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    config_dir = _config(tmp_path, "~/data")

    monkeypatch.setenv("TRELLIS_DATA_DIR", str(home / "data"))
    registry, warnings = _open(config_dir)
    assert len(warnings) == 1
    assert registry.stores_dir == Path("~/data/stores")

    # Spelled the same on both sides, the literal directory is the same one.
    monkeypatch.setenv("TRELLIS_DATA_DIR", "~/data")
    assert _open(config_dir)[1] == []


# -- advisory: it can never fail the write ----------------------------------


def test_the_warning_cannot_fail_a_registry_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _config(tmp_path, str(tmp_path / "keyed"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "env"))

    def boom(*_args: object, **_kwargs: object) -> None:
        msg = "a failing logger"
        raise RuntimeError(msg)

    monkeypatch.setattr(registry_module.logger, "warning", boom, raising=False)
    registry = StoreRegistry.from_config_dir(config_dir=config_dir)

    assert registry.stores_dir == tmp_path / "keyed" / "stores"
    # The log line failed; the fact it carries did not.
    assert registry.ignored_env_data_dir == tmp_path / "env"


def test_the_comparison_cannot_fail_a_registry_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _config(tmp_path, str(tmp_path / "keyed"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "env"))

    def boom(_path: Path) -> str:
        msg = "an unresolvable path"
        raise ValueError(msg)

    monkeypatch.setattr(registry_module, "_directory_key", boom)
    registry, warnings = _open(config_dir)

    assert warnings == []
    assert registry.stores_dir == tmp_path / "keyed" / "stores"


def test_a_symlink_loop_falls_back_to_the_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    config_dir = _config(tmp_path, str(loop))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "env"))

    registry, warnings = _open(config_dir)

    assert len(warnings) == 1
    assert registry.ignored_env_data_dir == tmp_path / "env"


def test_an_explicit_data_dir_argument_is_not_a_variable_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _config(tmp_path, None)
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "env"))

    with capture_logs() as logs:
        registry = StoreRegistry.from_config_dir(
            config_dir=config_dir, data_dir=tmp_path / "given"
        )

    assert [e for e in logs if e["event"] == _EVENT] == []
    assert registry.data_dir_origin == "argument"
    assert registry.ignored_env_data_dir is None
