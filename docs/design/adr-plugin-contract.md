# ADR: Plugin Contract (Entry-Point Runtime Extensions)

**Status:** Accepted
**Date:** 2026-04-18
**Context:** Client Boundary & Extension Contracts — Phase 1, Step 5

## Context

Trellis has two kinds of extension need:

1. **Data-only extensions** — client packages adding new entity types,
   new edge kinds, new properties.  Covered by the client-side
   extraction contract (Step 4, Playbook 13).  No server-side code
   change.
2. **Runtime extensions** — code that *must* run inside the
   `trellis-api` process.  Custom store backends, LLM providers,
   classifiers, rerankers, policy gates, search strategies.

This ADR governs (2).  For (1), see Playbook 13 and
`src/trellis_sdk/extract/`.

Before this step, `StoreRegistry._BUILTIN_BACKENDS` hardcoded the
complete set of store backends, and `StoreRegistry.build_llm_client`
hardcoded `if provider == "openai" / elif provider == "anthropic"`.
Adding a new backend or LLM provider required editing core — a
deploy-time coupling we need to break for scale.

## Decision

Runtime extensions are discovered via **Python entry points**.  The
central helpers live in `trellis.plugins`:

* `discover(group)` — returns parsed `PluginSpec` objects.
* `merge_with_builtins(group, builtins)` — applies the shadowing
  policy and returns the merged `name -> (module, attr)` map.
* `collect_plugin_report()` — walks every known group and returns a
  diagnostic report for `trellis admin check-plugins`.

Every registry that wants plugins routes through these helpers so
logging, shadowing policy, and the override env var behave
uniformly.

### Entry-point groups

| Group | Hooked by | Contract |
|---|---|---|
| `trellis.stores.{trace,document,graph,vector,event_log,blob}` | `StoreRegistry` | Subclass of the matching `*Store` ABC |
| `trellis.llm.providers` | `StoreRegistry.build_llm_client` | Callable accepting `api_key`, `base_url`, `default_model` → `LLMClient` |
| `trellis.llm.embedders` | `StoreRegistry.build_embedder_client` | Same shape as providers → `EmbedderClient` |
| `trellis.extractors` | `ExtractorRegistry.load_entry_points` | `Extractor` instance or zero-arg callable returning one |
| `trellis.classifiers` | *Reserved* (not wired yet — `ClassifierPipeline` takes a list today) | `Classifier` Protocol |
| `trellis.rerankers` | *Reserved* | `Reranker` ABC |
| `trellis.policies` | *Reserved* | `PolicyGate` Protocol |
| `trellis.search_strategies` | *Reserved* | `SearchStrategy` Protocol |

Reserved groups are discovered by `check-plugins` (so operators see
them and can experiment) but not yet consumed by any registry.
Wiring the remaining groups is a follow-up when there's a concrete
plugin author asking for it — don't pay the consumer-wiring cost
until the first consumer appears.

### Entry-point value syntax

Both forms are supported:

```toml
[project.entry-points."trellis.stores.graph"]
unity_native = "trellis_unity_catalog.stores:UCGraphStore"  # colon form
legacy       = "my_pkg.legacy.Backend"                       # dotted form
```

Malformed values are logged and dropped; they don't raise.  One
bad plugin wheel doesn't take down the process.

### Shadowing policy

Built-in names (`sqlite`, `postgres`, `openai`, `anthropic`, etc.)
win when a plugin collides.  The collision is logged at `warning`
so operators notice.

To opt into letting a plugin override a built-in, set:

```bash
export TRELLIS_PLUGIN_OVERRIDE=1
```

(accepted values: `1`, `true`, `yes`, `on`; case-insensitive).  The
override is process-wide — no way to override one backend but not
another in the same run.  If that granularity is ever needed, the
solution is to rename the plugin, not to extend the env-var
grammar.

Rationale: a plugin silently taking over the `sqlite` backend would
be very difficult to debug.  Making the override explicit is worth
the small papercut of needing to set an env var.

### Registry preparation hook (store plugins)

*Added 2026-09-24; the hook shipped in #537 (2026-09-04, issue #256).*

A store backend that needs setup the registry would otherwise have to
know about — env-var fallbacks, one client shared by several store
types, a migration run once per server, a closer at shutdown — does it
in an optional classmethod on the store class:

```python
@classmethod
def prepare_registry_params(
    cls, ctx: RegistryContext, store_type: str, params: dict[str, Any]
) -> dict[str, Any]: ...
```

The Protocol is `trellis.stores.base.registry.RegistryPreparable`, but
nothing needs to subclass or register it: `StoreRegistry._instantiate`
looks the method up with `getattr` and calls it when it is callable.
It runs when that store type is first built (first access, or
`validate()`), after the registry has applied its own defaults
(`db_path` for `sqlite`, `root_dir` for `local`, `dsn` for
`postgres`/`pgvector`), and the registry then constructs the store as
`cls(**returned)`. `params` is the store type's config block without
its `backend` key. **The return value is the complete constructor
kwargs**: a hook that builds a fresh dict and forgets to carry `params`
through silently disconnects the store from its config. An exception
raised here propagates like any construction failure; under
`validate()` it lands in the aggregated `RegistryValidationError`.

