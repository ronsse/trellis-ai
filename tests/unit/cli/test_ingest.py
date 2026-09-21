"""Tests for ingest CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trellis_cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CLI stores at a temp directory."""
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


def _trace_json() -> str:
    return json.dumps(
        {
            "source": "agent",
            "intent": "deploy service",
            "steps": [],
            "context": {"agent_id": "agent-1", "domain": "platform"},
        }
    )


def _evidence_json() -> str:
    return json.dumps(
        {
            "evidence_type": "snippet",
            "content": "SELECT * FROM users",
            "source_origin": "trace",
        }
    )


class TestIngestTrace:
    def test_ingest_trace_from_file(self, tmp_path: Path) -> None:
        f = tmp_path / "trace.json"
        f.write_text(_trace_json())
        result = runner.invoke(app, ["ingest", "trace", str(f)])
        assert result.exit_code == 0
        assert "ingested" in result.stdout.lower()

    def test_ingest_trace_json_format(self, tmp_path: Path) -> None:
        f = tmp_path / "trace.json"
        f.write_text(_trace_json())
        result = runner.invoke(app, ["ingest", "trace", str(f), "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ingested"
        assert "trace_id" in data

    def test_ingest_trace_from_stdin(self) -> None:
        result = runner.invoke(app, ["ingest", "trace", "-"], input=_trace_json())
        assert result.exit_code == 0

    def test_ingest_trace_invalid_json(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.json"
        f.write_text("not json")
        result = runner.invoke(app, ["ingest", "trace", str(f)])
        assert result.exit_code == 1

    def test_ingest_trace_invalid_schema(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.json"
        f.write_text(json.dumps({"bogus": "data"}))
        result = runner.invoke(app, ["ingest", "trace", str(f)])
        assert result.exit_code == 1

    def test_ingest_trace_file_not_found(self) -> None:
        result = runner.invoke(app, ["ingest", "trace", "/nonexistent/file.json"])
        assert result.exit_code == 1


def _rich_trace_json() -> str:
    """A trace exercising agent / domain / tool / evidence / artifact."""
    return json.dumps(
        {
            "source": "agent",
            "intent": "Find and fix the broken import in auth_service.py",
            "steps": [
                {"step_type": "tool_call", "name": "search_codebase"},
                {"step_type": "tool_call", "name": "edit_file"},
            ],
            "evidence_used": [{"evidence_id": "ev_123", "role": "reference"}],
            "artifacts_produced": [{"artifact_id": "pr_847", "artifact_type": "pr"}],
            "outcome": {"status": "success"},
            "context": {"agent_id": "code-orchestrator", "domain": "backend"},
        }
    )


class TestIngestTraceExtraction:
    """Feature-flagged TRELLIS_ENABLE_TRACE_EXTRACTION post-ingest hook."""

    def test_flag_off_writes_no_graph(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Flag absent -> behaviour byte-identical to today: trace stored,
        # graph untouched.
        monkeypatch.delenv("TRELLIS_ENABLE_TRACE_EXTRACTION", raising=False)
        f = tmp_path / "trace.json"
        f.write_text(_rich_trace_json())
        result = runner.invoke(app, ["ingest", "trace", str(f), "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ingested"
        assert "extraction" not in data

        from trellis_cli.stores import get_graph_store

        assert get_graph_store().count_nodes() == 0

    def test_flag_on_populates_graph_with_provenance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_ENABLE_TRACE_EXTRACTION", "1")
        f = tmp_path / "trace.json"
        f.write_text(_rich_trace_json())
        result = runner.invoke(app, ["ingest", "trace", str(f), "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ingested"
        assert data["extraction"]["executed"] is True
        assert data["extraction"]["entities"] > 0
        assert data["extraction"]["edges"] > 0

        from trellis_cli.stores import get_graph_store

        graph = get_graph_store()
        assert graph.count_nodes() > 0
        trace_id = data["trace_id"]
        # Activity node is retrievable by its stable id.
        activity = graph.get_node(f"trace:{trace_id}")
        assert activity is not None
        # Every edge carries source_trace_id provenance.
        edges = graph.get_edges(f"trace:{trace_id}", direction="outgoing")
        assert edges
        for edge in edges:
            props = edge.get("properties", {})
            assert props.get("source_trace_id") == trace_id

    def test_extraction_failure_does_not_fail_ingest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_ENABLE_TRACE_EXTRACTION", "1")
        f = tmp_path / "trace.json"
        f.write_text(_rich_trace_json())
        # Force the extraction batch to blow up; ingest must still succeed.
        import trellis.extract.trace_ingest_hook as hook

        def _boom(*_a: object, **_k: object) -> object:
            msg = "extraction exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr(hook, "result_to_batch", _boom)
        result = runner.invoke(app, ["ingest", "trace", str(f), "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ingested"
        assert data["extraction"]["executed"] is False
        assert "extraction exploded" in data["extraction"]["error"]


class TestIngestEvidence:
    def test_ingest_evidence_from_file(self, tmp_path: Path) -> None:
        f = tmp_path / "evidence.json"
        f.write_text(_evidence_json())
        result = runner.invoke(app, ["ingest", "evidence", str(f)])
        assert result.exit_code == 0
        assert "ingested" in result.stdout.lower()

    def test_ingest_evidence_json_format(self, tmp_path: Path) -> None:
        f = tmp_path / "evidence.json"
        f.write_text(_evidence_json())
        result = runner.invoke(app, ["ingest", "evidence", str(f), "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ingested"

    def test_ingest_evidence_invalid(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.json"
        f.write_text(json.dumps({"bad": "data"}))
        result = runner.invoke(app, ["ingest", "evidence", str(f)])
        assert result.exit_code == 1

    def test_ingest_evidence_file_not_found(self) -> None:
        result = runner.invoke(app, ["ingest", "evidence", "/nonexistent.json"])
        assert result.exit_code == 1


class TestIngestHelp:
    def test_ingest_help(self) -> None:
        result = runner.invoke(app, ["ingest", "--help"])
        assert result.exit_code == 0
        assert "trace" in result.stdout
        assert "evidence" in result.stdout


_SAMPLE_DBT_MANIFEST: dict = {
    "nodes": {
        "model.p.stg_orders": {
            "unique_id": "model.p.stg_orders",
            "resource_type": "model",
            "name": "stg_orders",
            "schema": "staging",
            "description": "Staged orders",
            "depends_on": {"nodes": ["source.p.raw.orders"]},
            "config": {"materialized": "view"},
        },
    },
    "sources": {
        "source.p.raw.orders": {
            "unique_id": "source.p.raw.orders",
            "resource_type": "source",
            "name": "orders",
            "source_name": "raw",
            "schema": "public",
            "description": "Raw orders table",
        },
    },
}

_SAMPLE_OL_EVENTS: list[dict] = [
    {
        "eventType": "COMPLETE",
        "job": {"namespace": "spark", "name": "etl_job"},
        "inputs": [{"namespace": "warehouse", "name": "raw.events"}],
        "outputs": [{"namespace": "warehouse", "name": "analytics.daily_events"}],
    },
]


class TestIngestDbtManifest:
    def test_ingest_manifest_file(self, tmp_path: Path) -> None:
        # admin init is needed for the stores dir layout the CLI expects.
        runner.invoke(app, ["admin", "init"])
        f = tmp_path / "manifest.json"
        f.write_text(json.dumps(_SAMPLE_DBT_MANIFEST))
        result = runner.invoke(
            app, ["ingest", "dbt-manifest", str(f), "--format", "json"]
        )
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ingested"
        assert data["nodes"] == 2
        assert data["edges"] == 1
        assert data["documents"] == 2  # both have descriptions

    def test_ingest_from_project_dir(self, tmp_path: Path) -> None:
        runner.invoke(app, ["admin", "init"])
        target = tmp_path / "target"
        target.mkdir()
        (target / "manifest.json").write_text(json.dumps(_SAMPLE_DBT_MANIFEST))
        result = runner.invoke(
            app, ["ingest", "dbt-manifest", str(tmp_path), "--format", "json"]
        )
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout.strip())
        assert data["nodes"] == 2

    def test_missing_path(self) -> None:
        runner.invoke(app, ["admin", "init"])
        result = runner.invoke(
            app, ["ingest", "dbt-manifest", "/nonexistent/manifest.json"]
        )
        assert result.exit_code == 1


class TestIngestOpenLineage:
    def test_ingest_events_json_array(self, tmp_path: Path) -> None:
        runner.invoke(app, ["admin", "init"])
        f = tmp_path / "events.json"
        f.write_text(json.dumps(_SAMPLE_OL_EVENTS))
        result = runner.invoke(
            app, ["ingest", "openlineage", str(f), "--format", "json"]
        )
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ingested"
        assert data["nodes"] == 3  # 1 job + 2 datasets
        assert data["edges"] == 2  # reads + writes

    def test_ingest_events_ndjson(self, tmp_path: Path) -> None:
        runner.invoke(app, ["admin", "init"])
        f = tmp_path / "events.ndjson"
        f.write_text("\n".join(json.dumps(e) for e in _SAMPLE_OL_EVENTS))
        result = runner.invoke(
            app, ["ingest", "openlineage", str(f), "--format", "json"]
        )
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout.strip())
        assert data["nodes"] == 3

    def test_missing_path(self) -> None:
        runner.invoke(app, ["admin", "init"])
        result = runner.invoke(
            app, ["ingest", "openlineage", "/nonexistent/events.json"]
        )
        assert result.exit_code == 1


class TestIngestDbtManifestClassifies:
    """dbt description documents are tagged on write (#568, first half).

    They were written outside both ingest hooks, so they carried no
    ``content_tags`` at all and the keyword axis — the one measured worst on
    this deployment — was the only one that could reach them. These pin the
    deterministic, no-cost half. Embedding is still deliberately absent, so
    there is nothing here asserting a vector row.
    """

    @staticmethod
    def _ingest(tmp_path: Path) -> list[dict]:
        """Run the command and return the ``dbt:`` documents it persisted."""
        runner.invoke(app, ["admin", "init"])
        f = tmp_path / "manifest.json"
        f.write_text(json.dumps(_SAMPLE_DBT_MANIFEST))
        result = runner.invoke(
            app, ["ingest", "dbt-manifest", str(f), "--format", "json"]
        )
        assert result.exit_code == 0, result.stdout
        assert json.loads(result.stdout.strip())["documents"] == 2

        from trellis_cli.stores import get_document_store

        docs = [
            d
            for d in get_document_store().list_documents(limit=100)
            if d["doc_id"].startswith("dbt:")
        ]
        assert len(docs) == 2, [d["doc_id"] for d in docs]
        return docs

    def test_descriptions_carry_tags_when_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_ENABLE_CLASSIFY_ON_INGEST", "1")
        for doc in self._ingest(tmp_path):
            tags = doc["metadata"]["content_tags"]
            # The affinities are the point: TierMapper reads an explicit
            # retrieval_affinity *before* falling back to heuristics, so this
            # is what makes a dbt description routable in a sectioned pack.
            assert set(tags["retrieval_affinity"]) == {
                "technical_pattern",
                "reference",
            }, doc["doc_id"]
            assert "auto_importance" in doc["metadata"]
            # Every writer of auto_importance must also stamp (#417 / the
            # importance-freshness ADR); an unstamped score raises in scoring.
            assert tags["importance_scored_at"]

    def test_domain_is_never_auto_assigned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``domain`` is the one facet that hard-excludes on mismatch.

        The deterministic classifiers *do* have an opinion here — the source
        map sends ``dbt`` to ``data-pipeline`` — and ``classify_for_ingest``
        drops it. Writing it would narrow these rows to domain-scoped queries
        naming that exact value, which is the #282 shape that made 36
        production documents invisible.
        """
        monkeypatch.setenv("TRELLIS_ENABLE_CLASSIFY_ON_INGEST", "1")
        for doc in self._ingest(tmp_path):
            assert doc["metadata"]["content_tags"]["domain"] == []

    def test_tagged_rows_still_answer_a_domain_scoped_query(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tagging must not cost these rows the one axis that reaches them."""
        monkeypatch.setenv("TRELLIS_ENABLE_CLASSIFY_ON_INGEST", "1")
        self._ingest(tmp_path)

        from trellis_cli.stores import get_document_store

        hits = get_document_store().search(
            "orders",
            limit=10,
            filters={"content_tags": {"domain": {"in": ["engineering"]}}},
        )
        assert {h["doc_id"] for h in hits if h["doc_id"].startswith("dbt:")}

    def test_no_tags_when_flag_is_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag off is byte-identical to the behaviour this replaced."""
        monkeypatch.delenv("TRELLIS_ENABLE_CLASSIFY_ON_INGEST", raising=False)
        for doc in self._ingest(tmp_path):
            assert "content_tags" not in doc["metadata"]
            assert "auto_importance" not in doc["metadata"]

    def test_source_system_is_written_either_way(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``source_system`` is a row fact, not a classification artefact.

        It is the declared ``DocumentMetadata`` field the ad-hoc ``source``
        key was standing in for, and ``PACK_ASSEMBLED.injected_items[]``
        reads it as ``domain_system``. Additive — ``source`` stays.
        """
        monkeypatch.delenv("TRELLIS_ENABLE_CLASSIFY_ON_INGEST", raising=False)
        for doc in self._ingest(tmp_path):
            assert doc["metadata"]["source_system"] == "dbt"
            assert doc["metadata"]["source"] == "dbt"

    def test_a_broken_classifier_does_not_fail_the_ingest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-soft is the property, not an implementation detail.

        The documents are the command's output; a tagging error must degrade
        to an untagged write rather than lose them.
        """
        monkeypatch.setenv("TRELLIS_ENABLE_CLASSIFY_ON_INGEST", "1")
        calls = []

        def _boom() -> object:
            calls.append(1)
            msg = "classifier exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr("trellis.classify.ingest.get_ingest_classifier", _boom)
        for doc in self._ingest(tmp_path):
            assert "content_tags" not in doc["metadata"]
        # Without this the test passes against a source that never calls the
        # seam at all, which is the state it was written to replace.
        assert len(calls) == 2
