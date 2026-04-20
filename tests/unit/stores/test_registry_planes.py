"""Tests for plane namespaces, plane-split config, and DSN resolution.

Covers Phase 2 of ADR planes-and-substrates: namespace objects
(``registry.knowledge`` / ``registry.operational``), config parsing
(both plane-split and legacy flat shapes), deprecation warnings on
flat properties, and the plane-aware Postgres DSN resolver.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pytest
import yaml

from trellis.stores.registry import (
    _PLANE_OF,
    StoreRegistry,
    _extract_store_config,
    _reset_deprecation_guards,
    _resolve_plane_pg_dsn,
)


@pytest.fixture(autouse=True)
def _reset_guards() -> None:
    """Ensure each test starts with fresh deprecation guards."""
    _reset_deprecation_guards()


# -- Plane taxonomy --------------------------------------------------------


def test_plane_of_covers_all_known_stores() -> None:
    assert _PLANE_OF == {
        "graph": "knowledge",
        "vector": "knowledge",
        "document": "knowledge",
        "blob": "knowledge",
        "trace": "operational",
        "event_log": "operational",
        "outcome": "operational",
        "parameter": "operational",
        "tuner_state": "operational",
    }


# -- Namespace objects ----------------------------------------------------


def test_registry_exposes_plane_namespaces() -> None:
    registry = StoreRegistry()
    assert type(registry.knowledge).__name__ == "_KnowledgePlane"
    assert type(registry.operational).__name__ == "_OperationalPlane"


def test_knowledge_namespace_resolves_to_same_instance(tmp_path: Path) -> None:
    """Namespace access and flat-alias access must hit the same cached instance."""
    registry = StoreRegistry(stores_dir=tmp_path / "stores")
    via_namespace = registry.knowledge.graph_store
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        via_flat = registry.graph_store
    assert via_namespace is via_flat


def test_operational_namespace_has_expected_properties(tmp_path: Path) -> None:
    registry = StoreRegistry(stores_dir=tmp_path / "stores")
    plane = registry.operational
    # Both stores instantiate lazily and are typed per ABC
    from trellis.stores.base import EventLog, TraceStore

    assert isinstance(plane.trace_store, TraceStore)
    assert isinstance(plane.event_log, EventLog)


def test_knowledge_namespace_has_expected_properties(tmp_path: Path) -> None:
    pytest.importorskip("numpy")  # SQLiteVectorStore requires [vectors]
    registry = StoreRegistry(stores_dir=tmp_path / "stores")
    plane = registry.knowledge
    from trellis.stores.base import (
        BlobStore,
        DocumentStore,
        GraphStore,
        VectorStore,
    )

    assert isinstance(plane.graph_store, GraphStore)
    assert isinstance(plane.vector_store, VectorStore)
    assert isinstance(plane.document_store, DocumentStore)
    assert isinstance(plane.blob_store, BlobStore)


# -- Flat property deprecation -------------------------------------------


def test_flat_property_emits_deprecation_warning(tmp_path: Path) -> None:
    registry = StoreRegistry(stores_dir=tmp_path / "stores")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        _ = registry.graph_store
    matches = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(matches) == 1
    assert "StoreRegistry.graph_store" in str(matches[0].message)
    assert "registry.knowledge.graph_store" in str(matches[0].message)


def test_flat_property_warning_is_one_shot(tmp_path: Path) -> None:
    registry = StoreRegistry(stores_dir=tmp_path / "stores")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        _ = registry.graph_store
        _ = registry.graph_store
        _ = registry.graph_store
    matches = [
        w
        for w in caught
        if issubclass(w.category, DeprecationWarning)
        and "graph_store" in str(w.message)
    ]
    assert len(matches) == 1  # only first access warns


def test_event_log_flat_property_names_its_plane(tmp_path: Path) -> None:
    registry = StoreRegistry(stores_dir=tmp_path / "stores")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        _ = registry.event_log
    messages = [str(w.message) for w in caught]
    assert any("registry.operational.event_log" in m for m in messages)


def test_all_six_flat_properties_have_deprecation(tmp_path: Path) -> None:
    """Every legacy flat property must emit exactly one DeprecationWarning."""
    pytest.importorskip("numpy")  # iterating all six hits SQLiteVectorStore
    registry = StoreRegistry(stores_dir=tmp_path / "stores")
    names = [
        "graph_store",
        "vector_store",
        "document_store",
        "blob_store",
        "trace_store",
        "event_log",
    ]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        for name in names:
            getattr(registry, name)
    dep_messages = [
        str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)
    ]
    for name in names:
        assert any(f"StoreRegistry.{name}" in m for m in dep_messages), (
            f"missing warning for {name}"
        )


# -- Config extraction: plane-split -------------------------------------


def test_extract_config_plane_split_shape() -> None:
    data: dict[str, Any] = {
        "knowledge": {
            "graph": {"backend": "sqlite"},
            "vector": {"backend": "sqlite"},
            "document": {"backend": "sqlite"},
            "blob": {"backend": "local"},
        },
        "operational": {
            "trace": {"backend": "sqlite"},
            "event_log": {"backend": "sqlite"},
        },
    }
    result = _extract_store_config(data, "<test>")
    assert set(result.keys()) == {
        "graph",
        "vector",
        "document",
        "blob",
        "trace",
        "event_log",
    }
    assert result["graph"] == {"backend": "sqlite"}


def test_extract_config_flat_shape_still_works() -> None:
    data: dict[str, Any] = {
        "stores": {
            "graph": {"backend": "sqlite"},
            "trace": {"backend": "sqlite"},
        }
    }
    result = _extract_store_config(data, "<test>")
    assert result == {
        "graph": {"backend": "sqlite"},
        "trace": {"backend": "sqlite"},
    }


def test_extract_config_rejects_store_in_wrong_plane() -> None:
    """A ``trace`` key under ``knowledge:`` should be dropped (with warning)."""
    data: dict[str, Any] = {
        "knowledge": {
            "graph": {"backend": "sqlite"},
            "trace": {"backend": "sqlite"},  # wrong plane
        }
    }
    result = _extract_store_config(data, "<test>")
    assert "graph" in result
    assert "trace" not in result


def test_extract_config_ignores_unknown_store_type() -> None:
    data: dict[str, Any] = {
        "knowledge": {
            "graph": {"backend": "sqlite"},
            "nonsense": {"backend": "whatever"},
        }
    }
    result = _extract_store_config(data, "<test>")
    assert "graph" in result
    assert "nonsense" not in result


def test_extract_config_planes_win_over_flat() -> None:
    data: dict[str, Any] = {
        "knowledge": {"graph": {"backend": "postgres"}},
        "stores": {"graph": {"backend": "sqlite"}},
    }
    result = _extract_store_config(data, "<test>")
    assert result["graph"] == {"backend": "postgres"}


def test_extract_config_empty_returns_empty() -> None:
    assert _extract_store_config({}, "<test>") == {}


# -- DSN resolution ------------------------------------------------------


def test_resolve_pg_dsn_uses_plane_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRELLIS_KNOWLEDGE_PG_DSN", "postgres://k-host/db")
    monkeypatch.delenv("TRELLIS_OPERATIONAL_PG_DSN", raising=False)
    monkeypatch.delenv("TRELLIS_PG_DSN", raising=False)
    assert _resolve_plane_pg_dsn("graph") == "postgres://k-host/db"


def test_resolve_pg_dsn_operational_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRELLIS_OPERATIONAL_PG_DSN", "postgres://o-host/db")
    monkeypatch.delenv("TRELLIS_KNOWLEDGE_PG_DSN", raising=False)
    monkeypatch.delenv("TRELLIS_PG_DSN", raising=False)
    assert _resolve_plane_pg_dsn("trace") == "postgres://o-host/db"


def test_resolve_pg_dsn_plane_env_wins_over_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRELLIS_KNOWLEDGE_PG_DSN", "postgres://new/db")
    monkeypatch.setenv("TRELLIS_PG_DSN", "postgres://legacy/db")
    assert _resolve_plane_pg_dsn("graph") == "postgres://new/db"


def test_resolve_pg_dsn_falls_back_to_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRELLIS_KNOWLEDGE_PG_DSN", raising=False)
    monkeypatch.delenv("TRELLIS_OPERATIONAL_PG_DSN", raising=False)
    monkeypatch.setenv("TRELLIS_PG_DSN", "postgres://legacy/db")
    assert _resolve_plane_pg_dsn("graph") == "postgres://legacy/db"
    # Operational plane also falls back
    assert _resolve_plane_pg_dsn("trace") == "postgres://legacy/db"


def test_resolve_pg_dsn_returns_none_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRELLIS_KNOWLEDGE_PG_DSN", raising=False)
    monkeypatch.delenv("TRELLIS_OPERATIONAL_PG_DSN", raising=False)
    monkeypatch.delenv("TRELLIS_PG_DSN", raising=False)
    assert _resolve_plane_pg_dsn("graph") is None


# -- from_config_dir integration -----------------------------------------


def test_from_config_dir_reads_plane_split(tmp_path: Path) -> None:
    config_dir = tmp_path / ".trellis"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "knowledge": {"graph": {"backend": "sqlite"}},
                "operational": {"trace": {"backend": "sqlite"}},
            }
        )
    )
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    # Internal flat representation preserved for backward-compat in _resolve_backend
    assert "graph" in registry._config
    assert "trace" in registry._config


def test_from_config_dir_accepts_flat_shape_with_deprecation(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".trellis"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({"stores": {"graph": {"backend": "sqlite"}}})
    )
    # Should not raise; flat shape still accepted
    registry = StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )
    assert "graph" in registry._config
