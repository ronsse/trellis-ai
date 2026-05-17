"""Unit smoke for the program_convergence master scenario.

Exercises end-to-end round execution, axis-substrate verification,
and the strict-mode error path against an in-memory SQLite registry.
No live backends.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from eval.scenarios._convergence_common import (
    NINE_AXIS_LABELS,
    _AxisRecord,
    _AxisTrack,
    _build_multi_axis_stats,
    _multi_axis_metrics,
    _NineAxisRound,
)
from eval.scenarios.program_convergence.scenario import (
    DEFAULT_ADVISORY_HIT_LOOKBACK_ROUNDS,
    DEFAULT_ANALYZER_CADENCE,
    SCENARIO_NAME,
    ProgramConvergenceError,
    _compute_advisory_hit_rate,
    _default_chart_output_dir,
    _RoundResult,
    run,
)

from trellis.schemas.advisory import (
    Advisory,
    AdvisoryCategory,
    AdvisoryEvidence,
)
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.registry import StoreRegistry


@pytest.fixture
def sqlite_registry(tmp_path: Path):
    config = {
        "knowledge": {
            "graph": {"backend": "sqlite"},
            "vector": {"backend": "sqlite"},
            "document": {"backend": "sqlite"},
            "blob": {"backend": "local"},
        },
        "operational": {
            "trace": {"backend": "sqlite"},
            "event_log": {"backend": "sqlite"},
        },
    }
    with StoreRegistry(config=config, stores_dir=tmp_path) as registry:
        yield registry


def test_run_against_sqlite_emits_all_nine_axes(
    sqlite_registry: StoreRegistry,
) -> None:
    """End-to-end smoke: every axis lands in the metrics dict."""
    report = run(
        sqlite_registry,
        seed=0,
        rounds=8,
        feedback_batch_size=4,
        analyzer_cadence=4,
        traces_per_domain=3,
    )

    assert report.name == SCENARIO_NAME
    assert report.status == "pass"

    # Each axis must surface three metrics: first_quarter_mean,
    # last_quarter_mean, delta. That's 9 * 3 = 27 keys minimum.
    axis_keys = [k for k in report.metrics if k.startswith("axis.")]
    assert len(axis_keys) >= len(NINE_AXIS_LABELS) * 3, (
        f"expected at least {len(NINE_AXIS_LABELS) * 3} axis metric "
        f"keys, got {len(axis_keys)}: {axis_keys}"
    )
    for label in NINE_AXIS_LABELS:
        for suffix in ("first_quarter_mean", "last_quarter_mean", "delta"):
            key = f"axis.{label}.{suffix}"
            assert key in report.metrics, f"missing metric {key!r}"

    # Composite finding must carry every axis delta in detail.
    composite = next(f for f in report.findings if "multi-axis summary" in f.message)
    assert set(composite.detail["axis_deltas"]) == set(NINE_AXIS_LABELS)


def test_run_is_deterministic(sqlite_registry: StoreRegistry, tmp_path: Path) -> None:
    """Same seed must produce identical axis metrics — POC determinism."""
    config = {
        "knowledge": {
            "graph": {"backend": "sqlite"},
            "vector": {"backend": "sqlite"},
            "document": {"backend": "sqlite"},
            "blob": {"backend": "local"},
        },
        "operational": {
            "trace": {"backend": "sqlite"},
            "event_log": {"backend": "sqlite"},
        },
    }
    rep1 = run(sqlite_registry, seed=7, rounds=6, traces_per_domain=3)

    # Fresh registry for the second run so prior state doesn't bleed.
    with StoreRegistry(config=config, stores_dir=tmp_path / "second") as reg2:
        rep2 = run(reg2, seed=7, rounds=6, traces_per_domain=3)

    axis_metrics_1 = {k: v for k, v in rep1.metrics.items() if k.startswith("axis.")}
    axis_metrics_2 = {k: v for k, v in rep2.metrics.items() if k.startswith("axis.")}
    assert axis_metrics_1 == axis_metrics_2


def test_run_raises_when_event_log_missing() -> None:
    """Strict mode — bare registry without an EventLog fails loud, not silently."""
    fake_registry = MagicMock()
    fake_registry.operational.event_log = None
    fake_registry.knowledge.graph_store = MagicMock()

    with pytest.raises(ProgramConvergenceError, match="EventLog"):
        run(fake_registry, rounds=1)


def test_run_raises_when_graph_store_missing() -> None:
    """Strict mode — registry without a GraphStore must fail loud, not silently."""
    fake_registry = MagicMock()
    fake_registry.operational.event_log = MagicMock()
    fake_registry.knowledge.graph_store = None

    with pytest.raises(ProgramConvergenceError, match="GraphStore"):
        run(fake_registry, rounds=1)


def test_axis_track_first_last_delta() -> None:
    """``_AxisTrack`` math matches ``_quarter_means`` semantics.

    With 8 records the window size is ``len // 4 == 2``, so the
    first-quarter mean is ``mean(0, 1) == 0.5`` and the last-quarter
    mean is ``mean(6, 7) == 6.5``. Same arithmetic the dual-loop
    scenarios use — we re-assert it here to lock the composition
    contract.
    """
    track = _AxisTrack(axis="A_test")
    for i in range(8):
        track.record(i, float(i))
    assert track.first_quarter_mean() == 0.5
    assert track.last_quarter_mean() == 6.5
    assert track.delta() == 6.0


def test_multi_axis_metrics_keys_are_stable() -> None:
    """``_multi_axis_metrics`` emits exactly the documented key shape."""
    rounds = [
        _NineAxisRound(
            round_index=i,
            weighted_score=float(i),
            items_served=10,
            items_referenced=i,
            coverage_fraction=0.5,
            success=i % 2 == 0,
            axis_pack_quality=float(i),
            axis_useful_item_fraction=i / 10,
            axis_advisory_hit_rate=0.5,
            axis_observation_enrichment=float(i),
            axis_provenance_queryability=1.0,
            axis_extraction_failure_clusters=10.0 - i,
            axis_schema_evolution_candidates=float(i),
            axis_meta_trace_density=1.0,
            axis_self_authored_proposals=float(i),
        )
        for i in range(8)
    ]
    stats = _build_multi_axis_stats(rounds)
    metrics = _multi_axis_metrics(stats)
    for label in NINE_AXIS_LABELS:
        for suffix in ("first_quarter_mean", "last_quarter_mean", "delta"):
            assert f"axis.{label}.{suffix}" in metrics


def test_default_analyzer_cadence_is_positive() -> None:
    """Sanity — the cadence default must be >= 1 or the modulo logic breaks."""
    assert DEFAULT_ANALYZER_CADENCE >= 1


def test_axis_record_is_frozen() -> None:
    """``_AxisRecord`` must be frozen so accidental mutation can't drift history."""
    record = _AxisRecord(axis="x", round_index=0, value=1.0)
    with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError
        record.value = 2.0  # type: ignore[misc]


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def test_run_with_render_chart_writes_png_and_sets_metric(
    sqlite_registry: StoreRegistry,
    tmp_path: Path,
) -> None:
    """``render_chart=True`` produces a PNG, surfaces it on the report.

    Passes an explicit ``chart_output_dir=tmp_path`` to keep the test
    isolated from the repo-anchored default — that default behavior is
    locked in by ``test_render_chart_default_output_dir_anchors_to_file``.
    Also confirms the in-memory ``_MultiAxisStats`` is attached to
    ``ScenarioReport.convergence_stats`` and excluded from
    ``to_dict()``.
    """
    report = run(
        sqlite_registry,
        seed=0,
        rounds=4,
        feedback_batch_size=4,
        analyzer_cadence=4,
        traces_per_domain=2,
        render_chart=True,
        chart_output_dir=tmp_path,
    )

    assert report.status == "pass"
    chart_path_str = report.metrics.get("chart_path")
    assert isinstance(chart_path_str, str), (
        f"render_chart=True must surface a string chart_path metric; "
        f"got {chart_path_str!r}"
    )
    chart_path = Path(chart_path_str)
    # Caller-supplied output_dir flows through the helper untouched.
    assert chart_path.parent == tmp_path
    assert chart_path.exists(), f"expected PNG at {chart_path}, found nothing"
    assert chart_path.read_bytes().startswith(_PNG_SIGNATURE)

    # convergence_stats is the in-memory _MultiAxisStats payload, set
    # whether or not the chart was rendered. ``to_dict()`` strips it
    # so the JSON report stays slim.
    assert report.convergence_stats is not None
    assert hasattr(report.convergence_stats, "axes")
    assert "convergence_stats" not in report.to_dict()


