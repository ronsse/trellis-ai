"""Tests for ``BackendNotInstalledError`` raises in ``StoreRegistry``.

Covers C2 Phase 2 of the silent-fallback cleanup
(see ``docs/design/plan-cleanup-silent-fallbacks.md``):

* Every ``except ImportError: return None`` site in
  ``src/trellis/stores/registry.py`` now raises
  :class:`BackendNotInstalledError` (or a sibling
  :class:`ConfigError`) naming the missing extra.
* The default-substrate path (SQLite + local blob) keeps working with
  no optional extras installed.
* Installed-but-misconfigured cases raise a *different* error class
  (``ConfigError`` / unknown-provider ``None``) so the operator can
  tell "extra missing" apart from "wrong knob".

All synthetic missing-import scenarios use ``monkeypatch`` to make the
import machinery raise; no extras are actually uninstalled. Tests run
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

from trellis.errors import BackendNotInstalledError, ConfigError
from trellis.stores.registry import (
    StoreRegistry,
    _build_openai_embedding_fn,
    _import_callable,
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


def _block_imports(
    monkeypatch: pytest.MonkeyPatch, blocked_names: set[str]
) -> None:
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
        StoreRegistry.from_config_dir(
            config_dir=config_dir, data_dir=tmp_path / "data"
        )
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
