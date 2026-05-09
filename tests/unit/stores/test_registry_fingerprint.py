"""Tests for the schema-fingerprint substrate-switch check (Logic Gap 4.5).

Covers ``StoreRegistry._check_schema_fingerprints`` and its integration
into :meth:`StoreRegistry.validate`. Fingerprint format is
``"{store_kind}/{backend}/v{SCHEMA_VERSION}"``; mismatch raises
:class:`ConfigError` aggregated into :class:`RegistryValidationError`.
First-boot writes; matching fingerprints are silent; the
``TRELLIS_SKIP_FINGERPRINT_CHECK`` env var bypasses both branches.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trellis.errors import ConfigError
from trellis.stores.registry import (
    _FINGERPRINT_META_FILENAME,
    RegistryValidationError,
    StoreRegistry,
)


def _read_meta(stores_dir: Path) -> dict[str, str]:
    meta_path = stores_dir / _FINGERPRINT_META_FILENAME
    if not meta_path.exists():
        return {}
    return json.loads(meta_path.read_text())


class TestFirstBoot:
    """No prior meta file → fingerprints get bootstrapped, no error."""

    def test_first_boot_writes_fingerprints(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        reg = StoreRegistry(stores_dir=stores_dir)
        reg.validate(store_types=["graph", "trace"])
        meta = _read_meta(stores_dir)
        # Both targets have entries with the expected default-substrate
        # fingerprint shape — graph + trace default to sqlite.
        assert meta["graph"] == "graph/sqlite/v1"
        assert meta["trace"] == "trace/sqlite/v1"

    def test_first_boot_full_stack_writes_every_store(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        reg = StoreRegistry(stores_dir=stores_dir)
        reg.validate()
        meta = _read_meta(stores_dir)
        # blob defaults to local; the rest default to sqlite. Either
        # way, every validated store_type ends up in the meta file.
        from trellis.stores.registry import _PLANE_OF

        assert set(meta.keys()) == set(_PLANE_OF.keys())
        assert meta["blob"] == "blob/local/v1"


class TestMatchingFingerprints:
    """Existing matching meta → silent, no error."""

    def test_second_boot_matches(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        reg = StoreRegistry(stores_dir=stores_dir)
        reg.validate(store_types=["graph", "trace"])
        meta_first = _read_meta(stores_dir)

        # Re-validate with the same config; meta file should be
        # untouched (no error, no rewrites of differing content).
        reg2 = StoreRegistry(stores_dir=stores_dir)
        reg2.validate(store_types=["graph", "trace"])
        assert _read_meta(stores_dir) == meta_first


class TestMismatchedFingerprints:
    """Mismatch → ConfigError with the actionable message."""

    def test_substrate_swap_raises_config_error(self, tmp_path: Path) -> None:
        # Plant a meta file claiming the document store was last
        # written by a different substrate (postgres). Configured
        # backend is sqlite (the default). Validation must reject.
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        (stores_dir / _FINGERPRINT_META_FILENAME).write_text(
            json.dumps({"document": "document/postgres/v1"})
        )
        reg = StoreRegistry(stores_dir=stores_dir)
        with pytest.raises(RegistryValidationError) as excinfo:
            reg.validate(store_types=["document"])

        ((store_type, exc),) = [
            pair
            for pair in excinfo.value.errors
            if pair[0] == "document" and isinstance(pair[1], ConfigError)
        ]
        assert store_type == "document"
        assert isinstance(exc, ConfigError)
        rendered = str(exc)
        assert "SchemaFingerprintMismatch" in rendered
        assert "document/sqlite/v1" in rendered  # configured (sqlite default)
        assert "document/postgres/v1" in rendered  # stored (planted)
        assert "TRELLIS_SKIP_FINGERPRINT_CHECK" in rendered

    def test_schema_version_bump_raises_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Plant a meta file claiming an older schema version. The
        # configured substrate has SCHEMA_VERSION = "1" (the default),
        # so a planted "v0" should mismatch.
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        (stores_dir / _FINGERPRINT_META_FILENAME).write_text(
            json.dumps({"trace": "trace/sqlite/v0"})
        )
        reg = StoreRegistry(stores_dir=stores_dir)
        with pytest.raises(RegistryValidationError) as excinfo:
            reg.validate(store_types=["trace"])
        cfg_errors = [
            exc
            for name, exc in excinfo.value.errors
            if name == "trace" and isinstance(exc, ConfigError)
        ]
        assert any("SchemaFingerprintMismatch" in str(e) for e in cfg_errors)
        assert any("trace/sqlite/v0" in str(e) for e in cfg_errors)


class TestEnvVarSkip:
    """``TRELLIS_SKIP_FINGERPRINT_CHECK=1`` short-circuits both branches."""

    def test_env_var_skips_mismatch_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        (stores_dir / _FINGERPRINT_META_FILENAME).write_text(
            json.dumps({"document": "document/postgres/v1"})
        )
        # With the bypass set, the same mismatch that fails in
        # ``test_substrate_swap_raises_config_error`` must validate
        # cleanly — letting an operator finish a migration window.
        monkeypatch.setenv("TRELLIS_SKIP_FINGERPRINT_CHECK", "1")
        reg = StoreRegistry(stores_dir=stores_dir)
        reg.validate(store_types=["document"])  # no error

    def test_env_var_does_not_overwrite_meta(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bypass shouldn't sneakily rewrite the meta file with the new
        # fingerprint — that would defeat the check on the next boot
        # without bypass. The file stays exactly as planted.
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        planted = {"document": "document/postgres/v1"}
        (stores_dir / _FINGERPRINT_META_FILENAME).write_text(json.dumps(planted))
        monkeypatch.setenv("TRELLIS_SKIP_FINGERPRINT_CHECK", "1")
        reg = StoreRegistry(stores_dir=stores_dir)
        reg.validate(store_types=["document"])
        assert _read_meta(stores_dir) == planted


class TestPartialMismatch:
    """Multi-store: only the bad one shows up; the others stay clean."""

    def test_only_bad_store_in_aggregate(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        # Document is mismatched; graph + trace are first-boot (no entry).
        (stores_dir / _FINGERPRINT_META_FILENAME).write_text(
            json.dumps({"document": "document/postgres/v1"})
        )
        reg = StoreRegistry(stores_dir=stores_dir)
        with pytest.raises(RegistryValidationError) as excinfo:
            reg.validate(store_types=["document", "graph", "trace"])
        # Only "document" fingerprint complaint in the aggregate; the
        # other two are first-boot and silent (other stores may add
        # their own non-fingerprint errors, but none from this check).
        fp_errors = [
            name
            for name, exc in excinfo.value.errors
            if isinstance(exc, ConfigError) and "SchemaFingerprintMismatch" in str(exc)
        ]
        assert fp_errors == ["document"]


class TestNoStoresDir:
    """No ``stores_dir`` → check silently skipped (all-remote deployment)."""

    def test_validate_succeeds_without_stores_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Postgres-only stack with DSN from env, no stores_dir. The
        # fingerprint check has nowhere to write, so it's a no-op
        # rather than a failure. (Documented limitation: all-remote
        # deployments don't get fingerprint protection until each
        # substrate grows its own metadata table.)
        monkeypatch.setenv("TRELLIS_KNOWLEDGE_PG_DSN", "postgres://u:p@host:5432/db")
        config = {"document": {"backend": "postgres"}}
        reg = StoreRegistry(config=config)
        # Just assert the fingerprint helper itself produces no error
        # and queues no writes when stores_dir is None.
        errors, to_write = reg._check_schema_fingerprints(["document"])
        assert errors == []
        assert to_write == {}


class TestNoWriteOnInstantiationFailure:
    """A failed instantiation must not bootstrap fingerprints for the world."""

    def test_failed_validate_does_not_write_meta(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pair: graph misconfigured (postgres, no DSN), trace healthy
        # (sqlite). Validate fails. The meta file must not exist after
        # — operator fixes the breakage and re-runs to bootstrap.
        monkeypatch.delenv("TRELLIS_KNOWLEDGE_PG_DSN", raising=False)
        monkeypatch.delenv("TRELLIS_OPERATIONAL_PG_DSN", raising=False)
        stores_dir = tmp_path / "stores"
        config = {"graph": {"backend": "postgres"}}
        reg = StoreRegistry(config=config, stores_dir=stores_dir)
        with pytest.raises(RegistryValidationError):
            reg.validate(store_types=["graph", "trace"])
        # No meta written — the registry held back the bootstrap.
        assert _read_meta(stores_dir) == {}
