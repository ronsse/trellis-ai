"""Parity tests: wire enums ↔ core enums.

These tests are the enforcement mechanism that keeps the two sides in
sync.  They fail loudly if:

* Core adds a value and wire doesn't (or vice versa).
* A value's string representation diverges between the two.
* A translator function mis-routes a value.

The wire package has zero runtime dependency on core, so parity is
*not* automatic — these tests are the safety net.
"""

from __future__ import annotations

import pytest

from trellis.mutate import BatchStrategy as CoreBatchStrategy
from trellis.schemas.enums import NodeRole as CoreNodeRole
from trellis.wire import (
    batch_strategy_to_core,
    batch_strategy_to_wire,
    node_role_to_core,
    node_role_to_wire,
)
from trellis_wire import BatchStrategy as WireBatchStrategy
from trellis_wire import NodeRole as WireNodeRole


class TestBatchStrategyParity:
    def test_same_members(self):
        core_members = {m.name for m in CoreBatchStrategy}
        wire_members = {m.name for m in WireBatchStrategy}
        assert core_members == wire_members, (
            f"BatchStrategy members diverged. Core only: "
            f"{core_members - wire_members}. Wire only: "
            f"{wire_members - core_members}."
        )

    def test_same_values(self):
        for name in (m.name for m in CoreBatchStrategy):
            core_value = CoreBatchStrategy[name].value
            wire_value = WireBatchStrategy[name].value
            assert core_value == wire_value, (
                f"BatchStrategy.{name}: core={core_value!r} wire={wire_value!r}"
            )

    @pytest.mark.parametrize("core_value", list(CoreBatchStrategy))
    def test_round_trip_core_wire_core(self, core_value):
        assert batch_strategy_to_core(batch_strategy_to_wire(core_value)) == core_value

    @pytest.mark.parametrize("wire_value", list(WireBatchStrategy))
    def test_round_trip_wire_core_wire(self, wire_value):
        assert batch_strategy_to_wire(batch_strategy_to_core(wire_value)) == wire_value


class TestNodeRoleParity:
    def test_same_members(self):
        core_members = {m.name for m in CoreNodeRole}
        wire_members = {m.name for m in WireNodeRole}
        assert core_members == wire_members

    def test_same_values(self):
        for name in (m.name for m in CoreNodeRole):
            assert CoreNodeRole[name].value == WireNodeRole[name].value

    @pytest.mark.parametrize("core_value", list(CoreNodeRole))
    def test_round_trip_core_wire_core(self, core_value):
        assert node_role_to_core(node_role_to_wire(core_value)) == core_value

    @pytest.mark.parametrize("wire_value", list(WireNodeRole))
    def test_round_trip_wire_core_wire(self, wire_value):
        assert node_role_to_wire(node_role_to_core(wire_value)) == wire_value


class TestWirePackageIsolation:
    """The wire package must not depend on trellis core.

    This is a *structural* invariant: if we accidentally import from
    ``trellis.*`` inside ``trellis_wire``, the client boundary story
    falls apart (clients would transitively pull in stores, etc).
    """

    def test_wire_package_has_no_core_imports(self):
        import ast
        from pathlib import Path

        wire_dir = Path(__file__).parent.parent.parent.parent / "src" / "trellis_wire"
        assert wire_dir.is_dir(), f"wire package not found at {wire_dir}"

        offenders: list[tuple[str, int, str]] = []
        for py_file in wire_dir.rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".", 1)[0]
                    if root == "trellis":
                        offenders.append((str(py_file), node.lineno, node.module))
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".", 1)[0]
                        if root == "trellis":
                            offenders.append((str(py_file), node.lineno, alias.name))
        assert not offenders, (
            f"trellis_wire must not import from trellis.*  Offenders: {offenders}"
        )
