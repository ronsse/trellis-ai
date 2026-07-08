# Audit — `src/trellis_workers/` orphan modules (2026-05-14)

> *Historical note: references to WorkflowEngine below predate its retirement (2026-05-18); see `docs/research/workflow-engine-disposition.md`.*

**Scope:** C1.9 of [`plan-cleanup-dead-code.md`](./plan-cleanup-dead-code.md) — a
read-only audit of every module under `src/trellis_workers/` to identify
orphans (no production callers, no `__init__.py` re-export) after the
recent simplification waves. **No code or test changes are part of this
audit.** Per the plan: "the decision per-module belongs to a human, not
the cleanup swarm. The PR is read-only audit if no human signal is
attached."

Base SHA: `2ca9584` (v0.9.0 CHANGELOG bump).

## Methodology

For each `*.py` file under `src/trellis_workers/` we (1) counted lines
with `Get-Content … Measure-Object -Line`, (2) grepped
`src/trellis`, `src/trellis_cli`, `src/trellis_api`, `src/trellis_sdk`,
and `src/trellis_workers` for production imports of the module's public
symbols (`PrecedentMiner`, `RetentionWorker`, `StalenessDetector`,
`WorkflowEngine`, `ThinkingPolicy`, `QueryPatternObserver`,
`QueryLogRecord`, `EnrichmentService`, `EnrichmentResult`,
`normalize_tag`, `ProposalGenerator`, `Proposal`, `Cluster`,
`cluster_failures`, `DbtManifestExtractor`, `OpenLineageExtractor`),
(3) grepped `tests/` for the same, and (4) inspected
`src/trellis_workers/__init__.py` and each sub-package `__init__.py` to
record whether the module is re-exported.

A module is **orphan-suspect** when its only callers live under `tests/`
*and* it is not re-exported from its package `__init__.py`. A re-exported
module whose re-export has no consumer is treated as a heightened
orphan-suspect (the re-export is dead code too).

