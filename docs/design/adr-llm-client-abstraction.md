# ADR: LLM Client Abstraction — Protocol in Core, Implementations Optional

**Status:** Accepted
**Date:** 2026-04-15
**Deciders:** XPG core
**Related:**
- [`./adr-deferred-cognition.md`](./adr-deferred-cognition.md) — LLM never in the write path; enrichment is deferred
- [`../../src/trellis/llm/`](../../src/trellis/llm/) — `LLMClient`, `EmbedderClient`, `CrossEncoderClient` protocols + types
- [`../../src/trellis/llm/providers/`](../../src/trellis/llm/providers/) — Reference OpenAI / Anthropic implementations
- [`../../src/trellis_workers/enrichment/service.py`](../../src/trellis_workers/enrichment/service.py) — `EnrichmentService` consumer
- [`../../src/trellis/classify/classifiers/llm.py`](../../src/trellis/classify/classifiers/llm.py) — `LLMFacetClassifier` wrapping `EnrichmentService`
- [`../../src/trellis_workers/learning/miner.py`](../../src/trellis_workers/learning/miner.py) — `PrecedentMiner` with optional LLM path

---

## 1. Context

### What existed before this ADR

The codebase had a single LLM abstraction — `LLMCallable`, a `Protocol` local to `trellis_workers/enrichment/service.py`:

```python
@runtime_checkable
class LLMCallable(Protocol):
    async def __call__(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> str: ...
```

Three components consumed it:

| Consumer | Package | What it does with the LLM |
|---|---|---|
| `EnrichmentService` | `trellis_workers` | Tags, classification, summary, importance scoring |
| `LLMFacetClassifier` | `trellis` (classify) | Wraps `EnrichmentService` for the classifier pipeline |
| `PrecedentMiner` | `trellis_workers` | Optional failure-pattern analysis |

Embeddings are handled separately — a bare `Callable[[str], list[float]]` built ad-hoc in `StoreRegistry._build_openai_embedding_fn()`, hardcoded to the OpenAI SDK.

No concrete `LLMCallable` implementation shipped with the project, no real deployment depended on it, and `LLMCallable` itself leaked from a worker package instead of living in core. Tests used a `FakeLLM` mock.

### What the backlog needs

Six blocked features need LLM capabilities beyond what `LLMCallable` provides:

| Feature | Needs | Gap in `LLMCallable` |
|---|---|---|
| **`LLMExtractor`** (entity/edge extraction) | Multi-turn prompts, token tracking for cost analysis | No message list, no usage reporting |
| **`HybridJSONExtractor`** | Conditional LLM fallback with cost awareness | No usage reporting |
| **`SaveMemoryExtractor`** | Fast extraction in `save_memory` path | No model routing (want cheaper model) |
| **`CrossEncoderClient`** rerankers | Pair scoring (query, candidate) | Entirely different operation shape |
| **LLM-assisted dedup** | Embedding similarity + LLM confirmation | No embedding support; needs both clients |
| **Prompt library** | Versioned templates with usage aggregation | No usage reporting, no prompt identity |

The `WorkflowEngine` (`trellis_workers/engine/thinking.py`) also defines `TierConfig` with model/temperature/max_tokens per cognition tier — configuration that has no client to target.

### The decision to make

Do we:
- **(A)** Keep the status quo — consumers bring everything, core stays LLM-free
- **(B)** Define richer Protocols in core, ship reference implementations as optional extras
- **(C)** Depend on a multi-provider library (litellm) as the abstraction layer

---

## 2. Decision

**Option B: Protocol in core, reference implementations behind optional extras.**

### 2.1 Two new Protocols in `trellis.llm`

```
src/trellis/llm/
    __init__.py          # re-exports protocols + types
    protocol.py          # LLMClient, EmbedderClient protocols
    types.py             # Message, LLMResponse, TokenUsage, EmbeddingResponse
```

**`LLMClient` protocol** — replaces `LLMCallable`:

