"""Tests for LLM / embedder client construction in ``StoreRegistry``.

These tests exercise :meth:`StoreRegistry.build_llm_client` and
:meth:`StoreRegistry.build_embedder_client` against the ``llm:`` block of
``config.yaml``. All failure modes must return ``None`` and log at
``debug`` — never raise — per the 8A brief.
"""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any

import pytest

from trellis.stores.registry import (
    StoreRegistry,
    _mask_api_key,
    _resolve_api_key,
)

# -- helpers ---------------------------------------------------------------


def _write_config(tmp_path: Path, llm_block: dict[str, Any] | None) -> Path:
    """Write a minimal ``config.yaml`` with the given ``llm:`` block."""
    import yaml

    data: dict[str, Any] = {"stores": {}}
    if llm_block is not None:
        data["llm"] = llm_block
    config_dir = tmp_path / ".trellis"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(yaml.safe_dump(data))
    return config_dir


# -- _mask_api_key ---------------------------------------------------------


def test_mask_api_key_masks_long_keys() -> None:
    assert _mask_api_key("sk-abcdefghX4F9") == "***X4F9"


def test_mask_api_key_short_key() -> None:
    assert _mask_api_key("abcd") == "***"


def test_mask_api_key_none() -> None:
    assert _mask_api_key(None) == "<none>"
    assert _mask_api_key("") == "<none>"


# -- _resolve_api_key ------------------------------------------------------


def test_resolve_api_key_prefers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_KEY_VAR", "sk-from-env-1234")
    cfg = {"api_key_env": "TEST_KEY_VAR", "api_key": "sk-literal-ignored"}
    assert _resolve_api_key(cfg) == "sk-from-env-1234"


def test_resolve_api_key_literal_fallback_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_KEY_VAR", raising=False)
    cfg = {"api_key_env": "TEST_KEY_VAR", "api_key": "sk-literal-1234"}
    assert _resolve_api_key(cfg) == "sk-literal-1234"


def test_resolve_api_key_literal_only() -> None:
    assert _resolve_api_key({"api_key": "sk-literal-1234"}) == "sk-literal-1234"


def test_resolve_api_key_none_when_neither_present() -> None:
    assert _resolve_api_key({}) is None


def test_resolve_api_key_env_unset_no_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_VAR", raising=False)
    assert _resolve_api_key({"api_key_env": "MISSING_VAR"}) is None


# -- build_llm_client ------------------------------------------------------


