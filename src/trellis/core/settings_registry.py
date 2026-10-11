"""Settings registry — the catalog of tunables an operator may override.

A :class:`SettingSpec` is metadata: name, a human label, type, bounds, env
var, a shipped default, operator-facing text (``description`` plus an
optional ``risk_note``), which surfaces read it, a risk tier, and which UI
group it belongs to. :func:`validate_value` is the one piece of behaviour
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

Operator text vs. maintainer detail
------------------------------------
``description`` and ``risk_note`` are rendered verbatim by the admin UI's
Settings tab, so they are written for an operator deciding whether to
flip a switch: what it does, what changes on vs. off (or low vs. high),
the default, and when you'd reach for it. They carry no issue/PR numbers,
no deployment history, and no internal process trivia (exact benchmark
numbers, module/function names, env-parsing edge cases) — that detail
lives in a code comment next to the relevant :class:`SettingSpec`
instead, where a maintainer reading this file will still find it.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Literal

from trellis.core.write_config import ENV_VAR_BY_FIELD, parse_finite_float

#: Three tiers, in increasing order of "read the room before you flip
#: this": ``"low"`` (no meaningful downside beyond its stated function —
#: no ``risk_note``), ``"caution"`` (a real cost or behavioural shift an
#: operator should know about before changing it — carries a
#: ``risk_note``), ``"high"`` (silently degrades correctness, cost or the
#: learning loop's own input if exposed as a bare toggle — also carries a
#: ``risk_note``, and the UI badges it distinctly from ``"caution"``).
#: Independent of ``restart_required``, which is its own flag: a knob can
#: need a restart at any risk tier, or none at all.
Risk = Literal["low", "caution", "high"]
SettingType = Literal["bool", "int", "float", "str"]

#: The UI's filter-box categories (brief e179, step 8): which area of the
#: system a knob belongs to, independent of ``risk`` or ``settings_live``.
#: ``"learning_loops"`` is currently unoccupied by this module's 14
#: entries — it is reserved for the concurrent ``learning.auto_promote.*``
#: PR, so the UI's filter box already has a place for those once they
#: land, with no UI change.
SettingGroup = Literal[
    "retrieval", "ingest_tagging", "learning_loops", "feedback_attribution", "ops"
]

#: The one place a human-facing group name lives, so a UI reading this
#: registry never hardcodes a display label (brief e179, step 8's "not
#: UI strings"). Keys are exactly :data:`SettingGroup`'s members —
#: ``tests.unit.core.test_settings_registry`` proves the sets match.
GROUP_LABELS: dict[SettingGroup, str] = {
    "retrieval": "Retrieval",
    "ingest_tagging": "Ingest & tagging",
    "learning_loops": "Learning loops",
    "feedback_attribution": "Feedback & attribution",
    "ops": "Ops",
}


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """One declared tunable. See the module docstring for what this is not."""

    name: str
    #: Short, human-readable name the UI shows in place of ``name`` —
    #: e.g. "Embed on ingest" for ``embed_on_ingest``. Plain words, no
    #: code handles.
    label: str
    type: SettingType
    default: bool | int | float | str
    #: Operator-facing prose — see the module docstring's "Operator text
    #: vs. maintainer detail" section. What it does; what an agent or the
    #: system does differently on vs. off (or low vs. high); the default;
    #: when an operator would change it.
    description: str
    surfaces: tuple[str, ...]
    restart_required: bool
    risk: Risk
    #: Which of the UI's five filter-box areas this knob belongs to (see
    #: :data:`SettingGroup`). Independent of ``risk``: a knob can be
    #: low-risk and still belong to a sensitive area, or vice versa.
    group: SettingGroup
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
    #: One operator-facing sentence — cost, blast radius, a known issue,
    #: or what a restart means for this knob. Required (non-empty) for
    #: every entry whose ``risk`` is not ``"low"``; ``None`` for every
    #: ``"low"`` entry, since the UI only shows this note beside a
    #: non-low risk badge. Must be checked against the knob's actual
    #: reading site, not carried over from an old description.
    risk_note: str | None = None


_SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(
        name="classify_on_ingest",
        label="Classify on ingest",
        type="bool",
        default=False,
        description=(
            "Runs the deterministic tagging pipeline as part of saving an "
            "item, instead of as a separate pass afterward. On, every "
            "newly saved item gets its content tags immediately, so "
            "tag-based filtering works on it right away. Off (the "
            "default), new items carry no tags until a separate "
            "classification pass runs over them. Turning this on does "
            "not retroactively tag items that were saved before the "
            "change."
        ),
        # Ingestion-mode classifiers are deterministic-only (no LLM
        # call) — microseconds per item — so there is no meaningful cost
        # to this one beyond what's in the description above.
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="low",
        group="ingest_tagging",
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["classify_on_ingest"],
    ),
    SettingSpec(
        name="embed_on_ingest",
        label="Embed on ingest",
        type="bool",
        default=False,
        description=(
            "Generates an embedding as part of saving an item, instead "
            "of waiting for a separate embedding pass. On, a newly saved "
            "item becomes visible to semantic search immediately. Off "
            "(the default), new items need that separate pass to run "
            "before semantic search can find them. This costs one "
            "embedder call per write, which is effectively free for a "
            "local embedder but a real per-call expense for a hosted "
            "one — check which embedder is configured before enabling "
            "this on a write-heavy workload."
        ),
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="low",
        group="ingest_tagging",
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["embed_on_ingest"],
    ),
    SettingSpec(
        name="memory_extraction",
        label="Extract entities from memories",
        type="bool",
        default=False,
        description=(
            "Mines entities out of a memory's content at the moment it "
            "is saved, adding what it finds to the knowledge graph. On, "
            "the graph grows from memory content automatically as "
            "memories arrive. Off (the default), memories are saved "
            "as-is and nothing new is added to the graph from them. "
            "Turn this on if you want memories, not just traces, to "
            "feed the graph."
        ),
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="high",
        risk_note=(
            "Entity mentions are currently not being produced; under investigation."
        ),
        group="ingest_tagging",
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["memory_extraction"],
    ),
    SettingSpec(
        name="reconcile_on_write",
        label="Reconcile on write",
        type="bool",
        default=False,
        description=(
            "Asks a model to judge whether a newly saved memory should "
            "be added, merged into an existing one, marked as "
            "superseding it, or treated as a no-op — instead of always "
            "recording it as a new add. On, duplicate and superseding "
            "writes get folded into the right outcome automatically. "
            "Off (the default), every write is recorded as a new add "
            "and no such judgment runs. This setting also gates the "
            "broader pairwise-supersession rollout, so turning it on "
            "changes more than just this one call's behavior."
        ),
        surfaces=("mcp",),
        restart_required=False,
        risk="high",
        risk_note=(
            "Every write pays the latency and cost of an extra model "
            "call, since the verdict is judged live on the write path."
        ),
        group="ingest_tagging",
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["reconcile_on_write"],
    ),
    SettingSpec(
        name="trace_extraction",
        label="Extract entities from traces",
        type="bool",
        default=False,
        description=(
            "Extracts entities from a trace as it is ingested. On, "
            "every ingested trace is scanned for entities right away. "
            "Off (the default), traces are stored without extraction. "
            "The deterministic extractor runs first and an LLM fallback "
            "stays off by default, so turning this on adds bounded "
            "parsing work per trace rather than a new model call."
        ),
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="low",
        group="ingest_tagging",
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["trace_extraction"],
    ),
    SettingSpec(
        name="trace_extraction_min_confidence",
        label="Trace extraction confidence floor",
        type="float",
        default=None,  # type: ignore[arg-type]  # unset means "no gate", the shipped state
        description=(
            "Sets a confidence floor, between 0 and 1, that an "
            "extracted entity draft must clear before it is submitted "
            "to the graph. Unset (the default) applies no floor, so "
            "every draft is submitted regardless of confidence. Raise "
            "this if low-confidence drafts are cluttering the graph; an "
            "unparseable or out-of-range value is treated as unset "
            "rather than as zero, so a mistyped value can only let more "
            "through, never silently block everything."
        ),
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="low",
        group="ingest_tagging",
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["trace_extraction_min_confidence"],
        minimum=0.0,
        maximum=1.0,
    ),
    SettingSpec(
        name="require_pack_attribution",
        label="Require pack attribution",
        type="bool",
        default=False,
        description=(
            "Requires at least one cited item on any feedback call "
            "that names a pack. Off (the default), a caller can rate a "
            "pack with no citations at all. On, such a call is "
            "rejected and handed back the pack's own item ids so the "
            "caller can retry with a citation attached. Enable this "
            "once your feedback callers are consistently able to cite "
            "something — previously-accepted uncited calls will start "
            "failing the moment you turn it on."
        ),
        surfaces=("mcp", "api"),
        restart_required=False,
        risk="low",
        group="feedback_attribution",
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["require_pack_attribution"],
    ),
    SettingSpec(
        name="require_bodied_attribution",
        label="Require bodied-item attribution",
        type="bool",
        default=False,
        description=(
            "Requires a verdict — helpful, unhelpful, or ignored — on "
            "every item a pack showed in full, not just one citation "
            "for the pack as a whole. Off (the default), a feedback "
            "call can satisfy pack-level attribution without "
            "addressing every such item. On, callers must account for "
            "each one, giving the learning loop much richer per-item "
            "signal. This setting and require_pack_attribution compose "
            "independently — turning this on does not change what the "
            "other one asks for."
        ),
        # Measured on the reference deployment's own feedback history:
        # this asks for roughly six more verdicts per call than callers
        # demonstrably volunteer today (~46% more than the ~8.9 they
        # already supply per attributed call) — see write_config.py's
        # REQUIRE_BODIED_ATTRIBUTION_FLAG docstring for the full
        # arithmetic. That ceiling, not a correctness concern, is why
        # this ships off.
        surfaces=("mcp", "api"),
        restart_required=False,
        risk="caution",
        risk_note=(
            "Raises the number of verdicts a caller must supply per "
            "call well above what callers demonstrably volunteer "
            "today, so a grading surface that can't keep up may "
            "respond less often rather than more fully — watch the "
            "feedback rate after enabling this."
        ),
        group="feedback_attribution",
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["require_bodied_attribution"],
    ),
    SettingSpec(
        name="minhash_seed_max_docs",
        label="MinHash dedup seed size",
        type="int",
        default=0,
        description=(
            "How many stored documents to load into the "
            "fuzzy-duplicate-detection index the first time an agent "
            "process uses it. 0 (the default) seeds nothing. This is "
            "read once, the first time it's needed in a process, and "
            "then stays fixed for that process's whole lifetime — "
            "changing it only takes effect the next time the process "
            "restarts, never while it's running. That first use blocks "
            "while the index builds, and the delay grows with how many "
            "documents you seed, so set this to the smallest number "
            "that covers what you need."
        ),
        # Measured at ~32ms per document (128 MinHash permutations), so
        # a corpus of 735 whole documents costs ~24s of blocking CPU on
        # that first save_memory call, scaling linearly with corpus
        # size. The upper bound below is a sanity cap against a typo or
        # an overflow-class value (e.g. 10**400), not an operational
        # ceiling.
        surfaces=("mcp",),
        restart_required=True,
        risk="low",
        group="ops",
        settings_live=False,
        env_var=ENV_VAR_BY_FIELD["minhash_seed_max_docs"],
        minimum=0,
        maximum=100_000_000,
    ),
    SettingSpec(
        name="pack_holdout_rate",
        label="Pack holdout rate",
        type="float",
        default=0.0,
        description=(
            "Share of assembled packs, between 0 and 1, that are "
            "withheld entirely from the requesting agent rather than "
            "served normally. 0.0 (the default) withholds nothing. A "
            "withheld pack reaches the caller as an ordinary-looking "
            "empty pack, with no distinguishing signal beyond the "
            "measurement it feeds. Use this only when you deliberately "
            "want to run a pack-effect measurement."
        ),
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="high",
        risk_note=(
            "This fraction of agents' context requests gets no pack at "
            "all, in exchange for a controlled before/after measurement "
            "of pack effect — treat any nonzero value as active "
            "instrumentation, not a default posture."
        ),
        group="retrieval",
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["pack_holdout_rate"],
        minimum=0.0,
        maximum=1.0,
    ),
    SettingSpec(
        name="reconcile_model",
        label="Reconcile model",
        type="str",
        default="hermes3:8b",
        description=(
            "The model identifier labelled on reconcile verdict events "
            "— the add/update/supersede/no-op judgment made when "
            "reconcile-on-write is enabled. This is free text: nothing "
            "validates it against the models actually available in "
            "your deployment, so a typo silently changes verdict "
            "quality with no error anywhere. A blank or "
            "whitespace-only value is refused rather than stored, "
            "since an empty override behaves the same as having none "
            "at all."
        ),
        surfaces=("mcp",),
        restart_required=False,
        risk="low",
        group="ingest_tagging",
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["reconcile_model"],
    ),
    SettingSpec(
        name="reconcile_timeout_s",
        label="Reconcile timeout",
        type="float",
        default=20.0,
        description=(
            "Per-verdict timeout, in seconds, for the reconcile tier. "
            "The default is 20 seconds. Must be a positive number — "
            "zero and negative values are refused rather than stored, "
            "since such a value would have no effect anyway. Raise "
            "this if verdicts are being judged by a slower model; "
            "lower it if you'd rather fail fast than wait."
        ),
        surfaces=("mcp",),
        restart_required=False,
        risk="low",
        group="ingest_tagging",
        settings_live=True,
        env_var=ENV_VAR_BY_FIELD["reconcile_timeout_s"],
        minimum=0.0,
        minimum_exclusive=True,
    ),
    SettingSpec(
        name="graph_seeding",
        label="Graph seeding",
        type="bool",
        default=True,
        description=(
            "Seeds the graph retrieval axis from a pack's intent, "
            "rather than falling back to a plain recency window that "
            "ignores what the request is about. On by default. "
            "Turning this off makes graph results an intent-blind "
            "recency window, which generally returns less relevant "
            "graph context."
        ),
        # Kept out of write_config.py/WriteBehaviourConfig on purpose:
        # mixing a retrieval toggle into the write-behaviour module
        # would make `trellis admin write-config` report something it
        # does not govern.
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="low",
        group="retrieval",
        settings_live=True,
        env_var="TRELLIS_GRAPH_SEEDING",
    ),
    SettingSpec(
        name="filter_legacy_advisories",
        label="Filter legacy advisories",
        type="bool",
        default=True,
        description=(
            "Drops advisories that predate scoped success-history "
            "tracking from what a pack serves, since the learning loop "
            "has no history to grade them on. On by default, matching "
            "how the system has always behaved. Turning this off lets "
            "those older, ungraded advisories back into served packs."
        ),
        # Kept out of write_config.py/WriteBehaviourConfig for the same
        # reason as graph_seeding: it governs what retrieval serves,
        # not how a write behaves.
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="low",
        group="retrieval",
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
    "GROUP_LABELS",
    "SETTINGS_REGISTRY",
    "Risk",
    "SettingGroup",
    "SettingSpec",
    "SettingType",
    "get_setting",
    "list_settings",
    "resolve_source",
    "validate_value",
]
