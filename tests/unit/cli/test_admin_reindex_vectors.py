"""Tests for ``trellis admin reindex-vectors``.

Runs the command through CliRunner against real SQLite stores in a
tmp config/data dir (the same harness as the other admin-command
tests). The embedder is injected via ``TRELLIS_EMBEDDING_FN`` — the
env override the registry checks first — pointing at
:func:`fake_embed` below, so no network or provider extra is needed.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from trellis.errors import ConfigError
from trellis.stores.registry import _KnowledgePlane
from trellis_cli.admin import admin_app
from trellis_cli.main import app
from trellis_cli.stores import _get_registry, _reset_registry

runner = CliRunner()

#: Dotted path handed to TRELLIS_EMBEDDING_FN in the tests below.
EMBED_FN_PATH = "tests.unit.cli.test_admin_reindex_vectors.fake_embed"


def fake_embed(text: str) -> list[float]:
    """Deterministic 3-dim embedding for CLI tests."""
    return [1.0, 0.0, float(len(text) % 7)]


@pytest.fixture
def cli_env(tmp_path, monkeypatch) -> None:
    """Isolated config/data dirs with initialised SQLite stores."""
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_FN_PATH)
    init = runner.invoke(admin_app, ["init"])
    assert init.exit_code == 0, init.output
    _reset_registry()


def _seed_documents() -> None:
    registry = _get_registry()
    registry.knowledge.document_store.put("doc-1", "first document", metadata={})
    registry.knowledge.document_store.put(
        "doc-2", "second document", metadata={"domain": "backend"}
    )
    registry.knowledge.document_store.put("doc-empty", "", metadata={})


def _run_json(*args: str) -> dict:
    result = runner.invoke(admin_app, ["reindex-vectors", "--format", "json", *args])
    assert result.exit_code == 0, result.output
    # All of stdout, as ``| jq`` reads it -- not the last line of ``output``,
    # which interleaves stderr and so parsed a payload printed there.
    return json.loads(result.stdout)


class TestReindexVectorsCLI:
    def test_command_registered_on_admin_app(self) -> None:
        names = [cmd.name for cmd in admin_app.registered_commands]
        assert "reindex-vectors" in names

    def test_missing_embedder_exits_loudly(self, cli_env, monkeypatch) -> None:
        monkeypatch.delenv("TRELLIS_EMBEDDING_FN", raising=False)
        _reset_registry()
        result = runner.invoke(admin_app, ["reindex-vectors", "--format", "json"])
        assert result.exit_code == 1
        # The error envelope is all of stdout too; ``output`` also holds stderr.
        payload = json.loads(result.stdout)
        assert payload["status"] == "error"
        assert "embeddings config" in payload["message"]

    def test_backfills_and_skips_on_rerun(self, cli_env) -> None:
        _seed_documents()

        summary = _run_json()
        assert summary["scanned"] == 3
        assert summary["embedded"] == 2
        assert summary["skipped_empty"] == 1
        assert summary["errors"] == 0

        # Vectors landed, keyed by doc_id, carrying the excerpt + metadata.
        vector_store = _get_registry().knowledge.vector_store
        row = vector_store.get("doc-2")
        assert row is not None
        assert row["metadata"]["content"] == "second document"
        assert row["metadata"]["domain"] == "backend"

        # Rerun: everything already indexed.
        rerun = _run_json()
        assert rerun["embedded"] == 0
        assert rerun["skipped_existing"] == 2

        # --force re-embeds.
        forced = _run_json("--force")
        assert forced["embedded"] == 2

    def test_dry_run_counts_without_writing(self, cli_env) -> None:
        _seed_documents()
        summary = _run_json("--dry-run")
        assert summary["dry_run"] is True
        assert summary["embedded"] == 2
        assert _get_registry().knowledge.vector_store.count() == 0

    def test_limit_bounds_scan(self, cli_env) -> None:
        _seed_documents()
        summary = _run_json("--limit", "1")
        assert summary["scanned"] == 1


class TestReindexVectorsStoreResolveFailure:
    """A raising ``vector_store`` property must not escape as a traceback.

    ``getattr(registry.knowledge, "vector_store", None)`` only ever
    absorbed an ``AttributeError`` the real property never raises — any
    OTHER exception from resolving it (a broken backend config) escaped
    uncaught. It is now routed through the root CLI boundary
    (``trellis_cli.main._BoundaryGroup``), which is why these three tests
    invoke ``trellis_cli.main.app`` rather than ``admin_app`` directly: a
    local ``except StoreError`` here once pre-empted that boundary and
    printed the raw, unsanitized exception text — the regression #839's
    fix round closed (see the module docstring at ``_resolve_vector_store``).
    A ``ConfigError`` (already a ``TrellisError``) is re-raised unchanged
    and keeps its own ``error_code``/``setting``; only an untyped
    exception is wrapped into a ``StoreError``. Both reach the boundary
    and exit ``EXIT_STORE`` (5), distinct from the already-existing
    ``EXIT_INTERNAL`` (1) "unconfigured" branch covered by
    ``test_missing_embedder_exits_loudly`` above.
    """

    #: A DSN-shaped credential, embedded in the raised exception's own
    #: message. ``sanitize_error_message`` (src/trellis/core/error_sanitize.py)
    #: suppresses a URL with inline credentials wholesale; asserting on
    #: this exact substring is what makes the "no message text leaks"
    #: checks below non-vacuous rather than merely checking the type name
    #: is present.
    _LEAK = "postgres://trellis:hunter2@dbhost.internal:5432/trellis"

    def test_json_format_exits_store(self, cli_env, monkeypatch) -> None:
        monkeypatch.setattr(
            _KnowledgePlane,
            "vector_store",
            property(
                lambda _self: (_ for _ in ()).throw(
                    ConfigError(f"boom ({self._LEAK})", setting="vectors.backend")
                )
            ),
        )
        result = runner.invoke(app, ["admin", "reindex-vectors", "--format", "json"])
        assert result.exit_code == 5
        payload = json.loads(result.stdout)
        assert payload["status"] == "error"
        assert payload["error_type"] == "ConfigError"
        assert payload["error_code"] == "CONFIG_ERROR"
        assert payload["setting"] == "vectors.backend"
        assert self._LEAK not in payload["message"]
        assert "hunter2" not in payload["message"]

    def test_text_format_exits_store_too(self, cli_env, monkeypatch) -> None:
        """Same exit code regardless of ``--format`` (parity rule).

        Text is the terminal surface, not the machine envelope: per
        ``_render_boundary_failure``'s own docstring it prints
        ``exc.message`` unsanitized "as every other error render in this
        CLI does" — a pre-existing, documented convention this PR does not
        change. Only ``--format json`` is the leak-checked surface
        (asserted in :meth:`test_json_format_exits_store`); this test
        pins the exit code and that the error's own code/type still
        appears on text.
        """
        monkeypatch.setattr(
            _KnowledgePlane,
            "vector_store",
            property(
                lambda _self: (_ for _ in ()).throw(
                    ConfigError(f"boom ({self._LEAK})", setting="vectors.backend")
                )
            ),
        )
        result = runner.invoke(app, ["admin", "reindex-vectors"])
        assert result.exit_code == 5
        assert "CONFIG_ERROR" in result.output

    def test_describes_a_non_config_error_by_type_alone(
        self, cli_env, monkeypatch
    ) -> None:
        """An untyped cause is wrapped into a ``StoreError`` naming only
        its type — never its message — and still exits 5 through the
        same boundary, with its own envelope keys (``error_type``,
        ``error_code``, ``store``)."""
        monkeypatch.setattr(
            _KnowledgePlane,
            "vector_store",
            property(
                lambda _self: (_ for _ in ()).throw(
                    RuntimeError(f"db down ({self._LEAK})")
                )
            ),
        )
        result = runner.invoke(app, ["admin", "reindex-vectors", "--format", "json"])
        assert result.exit_code == 5
        payload = json.loads(result.stdout)
        assert payload["status"] == "error"
        assert payload["error_type"] == "StoreError"
        assert payload["error_code"] == "STORE_ERROR"
        assert payload["store"] == "vector"
        assert "RuntimeError" in payload["message"]
        assert self._LEAK not in payload["message"]
        assert "db down" not in payload["message"]
