"""Matplotlib chart renderer for the ``program_convergence`` master scenario.

Renders a 3x3 grid of subplots — one per axis A..I — from a
:class:`_MultiAxisStats` snapshot produced by the master scenario. The
subplot grid is preferred over a single-figure-with-9-lines layout
because the nine axes carry mixed units (axis A is a 0-1 weighted
score; axis F is an integer cluster count; axis H is a per-round
Activity-node delta). Per-subplot Y-scales preserve each axis's
natural units; the chart reader compares *shapes* (rises / falls /
flat) against the expected-shape annotation in each subplot's corner,
which is the property plan §2.1 cares about.

Entry point is the importable function
:func:`render_program_convergence_chart` — see plan §4.3. A CLI was
considered and rejected: the master scenario already orchestrates the
run and holds the :class:`_MultiAxisStats` in memory; forcing
serialization to JSON and back through ``argparse`` is round-trip work
for the same caller. Operators who want a chart after a run import
this function from the same Python session.

POC directives applied:
- No silent fallback. If ``matplotlib`` is not installed the module
  raises :class:`ImportError` at import time. No try/except wrapper.
- The output PNG filename is deterministic given ``timestamp`` alone
  (``invocation_id`` appears only in the chart title, not the
  filename) — re-rendering with the same ``timestamp`` overwrites the
  prior PNG at the same path, so a re-invocation is idempotent. Two
  distinct invocations rendered at the same wall-clock second will
  collide on the same filename; pass a distinct ``timestamp`` if you
  want both PNGs side-by-side.
- Empty axes (a track with zero records) render as a labelled-but-blank
  subplot rather than raising; the chart's job is to surface the gap,
  not crash on it. A track with a single round renders as a single
  marker.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import matplotlib as mpl

# The renderer must work in CI / headless contexts where no display
# server is available. ``Agg`` is the non-interactive backend that
# writes directly to a file buffer; selecting it before any pyplot
# import is the documented matplotlib pattern.
mpl.use("Agg")

import matplotlib.pyplot as plt
import structlog

if TYPE_CHECKING:
    from eval.scenarios._convergence_common import _MultiAxisStats

logger = structlog.get_logger(__name__)


#: Subplot layout — 3 rows x 3 columns matches the nine axes and keeps
#: per-subplot area legible at the default figsize. Order matches
#: ``NINE_AXIS_LABELS`` so axis A is top-left and axis I is bottom-right.
_SUBPLOT_ROWS = 3
_SUBPLOT_COLS = 3
_FIGSIZE_INCHES = (15.0, 11.0)
_DPI = 100

#: ISO-8601-with-Z compact form used in PNG filenames. The colons
#: matter for ISO-8601 conformance but are illegal in Windows filenames,
#: so the filename uses ``HHMMSSZ`` — still ISO-8601-ish, still
#: portable. Example: ``2026-05-15T103045Z``.
_FILENAME_TIMESTAMP_FORMAT = "%Y-%m-%dT%H%M%SZ"

#: Human-facing axis titles for each of the nine subplots. Pulled from
#: plan §2.1; order matches ``NINE_AXIS_LABELS``.
_AXIS_DISPLAY_TITLES: dict[str, str] = {
    "A_pack_quality": "A. Pack quality",
    "B_useful_item_fraction": "B. Useful-item fraction",
    "C_advisory_hit_rate": "C. Advisory hit rate",
    "D_observation_enrichment": "D. Observations / round",
    "E_provenance_queryability": "E. Provenance queryable",
    "F_extraction_failure_clusters": "F. Open EXTRACTION_FAILED clusters",
    "G_schema_evolution_candidates": "G. WELL_KNOWN_CANDIDATEs / round",
    "H_meta_trace_density": "H. Meta-trace density",
    "I_self_authored_proposals": "I. PROPOSAL_DRAFTEDs / round",
}

#: Expected shape per axis, from plan §2.1. Rendered as a small
#: annotation in each subplot corner so a chart reader can compare
#: observed shape against expected without flipping back to the doc.
_AXIS_EXPECTED_SHAPE: dict[str, str] = {
    "A_pack_quality": "expected: rises",
    "B_useful_item_fraction": "expected: rises",
    "C_advisory_hit_rate": "expected: rises",
    "D_observation_enrichment": "expected: rises → plateau",
    "E_provenance_queryability": "expected: flat at 1.0",
    "F_extraction_failure_clusters": "expected: falls",
    "G_schema_evolution_candidates": "expected: rises",
    "H_meta_trace_density": "expected: flat (sampling cap)",
    "I_self_authored_proposals": "expected: rises",
}


def render_program_convergence_chart(
    stats: _MultiAxisStats,
    *,
    output_dir: Path,
    invocation_id: str,
    timestamp: datetime | None = None,
) -> Path:
    """Render the 9-axis convergence chart to a PNG and return its path.

    Args:
        stats: Multi-axis statistics from the master scenario run.
            ``stats.axes`` must carry one ``_AxisTrack`` per label in
            ``NINE_AXIS_LABELS``; missing axes render as blank-but-titled
            subplots.
        output_dir: Directory the PNG is written to. Created with
            ``parents=True, exist_ok=True`` — the renderer takes
            responsibility for the directory because tests use
            ``tmp_path`` and operators use ``eval/reports/`` and both
            paths should "just work".
        invocation_id: Run-identifier surfaced in the chart title. The
            same identifier the runner emits in ``ScenarioReport``.
            Note: not encoded in the filename — see module docstring.
        timestamp: When the chart is being rendered. Defaults to
            ``datetime.now(UTC)``. The filename derives from this
            value alone, so re-rendering with the same ``timestamp``
            overwrites the prior PNG (idempotent re-invocation).
            Exposed so tests can pin the value and the
            re-render-overwrites property is observable.

    Returns:
        Absolute path to the written PNG.

    Raises:
        ValueError: If ``invocation_id`` is empty / whitespace, or
            ``output_dir`` is not a directory we can create.
    """
    from eval.scenarios._convergence_common import NINE_AXIS_LABELS  # noqa: PLC0415

    if not invocation_id or not invocation_id.strip():
        msg = "invocation_id must be a non-empty string"
        raise ValueError(msg)

    ts = timestamp if timestamp is not None else datetime.now(UTC)
    if ts.tzinfo is None:
        msg = "timestamp must be timezone-aware (UTC); got naive datetime"
        raise ValueError(msg)

    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"program_convergence_{ts.strftime(_FILENAME_TIMESTAMP_FORMAT)}.png"
    output_path = output_dir / filename

    rounds_count = _infer_rounds_count(stats)

    palette = plt.get_cmap("tab10")
    fig, axes_grid = plt.subplots(
        nrows=_SUBPLOT_ROWS,
        ncols=_SUBPLOT_COLS,
        figsize=_FIGSIZE_INCHES,
        dpi=_DPI,
        squeeze=False,
    )
    fig.suptitle(
        f"Program convergence — {rounds_count} rounds — {invocation_id}",
        fontsize=14,
        fontweight="bold",
    )

    for index, label in enumerate(NINE_AXIS_LABELS):
        row = index // _SUBPLOT_COLS
        col = index % _SUBPLOT_COLS
        ax = axes_grid[row][col]
        _render_axis(
            ax,
            stats=stats,
            label=label,
            color=palette(index),
        )

    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.96))
    fig.savefig(output_path, format="png", dpi=_DPI)
    plt.close(fig)

    logger.info(
        "program_convergence_chart_rendered",
        output_path=str(output_path),
        invocation_id=invocation_id,
        rounds=rounds_count,
        timestamp=ts.isoformat(),
    )
    return output_path


def _render_axis(
    ax: plt.Axes,
    *,
    stats: _MultiAxisStats,
    label: str,
    color: tuple[float, float, float, float],
) -> None:
    """Draw one subplot for ``label`` onto ``ax``.

    Pulls the per-round values from ``stats.axes[label]``. A missing or
    empty track renders the title and expected-shape annotation but
    leaves the plot blank; that's the visible signal that this axis's
    machinery didn't fire.
    """
    title = _AXIS_DISPLAY_TITLES.get(label, label)
    expected = _AXIS_EXPECTED_SHAPE.get(label, "")

    ax.set_title(title, fontsize=10, fontweight="bold")
    ax.set_xlabel("round", fontsize=8)
    ax.tick_params(labelsize=8)
    ax.grid(visible=True, alpha=0.3)

    track = stats.axes.get(label)
    if track is None or not track.records:
        ax.text(
            0.5,
            0.5,
            "(no data)",
            transform=ax.transAxes,
            ha="center",
            va="center",
            fontsize=10,
            color="#888888",
        )
    else:
        x_values = [record.round_index for record in track.records]
        y_values = [record.value for record in track.records]
        # ``marker="o"`` so a single-round track renders as a visible
        # dot rather than vanishing — same line style for all axes so
        # the eye-comparison across subplots stays clean.
        ax.plot(x_values, y_values, color=color, marker="o", linewidth=1.5)

    if expected:
        ax.text(
            0.98,
            0.02,
            expected,
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            fontsize=7,
            color="#555555",
            style="italic",
        )


def _infer_rounds_count(stats: _MultiAxisStats) -> int:
    """Return the highest ``round_index + 1`` across every populated track.

    The master scenario writes the same round_index into every axis
    track on every round, but a partial run (or a synthetic test
    fixture with some axes empty) could leave some tracks short. Taking
    the max across tracks keeps the chart title honest about how many
    rounds *any* axis actually saw.
    """
    max_rounds = 0
    for track in stats.axes.values():
        if not track.records:
            continue
        last_index = track.records[-1].round_index
        max_rounds = max(max_rounds, last_index + 1)
    return max_rounds