def test_run_without_render_chart_omits_chart_path(
    sqlite_registry: StoreRegistry,
) -> None:
    """Default ``render_chart=False`` must not touch metrics['chart_path']."""
    report = run(
        sqlite_registry,
        seed=0,
        rounds=4,
        feedback_batch_size=4,
        analyzer_cadence=4,
        traces_per_domain=2,
    )

    assert "chart_path" not in report.metrics
    # convergence_stats is still set — post-hoc rendering is supported
    # even when the run didn't auto-render.
    assert report.convergence_stats is not None


def test_axis_g_emits_candidate_before_round_thirty(
    sqlite_registry: StoreRegistry,
) -> None:
    """Phase 5A — axis G must surface >=1 candidate before round 30.

    The Phase 4 calibration lowered ``well_known_count_threshold`` from
    10 to 3 but axis G stayed at 0 because the master scenario bypassed
    the governed mutation pipeline — no ``MUTATION_EXECUTED`` events
    were written, so ``_index_mutation_extractors`` returned an empty
    map and the analyzer's distinct-extractors gate tripped on every
    seed type. Phase 5A synthesises those events at seed time and
    decorates seed nodes with ``content_tags`` so the distinct-domains
    gate also passes. This test locks in the fix.
    """
    report = run(
        sqlite_registry,
        seed=0,
        rounds=30,
        analyzer_cadence=5,
        traces_per_domain=6,
        entities_per_trace=3,
    )

    stats = report.convergence_stats
    assert stats is not None
    track = stats.axes["G_schema_evolution_candidates"]
    nonzero = [r for r in track.records if r.value > 0]
    assert nonzero, (
        "axis G must emit >=1 candidate before round 30; "
        f"actual track: {[r.value for r in track.records]}"
    )
    # First non-zero round should land on the first cadence pass after
    # seeding (cadence=5 → round index 4 is the first analyzer run).
    first_emit = nonzero[0]
    assert first_emit.round_index < 30, (
        f"axis G first emission must be before round 30, got round "
        f"{first_emit.round_index}"
    )


