"""No-op scenario used by the runner smoke test.

Reads nothing, writes nothing, asserts nothing. If this returns a
``pass`` ScenarioReport, the runner discovery + execution path works.
"""

from __future__ import annotations

from eval.runner import Finding, ScenarioReport
from trellis.stores.registry import StoreRegistry


def run(registry: StoreRegistry) -> ScenarioReport:  # noqa: ARG001 — protocol shape
    return ScenarioReport(
        name="_example",
        status="pass",
        metrics={"noop": 1.0},
        findings=[
            Finding(
                severity="info",
                message="Example scenario executed; runner harness is wired.",
            )
        ],
        decision=(
            "No decision — this scenario only proves the runner works. "
            "Real scenarios live in sibling packages."
        ),
    )
