"""Tests for ``trellis.extract.sources`` — the sources.yaml registry."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from trellis.extract.base import ExtractorTier
from trellis.extract.sources import (
    SourceEntry,
    SourcesConfig,
    load_sources,
)


# ---------------------------------------------------------------------------
# SourceEntry validation
# ---------------------------------------------------------------------------


class TestSourceEntry:
    def test_minimal_valid_entry(self) -> None:
        e = SourceEntry(name="jaffle", type="dbt-manifest", path="./foo.json")
        assert e.name == "jaffle"
        assert e.type == "dbt-manifest"
        assert e.path == "./foo.json"
        assert e.endpoint is None
        assert e.enabled is True
        assert e.tier_override is None
        assert e.credentials_ref is None

    def test_endpoint_form(self) -> None:
        e = SourceEntry(
            name="lineage",
            type="openlineage",
            endpoint="https://example.com/api",
        )
        assert e.endpoint == "https://example.com/api"
        assert e.path is None

    def test_path_and_endpoint_mutually_exclusive(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of"):
            SourceEntry(
                name="bad",
                type="dbt-manifest",
                path="./foo.json",
                endpoint="https://example.com",
            )

    def test_neither_path_nor_endpoint_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of"):
            SourceEntry(name="bad", type="dbt-manifest")

    def test_invalid_name_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must match"):
            SourceEntry(name="1-leading-digit", type="x", path="./a")
        with pytest.raises(ValidationError, match="must match"):
            SourceEntry(name="has spaces", type="x", path="./a")
        with pytest.raises(ValidationError, match="must match"):
            SourceEntry(name="has.dot", type="x", path="./a")

    def test_valid_names_accepted(self) -> None:
        for name in ("a", "abc", "abc123", "abc_def", "abc-def", "A_B-c1"):
            SourceEntry(name=name, type="x", path="./a")

    def test_credentials_ref_must_be_env_var_shape(self) -> None:
        # Looks like an inline secret — should fail loudly at validation
        # time, not silently propagate to the extractor.
        with pytest.raises(ValidationError, match="env-var name"):
            SourceEntry(
                name="x",
                type="dbt-manifest",
                path="./a",
                credentials_ref="sk_live_abcd1234",
            )
        with pytest.raises(ValidationError, match="env-var name"):
            SourceEntry(
                name="x",
                type="dbt-manifest",
                path="./a",
                credentials_ref="lowercase_only",
            )

    def test_credentials_ref_valid_env_var(self) -> None:
        e = SourceEntry(
            name="x",
            type="dbt-manifest",
            path="./a",
            credentials_ref="TRELLIS_LINEAGE_TOKEN",
        )
        assert e.credentials_ref == "TRELLIS_LINEAGE_TOKEN"

    def test_tier_override_accepts_enum_value(self) -> None:
        e = SourceEntry(
            name="x",
            type="dbt-manifest",
            path="./a",
            tier_override="deterministic",
        )
        assert e.tier_override is ExtractorTier.DETERMINISTIC

    def test_extra_keys_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceEntry(
                name="x",
                type="dbt-manifest",
                path="./a",
                unexpected_key="oops",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# SourcesConfig validation and helpers
# ---------------------------------------------------------------------------


class TestSourcesConfig:
    def test_empty_config_valid(self) -> None:
        cfg = SourcesConfig(sources=[])
        assert cfg.sources == []
        assert cfg.enabled() == []
        assert cfg.find("anything") is None

    def test_duplicate_names_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Duplicate source name"):
            SourcesConfig(
                sources=[
                    SourceEntry(name="dup", type="dbt-manifest", path="./a.json"),
                    SourceEntry(name="dup", type="openlineage", path="./b.json"),
                ]
            )

    def test_enabled_filter_preserves_order(self) -> None:
        cfg = SourcesConfig(
            sources=[
                SourceEntry(name="a", type="x", path="./a", enabled=True),
                SourceEntry(name="b", type="x", path="./b", enabled=False),
                SourceEntry(name="c", type="x", path="./c", enabled=True),
            ]
        )
        names = [e.name for e in cfg.enabled()]
        assert names == ["a", "c"]

    def test_find_returns_entry_or_none(self) -> None:
        cfg = SourcesConfig(
            sources=[
                SourceEntry(name="alpha", type="x", path="./a"),
                SourceEntry(name="beta", type="x", path="./b"),
            ]
        )
        assert cfg.find("alpha") is not None
        assert cfg.find("alpha").name == "alpha"  # type: ignore[union-attr]
        assert cfg.find("missing") is None


# ---------------------------------------------------------------------------
# load_sources() file loading
# ---------------------------------------------------------------------------


_VALID_YAML = """
sources:
  - name: jaffle-dbt
    type: dbt-manifest
    path: ./fixtures/dbt/manifest.json
  - name: lineage-events
    type: openlineage
    path: ./fixtures/openlineage/events.jsonl
    enabled: true
  - name: streaming-events
    type: openlineage
    endpoint: https://lineage.example.com/api/v1/events
    credentials_ref: TRELLIS_LINEAGE_TOKEN
    enabled: false
"""


class TestLoadSources:
    def test_loads_valid_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "sources.yaml"
        f.write_text(_VALID_YAML, encoding="utf-8")
        cfg = load_sources(f)
        assert len(cfg.sources) == 3
        names = [s.name for s in cfg.sources]
        assert names == ["jaffle-dbt", "lineage-events", "streaming-events"]
        assert [s.name for s in cfg.enabled()] == ["jaffle-dbt", "lineage-events"]

    def test_empty_file_returns_empty_config(self, tmp_path: Path) -> None:
        f = tmp_path / "sources.yaml"
        f.write_text("", encoding="utf-8")
        cfg = load_sources(f)
        assert cfg.sources == []

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_sources(tmp_path / "does-not-exist.yaml")

    def test_non_mapping_top_level_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "sources.yaml"
        f.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(TypeError, match="top-level YAML must be a mapping"):
            load_sources(f)

    def test_unknown_top_level_key_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "sources.yaml"
        f.write_text("sources: []\nrandom_key: oops\n", encoding="utf-8")
        with pytest.raises(ValidationError):
            load_sources(f)

    def test_accepts_string_path(self, tmp_path: Path) -> None:
        f = tmp_path / "sources.yaml"
        f.write_text("sources: []\n", encoding="utf-8")
        cfg = load_sources(str(f))
        assert cfg.sources == []