*(Post-audit note: `engine/thinking.py` was subsequently deleted in Phase F F0, `1291210`.)* Per C1.6 / C1.7 plan guidance, `engine/thinking.py` (WorkflowEngine) and
`enrichment/service.py` (EnrichmentService's triggered-consumer stubs)
are deliberately left in place pending real workload signal — they are
annotated `deliberate-no-action` so this audit doesn't re-litigate that
decision.

## Audit table

| Module path | LOC | Production callers (`src/`) | Test callers (`tests/`) | Re-exported in package `__init__.py`? | Classification |
|---|---:|---|---|---|---|
| `src/trellis_workers/__init__.py` | 1 | (n/a — package init) | (n/a) | (n/a) | active |
| `src/trellis_workers/code_authoring/__init__.py` | 51 | `src/trellis_cli/admin_proposals.py` (lines 52, 175 — imports `Proposal`, `ProposalGenerator`) | `tests/unit/workers/code_authoring/test_generator.py` | (n/a — is the package init) | active |
| `src/trellis_workers/code_authoring/clustering.py` | 205 | re-exported via `code_authoring/__init__.py`; imported by `code_authoring/generator.py` | `tests/unit/workers/code_authoring/test_clustering.py`, `…/test_proposal.py` | yes (`Cluster`, `cluster_failures`, `compute_cluster_signature`) | active |
| `src/trellis_workers/code_authoring/generator.py` | 284 | re-exported via `code_authoring/__init__.py`; imported by `src/trellis_cli/admin_proposals.py` line 175 | `tests/unit/workers/code_authoring/test_generator.py` | yes (`DEFAULT_WINDOW`, `PROPOSAL_GENERATOR_AGENT_ID`, `PROPOSAL_GENERATOR_ANALYZER_NAME`, `ProposalGenerator`) | active |
| `src/trellis_workers/code_authoring/proposal.py` | 216 | re-exported via `code_authoring/__init__.py`; imported by `code_authoring/generator.py` (line 44); re-export consumed by `src/trellis_cli/admin_proposals.py` line 52 | `tests/unit/workers/code_authoring/test_proposal.py` | yes (`MARKDOWN_PREVIEW_CHARS`, `MAX_SAMPLE_EVENT_IDS_IN_MARKDOWN`, `Proposal`, `compute_proposal_id`, `render_markdown`) | active |
| `src/trellis_workers/engine/__init__.py` | 1 | (n/a — package init, no exports) | (n/a) | (n/a) | deliberate-no-action per C1.6 (parent package of `thinking.py`) |
| `src/trellis_workers/engine/thinking.py` | 241 | **none** (`WorkflowEngine`, `ThinkingPolicy`, `WorkflowTier` referenced nowhere in `src/`) | `tests/unit/workers/engine/test_thinking.py` | no | **deliberate-no-action per C1.6** — TODO.md / plan C1.6 explicitly say "validate before deleting; do not delete in this cleanup track" |
| `src/trellis_workers/enrichment/__init__.py` | 11 | re-exports consumed indirectly via `enrichment.service` import in `src/trellis/classify/classifiers/llm.py` (uses the submodule path, not the package re-export) | (none directly; tests import via `enrichment.service`) | (n/a — is the package init) | active |
| `src/trellis_workers/enrichment/service.py` | 235 | `src/trellis/classify/classifiers/llm.py` line 16 imports `EnrichmentService` (production wiring is alive). The triggered-consumer pattern (scheduled sweep / event handler) is unwired by design. | `tests/unit/workers/enrichment/test_service.py`, `tests/unit/classify/test_llm.py`, `tests/unit/classify/test_llm_classifier.py` | yes (`EnrichmentResult`, `EnrichmentService`, `normalize_tag`) | active (LLMFacetClassifier wiring); **deliberate-no-action per C1.7** for the unwired triggered-consumer stubs |
| `src/trellis_workers/extract/__init__.py` | 22 | (n/a — package init) | (n/a) | (n/a) | active |
| `src/trellis_workers/extract/dbt_manifest.py` | 153 | `src/trellis_cli/demo.py` line 1170, `src/trellis_cli/extract_refresh.py` line 66, `src/trellis_cli/ingest.py` line 217 (all via package re-export `from trellis_workers.extract import …`) | `tests/unit/workers/test_ingestion.py` | yes (`DbtManifestExtractor`) | active |
| `src/trellis_workers/extract/openlineage.py` | 174 | `src/trellis_cli/demo.py` line 1170, `src/trellis_cli/extract_refresh.py` line 66, `src/trellis_cli/ingest.py` line 295 (all via package re-export) | `tests/unit/workers/test_ingestion.py` | yes (`OpenLineageExtractor`) | active |
| `src/trellis_workers/extract/query_pattern_observer.py` | 384 | **none in production** — re-exported from `extract/__init__.py` but no consumer of `QueryPatternObserver` / `QueryLogRecord` outside the re-export itself (grep against `src/trellis`, `src/trellis_cli`, `src/trellis_api`, `src/trellis_sdk` returns zero hits). **Context:** this module is the deliberate Phase 3 *sample extractor* shipped under [`plan-observation-entity-type.md`](./plan-observation-entity-type.md) §6 (Item 1, landed via PR #125) — the plan describes it as "the simplest end-to-end demonstration" of the Observation entity-type pipeline. "No production caller" is consistent with the plan; do not delete without re-reading Item 1's Phase 3 contract first. | `tests/unit/workers/extract/test_query_pattern_observer.py` | yes (`QueryLogRecord`, `QueryPatternObserver`) | **orphan-suspect (heightened)** — re-exported but no production caller; tests only. See plan context above before any deletion decision. |
| `src/trellis_workers/learning/__init__.py` | 1 | (n/a — package init, no exports) | (n/a) | (n/a) | parent of an orphan-suspect |
| `src/trellis_workers/learning/miner.py` | 272 | **none** — `PrecedentMiner` appears only inside its own module (`miner.py`) and in *prose docstrings* of `src/trellis/extract/telemetry.py` line 13 and `src/trellis/stores/base/event_log.py` line 129 (those are comments referencing the class, not imports) | `tests/unit/workers/learning/test_miner.py` | no | **orphan-suspect** — zero production callers; tests only |
| `src/trellis_workers/maintenance/__init__.py` | 1 | (n/a — package init, no exports) | (n/a) | (n/a) | parent of an orphan-suspect |
| `src/trellis_workers/maintenance/retention.py` | 220 | **none** (`RetentionWorker`, `RetentionPolicy`, `RetentionReport`, `StalenessDetector` referenced nowhere in `src/`) | `tests/unit/workers/maintenance/test_retention.py` | no | **orphan-suspect** — zero production callers; tests only |

## Summary

Total `*.py` files under `src/trellis_workers/`: **17** (7 `__init__.py`
files including the top-level plus 10 implementation modules).

By classification:

| Classification | Count | Modules |
|---|---:|---|
| active | 10 | `__init__.py` (top-level), `code_authoring/__init__.py`, `code_authoring/clustering.py`, `code_authoring/generator.py`, `code_authoring/proposal.py`, `enrichment/__init__.py`, `enrichment/service.py`, `extract/__init__.py`, `extract/dbt_manifest.py`, `extract/openlineage.py` |
| deliberate-no-action per C1.6 | 2 | `engine/__init__.py`, `engine/thinking.py` |
| deliberate-no-action per C1.7 (partial; overlaps with active) | 1 | `enrichment/service.py` — wired in production via `LLMFacetClassifier`, but the triggered-consumer stubs inside the module are deliberately unwired |
| orphan-suspect | 3 | `extract/query_pattern_observer.py` (heightened — re-exported but no production consumer), `learning/miner.py`, `maintenance/retention.py` |
| parent-of-orphan (no-action; stays as long as child stays) | 2 | `learning/__init__.py`, `maintenance/__init__.py` |

### Orphan-suspect modules — names for the next human reviewer

- `src/trellis_workers/extract/query_pattern_observer.py` (384 LOC) — **Phase 3 sample extractor of [`plan-observation-entity-type.md`](./plan-observation-entity-type.md) (Item 1, landed PR #125).** "No production caller" is by plan design; deletion needs more signal than the audit grep.
- `src/trellis_workers/learning/miner.py` (272 LOC)
- `src/trellis_workers/maintenance/retention.py` (220 LOC)

Combined: **876 LOC** of implementation plus their associated test
modules and the `__init__.py` files for `learning/` and `maintenance/`.

## Next steps

Per C1.9's "validate before deleting" discipline:

1. **Do NOT delete any orphan-suspect module in this audit PR.** The
   decision per-module belongs to a human and requires a signal beyond
   "no current caller." Examples of signals that would justify deletion:
   - The roadmap explicitly drops the use case (e.g., a maintainer
     decides the Phase 3 sample extractor of an already-landed plan has
     served its demonstration purpose and the sample can move to
     `eval/` or be retired).
   - The module is superseded by a `trellis/` core implementation that
     subsumes it.
2. **Confirm `engine/thinking.py` and `enrichment/service.py`** remain
   covered by C1.6 / C1.7 deferrals before re-auditing them. As of this
   audit, both are correctly classified as deliberate-no-action.
3. **For each orphan-suspect, the maintainer's decision per the plan is
   one of**: (a) wire it up to a real caller, (b) collapse it into a
   smaller surface, or (c) delete it (module + tests + any `__init__.py`
   re-exports). The `extract/query_pattern_observer.py` case carries a
   heightened note because the `extract/__init__.py` re-export is itself
   dead — option (c) would also need to drop the re-export tuple.