```python
class Message(TrellisModel):
    role: Literal["system", "user", "assistant"]
    content: str

class TokenUsage(TrellisModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

class LLMResponse(TrellisModel):
    content: str
    model: str | None = None
    usage: TokenUsage | None = None

@runtime_checkable
class LLMClient(Protocol):
    async def generate(
        self,
        *,
        messages: list[Message],
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> LLMResponse: ...
```

**`EmbedderClient` protocol** — replaces the bare `Callable[[str], list[float]]`:

```python
class EmbeddingResponse(TrellisModel):
    embedding: list[float]
    model: str | None = None
    usage: TokenUsage | None = None

@runtime_checkable
class EmbedderClient(Protocol):
    async def embed(self, text: str, *, model: str | None = None) -> EmbeddingResponse: ...
    async def embed_batch(self, texts: list[str], *, model: str | None = None) -> list[EmbeddingResponse]: ...
```

### 2.2 Reference implementations behind optional extras

```
src/trellis/llm/
    providers/
        __init__.py
        openai.py        # OpenAIClient(LLMClient), OpenAIEmbedder(EmbedderClient)
        anthropic.py     # AnthropicClient(LLMClient)
```

```toml
# pyproject.toml
[project.optional-dependencies]
llm-openai = ["openai>=1.0"]
llm-anthropic = ["anthropic>=0.40"]
```

Each implementation is ~100-150 LOC: constructor (api_key, base_url, default_model), `generate()` mapping to the provider SDK, retry with exponential backoff, and `TokenUsage` extraction from the provider response.

Consumers install `trellis-ai[llm-openai]` or `trellis-ai[llm-anthropic]`. Core (`trellis`) never imports these — they are late-bound via `StoreRegistry`-style config or direct construction.

### 2.3 Clean cut-over — no backward compatibility

`LLMCallable` had no external consumers. Rather than carry a dual-accept union and an adapter class, the old protocol was deleted outright:

- `trellis_workers/enrichment/service.py` — `LLMCallable` removed; `EnrichmentService.__init__` accepts `LLMClient` only.
- `trellis_workers/learning/miner.py` — `PrecedentMiner.__init__` accepts `LLMClient | None` only.
- `EnrichmentResult` gained a `usage: TokenUsage | None` field so callers can surface per-request token counts.

No adapter ships; callers with existing `LLMCallable`-shaped code can write ~10 lines of glue to satisfy `LLMClient` if needed.

### 2.4 What is NOT in scope

- **Structured output parsing.** Callers parse JSON from `LLMResponse.content`, as `EnrichmentService` already does. Baking `response_model: type[T]` into the protocol couples it to Pydantic and to provider-specific structured-output features. Keep it out.
- **Streaming.** No current consumer needs it. Add a `stream()` method to the protocol later if needed.
- **Caching.** Response caching is an orthogonal concern (decorator or middleware pattern). Not part of the protocol.
- **Prompt library.** Jinja2 templates and prompt versioning are a layer above `LLMClient`, not inside it. They consume `LLMClient.generate()`. Build when the first extractor lands.

### 2.5 CrossEncoderClient is a separate protocol

Cross-encoder reranking is not text generation — it scores `(query, candidate)` pairs. It gets its own protocol alongside `LLMClient`:

```python
@runtime_checkable
class CrossEncoderClient(Protocol):
    async def score_pairs(
        self, query: str, candidates: list[str], *, model: str | None = None,
    ) -> list[float]: ...
```

Implementations: local `sentence-transformers` (already ships as `BGECrossEncoder` behind `[rerank]` extra), or API-based (OpenAI, Cohere) behind `[llm-openai]`, etc.

---

## 3. Options considered

### Option A: Status quo — consumers bring everything

**How it works:** `LLMCallable` stays. Each consumer (fd-poc, trellis-platform, etc.) instantiates their own OpenAI/Anthropic client and wraps it in a callable. Core ships no LLM code beyond the bare protocol.

**Pros:**
- Zero core complexity and maintenance burden
- No provider SDK dependencies, even optional
- Maximum flexibility for consumers

