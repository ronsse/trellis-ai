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

**#195 — edge re-ingest idempotency. DONE (two parts).**
- Part 1 (commit `d6ce259`): added 2 cross-backend contract tests
  (`test_upsert_edge_same_triplet_is_idempotent`, `test_reingest_same_graph_is_idempotent`).
  On SQLite the guarantee already held (edges key off the node_id string), so I initially
  judged "no new logic needed."
- Part 2 — **the contract test then caught a real Bolt bug** when finally run against a
  live Neo4j (the dead AuraDB had hidden it): re-ingesting an identical node+edge set
  *multiplies* the edge count on the Bolt backends (Neo4j + the blessed ArcadeDB). Root
  cause: Bolt stores each node version as a separate `:Node` row; `upsert_nodes_bulk`
  re-versions a node even when unchanged, and the node's current edge stays
  `valid_to=NULL` stranded on the now-closed old row — the next edge upsert misses it and
  writes a duplicate. **This is the actual consumer-kg 14→28 mechanism** (they run on a
  Bolt substrate). Fix: `upsert_nodes_bulk` is now version-preserving when content is
  unchanged (`_node_spec_matches_current` on the base class; extends the existing
  pre-fetch, no new round trip). Validated on a Neo4j 5.26 container — contract (49), e2e
  (6), backend (39) green; SQLite/Postgres graph contracts (93/93) unchanged.
- Lesson: the AuraDB outage had masked this; containerizing the live-infra CI (below)
  and adding the contract suites to it means this class of cross-backend drift is now
  caught on every push.

**Verification:** full `ruff check src/ tests/` clean; full `mypy src/` clean (257 files);
1496 passed / 13 skipped across stores+mutate+api+sdk+extract; OpenAPI stable. `uv.lock`
mypy bump (env rebuild side-effect) reverted — not part of this work.

**Downstream issues confirmed not core-actionable (recommend closing as fixed-in-consumer-kg
or reframing as core-support asks):** #212, #222, #223, #224, and #202's query-history
matching — the symbols (`build_query_history_context`, `classify_query`, `defines_metric`)
live in consumer-kg, not core `src/`.

## Stream 2 — Pilot security floor (2026-07-02, commits `0e62aa5`/`d235e10`/`37d69e2`)

Scoped the four security-labeled issues the same way (core vs. downstream first):

**#206 — JSON error sanitization. DONE (`0e62aa5`).** New shared guard
`trellis.core.error_sanitize`: `sanitize_error_message` (clean text passes through
bounded; email / URL-with-credentials / secret-assignment / 40+ char token-run /
raw-SQL shapes are replaced wholesale with a static marker) +
`sanitized_error_payload` (status / error_type / sanitized message envelope).
Applied to every JSON-mode CLI error site (ingest ×7, extract-refresh ×3, api-keys,
migrate-provenance, skill install) and the API surfaces that embed exception text:
`/readyz` per-backend probe errors (the DSN-leak path), metrics-timeseries 422
detail, vectors-reset response. The unhandled-500 envelope was already generic;
structlog keeps full detail on the operator channel. 14 heuristic tests + an
end-to-end readyz credential-suppression test.

**#193 — ArcadeDB credential split. DONE (`d235e10`).** Optional
`admin_user`/`admin_password` (config or `TRELLIS_ARCADEDB_ADMIN_*` env) used ONLY
for `ensure_database` + edge-provenance DDL + vector `LSM_VECTOR`/property DDL; the
Bolt runtime driver and runtime HTTP SQL always use the least-privilege pair; the
registry strips the admin pair from forwarded constructor params. Unset ⇒ full
fallback to the runtime pair (backward compatible; all existing wiring tests pass
unchanged). ADR gains a "Credential split" section w/ deployment order;
`.env.example` documents the vars. 7 credential-routing unit tests.

**#207 — SQL statement-type allowlist. CLOSED (no core path).** The fix shipped in
consumer-kg; verified by grep that no SQL statement-type classification exists
anywhere in core `src/` — nothing to allowlist. #206's raw-SQL suppression is a
second net under this leak class.

**#194 — classification enforcement. DEFERRED (comment on issue).** It's Tag-Vocab
ADR Phases 1+4, explicitly design-partner-gated in the roadmap ("do not pre-build");
`DataClassification` is unpopulated so enforcement would gate on data no producer
writes. Left open as the tracking issue for that signal.

**Also fixed in passing (`37d69e2`):** the neo4j-marked mock suite
`test_neo4j_upsert_bulk_fast_path.py` still faked the pre-`ab36af6` pre-fetch record
shape (`{node_id, node_role}` vs the new `RETURN n`) and failed with `KeyError: 'n'`
on toggles-on runs — invisible to default CI (marker-deselected). Mocks updated;
new test locks in the #195 unchanged-re-upsert-skips-write semantics at this level.

**Verification:** full ruff + mypy clean; 1397 passed / 4 skipped across
core+cli+api+stores.

## Stream 3 — Backlog cleanup: docs cluster + consumer-kg triage (2026-07-02)

**Docs cluster — all four closed, three were already satisfied on main:**
- #197 (ArcadeDB Bolt plugin) — ADR "Self-hosting requirement" callout + module
  docstring + connection-refused troubleshooting all pre-existed.
- #198 (direct-URL extras) — README "Git-pinned installs" section pre-existed
  (extras→driver table + pyproject example).
- #199 (`from_config_dict`) — the method pre-existed (`fa03f4d`) with docstring
  example + 4 tests in `test_registry_planes.py`.
- #209 (CLI limit semantics) — the misleading text was consumer-kg's; core help
  texts audited (per-command, precise); added help to the two unlabeled
  `--limit` options in `trellis metrics proposals`/`versions`.

**consumer-kg triage — 20 closed, 4 kept:**
- Closed 13 stacked-branch change-records (#214 #225–#235 #239), 5 environment
  findings (#204 #205 #213 #216 #236), #210 (their docs-repo task, executed
  downstream), #215 (stub-materialization, fixed downstream).
- Kept open as pilot-gated core capability asks, each with a boundary-analysis
  comment: **#200** (usage families — guidance + gate primitives, types stay
  open-string), **#201** (BI-metadata extractor — plausible home
  `trellis_workers.extract`), **#202** (re-scoped to guidance + optional shared
  predicate-validation helper), **#203** (aggregate-counts scouting primitive).

**Correction recorded on #211/#215 + `LinkRequest` docs:** `allow_dangling`
skips only the handler FK pre-flight. On Bolt/openCypher backends the store
layer still requires both endpoint vertices (a relationship cannot exist
without endpoints) — a true dangling edge is representable only on
SQLite/Postgres. On Bolt substrates the supported pattern is
materialize-a-stub-endpoint-first (what meta/recorder does in core and
consumer-kg's `run_source_ingest` does downstream). `LinkRequest.allow_dangling`
docstring + OpenAPI spec amended to state this.

**Issue tracker after this stream: 9 open, every one gated** —
#200/#201/#202/#203 (pilot-gated capability asks), #194 (partner-gated
classification enforcement), #208 (blocked: needs ArcadeDB secret + AWS SSO),
#248/#249 (eval build-outs, deliberately deferred / feature-scale), #250
(operator credential hygiene). Everything actionable-now is done.
