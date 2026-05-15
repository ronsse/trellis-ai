"""Unit tests for ``eval.reports.program_convergence_chart``.

These tests assert that:
- The renderer produces a non-empty PNG at the expected path.
- The output path is deterministic given ``(timestamp, invocation_id)``.
- Re-rendering with the same timestamp overwrites the prior file.
- Edge cases — empty axes, single-round axes, missing-axis tracks —
  do not raise.

They deliberately do **not** assert on pixel content; matplotlib's
rendering drifts across versions, so file-exists + size-positive is
the contract we lock in. See plan-program-level-eval.md §4.3.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime
from pathlib import Path

import pytest
from eval.reports.program_convergence_chart import render_program_convergence_chart
from eval.scenarios._convergence_common import (
    NINE_AXIS_LABELS,
    _ConvergenceStats,
    _MultiAxisStats,
)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _empty_convergence_stats() -> _ConvergenceStats:
    return _ConvergenceStats(
        weighted_first_quarter_mean=0.0,
        weighted_last_quarter_mean=0.0,
        weighted_delta=0.0,
        useful_first_quarter_mean=0.0,
        useful_last_quarter_mean=0.0,
        useful_delta=0.0,
    )


def _build_full_stats(rounds: int) -> _MultiAxisStats:
    """Build a multi-axis stats container with deterministic per-axis tracks.

    Each axis gets a synthetic shape so the rendered chart is
    eyeball-checkable when an operator looks at it: monotonically
    rising for A/B/C/D/G/I, monotonically falling for F, flat for E/H.
    """
    stats = _MultiAxisStats(convergence=_empty_convergence_stats())
    shapes: dict[str, list[float]] = {
        "A_pack_quality": [0.30 + 0.02 * i for i in range(rounds)],
        "B_useful_item_fraction": [0.25 + 0.015 * i for i in range(rounds)],
        "C_advisory_hit_rate": [0.10 + 0.025 * i for i in range(rounds)],
        "D_observation_enrichment": [float(i + 1) for i in range(rounds)],
        "E_provenance_queryability": [1.0 for _ in range(rounds)],
        "F_extraction_failure_clusters": [
            float(max(0, rounds - i)) for i in range(rounds)
        ],
        "G_schema_evolution_candidates": [float(i // 5) for i in range(rounds)],
        "H_meta_trace_density": [1.0 for _ in range(rounds)],
        "I_self_authored_proposals": [float(i // 5) for i in range(rounds)],
    }
    for label in NINE_AXIS_LABELS:
        track = stats.ensure_axis(label)
        for round_index, value in enumerate(shapes[label]):
            track.record(round_index, value)
    return stats


def test_render_writes_png_at_expected_path(tmp_path: Path) -> None:
    stats = _build_full_stats(rounds=12)
    timestamp = datetime(2026, 5, 15, 10, 30, 45, tzinfo=UTC)

    output_path = render_program_convergence_chart(
        stats,
        output_dir=tmp_path,
        invocation_id="run-001",
        timestamp=timestamp,
    )

    assert output_path == tmp_path / "program_convergence_2026-05-15T103045Z.png"
    assert output_path.exists()
    assert output_path.stat().st_size > 0


def test_render_emits_valid_png_signature(tmp_path: Path) -> None:
    """Sanity check on the bytes — at least confirms matplotlib wrote a PNG."""
    stats = _build_full_stats(rounds=8)
    timestamp = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)

    output_path = render_program_convergence_chart(
        stats,
        output_dir=tmp_path,
        invocation_id="png-signature-check",
        timestamp=timestamp,
    )

    header = output_path.read_bytes()[:8]
    assert header == _PNG_SIGNATURE


def test_render_is_idempotent_for_same_timestamp(tmp_path: Path) -> None:
    """Re-rendering with the same (timestamp, invocation_id) overwrites the file.

    Idempotency is the property the POC directive on this phase asks
    for: the path is deterministic given the inputs, so an operator
    re-running a chart from a checkpoint never multiplies PNGs.
    """
    stats = _build_full_stats(rounds=6)
    timestamp = datetime(2026, 5, 15, 9, 15, 0, tzinfo=UTC)

    first_path = render_program_convergence_chart(
        stats,
        output_dir=tmp_path,
        invocation_id="idempotent",
        timestamp=timestamp,
    )
    first_size = first_path.stat().st_size

    second_path = render_program_convergence_chart(
        stats,
        output_dir=tmp_path,
        invocation_id="idempotent",
        timestamp=timestamp,
    )

    assert second_path == first_path
    assert first_path.exists()
    assert first_path.stat().st_size == first_size
    # Only one PNG should exist in the output directory.
    pngs = list(tmp_path.glob("*.png"))
    assert len(pngs) == 1


def test_render_creates_output_directory(tmp_path: Path) -> None:
    """The renderer creates ``output_dir`` if it doesn't exist."""
    nested_dir = tmp_path / "nested" / "reports"
    assert not nested_dir.exists()

    stats = _build_full_stats(rounds=4)
    output_path = render_program_convergence_chart(
        stats,
        output_dir=nested_dir,
        invocation_id="creates-dir",
        timestamp=datetime(2026, 5, 15, 0, 0, 0, tzinfo=UTC),
    )

    assert nested_dir.is_dir()
    assert output_path.exists()


