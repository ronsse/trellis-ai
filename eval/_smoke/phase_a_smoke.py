"""Phase A smoke — run the real-LLM scenario at a configurable round count.

Builds an in-memory SQLite registry, invokes the
``agent_loop_convergence_real_llm`` scenario, prints a JSON report +
human-readable summary.

Round count via ``--rounds N`` (default 30). The Phase A target chart
in ``docs/design/plan-real-corpus-eval.md`` §5.1 wants ``--rounds 100``.

Run via::

    op run --env-file=.env -- uv run python -m eval._smoke.phase_a_smoke
    op run --env-file=.env -- uv run python -m eval._smoke.phase_a_smoke --rounds 100

Cost: ~$0.006-0.01 per run regardless of round count (LLM fires only
at setup; round loop only embeds the query once per round).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

# Force stdout to UTF-8 so unicode characters (→, Δ) in finding messages
# don't crash on Windows' default cp1252. Has to happen before any print.
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

from eval.scenarios.agent_loop_convergence_real_llm.scenario import run as run_scenario
from trellis.stores.registry import StoreRegistry

SQLITE_REGISTRY_CONFIG = {
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase A smoke runner")
    parser.add_argument("--rounds", type=int, default=30)
    parser.add_argument("--feedback-batch-size", type=int, default=5)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as stores_dir, StoreRegistry(
        config=SQLITE_REGISTRY_CONFIG, stores_dir=Path(stores_dir)
    ) as registry:
        report = run_scenario(
            registry,
            seed=args.seed,
            rounds=args.rounds,
            feedback_batch_size=args.feedback_batch_size,
        )

    payload = asdict(report)
    # Print the report — JSON for the bot, human-readable summary at the end
    print(json.dumps(payload, indent=2))
    print()
    print("=" * 60)
    print(f"Status: {report.status}")
    print(f"Duration: {report.duration_seconds:.2f}s")
    print()
    print("Cost summary:")
    for k in (
        "cost.total_usd",
        "cost.chat_usd",
        "cost.embed_usd",
        "llm.calls_total",
        "embedder.calls_total",
        "latency.chat_ms_p50",
        "latency.chat_ms_max",
        "latency.embed_ms_p50",
    ):
        if k in report.metrics:
            print(f"  {k}: {report.metrics[k]}")
    print()
    print("Convergence:")
    for k in (
        "convergence.useful_delta",
        "convergence.weighted_delta",
        "round_success_rate",
        "round_useful_fraction_overall",
    ):
        if k in report.metrics:
            print(f"  {k}: {report.metrics[k]}")
    print()
    print(f"Findings ({len(report.findings)}):")
    for f in report.findings:
        print(f"  [{f.severity}] {f.message}")
    print()
    print(f"Decision: {report.decision}")

    return 0 if report.status in {"pass", "regress"} else 1


if __name__ == "__main__":
    sys.exit(main())