# ---------------------------------------------------------------------------
# advisory_hit_lookback_rounds kwarg coverage (Unit A2)
# ---------------------------------------------------------------------------


def _make_round(
    *,
    round_index: int,
    domain: str,
    success: bool,
    advisory_ids_per_item: list[list[str]] | None = None,
) -> _RoundResult:
    """Build a minimally-populated _RoundResult for axis-C slicing tests.

    ``advisory_ids_per_item`` populates the Unit D1 provenance field —
    one inner list per ``PackItem`` in the round's pack, empty when the
    builder didn't stamp the item. ``None`` (the default) leaves it
    empty, matching the pre-advisory-loop state.
    """
    return _RoundResult(
        round_index=round_index,
        domain=domain,
        pack_id=f"pack:{round_index}",
        items_served=0,
        items_referenced=0,
        coverage_fraction=0.0,
        weighted_score=0.0,
        success=success,
        axis_pack_quality=0.0,
        axis_useful_item_fraction=0.0,
        axis_advisory_hit_rate=0.0,
        axis_observation_enrichment=0.0,
        axis_provenance_queryability=0.0,
        axis_extraction_failure_clusters=0.0,
        axis_schema_evolution_candidates=0.0,
        axis_meta_trace_density=0.0,
        axis_self_authored_proposals=0.0,
        injected_advisory_ids_per_item=advisory_ids_per_item or [],
    )