def test_build_llm_client_openai_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenAI provider via ``api_key_env`` returns an ``OpenAIClient``."""
    pytest.importorskip("openai")  # optional extra; skip when unavailable
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-TEST-ABCD")
    config_dir = _write_config(
        tmp_path,
        {
            "provider": "openai",
            "api_key_env": "OPENAI_API_KEY",
            "model": "gpt-4o-mini",
        },
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )

    client = registry.build_llm_client()
    assert client is not None
    # Avoid importing the provider module at collection time — it's fine
    # here because the happy path requires the SDK to be installed.
    from trellis.llm.providers.openai import OpenAIClient

    assert isinstance(client, OpenAIClient)


def test_build_llm_client_anthropic_literal_key(
    tmp_path: Path,
) -> None:
    """Anthropic provider via literal ``api_key`` returns an ``AnthropicClient``."""
    pytest.importorskip("anthropic")  # optional extra; skip when unavailable
    config_dir = _write_config(
        tmp_path,
        {
            "provider": "anthropic",
            "api_key": "sk-ant-literal-XYZ9",
            "model": "claude-haiku-4-5-20251001",
        },
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )

    client = registry.build_llm_client()
    assert client is not None
    from trellis.llm.providers.anthropic import AnthropicClient

    assert isinstance(client, AnthropicClient)


def test_build_llm_client_no_config_file(tmp_path: Path) -> None:
    """Registry with no config file at all returns None."""
    registry = StoreRegistry.from_config_dir(
        config_dir=tmp_path / "does-not-exist",
        data_dir=tmp_path / "data",
    )
    assert registry.build_llm_client() is None


def test_build_llm_client_missing_llm_block(tmp_path: Path) -> None:
    """Config with no ``llm:`` block returns None."""
    config_dir = _write_config(tmp_path, None)
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    assert registry.build_llm_client() is None


def test_build_llm_client_unknown_provider(tmp_path: Path) -> None:
    """Unknown provider string returns None."""
    config_dir = _write_config(
        tmp_path,
        {"provider": "bogus-provider", "api_key": "sk-1234"},
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    assert registry.build_llm_client() is None


def test_build_llm_client_provider_missing(tmp_path: Path) -> None:
    """``llm:`` block missing the ``provider`` key returns None."""
    config_dir = _write_config(
        tmp_path,
        {"api_key": "sk-1234", "model": "gpt-4o-mini"},
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    assert registry.build_llm_client() is None


def test_build_llm_client_no_api_key(tmp_path: Path) -> None:
    """Neither ``api_key`` nor ``api_key_env`` present returns None."""
    config_dir = _write_config(tmp_path, {"provider": "openai", "model": "gpt-4o-mini"})
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    assert registry.build_llm_client() is None


def test_build_llm_client_env_var_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``api_key_env`` naming an unset env var returns None."""
    monkeypatch.delenv("UNSET_OPENAI_KEY_XYZ", raising=False)
    config_dir = _write_config(
        tmp_path,
        {
            "provider": "openai",
            "api_key_env": "UNSET_OPENAI_KEY_XYZ",
            "model": "gpt-4o-mini",
        },
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    assert registry.build_llm_client() is None


def test_build_llm_client_sdk_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulated missing SDK — ``build_llm_client`` returns None, no raise."""
    # Pretend both the provider module and its underlying SDK are unavailable.
    real_import = builtins.__import__

    def fake_import(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "openai" or name.startswith("trellis.llm.providers.openai"):
            msg = f"No module named {name!r}"
            raise ModuleNotFoundError(msg)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    config_dir = _write_config(
        tmp_path,
        {
            "provider": "openai",
            "api_key": "sk-whatever-1234",
            "model": "gpt-4o-mini",
        },
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    assert registry.build_llm_client() is None


# -- build_embedder_client -------------------------------------------------


def test_build_embedder_client_from_sub_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit ``embedding:`` sub-block builds an ``OpenAIEmbedder``."""
    pytest.importorskip("openai")  # optional extra; skip when unavailable
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-TEST-EMB0")
    config_dir = _write_config(
        tmp_path,
        {
            "provider": "anthropic",
            "api_key": "sk-ant-parent-0000",
            "model": "claude-haiku-4-5-20251001",
            "embedding": {
                "provider": "openai",
                "api_key_env": "OPENAI_API_KEY",
                "model": "text-embedding-3-small",
            },
        },
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )

    embedder = registry.build_embedder_client()
    assert embedder is not None
    from trellis.llm.providers.openai import OpenAIEmbedder

    assert isinstance(embedder, OpenAIEmbedder)


def test_build_embedder_client_inherits_from_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing ``embedding:`` sub-block inherits provider + key from parent."""
    pytest.importorskip("openai")  # optional extra; skip when unavailable
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-TEST-INHE")
    config_dir = _write_config(
        tmp_path,
        {
            "provider": "openai",
            "api_key_env": "OPENAI_API_KEY",
            "model": "gpt-4o-mini",
        },
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )

    embedder = registry.build_embedder_client()
    assert embedder is not None
    from trellis.llm.providers.openai import OpenAIEmbedder

    assert isinstance(embedder, OpenAIEmbedder)


def test_build_embedder_client_no_llm_block(tmp_path: Path) -> None:
    """No ``llm:`` block at all returns None."""
    config_dir = _write_config(tmp_path, None)
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    assert registry.build_embedder_client() is None


def test_build_embedder_client_unknown_provider(tmp_path: Path) -> None:
    """Unknown embedder provider returns None (e.g. Anthropic-only parent)."""
    config_dir = _write_config(
        tmp_path,
        {
            "provider": "anthropic",
            "api_key": "sk-ant-literal-0001",
        },
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    # Anthropic has no first-party embedder; returns None per ADR §2.2.
    assert registry.build_embedder_client() is None