def test_render_with_empty_axis_track(tmp_path: Path) -> None:
    """An axis with zero records renders as a blank-but-titled subplot."""
    stats = _MultiAxisStats(convergence=_empty_convergence_stats())
    # Populate eight of nine axes; leave axis H empty so the renderer
    # has to handle the empty-track case in the middle of an
    # otherwise-populated grid.
    for label in NINE_AXIS_LABELS:
        if label == "H_meta_trace_density":
            stats.ensure_axis(label)
            continue
        track = stats.ensure_axis(label)
        for round_index in range(5):
            track.record(round_index, float(round_index))

    output_path = render_program_convergence_chart(
        stats,
        output_dir=tmp_path,
        invocation_id="empty-axis",
        timestamp=datetime(2026, 5, 15, 11, 0, 0, tzinfo=UTC),
    )

    assert output_path.exists()
    assert output_path.stat().st_size > 0


def test_render_with_single_round_track(tmp_path: Path) -> None:
    """A track with one record renders as a single marker — does not raise."""
    stats = _MultiAxisStats(convergence=_empty_convergence_stats())
    for label in NINE_AXIS_LABELS:
        track = stats.ensure_axis(label)
        track.record(0, 0.5)

    output_path = render_program_convergence_chart(
        stats,
        output_dir=tmp_path,
        invocation_id="single-round",
        timestamp=datetime(2026, 5, 15, 13, 0, 0, tzinfo=UTC),
    )

    assert output_path.exists()
    assert output_path.stat().st_size > 0


def test_render_with_missing_axis_in_stats(tmp_path: Path) -> None:
    """A stats container missing an axis entirely still renders (8-of-9)."""
    stats = _MultiAxisStats(convergence=_empty_convergence_stats())
    for label in NINE_AXIS_LABELS:
        if label == "I_self_authored_proposals":
            continue
        track = stats.ensure_axis(label)
        for round_index in range(3):
            track.record(round_index, float(round_index))

    output_path = render_program_convergence_chart(
        stats,
        output_dir=tmp_path,
        invocation_id="missing-axis",
        timestamp=datetime(2026, 5, 15, 14, 0, 0, tzinfo=UTC),
    )

    assert output_path.exists()
    assert output_path.stat().st_size > 0


def test_render_rejects_empty_invocation_id(tmp_path: Path) -> None:
    stats = _build_full_stats(rounds=3)
    with pytest.raises(ValueError, match="invocation_id"):
        render_program_convergence_chart(
            stats,
            output_dir=tmp_path,
            invocation_id="   ",
            timestamp=datetime(2026, 5, 15, 0, 0, 0, tzinfo=UTC),
        )


def test_render_rejects_naive_timestamp(tmp_path: Path) -> None:
    stats = _build_full_stats(rounds=3)
    with pytest.raises(ValueError, match="timezone-aware"):
        render_program_convergence_chart(
            stats,
            output_dir=tmp_path,
            invocation_id="naive-ts",
            timestamp=datetime(2026, 5, 15, 0, 0, 0),  # noqa: DTZ001 — deliberate
        )


def test_render_defaults_timestamp_to_now(tmp_path: Path) -> None:
    """Omitting ``timestamp`` falls back to ``datetime.now(UTC)``."""
    stats = _build_full_stats(rounds=2)

    output_path = render_program_convergence_chart(
        stats,
        output_dir=tmp_path,
        invocation_id="default-ts",
    )

    assert output_path.exists()
    assert output_path.name.startswith("program_convergence_")
    assert output_path.name.endswith("Z.png")


def test_png_dimensions_match_configured_figsize(tmp_path: Path) -> None:
    """Decode the PNG header and assert pixel dimensions match the figsize.

    ``figsize=(15.0, 11.0)`` at ``dpi=100`` = 1500 x 1100 pixels. The
    PNG IHDR chunk stores width/height as big-endian uint32 at offset
    16. We don't need a PNG library for this — eight bytes of struct
    unpack.
    """
    stats = _build_full_stats(rounds=5)
    output_path = render_program_convergence_chart(
        stats,
        output_dir=tmp_path,
        invocation_id="dimensions",
        timestamp=datetime(2026, 5, 15, 15, 0, 0, tzinfo=UTC),
    )
    header = output_path.read_bytes()[:24]
    width, height = struct.unpack(">II", header[16:24])
    assert width == 1500
    assert height == 1100
