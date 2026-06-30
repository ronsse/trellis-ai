# Pilot core / bug fixes — 2026-06-30

**Stream:** "Pilot core + bug fixes" (the unblocked, core-actionable subset of the
consumer-kg pilot cluster). Picked up cold from a clean, fully-synced `main`
(`c80bf5e`). The roadmap's ADR phases A–E are all landed or deferred-pending-signal;
the live work is in GitHub issues.

## Re-scope finding — core vs. downstream

The pilot cluster (#193–#239) is filed in *this* repo's tracker, but several issues
describe fixes **already implemented in the downstream `consumer-kg` repo** against
symbols that **do not exist in core Trellis**. Verified by grep — none of
`build_query_history_context`, `classify_query`, `defines_metric` / `evidenced_by` /
`references_table`, nor any `query_history` module is present in `src/`:

| Issue | Verdict | Why |
|---|---|---|
| #212 (defines_metric retrieval) | **Downstream** | `build_query_history_context` lives in consumer-kg, not core. |
| #222 (saved-query → BI evidence) | **Downstream** | `classify_query` / `test_sql_enrichment.py` not in core. |
| #223 (dedupe stale doc paths) | **Downstream** | query-history retrieval helper is consumer-kg. |
| #224 (transformation_logic) | **Downstream** | consumer-kg PR #4 / `stack/enriched-graph-population`. |
| #202 (broad-keyword false positives) | **Mostly downstream** | query-history matching lives downstream; only generic guard-rail guidance could be core. |

These are not actionable as core code changes here. They should be closed as
"fixed in consumer-kg" or reframed as core-support asks by the owner — out of scope
for this push.

**Core-actionable set: #211, #196, #195.** All three are genuine core-boundary
mutation concerns and the relevant code is in `src/trellis/`.

## Issue 1 — #211: expose `allow_dangling` over the REST LINK_CREATE path

**Root cause (verified):** the `allow_dangling` escape hatch is fully wired
draft → command → handler (`EdgeDraft.allow_dangling` → `extract/commands.py:81`
→ `mutate/handlers.py:313` `LinkCreateHandler`). But the **REST** `create_link`
route (`curate.py:66`) and the bulk `_edge_command` (`ingest.py:158`) build the
`LINK_CREATE` command **without** plumbing `allow_dangling`, so a downstream
curator writing table-reference edges over HTTP cannot opt out of FK validation —
exactly the #211 failure ("target has no current version").

**Fix:**
- Add `allow_dangling: bool = False` to `LinkRequest` and `BulkEdgeItem` DTOs
  (`trellis_wire.dtos`).
- Thread it into `command.args["allow_dangling"]` in `curate.create_link` and
  `ingest._edge_command`.
- (The handler already honors `command.args["allow_dangling"]` — no handler change.)

**Done when:** a REST `POST /curate/links` (and bulk ingest edge) with
`allow_dangling=true` creates an edge whose target has no current node; with the
default `false` it still 400s with the orphan-edge message.

**Out of scope (downstream):** the issue's "retrieval should warn when a table
anchor has no node" — that's the consumer-kg query-history retrieval helper.

## Issue 2 — #196: knowledge-plane-only mutation executor (no EventLog)

**Root cause (verified):** `MutationExecutor` already tolerates `event_log=None`
(`executor.py:433`). The coupling is (a) `build_curate_executor` hardcodes
`event_log=registry.operational.event_log`, and (b) **10 handler sites** call
`self._registry.operational.event_log.emit(...)` directly — bypassing the
executor's None-tolerance — so a knowledge-plane-only registry (no operational
plane) fails inside the handlers.

**Fix (design):**
- Add a `NullEventLog` (no-op `EventLog` ABC implementation) in core.
- Provide a supported builder path for graph-only execution that wires both the
  executor *and* the handlers to the `NullEventLog`, so emission is a documented
  no-op rather than a downstream monkey-patch.
- Seam choice TBD at implementation: prefer the smallest change that keeps the
  loud-on-misconfig behavior for *operational* deployments while making
  knowledge-plane-only an explicit, opt-in mode.

**Done when:** a registry with only knowledge-plane stores configured can build a
curate executor and run ENTITY_CREATE / LINK_CREATE end-to-end with no operational
stores present; emission is a no-op; tests cover it.

## Issue 3 — #195: deterministic edge identity / idempotent re-ingest

**Root cause (verified):** all three backends (SQLite single `upsert_edge` +
`upsert_edges_bulk`, Bolt `upsert_edge`) **already dedupe by
`(source_id, target_id, edge_type)`** via SCD-2 versioning. So the consumer-kg
14→28 multiplication is **not** a missing store-level dedup — it is endpoint-ID
instability (`EdgeDraft` carries no deterministic identity, and within a single
`upsert_edges_bulk` batch, two identical triplets are not collapsed before insert).