def _make_advisory(scope: str) -> Advisory:
    """Build a minimal active Advisory scoped to ``scope``.

    Pre-D1 this fed ``_compute_advisory_hit_rate``'s ``advisory_store``
    kwarg directly; the D1 implementation no longer reads the store but
    the kwarg is preserved for backward compatibility, so the fixture
    stays useful for the ``advisory_store=`` smoke-test.
    """
    return Advisory(
        category=AdvisoryCategory.ENTITY,
        confidence=0.9,
        message=f"test advisory for {scope}",
        scope=scope,
        evidence=AdvisoryEvidence(
            sample_size=10,
            success_rate_with=0.8,
            success_rate_without=0.4,
            effect_size=0.4,
        ),
    )


@pytest.mark.parametrize(
    ("lookback", "expected_hit_rate"),
    [
        # Last 3 rounds are all failures with advisory_ids stamped on
        # every item — denominator = 6 (3 rounds * 2 advisories), hits = 0.
        (3, 0.0),
        # 10-round lookback pulls in rounds 0-6 (all successes carrying
        # the same advisory_ids) plus rounds 7-9 (all failures). Hits =
        # 14 (7 rounds * 2 advisories), denominator = 20 (10 rounds * 2),
        # so hit_rate = 0.7. Same history, only the window changed —
        # proves the kwarg flows through to the aggregator.
        (10, 0.7),
    ],
)
def test_compute_advisory_hit_rate_respects_lookback(
    lookback: int,
    expected_hit_rate: float,
) -> None:
    """Axis C must read only the last ``lookback`` rounds (Unit D1 semantics).

    Builds a 10-round history where the first 7 rounds succeed and the
    trailing 3 fail. Every round carries the same two advisory_ids on a
    single item, so the lookback window changes the denominator AND the
    hits count proportionally. With a short window of trailing failures
    the rate drops to 0.0; with a long window the success-weighted
    portion of the window lifts it back up.
    """
    ids = ["adv-A", "adv-B"]
    full_history = [
        _make_round(
            round_index=i,
            domain="finance",
            success=True,
            advisory_ids_per_item=[list(ids)],
        )
        for i in range(7)
    ] + [
        _make_round(
            round_index=i,
            domain="health",
            success=False,
            advisory_ids_per_item=[list(ids)],
        )
        for i in range(7, 10)
    ]

    sliced = full_history[-lookback:]
    hit_rate = _compute_advisory_hit_rate(recent_rounds=sliced)
    rounds_repr = [
        (r.round_index, r.success, r.injected_advisory_ids_per_item) for r in sliced
    ]
    assert hit_rate == pytest.approx(expected_hit_rate), (
        f"lookback={lookback} should yield hit_rate={expected_hit_rate}; "
        f"got {hit_rate}. Sliced rounds: {rounds_repr}"
    )


# ---------------------------------------------------------------------------
# advisory_hit_rate provenance-based aggregator (Unit D1)
# ---------------------------------------------------------------------------


def test_compute_advisory_hit_rate_all_success_all_stamped() -> None:
    """Every item stamped, every round successful → hit rate is 1.0.

    Three rounds, two items each, two advisory_ids per item. Total
    presented = 3 * 2 * 2 = 12; hits = 12 (every round succeeded);
    ratio = 1.0. This is the plan-prose "advisories whose recommendation
    was followed AND outcome=success" upper bound.
    """
    rounds = [
        _make_round(
            round_index=i,
            domain="finance",
            success=True,
            advisory_ids_per_item=[["adv-A", "adv-B"], ["adv-A", "adv-C"]],
        )
        for i in range(3)
    ]
    hit_rate = _compute_advisory_hit_rate(recent_rounds=rounds)
    assert hit_rate == pytest.approx(1.0)


def test_compute_advisory_hit_rate_all_failure_all_stamped() -> None:
    """Every item stamped, every round failed → hit rate is 0.0.

    Same shape as the all-success case but with ``success=False``
    everywhere — denominator is identical (12), hits is 0. The plan-
    prose lower bound: a misfiring advisory whose recommendations land
    in failing rounds drags axis C to the floor.
    """
    rounds = [
        _make_round(
            round_index=i,
            domain="finance",
            success=False,
            advisory_ids_per_item=[["adv-A", "adv-B"], ["adv-A", "adv-C"]],
        )
        for i in range(3)
    ]
    hit_rate = _compute_advisory_hit_rate(recent_rounds=rounds)
    assert hit_rate == pytest.approx(0.0)


