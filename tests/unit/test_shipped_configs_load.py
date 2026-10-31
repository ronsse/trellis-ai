"""Every config.yaml Trellis ships or generates still loads.

``StoreRegistry.from_config_dir`` refuses a top-level key nothing reads, a
store name no plane has and a store under the wrong plane. A config the repo
itself hands an operator must never trip those, or the refusal is the bug.
Each file here is loaded through the real ``from_config_dir`` and, so that a
file which loads by being empty proves nothing, asserted to configure the
backends it documents.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from typer.testing import CliRunner

from trellis.stores.registry import StoreRegistry
from trellis_cli.main import app

if TYPE_CHECKING:
    import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_CONFIG = REPO_ROOT / "deploy" / "config.compose.yaml"
AWS_EXAMPLE = REPO_ROOT / "docs" / "deployment" / "config.yaml.aws.example"
RECOMMENDED = REPO_ROOT / "docs" / "deployment" / "recommended-config.yaml"

#: The in-tree files that are a config.yaml, or hold one in comments.
SHIPPED = {DEPLOY_CONFIG, AWS_EXAMPLE, RECOMMENDED}

_TOP_LEVEL_KEY = re.compile(r"^# [a-z_]+:\s*(#.*)?$")


def _registry_for(tmp_path: Path, text: str) -> StoreRegistry:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(text, encoding="utf-8")
    return StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )


def _commented_blocks(text: str) -> list[str]:
    """Return each section's commented-out top-level YAML in ``text``, uncommented.

    A block opens at a ``# key:`` line and runs over the ``#   indented``
    lines under it; the next prose line, rule or blank line closes it. The
    blocks of one ``# =====`` section are joined, since an operator copies a
    section whole (``knowledge:`` and ``operational:`` together).
    """
    sections: list[list[list[str]]] = []
    current: list[list[str]] = []
    block: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("# ====="):
            if current:
                sections.append(current)
            current, block = [], None
        elif _TOP_LEVEL_KEY.match(line):
            block = [line[2:]]
            current.append(block)
        elif block is not None and re.match(r"^#  +\S", line):
            block.append(line[2:])
        else:
            block = None
    if current:
        sections.append(current)
    return ["\n".join("\n".join(b) for b in section) + "\n" for section in sections]


def test_the_shipped_config_roster_is_complete() -> None:
    found = {
        path
        for pattern in ("deploy/*.yaml", "docs/deployment/*.yaml*")
        for path in REPO_ROOT.glob(pattern)
    }
    assert found == SHIPPED, f"a config file is not covered here: {found ^ SHIPPED}"


def test_the_compose_config_loads(tmp_path: Path) -> None:
    registry = _registry_for(tmp_path, DEPLOY_CONFIG.read_text(encoding="utf-8"))

    assert registry.configured_backend("graph") == "postgres"
    assert registry.configured_backend("vector") == "pgvector"
    assert registry.configured_backend("document") == "postgres"
    assert registry.configured_backend("blob") == "local"
    assert registry.configured_backend("trace") == "postgres"
    assert registry.configured_backend("event_log") == "postgres"


def test_the_aws_example_loads(tmp_path: Path) -> None:
    registry = _registry_for(tmp_path, AWS_EXAMPLE.read_text(encoding="utf-8"))

    assert registry.configured_backend("document") == "postgres"
    assert registry.configured_backend("blob") == "s3"


def test_the_recommended_config_blocks_load(tmp_path: Path) -> None:
    sections = _commented_blocks(RECOMMENDED.read_text(encoding="utf-8"))
    # Four store shapes and the trailing llm/classify template.
    assert len(sections) >= 4, sections
    loaded = 0
    for index, text in enumerate(sections):
        data = yaml.safe_load(text)
        assert isinstance(data, dict), text
        if "knowledge" not in data:
            continue
        registry = _registry_for(tmp_path / str(index), text)
        for plane in ("knowledge", "operational"):
            for store_type, cfg in data.get(plane, {}).items():
                assert registry.configured_backend(store_type) == cfg["backend"]
        loaded += 1
    assert loaded >= 4


def test_admin_init_writes_a_config_that_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))

    result = CliRunner().invoke(app, ["admin", "init"])

    assert result.exit_code == 0, result.output
    written = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
    assert set(written) == {"data_dir", "default_domain", "default_agent", "format"}
    registry = StoreRegistry.from_config_dir()
    assert registry.stores_dir == tmp_path / "data" / "stores"
    assert registry.configured_backend("graph") == "sqlite"


def test_the_templates_admin_init_appends_load_uncommented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``init`` appends commented ``llm:`` and ``classify:`` blocks to fill in."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    assert CliRunner().invoke(app, ["admin", "init"]).exit_code == 0
    text = (config_dir / "config.yaml").read_text(encoding="utf-8")
    uncommented = "\n".join(line.removeprefix("# ") for line in text.splitlines())

    assert {"llm", "classify"} <= set(yaml.safe_load(uncommented))
    _registry_for(tmp_path / "filled", uncommented)
