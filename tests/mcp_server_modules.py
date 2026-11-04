"""The modules ``trellis.mcp.server`` is split into, read as one source.

``src/trellis/mcp/server.py`` used to hold every MCP tool and helper. Several
AST rules keyed on that one path (the capture-surface roster, the sensitivity
roster, the access-log roster) because the properties they police are
properties of *the tool surface*, not of a file. The code now lives in sibling
modules, so those rules read this population instead of ``server.py`` alone.

The population is one logical module: the sources are concatenated, so a rule
that resolved a helper by name across the old file resolves it across the new
ones too, and a name defined in two of them would show up as a duplicate.
``server.py`` is part of it because it still holds ``main`` and the signal
handlers; a rule scanning only the façade would find no tools at all and pass.
"""

from __future__ import annotations

from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1] / "src" / "trellis" / "mcp"

#: Flat modules, then ``tools/*.py`` (globbed so a new tool module is scanned
#: without anyone remembering to add it here).
_FLAT_MODULES = ("server.py", "app.py", "boundary.py", "rejections.py", "wiring.py")

#: Hand-read floor: the five flat modules above plus the ``tools/`` package's
#: ``__init__.py`` and its ten family modules. A glob that quietly matched
#: nothing would leave every rule scanning only the façade.
MODULE_COUNT_FLOOR = 16


def server_module_paths() -> list[Path]:
    paths = [MCP_DIR / name for name in _FLAT_MODULES]
    paths += sorted((MCP_DIR / "tools").glob("*.py"))
    missing = [p for p in paths if not p.is_file()]
    assert not missing, f"server modules not found: {missing}"
    assert len(paths) >= MODULE_COUNT_FLOOR, (
        f"found {len(paths)} server modules, expected at least {MODULE_COUNT_FLOOR}"
    )
    return paths


def server_source() -> str:
    """Every server module's text, concatenated in a stable order."""
    return "\n".join(p.read_text(encoding="utf-8") for p in server_module_paths())
