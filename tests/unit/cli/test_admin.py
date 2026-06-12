"""Tests for admin CLI commands."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from trellis_cli.admin import admin_app
from trellis_cli.main import app

runner = CliRunner()


class TestAdminInit:
    def test_init_creates_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        result = runner.invoke(app, ["admin", "init"])
        assert result.exit_code == 0
        assert (tmp_path / "config" / "config.yaml").exists()
        assert (tmp_path / "data" / "stores").exists()
        # Surfaces the human-decision setup pointer so team/enterprise
        # setups don't silently skip domains/ontology/security choices.
        assert "setup-decisions.md" in result.stdout

    def test_init_custom_data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        custom = str(tmp_path / "custom")
        result = runner.invoke(app, ["admin", "init", "--data-dir", custom])
        assert result.exit_code == 0
        assert (tmp_path / "custom" / "stores").exists()

    def test_init_no_overwrite_without_force(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        runner.invoke(app, ["admin", "init"])
        result = runner.invoke(app, ["admin", "init"])
        assert result.exit_code == 0
        assert "already exists" in result.stdout or "exists" in result.stdout

    def test_init_force_overwrites(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        runner.invoke(app, ["admin", "init"])
        result = runner.invoke(app, ["admin", "init", "--force"])
        assert result.exit_code == 0

    def test_init_json_format(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        result = runner.invoke(app, ["admin", "init", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "initialized"
        assert data["next_steps_doc"] == "docs/getting-started/setup-decisions.md"

    def test_init_writes_classify_domain_keywords_example(self, tmp_path, monkeypatch):
        """The generated config carries a commented classify.domain_keywords
        example, and uncommenting it yields a config the pipeline can load.
        """
        import yaml

        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        result = runner.invoke(app, ["admin", "init"])
        assert result.exit_code == 0
        config_path = tmp_path / "config" / "config.yaml"
        text = config_path.read_text()
        # Example block present, but commented out (not active config).
        assert "# classify:" in text
        assert "#   domain_keywords:" in text
        loaded = yaml.safe_load(text) or {}
        assert "classify" not in loaded

        # Uncommenting the block produces a config the ingestion pipeline
        # accepts and the custom domain is assigned.
        from trellis.stores.registry import StoreRegistry

        config_path.write_text(
            text
            + "\nclassify:\n  domain_keywords:\n"
            + "    payments:\n      - stripe\n      - invoice\n      - chargeback\n"
        )
        registry = StoreRegistry.from_config_dir(config_dir=tmp_path / "config")
        pipeline = registry.build_ingestion_pipeline()
        merged = pipeline.classify("stripe invoice chargeback reconciliation")
        assert "payments" in merged.tags.get("domain", [])


class TestAdminHealth:
    def test_health_uninitialized(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        result = runner.invoke(app, ["admin", "health"])
        assert result.exit_code == 0

    def test_health_after_init(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        runner.invoke(app, ["admin", "init"])
        result = runner.invoke(app, ["admin", "health"])
        assert result.exit_code == 0

    def test_health_json_format(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        runner.invoke(app, ["admin", "init"])
        result = runner.invoke(app, ["admin", "health", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["config"] is True
        assert data["data_dir"] is True


class TestAppStructure:
    def test_help(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "Trellis" in result.stdout

    def test_admin_help(self):
        result = runner.invoke(app, ["admin", "--help"])
        assert result.exit_code == 0
        assert "init" in result.stdout
        assert "health" in result.stdout

    def test_command_groups_exist(self):
        result = runner.invoke(app, ["--help"])
        for group in ["admin", "ingest", "curate", "retrieve", "analyze", "worker"]:
            assert group in result.stdout


_SENTINEL_LLM_CLIENT = object()


def _make_registry(
    *,
    llm_client=_SENTINEL_LLM_CLIENT,
    provider: str | None = "openai",
    model: str | None = "gpt-4o-mini",
):
    """Construct a mock StoreRegistry for check-extractors tests.

    ``llm_client=None`` simulates a non-configurable LLM. Anything
    non-``None`` (the default sentinel) simulates a buildable client.
    """
    reg = MagicMock()
    reg.build_llm_client.return_value = llm_client
    reg._llm_config = {"provider": provider, "model": model}
    reg.graph_store = MagicMock()
    return reg


class TestCheckExtractorsReady:
    @patch("trellis_cli.admin._get_registry")
    def test_ready_exit_zero(self, mock_get_reg, monkeypatch):
        mock_get_reg.return_value = _make_registry()
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", "1")
        result = runner.invoke(admin_app, ["check-extractors"])
        assert result.exit_code == 0
        assert "READY" in result.stdout

    @patch("trellis_cli.admin._get_registry")
    def test_ready_json(self, mock_get_reg, monkeypatch):
        mock_get_reg.return_value = _make_registry()
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", "1")
        result = runner.invoke(admin_app, ["check-extractors", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ready"
        assert data["exit_code"] == 0
        assert data["llm_client"]["config_buildable"] is True
        assert data["llm_client"]["provider"] == "openai"
        assert data["llm_client"]["model"] == "gpt-4o-mini"
        assert data["llm_client"]["env_fallback_available"] is True
        assert data["feature_flag"]["name"] == "TRELLIS_ENABLE_MEMORY_EXTRACTION"
        assert data["feature_flag"]["set"] is True
        assert data["dependencies"]["alias_resolver"] is True
        assert data["dependencies"]["llm_client"] is True
        assert data["dependencies"]["memory_prompt"] is True
        assert data["warnings"] == []


class TestCheckExtractorsBlocked:
    # Pre-ADR convention used exit code 2 to mean "BLOCKED" here. Code 2
    # is now reserved for validation errors (see
    # docs/design/adr-cli-exit-codes.md). BLOCKED is a deployment-
    # misconfiguration probe failure rather than a validation / policy /
    # store error, so it collapses to EXIT_INTERNAL (1) under the new
    # map. The assertions below were updated from 2 -> 1 with the
    # implementation in PR #refactor-cli-adopt-exit-code-constants.
    @patch("trellis_cli.admin._get_registry")
    def test_flag_set_no_llm_anywhere_exits_two(self, mock_get_reg, monkeypatch):
        mock_get_reg.return_value = _make_registry(
            llm_client=None, provider=None, model=None
        )
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", "1")
        result = runner.invoke(admin_app, ["check-extractors"])
        assert result.exit_code == 1  # was 2 pre-ADR; see class docstring
        assert "BLOCKED" in result.stdout

    @patch("trellis_cli.admin._get_registry")
    def test_blocked_json(self, mock_get_reg, monkeypatch):
        mock_get_reg.return_value = _make_registry(
            llm_client=None, provider=None, model=None
        )
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", "1")
        result = runner.invoke(admin_app, ["check-extractors", "--format", "json"])
        assert result.exit_code == 1  # was 2 pre-ADR; see class docstring
        data = json.loads(result.stdout.strip())
        assert data["status"] == "blocked"
        assert data["exit_code"] == 1  # was 2 pre-ADR; see class docstring
        assert data["llm_client"]["config_buildable"] is False
        assert data["llm_client"]["env_fallback_available"] is False
        assert any(w["signal"] == "no_llm_client" for w in data["warnings"])


class TestCheckExtractorsWarn:
    @patch("trellis_cli.admin._get_registry")
    def test_flag_unset_but_llm_buildable(self, mock_get_reg, monkeypatch):
        mock_get_reg.return_value = _make_registry()
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.delenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", raising=False)
        result = runner.invoke(admin_app, ["check-extractors"])
        assert result.exit_code == 1
        assert "WARN" in result.stdout

    @patch("trellis_cli.admin._get_registry")
    def test_flag_unset_json(self, mock_get_reg, monkeypatch):
        mock_get_reg.return_value = _make_registry()
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.delenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", raising=False)
        result = runner.invoke(admin_app, ["check-extractors", "--format", "json"])
        assert result.exit_code == 1
        data = json.loads(result.stdout.strip())
        assert data["status"] == "warn"
        assert data["exit_code"] == 1
        assert any(w["signal"] == "flag_unset" for w in data["warnings"])

    @patch("trellis_cli.admin._get_registry")
    def test_env_fallback_only(self, mock_get_reg, monkeypatch):
        mock_get_reg.return_value = _make_registry(
            llm_client=None, provider=None, model=None
        )
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", "1")
        result = runner.invoke(admin_app, ["check-extractors", "--format", "json"])
        assert result.exit_code == 1
        data = json.loads(result.stdout.strip())
        assert data["status"] == "warn"
        assert any(w["signal"] == "env_fallback_only" for w in data["warnings"])


# ---------------------------------------------------------------------------
# draft-promotion-adr (self-improvement item 5)
# ---------------------------------------------------------------------------


class TestDraftPromotionAdr:
    """Tests for ``trellis admin draft-promotion-adr <candidate_id>``."""

    def _seed_candidate_event(
        self,
        tmp_path,
        monkeypatch,
        *,
        candidate_id: str = "wkc_ent_test1234567890ab",
        open_string: str = "dbt_model",
        suggested_canonical: str = "DbtModel",
        kind: str = "entity_type",
    ):
        """Emit a WELL_KNOWN_CANDIDATE event and return paths used by the test."""
        config_dir = tmp_path / "config"
        data_dir = tmp_path / "data"
        stores_dir = data_dir / "stores"
        stores_dir.mkdir(parents=True)
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))

        # Initialize a fresh registry so the event_log is real.
        from trellis.stores.base.event_log import EventType
        from trellis.stores.registry import StoreRegistry
        from trellis_cli.stores import _reset_registry

        _reset_registry()
        registry = StoreRegistry(stores_dir=stores_dir)
        registry.operational.event_log.emit(
            EventType.WELL_KNOWN_CANDIDATE,
            source="learning.schema_evolution",
            entity_id=candidate_id,
            entity_type=kind,
            payload={
                "candidate_id": candidate_id,
                "candidate_kind": kind,
                "open_string_value": open_string,
                "count": 500,
                "distinct_extractors": ["worker:dbt", "worker:lineage"],
                "distinct_domains": ["analytics", "finance"],
                "avg_signal_quality": "standard",
                "first_seen": "2026-04-01T00:00:00+00:00",
                "last_seen": "2026-05-11T00:00:00+00:00",
                "suggested_canonical_name": suggested_canonical,
                "suggested_alignment_uri": None,
                "naming_collision": False,
                "recurrence_count": 0,
                "notes": [],
            },
        )
        registry.close()
        _reset_registry()
        return config_dir, data_dir

    def test_drafts_adr_to_default_path(self, tmp_path, monkeypatch):
        cid = "wkc_ent_t111111111111111"
        self._seed_candidate_event(tmp_path, monkeypatch, candidate_id=cid)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(admin_app, ["draft-promotion-adr", cid])
        assert result.exit_code == 0, result.output
        output_path = tmp_path / "docs" / "design" / "adr-promote-dbt_model.md"
        assert output_path.exists()
        content = output_path.read_text(encoding="utf-8")
        # Sanity check: template variables were substituted.
        assert cid in content
        assert "dbt_model" in content
        assert "DbtModel" in content
        assert "Decision" in content
        # The candidate evidence rendered.
        assert "500" in content  # count
        assert "worker:dbt" in content

    def test_refuses_to_overwrite_without_force(self, tmp_path, monkeypatch):
        cid = "wkc_ent_t222222222222222"
        self._seed_candidate_event(tmp_path, monkeypatch, candidate_id=cid)
        monkeypatch.chdir(tmp_path)
        result1 = runner.invoke(admin_app, ["draft-promotion-adr", cid])
        assert result1.exit_code == 0
        # Second invocation without --force fails.
        result2 = runner.invoke(admin_app, ["draft-promotion-adr", cid])
        assert result2.exit_code == 1, result2.output
        assert "Refusing to overwrite" in result2.output

    def test_force_overwrites_existing_file(self, tmp_path, monkeypatch):
        cid = "wkc_ent_t333333333333333"
        self._seed_candidate_event(tmp_path, monkeypatch, candidate_id=cid)
        monkeypatch.chdir(tmp_path)
        runner.invoke(admin_app, ["draft-promotion-adr", cid])
        output_path = tmp_path / "docs" / "design" / "adr-promote-dbt_model.md"
        output_path.write_text("STALE", encoding="utf-8")
        result = runner.invoke(admin_app, ["draft-promotion-adr", cid, "--force"])
        assert result.exit_code == 0, result.output
        assert "STALE" not in output_path.read_text(encoding="utf-8")

    def test_naming_collision_raises(self, tmp_path, monkeypatch):
        # Seed a candidate whose suggested name collides with the
        # canonical PERSON in well_known.
        cid = "wkc_ent_t444444444444444"
        self._seed_candidate_event(
            tmp_path,
            monkeypatch,
            candidate_id=cid,
            open_string="PERSON",
            suggested_canonical="Person",
        )
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(admin_app, ["draft-promotion-adr", cid])
        assert result.exit_code != 0
        assert "collide" in result.output.lower() or "person" in result.output.lower()

    def test_canonical_name_override_passes_through(self, tmp_path, monkeypatch):
        cid = "wkc_ent_t555555555555555"
        self._seed_candidate_event(
            tmp_path,
            monkeypatch,
            candidate_id=cid,
            open_string="dbt_model",
            suggested_canonical="DbtModel",
        )
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            admin_app,
            [
                "draft-promotion-adr",
                cid,
                "--canonical-name",
                "DbtBuildArtifact",
            ],
        )
        assert result.exit_code == 0, result.output
        output_path = tmp_path / "docs" / "design" / "adr-promote-dbt_model.md"
        content = output_path.read_text(encoding="utf-8")
        assert "DbtBuildArtifact" in content
        assert "DBT_BUILD_ARTIFACT" in content  # constant name derivation

    def test_unknown_candidate_id_errors(self, tmp_path, monkeypatch):
        # Seed nothing, then look up a non-existent id.
        config_dir = tmp_path / "config"
        data_dir = tmp_path / "data"
        stores_dir = data_dir / "stores"
        stores_dir.mkdir(parents=True)
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
        from trellis_cli.stores import _reset_registry

        _reset_registry()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            admin_app, ["draft-promotion-adr", "wkc_ent_nonexistent"]
        )
        assert result.exit_code == 1
        assert "No WELL_KNOWN_CANDIDATE" in result.output

    def test_json_output(self, tmp_path, monkeypatch):
        cid = "wkc_ent_t666666666666666"
        self._seed_candidate_event(tmp_path, monkeypatch, candidate_id=cid)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            admin_app, ["draft-promotion-adr", cid, "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        # structlog may emit pre-empt store-open logs before our JSON
        # write; the JSON object is always the final non-blank line.
        json_line = next(
            line
            for line in reversed(result.stdout.splitlines())
            if line.strip().startswith("{")
        )
        data = json.loads(json_line)
        assert data["status"] == "ok"
        assert data["candidate_id"] == cid
        assert "adr-promote-dbt_model.md" in data["output_path"]
        assert data["bytes_written"] > 0
