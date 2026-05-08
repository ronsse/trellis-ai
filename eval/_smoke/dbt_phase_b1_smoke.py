"""Phase B-1 smoke — dbt corpus convergence at configurable rounds.

Run via::

    op run --env-file=.env -- uv run python -m eval._smoke.dbt_phase_b1_smoke
    op run --env-file=.env -- uv run python -m eval._smoke.dbt_phase_b1_smoke --rounds 100 --feedback-batch-size 10

Cost: under $0.001 per run (embeddings only — no LLM chat for B-1).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

from eval.scenarios.dbt_corpus_convergence.scenario import run as run_scenario
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
    parser = argparse.ArgumentParser(description="Phase B-1 smoke runner")
    parser.add_argument("--rounds", type=int, default=12)
    parser.add_argument("--feedback-batch-size", type=int, default=4)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--no-graphsearch",
        action="store_true",
        help="Disable SeededGraphSearch (run KeywordSearch + SemanticSearch only)",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as stores_dir, StoreRegistry(
        config=SQLITE_REGISTRY_CONFIG, stores_dir=Path(stores_dir)
    ) as registry:
        report = run_scenario(
            registry,
            seed=args.seed,
            rounds=args.rounds,
            feedback_batch_size=args.feedback_batch_size,
            enable_graph_search=not args.no_graphsearch,
        )

    payload = asdict(report)
    print(json.dumps(payload, indent=2))
    print()
    print("=" * 60)
    print(f"Status: {report.status}")
    print()
    print("Cost & latency:")
    for k in (
        "cost.total_usd",
        "embedder.calls_total",
        "latency.embed_ms_p50",
        "latency.embed_ms_max",
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
    print("Per-skill success rate:")
    for k in sorted(report.metrics):
        if k.startswith("per_skill.") and k.endswith(".success_rate"):
            print(f"  {k}: {report.metrics[k]}")
    print()
    print("Per-difficulty success rate:")
    for k in sorted(report.metrics):
        if k.startswith("per_difficulty.") and k.endswith(".success_rate"):
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
