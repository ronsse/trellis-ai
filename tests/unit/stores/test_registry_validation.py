"""Tests for StoreRegistry.validate — E.3 fail-fast startup gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.stores.registry import RegistryValidationError, StoreRegistry


class TestValidateSuccess:
    """Happy paths: configs that resolve cleanly produce no error."""

    def test_default_sqlite_stack_validates(self, tmp_path: Path) -> None:
        # No explicit config — every store falls back to sqlite (knowledge
        # + operational) or local (blob), all of which resolve against
        # ``stores_dir`` without external dependencies. This is the
        # ``trellis admin init`` happy path.
        reg = StoreRegistry(stores_dir=tmp_path / "stores")
        reg.validate()
        # Every store_type ended up cached — proves we actually
        # instantiated each, not just walked the keys.
        from trellis.stores.registry import _PLANE_OF

        assert set(reg._cache.keys()) == set(_PLANE_OF.keys())

    def test_validate_caches_for_subsequent_access(self, tmp_path: Path) -> None:
        # Validation must warm the cache so production access doesn't
        # pay re-instantiation cost on the first request.
        reg = StoreRegistry(stores_dir=tmp_path / "stores")
        reg.validate()
        graph_first = reg.knowledge.graph_store
        graph_second = reg.knowledge.graph_store
        assert graph_first is graph_second  # cache hit

    def test_validate_subset(self, tmp_path: Path) -> None:
        # Only validate the subset the caller cares about — useful for
        # CLI subcommands that don't touch every store.
        reg = StoreRegistry(stores_dir=tmp_path / "stores")
        reg.validate(store_types=["graph", "trace"])
        assert set(reg._cache.keys()) == {"graph", "trace"}


class TestValidateFailure:
    """Misconfigurations must surface as ``RegistryValidationError``."""

    def test_missing_dsn_raises_aggregate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Postgres store_type with no DSN — neither in config nor env —
        # is a textbook fail-fast scenario. The exact exception type
        # depends on whether psycopg is installed (a missing driver
        # would raise ``ModuleNotFoundError`` first), but either way
        # validation must catch it before request-time.
        monkeypatch.delenv("TRELLIS_PG_DSN", raising=False)
        monkeypatch.delenv("TRELLIS_KNOWLEDGE_PG_DSN", raising=False)
        monkeypatch.delenv("TRELLIS_OPERATIONAL_PG_DSN", raising=False)

        config = {
            "graph": {"backend": "postgres"},
            "trace": {"backend": "postgres"},
        }
        reg = StoreRegistry(config=config, stores_dir=tmp_path / "stores")
        with pytest.raises(RegistryValidationError) as excinfo:
            reg.validate(store_types=["graph", "trace"])

        # Both stores must report — single failure isn't acceptable
        # because an operator would otherwise fix one and re-deploy
        # only to hit the next. Multi-error semantics are the value-add.
        store_types = [name for name, _ in excinfo.value.errors]
        assert set(store_types) == {"graph", "trace"}

        # Rendered message lists both for direct shell display.
        rendered = str(excinfo.value)
        assert "graph:" in rendered
        assert "trace:" in rendered

    def test_unknown_backend_raises(self, tmp_path: Path) -> None:
        config = {"graph": {"backend": "made_up_backend"}}
        reg = StoreRegistry(config=config, stores_dir=tmp_path / "stores")
        with pytest.raises(RegistryValidationError) as excinfo:
            reg.validate(store_types=["graph"])
        ((store_type, exc),) = excinfo.value.errors
        assert store_type == "graph"
        assert "Unknown backend" in str(exc)

    def test_missing_s3_bucket_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRELLIS_S3_BUCKET", raising=False)
        config = {"blob": {"backend": "s3"}}
        reg = StoreRegistry(config=config, stores_dir=tmp_path / "stores")
        with pytest.raises(RegistryValidationError) as excinfo:
            reg.validate(store_types=["blob"])
        ((_, exc),) = excinfo.value.errors
        assert "TRELLIS_S3_BUCKET" in str(exc)

    def test_partial_failure_reports_only_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The graph entry is broken; trace is fine. The aggregate must
        # carry only the broken one — no false-positive failures from
        # healthy stores leaking into the report.
        monkeypatch.delenv("TRELLIS_PG_DSN", raising=False)
        monkeypatch.delenv("TRELLIS_KNOWLEDGE_PG_DSN", raising=False)

        config = {"graph": {"backend": "postgres"}}
        reg = StoreRegistry(config=config, stores_dir=tmp_path / "stores")
        with pytest.raises(RegistryValidationError) as excinfo:
            reg.validate(store_types=["graph", "trace"])
        assert [name for name, _ in excinfo.value.errors] == ["graph"]
        # The healthy store is still cached — partial validation isn't
        # all-or-nothing.
        assert "trace" in reg._cache


class TestRegistryValidationErrorRendering:
    def test_aggregate_message_includes_count_and_each_error(self) -> None:
        err = RegistryValidationError(
            [
                ("graph", ValueError("dsn missing")),
                ("blob", RuntimeError("bucket unset")),
            ]
        )
        rendered = str(err)
        assert "validation failed for 2 store(s)" in rendered
        assert "graph: ValueError: dsn missing" in rendered
        assert "blob: RuntimeError: bucket unset" in rendered

    def test_errors_attribute_preserves_pairs(self) -> None:
        # Programmatic consumers (a wrapping CLI command, a structured
        # health endpoint) need the (store_type, exception) pairs, not
        # just the rendered string.
        original = [("graph", ValueError("x"))]
        err = RegistryValidationError(original)
        assert err.errors == original
