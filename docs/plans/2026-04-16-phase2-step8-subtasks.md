# Phase 2 Step 8 — Sub-agent work orders

Step 8 of the Tiered Extraction Pipeline Phase 2 plan: lift LLM-client
construction out of the MCP server and into `StoreRegistry` so it
reads from `~/.config/trellis/config.yaml` as a first-class dependency. Currently
the MCP server builds an LLM client ad-hoc from `OPENAI_API_KEY` /
`ANTHROPIC_API_KEY` env vars ([server.py `_build_llm_client_from_env`](../../src/trellis/mcp/server.py)) —
that works for the feature-flagged rollout but doesn't scale to
deployments that mount secrets from files or want per-component model
routing.

Each section below is a **self-contained brief** — paste it as the
prompt to a fresh agent. Dependencies are called out explicitly so
sub-tasks can be handed off in the right order (or in parallel when
independent).

**Status:** Steps 1–7 of Phase 2 are DONE and pushed. See
[TODO.md — Tiered Extraction Pipeline — Phase 2 Plan](../../TODO.md#tiered-extraction-pipeline--phase-2-plan).

**Dependencies between sub-tasks:**

```
8A (config plumbing) ──┬── 8B (MCP wiring)
                       └── 8C (admin diagnostic)
8D (docs) ── independent; land after 8A–8C
```

---

## 8A — LLM client config plumbing in `StoreRegistry`

**Goal.** Add `StoreRegistry.build_llm_client()` and
`StoreRegistry.build_embedder_client()` that read an optional `llm:`
block from `~/.config/trellis/config.yaml` and construct provider clients.
Return `None` when the config is absent or incomplete — never raise.
Core must stay dependency-free: provider SDK imports must be lazy
(inside the build method body).

**Context to read (do not read beyond this list):**
- [`src/trellis/stores/registry.py`](../../src/trellis/stores/registry.py) — focus on `from_config_dir()` and `_build_openai_embedding_fn` as the pattern to mirror
- [`src/trellis/llm/protocol.py`](../../src/trellis/llm/protocol.py) — `LLMClient` and `EmbedderClient` protocols
- [`src/trellis/llm/providers/openai.py`](../../src/trellis/llm/providers/openai.py) and [`anthropic.py`](../../src/trellis/llm/providers/anthropic.py) — constructor shapes
- [`docs/design/adr-llm-client-abstraction.md`](../design/adr-llm-client-abstraction.md) §2.1–2.3 — design intent
- `CLAUDE.md` "Hard Rules" — especially structlog, no print, type hints

**Config schema to implement.** Nested under a top-level `llm:` key
in `~/.config/trellis/config.yaml`:

```yaml
llm:
  provider: openai          # or "anthropic"
  api_key_env: OPENAI_API_KEY   # env var name (preferred)
  # api_key: sk-...             # OR literal (discouraged)
  model: gpt-4o-mini            # default model for generate()
  base_url: https://...         # optional, for proxies / self-hosted
  embedding:                    # optional sub-block
    provider: openai
    api_key_env: OPENAI_API_KEY
    model: text-embedding-3-small
```

Rules:
- Exactly one of `api_key` / `api_key_env` must be present.
- `api_key_env` is preferred; document that in a comment near the
  method.
- Embedding sub-block falls back to parent provider/api_key when
  omitted.

**Constraints.**
- Never log raw `api_key` values. Mask to `"sk-...X4F"` form (keep last
  4 chars) in debug logs.
- Provider SDK imports MUST be inside the build method body, gated on
  config presence. Do not import at module scope.
- `build_llm_client()` / `build_embedder_client()` return `None` when:
  - config file absent
  - `llm:` block absent
  - provider missing or unknown
  - api_key resolution fails (neither literal nor env var set)
  - provider SDK not installed (`ModuleNotFoundError`)
- Log at `info` on successful construction with `provider`, `model`,
  `masked_key` fields.
- Log at `debug` on each failure mode. Never raise.

**Deliverables.**
1. `src/trellis/stores/registry.py` — new `build_llm_client()` and
   `build_embedder_client()` methods on `StoreRegistry`, plus a private
   `_resolve_api_key(cfg)` helper used by both. Refactor the existing
   `_build_openai_embedding_fn` to call `_resolve_api_key` for
   consistency (don't change its return type — still sync
   `Callable[[str], list[float]]` — because `SemanticSearch.search()`
   is sync per ADR §3 Phase 3).
2. `tests/unit/stores/test_registry_llm.py` — new test file:
   - happy path for OpenAI provider via `api_key_env`
   - happy path for Anthropic provider via literal `api_key`
   - missing `llm:` block → returns `None`
   - unknown provider → returns `None`, debug log
   - neither `api_key` nor `api_key_env` → returns `None`
   - `api_key_env` pointing at an unset env var → returns `None`
   - SDK not installed (monkeypatch `ModuleNotFoundError`) → returns `None`
   - embedding sub-block present → `build_embedder_client()` returns
     configured client
   - embedding sub-block absent → inherits from parent `llm:` block

**Out of scope.**
- Wiring into MCP server (sub-task 8B)
- CLI diagnostic (sub-task 8C)
- Doc updates (sub-task 8D)
- Any changes to `LLMClient`/`EmbedderClient` protocol shapes

**Done when.**
- `python -m pytest tests/unit/stores/test_registry_llm.py -v` passes
- `python -m mypy src/trellis/stores/` clean
- `python -m ruff check src/trellis/stores/ tests/unit/stores/` clean
- Pre-existing `tests/unit/stores/` suite still passes

---

## 8B — MCP `save_memory` uses `registry.build_llm_client()`

**Depends on:** 8A landed and committed.

**Goal.** Replace the env-var-only path in
[`src/trellis/mcp/server.py`](../../src/trellis/mcp/server.py)
`_build_llm_client_from_env()` with a thin wrapper that prefers
`registry.build_llm_client()` (from 8A) and falls back to the
existing env-var construction when the registry returns `None`. This
preserves backward compatibility for deployments that only set env
vars.

**Context to read:**
- [`src/trellis/mcp/server.py`](../../src/trellis/mcp/server.py) — `_get_memory_extractor`, `_build_llm_client_from_env` (~lines 66–170)
- [`tests/unit/mcp/test_server.py`](../../tests/unit/mcp/test_server.py) `TestSaveMemoryExtractionFeatureFlag` — 4 existing tests that monkeypatch `_build_llm_client_from_env`
- 8A's delivered `registry.build_llm_client()` method signature

**Behavior.**
1. Rename `_build_llm_client_from_env(registry)` to
   `_build_llm_client(registry)`.
2. New logic: first call `registry.build_llm_client()`. If non-None,
   return it and log `"llm_client_from_registry"` at debug.
3. Else, fall through to the existing OpenAI-then-Anthropic env
   lookup. Log `"llm_client_from_env"` at debug on success.
4. Else, return `None` (unchanged).
5. Update the call site in `_get_memory_extractor`.

**Deliverables.**
- `src/trellis/mcp/server.py` — refactor as above. Keep
  `_build_llm_client_from_env` as a private helper called by
  `_build_llm_client` when the registry returns None, so the test
  monkeypatch surface stays stable. Or rename the tests; either is
  fine, just be consistent.
- `tests/unit/mcp/test_server.py` — keep the 4 existing tests green.
  Add ONE new test: registry-configured LLM preempts env vars
  (monkeypatch `registry.build_llm_client` to return a fake; set
  `OPENAI_API_KEY` too; verify the registry-sourced client is used,
  not the env path).

**Out of scope.**
- Changes to `_build_alias_resolver`
- Config schema changes (done in 8A)
- Behavior of the MCP feature flag itself

**Done when.**
- `python -m pytest tests/unit/mcp/ -q` passes (44 tests + 1 new)
- `python -m ruff check src/trellis/mcp/ tests/unit/mcp/` clean
- `python -m mypy src/trellis/mcp/` clean
- `TRELLIS_ENABLE_MEMORY_EXTRACTION=1` with only env vars set still works
  (backward compat)

---

## 8C — `trellis admin check-extractors` diagnostic

**Depends on:** 8A landed (for `registry.build_llm_client()`).

**Goal.** Add a CLI command that reports extractor readiness — which
extractors are registered, their tiers, and whether their
dependencies (LLM client, memory-extraction feature flag) are
configured. Exit codes enable CI gating.

**Context to read:**
- [`src/trellis_cli/admin.py`](../../src/trellis_cli/admin.py) — find the `graph_health` command; mirror its structure (flags, output formatting, exit codes)
- [`src/trellis/extract/registry.py`](../../src/trellis/extract/registry.py) — `ExtractorRegistry` API
- [`tests/unit/cli/test_admin.py`](../../tests/unit/cli/test_admin.py) — existing test pattern
- `CLAUDE.md` "Hard Rules" — `--format json` support is required

**Output (text format).**
```
Tiered Extraction — Readiness Report

Extractors registered:
  ✓ alias_match          [deterministic] save_memory
  ✓ llm_memory           [llm]           save_memory
  ✓ hybrid(alias_match+llm_memory) [hybrid] save_memory

Dependencies:
  ✓ LLM client configured (provider=openai, model=gpt-4o-mini)
  ✓ TRELLIS_ENABLE_MEMORY_EXTRACTION=1
```

When LLM-tier extractors exist but no LLM client is buildable, emit a
warning block and exit 2. When all is well, exit 0. When there are
non-critical warnings (flag unset but clients buildable), exit 1.

**Deliverables.**
- `src/trellis_cli/admin.py` — new `check_extractors` command
- `tests/unit/cli/test_admin.py` — tests for:
  - all green path
  - LLM tier registered but no LLM client configured → exit 2
  - flag unset → exit 1 with warning
  - `--format json` produces machine-readable output
- Extractors to inspect come from whatever registration flow the MCP
  server uses — if that's not exposed, the command can construct a
  default registry via `build_save_memory_extractor` with a stub
  alias resolver + the registry's LLM client to answer the "can we
  build one?" question without actually registering anything.

**Out of scope.**
- Changing how extractors are registered in the MCP server or workers
- Adding new extractor capabilities

**Done when.**
- Command runs cleanly: `trellis admin check-extractors` and
  `trellis admin check-extractors --format json`
- Tests pass
- Lint + mypy clean

---

## 8D — Docs + config template + Phase 4 ADR close-out

**Depends on:** 8A–8C landed (so docs describe reality).

**Goal.** Close out the docs story for Phase 2. No code changes.

**Context to read:**
- [`docs/agent-guide/playbooks.md`](../agent-guide/playbooks.md) — existing structure; find a good insertion point
- [`docs/design/adr-llm-client-abstraction.md`](../design/adr-llm-client-abstraction.md) — §5 Implementation Plan, Phase 4 section
- [`TODO.md`](../../TODO.md) — Phase 2 plan section; Step 8 and Step 9 entries
- `trellis admin init` command — find where the default `config.yaml` template lives

**Deliverables.**
1. **Config template** — add commented-out `llm:` block to the
   default `config.yaml` written by `trellis admin init`. Use the exact
   schema from 8A. Include a one-line comment per key.
2. **Playbook entry** — new "Configuring LLM extraction" section in
   `docs/agent-guide/playbooks.md`. Cover:
   - Setting the env flag (`TRELLIS_ENABLE_MEMORY_EXTRACTION=1`)
   - Choosing env-var vs config-file LLM wiring
   - Running `trellis admin check-extractors` to verify
   - What happens when LLM isn't configured (graceful degrade)
3. **ADR Phase 4 close** — in `docs/design/adr-llm-client-abstraction.md`
   §5 Phase 4, mark the shipped items: `LLMExtractor`,
   `HybridJSONExtractor`, `build_save_memory_extractor` factory,
   prompt scaffolding. Leave unshipped items (`CrossEncoderClient`
   implementations, LLM-assisted dedup, full prompt library) as open.
4. **TODO update** — mark Steps 8 and 9 DONE in
   `TODO.md#tiered-extraction-pipeline--phase-2-plan`. Strike the
   matching items in the "Deprioritized / deferred" list that are
   now actually shipped (`LLMExtractor`, `SaveMemoryExtractor`,
   `HybridJSONExtractor`, prompt library pattern).

**Out of scope.**
- Any code changes (except the config template, which is text)
- New docs beyond what's listed

**Done when.**
- `trellis admin init` in a clean dir produces a `config.yaml` with the
  new `llm:` commented example
- Playbook reads coherently end-to-end
- ADR and TODO reflect reality
- `python -m pytest` still green (in case the config template change
  is tested)

---

## Hand-off order recommendation

1. **8A first** (solo) — blocks 8B and 8C.
2. **8B and 8C in parallel** after 8A — no overlap between them.
3. **8D last** — so docs describe what actually shipped.

If you want to split further, 8A is the largest brief and could
itself be two agents (one for `LLMClient`, one for `EmbedderClient`
+ `_resolve_api_key` refactor), but the shared `_resolve_api_key`
helper makes that awkward. Prefer keeping 8A as one agent.