**Cons:**
- Every consumer reimplements: retries, token tracking, model routing, error normalization
- Cannot ship standardized extractors (`LLMExtractor`, `SaveMemoryExtractor`) because they have no client to call — each consumer must wire them up from scratch
- `WorkflowEngine.TierConfig` (model, temperature, max_tokens) has no client to target
- Embedding abstraction remains ad-hoc (`Callable[[str], list[float]]` in `StoreRegistry`)
- The "bring your own" barrier delays adoption — new users must write glue code before enrichment works

**Verdict:** This was the right choice when enrichment was experimental. Now that 6+ features are blocked, the cost of not standardizing exceeds the cost of maintaining two thin wrappers.

### Option C: Depend on litellm

**How it works:** Add `litellm` as a core or optional dependency. Use `litellm.acompletion()` as the generation interface. Get 100+ provider support for free.

**Pros:**
- Massive provider coverage (OpenAI, Anthropic, Azure, Bedrock, Vertex, Ollama, etc.)
- Built-in retries, fallbacks, load balancing, cost tracking
- Community maintained; fast to adopt new providers

**Cons:**
- Heavy transitive dependency tree (pulls `openai`, `tiktoken`, `tokenizers`, `httpx`, and more)
- Opinionated interface — wraps everything through OpenAI's message format, which leaks into our protocol
- Version churn and breaking changes (litellm moves fast)
- Abstracts away provider-specific features (Anthropic's extended thinking, tool use patterns)
- Violates the project's pattern of Protocol-based injection with late-binding — litellm is a concrete dependency, not a protocol
- SQLite-local deployments (no LLM needed) still pull the dependency graph unless it's optional — and if it's optional, we need the protocol anyway

**Verdict:** litellm is the right tool for applications that talk to many providers. XPG is a library — it should define the interface and let consumers pick the backend. Nothing stops a consumer from writing a `LiteLLMClient(LLMClient)` adapter (~30 LOC).

---

## 4. Consequences

### Positive

- **Unblocks Sprint F.** `LLMExtractor`, `HybridJSONExtractor`, `SaveMemoryExtractor`, prompt library, and LLM-based reranking can all target `LLMClient` without waiting for consumers to invent their own wiring.
- **Token tracking becomes standard.** `TokenUsage` on every response enables cost analysis, budget enforcement, and the `tokens_used` field on `ExtractionResult` without per-consumer plumbing.
- **Embedding abstraction standardized.** `EmbedderClient` replaces the ad-hoc `Callable[[str], list[float]]` in `StoreRegistry`, and gains batch support and usage tracking.
- **`TierConfig` connects to reality.** `WorkflowEngine` can route to different models via `LLMClient.generate(model=tier_config.model)`.
- **Core stays dependency-free.** `trellis.llm.protocol` and `trellis.llm.types` are pure Python + Pydantic. No SDK imports.

### Negative

- **Two provider wrappers to maintain.** OpenAI and Anthropic SDKs evolve; wrapper code may need updates. Mitigation: wrappers are thin (~100-150 LOC each), covered by unit tests that run against injected mocks.
- **Protocol surface area grows.** `LLMClient` is richer than `LLMCallable`. Mitigation: the protocol has exactly one method (`generate`). Complexity is in the types, not the interface.

### Neutral

- `LLMCallable` was deleted rather than deprecated — it had no production consumers, so the compatibility tax was not worth paying.
- Consumers who prefer litellm, LangChain, or another framework write a thin adapter implementing `LLMClient`. This is the expected and encouraged pattern.

---

## 5. Implementation plan

Ordered for incremental delivery. Each phase is independently shippable.

### Phase 1: Protocols and types — DONE

1. `src/trellis/llm/` created with `protocol.py`, `types.py`, `__init__.py`.
2. `LLMClient`, `EmbedderClient`, `CrossEncoderClient` protocols defined alongside `Message`, `LLMResponse`, `TokenUsage`, `EmbeddingResponse`.
3. Unit tests for type validation and protocol conformance (`tests/unit/llm/`).

### Phase 2: Reference implementations — DONE

1. `providers/openai.py` — `OpenAIClient(LLMClient)` + `OpenAIEmbedder(EmbedderClient)`.
2. `providers/anthropic.py` — `AnthropicClient(LLMClient)`.
3. `[llm-openai]` and `[llm-anthropic]` optional extras added to `pyproject.toml`.
4. Unit tests with injected mock SDK clients (`tests/unit/llm/test_openai_provider.py`, `test_anthropic_provider.py`).

### Phase 3: Consumer cut-over — DONE

1. `EnrichmentService` now takes `LLMClient` only; `EnrichmentResult.usage` surfaces `TokenUsage`.
2. `PrecedentMiner` now takes `LLMClient | None`; logs `TokenUsage` via structlog (`precedent_generation_llm_usage`).
3. `LLMCallable` deleted; no adapter shipped.

Deferred — not yet needed:
- `StoreRegistry._build_openai_embedding_fn` still returns a sync `Callable[[str], list[float]]` because `SemanticSearch.search()` is sync. Migrating to `EmbedderClient` means making that call path async; out of scope until a consumer needs batch or usage tracking.
- `WorkflowEngine.TierConfig` has no `LLMClient` plumbed yet. Wire it up when the first tier-routed feature lands.

### Phase 4: Unblocked features (Sprint F)

Features that can now target `LLMClient` directly.

**Shipped (Phase 2 of the Tiered Extraction Pipeline — see [TODO.md §Phase 2 Plan](../../TODO.md#tiered-extraction-pipeline--phase-2-plan)):**

- **Prompt scaffolding** (`PromptTemplate`, `render`, `ENTITY_EXTRACTION_V1`, `MEMORY_EXTRACTION_V1`) — Phase 2 Step 3. Plain `str.format`-based, deliberately minimal; lives in [`src/trellis/extract/prompts/`](../../src/trellis/extract/prompts/).
- **`LLMExtractor`** — Phase 2 Step 4. Tier=`LLM`, consumes `LLMClient.generate()`, tolerant JSON parsing, per-draft validation, never raises. See [`src/trellis/extract/llm.py`](../../src/trellis/extract/llm.py).
- **`HybridJSONExtractor`** — Phase 2 Step 5. Tier=`HYBRID`, composes any deterministic + LLM pair with deterministic-first short-circuit and explicit budget gates. See [`src/trellis/extract/hybrid.py`](../../src/trellis/extract/hybrid.py).
- **`build_save_memory_extractor` factory** — Phase 2 Step 6. Replaced the originally-planned `SaveMemoryExtractor` class; composition (`HybridJSONExtractor(AliasMatchExtractor, LLMExtractor)`) turned out to be sufficient. See [`src/trellis/extract/save_memory.py`](../../src/trellis/extract/save_memory.py).
- **MCP `save_memory` wiring** — Phase 2 Steps 7 + 8B. Feature-flagged via `TRELLIS_ENABLE_MEMORY_EXTRACTION`; `_build_llm_client(registry)` prefers `registry.build_llm_client()` and falls back to `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` env vars for backward compatibility.
- **`StoreRegistry.build_llm_client()` + `build_embedder_client()`** — Phase 2 Step 8A. Read the `llm:` block from `~/.config/trellis/config.yaml`, lazy SDK imports, masked-key logging, never raise.
- **`trellis admin check-extractors` diagnostic** — Phase 2 Step 8C. Reports extractor readiness with CI-friendly exit codes (0 READY / 1 WARN / 2 BLOCKED); catches the "flag set but no `LLMClient` obtainable" case that would otherwise silently skip extraction.

**Still open:**

- LLM-based `CrossEncoderClient` implementations (rerankers).
- LLM-assisted dedup in `save_memory` (needs async embedding path before wiring — see Phase 3 deferred note).
- Full Jinja2-based prompt library with a versioning registry. Three prompts against `str.format` is still below the complexity threshold where Jinja2 pays for itself.
