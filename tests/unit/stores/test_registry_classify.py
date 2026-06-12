"""Tests for ``classify:`` config wiring in ``StoreRegistry`` (WP7 Part 1).

Exercise :meth:`StoreRegistry.build_ingestion_pipeline` against the
``classify.domain_keywords`` block of ``config.yaml``: a domain defined only in
config is assigned by the ingestion pipeline, and reserved policy namespaces are
rejected loudly at build time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from trellis.stores.registry import StoreRegistry


def _write_config(tmp_path: Path, classify_block: dict[str, Any] | None) -> Path:
    data: dict[str, Any] = {"stores": {}}
    if classify_block is not None:
        data["classify"] = classify_block
    config_dir = tmp_path / ".trellis"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(yaml.safe_dump(data))
    return config_dir


def test_no_classify_block_uses_defaults(tmp_path: Path) -> None:
    config_dir = _write_config(tmp_path, None)
    registry = StoreRegistry.from_config_dir(config_dir=config_dir)
    pipeline = registry.build_ingestion_pipeline()
    result = pipeline.classify("dbt spark warehouse etl transform")
    assert "data-pipeline" in result.tags.get("domain", [])


def test_config_only_domain_assigned_in_ingestion_mode(tmp_path: Path) -> None:
    config_dir = _write_config(
        tmp_path,
        {"domain_keywords": {"payments": ["stripe", "invoice", "chargeback"]}},
    )
    registry = StoreRegistry.from_config_dir(config_dir=config_dir)
    pipeline = registry.build_ingestion_pipeline()
    assert pipeline.mode == "ingestion"
    result = pipeline.classify("stripe invoice and chargeback reconciliation")
    assert "payments" in result.tags.get("domain", [])


def test_reserved_domain_in_config_rejected(tmp_path: Path) -> None:
    config_dir = _write_config(
        tmp_path, {"domain_keywords": {"sensitivity": ["secret"]}}
    )
    registry = StoreRegistry.from_config_dir(config_dir=config_dir)
    with pytest.raises(ValueError, match="reserved namespace 'sensitivity'"):
        registry.build_ingestion_pipeline()


def test_from_config_dict_carries_classify(tmp_path: Path) -> None:
    registry = StoreRegistry.from_config_dict(
        {
            "knowledge": {"document": {"backend": "sqlite"}},
            "classify": {"domain_keywords": {"widgets": ["sprocket", "gizmo"]}},
        },
        data_dir=tmp_path,
    )
    pipeline = registry.build_ingestion_pipeline()
    result = pipeline.classify("the sprocket and gizmo subassembly")
    assert "widgets" in result.tags.get("domain", [])
