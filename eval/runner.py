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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import structlog

from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)


SCENARIOS_PACKAGE = "eval.scenarios"
REPORTS_DIR = Path(__file__).parent / "reports"

Severity = Literal["info", "warn", "fail"]
ScenarioStatus = Literal["pass", "fail", "regress", "skip"]


# ---------------------------------------------------------------------------
# Report types
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """A single observation surfaced by a scenario.

    Severity is purely advisory: ``info`` for context, ``warn`` for
    something a human should review, ``fail`` for a hard regression.
    """

    severity: Severity
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
        Free-form scalar metrics. Keys are stable; values are usually
        ``float``, with ``str`` allowed for path-like grep-targets
        (e.g. ``chart_path`` on the ``program_convergence`` master
        scenario when ``render_chart=True``). Reports treat
        non-numeric metrics as opaque values — no arithmetic is
        applied to them. Typed as :class:`Mapping` (covariant in the
        value type) so existing scenarios that declare their local
        ``metrics`` dict as ``dict[str, float]`` remain assignable —
        ``dict`` is invariant, so a ``dict[str, float | str]`` field
        annotation would force every scenario to widen its local
        annotation.
    findings:
        Human-readable observations, severity-tagged.
    decision:
        Free-form prose: which Phase 3 deferred item this run informs,
        and what the recommendation is. The point of the eval harness is
        decisions, not metrics — this field is what a human reads first.
    duration_seconds:
        Wall time spent inside the scenario's ``run()`` call.
    convergence_stats:
        Optional in-memory payload a scenario can attach for downstream
        callers (e.g. the ``program_convergence`` master scenario sets
        this to the ``_MultiAxisStats`` instance it built so a post-hoc
        caller can re-render the chart without re-running the loop).
        Typed as ``Any`` to keep the runner free of scenario-specific
        imports. Excluded from :meth:`to_dict` to keep the JSON report
        slim — operators consume this field in-process, not from disk.
    """

    name: str
    status: ScenarioStatus
    metrics: Mapping[str, float | str] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    decision: str = ""
    duration_seconds: float = 0.0
    convergence_stats: Any = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("convergence_stats", None)
        return payload


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


def run_scenario(
    name: str,
    registry: StoreRegistry,
    **scenario_kwargs: Any,
) -> ScenarioReport:
    """Run a single scenario by name.

    ``scenario_kwargs`` are forwarded as keyword arguments to the
    scenario's ``run`` callable. If the scenario does not accept a
    given kwarg, the resulting ``TypeError`` is captured as a ``fail``
    report finding (same loud-failure path as any other scenario
    exception) — the runner does not silently drop unknown kwargs.

    Exceptions raised by the scenario are caught and turned into a
    ``fail`` report with a ``finding`` containing the traceback — the
    runner never propagates scenario failures, because we want every
    scenario in a multi-scenario run to execute even if an earlier one
    blew up.
    """
    started = datetime.now(UTC)
    try:
        run = _load_scenario(name)
        report = run(registry, **scenario_kwargs)
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

    report.duration_seconds = (
        report.duration_seconds or (datetime.now(UTC) - started).total_seconds()
    )
    return report


def run_scenarios(
    names: Iterable[str],
    registry: StoreRegistry,
    **scenario_kwargs: Any,
) -> list[ScenarioReport]:
    """Run multiple scenarios in order against the same registry.

    ``scenario_kwargs`` are forwarded unchanged to every scenario. When
    multiple scenarios are run in one invocation the same kwargs apply
    to all of them — a scenario that doesn't accept a given kwarg will
    fail loudly via the same ``TypeError`` path as any other scenario
    exception.
    """
    return [run_scenario(name, registry, **scenario_kwargs) for name in names]


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
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")

    json_path = out_dir / f"report-{timestamp}.json"
    md_path = out_dir / f"report-{timestamp}.md"

    payload = {
        "generated_at": timestamp,
        "scenarios": [r.to_dict() for r in reports],
    }
    # Force UTF-8 — on Windows ``write_text`` defaults to ``cp1252``,
    # which can't encode characters like ``→`` that scenarios may emit
    # in findings or decision text. Reports are machine artifacts; UTF-8
    # is the only sane wire format.
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    md_path.write_text(_render_markdown(reports, timestamp), encoding="utf-8")
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
            lines.extend(f"- `{k}`: {v}" for k, v in sorted(r.metrics.items()))
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
    parser.add_argument(
        "--scenario-arg",
        action="append",
        default=None,
        metavar="NAME=VALUE",
        help=(
            "Keyword argument to forward to the scenario's run() callable. "
            "Repeatable, e.g. '--scenario-arg profile=real --scenario-arg "
            "rounds=20'. Values are coerced int -> float -> bool -> str (bool "
            "accepts true/false case-insensitive). Unknown kwargs surface as a "
            "loud scenario failure, not a silent drop."
        ),
    )
    return parser


def _resolve_names(arg: str) -> list[str]:
    if arg == "all":
        return list_scenarios()
    return [n.strip() for n in arg.split(",") if n.strip()]


def _coerce_scenario_value(raw: str) -> Any:
    """Coerce a CLI string value to int -> float -> bool -> str.

    Booleans accept ``true`` / ``false`` case-insensitive. All other
    strings fall through to ``str``. Empty strings are returned as
    empty strings (not coerced) — the caller already validated the
    name half of ``name=value``; an empty value is a deliberate
    scenario-side choice (e.g. ``--scenario-arg comment=``).
    """
    # int first — ``int("1.0")`` raises, so floats fall through to the
    # next branch. ``int("true")`` raises too, so bools fall through.
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return raw


def _parse_scenario_args(items: Iterable[str] | None) -> dict[str, Any]:
    """Parse repeated ``NAME=VALUE`` flags into a ``dict[str, Any]``.

    Raises ``ValueError`` on a missing ``=`` separator or an empty /
    non-identifier name. Loud-on-parse-failure is the contract — we
    never want a typo'd flag to silently become a string-valued kwarg.
    Later occurrences of the same name overwrite earlier ones (argparse
    ``action="append"`` preserves order, so the right-most wins).
    """
    parsed: dict[str, Any] = {}
    if not items:
        return parsed
    for item in items:
        if "=" not in item:
            msg = (
                f"--scenario-arg expected NAME=VALUE; got {item!r} "
                f"(missing '=' separator)"
            )
            raise ValueError(msg)
        name, _, value = item.partition("=")
        name = name.strip()
        if not name or not name.isidentifier():
            msg = (
                f"--scenario-arg name must be a non-empty Python identifier; "
                f"got {name!r} from {item!r}"
            )
            raise ValueError(msg)
        parsed[name] = _coerce_scenario_value(value)
    return parsed


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

    try:
        scenario_kwargs = _parse_scenario_args(args.scenario_arg)
    except ValueError as exc:
        parser.error(str(exc))

    with StoreRegistry.from_config_dir(args.config_dir, args.data_dir) as registry:
        reports = run_scenarios(names, registry, **scenario_kwargs)

    if not args.no_write:
        json_path, md_path = write_report(reports)
        print(f"wrote {json_path}")
        print(f"wrote {md_path}")

    fails = [r for r in reports if r.status in {"fail", "regress"}]
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