`RegistryContext` (frozen dataclass, same module) carries four
things, and a hook should need nothing else from the registry:

| Field | What it is | Use it for |
|---|---|---|
| `env` | Read-only snapshot of `os.environ`, taken when the context is built (once per store instantiation) | Env-var fallbacks — read these instead of `os.environ` |
| `shared` | One plain `dict` per registry instance, the same object for every store that registry builds | Resources several store types share (the built-in Bolt backends keep one driver per `(uri, user)` here). **Namespace keys by module path**, e.g. `f"{__name__}:drivers"` — independently installed plugins share this dict |
| `register_closer` | Appends a zero-argument callable | Releasing what you put in `shared`. Register once, when you create the resource — not on a cache hit |
| `emit_warning` | Logs `store_registry_backend_warning` at `warning`, tagged with `store_type` and `backend` | Degraded-but-working setup an operator should see |

`StoreRegistry.close()` closes every cached store first, then runs the
closers in registration order (a failing closer is logged and skipped),
then clears both the closer list and `shared` — so a closer runs once
even when `close()` is called twice. A store holding a resource the
registry injected should therefore make its own `close()` a no-op for
it, as the built-in Bolt stores do for an injected driver.

**The pre-flight connectivity convention is duck-typed, and opt-in by
shape.** `validate(check_connectivity=True)` (or
`TRELLIS_VALIDATE_CONNECTIVITY`) walks every `dict` value in `shared`
and calls `verify_connectivity()` on each entry whose key is a
2-tuple of strings `(uri, user)` and whose value has that method,
reporting a failure as `bolt-driver:<uri>`. Nothing in that walk names
a backend. A plugin that caches its client under that shape gets
pre-flight connectivity checking for free; one that does not gets
none, and nothing warns that it was skipped. The built-in Bolt
backends are the worked example:
`trellis.stores.bolt_opencypher.base.registry_driver_cache` returns
such a map, and `trellis.stores.neo4j.base.prepare_neo4j_registry_params`
is a complete hook in about forty lines.

The reference *plugin* is synthetic:
`tests/unit/plugins/test_registry_prepare_hook.py::test_plugin_hook_shares_context_and_registers_closer`
loads a store class through a faked `trellis.stores.graph` /
`trellis.stores.vector` entry point and asserts the shared state, the
env snapshot and the run-once closer.

What this does and does not make true today. Neo4j and ArcadeDB use
the hook, but they are still `_BUILTIN_BACKENDS` rows, not entry
points — `pyproject.toml` declares no `trellis.stores.*` entry points.
What the hook buys is that `src/trellis/stores/registry.py` imports
none of `trellis.stores.{neo4j,arcadedb,bolt_opencypher}` and compares
no value against either backend name, which
`tests/unit/test_registry_plugin_boundary_rule.py` derives by AST and
fails on a reintroduction. That rule reads imports and comparisons,
not dict keys, and besides `_BUILTIN_BACKENDS` two name-keyed tables
remain: `_EXTRA_FOR_BACKEND` (the extra named in
`BackendNotInstalledError` — a plugin backend absent from it just gets
the error without that hint) and the pre-flight URI-scheme check
`_BACKEND_URI_RULES` (covering `postgres`, `pgvector` and `neo4j`).
The second is a real limit: a plugin cannot contribute a URI rule
without editing `registry.py`.

### ABI stability

Plugins conform to the Protocol / ABC contracts in `trellis.*`.  We
ship `py.typed` and run `mypy --strict`-ish on core, so contract
changes surface as typecheck failures in plugin CI.

**No declared `trellis-abi` version** for now.  The Protocol
contracts are the ABI.  Revisit only if:

* We need to break a Protocol in a minor release (not planned), or
* Plugin CI fails to catch contract drift in practice (no evidence
  of this yet).

See [TODO.md — Phase 1, Step 5](../../TODO.md) for the deferred-ABI
decision.

### Contract test suites — the runtime spec

For `GraphStore` and `VectorStore` plugin backends, the parameterized
contract test suites in
[`tests/unit/stores/contracts/`](../../tests/unit/stores/contracts/)
are the **authoritative behavioural specification**. The Protocol /
ABC method signatures pin types; the contract suites pin semantics
(operator support in filters, `as_of` time-travel, role validation,
SCD-2 close-then-insert, etc.).

Plugin authors are expected to:

1. Subclass `GraphStoreContractTests` / `VectorStoreContractTests` in
   their own test suite, providing a `store` fixture that yields a
   fresh instance.
2. Run the suite in their plugin's CI. Failures indicate contract
   drift; fix the implementation rather than skipping the test.
