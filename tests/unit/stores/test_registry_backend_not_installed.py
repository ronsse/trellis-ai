"""Tests for ``BackendNotInstalledError`` raises in ``StoreRegistry``.

Covers C2 Phase 2 of the silent-fallback cleanup
(see ``docs/design/plan-cleanup-silent-fallbacks.md``):

* Every ``except ImportError: return None`` site in
  ``src/trellis/stores/registry.py`` now raises
  :class:`BackendNotInstalledError` (or a sibling
  :class:`ConfigError`) naming the missing extra.
* ``StoreRegistry._instantiate`` raises it too, when a backend's module,
  its registry preparation or its constructor needs a module that is not
  installed.
* The default-substrate path (SQLite + local blob) keeps working with
  no optional extras installed.
* Installed-but-misconfigured cases raise a *different* error class
  (``ConfigError`` / unknown-provider ``None``) so the operator can
  tell "extra missing" apart from "wrong knob".

All synthetic missing-import scenarios use ``monkeypatch`` to make the
import machinery raise, or to clear the flag a guarded import sets; no
extras are actually uninstalled. Tests run
the same way whether or not ``[llm-openai]``, ``[neo4j]``,
``[arcadedb]`` etc. happen to be present in the test environment.
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.unreadable_paths import (
    UNREADABLE_PATH_IDS,
    UNREADABLE_PATH_SHAPES,
    UnreadablePathShape,
    unreadable,
)
from trellis.errors import BackendNotInstalledError, ConfigError
from trellis.stores.registry import (
    StoreRegistry,
    _build_openai_embedding_fn,
    _import_callable,
    _mask_api_key,
)


def _write_config(
    config_dir: Path,
    *,
    stores: dict[str, Any] | None = None,
    embeddings: dict[str, Any] | None = None,
    llm: dict[str, Any] | None = None,
) -> Path:
    """Write a config.yaml with the supplied blocks."""
    data: dict[str, Any] = {}
    if stores is not None:
        # Use plane-split shape so ``_extract_store_config`` sees it.
        # Callers pass already-classified plane keys.
        data.update(stores)
    if embeddings is not None:
        data["embeddings"] = embeddings
    if llm is not None:
        data["llm"] = llm
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(yaml.safe_dump(data))
    return config_dir


def _block_imports(monkeypatch: pytest.MonkeyPatch, blocked_names: set[str]) -> None:
    """Make ``import`` raise ``ModuleNotFoundError`` for selected modules.

    Matches an exact module name OR any submodule (``blocked.name``,
    ``blocked.name.sub``). All other imports go through normally.

    Also evicts already-cached versions of the blocked modules from
    ``sys.modules`` so that subsequent ``importlib.import_module`` calls
    actually retry the import (cache hits would otherwise bypass the
    ``__import__`` hook and return the real module).
    """
    import sys

    real_import = builtins.__import__

    # Evict cached versions; restore on test teardown via monkeypatch.
    for mod_name in list(sys.modules):
        for blocked in blocked_names:
            if mod_name == blocked or mod_name.startswith(blocked + "."):
                monkeypatch.delitem(sys.modules, mod_name, raising=False)
                break

    def fake_import(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        for blocked in blocked_names:
            if name == blocked or name.startswith(blocked + "."):
                msg = f"No module named {name!r}"
                raise ModuleNotFoundError(msg)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)


# -- BackendNotInstalledError construction --------------------------------


def test_error_message_includes_install_hint_with_extra() -> None:
    """When ``extra=`` is set, the message names the install command."""
    err = BackendNotInstalledError(backend_name="arcadedb", extra="arcadedb")
    msg = str(err)
    assert "arcadedb" in msg
    assert 'uv pip install -e ".[arcadedb]"' in msg
    assert err.backend_name == "arcadedb"
    assert err.extra == "arcadedb"
    assert err.code == "BACKEND_NOT_INSTALLED"


def test_error_message_falls_back_to_package_name() -> None:
    """When no extra exists, fall back to a bare package name hint."""
    err = BackendNotInstalledError(
        backend_name="custom-llm",
        package_name="trellis-plugin-bedrock",
    )
    assert "trellis-plugin-bedrock" in str(err)
    assert err.extra is None


def test_error_message_with_neither_extra_nor_package() -> None:
    """Bare construction still produces an actionable message."""
    err = BackendNotInstalledError(backend_name="unknown")
    assert "unknown" in str(err)
    assert "optional dependency" in str(err)


def test_error_is_subclass_of_configerror() -> None:
    """Aggregating callers (``RegistryValidationError``) should keep classifying
    it as a config-shaped problem, not a runtime crash."""
    err = BackendNotInstalledError(backend_name="neo4j", extra="neo4j")
    assert isinstance(err, ConfigError)


# -- build_llm_client raises with the right extra -------------------------


@pytest.mark.parametrize(
    ("provider", "blocked_module", "expected_extra"),
    [
        ("openai", "openai", "llm-openai"),
        ("anthropic", "anthropic", "llm-anthropic"),
    ],
)
def test_build_llm_client_missing_sdk_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    blocked_module: str,
    expected_extra: str,
) -> None:
    """``provider: <name>`` without the SDK raises with the install hint.

    Synthetic block: we make both the provider SDK and its Trellis
    wrapper unimportable so the test never depends on which extras are
    actually installed in the test environment.
    """
    _block_imports(
        monkeypatch,
        {blocked_module, f"trellis.llm.providers.{provider}"},
    )
    config_dir = _write_config(
        tmp_path / "cfg",
        llm={
            "provider": provider,
            "api_key": "sk-test-1234",
            "model": "ignored-here",
        },
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    with pytest.raises(BackendNotInstalledError) as exc_info:
        registry.build_llm_client()
    assert exc_info.value.backend_name == provider
    assert exc_info.value.extra == expected_extra
    assert expected_extra in str(exc_info.value)


def test_build_embedder_client_missing_sdk_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``provider: openai`` without the SDK raises in the embedder path too."""
    _block_imports(monkeypatch, {"openai", "trellis.llm.providers.openai"})
    config_dir = _write_config(
        tmp_path / "cfg",
        llm={
            "provider": "openai",
            "api_key": "sk-test-1234",
            "embedding": {
                "provider": "openai",
                "api_key": "sk-test-1234",
                "model": "text-embedding-3-small",
            },
        },
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    with pytest.raises(BackendNotInstalledError) as exc_info:
        registry.build_embedder_client()
    assert exc_info.value.backend_name == "openai"
    assert exc_info.value.extra == "llm-openai"


def test_build_llm_client_unknown_provider_still_returns_none(
    tmp_path: Path,
) -> None:
    """Unknown provider remains a soft "not configured" return.

    Distinguish "operator named a provider we don't ship" (soft None,
    matches existing behaviour) from "operator named a provider we ship
    but the extra isn't installed" (loud ``BackendNotInstalledError``).
    """
    config_dir = _write_config(
        tmp_path / "cfg",
        llm={"provider": "bogus-provider", "api_key": "sk-1234"},
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    assert registry.build_llm_client() is None


# -- _build_openai_embedding_fn raises ------------------------------------


def test_openai_embedding_fn_missing_sdk_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``embeddings: provider: openai`` without the SDK raises."""
    _block_imports(monkeypatch, {"openai"})
    with pytest.raises(BackendNotInstalledError) as exc_info:
        _build_openai_embedding_fn({"model": "text-embedding-3-small"})
    assert exc_info.value.backend_name == "openai-embeddings"
    assert exc_info.value.extra == "llm-openai"


def test_embedding_fn_property_propagates_backend_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cached ``embedding_fn`` property raises rather than returning None.

    Previous behaviour: missing SDK silently flipped
    ``embeddings: provider: openai`` to a no-embedding configuration.
    """
    _block_imports(monkeypatch, {"openai"})
    config_dir = _write_config(
        tmp_path / "cfg",
        embeddings={"provider": "openai"},
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    with pytest.raises(BackendNotInstalledError):
        _ = registry.embedding_fn


# -- _build_openai_embedding_fn raises on a missing key (#779 F-a) ---------


def test_openai_embedding_fn_missing_key_raises_configerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key anywhere: the SDK's own ``OpenAIError`` becomes a ``ConfigError``."""
    openai = pytest.importorskip("openai")  # optional extra; skip when unavailable
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ConfigError) as exc_info:
        _build_openai_embedding_fn({"model": "text-embedding-3-small"})
    assert exc_info.value.setting == "embeddings.api_key_env"
    assert "OPENAI_API_KEY" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, openai.OpenAIError)
    # The SDK's own wording, whatever its version, stays out of the message.
    assert str(exc_info.value.__cause__) not in str(exc_info.value)


def test_openai_embedding_fn_sdk_env_fallback_builds_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OPENAI_API_KEY`` alone, with no key in config, still builds the callable."""
    pytest.importorskip("openai")  # optional extra; skip when unavailable
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-obviously-fake-0000")
    embed = _build_openai_embedding_fn({"model": "text-embedding-3-small"})
    assert callable(embed)


def test_openai_embed_network_error_survives_as_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``OpenAIError`` from the embeddings call itself is not a config error."""
    pytest.importorskip("openai")  # optional extra; skip when unavailable
    import openai

    monkeypatch.setenv("FAKE_OPENAI_KEY_VAR", "sk-test-obviously-fake-0000")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    embed = _build_openai_embedding_fn(
        {"model": "text-embedding-3-small", "api_key_env": "FAKE_OPENAI_KEY_VAR"}
    )

    def _raise_network_error(*args: Any, **kwargs: Any) -> Any:
        synthetic_msg = "synthetic network failure"
        raise openai.OpenAIError(synthetic_msg)

    monkeypatch.setattr(
        openai.resources.embeddings.Embeddings, "create", _raise_network_error
    )
    with pytest.raises(openai.OpenAIError) as exc_info:
        embed("synthetic probe text")
    assert not isinstance(exc_info.value, ConfigError)


def test_openai_constructor_non_openai_error_propagates_unwrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-``OpenAIError`` from ``openai.OpenAI(...)`` propagates unwrapped.

    Only ``openai.OpenAIError`` becomes a ``ConfigError`` (#786); any other
    exception reaches the ``embedding_fn`` caller as itself. The constructor is
    replaced, so the result does not depend on which SDK version is installed.
    """
    openai = pytest.importorskip("openai")  # optional extra; skip when unavailable
    synthetic_error = RuntimeError("synthetic")

    def _raise_non_openai_error(**kwargs: Any) -> Any:
        raise synthetic_error

    monkeypatch.setattr(openai, "OpenAI", _raise_non_openai_error)
    config_dir = _write_config(
        tmp_path / "cfg",
        embeddings={"provider": "openai", "api_key": "sk-test-1234"},
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    with pytest.raises(RuntimeError) as exc_info:
        _ = registry.embedding_fn
    assert exc_info.value is synthetic_error


# -- a failed resolution is not cached (#775 follow-up F1) -----------------

_UNIMPORTABLE_PATH = "no_such_module_for_embedding_fn_cache_test.embed"


def test_embedding_fn_retries_a_failed_env_var_until_it_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``TRELLIS_EMBEDDING_FN`` that fails to import raises on every call.

    A raise caches nothing, so once the path imports, the next call
    returns the callable rather than a cached error or "not configured".
    """
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", _UNIMPORTABLE_PATH)
    registry = StoreRegistry.from_config_dir(
        config_dir=tmp_path / "cfg", data_dir=tmp_path / "data"
    )
    with pytest.raises(ConfigError):
        _ = registry.embedding_fn
    with pytest.raises(ConfigError):
        _ = registry.embedding_fn
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", "trellis.stores.registry._mask_api_key")
    assert registry.embedding_fn is _mask_api_key


def test_embedding_fn_raises_again_on_second_call_config_path(
    tmp_path: Path,
) -> None:
    """An unimportable ``embeddings.provider`` raises on every call too."""
    config_dir = _write_config(
        tmp_path / "cfg",
        embeddings={"provider": _UNIMPORTABLE_PATH},
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    with pytest.raises(ConfigError):
        _ = registry.embedding_fn
    with pytest.raises(ConfigError):
        _ = registry.embedding_fn


# -- env var outranks config when both are set ----------------------------


@pytest.mark.parametrize(
    "config_provider",
    [
        pytest.param(
            "trellis.stores.registry._import_callable",
            id="config_resolves_to_a_different_callable",
        ),
        pytest.param(_UNIMPORTABLE_PATH, id="config_path_does_not_import"),
        pytest.param("openai", id="config_provider_is_openai"),
    ],
)
def test_embedding_fn_env_var_wins_when_config_also_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_provider: str
) -> None:
    """``TRELLIS_EMBEDDING_FN`` outranks ``embeddings.provider``.

    Config is not resolved while the env var is set, so the env callable
    comes back whether the provider names another callable, a path that
    cannot import, or ``openai``.
    """
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", "trellis.stores.registry._mask_api_key")
    config_dir = _write_config(
        tmp_path / "cfg",
        embeddings={"provider": config_provider},
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    assert registry.embedding_fn is _mask_api_key


def test_embedding_fn_unimportable_env_var_raises_rather_than_using_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``TRELLIS_EMBEDDING_FN`` that cannot import raises; config is no fallback."""
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", _UNIMPORTABLE_PATH)
    config_dir = _write_config(
        tmp_path / "cfg",
        embeddings={"provider": "trellis.stores.registry._mask_api_key"},
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    with pytest.raises(ConfigError, match=_UNIMPORTABLE_PATH):
        _ = registry.embedding_fn


# -- F2: ConfigError's setting names the source that was wrong ------------
#
# (#794 gate follow-up.) A bad dotted path raises ConfigError either way,
# but the *setting* it names must point at whichever of the env var or the
# YAML key actually supplied the path.

_BAD_DOTTED_PATHS = [
    pytest.param(
        "no_such_module_for_embedding_fn_setting_test.embed", id="missing_module"
    ),
    pytest.param(
        "trellis.errors.no_such_attr_for_embedding_fn_setting_test",
        id="missing_attribute",
    ),
    pytest.param("trellis.errors.__doc__", id="not_callable"),
]


@pytest.mark.parametrize("bad_path", _BAD_DOTTED_PATHS)
def test_embedding_fn_env_var_bad_path_setting_names_the_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_path: str
) -> None:
    """A bad ``TRELLIS_EMBEDDING_FN`` path's ConfigError names the env var.

    Not ``embeddings.provider``: that YAML key was never read, so pointing
    an operator there sends them to edit the wrong setting.
    """
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", bad_path)
    registry = StoreRegistry.from_config_dir(
        config_dir=tmp_path / "cfg", data_dir=tmp_path / "data"
    )
    with pytest.raises(ConfigError) as exc_info:
        _ = registry.embedding_fn
    assert exc_info.value.setting == "TRELLIS_EMBEDDING_FN"


@pytest.mark.parametrize("bad_path", _BAD_DOTTED_PATHS)
def test_embedding_fn_config_provider_bad_path_setting_names_the_yaml_key(
    tmp_path: Path, bad_path: str
) -> None:
    """The same bad path via ``embeddings.provider`` keeps naming the YAML key."""
    config_dir = _write_config(tmp_path / "cfg", embeddings={"provider": bad_path})
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    with pytest.raises(ConfigError) as exc_info:
        _ = registry.embedding_fn
    assert exc_info.value.setting == "embeddings.provider"


def test_import_callable_setting_kwarg_overrides_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_import_callable``'s own ``setting=`` parameter is honoured directly."""
    with pytest.raises(ConfigError) as exc_info:
        _import_callable(_UNIMPORTABLE_PATH, setting="TRELLIS_EMBEDDING_FN")
    assert exc_info.value.setting == "TRELLIS_EMBEDDING_FN"


# -- F1: a dotted path's own import-time failure is not a bad path --------


@pytest.mark.parametrize("via_env", [True, False], ids=["env_var", "config_provider"])
def test_embedding_fn_dotted_path_import_time_error_propagates_unwrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, via_env: bool
) -> None:
    """A dotted-path module whose own top-level code raises is not a bad path.

    ``_import_callable`` catches only ``ImportError``; an unrelated exception
    raised while the target module executes (not a missing module, not a
    missing or non-callable attribute) is that module's bug and propagates
    as itself — the dotted-path counterpart of #794's
    ``test_openai_constructor_non_openai_error_propagates_unwrapped``.
    """
    monkeypatch.syspath_prepend(str(tmp_path))
    module_name = "efp_raises_at_import_env" if via_env else "efp_raises_at_import_cfg"
    (tmp_path / f"{module_name}.py").write_text(
        "raise RuntimeError('synthetic import-time failure')\n"
    )
    dotted_path = f"{module_name}.target"
    if via_env:
        monkeypatch.setenv("TRELLIS_EMBEDDING_FN", dotted_path)
        config_dir = tmp_path / "cfg"
    else:
        config_dir = _write_config(
            tmp_path / "cfg", embeddings={"provider": dotted_path}
        )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    # `pytest.raises(RuntimeError, ...)` alone proves "unwrapped": a
    # `ConfigError` (TrellisError, not a RuntimeError) raised instead would
    # already fail this context manager, so a further `isinstance` assertion
    # here could never fail (#794 gate finding #5 flagged the same pattern).
    with pytest.raises(RuntimeError, match="synthetic import-time failure"):
        _ = registry.embedding_fn


# -- F3: an empty TRELLIS_EMBEDDING_FN is not "set" ------------------------


def test_embedding_fn_empty_env_var_falls_through_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``TRELLIS_EMBEDDING_FN=""`` is falsy, so config still resolves.

    An env file that declares the var but leaves it blank should not be
    treated as "set to an empty, malformed path" — ``if custom_path:``
    falls through to ``embeddings.provider`` instead (#794 gate M4).
    """
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", "")
    config_dir = _write_config(
        tmp_path / "cfg",
        embeddings={"provider": "trellis.stores.registry._mask_api_key"},
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    assert registry.embedding_fn is _mask_api_key


def test_embedding_fn_not_configured_is_still_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``None`` for "not configured" is cached, not re-resolved per call.

    An embedder configured after the first ``None`` answer does not change
    the second answer.
    """
    registry = StoreRegistry.from_config_dir(
        config_dir=tmp_path / "cfg", data_dir=tmp_path / "data"
    )
    assert registry.embedding_fn is None
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", "trellis.stores.registry._mask_api_key")
    assert registry.embedding_fn is None


def test_embedding_fn_success_is_resolved_once_across_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful resolution is cached: the callable is built once.

    Counts calls into ``_import_callable`` rather than asserting on
    identity alone, so the test fails if a future change re-resolves on
    every access even when the returned object happens to be memoized
    some other way.
    """
    calls: list[str] = []
    real_import_callable = _import_callable

    def counting_import_callable(dotted_path: str, **kwargs: Any) -> Any:
        calls.append(dotted_path)
        return real_import_callable(dotted_path, **kwargs)

    monkeypatch.setattr(
        "trellis.stores.registry._import_callable", counting_import_callable
    )
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", "trellis.stores.registry._mask_api_key")
    registry = StoreRegistry.from_config_dir(
        config_dir=tmp_path / "cfg", data_dir=tmp_path / "data"
    )
    first = registry.embedding_fn
    second = registry.embedding_fn
    assert first is second
    assert calls == ["trellis.stores.registry._mask_api_key"]


# -- _import_callable raises ----------------------------------------------


def test_import_callable_bad_path_raises_configerror() -> None:
    """A path without a dot can't resolve to module + attribute."""
    with pytest.raises(ConfigError) as exc_info:
        _import_callable("nodot")
    assert "Invalid embedding callable path" in str(exc_info.value)


def test_import_callable_missing_module_raises_configerror() -> None:
    """Module that doesn't exist raises ``ConfigError`` with a hint."""
    with pytest.raises(ConfigError) as exc_info:
        _import_callable("no_such_module_xyz.embed")
    assert "is not importable" in str(exc_info.value)


def test_import_callable_missing_attribute_raises_configerror() -> None:
    """Module imports OK but lacks the attribute → ConfigError, not None."""
    with pytest.raises(ConfigError) as exc_info:
        _import_callable("trellis.errors.does_not_exist")
    assert "attribute 'does_not_exist'" in str(exc_info.value)


def test_import_callable_not_callable_raises_configerror() -> None:
    """Attribute exists but is not callable — ``ConfigError`` again."""
    with pytest.raises(ConfigError) as exc_info:
        _import_callable("trellis.errors.__doc__")
    assert "not callable" in str(exc_info.value)


def test_import_callable_happy_path_returns_callable() -> None:
    """Sanity: a valid dotted-path returns the callable, doesn't raise."""

    result = _import_callable("trellis.stores.registry._mask_api_key")
    assert callable(result)


# -- _resolve_substrate_class raises --------------------------------------


def test_resolve_substrate_class_missing_extra_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A graph backend whose substrate module can't import raises loudly."""
    # Block both the parent package and the specific submodule. Earlier
    # tests in the suite cache ``trellis.stores.neo4j`` via the
    # connectivity fixtures; blocking only the leaf module would let the
    # cached parent slip the import through.
    _block_imports(
        monkeypatch,
        {"trellis.stores.neo4j", "trellis.stores.neo4j.graph"},
    )
    config_dir = _write_config(
        tmp_path / "cfg",
        stores={
            "knowledge": {
                "graph": {"backend": "neo4j", "uri": "bolt://localhost:7687"},
            },
        },
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    with pytest.raises(BackendNotInstalledError) as exc_info:
        registry._resolve_substrate_class("graph")
    assert exc_info.value.backend_name == "neo4j"
    assert exc_info.value.extra == "neo4j"


def test_resolve_substrate_class_unknown_backend_still_returns_none(
    tmp_path: Path,
) -> None:
    """Unknown-backend names still soft-return None (no install hint exists)."""
    config_dir = _write_config(
        tmp_path / "cfg",
        stores={
            "knowledge": {
                "graph": {"backend": "made-up-backend"},
            },
        },
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    assert registry._resolve_substrate_class("graph") is None


# -- _instantiate raises --------------------------------------------------


def test_instantiate_missing_module_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backend module whose import needs a missing module raises loudly.

    Pins the import site in ``_instantiate``: ``trellis.stores.postgres``
    imports ``psycopg_pool`` at module level.
    """
    # Block the package too: an earlier test may have cached it, and a
    # cached module would slip the import through.
    _block_imports(monkeypatch, {"psycopg_pool", "psycopg", "trellis.stores.postgres"})
    config_dir = _write_config(
        tmp_path / "cfg",
        stores={
            "knowledge": {
                "document": {
                    "backend": "postgres",
                    "dsn": "postgresql://nobody@127.0.0.1:9/x",
                },
            },
        },
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    with pytest.raises(BackendNotInstalledError) as exc_info:
        _ = registry.knowledge.document_store
    assert exc_info.value.backend_name == "postgres"
    assert exc_info.value.extra == "cloud"
    # The refusal names the extra, not the module that failed; the chained
    # ImportError is what a traceback or an exc_info log line shows.
    assert isinstance(exc_info.value.__cause__, ImportError)


def test_instantiate_missing_neo4j_driver_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registry preparation that needs a missing driver raises loudly.

    Pins the preparation site in ``_instantiate``: the Neo4j store's
    ``prepare_registry_params`` builds the driver before the constructor
    runs.
    """
    # Both halves of a missing driver, so the test behaves the same with
    # or without ``neo4j`` installed: the flag ``check_driver_installed``
    # reads, and the name the guarded ``from neo4j import GraphDatabase``
    # leaves unbound.
    monkeypatch.setattr("trellis.stores.bolt_opencypher.base.HAS_NEO4J", False)
    monkeypatch.delattr("trellis.stores.neo4j.base.GraphDatabase", raising=False)
    config_dir = _write_config(
        tmp_path / "cfg",
        stores={
            "knowledge": {
                # Preparation refuses a missing password before it builds
                # the driver.
                "graph": {
                    "backend": "neo4j",
                    "uri": "bolt://127.0.0.1:9",
                    "password": "unused",
                },
            },
        },
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    with pytest.raises(BackendNotInstalledError) as exc_info:
        _ = registry.knowledge.graph_store
    assert exc_info.value.backend_name == "neo4j"
    assert exc_info.value.extra == "neo4j"
    assert isinstance(exc_info.value.__cause__, ImportError)


@pytest.mark.parametrize(
    "ensure_setting",
    [{}, {"ensure_database_exists": False}],
    ids=["ensure-default", "ensure-off"],
)
def test_arcadedb_preparation_refuses_before_touching_the_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ensure_setting: dict[str, Any],
) -> None:
    """A missing driver is refused before ArcadeDB's preparation calls HTTP.

    Preparation creates the database and migrates its schema over HTTP; with
    ``ensure_database_exists`` off it still migrates the schema. Checking for
    the driver afterwards changed the server first, and with the server down
    it reported a connection error instead of the extra.
    """
    from trellis.stores.arcadedb.graph import ArcadeDBGraphStore

    monkeypatch.setattr("trellis.stores.bolt_opencypher.base.HAS_NEO4J", False)
    calls: list[str] = []
    monkeypatch.setattr(
        "trellis.stores.arcadedb.graph.ensure_database",
        lambda *_args: calls.append("ensure_database"),
    )
    monkeypatch.setattr(
        ArcadeDBGraphStore,
        "_init_arcadedb_edge_provenance_schema",
        classmethod(lambda _cls, **_kwargs: calls.append("migrate_schema")),
    )
    config_dir = _write_config(
        tmp_path / "cfg",
        stores={
            "knowledge": {
                "graph": {
                    "backend": "arcadedb",
                    "uri": "bolt://127.0.0.1:9",
                    "password": "unused",
                    **ensure_setting,
                },
            },
        },
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    with pytest.raises(BackendNotInstalledError) as exc_info:
        _ = registry.knowledge.graph_store
    assert exc_info.value.backend_name == "arcadedb"
    assert exc_info.value.extra == "arcadedb"
    assert isinstance(exc_info.value.__cause__, ImportError)
    assert calls == []


def test_instantiate_constructor_import_error_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store constructor that needs a missing module raises loudly.

    Pins the constructor site in ``_instantiate``: ``S3BlobStore.__init__``
    raises ``ImportError`` when ``boto3`` is missing.
    """
    monkeypatch.setattr("trellis.stores.s3.blob.HAS_BOTO3", False)
    config_dir = _write_config(
        tmp_path / "cfg",
        stores={"knowledge": {"blob": {"backend": "s3", "bucket": "b"}}},
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    with pytest.raises(BackendNotInstalledError) as exc_info:
        _ = registry.knowledge.blob_store
    assert exc_info.value.backend_name == "s3"
    assert exc_info.value.extra == "cloud"
    assert isinstance(exc_info.value.__cause__, ImportError)


# -- _load_fingerprint_meta raises ---------------------------------------


def test_load_fingerprint_meta_corrupt_file_raises(tmp_path: Path) -> None:
    """A corrupt fingerprint meta file raises rather than silently empty-ing."""
    config_dir = tmp_path / "cfg"
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True, exist_ok=True)
    (stores_dir / "_trellis_meta.json").write_text("{ not valid json")
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text("knowledge: {}\n")

    registry = StoreRegistry.from_config_dir(config_dir=config_dir, data_dir=data_dir)
    with pytest.raises(ConfigError) as exc_info:
        registry._load_fingerprint_meta()
    assert "corrupt" in str(exc_info.value).lower()


@pytest.mark.parametrize("shape", UNREADABLE_PATH_SHAPES, ids=UNREADABLE_PATH_IDS)
def test_load_fingerprint_meta_unreadable_path_raises(
    tmp_path: Path, shape: UnreadablePathShape
) -> None:
    """An unreadable *path* must raise like an unreadable *file* (#479).

    The presence guard was ``Path.exists()``, which reports ``ELOOP`` and
    ``ENOTDIR`` as ``False`` — so a broken meta path read as *first boot*,
    the fingerprint map came back empty, and schema-drift detection was
    silently disabled for every store. That is precisely the regression the
    function's own docstring says it raises to prevent, and the same
    laundering ``policy_source`` used to do with access-control policies.
    """
    config_dir = tmp_path / "cfg"
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text("knowledge: {}\n")

    registry = StoreRegistry.from_config_dir(config_dir=config_dir, data_dir=data_dir)

    with (
        unreadable(shape, stores_dir / "_trellis_meta.json"),
        pytest.raises(ConfigError) as exc_info,
    ):
        registry._load_fingerprint_meta()

    assert shape.message_fragment in str(exc_info.value)


@pytest.mark.parametrize("shape", UNREADABLE_PATH_SHAPES, ids=UNREADABLE_PATH_IDS)
def test_unreadable_config_yaml_raises_rather_than_defaulting(
    tmp_path: Path, shape: UnreadablePathShape
) -> None:
    """The eighth site: the *config* presence check, one default further out.

    ``from_config_dir`` gated the whole of ``config.yaml`` on
    ``Path.exists()``, so an ``ELOOP``/``ENOTDIR`` config read as *absent*
    and the registry came up on its default local sqlite backends — a
    deployment that had declared postgres writing to an empty store, with no
    error anywhere. Same laundering as ``_load_fingerprint_meta`` above; the
    ``except OSError`` arm that raises this ``ConfigError`` was already
    written and simply unreachable for the shapes ``exists()`` swallows.
    """
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(
        "knowledge:\n  graph:\n    backend: postgres\n    dsn: postgresql://x/y\n"
    )

    with (
        unreadable(shape, config_dir / "config.yaml"),
        pytest.raises(ConfigError) as exc_info,
    ):
        StoreRegistry.from_config_dir(config_dir=config_dir, data_dir=tmp_path / "data")

    assert shape.message_fragment in str(exc_info.value)
    assert str(config_dir / "config.yaml") in str(exc_info.value)


def test_a_declared_backend_is_what_comes_back_when_the_config_is_readable(
    tmp_path: Path,
) -> None:
    """The control the test above is measured against.

    Without it, "raises on an unreadable config" would be satisfiable by a
    registry that raised on every config — and the silent-default failure it
    replaces is only visible because the *readable* case resolves the
    declared backend rather than the sqlite fallback.
    """
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(
        "knowledge:\n  graph:\n    backend: postgres\n    dsn: postgresql://x/y\n"
    )

    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    backend, _ = registry._resolve_backend("graph")
    assert backend == "postgres"


def test_an_absent_config_yaml_is_still_the_silent_default(tmp_path: Path) -> None:
    """The other control: no config at all must stay a normal, quiet boot."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)

    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    backend, _ = registry._resolve_backend("graph")
    assert backend == "sqlite"


def test_load_fingerprint_meta_absent_file_is_still_first_boot(tmp_path: Path) -> None:
    """The control: no meta file is a normal first boot and must not raise."""
    config_dir = tmp_path / "cfg"
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text("knowledge: {}\n")

    registry = StoreRegistry.from_config_dir(config_dir=config_dir, data_dir=data_dir)
    assert registry._load_fingerprint_meta() == {}


def test_load_fingerprint_meta_valid_returns_dict(tmp_path: Path) -> None:
    """Happy-path read still returns the parsed dict."""
    config_dir = tmp_path / "cfg"
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True, exist_ok=True)
    payload = {"graph": "graph/sqlite/v1"}
    (stores_dir / "_trellis_meta.json").write_text(json.dumps(payload))
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text("knowledge: {}\n")

    registry = StoreRegistry.from_config_dir(config_dir=config_dir, data_dir=data_dir)
    assert registry._load_fingerprint_meta() == payload


# -- from_config_dir surfaces corrupt YAML --------------------------------


def test_from_config_dir_corrupt_yaml_raises(tmp_path: Path) -> None:
    """Corrupt ``config.yaml`` raises ``ConfigError`` (not silent skip)."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text("not: valid: yaml: ::\n  -[ bad\n")
    with pytest.raises(ConfigError) as exc_info:
        StoreRegistry.from_config_dir(config_dir=config_dir, data_dir=tmp_path / "data")
    assert "config.yaml" in str(exc_info.value)


def test_from_config_dir_missing_file_still_works(tmp_path: Path) -> None:
    """A nonexistent config dir is *not* an error — first-boot case."""
    registry = StoreRegistry.from_config_dir(
        config_dir=tmp_path / "does-not-exist", data_dir=tmp_path / "data"
    )
    # Default substrate is reachable: sqlite for everything.
    assert registry is not None


# -- Default-substrate path keeps working with no extras ------------------


def test_default_sqlite_path_works_without_any_extras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite + local blob path doesn't trip ``BackendNotInstalledError``.

    Block every optional extra synthetically — the default substrate
    must not touch any of them.
    """
    _block_imports(
        monkeypatch,
        {
            "openai",
            "anthropic",
            "neo4j",
            "trellis.stores.neo4j",
            "trellis.stores.arcadedb",
            "trellis.stores.postgres",
            "trellis.stores.pgvector",
            "trellis.stores.s3",
            "trellis.llm.providers.openai",
            "trellis.llm.providers.anthropic",
            "psycopg",
            "boto3",
        },
    )
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    # Empty config.yaml ⇒ every store uses the default backend.
    (config_dir / "config.yaml").write_text("\n")

    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    # All default substrates resolve, none raises.
    assert registry.knowledge.graph_store is not None
    assert registry.knowledge.vector_store is not None
    assert registry.knowledge.document_store is not None
    assert registry.knowledge.blob_store is not None
    assert registry.operational.trace_store is not None
    assert registry.operational.event_log is not None


def test_default_path_build_llm_client_returns_none_without_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``llm:`` block + no optional extras ⇒ ``None``, never raise."""
    _block_imports(
        monkeypatch,
        {"openai", "anthropic", "trellis.llm.providers.openai"},
    )
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text("\n")
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    assert registry.build_llm_client() is None
    assert registry.build_embedder_client() is None
    # No embedding configured either: returns None without raising.
    assert registry.embedding_fn is None


# -- Installed-but-misconfigured raises a different error -----------------


def test_installed_provider_with_bad_uri_is_config_error_not_backend_error(
    tmp_path: Path,
) -> None:
    """Wrong-scheme URI on a *successfully imported* backend raises
    plain ``ConfigError``, not ``BackendNotInstalledError`` — the operator
    should see this as "fix the knob", not "install the extra".

    Uses the URI pre-flight check (``_check_uri_formats``) directly so we
    exercise the install-OK-but-config-bad code path regardless of which
    optional extras happen to be installed in the test environment.
    """
    config_dir = _write_config(
        tmp_path / "cfg",
        stores={
            "knowledge": {
                "graph": {
                    "backend": "postgres",
                    "dsn": "not-a-valid-scheme://nope",
                },
            },
        },
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    failures = registry._check_uri_formats(["graph"])
    assert len(failures) == 1
    store_type, exc = failures[0]
    assert store_type == "graph"
    assert isinstance(exc, ConfigError)
    assert not isinstance(exc, BackendNotInstalledError)
    assert "unexpected URL scheme" in str(exc)
