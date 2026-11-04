"""Every agent-visible rendering of the loop is byte-identical to ``main``.

The holdout's subjects are agents, so the text they read in the server
instructions, tool descriptions, packaged prompts and skills, pack footers and
tool replies is part of the treatment environment. Until the readout is in,
serving and that text are frozen.

``golden/loop_text_surfaces.json`` was captured from ``main`` (8eae6c98) before
the loop text had a single source, by ``collect_surfaces`` on the unmodified
tree. There is deliberately no regenerate switch: a golden the code under test
can rewrite pins nothing. Change a surface on purpose only after the readout,
and replace the golden by hand in the same change, saying so in the PR.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.unit.mcp.loop_text_surfaces import collect_surfaces
from trellis.stores.registry import StoreRegistry

GOLDEN = Path(__file__).parent / "golden" / "loop_text_surfaces.json"


def _golden() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return loaded


def _flatten(prefix: str, value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        out: dict[str, str] = {}
        for key, inner in value.items():
            out.update(_flatten(f"{prefix}.{key}" if prefix else key, inner))
        return out
    return {prefix: value}


GOLDEN_KEYS = sorted(_flatten("", _golden()))


def test_the_golden_is_not_vacuous() -> None:
    flat = _flatten("", _golden())
    assert len(flat) >= 29  # hand-read: 1 + 5 + 3 + 4 + 9 + 7
    assert all(isinstance(text, str) and text for text in flat.values())


def test_the_golden_names_every_surface_the_collector_reads(
    temp_registry: StoreRegistry,
) -> None:
    assert sorted(_flatten("", collect_surfaces(temp_registry))) == GOLDEN_KEYS


@pytest.mark.parametrize("key", GOLDEN_KEYS)
def test_surface_is_byte_identical_to_main(
    key: str, temp_registry: StoreRegistry
) -> None:
    live = _flatten("", collect_surfaces(temp_registry))
    assert live[key] == _flatten("", _golden())[key]
