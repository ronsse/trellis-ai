"""``max_scan`` must bound the work, never move the cursor past unseen rows."""

from __future__ import annotations

from trellis_workers.trace_embed import run_trace_embed_pass, trace_summary_doc_id

from .conftest import seed_traces


def test_a_bounded_pass_takes_the_oldest_traces_not_the_newest(
    registry, watermark_path, recorder
) -> None:
    traces = seed_traces(registry, 10)
    report = run_trace_embed_pass(
        registry, watermark_path=watermark_path, max_scan=4, page_size=2
    )
    assert report.embedded == 4
    embedded = {
        t.trace_id
        for t in traces
        if registry.knowledge.vector_store.get(trace_summary_doc_id(t.trace_id))
    }
    assert embedded == {t.trace_id for t in traces[:4]}, (
        "a truncated walk must yield the run that STARTS at the cursor; "
        f"got {sorted(embedded)}"
    )
    assert report.watermark_after == traces[3].created_at.isoformat()


def test_repeated_bounded_passes_cover_everything(
    registry, watermark_path, recorder
) -> None:
    traces = seed_traces(registry, 10)
    for _ in range(4):
        run_trace_embed_pass(
            registry, watermark_path=watermark_path, max_scan=3, page_size=2
        )
    embedded = {
        t.trace_id
        for t in traces
        if registry.knowledge.vector_store.get(trace_summary_doc_id(t.trace_id))
    }
    assert embedded == {t.trace_id for t in traces}
    for trace in traces:
        assert recorder.calls_containing(trace.intent) == 1


def test_an_oversized_backlog_raises_instead_of_reading_less(
    registry, watermark_path, recorder
) -> None:
    """Reading the whole range is what makes the oldest-first slice sound, so
    there is no correct way to give up quietly. ``SCAN_CEILING_FACTOR`` turns
    an unanticipated backlog into a loud refusal rather than a skipped tail."""
    import pytest

    from trellis_workers.trace_embed import TraceEmbedScanLimitError

    seed_traces(registry, 25)
    with pytest.raises(TraceEmbedScanLimitError, match="refuses"):
        run_trace_embed_pass(
            registry, watermark_path=watermark_path, max_scan=1, page_size=2
        )
    assert not watermark_path.exists(), "a refused pass must not move the cursor"


def test_the_cli_surfaces_a_refused_pass(registry, tmp_path, cli_runner) -> None:
    import json

    from trellis_cli.worker import worker_app

    seed_traces(registry, 25)
    result = cli_runner.invoke(
        worker_app,
        [
            "embed-traces",
            "--format",
            "json",
            "--watermark",
            str(tmp_path / "wm.json"),
            "--max-scan",
            "1",
            "--page-size",
            "2",
        ],
    )
    assert result.exit_code == 1, result.output
    assert json.loads(result.output.strip().splitlines()[-1])["status"] == "error"


def test_a_bounded_pass_says_more_remain(registry, watermark_path, recorder) -> None:
    """A scheduled pass that leaves a backlog must not look like one that
    finished — that is how a half-covered corpus stays half-covered."""
    seed_traces(registry, 10)
    partial = run_trace_embed_pass(
        registry, watermark_path=watermark_path, max_scan=4, page_size=2
    )
    assert partial.more_remaining is True
    assert partial.to_dict()["more_remaining"] is True

    for _ in range(3):
        final = run_trace_embed_pass(
            registry, watermark_path=watermark_path, max_scan=4, page_size=2
        )
    assert final.more_remaining is False


def test_limit_also_reports_a_remaining_backlog(
    registry, watermark_path, recorder
) -> None:
    seed_traces(registry, 5)
    report = run_trace_embed_pass(registry, watermark_path=watermark_path, limit=2)
    assert report.more_remaining is True
