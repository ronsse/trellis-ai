"""Tests for StoreRegistry.validate — E.3 fail-fast startup gate."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from trellis.errors import ConfigError
from trellis.stores import registry as registry_module
from trellis.stores.registry import (
    _FINGERPRINT_META_FILENAME,
    RegistryValidationError,
    StoreRegistry,
    _reset_backend_cache,
)


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
        # would raise ``BackendNotInstalledError`` first), but either way
        # validation must catch it before request-time.
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


class TestTypedExceptionMigration:
    """The ``ValueError`` sites in ``_instantiate`` are now typed errors."""

    def test_unknown_backend_raises_config_error(self, tmp_path: Path) -> None:
        # The "unknown backend" path was a ``ValueError`` before — it's now
        # a ``ConfigError`` with the offending YAML path on ``setting`` so
        # operators can edit the right key.
        config = {"graph": {"backend": "made_up_backend"}}
        reg = StoreRegistry(config=config, stores_dir=tmp_path / "stores")
        with pytest.raises(RegistryValidationError) as excinfo:
            reg.validate(store_types=["graph"])
        ((_, exc),) = excinfo.value.errors
        assert isinstance(exc, ConfigError)
        assert exc.setting == "stores.graph.backend"


class TestUriFormatValidation:
    """Pre-flight ``urlparse`` check on Postgres / Neo4j / S3 URIs."""

    def test_postgres_typo_missing_scheme_aggregates(self, tmp_path: Path) -> None:
        # ``://localhost:5432/db`` (no scheme) is a textbook typo that
        # otherwise bubbles up as a psycopg parser error far from the
        # config file. The pre-flight check catches it as a ConfigError.
        config = {
            "trace": {"backend": "postgres", "dsn": "://localhost:5432/db"},
        }
        reg = StoreRegistry(config=config, stores_dir=tmp_path / "stores")
        with pytest.raises(RegistryValidationError) as excinfo:
            reg.validate(store_types=["trace"])
        cfg_errors = [
            exc for _, exc in excinfo.value.errors if isinstance(exc, ConfigError)
        ]
        assert any("empty URL scheme" in str(exc) for exc in cfg_errors)

    def test_postgres_empty_netloc_aggregates(self, tmp_path: Path) -> None:
        # ``postgres:///dbname`` parses with a scheme but no netloc — a
        # subtle malformed-URI mistake that the pre-flight catches.
        config = {
            "trace": {"backend": "postgres", "dsn": "postgres:///dbname"},
        }
        reg = StoreRegistry(config=config, stores_dir=tmp_path / "stores")
        with pytest.raises(RegistryValidationError) as excinfo:
            reg.validate(store_types=["trace"])
        cfg_errors = [
            exc for _, exc in excinfo.value.errors if isinstance(exc, ConfigError)
        ]
        assert any("empty network location" in str(exc) for exc in cfg_errors)

    def test_postgres_happy_uri_passes_format_check(self, tmp_path: Path) -> None:
        # A well-formed URI must NOT show up in the URI-format failures.
        # (Instantiation may still fail downstream because psycopg can't
        # actually connect — that's separate. We only assert the format
        # check itself produced no errors.)
        config = {
            "trace": {
                "backend": "postgres",
                "dsn": "postgres://user:pw@host:5432/db",
            },
        }
        reg = StoreRegistry(config=config, stores_dir=tmp_path / "stores")
        format_errors = reg._check_uri_formats(["trace"])
        assert format_errors == []


class _SpyStore:
    """Stands in for a backend class: records each construction, connects to nothing."""

    built: ClassVar[list[str]] = []
    raises: ClassVar[Exception | None] = None

    def __init__(self, dsn: str | None = None, uri: str | None = None) -> None:
        type(self).built.append(dsn or uri or "")
        if type(self).raises is not None:
            raise type(self).raises


_HEALTHY_DSN = "postgres://user:pw@host:5432/db"
_REFUSED_DSN = "postgres:///dbname"


@pytest.fixture
def spy() -> Iterator[type[_SpyStore]]:
    """Swap the Postgres trace/document and Neo4j graph backends for a spy."""
    _SpyStore.built = []
    _SpyStore.raises = None
    try:
        with ExitStack() as stack:
            for store_type, backend in (
                ("trace", "postgres"),
                ("document", "postgres"),
                ("graph", "neo4j"),
            ):
                plane = registry_module._PLANE_OF[store_type]
                stack.enter_context(
                    patch.dict(
                        registry_module._BUILTIN_BACKENDS[plane][store_type],
                        {backend: (__name__, "_SpyStore")},
                    )
                )
            _reset_backend_cache()
            yield _SpyStore
    finally:
        _reset_backend_cache()


class TestRefusedUriIsNotInstantiated:
    """A store whose URI the pre-flight refused is named, not built.

    Building it would hand a known-bad URI to a driver; the Postgres pool
    waits out a 30 s timeout on one.
    """

    def test_a_refused_dsn_is_reported_and_never_constructed(
        self, tmp_path: Path, spy: type[_SpyStore]
    ) -> None:
        config = {
            "trace": {"backend": "postgres", "dsn": _REFUSED_DSN},
            "document": {"backend": "postgres", "dsn": _HEALTHY_DSN},
        }
        reg = StoreRegistry(config=config, stores_dir=tmp_path / "stores")

        with pytest.raises(RegistryValidationError) as excinfo:
            reg.validate(store_types=["trace", "document"])

        ((name, exc),) = excinfo.value.errors
        assert name == "trace"
        assert isinstance(exc, ConfigError)
        assert exc.setting == "dsn"
        assert str(excinfo.value).splitlines() == [
            "StoreRegistry validation failed for 1 store(s):",
            "  - trace: ConfigError: dsn: empty network location (host:port required)",
        ]
        # The healthy store in the same validate is still built and cached.
        assert spy.built == [_HEALTHY_DSN]
        assert "document" in reg._cache
        assert "trace" not in reg._cache

    def test_stores_sharing_a_refused_dsn_are_each_named_and_none_built(
        self, tmp_path: Path, spy: type[_SpyStore]
    ) -> None:
        config = {
            "trace": {"backend": "postgres", "dsn": _REFUSED_DSN},
            "document": {"backend": "postgres", "dsn": _REFUSED_DSN},
        }
        reg = StoreRegistry(config=config, stores_dir=tmp_path / "stores")

        with pytest.raises(RegistryValidationError) as excinfo:
            reg.validate(store_types=["trace", "document"])

        assert [name for name, _ in excinfo.value.errors] == ["trace", "document"]
        assert spy.built == []
        assert reg._cache == {}

    def test_the_skip_follows_the_store_not_the_uri_text(
        self, tmp_path: Path, spy: type[_SpyStore]
    ) -> None:
        # ``bolt://`` is refused for a Postgres store and valid for Neo4j.
        shared = "bolt://host:7687"
        config = {
            "trace": {"backend": "postgres", "dsn": shared},
            "graph": {"backend": "neo4j", "uri": shared},
        }
        reg = StoreRegistry(config=config, stores_dir=tmp_path / "stores")

        with pytest.raises(RegistryValidationError) as excinfo:
            reg.validate(store_types=["trace", "graph"])

        ((name, exc),) = excinfo.value.errors
        assert name == "trace"
        assert "unexpected URL scheme for postgres backend" in str(exc)
        assert spy.built == [shared]  # the graph store, not the trace store
        assert "graph" in reg._cache

    def test_a_store_another_preflight_check_names_is_still_instantiated(
        self, tmp_path: Path, spy: type[_SpyStore]
    ) -> None:
        # The fingerprint check labels its error with a store type too.
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir()
        (stores_dir / _FINGERPRINT_META_FILENAME).write_text(
            json.dumps({"trace": "trace/sqlite/v0"})
        )
        config = {"document": {"backend": "postgres", "dsn": _REFUSED_DSN}}
        reg = StoreRegistry(config=config, stores_dir=stores_dir)

        with pytest.raises(RegistryValidationError) as excinfo:
            reg.validate(store_types=["trace", "document"])

        named = [name for name, _ in excinfo.value.errors]
        assert named == ["document", "trace"]
        assert "SchemaFingerprintMismatch" in str(excinfo.value.errors[1][1])
        assert "trace" in reg._cache  # built despite the fingerprint error
        assert spy.built == []  # the refused document store never was

    def test_a_refused_store_still_reports_what_precedes_the_constructor(
        self, tmp_path: Path, spy: type[_SpyStore]
    ) -> None:
        config = {
            "trace": {"backend": "postgres", "dsn": _REFUSED_DSN, "bogus": 1},
        }
        reg = StoreRegistry(config=config, stores_dir=tmp_path / "stores")

        with pytest.raises(RegistryValidationError) as excinfo:
            reg.validate(store_types=["trace"])

        errors = excinfo.value.errors
        assert [name for name, _ in errors] == ["trace", "trace"]
        assert "empty network location" in str(errors[0][1])
        assert "does not accept" in str(errors[1][1])
        assert spy.built == []

    def test_a_well_formed_dsn_is_constructed_and_its_failure_reported(
        self, tmp_path: Path, spy: type[_SpyStore]
    ) -> None:
        spy.raises = ConnectionError("pool timed out")
        config = {"trace": {"backend": "postgres", "dsn": _HEALTHY_DSN}}
        reg = StoreRegistry(config=config, stores_dir=tmp_path / "stores")

        with pytest.raises(RegistryValidationError) as excinfo:
            reg.validate(store_types=["trace"])

        ((name, exc),) = excinfo.value.errors
        assert name == "trace"
        assert isinstance(exc, ConnectionError)
        assert spy.built == [_HEALTHY_DSN]


class TestEmbeddingDimConsistency:
    """``embedding_dim`` must match across graph + vector when both declared."""

    def test_mismatch_aggregates_into_validation_error(self, tmp_path: Path) -> None:
        config = {
            "graph": {"backend": "sqlite", "embedding_dim": 384},
            "vector": {"backend": "sqlite", "embedding_dim": 768},
        }
        reg = StoreRegistry(config=config, stores_dir=tmp_path / "stores")
        with pytest.raises(RegistryValidationError) as excinfo:
            reg.validate(store_types=["graph", "vector"])
        labels = [name for name, _ in excinfo.value.errors]
        assert "embedding_dim" in labels
        ((_, exc),) = [
            pair for pair in excinfo.value.errors if pair[0] == "embedding_dim"
        ]
        assert isinstance(exc, ConfigError)
        rendered = str(exc)
        assert "384" in rendered
        assert "768" in rendered

    def test_matching_dims_produces_no_consistency_error(self, tmp_path: Path) -> None:
        # Both stores agree on dim — consistency check must produce no error.
        # (The vector store may reject the kwarg at instantiation; we
        # don't care, we only assert the dim-consistency helper itself.)
        config = {
            "graph": {"backend": "sqlite", "embedding_dim": 768},
            "vector": {"backend": "sqlite", "embedding_dim": 768},
        }
        reg = StoreRegistry(config=config, stores_dir=tmp_path / "stores")
        assert reg._check_embedding_dim_consistency() == []


class TestEmbeddingsShapeAdvisory:
    """A non-mapping ``embeddings:`` block is a
    startup-visible warning, not a validation failure.

    :attr:`StoreRegistry.embedding_fn` already degrades this shape to
    "unconfigured" lazily, on first access; this check makes the same
    fact observable at ``validate()`` time too, for a deployment whose
    request paths never happen to touch the lazy property.
    """

    def test_a_non_mapping_block_never_fails_validate(self, tmp_path: Path) -> None:
        reg = StoreRegistry(
            stores_dir=tmp_path / "stores",
            embedding_config="sk-live-RegistryValidateEmbeddingsShapeSecret",
        )
        reg.validate(check_connectivity=False)  # must not raise
        assert reg._check_embeddings_shape() == []

    def test_a_non_mapping_block_logs_the_knob_not_the_value(
        self, tmp_path: Path
    ) -> None:
        secret = "sk-live-RegistryValidateEmbeddingsShapeSecret"  # noqa: S105
        reg = StoreRegistry(stores_dir=tmp_path / "stores", embedding_config=secret)
        with capture_logs() as logs:
            reg.validate(check_connectivity=False)
        malformed = [
            log for log in logs if log.get("event") == "config.embeddings_malformed"
        ]
        assert len(malformed) == 1, logs
        assert malformed[0]["knob"] == "embeddings"
        assert malformed[0]["log_level"] == "warning"
        for log in logs:
            for value in log.values():
                assert secret not in str(value)

    @pytest.mark.parametrize(
        "embedding_config",
        [
            None,
            {},
            {"provider": "openai"},
            {"provider": None},
        ],
    )
    def test_a_well_shaped_or_unset_block_never_logs(
        self, tmp_path: Path, embedding_config: dict | None
    ) -> None:
        reg = StoreRegistry(
            stores_dir=tmp_path / "stores", embedding_config=embedding_config
        )
        with capture_logs() as logs:
            # openai provider with no API key raises inside _get("vector")
            # for an unrelated reason in some configs; the pre-flight
            # checks run before that, so assert on the pre-flight helper
            # directly rather than requiring the whole validate() to
            # succeed for every parametrized shape here.
            assert reg._check_embeddings_shape() == []
        malformed = [
            log for log in logs if log.get("event") == "config.embeddings_malformed"
        ]
        assert malformed == []
