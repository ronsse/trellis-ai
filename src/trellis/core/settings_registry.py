"""Settings registry — the catalog of tunables an operator may override.

A :class:`SettingSpec` is metadata only: name, type, bounds, a shipped
default, an operator-facing description, which surfaces read it, and a
risk tier. **It does not validate anything in this PR** — no write route,
no CLI write command and no :class:`~trellis.mutate.executor.MutationExecutor`
operation exist yet for settings (tracked for a follow-up PR, which is
exactly where this registry's ``type``/``minimum``/``maximum``/``enum``
fields are meant to gate a write). Today the registry is read by:

* an operator or test skimming :data:`SETTINGS_REGISTRY` directly;
* :func:`trellis.core.write_config.WriteBehaviourConfig.from_env_and_settings`
  and :func:`~trellis.core.write_config.resolve_overridden_by` **not at
  all** — those two functions take a plain ``field -> value`` mapping and
  know nothing about this registry, by design (see their docstrings): the
  registry is the catalog, not the resolver.

Scope, stated once rather than per entry
-----------------------------------------
Registered: the 12 :class:`~trellis.core.write_config.WriteBehaviourConfig`
fields, plus ``graph_seeding``
(:data:`trellis.retrieve.builder_factory.GRAPH_SEEDING_ENV`) as a catalog
entry. Deliberately **not** registered: ``learning.auto_promote.*``
(the tuner's own config, `config.yaml`-sourced, not `StoreRegistry`-backed —
pending an owner decision per the plan this PR implements) and anything
`StoreRegistry`-construction-time (backend choice, DSN, blob bucket,
auth mode) — those are process-start parameters, never read live by any
of the stores or readers this registry's siblings touch, so a settings
override could never take effect and would be a lie to list.

``settings_live`` — the one field that is not metadata
--------------------------------------------------------
Whether a *settings* override (as opposed to env) actually changes this
knob's real enforcement today, not merely what ``trellis admin
write-config`` / ``GET /api/version`` report about it. As of this PR that
is true for exactly **one** entry, ``pack_holdout_rate`` — wired into
:func:`trellis.retrieve.builder_factory.build_pack_builder`. The other
twelve are resolved through the settings-aware path only at the two
reporting surfaces; their real enforcement call sites
(``trace_ingest_hook.py``, ``memory_ingest_hook.py``,
``embed_ingest_hook.py``, ``classify/ingest.py``, ``mcp/reconcile.py``,
``mcp/server.py``) still call ``WriteBehaviourConfig.from_env()`` directly
and so only ever see an *environment* override — a settings-only row for
one of those twelve is stored, reported as in force, and has **no runtime
effect** until a follow-up wires its call site. That gap is deliberate
(the brief's own escape valve: wiring every hot ingest call site to read
an extra file risked exactly the per-document cost this module exists to
avoid) and ``settings_live`` is what makes it machine-checkable rather
than a claim nothing can catch drifting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: Mirrors the plan's own vocabulary (`docs/design` plan p1, §(a)) rather
#: than a tier invented for this module: ``"safe"`` (no meaningful
#: downside beyond its stated function), ``"restart"`` (its cost or effect
#: lands at the next process's first use, not instantaneously — a naive
#: control inviting a stall mid-request), ``"unsafe"`` (silently degrades
#: correctness, cost or the learning loop's own input if exposed as a bare
#: toggle; needs a caption, not just a control).
Risk = Literal["safe", "restart", "unsafe"]
SettingType = Literal["bool", "int", "float", "str"]


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """One declared tunable. See the module docstring for what this is not."""

    name: str
    type: SettingType
    default: bool | int | float | str
    description: str
    surfaces: tuple[str, ...]
    restart_required: bool
    risk: Risk
    #: Whether a *settings* override reaches this knob's real enforcement
    #: today (see the module docstring's "settings_live" section) — not
    #: merely whether it is reported correctly by the two surfaces that
    #: always see settings (``trellis admin write-config``, ``GET
    #: /api/version``).
    settings_live: bool
    minimum: float | None = None
    maximum: float | None = None
    enum: tuple[str, ...] | None = None


_SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(
        name="classify_on_ingest",
        type="bool",
        default=False,
        description=(
            "Run the deterministic tagging pipeline inline at write time "
            "(mcp.save_memory, cli ingest, api documents). Ingestion mode "
            "classifiers are deterministic-only — microseconds per item, "
            "no LLM call — so there is no meaningful cost to turning this "
            "on; it is off by default because an existing deployment's "
            "items simply have no ContentTags until classified, and "
            "enabling this does not retroactively tag them."
        ),
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="safe",
        settings_live=False,
    ),
    SettingSpec(
        name="embed_on_ingest",
        type="bool",
        default=False,
        description=(
            "Generate an embedding inline at write time (mcp.save_memory, "
            "cli ingest, api documents), so semantic search sees an item "
            "immediately rather than after a separate embed pass. Costs "
            "one embedder call per write — cheap for a local embedder, a "
            "real per-call bill for a hosted one — so check which embedder "
            "is configured before enabling on a write-heavy surface."
        ),
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="safe",
        settings_live=False,
    ),
    SettingSpec(
        name="memory_extraction",
        type="bool",
        default=False,
        description=(
            "Mine entities out of saved memories at write time. Flagged "
            "unsafe to expose as a bare toggle: on the reference "
            "deployment this has been on since 2026-10-01 and `mentions` "
            "edges are still zero for an undiagnosed reason, so turning "
            "this on elsewhere inherits a known, open correctness gap "
            "rather than a working feature — present it with that caveat, "
            "not as an ordinary switch."
        ),
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="unsafe",
        settings_live=False,
    ),
    SettingSpec(
        name="reconcile_on_write",
        type="bool",
        default=False,
        description=(
            "Ask a model for an ADD/UPDATE/SUPERSEDE/NOOP verdict at "
            "capture time (mcp save_memory path) instead of treating "
            "every write as an unconditional add. Flagged unsafe to "
            "expose bare: it is an LLM-judged call on the write path, so "
            "every write pays that call's latency and cost, and it is "
            "also the gate for enabling the pairwise supersession "
            "rollout (`TRELLIS_ENABLE_RECONCILE_ON_WRITE`) — show a "
            "cost-per-call estimate alongside the control, never a bare "
            "switch."
        ),
        surfaces=("mcp",),
        restart_required=False,
        risk="unsafe",
        settings_live=False,
    ),
    SettingSpec(
        name="trace_extraction",
        type="bool",
        default=False,
        description=(
            "Extract entities from ingested traces at write time. "
            "Deterministic-tier extraction is the default dispatch "
            "priority (`DETERMINISTIC > HYBRID > LLM`, LLM fallback off "
            "by default), so turning this on does not by itself add an "
            "LLM call — the cost is bounded parsing work per trace."
        ),
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="safe",
        settings_live=False,
    ),
    SettingSpec(
        name="trace_extraction_min_confidence",
        type="float",
        default=None,  # type: ignore[arg-type]  # unset means "no gate", the shipped state
        description=(
            "Confidence floor applied to trace-extraction drafts, in "
            "[0, 1]. Unset (the default) gates nothing — every draft is "
            "submitted. An unparseable or out-of-range value degrades to "
            "unset with a warning rather than to 0.0, so a typo can only "
            "under-gate, never silently drop every draft."
        ),
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="safe",
        settings_live=False,
        minimum=0.0,
        maximum=1.0,
    ),
    SettingSpec(
        name="require_pack_attribution",
        type="bool",
        default=False,
        description=(
            "Require at least one cited item on a pack-targeted feedback "
            "call (mcp record_feedback, REST POST /packs/{pack_id}/feedback). "
            "Off is today's shipped behaviour: a caller naming a pack_id "
            "with no citations still records a rating. Turning this on "
            "changes the contract — such a call is rejected, with the "
            "pack's actual item ids handed back — so expect previously-"
            "accepted calls to start failing until callers adapt."
        ),
        surfaces=("mcp", "api"),
        restart_required=False,
        risk="safe",
        settings_live=False,
    ),
    SettingSpec(
        name="require_bodied_attribution",
        type="bool",
        default=False,
        description=(
            "Require a verdict on every graduated-disclosure 'bodied' "
            "item of a pack-targeted feedback call, not just one "
            "citation (stricter than require_pack_attribution). "
            "write_config.py's own measurement: this asks for roughly six "
            "more verdicts per call than callers demonstrably volunteer "
            "today (~46% more). Its documented failure mode is the "
            "grading surface going quiet rather than answering more "
            "fully, which is worse for the learning loop than an "
            "incomplete rating — treat as unsafe to flip without "
            "watching the feedback rate afterward."
        ),
        surfaces=("mcp", "api"),
        restart_required=False,
        risk="unsafe",
        settings_live=False,
    ),
    SettingSpec(
        name="minhash_seed_max_docs",
        type="int",
        default=0,
        description=(
            "How many stored documents to load into the MCP fuzzy-dedup "
            "index at first use. 0 (default) seeds nothing. Flagged "
            "'restart' risk: the cost lands at the *next* process's first "
            "save_memory call, not when this is changed — measured at "
            "~32ms per document (128 MinHash permutations), so a corpus "
            "of 735 whole documents costs ~24s of blocking CPU on that "
            "first call, scaling linearly with corpus size. Present as "
            "'takes effect on next session start; will block for ~Ns', "
            "never a bare slider."
        ),
        surfaces=("mcp",),
        restart_required=False,
        risk="restart",
        settings_live=False,
        minimum=0,
    ),
    SettingSpec(
        name="pack_holdout_rate",
        type="float",
        default=0.0,
        description=(
            "Share of assembled packs withheld from the caller, in "
            "[0, 1], for the pack-effect A/B measurement. 0.0 (default) "
            "withholds nothing. Flagged unsafe to expose bare: it "
            "directly degrades what live agents receive — a withheld "
            "pack reaches the caller as an ordinary empty one with no "
            "visible symptom beyond measurement, so a fat-fingered value "
            "silently degrades retrieval quality fleet-wide until someone "
            "runs `trellis analyze replay`. This is the one setting a "
            "settings-store override actually changes today (see "
            "`settings_live`) — it is read per pack build in "
            "`build_pack_builder`, env > settings > default, same as the "
            "other two surfaces."
        ),
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="unsafe",
        settings_live=True,
        minimum=0.0,
        maximum=1.0,
    ),
    SettingSpec(
        name="reconcile_model",
        type="str",
        default="hermes3:8b",
        description=(
            "Model identifier labelled on reconcile verdict events "
            "(mcp capture-time ADD/UPDATE/SUPERSEDE/NOOP judging). "
            "Free text — no enum is enforced today, by this store or by "
            "anything that reads it — so a typo is not rejected, it "
            "silently changes verdict quality with no error anywhere. "
            "A future governed write should validate this against the "
            "deployment's actually-configured routing targets before "
            "accepting it."
        ),
        surfaces=("mcp",),
        restart_required=False,
        risk="safe",
        settings_live=False,
    ),
    SettingSpec(
        name="reconcile_timeout_s",
        type="float",
        default=20.0,
        description=(
            "Per-verdict timeout, in seconds, for the reconcile tier. "
            "Non-positive or unparseable values degrade to the shipped "
            "default (20.0) rather than to 0 or an unbounded wait, so a "
            "typo cannot hang or instantly time out every verdict."
        ),
        surfaces=("mcp",),
        restart_required=False,
        risk="safe",
        settings_live=False,
        minimum=0.0,
    ),
    SettingSpec(
        name="graph_seeding",
        type="bool",
        default=True,
        description=(
            "Seed the graph retrieval axis from the pack's intent "
            "(TRELLIS_GRAPH_SEEDING) instead of falling back to an "
            "intent-blind recency window. Defaults on. Catalog entry "
            "only in this PR: builder_factory.py deliberately keeps this "
            "flag's own env read out of write_config.py (mixing a "
            "retrieval toggle into the write-behaviour module would make "
            "`trellis admin write-config` report something it does not "
            "govern), and this PR does not add a second, settings-aware "
            "read next to it — a settings override stored under this "
            "name has no runtime effect until a follow-up wires "
            "builder_factory.py's own read of it."
        ),
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="safe",
        settings_live=False,
    ),
)

#: Name -> spec. Declared after ``_SPECS`` so a duplicate name fails loudly
#: (a dict comprehension over a tuple with a repeated name silently keeps
#: the last one) rather than shadowing silently.
SETTINGS_REGISTRY: dict[str, SettingSpec] = {spec.name: spec for spec in _SPECS}

if len(SETTINGS_REGISTRY) != len(_SPECS):
    _seen: set[str] = set()
    _dupes = sorted(
        {s.name for s in _SPECS if s.name in _seen or _seen.add(s.name)}  # type: ignore[func-returns-value]
    )
    _message = f"duplicate SettingSpec name(s) in _SPECS: {_dupes}"
    raise AssertionError(_message)


def get_setting(name: str) -> SettingSpec | None:
    """The declared spec for ``name``, or ``None`` if it is not registered."""
    return SETTINGS_REGISTRY.get(name)


def list_settings() -> list[SettingSpec]:
    """Every declared spec, in registration order."""
    return list(SETTINGS_REGISTRY.values())


__all__ = [
    "SETTINGS_REGISTRY",
    "Risk",
    "SettingSpec",
    "SettingType",
    "get_setting",
    "list_settings",
]
