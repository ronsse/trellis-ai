"""Tests for ``trellis classify backfill``.

Runs the command through CliRunner against real SQLite stores in a tmp
config/data dir — the same harness as ``test_admin_reindex_vectors``, the
sibling operator-driven backfill.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from trellis.stores.base.event_log import EventType
from trellis_cli.admin import admin_app
from trellis_cli.classify import classify_app
from trellis_cli.main import app
from trellis_cli.stores import _get_registry, _reset_registry

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path, monkeypatch) -> None:
    """Isolated config/data dirs with initialised SQLite stores."""
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    init = runner.invoke(admin_app, ["init"])
    assert init.exit_code == 0, init.output
    _reset_registry()


#: Content the deterministic StructuralClassifier tags as an error-resolution
#: (error keyword + fix keyword), so a backfill over it produces real signal.
_TAGGABLE = (
    "Traceback while booting the widget service: missing table 'jobs'. "
    "Fixed by running the schema migration before the web tier starts."
)


def _seed_documents(count: int = 3) -> None:
    store = _get_registry().knowledge.document_store
    for i in range(count):
        store.put(f"doc-{i}", f"{_TAGGABLE} (note {i})", {"title": f"doc {i}"})


def _run_json(*args: str) -> dict[str, Any]:
    result = runner.invoke(classify_app, ["backfill", "--format", "json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output.strip().splitlines()[-1])


def _tags(doc_id: str) -> dict[str, Any]:
    doc = _get_registry().knowledge.document_store.get(doc_id)
    assert doc is not None
    return doc["metadata"].get("content_tags") or {}


class TestClassifyBackfillCLI:
    def test_registered_on_the_root_app(self) -> None:
        groups = [group.name for group in app.registered_groups]
        assert "classify" in groups

    def test_tags_untagged_documents(self, cli_env) -> None:
        _seed_documents()

        summary = _run_json()

        assert summary["status"] == "ok"
        assert summary["scanned"] == 3
        assert summary["refreshed"] == 3
        assert sorted(summary["item_ids_refreshed"]) == ["doc-0", "doc-1", "doc-2"]
        tags = _tags("doc-1")
        assert tags["classified_at"]
        assert tags["content_type"]

    def test_rerun_skips_freshly_tagged_documents(self, cli_env) -> None:
        _seed_documents()
        _run_json()

        rerun = _run_json()

        assert rerun["scanned"] == 3
        assert rerun["refreshed"] == 0
        assert rerun["skipped_fresh"] == 3

    def test_max_age_days_zero_retags_everything(self, cli_env) -> None:
        _seed_documents()
        _run_json()

        rerun = _run_json("--max-age-days", "0")

        assert rerun["skipped_fresh"] == 0
        assert rerun["scanned"] == 3

    def test_emits_one_tags_refreshed_event_per_document(self, cli_env) -> None:
        _seed_documents(2)

        _run_json()

        events = _get_registry().operational.event_log.get_events(
            event_type=EventType.TAGS_REFRESHED, limit=50
        )
        assert len(events) == 2
        assert {e.entity_id for e in events} == {"doc-0", "doc-1"}

    def test_dry_run_writes_nothing_and_emits_nothing(self, cli_env) -> None:
        _seed_documents(2)

        summary = _run_json("--dry-run")

        assert summary["dry_run"] is True
        assert summary["refreshed"] == 2
        assert _tags("doc-0") == {}
        events = _get_registry().operational.event_log.get_events(
            event_type=EventType.TAGS_REFRESHED, limit=50
        )
        assert events == []

    def test_domain_is_not_assigned_by_default(self, cli_env) -> None:
        """The hard-excluding facet stays empty unless explicitly opted in."""
        store = _get_registry().knowledge.document_store
        store.put(
            "doc-code",
            "Deploy the kubernetes cluster with terraform and docker.",
            {"title": "infra note"},
        )

        _run_json()

        assert _tags("doc-code")["domain"] == []

    def test_include_domain_lets_the_classifier_assign_domain(
        self, cli_env, tmp_path
    ) -> None:
        config_path = tmp_path / "config" / "config.yaml"
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        raw["classify"] = {"domain_keywords": {"deployment": ["terraform", "docker"]}}
        config_path.write_text(yaml.dump(raw), encoding="utf-8")
        _reset_registry()

        store = _get_registry().knowledge.document_store
        store.put(
            "doc-code",
            "Deploy the cluster with terraform and docker.",
            {"title": "infra note"},
        )

        summary = _run_json("--include-domain")

        assert summary["include_domain"] is True
        # The operator's config domain wins a place alongside any built-in
        # keyword match — the point is that `domain` is populated at all.
        assert "deployment" in _tags("doc-code")["domain"]

    def test_limit_bounds_the_scan(self, cli_env) -> None:
        _seed_documents(3)

        summary = _run_json("--limit", "1", "--page-size", "1")

        assert summary["scanned"] == 1

    def test_pages_a_store_larger_than_one_page(self, cli_env) -> None:
        _seed_documents(5)

        summary = _run_json("--page-size", "2")

        assert summary["scanned"] == 5
        assert summary["refreshed"] == 5

    def test_stale_stamp_is_refreshed(self, cli_env) -> None:
        store = _get_registry().knowledge.document_store
        old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
        store.put(
            "doc-old",
            "# Heading\n\nprose",
            {"content_tags": {"domain": ["kept"], "classified_at": old}},
        )

        summary = _run_json()

        assert summary["refreshed"] == 1
        tags = _tags("doc-old")
        # domain is carried forward untouched, the stamp is refreshed.
        assert tags["domain"] == ["kept"]
        assert tags["classified_at"] > old

    def test_text_output_is_human_readable(self, cli_env) -> None:
        _seed_documents(1)

        result = runner.invoke(classify_app, ["backfill"])

        assert result.exit_code == 0
        assert "Classify backfill" in result.output

    def test_malformed_classify_config_exits_nonzero(self, cli_env, tmp_path) -> None:
        config_path = tmp_path / "config" / "config.yaml"
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        raw["classify"] = {"domain_keywords": {"deployment": "not-a-list"}}
        config_path.write_text(yaml.dump(raw), encoding="utf-8")
        _reset_registry()
        _seed_documents(1)

        result = runner.invoke(classify_app, ["backfill", "--format", "json"])

        assert result.exit_code == 1
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["status"] == "error"