3. For backends that legitimately deviate from the contract (e.g.,
   the in-tree `Neo4jVectorStore` shape #2 where embeddings live as
   properties on graph nodes), document the deviation in the plugin
   README and ship per-backend tests instead. The exclusion is
   structural, not aspirational — these backends are not in the
   "drop-in vector store" category.

### Required: implement the canonical DSL

`GraphStore` plugin backends **must** implement the canonical query
DSL — not just the legacy `query()` / `get_subgraph()` methods.
Specifically:

* **Override** `execute_node_query(NodeQuery)` and
  `execute_subgraph_query(SubgraphQuery)` on the backend class.
* **Support** the full Phase 1 operator surface (`eq` / `in` /
  `exists`) on top-level columns (`node_type`, `node_role`,
  `node_id`) and on `properties.<key>` paths.
* **Pass** the `test_execute_*` cases in `GraphStoreContractTests`.

The `GraphStore` ABC ships a default `execute_node_query`
implementation that routes through `query()`, but this default only
handles `eq` on a narrow field set and raises `NotImplementedError`
on `in` / `exists`. That default exists so the in-tree backends had
a migration path during Phase 1 — it is **not** a sanctioned plugin
implementation. New plugins that rely on the default fail the
contract suite immediately.

The contract is enforced mechanically: every contract test for the
DSL is parameterized over the plugin's `store` fixture, so a missing
or incomplete compiler shows up as a red CI run, not a deferred
review comment.

See [`adr-canonical-graph-layer.md`](./adr-canonical-graph-layer.md)
for the DSL design (`FilterClause` / `NodeQuery` / `SubgraphQuery`),
the per-backend compilers in core (SQLite / Postgres / Neo4j) for
reference implementations, and the deviations recorded so far.

### Diagnostic

`trellis admin check-plugins [--format json]` walks every known
group and reports per-plugin status:

* `LOADED` — module imports, class resolves.  Will be available at
  runtime.
* `SHADOWED` — collides with a built-in; built-in wins (or plugin
  wins if `TRELLIS_PLUGIN_OVERRIDE=1`).
* `BLOCKED` — declared but module import or attribute resolution
  failed.  This is the case the probe most wants to catch: silent
  in production if not checked.

CI exit codes: `0` clean, `1` shadowing, `2` blocked.  Matches the
convention in `check-extractors`.

## Consequences

### Positive

* **No core changes to add a provider.** Bedrock, Vertex, internal
  inference, private vector stores — all ship as wheels.
* **Uniform loader semantics.** One implementation of discovery,
  shadowing, override, import-safety.  Registries that want plugins
  route through it; the behavior is identical across all of them.
* **Observability.** `check-plugins` makes the plugin ecosystem
  visible.  Blocked plugins are caught before they silently skip.
* **Opt-in override.** Operators who do want to replace a built-in
  can, but they have to say so out loud.

### Negative

* **Another discovery surface to document.** Playbook 13 covers
  client-side extraction; this ADR covers runtime extensions.  Two
  paths, two sets of fork instructions.
* **Entry-point groups are committed public names.** Renaming
  them is a plugin-ecosystem break.  Reserving group names now
  (before anyone uses them) is cheap insurance.
* **Plugin authors must handle import-time side effects carefully.**
  `discover()` imports the module to resolve the class; heavy
  import-time work blocks the diagnostic.

### Neutral

* Entry points are the standard Python plugin mechanism.  Operators
  already know how to `pip install` a package.
* Plugins run in the same process as Trellis core — the usual
  caveat about untrusted plugins applies.  No sandbox.

## Alternatives considered

### A. Config-driven dotted-path loading

Example: `stores.graph.class: trellis_unity_catalog.stores.UCGraphStore`
in `config.yaml`.

* Pro: zero Python entry-point plumbing; just config.
* Con: no discovery, no diagnostic, no shadowing rule; every
  plugin has to be listed explicitly in config.

Rejected: the central point of plugins is zero-config discovery.
Installing the wheel should be enough; typing the class path is a
worse DX.

### B. Protocol-only, no registry

Let callers pass plugin instances directly: `StoreRegistry(graph_store=UCGraphStore(...))`.

* Pro: simplest possible surface.
* Con: breaks the config-driven deployment story.  Every deployment
  has to have Python wiring code.

Rejected: we want config-driven deployments (`config.yaml`
decides which backends to use) and plugins that tie into that.

### C. Hybrid: entry points for discovery, config for selection

Adopted.  Entry points advertise what's available; `config.yaml`
selects by name.  The plugin wheel + config line is the complete
surface for adding a backend.

## References

- [src/trellis/plugins/](../../src/trellis/plugins/)
- [src/trellis/stores/registry.py](../../src/trellis/stores/registry.py)
- [src/trellis/extract/registry.py](../../src/trellis/extract/registry.py)
- Playbook 13 in [docs/agent-guide/playbooks.md](../agent-guide/playbooks.md)
- Python packaging entry-points spec: https://packaging.python.org/en/latest/specifications/entry-points/