def test_compute_advisory_hit_rate_mixed_success_fraction() -> None:
    """Mixed success → ``hits / total_presented``.

    Four rounds, one item each, one advisory per item. Two succeed,
    two fail → 2 hits / 4 presented = 0.5. Locks in the proportional-
    aggregation rule (every advisory_id occurrence counts once on the
    denominator and on the hits side iff the round succeeded).
    """
    rounds = [
        _make_round(
            round_index=0,
            domain="finance",
            success=True,
            advisory_ids_per_item=[["adv-A"]],
        ),
        _make_round(
            round_index=1,
            domain="finance",
            success=False,
            advisory_ids_per_item=[["adv-A"]],
        ),
        _make_round(
            round_index=2,
            domain="finance",
            success=True,
            advisory_ids_per_item=[["adv-A"]],
        ),
        _make_round(
            round_index=3,
            domain="finance",
            success=False,
            advisory_ids_per_item=[["adv-A"]],
        ),
    ]
    hit_rate = _compute_advisory_hit_rate(recent_rounds=rounds)
    assert hit_rate == pytest.approx(0.5)


def test_compute_advisory_hit_rate_no_advisories_stamped() -> None:
    """No item carries an advisory_id → hit rate is 0.0 (documented contract).

    Pre-advisory-loop runs (and any future regression that stops the
    PackBuilder from stamping items) land here. The plan-prose
    contract is "0.0", not NaN — the regression suite's
    ``THRESHOLD_C_LAST_QUARTER`` still bites when the stamping path
    breaks.
    """
    rounds = [
        _make_round(
            round_index=i,
            domain="finance",
            success=True,
            advisory_ids_per_item=[[], [], []],
        )
        for i in range(5)
    ]
    hit_rate = _compute_advisory_hit_rate(recent_rounds=rounds)
    assert hit_rate == pytest.approx(0.0)


def test_compute_advisory_hit_rate_empty_rounds_window() -> None:
    """An empty lookback window degenerates to 0.0, not a ZeroDivisionError."""
    hit_rate = _compute_advisory_hit_rate(recent_rounds=[])
    assert hit_rate == pytest.approx(0.0)


def test_compute_advisory_hit_rate_partial_item_coverage() -> None:
    """Items with empty advisory_ids skip the denominator.

    Three items per round; only the first carries an advisory. The
    blank inner lists must not inflate the denominator — otherwise a
    pack with 50 items and one stamped one would dilute axis C
    artificially. Hits = 2 (both rounds succeeded), presented = 2 →
    ratio = 1.0.
    """
    rounds = [
        _make_round(
            round_index=i,
            domain="finance",
            success=True,
            advisory_ids_per_item=[["adv-A"], [], []],
        )
        for i in range(2)
    ]
    hit_rate = _compute_advisory_hit_rate(recent_rounds=rounds)
    assert hit_rate == pytest.approx(1.0)


def test_compute_advisory_hit_rate_ignores_advisory_store(
    tmp_path: Path,
) -> None:
    """Back-compat — ``advisory_store=`` is accepted but no longer consulted.

    Unit D1 derives axis C entirely from per-item provenance, but
    Unit A2 introduced the keyword and operator scripts may still pass
    it. The implementation must accept the kwarg without behaviour
    change. A populated store + an empty round window should still
    return 0.0 (no items presented means no hits, no denominator).
    """
    advisory_store = AdvisoryStore(tmp_path / "advisories.json")
    advisory_store.put(_make_advisory(scope="finance"))
    advisory_store.put(_make_advisory(scope="health"))

    # Two successful rounds, no advisory_ids stamped — D1 should return 0.0
    # regardless of how many actives the store carries.
    rounds = [
        _make_round(round_index=i, domain="finance", success=True) for i in range(2)
    ]
    hit_rate = _compute_advisory_hit_rate(
        advisory_store=advisory_store,
        recent_rounds=rounds,
    )
    assert hit_rate == pytest.approx(0.0)


