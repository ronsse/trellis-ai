"""Settings registry — the catalog of tunables an operator may override.

A :class:`SettingSpec` is metadata: name, type, bounds, env var, a
shipped default, an operator-facing description, which surfaces read it,
and a risk tier. :func:`validate_value` is the one piece of behaviour
this module owns — it coerces and bounds-checks a raw value against a
spec's declared ``type``/``minimum``/``maximum``/``enum``, raising
``ValueError`` rather than clamping, for
:class:`~trellis.mutate.handlers.SettingsUpdateHandler` (the governed
write route this registry's fields were always meant to gate) to
translate into a rejection. Beyond that this module still does not read
or write ``settings.json`` itself — that is
:class:`~trellis.stores.settings_store.SettingsStore`'s job. Today the
registry is read by:

* :class:`~trellis.mutate.handlers.SettingsUpdateHandler`, via
  :func:`validate_value` and :func:`get_setting`;
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
(:data:`trellis.retrieve.builder_factory.GRAPH_SEEDING_ENV`) and
``filter_legacy_advisories``
(:data:`trellis.retrieve.pack_builder.FILTER_LEGACY_ADVISORIES_ENV`) as
catalog entries outside ``WriteBehaviourConfig``. Deliberately **not**
registered: ``learning.auto_promote.*`` (the tuner's own config,
`config.yaml`-sourced, not `StoreRegistry`-backed — pending an owner
decision per the plan this PR implements) and anything
`StoreRegistry`-construction-time (backend choice, DSN, blob bucket,
auth mode) — those are process-start parameters, never read live by any
of the stores or readers this registry's siblings touch, so a settings
override could never take effect and would be a lie to list.

``settings_live`` — the one field that is not metadata
--------------------------------------------------------
Whether a *settings* override (as opposed to env) actually changes this
knob's real enforcement today, not merely what ``trellis admin
write-config`` / ``GET /api/version`` report about it. Every real
enforcement call site (``trace_ingest_hook.py``, ``memory_ingest_hook.py``,
``embed_ingest_hook.py``, ``classify/ingest.py``, ``mcp/reconcile.py``,
``mcp/server.py``, ``builder_factory.py``, ``pack_builder.py``) now
resolves env > settings > default via
:func:`trellis.core.write_config.resolve_write_behaviour` or an
equivalent per-knob settings-aware read, so ``settings_live`` is ``True``
for every entry except ``minhash_seed_max_docs`` — read once at process
start (see its own entry) — which instead carries
``restart_required=True`` and states why in its description.
:mod:`tests.unit.core.test_settings_registry` proves every entry is one
or the other, against a deliberately defective mutant entry that is
neither.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Literal

from trellis.core.write_config import ENV_VAR_BY_FIELD, parse_finite_float

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
    #: The environment variable whose presence outranks a settings-store
    #: override for this knob (env > settings > default). For the 12
    #: :class:`~trellis.core.write_config.WriteBehaviourConfig` fields this
    #: is literally ``ENV_VAR_BY_FIELD[name]``; ``graph_seeding`` is the one
    #: entry outside that module (``trellis.retrieve.builder_factory``'s
    #: own ``GRAPH_SEEDING_ENV``, spelled here as a literal rather than
    #: imported — ``trellis.core`` does not depend on ``trellis.retrieve``).
    env_var: str
    minimum: float | None = None
    maximum: float | None = None
    enum: tuple[str, ...] | None = None
    #: ``True`` makes ``minimum`` a strict lower bound (``value > minimum``
    #: required) rather than the default inclusive one. Exists for
    #: ``reconcile_timeout_s``: ``0`` (or ``-0.0``, equal to ``0.0`` in
    #: Python) is a number its own *env* reader (``_reconcile_timeout``)
    #: silently degrades to the shipped default rather than honouring, so
    #: ``validate_value`` must refuse it too rather than store a value the
    #: env path would never actually apply.
    minimum_exclusive: bool = False


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
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["classify_on_ingest"],
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
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["embed_on_ingest"],
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
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["memory_extraction"],
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
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["reconcile_on_write"],
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
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["trace_extraction"],
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
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["trace_extraction_min_confidence"],
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
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["require_pack_attribution"],
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
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["require_bodied_attribution"],
    ),
    SettingSpec(
        name="minhash_seed_max_docs",
        type="int",
        default=0,
        description=(
            "How many stored documents to load into the MCP fuzzy-dedup "
            "index at first use. 0 (default) seeds nothing. Not "
            "settings-live and cannot become so without a process restart: "
            "``_get_minhash_index`` reads this once, lazily, on an MCP "
            "server process's first ``save_memory`` call, then caches the "
            "built index for that process's lifetime — a settings write "
            "after that first call has no effect until the next process "
            "starts. Flagged 'restart' risk on top of that: the cost lands "
            "at that *next* process's first save_memory call — measured at "
            "~32ms per document (128 MinHash permutations), so a corpus of "
            "735 whole documents costs ~24s of blocking CPU on that first "
            "call, scaling linearly with corpus size. Present as 'takes "
            "effect on next session start; will block for ~Ns', never a "
            "bare slider. Capped well above any plausible corpus (not an "
            "operational ceiling, just a sanity bound against a typo or "
            "an overflow-class value such as 10**400)."
        ),
        surfaces=("mcp",),
        restart_required=True,
        risk="restart",
        settings_live=False,
        env_var=ENV_VAR_BY_FIELD["minhash_seed_max_docs"],
        minimum=0,
        maximum=100_000_000,
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
            "runs `trellis analyze replay`. Settings-live: read per pack "
            "build in `build_pack_builder`, env > settings > default, "
            "same as the other two surfaces."
        ),
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="unsafe",
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["pack_holdout_rate"],
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
            "Blank or whitespace-only is refused rather than stored: the "
            "env reader for this knob treats an empty value as absent "
            "and falls back to the shipped default, so storing '' would "
            "claim an override that never actually applies. "
            "A future governed write should validate this against the "
            "deployment's actually-configured routing targets before "
            "accepting it."
        ),
        surfaces=("mcp",),
        restart_required=False,
        risk="safe",
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["reconcile_model"],
    ),
    SettingSpec(
        name="reconcile_timeout_s",
        type="float",
        default=20.0,
        description=(
            "Per-verdict timeout, in seconds, for the reconcile tier. "
            "Non-positive or unparseable *env* values degrade to the "
            "shipped default (20.0) rather than to 0 or an unbounded "
            "wait, so a typo there cannot hang or instantly time out "
            "every verdict. A *settings* write holds itself to the same "
            "standard instead of storing a value its own env reader "
            "would silently discard: 0, a negative number and -0.0 "
            "(equal to 0.0 in Python) are all refused here, not clamped."
        ),
        surfaces=("mcp",),
        restart_required=False,
        risk="safe",
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["reconcile_timeout_s"],
        minimum=0.0,
        minimum_exclusive=True,
    ),
    SettingSpec(
        name="graph_seeding",
        type="bool",
        default=True,
        description=(
            "Seed the graph retrieval axis from the pack's intent "
            "(TRELLIS_GRAPH_SEEDING) instead of falling back to an "
            "intent-blind recency window. Defaults on. Kept out of "
            "`write_config.py`/`WriteBehaviourConfig` on purpose — mixing "
            "a retrieval toggle into the write-behaviour module would "
            "make `trellis admin write-config` report something it does "
            "not govern — but settings-live via its own env > settings > "
            "default read in `builder_factory._graph_seeding_enabled`, "
            "called once per `build_pack_builder`."
        ),
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="safe",
        settings_live=True,
        env_var="TRELLIS_GRAPH_SEEDING",
    ),
    SettingSpec(
        name="filter_legacy_advisories",
        type="bool",
        default=True,
        description=(
            "Drop pre-#848 advisories (ones with no scoped-success "
            "history, so a learning-loop effectiveness check has nothing "
            "to grade them on) from what a pack serves. Defaults on, "
            "matching the shipped behaviour before this setting existed. "
            "Kept out of `write_config.py`/`WriteBehaviourConfig` for the "
            "same reason as `graph_seeding`: it governs what retrieval "
            "serves, not how a write behaves. Settings-live via its own "
            "env > settings > default read in "
            "`pack_builder._filter_legacy_advisories_enabled`, called "
            "once per `build_pack_builder` and threaded through as a "
            "resolved bool — a `PackBuilder` constructed directly (no "
            "factory in front of it, as several tests do) instead falls "
            "back to a live, env-only read with the same default."
        ),
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="safe",
        settings_live=True,
        env_var="TRELLIS_FILTER_LEGACY_ADVISORIES",
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


def resolve_source(spec: SettingSpec, settings_values: dict[str, Any]) -> str:
    """Which layer is in force for *spec* right now.

    ``"env"``, ``"settings"`` or ``"default"``.

    Env beats a stored override beats the shipped default — the same
    precedence :func:`trellis.core.write_config.resolve_overridden_by`
    reports for its own twelve fields, generalised to every registered
    spec (``graph_seeding`` included, which that function's
    ``ENV_VAR_BY_FIELD`` does not cover) by reading ``spec.env_var``
    directly rather than through a second lookup table.

    Shared by two callers that must agree: ``SettingsUpdateHandler``
    stamps this on every ``SETTINGS_UPDATED`` event's
    ``overridden_by_at_write``, and ``GET /api/v1/settings`` reports it
    for every entry, not just the one most recently written.
    """
    if os.environ.get(spec.env_var, "").strip():
        return "env"
    return "settings" if spec.name in settings_values else "default"


#: Case-insensitive spellings ``validate_value`` accepts for a boolean,
#: mirroring the forgiving vocabulary ``WriteBehaviourConfig.from_env``
#: already accepts for the *environment* spelling of the same knobs — a
#: settings-store override and an env override should not disagree about
#: what ``"yes"`` means.
_BOOL_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_BOOL_FALSE_TOKENS = frozenset({"0", "false", "no", "off"})


def _coerce_bool(name: str, raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token in _BOOL_TRUE_TOKENS:
            return True
        if token in _BOOL_FALSE_TOKENS:
            return False
    msg = f"{name} must be a boolean (true/false), got {raw!r}"
    raise ValueError(msg)


def _coerce_int(name: str, raw: Any) -> int:
    if isinstance(raw, bool):
        msg = f"{name} must be an integer, got a boolean ({raw!r})"
        # ValueError (not TypeError): validate_value's contract is "always
        # raises ValueError", so the handler's one `except ValueError` catches
        # every rejection reason uniformly.
        raise ValueError(msg)  # noqa: TRY004
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            pass
    msg = f"{name} must be an integer, got {raw!r}"
    raise ValueError(msg)


def _coerce_float(name: str, raw: Any) -> float:
    if isinstance(raw, bool):
        msg = f"{name} must be a number, got a boolean ({raw!r})"
        # ValueError (not TypeError): same contract as _coerce_int above.
        raise ValueError(msg)  # noqa: TRY004
    if isinstance(raw, int | float | str):
        source = raw.strip() if isinstance(raw, str) else raw
        try:
            # Shared with write_config.py's own float-typed env parsers
            # (see parse_finite_float's docstring) — it is also what
            # turns an int or numeric string outside float range
            # (``10**400``) into a clean ``ValueError`` instead of the
            # ``OverflowError`` plain ``float()`` raises for that case,
            # which this function's one ``except ValueError`` could not
            # otherwise catch.
            value = parse_finite_float(source)
        except ValueError:
            pass
        else:
            if math.isfinite(value):
                return value
    msg = f"{name} must be a finite number, got {raw!r}"
    raise ValueError(msg)


def _coerce_str(name: str, raw: Any) -> str:
    if not isinstance(raw, str):
        msg = f"{name} must be a string, got {raw!r}"
        # ValueError (not TypeError): same contract as _coerce_int above.
        raise ValueError(msg)  # noqa: TRY004
    stripped = raw.strip()
    if not stripped:
        # Blank/whitespace-only is refused, not stored: every str-typed
        # knob's own env reader treats an empty value as absent and
        # falls back to the default (see e.g. reconcile_model's
        # description), so a stored blank would claim an override that
        # never actually takes effect.
        msg = f"{name} must not be blank, got {raw!r}"
        raise ValueError(msg)
    # Stored must equal effective: from_env_and_settings
    # renders this value back into an env-string and re-parses it through
    # the same from_env reader that strips surrounding whitespace off
    # reconcile_model before using it -- stripping here too, rather than
    # storing the raw ' x ', is what keeps those two readings the same
    # value instead of one stripped and one not.
    return stripped


def _check_bounds(spec: SettingSpec, value: bool | int | float | str) -> None:
    """Enum wins outright; otherwise a numeric min/max, bools exempt."""
    if spec.enum is not None:
        if not isinstance(value, str) or value not in spec.enum:
            msg = f"{spec.name} must be one of {list(spec.enum)!r}, got {value!r}"
            raise ValueError(msg)
        return
    if isinstance(value, bool) or not isinstance(value, int | float):
        return
    if spec.minimum is not None:
        if spec.minimum_exclusive:
            if value <= spec.minimum:
                msg = f"{spec.name} must be > {spec.minimum!r}, got {value!r}"
                raise ValueError(msg)
        elif value < spec.minimum:
            msg = f"{spec.name} must be >= {spec.minimum!r}, got {value!r}"
            raise ValueError(msg)
    if spec.maximum is not None and value > spec.maximum:
        msg = f"{spec.name} must be <= {spec.maximum!r}, got {value!r}"
        raise ValueError(msg)


def validate_value(spec: SettingSpec, raw: Any) -> bool | int | float | str:
    """Coerce ``raw`` to ``spec.type`` and bounds-check it, or raise.

    Always raises :class:`ValueError` on any failure — never clamps, never
    silently substitutes the default. That guarantee used to hold only for
    *this* function's own notion of "invalid": a value it accepted could
    still be silently replaced one layer down, at the knob's real
    enforcement site, by ``WriteBehaviourConfig.from_env``'s degrade-to-
    default handling (``reconcile_timeout_s <= 0``, a blank
    ``reconcile_model``). Bounds here are now kept at least as strict as
    that env-side degrade — see ``minimum_exclusive`` and the blank-string
    check in ``_coerce_str`` — specifically so a value stored here is
    never later overridden by a from_env degrade it should have refused
    instead. The caller
    (:class:`~trellis.mutate.handlers.SettingsUpdateHandler`) is the one
    place this is meant to be called from; it translates the
    ``ValueError`` into a governed rejection so the real reason reaches
    both the ``CommandResult`` and the audit event.

    A string is accepted for every type (not just ``"str"``) because every
    wire caller — CLI argv, a JSON body's string field, an MCP tool
    argument — may hand this a string even for a boolean or numeric
    setting; coercion here is what ``WriteBehaviourConfig.from_env``
    already does for the environment spelling of the same values, applied
    to the settings-store spelling instead. ``raw is None`` is never
    passed here — the handler treats it as "remove the override" before
    this function is reached.
    """
    if spec.type == "bool":
        value: bool | int | float | str = _coerce_bool(spec.name, raw)
    elif spec.type == "int":
        value = _coerce_int(spec.name, raw)
    elif spec.type == "float":
        value = _coerce_float(spec.name, raw)
    else:
        value = _coerce_str(spec.name, raw)
    _check_bounds(spec, value)
    return value


__all__ = [
    "SETTINGS_REGISTRY",
    "Risk",
    "SettingSpec",
    "SettingType",
    "get_setting",
    "list_settings",
    "resolve_source",
    "validate_value",
]