**Fix (framed for core):**
- Confirm/verify the in-batch duplicate gap in `upsert_edges_bulk` (same triplet
  twice in one call) and close it across backends.
- Add a cross-backend idempotency **contract test** (re-ingest a fixture twice →
  node *and* edge counts stable) to `graph_store_contract.py`.
- Decide whether `EdgeDraft` needs a caller-supplied deterministic key vs. relying
  on the triplet; document the identity contract.

**Done when:** re-ingesting the same logical edges (single + bulk, in-batch dupes
included) leaves edge counts stable on SQLite/Postgres/Bolt, covered by a contract
test.

## Sequence

1. **#211** — smallest, highest-confidence (DTO + 2 command builders + tests).
2. **#196** — `NullEventLog` + knowledge-plane builder path.
3. **#195** — in-batch dedup + cross-backend idempotency contract test.

Hard rules in play: all mutations through `MutationExecutor`; schemas `extra="forbid"`;
`structlog` not `print`; `--format json` for CLI; type hints on public APIs;
new graph-store semantics get a contract test, not just a backend test.

## Outcome — 2026-06-30 (all three landed locally, unpushed)

**#211 — `allow_dangling` over the wire. DONE.**
- `LinkRequest.allow_dangling: bool = False` added (`trellis_wire/dtos.py`); propagates
  to `BulkEdgeItem`.
- Threaded into `command.args["allow_dangling"]` in `curate.create_link` and
  `ingest._edge_command`. Handler already honored it — no handler change.
- SDK sync + async `create_link(..., allow_dangling=False)`; flag sent only when
  `True` so a new client stays compatible with an older `extra=forbid` server.
- `docs/api/v1.yaml` regenerated (stable).
- Tests: 3 route (`test_routes.py`) + 2 SDK (sync/async). MCP needed no change —
  `save_knowledge` already skips+warns on missing target; `execute_mutation`/LINK_CREATE
  honors the flag via command args.

**#196 — knowledge-plane-only executor. DONE.**
- `NullEventLog` (`src/trellis/stores/null/event_log.py`): no-op `EventLog`. `append`
  drops; `emit` (inherited) still returns a valid id-bearing Event so callers reading
  `event_id` keep working; reads empty; `has_idempotency_key` always `False`.
- Registered as the `null` `event_log` backend in `_BUILTIN_BACKENDS`. Reaches the
  executor *and* the 10 handler sites that emit through `registry.operational.event_log`
  — no per-site refactor, no monkey patch. Needs no `stores_dir`.
- `build_curate_executor` docstring documents the knowledge-plane-only path.
- ruff per-file-ignore (ARG002) for the no-op stub.
- Tests: 9 (`test_null_event_log.py`) + 3 end-to-end (`test_knowledge_plane_executor.py`).

**#195 — edge re-ingest idempotency. DONE (no new logic needed).**
- Finding: the dedup machinery the issue asks for **already exists** — SCD-2 collapses
  the same `(source,target,type)` triplet to one current version (single + bulk, all
  backends), and the shared base `_pre_validate_edges_bulk` already *rejects* in-batch
  duplicate triplets. The consumer-kg 14→28 multiplication is **node-ID instability**
  (auto-assigned ids vs deterministic `entity_id`), a caller contract already supported.
  Verified empirically on SQLite (nodes 2→2, edges 1→1 on re-ingest).
- Contribution: 2 cross-backend contract tests in `graph_store_contract.py`
  (`test_upsert_edge_same_triplet_is_idempotent`, `test_reingest_same_graph_is_idempotent`)
  locking the guarantee in for every backend incl. future ArcadeDB. Recommend closing
  #195 as satisfied; the speculative `EdgeDraft.edge_id`/qualifier identity stays deferred
  (no consumer needs same-pair multi-edges). #196 also obviates consumer-kg's custom
  handlers (the workaround that bypassed the idempotent upsert), addressing the root cause.

**Verification:** full `ruff check src/ tests/` clean; full `mypy src/` clean (257 files);
1496 passed / 13 skipped across stores+mutate+api+sdk+extract; OpenAPI stable. `uv.lock`
mypy bump (env rebuild side-effect) reverted — not part of this work.

**Downstream issues confirmed not core-actionable (recommend closing as fixed-in-consumer-kg
or reframing as core-support asks):** #212, #222, #223, #224, and #202's query-history
matching — the symbols (`build_query_history_context`, `classify_query`, `defines_metric`)
live in consumer-kg, not core `src/`.