def test_run_accepts_advisory_hit_lookback_rounds_kwarg(
    sqlite_registry: StoreRegistry,
) -> None:
    """``run()`` accepts the new kwarg and completes successfully.

    Smoke test only — the slicing math is covered by the parametrized
    test above. This locks in the public surface so a future refactor
    that drops the kwarg from ``run()`` trips a clear failure.
    """
    report = run(
        sqlite_registry,
        seed=0,
        rounds=4,
        feedback_batch_size=4,
        analyzer_cadence=4,
        traces_per_domain=2,
        advisory_hit_lookback_rounds=3,
    )
    assert report.status == "pass"


def test_run_rejects_invalid_advisory_hit_lookback_rounds(
    sqlite_registry: StoreRegistry,
) -> None:
    """``advisory_hit_lookback_rounds`` must be >= 1; zero raises loudly.

    POC directive: loud on misuse. ``[-0:]`` slices to the full list
    and would silently change axis C's semantics, so we refuse it at
    entry rather than letting the bug ride.
    """
    with pytest.raises(ValueError, match="advisory_hit_lookback_rounds"):
        run(
            sqlite_registry,
            seed=0,
            rounds=2,
            feedback_batch_size=2,
            analyzer_cadence=2,
            traces_per_domain=2,
            advisory_hit_lookback_rounds=0,
        )


def test_default_advisory_hit_lookback_rounds_matches_prior_constant() -> None:
    """Backwards-compat anchor — default must stay at 5 after the refactor.

    Prior to Unit A2 this lived as a module-level ``_ADVISORY_HIT_LOOKBACK_ROUNDS = 5``
    constant; the kwarg default must match so existing callers see no
    behavior change.
    """
    assert DEFAULT_ADVISORY_HIT_LOOKBACK_ROUNDS == 5


# ---------------------------------------------------------------------------
# Chart kwargs + default-output-dir anchor (Units B4 + B5)
# ---------------------------------------------------------------------------


def test_default_chart_output_dir_is_absolute_and_anchored() -> None:
    """``_default_chart_output_dir`` must be absolute and end in ``eval/reports``.

    This is the B5 contract: the legacy ``Path("eval/reports")`` literal
    was CWD-relative, so an operator running the scenario from a
    nested directory wrote the PNG into ``<nested>/eval/reports/``.
    The anchor flips that to an absolute path under the repo root.
    """
    resolved = _default_chart_output_dir()
    assert resolved.is_absolute(), (
        f"default output dir must be absolute (anchored against __file__); "
        f"got {resolved}"
    )
    assert resolved.parts[-2:] == ("eval", "reports"), (
        f"default output dir must end in eval/reports; got {resolved}"
    )


