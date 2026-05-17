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

import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import matplotlib as mpl

# The renderer must work in CI / headless contexts where no display
# server is available. ``Agg`` is the non-interactive backend that
# writes directly to a file buffer; selecting it before any pyplot
# import is the documented matplotlib pattern.
mpl.use("Agg")

import matplotlib.lines as mlines
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

#: Short arrow glyph per axis summarising the expected direction. Used
#: in the overlay legend where the full ``expected: ...`` phrase would
#: clutter a nine-line legend box. ``↑`` = rises (improvement), ``↓``
#: = falls (improvement), ``≈`` = flat (no expected motion). Keys match
#: ``NINE_AXIS_LABELS`` exactly.
_AXIS_EXPECTED_ARROW: dict[str, str] = {
    "A_pack_quality": "↑",
    "B_useful_item_fraction": "↑",
    "C_advisory_hit_rate": "↑",
    "D_observation_enrichment": "↑",
    "E_provenance_queryability": "≈",
    "F_extraction_failure_clusters": "↓",
    "G_schema_evolution_candidates": "↑",
    "H_meta_trace_density": "≈",
    "I_self_authored_proposals": "↑",
}

#: Valid ``style`` values. Listed here so the validator and the docstring
#: agree on the exact spelling.
ChartStyle = Literal["grid", "overlay"]
_VALID_STYLES: tuple[str, ...] = ("grid", "overlay")

#: Quarter-window divisor used by the overlay baseline. Mirrors
#: ``ROUND_WINDOW_FRACTION`` in ``eval.scenarios._convergence_common`` —
#: kept as a module-local constant rather than imported to avoid an
#: import cycle (the renderer is supposed to be the leaf node).
_OVERLAY_QUARTER_DIVISOR = 4

#: Threshold below which a first-quarter baseline is treated as zero
#: for normalization. Anything inside this magnitude divides into
#: ``inf`` or near-inf and isn't a useful relative baseline; tracks
#: with a baseline this small render as "no baseline" entries in the
#: legend rather than producing nonsense lines.
_OVERLAY_BASELINE_EPSILON = 1e-12


