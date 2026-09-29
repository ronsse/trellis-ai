"""Every YAML file under ``examples/`` parses.

These files are copied, not read: ``docs/deployment/scheduled-curation.md``
tells the operator to copy ``examples/integrations/github-actions/curation.yml``
into ``.github/workflows/``. A copied file that does not parse is an invalid
workflow, and that one did not parse, because two lines of stray markup had
been appended after its last step.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.ast_rules import assert_hand_read_floor

REPO_ROOT = Path(__file__).resolve().parents[2]

# Counted by hand, not by the scan below: the curation workflow and
# ``examples/cold-start-fixture/sources.yaml``.
EXAMPLE_YAML_FLOOR = 2

EXAMPLE_YAML = sorted(
    path
    for pattern in ("*.yml", "*.yaml")
    for path in (REPO_ROOT / "examples").rglob(pattern)
)


def test_scan_finds_the_hand_counted_files() -> None:
    assert_hand_read_floor(
        len(EXAMPLE_YAML), EXAMPLE_YAML_FLOOR, subject="example YAML file"
    )


@pytest.mark.parametrize(
    "path", EXAMPLE_YAML, ids=lambda path: path.relative_to(REPO_ROOT).as_posix()
)
def test_example_yaml_parses(path: Path) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path.name} parsed to {type(document)}"
