"""Eval scenario runner.

Discovers scenario packages under :mod:`eval.scenarios`, runs each against
a :class:`StoreRegistry`, and writes JSON + Markdown reports under
``eval/reports/``.

Each scenario lives at ``eval/scenarios/<name>/scenario.py`` and exposes a
single ``run(registry) -> ScenarioReport`` callable. The runner is
intentionally thin — scenario logic, fixtures, and metric computation
belong inside each scenario package, not here.

Run from the repo root::

    python -m eval.runner --scenario _example
    python -m eval.runner --scenario all --config-dir ~/.trellis

Or programmatically::

    from eval.runner import run_scenario, list_scenarios
    report = run_scenario("multi_backend_equivalence", registry)
"""

from __future__ import annotations

import argparse
import importlib
import json
import pkgutil
import sys
import traceback
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import structlog

from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)


SCENARIOS_PACKAGE = "eval.scenarios"
REPORTS_DIR = Path(__file__).parent / "reports"


# ---------------------------------------------------------------------------
# Report types
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """A single observation surfaced by a scenario.

    Severity is purely advisory: ``info`` for context, ``warn`` for
    something a human should review, ``fail`` for a hard regression.
    """

    severity: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScenarioReport:
    """The structured result a scenario returns to the runner.

    Attributes
    ----------
    name:
        Scenario identifier (matches the package name under
        ``eval/scenarios/``).
    status:
        ``pass`` / ``fail`` / ``regress`` / ``skip``. ``regress`` means
        the scenario completed but a metric crossed a threshold. ``skip``
        means the scenario was deliberately not exercised (e.g. a
        backend not configured in the registry).
    metrics:
        Free-form numeric metrics. Keys are stable; values are scalars.
    findings:
        Human-readable observations, severity-tagged.
    decision:
        Free-form prose: which Phase 3 deferred item this run informs,
        and what the recommendation is. The point of the eval harness is
        decisions, not metrics — this field is what a human reads first.
    duration_seconds:
        Wall time spent inside the scenario's ``run()`` call.
    """

    name: str
    status: str
    metrics: dict[str, float] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    decision: str = ""
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class ScenarioModule(Protocol):
    """Shape every scenario module must satisfy."""

    def run(self, registry: StoreRegistry) -> ScenarioReport: ...


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def list_scenarios() -> list[str]:
    """Return scenario names discovered under :mod:`eval.scenarios`.

    Filters out private packages (``_example`` is included intentionally
    so the smoke test has something to exercise; future private fixtures
    starting with ``__`` are skipped).
    """
    pkg = importlib.import_module(SCENARIOS_PACKAGE)
    names = [
        info.name
        for info in pkgutil.iter_modules(pkg.__path__)
        if info.ispkg and not info.name.startswith("__")
    ]
    names.sort()
    return names


def _load_scenario(name: str) -> Callable[[StoreRegistry], ScenarioReport]:
    """Import ``eval.scenarios.<name>.scenario`` and return its ``run``."""
    module_path = f"{SCENARIOS_PACKAGE}.{name}.scenario"
    module = importlib.import_module(module_path)
    run = getattr(module, "run", None)
    if not callable(run):
        msg = f"scenario module {module_path!r} missing callable run()"
        raise TypeError(msg)
    return run  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def run_scenario(name: str, registry: StoreRegistry) -> ScenarioReport:
    """Run a single scenario by name.

    Exceptions raised by the scenario are caught and turned into a
    ``fail`` report with a ``finding`` containing the traceback — the
    runner never propagates scenario failures, because we want every
    scenario in a multi-scenario run to execute even if an earlier one
    blew up.
    """
    started = datetime.now(UTC)
    try:
        run = _load_scenario(name)
        report = run(registry)
    except Exception as exc:
        elapsed = (datetime.now(UTC) - started).total_seconds()
        logger.exception("eval.scenario_crashed", scenario=name)
        return ScenarioReport(
            name=name,
            status="fail",
            findings=[
                Finding(
                    severity="fail",
                    message=f"scenario raised {type(exc).__name__}: {exc}",
                    detail={"traceback": traceback.format_exc()},
                )
            ],
            duration_seconds=elapsed,
        )

    elapsed = (datetime.now(UTC) - started).total_seconds()
    if report.duration_seconds == 0.0:
        report.duration_seconds = elapsed
    return report


def run_scenarios(
    names: Iterable[str], registry: StoreRegistry
) -> list[ScenarioReport]:
    """Run multiple scenarios in order against the same registry."""
    return [run_scenario(name, registry) for name in names]


# ---------------------------------------------------------------------------
# Report writing
# ---------------------------------------------------------------------------


def write_report(
    reports: list[ScenarioReport],
    out_dir: Path | None = None,
) -> tuple[Path, Path]:
    """Write JSON + Markdown reports; return ``(json_path, md_path)``."""
    out_dir = out_dir or REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    json_path = out_dir / f"report-{timestamp}.json"
    md_path = out_dir / f"report-{timestamp}.md"

    payload = {
        "generated_at": timestamp,
        "scenarios": [r.to_dict() for r in reports],
    }
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    md_path.write_text(_render_markdown(reports, timestamp))
    return json_path, md_path


def _render_markdown(reports: list[ScenarioReport], timestamp: str) -> str:
    lines: list[str] = [f"# Eval report — {timestamp}", ""]
    for r in reports:
        lines.append(f"## {r.name} — `{r.status}`")
        lines.append("")
        lines.append(f"_Duration: {r.duration_seconds:.2f}s_")
        lines.append("")
        if r.decision:
            lines.append(f"**Decision:** {r.decision}")
            lines.append("")
        if r.metrics:
            lines.append("### Metrics")
            lines.extend(f"- `{k}`: {r.metrics[k]}" for k in sorted(r.metrics))
            lines.append("")
        if r.findings:
            lines.append("### Findings")
            lines.extend(f"- **{f.severity}**: {f.message}" for f in r.findings)
            lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.runner",
        description="Run Trellis evaluation scenarios.",
    )
    parser.add_argument(
        "--scenario",
        default="_example",
        help=(
            "Scenario name, comma-separated names, or 'all'. Defaults to "
            "the no-op '_example' scenario used for harness smoke tests."
        ),
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help=(
            "Trellis config dir (passed to StoreRegistry.from_config_dir). "
            "Defaults to TRELLIS_CONFIG_DIR or ~/.trellis."
        ),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Trellis data dir override.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List discovered scenarios and exit.",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Print results to stdout but do not write files under eval/reports/.",
    )
    return parser


def _resolve_names(arg: str) -> list[str]:
    if arg == "all":
        return list_scenarios()
    return [n.strip() for n in arg.split(",") if n.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.list:
        for name in list_scenarios():
            print(name)
        return 0

    names = _resolve_names(args.scenario)
    if not names:
        parser.error("--scenario must name at least one scenario")

    with StoreRegistry.from_config_dir(args.config_dir, args.data_dir) as registry:
        reports = run_scenarios(names, registry)

    if not args.no_write:
        json_path, md_path = write_report(reports)
        print(f"wrote {json_path}")
        print(f"wrote {md_path}")

    fails = [r for r in reports if r.status in {"fail", "regress"}]
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