def render_program_convergence_chart(
    stats: _MultiAxisStats,
    *,
    output_dir: Path,
    invocation_id: str,
    timestamp: datetime | None = None,
    figsize: tuple[float, float] | None = None,
    dpi: int | None = None,
    style: ChartStyle = "grid",
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
        figsize: ``(width_inches, height_inches)`` for the matplotlib
            figure. Defaults to the module-level ``_FIGSIZE_INCHES``
            (15.0, 11.0) when ``None`` — the only size the 3x3 grid
            has been laid out against. Operators wanting a denser
            chart for slide decks (e.g. ``(10.0, 7.5)``) override.
        dpi: Pixels-per-inch passed to both ``plt.subplots`` and
            ``fig.savefig``. Defaults to the module-level ``_DPI``
            (100) when ``None``. Higher values produce sharper PNGs
            at the cost of file size.
        style: Layout style. ``"grid"`` (default) renders the 3x3
            subplot grid — one axes object per of the nine tracks,
            preserving each axis's natural units. ``"overlay"`` renders
            a single-figure 9-line plot with each axis's series
            normalized against its own first-quarter mean (so 1.0 =
            baseline, >1.0 = improvement relative to baseline, <1.0 =
            regression). The overlay variant trades absolute units for
            cross-axis comparability — useful for an at-a-glance "did
            everything move the right direction?" check; the grid
            variant remains authoritative for shape inspection.

    Returns:
        Absolute path to the written PNG.

    Raises:
        ValueError: If ``invocation_id`` is empty / whitespace,
            ``timestamp`` is naive, or ``style`` is not one of
            ``"grid"`` / ``"overlay"``.
    """
    if style not in _VALID_STYLES:
        msg = (
            f"style must be one of {_VALID_STYLES!r}; got {style!r}. "
            "POC directive: loud on misuse rather than silent fallback."
        )
        raise ValueError(msg)

    if not invocation_id or not invocation_id.strip():
        msg = "invocation_id must be a non-empty string"
        raise ValueError(msg)

    ts = timestamp if timestamp is not None else datetime.now(UTC)
    if ts.tzinfo is None:
        msg = "timestamp must be timezone-aware (UTC); got naive datetime"
        raise ValueError(msg)

    resolved_figsize = figsize if figsize is not None else _FIGSIZE_INCHES
    resolved_dpi = dpi if dpi is not None else _DPI

    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"program_convergence_{ts.strftime(_FILENAME_TIMESTAMP_FORMAT)}.png"
    output_path = output_dir / filename

    rounds_count = _infer_rounds_count(stats)

    if style == "grid":
        _render_grid(
            stats=stats,
            output_path=output_path,
            invocation_id=invocation_id,
            rounds_count=rounds_count,
            figsize=resolved_figsize,
            dpi=resolved_dpi,
        )
    else:  # style == "overlay" — exhaustive after the validator above.
        _render_overlay(
            stats=stats,
            output_path=output_path,
            invocation_id=invocation_id,
            rounds_count=rounds_count,
            figsize=resolved_figsize,
            dpi=resolved_dpi,
        )

    logger.info(
        "program_convergence_chart_rendered",
        output_path=str(output_path),
        invocation_id=invocation_id,
        rounds=rounds_count,
        timestamp=ts.isoformat(),
        figsize=resolved_figsize,
        dpi=resolved_dpi,
        style=style,
    )
    return output_path


def _render_grid(
    *,
    stats: _MultiAxisStats,
    output_path: Path,
    invocation_id: str,
    rounds_count: int,
    figsize: tuple[float, float],
    dpi: int,
) -> None:
    """Render the 3x3 subplot grid — one axes object per axis label.

    Extracted from ``render_program_convergence_chart`` to make the
    ``style`` dispatch readable. Behavior identical to the pre-D2
    implementation; the grid variant is the regression anchor and must
    stay byte-for-byte compatible with the prior renderer for any
    given input.
    """
    from eval.scenarios._convergence_common import NINE_AXIS_LABELS  # noqa: PLC0415

    palette = plt.get_cmap("tab10")
    fig, axes_grid = plt.subplots(
        nrows=_SUBPLOT_ROWS,
        ncols=_SUBPLOT_COLS,
        figsize=figsize,
        dpi=dpi,
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
    fig.savefig(output_path, format="png", dpi=dpi)
    plt.close(fig)


def _render_overlay(
    *,
    stats: _MultiAxisStats,
    output_path: Path,
    invocation_id: str,
    rounds_count: int,
    figsize: tuple[float, float],
    dpi: int,
) -> None:
    """Render a single-axes plot with all 9 tracks normalized to baselines.

    Each track is normalized against its own first-quarter mean (the
    same ``_quarter_means`` window the convergence finding uses); the
    resulting series is ``value / baseline`` so 1.0 means "no change
    vs. early-run baseline", >1.0 means "improved", <1.0 means
    "regressed". An axis with a zero (or near-zero) first-quarter mean
    has no meaningful relative baseline; we render those tracks with
    label ``"(no baseline)"`` and skip plotting their data points,
    rather than fabricating a synthetic baseline or hitting
    div-by-zero. Same ``tab10`` colour-per-axis-index ordering as the
    grid variant so an operator switching between styles sees the same
    colour for each axis.
    """
    from eval.scenarios._convergence_common import NINE_AXIS_LABELS  # noqa: PLC0415

    palette = plt.get_cmap("tab10")
    fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
    fig.suptitle(
        f"Program convergence (overlay) — {rounds_count} rounds — {invocation_id}",
        fontsize=14,
        fontweight="bold",
    )

    ax.set_xlabel("round", fontsize=9)
    ax.set_ylabel("value / first-quarter-mean baseline", fontsize=9)
    ax.tick_params(labelsize=8)
    ax.grid(visible=True, alpha=0.3)
    # 1.0 = baseline reference. Dashed horizontal line so eye snaps to
    # "above this line is improvement, below is regression".
    ax.axhline(1.0, color="#888888", linestyle="--", linewidth=0.8, alpha=0.7)

    legend_handles: list[mlines.Line2D] = []
    legend_labels: list[str] = []

    for index, label in enumerate(NINE_AXIS_LABELS):
        track = stats.axes.get(label)
        color = palette(index)
        title = _AXIS_DISPLAY_TITLES.get(label, label)
        arrow = _AXIS_EXPECTED_ARROW.get(label, "")
        legend_label = f"{title} ({arrow})" if arrow else title

        if track is None or not track.records:
            legend_handles.append(_make_legend_proxy(color))
            legend_labels.append(f"{legend_label} — no data")
            continue

        baseline = _first_quarter_baseline(track)
        if baseline is None:
            legend_handles.append(_make_legend_proxy(color))
            legend_labels.append(f"{legend_label} — no baseline")
            continue

        x_values = [record.round_index for record in track.records]
        y_values = [record.value / baseline for record in track.records]
        (line,) = ax.plot(
            x_values,
            y_values,
            color=color,
            marker="o",
            linewidth=1.5,
            markersize=4,
            label=legend_label,
        )
        legend_handles.append(line)
        legend_labels.append(legend_label)

    # ``ax.legend()`` would only pick up handles for plotted lines;
    # build the legend explicitly so empty / baseline-less axes still
    # surface in the legend with their colour swatch.
    ax.legend(
        legend_handles,
        legend_labels,
        loc="center left",
        bbox_to_anchor=(1.02, 0.5),
        fontsize=8,
        frameon=True,
    )

    # Right-side legend takes ~25% of the figure width; leave room.
    fig.tight_layout(rect=(0.0, 0.0, 0.78, 0.94))
    fig.savefig(output_path, format="png", dpi=dpi)
    plt.close(fig)


def _first_quarter_baseline(track: object) -> float | None:
    """Compute the first-quarter mean baseline for overlay normalization.

    Returns ``None`` when the baseline is too close to zero to use as
    a divisor — overlay normalization is value-over-baseline, and a
    zero baseline either means the axis genuinely sat at zero through
    the first quarter (legitimately "no baseline to compare against")
    or the track is empty. Either way, dividing into it produces
    nonsense; the caller skips plotting those tracks rather than
    hitting ``ZeroDivisionError`` or rendering an ``inf``.

    The threshold (``1e-12``) is tight on purpose — we want to skip
    only literal-zero baselines, not legitimate small values; an axis
    with first-quarter mean 0.001 is still a real baseline for an
    operator's "did we double?" eyeball check.
    """
    records = getattr(track, "records", None)
    if not records:
        return None
    values = [r.value for r in records]
    if not values:
        return None
    # Match ``_quarter_means`` window semantics: rounds < 4 fall back
    # to the full-sample mean rather than a (potentially noisier)
    # one-sample window. Keeps the overlay baseline consistent with
    # the convergence-finding baseline an operator already trusts.
    if len(values) < _OVERLAY_QUARTER_DIVISOR:
        baseline = statistics.fmean(values)
    else:
        window = max(1, len(values) // _OVERLAY_QUARTER_DIVISOR)
        baseline = statistics.fmean(values[:window])
    if abs(baseline) < _OVERLAY_BASELINE_EPSILON:
        return None
    return baseline


def _make_legend_proxy(
    color: tuple[float, float, float, float],
) -> mlines.Line2D:
    """Build an invisible-data Line2D for legend rows with no plotted series.

    Tracks with zero records or a zero baseline still appear in the
    legend so an operator can see at a glance which axes contributed
    no data. Matplotlib's auto-legend skips lines that were never
    plotted; the workaround is a zero-length proxy with the same
    colour and marker as the real line would have used.
    """
    return mlines.Line2D(
        [],
        [],
        color=color,
        marker="o",
        linewidth=1.5,
        markersize=4,
    )


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