def test_default_chart_output_dir_does_not_track_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing CWD must NOT change the default output dir.

    Before B5, the helper used ``Path("eval/reports")`` which silently
    re-anchored when the operator ``chdir``-ed. Lock in the new
    behavior: the resolved default is identical regardless of CWD.
    """
    before = _default_chart_output_dir()
    monkeypatch.chdir(tmp_path)
    after = _default_chart_output_dir()
    assert before == after, (
        f"default chart output_dir must not depend on CWD; before={before} "
        f"after={after}"
    )
    # And the anchored path must not live inside ``tmp_path``.
    assert tmp_path not in after.parents, (
        f"default output dir leaked CWD ({tmp_path}); resolved to {after}"
    )


def test_run_render_chart_custom_output_dir_kwarg(
    sqlite_registry: StoreRegistry,
    tmp_path: Path,
) -> None:
    """``chart_output_dir=`` overrides the repo-anchored default.

    Operators wanting a different drop location (CI artifact dir,
    slide-deck folder, etc.) pass it through ``run()``. This locks in
    that the kwarg actually flows into the renderer.
    """
    custom_dir = tmp_path / "ci_artifacts" / "convergence"
    assert not custom_dir.exists()

    report = run(
        sqlite_registry,
        seed=0,
        rounds=4,
        feedback_batch_size=4,
        analyzer_cadence=4,
        traces_per_domain=2,
        render_chart=True,
        chart_output_dir=custom_dir,
    )

    chart_path_str = report.metrics["chart_path"]
    assert isinstance(chart_path_str, str)
    chart_path = Path(chart_path_str)
    assert chart_path.parent == custom_dir
    assert chart_path.exists()
    assert chart_path.read_bytes().startswith(_PNG_SIGNATURE)


def test_run_render_chart_custom_figsize_and_dpi_kwargs(
    sqlite_registry: StoreRegistry,
    tmp_path: Path,
) -> None:
    """``chart_figsize`` + ``chart_dpi`` thread through to the PNG header.

    With ``figsize=(8.0, 6.0)`` and ``dpi=75`` the resulting PNG must
    be 600 x 450 pixels (figsize_inches * dpi). Both kwargs travelled
    from ``run()`` through ``_render_chart()`` into
    ``render_program_convergence_chart``.
    """
    import struct

    report = run(
        sqlite_registry,
        seed=0,
        rounds=4,
        feedback_batch_size=4,
        analyzer_cadence=4,
        traces_per_domain=2,
        render_chart=True,
        chart_output_dir=tmp_path,
        chart_figsize=(8.0, 6.0),
        chart_dpi=75,
    )

    chart_path = Path(report.metrics["chart_path"])  # type: ignore[arg-type]
    header = chart_path.read_bytes()[:24]
    width, height = struct.unpack(">II", header[16:24])
    assert width == 600, f"expected 600 px wide (8.0in * 75dpi); got {width}"
    assert height == 450, f"expected 450 px tall (6.0in * 75dpi); got {height}"


def test_run_render_chart_style_overlay_threads_through(
    sqlite_registry: StoreRegistry,
    tmp_path: Path,
) -> None:
    """``chart_style="overlay"`` flows from ``run()`` into the renderer.

    Confirms the kwarg is plumbed end-to-end and the resulting PNG is
    written + surfaced as a ``chart_path`` metric. The renderer-level
    contract (overlay vs grid byte layout) is locked in by
    ``test_program_convergence_chart.py``; this test only checks the
    plumbing.
    """
    report = run(
        sqlite_registry,
        seed=0,
        rounds=4,
        feedback_batch_size=4,
        analyzer_cadence=4,
        traces_per_domain=2,
        render_chart=True,
        chart_output_dir=tmp_path,
        chart_style="overlay",
    )

    chart_path_str = report.metrics.get("chart_path")
    assert isinstance(chart_path_str, str)
    chart_path = Path(chart_path_str)
    assert chart_path.exists()
    assert chart_path.parent == tmp_path
    # PNG signature — sanity check the overlay renderer actually wrote
    # a valid file rather than half-rendering and leaving an empty stub.
    assert chart_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_run_render_chart_default_output_dir_anchors_to_file(
    sqlite_registry: StoreRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``chart_output_dir=None`` ignores CWD and lands at the repo-anchored dir.

    The B5 contract — chdir-ing into ``tmp_path`` and rendering with
    no explicit output dir must not produce a PNG under
    ``tmp_path/eval/reports/``. The PNG lands at the absolute repo
    location resolved by :func:`_default_chart_output_dir`.

    Cleanup: we delete the resolved PNG at end-of-test because the
    default dir is a real on-disk location under the repo and we
    don't want test artifacts piling up there.
    """
    monkeypatch.chdir(tmp_path)
    expected_dir = _default_chart_output_dir()

    report = run(
        sqlite_registry,
        seed=0,
        rounds=4,
        feedback_batch_size=4,
        analyzer_cadence=4,
        traces_per_domain=2,
        render_chart=True,
    )

    chart_path = Path(report.metrics["chart_path"])  # type: ignore[arg-type]
    try:
        assert chart_path.is_absolute()
        assert chart_path.parent == expected_dir, (
            f"render_chart default must anchor against __file__; "
            f"got {chart_path.parent}, expected {expected_dir}"
        )
        # CWD was tmp_path; the PNG must NOT live under tmp_path.
        assert tmp_path not in chart_path.parents, (
            f"PNG leaked into CWD; ended up under {tmp_path}"
        )
        assert chart_path.exists()
    finally:
        chart_path.unlink(missing_ok=True)
