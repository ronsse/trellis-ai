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
fields; ``graph_seeding``
(:data:`trellis.retrieve.builder_factory.GRAPH_SEEDING_ENV`) and
``filter_legacy_advisories``
(:data:`trellis.retrieve.pack_builder.FILTER_LEGACY_ADVISORIES_ENV`) as
catalog entries outside ``WriteBehaviourConfig``; ``class_prior_mode``
(:mod:`trellis.retrieve.class_prior`) the same way — the gate's other two
fields, ``forms``/``axes``, stay env-only since this module has no
list-typed field, so they are deliberately not catalog entries; eight more
read-side knobs, also outside ``WriteBehaviourConfig``:
the knowledge-graph
subgraph depth cap, the two noise-demotion evidence thresholds, the two
advisory-generation thresholds, and the three capture-health knobs (the
warning threshold, its trailing window, and whether a partial
session-capture sweep fails the run) — each registered only once its real
enforcement site(s) were confirmed to resolve env > settings > default
through a dedicated resolver, same as every other ``settings_live=True``
entry below; the tuner's three ``learning.auto_promote.*`` knobs
(:func:`trellis.learning.tuners.auto_promote.resolve_auto_promote_overrides`,
env > settings > ``config.yaml`` > default); the six
``learning.candidate_auto_promote.*`` knobs for
:class:`~trellis.learning.candidate_auto_promote.LearningCandidateAutoPromotePolicy`
(env > settings > default — no ``config.yaml`` layer, since this policy has
no legacy file-based config to shadow); and the three
``learning.precedent_auto_demote.*`` knobs for
:class:`~trellis.learning.precedent_auto_demote.PrecedentAutoDemotePolicy`
(:func:`~trellis.learning.precedent_auto_demote.resolve_precedent_auto_demote_overrides`,
same env > settings > default shape as the candidate-promotion family — this
is commit mode's rollback policy, not a parameter of the promotion policy
itself); and seven pack-size/excerpt knobs — ``get_context_max_tokens``,
``get_context_max_items`` and ``search_max_tokens`` for the mcp surface,
``rest_pack_max_items`` and ``rest_pack_max_tokens`` for ``POST /packs``
(resolved from the request's ``model_fields_set``, never from
:class:`~trellis_wire.dtos.PackRequest`'s own field defaults, so the REST
DTO and its OpenAPI schema are untouched), and ``disclosure_body_items``
(:data:`trellis.retrieve.disclosure.DEFAULT_BODY_ITEMS`) and
``excerpt_max_chars`` (:data:`trellis.retrieve.excerpts.EXCERPT_MAX_CHARS`)
for every surface that builds a pack through
:func:`~trellis.retrieve.builder_factory.build_pack_builder` — each
surface's shipped default is left exactly as it was and the five
``max_tokens``/``max_items`` defaults are deliberately not unified across
surfaces. Not registered: anything
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
``mcp/server.py``, ``builder_factory.py``, ``pack_builder.py``,
``class_prior.py``, ``demotion_gate.py``, ``advisory_generator.py``,
``capture_health.py``, ``session_capture/sweep.py``,
``trellis_cli.worker._build_auto_promote_policy``,
``trellis.learning.candidate_auto_promote``,
``trellis.learning.precedent_auto_demote``, ``retrieve/strategies.py``,
``trellis_api/routes/retrieve.py``) now resolves env > settings
> default (the two auto-promote families add a ``config.yaml`` and/or
default-only layer beneath that — see their own entries above) via
:func:`trellis.core.write_config.resolve_write_behaviour` or an
equivalent per-knob settings-aware read, so ``settings_live`` is ``True``
for every entry except ``minhash_seed_max_docs`` and
``graph_max_subgraph_depth`` — both read once at process start (see
their own entries) — which instead carry ``restart_required=True`` and
state why in their descriptions.
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
from trellis.learning.candidate_auto_promote import (
    CANDIDATE_AUTO_PROMOTE_ENABLED_ENV,
    CANDIDATE_AUTO_PROMOTE_MAX_PROMOTIONS_PER_RUN_ENV,
    CANDIDATE_AUTO_PROMOTE_MIN_DISTINCT_SESSIONS_ENV,
    CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT_ENV,
    CANDIDATE_AUTO_PROMOTE_MODE_ENV,
    CANDIDATE_AUTO_PROMOTE_REQUIRE_ZERO_UNHELPFUL_ENV,
    MAX_PROMOTIONS_PER_RUN_FLOOR,
    MIN_DISTINCT_SESSIONS_FLOOR,
    MIN_HELPFUL_COUNT_FLOOR,
)
from trellis.learning.precedent_auto_demote import (
    MAX_DEMOTIONS_PER_RUN_FLOOR,
    PRECEDENT_AUTO_DEMOTE_ENABLED_ENV,
    PRECEDENT_AUTO_DEMOTE_MAX_DEMOTIONS_PER_RUN_ENV,
    PRECEDENT_AUTO_DEMOTE_MODE_ENV,
)
from trellis.learning.tuners.auto_promote import (
    AUTO_PROMOTE_ENABLED_ENV,
    AUTO_PROMOTE_MIN_EFFECT_SIZE_ENV,
    AUTO_PROMOTE_MIN_SAMPLE_SIZE_ENV,
    DEFAULT_AUTO_MIN_EFFECT_SIZE,
    DEFAULT_AUTO_MIN_SAMPLE_SIZE,
)
from trellis.learning.tuners.promotion import (
    DEFAULT_MIN_EFFECT_SIZE,
    DEFAULT_MIN_SAMPLE_SIZE,
)

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

#: The UI's filter-box categories: which area of the system a knob
#: belongs to, independent of ``risk`` or ``settings_live``.
#: ``"learning_loops"`` holds the tuner's ``learning.auto_promote.*``, the
#: ``learning.candidate_auto_promote.*`` and the
#: ``learning.precedent_auto_demote.*`` entries below, out of this
#: module's 42 entries total.
SettingGroup = Literal[
    "retrieval", "ingest_tagging", "learning_loops", "feedback_attribution", "ops"
]

#: The one place a human-facing group name lives, so a UI reading this
#: registry never hardcodes a display label. Keys are exactly
#: :data:`SettingGroup`'s members —
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
    #: Values that pass type/enum/bounds checking but are refused anyway,
    #: with ``refused_reason`` as the message. For a value that is
    #: structurally valid and worth keeping in ``enum`` so the UI still
    #: documents it as a real (future) option, but is not yet safe to
    #: accept — e.g. ``learning.candidate_auto_promote.mode``'s and
    #: ``learning.precedent_auto_demote.mode``'s ``"commit"``, which this
    #: registry refuses at the Settings layer unconditionally, as a
    #: second, independent gate alongside each policy's own
    #: ``__post_init__`` check — only an environment variable can ever set
    #: either to ``"commit"``. Checked in
    #: :func:`validate_value` after ``_check_bounds``, so it layers a
    #: business-rule refusal on top of, never instead of, the
    #: type/enum/bounds one.
    refused_values: tuple[Any, ...] = ()
    refused_reason: str | None = None
    #: ``False`` means :class:`~trellis.mutate.handlers.SettingsUpdateHandler`
    #: refuses a write that sets a value (clearing one, ``value=None``, is
    #: still allowed, so a stray legacy row can be removed). Reserved for
    #: a knob whose enforcement site never reads the settings store at
    #: all, at *any* point in its process -- not merely one that is slow
    #: to pick up a change (that is ``restart_required``, which stays
    #: writable: a restart's first read still sees the stored value).
    #: ``graph_max_subgraph_depth`` is the only such entry today: storing
    #: a value there would be accepted and echoed back by
    #: ``GET /settings`` with no code path that could ever make it real.
    settings_writable: bool = True


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
            "Runs extraction on every memory write and adds entities a "
            "memory mentions, which is not proof they exist; those stay "
            "out of packs until confirmed, but the graph grows with "
            "every write and the additions are not undone by turning "
            "this off again."
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
            "and no such judgment runs. A memory this marks superseded "
            "is withheld from a pack whenever its successor is also a "
            "candidate there, so turning this on changes what packs "
            "serve, not only how writes are recorded."
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
            "The model name recorded on reconcile verdict events when "
            "the model client does not report its own. It is a label "
            "for the audit trail only: it does not choose which model "
            "judges verdicts, which comes from the configured LLM "
            "client. Free text, not checked against available models; "
            "a blank or whitespace-only value is refused."
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
            "Withholds advisories written by the advisory generator "
            "before it was repaired. Those rows were given a new random "
            "id on every nightly run, so regeneration never replaces "
            "them, and they carry no evidence-confidence data, so the "
            "learning loop cannot grade them. On by default. Turning "
            "this off serves those old rows again; it does not delete "
            "them."
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
    SettingSpec(
        name="class_prior_mode",
        label="Conversation document prior",
        type="str",
        default="off",
        description=(
            "Applies a class-level prior to imported conversation "
            "documents on the retrieval axes TRELLIS_CLASS_PRIOR_AXES "
            "names (keyword only, by default) — a judgment about the "
            "whole document form, not a per-item one. 'off' (the "
            "default) serves this class exactly like everything else. "
            "'withhold' drops it from the pack outright, recorded under "
            "a rejection reason distinct from noise/archived/superseded. "
            "Withholds conversation parent documents; their chunks carry "
            "no document form and are never touched. Measured on the "
            "live store: this class took about 11% of pack tokens on "
            "the keyword axis while being marked helpful 0 of 83 times "
            "and unhelpful on 44% of rows there, and only 1.2% of these "
            "documents have ever been marked helpful at all — but other "
            "namespaces are helpful 8-25% of the time, so this must stay "
            "scoped to one document form rather than widened "
            "store-wide."
        ),
        # Which document forms and axes this reaches is set by
        # TRELLIS_CLASS_PRIOR_FORMS / TRELLIS_CLASS_PRIOR_AXES — env-only,
        # since SettingSpec has no list-typed field (see the module
        # docstring's "Scope" section) — defaulting to the imported
        # conversation form on the keyword axis only. See
        # trellis.retrieve.class_prior for the gate itself.
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="caution",
        risk_note=(
            "'withhold' removes matching items from every pack on the "
            "configured axes before an operator sees them."
        ),
        group="retrieval",
        settings_live=True,
        env_var="TRELLIS_CLASS_PRIOR_MODE",
        enum=("off", "withhold"),
    ),
    SettingSpec(
        name="get_context_max_tokens",
        label="get_context token budget",
        type="int",
        default=2000,
        description=(
            "Default token budget for mcp get_context's flat (non-"
            "sectioned) layout when a caller omits max_tokens. Raising it "
            "returns more context per call at the cost of a larger "
            "response; lowering it returns less. A caller that passes "
            "max_tokens explicitly always wins over this setting. Default "
            "2000. Does not affect get_context's sections= layout, which "
            "resolves its own budget from config.yaml's retrieval.budgets "
            "(the same mechanism get_objective_context/get_task_context "
            "use) — the two are deliberately separate."
        ),
        risk_note=(
            "A larger budget costs more tokens on every get_context call "
            "that doesn't specify its own; a smaller one can silently "
            "return less than an agent would otherwise have received."
        ),
        surfaces=("mcp",),
        restart_required=False,
        risk="caution",
        group="retrieval",
        settings_live=True,
        env_var="TRELLIS_GET_CONTEXT_MAX_TOKENS",
        minimum=1,
        maximum=200000,
    ),
    SettingSpec(
        name="get_context_max_items",
        label="get_context item ceiling",
        type="int",
        default=50,
        description=(
            "Ceiling on how many candidate items mcp get_context's flat "
            "layout considers before the token budget trims the list. "
            "get_context exposes no caller-facing max_items parameter, so "
            "this setting is the only way to change it without an env "
            "var. Raising it widens the candidate pool the token budget "
            "then walks; lowering it narrows the pool before ranking. "
            "Default 50."
        ),
        risk_note=(
            "Raising this widens the candidate pool PackBuilder ranks "
            "before budgeting, costing more compute per call; it does "
            "not by itself grow the response, since max_tokens still "
            "trims the rendered pack."
        ),
        surfaces=("mcp",),
        restart_required=False,
        risk="caution",
        group="retrieval",
        settings_live=True,
        env_var="TRELLIS_GET_CONTEXT_MAX_ITEMS",
        minimum=1,
        maximum=5000,
    ),
    SettingSpec(
        name="search_max_tokens",
        label="search token budget",
        type="int",
        default=2000,
        description=(
            "Default token budget for the mcp search tool when a caller "
            "omits max_tokens. Same shape as get_context_max_tokens, but "
            "resolved independently — search and get_context currently "
            "ship the same default (2000) by coincidence, not by "
            "unification, so changing one does not change the other. A "
            "caller that passes max_tokens explicitly always wins over "
            "this setting. Default 2000."
        ),
        risk_note=(
            "A larger budget costs more tokens on every search call that "
            "doesn't specify its own; a smaller one can silently return "
            "less than an agent would otherwise have received."
        ),
        surfaces=("mcp",),
        restart_required=False,
        risk="caution",
        group="retrieval",
        settings_live=True,
        env_var="TRELLIS_SEARCH_MAX_TOKENS",
        minimum=1,
        maximum=200000,
    ),
    SettingSpec(
        name="rest_pack_max_items",
        label="REST pack item ceiling",
        type="int",
        default=50,
        description=(
            "Default max_items for POST /packs when the request body "
            "omits the field. Resolved from the request's own "
            "model_fields_set, not from PackRequest's field default, so "
            "a caller that supplies max_items — even the value 50 — "
            "always wins over this setting. Independent of "
            "get_context_max_items: REST's shipped default (50) happens "
            "to match get_context's today, but the two surfaces are "
            "tuned separately by design. Default 50."
        ),
        risk_note=(
            "Raising this widens the candidate pool POST /packs ranks "
            "before budgeting, costing more compute per call, and can "
            "return a larger response for callers that omit max_items."
        ),
        surfaces=("api",),
        restart_required=False,
        risk="caution",
        group="retrieval",
        settings_live=True,
        env_var="TRELLIS_REST_PACK_MAX_ITEMS",
        minimum=1,
        maximum=5000,
    ),
    SettingSpec(
        name="rest_pack_max_tokens",
        label="REST pack token budget",
        type="int",
        default=8000,
        description=(
            "Default max_tokens for POST /packs when the request body "
            "omits the field. Resolved from the request's own "
            "model_fields_set, not from PackRequest's field default, so "
            "a caller that supplies max_tokens — even the value 8000 — "
            "always wins over this setting. Independent of "
            "get_context_max_tokens/search_max_tokens: REST's shipped "
            "default (8000) is larger than either MCP tool's (2000), and "
            "the three are deliberately not unified. Default 8000."
        ),
        risk_note=(
            "A larger budget costs more tokens on every POST /packs call "
            "that omits max_tokens; a smaller one can silently return "
            "less than a caller would otherwise have received."
        ),
        surfaces=("api",),
        restart_required=False,
        risk="caution",
        group="retrieval",
        settings_live=True,
        env_var="TRELLIS_REST_PACK_MAX_TOKENS",
        minimum=1,
        maximum=200000,
    ),
    SettingSpec(
        name="disclosure_body_items",
        label="Full-body disclosure ceiling",
        type="int",
        default=12,
        description=(
            "How many ranked pack items keep their excerpt before "
            "graduated disclosure turns the rest into one-line pointers "
            "(label plus the size of the withheld body, fetchable with "
            "get_items) instead of dropping them. Raising it gives more "
            "items a body at the cost of a larger pack for the same item "
            "count; lowering it switches to pointers sooner. Applies to every "
            "surface that assembles a pack through build_pack_builder "
            "(mcp, REST, the CLI's operator preview, the pack-quality "
            "evaluator). Default 12."
        ),
        risk_note=(
            "Raising this increases the token cost of a pack with the "
            "same item count, since more items render with a body "
            "rather than as a pointer."
        ),
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="caution",
        group="retrieval",
        settings_live=True,
        env_var="TRELLIS_DISCLOSURE_BODY_ITEMS",
        minimum=1,
        maximum=5000,
    ),
    SettingSpec(
        name="excerpt_max_chars",
        label="Excerpt character cap",
        type="int",
        default=500,
        description=(
            "Character cap applied to each retrieved item's excerpt "
            "(keyword, semantic and graph axes) before it enters a pack. "
            "Raising it lets each item carry more text at the cost of "
            "fewer items fitting the same token budget; lowering it fits "
            "more items per pack at the cost of shorter excerpts. "
            "Applies to every surface that assembles a pack through "
            "build_pack_builder. Default 500."
        ),
        risk_note=(
            "Raising this trades item count for per-item detail within "
            "the same token budget — a pack's max_items/max_tokens are "
            "unchanged, but fewer items survive before the budget fills."
        ),
        surfaces=("mcp", "api", "cli"),
        restart_required=False,
        risk="caution",
        group="retrieval",
        settings_live=True,
        env_var="TRELLIS_EXCERPT_MAX_CHARS",
        minimum=1,
        maximum=50000,
    ),
    SettingSpec(
        name="learning.auto_promote.enabled",
        label="Auto-promote tuner proposals",
        type="bool",
        default=False,
        description=(
            "Let `trellis worker tune` auto-promote a tuner proposal "
            "through the same governed pipeline `trellis metrics promote "
            "--commit` uses, with no human looking at it first. Off "
            "(default): the tuner still runs and still writes `pending` "
            "proposals, it just never promotes one itself. On: a "
            "qualifying proposal is promoted unattended on the next "
            "*live* run of `trellis worker tune` (one invoked without "
            "--dry-run) — the job catalog's own scheduled `tune` entry "
            "always passes --dry-run, so flipping this on changes "
            "nothing until something runs the command live, scheduled "
            "or manual. Pair it with the min_sample_size / "
            "min_effect_size floors below, never ship it alone."
        ),
        surfaces=("cli",),
        restart_required=False,
        risk="high",
        risk_note=(
            "Changes what agents are served, unattended, without a human "
            "reviewing the specific proposal first."
        ),
        group="learning_loops",
        settings_live=True,
        env_var=AUTO_PROMOTE_ENABLED_ENV,
    ),
    SettingSpec(
        name="learning.auto_promote.min_sample_size",
        label="Tuner auto-promote: minimum sample size",
        type="int",
        default=DEFAULT_AUTO_MIN_SAMPLE_SIZE,
        description=(
            "Minimum `ParameterProposal.sample_size` an auto-promotion "
            "will accept. Shipped default (30) is 6x the manual-review "
            "floor (5) a human-eyeballed promotion accepts — an "
            "unattended promotion has no reviewer to catch a thin sample, "
            "so it demands more evidence going in. Cannot be set below "
            "the manual floor, by settings or by env."
        ),
        surfaces=("cli",),
        restart_required=False,
        risk="low",
        group="learning_loops",
        settings_live=True,
        env_var=AUTO_PROMOTE_MIN_SAMPLE_SIZE_ENV,
        minimum=DEFAULT_MIN_SAMPLE_SIZE,
    ),
    SettingSpec(
        name="learning.auto_promote.min_effect_size",
        label="Tuner auto-promote: minimum effect size",
        type="float",
        default=DEFAULT_AUTO_MIN_EFFECT_SIZE,
        description=(
            "Minimum measured relative effect against the live baseline "
            "an auto-promotion will accept, in [0, 1]. Shipped default "
            "(0.25) is stricter than the manual-review floor (0.15) for "
            "the same reason as min_sample_size: no reviewer, so a larger "
            "margin is required before noise becomes an implausible "
            "explanation. Cannot be set below the manual floor, by "
            "settings or by env."
        ),
        surfaces=("cli",),
        restart_required=False,
        risk="low",
        group="learning_loops",
        settings_live=True,
        env_var=AUTO_PROMOTE_MIN_EFFECT_SIZE_ENV,
        minimum=DEFAULT_MIN_EFFECT_SIZE,
        maximum=1.0,
    ),
    SettingSpec(
        name="learning.candidate_auto_promote.enabled",
        label="Auto-evaluate learning candidates",
        type="bool",
        default=False,
        description=(
            "This master switch has no observable effect on its own "
            "today. `trellis curate auto-promote-candidates` always "
            "stays report-only unless the mode setting below is also "
            "switched to `commit` via its environment variable, and "
            "that is refused unless learning.precedent_auto_demote."
            "enabled is on too. This master switch starts gating real "
            "writes only once both that mode and the demotion policy "
            "are enabled."
        ),
        surfaces=("cli",),
        restart_required=False,
        risk="low",
        group="learning_loops",
        settings_live=True,
        env_var=CANDIDATE_AUTO_PROMOTE_ENABLED_ENV,
    ),
    SettingSpec(
        name="learning.candidate_auto_promote.mode",
        label="Learning-candidate auto-promote mode",
        type="str",
        default="report_only",
        description=(
            "`report_only` (default) evaluates every candidate and emits "
            "the disposition event but never writes. `commit` promotes "
            "each qualifying candidate through the same governed path a "
            "human review-queue approval uses, but only once "
            "learning.precedent_auto_demote.enabled is also on — that "
            "policy is commit mode's rollback, so running one without the "
            "other would leave a wrong unattended promotion with no "
            "automatic undo. This setting itself cannot be written to "
            "`commit` through Settings at all, by design (see "
            "`refused_values` below); only the environment variable can, "
            "and even then construction refuses it unless auto-demotion "
            "is enabled too."
        ),
        surfaces=("cli",),
        restart_required=False,
        risk="caution",
        risk_note=(
            "Setting this to `commit` is refused immediately, at save "
            "time, regardless of any other configuration — this Settings-"
            "layer block is permanent, not merely until auto-demotion "
            "ships (it has). `commit` stays in the enum above because it "
            "documents a real, reachable-by-env-var value, not a typo to "
            "reject silently."
        ),
        group="learning_loops",
        settings_live=True,
        env_var=CANDIDATE_AUTO_PROMOTE_MODE_ENV,
        enum=("report_only", "commit"),
        refused_values=("commit",),
        refused_reason=(
            "commit mode cannot be set through Settings; only the "
            f"{CANDIDATE_AUTO_PROMOTE_MODE_ENV} environment variable can, "
            "and construction still refuses it unless auto-demotion "
            "(learning.precedent_auto_demote.enabled) is also on"
        ),
    ),
    SettingSpec(
        name="learning.candidate_auto_promote.min_helpful_count",
        label="Learning-candidate auto-promote: minimum helpful count",
        type="int",
        default=5,
        description=(
            "Minimum helpful-citation count a learning candidate needs "
            "before the report-only job will mark it `would_promote` "
            "instead of `ineligible`. Conservative default (5): the "
            "manual review queue has no equivalent floor today, so this "
            "number has no human-reviewed precedent to match — set "
            "deliberately high until the agreement metric (would_promote "
            "vs. a later human decision) gives real evidence to lower it."
        ),
        surfaces=("cli",),
        restart_required=False,
        risk="low",
        group="learning_loops",
        settings_live=True,
        env_var=CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT_ENV,
        minimum=MIN_HELPFUL_COUNT_FLOOR,
    ),
    SettingSpec(
        name="learning.candidate_auto_promote.min_distinct_sessions",
        label="Learning-candidate auto-promote: minimum distinct sessions",
        type="int",
        default=3,
        description=(
            "Minimum number of distinct sessions whose helpful citations "
            "count toward min_helpful_count — a real conversation "
            "identity (`PACK_ASSEMBLED.session_id`), never a pack or run "
            "id, and a citation with no captured session id never counts "
            "toward this floor either. Floored at 2 and defaulted to 3: "
            "a single session repeatedly citing the same candidate is one "
            "data point wearing several counts as a disguise, not "
            "corroboration — a single-session candidate can never "
            "qualify no matter how high its raw helpful count climbs."
        ),
        surfaces=("cli",),
        restart_required=False,
        risk="low",
        group="learning_loops",
        settings_live=True,
        env_var=CANDIDATE_AUTO_PROMOTE_MIN_DISTINCT_SESSIONS_ENV,
        minimum=MIN_DISTINCT_SESSIONS_FLOOR,
    ),
    SettingSpec(
        name="learning.candidate_auto_promote.require_zero_unhelpful",
        label="Learning-candidate auto-promote: require zero unhelpful",
        type="bool",
        default=True,
        description=(
            "Require zero unhelpful citations for a candidate to be "
            "reported as `would_promote`, on top of the helpful-count / "
            "session floors. On by default. Turning this off lets the "
            "report-only job mark a candidate `would_promote` despite a "
            "documented unhelpful citation already against it — the "
            "exact evidence the manual review queue would use to reject "
            "it. Shapes the report today; in commit mode (default off) "
            "it shapes which candidates get written, not only reported."
        ),
        surfaces=("cli",),
        restart_required=False,
        risk="caution",
        risk_note=(
            "Makes the report-only output disagree with what a human "
            "reviewer would very likely do with the same candidate, on "
            "purpose rather than by threshold miscalibration."
        ),
        group="learning_loops",
        settings_live=True,
        env_var=CANDIDATE_AUTO_PROMOTE_REQUIRE_ZERO_UNHELPFUL_ENV,
    ),
    SettingSpec(
        name="learning.candidate_auto_promote.max_promotions_per_run",
        label="Learning-candidate auto-promote: max promotions per run",
        type="int",
        default=3,
        description=(
            "This has no observable effect while commit mode stays "
            "refused or while the master switch above is off. Once "
            "both commit mode (see learning.candidate_auto_promote."
            "mode) and that switch are enabled, this ceilings how many "
            "candidates one `commit`-mode run may promote, regardless "
            "of how many qualify — bounding a single scheduled run's "
            "blast radius, so a bug that makes every candidate qualify "
            "still only promotes a few before the job stops."
        ),
        surfaces=("cli",),
        restart_required=False,
        risk="low",
        group="learning_loops",
        settings_live=True,
        env_var=CANDIDATE_AUTO_PROMOTE_MAX_PROMOTIONS_PER_RUN_ENV,
        minimum=MAX_PROMOTIONS_PER_RUN_FLOOR,
    ),
    SettingSpec(
        name="learning.precedent_auto_demote.enabled",
        label="Auto-evaluate precedents for demotion",
        type="bool",
        default=False,
        description=(
            "This master switch alone does not make `trellis curate "
            "evaluate-precedent-demotion` write anything: that stays "
            "report-only unless the mode setting below is also switched "
            "to `commit` via its environment variable. But it does have "
            "an immediate effect on the sibling promotion policy: "
            "learning.candidate_auto_promote.mode can only be "
            "constructed as `commit` once this switch is on, because "
            "this policy is that commit mode's rollback — so turning "
            "this on unlocks promotion's commit mode even while this "
            "policy itself keeps only evaluating. Once this switch and "
            "the mode setting below are both on, a run demotes each "
            "current precedent the evidence gate judges `would_demote` "
            "through the same governed path a human `trellis curate "
            "demote-precedent` call uses, capped by "
            "max_demotions_per_run. The unhelpful-citation floor the "
            "gate judges against is provisional: borrowed from the "
            "noise-document demotion gate and measured for noise "
            "documents, not for precedents."
        ),
        surfaces=("cli",),
        restart_required=False,
        risk="low",
        group="learning_loops",
        settings_live=True,
        env_var=PRECEDENT_AUTO_DEMOTE_ENABLED_ENV,
    ),
    SettingSpec(
        name="learning.precedent_auto_demote.mode",
        label="Precedent auto-demote mode",
        type="str",
        default="report_only",
        description=(
            "`report_only` (default) evaluates every current precedent "
            "against the evidence gate and emits the disposition event "
            "but never writes. `commit` demotes each `would_demote` "
            "precedent through the same governed path a human `trellis "
            "curate demote-precedent` call uses, capped by "
            "max_demotions_per_run. Unlike "
            "learning.candidate_auto_promote.mode, this policy's own "
            "commit mode carries no further gate on a sibling policy: "
            "demotion is itself the rollback a safe commit mode depends "
            "on, so there is nothing else for it to wait on. This "
            "setting itself cannot be written to `commit` through "
            "Settings at all, by design (see `refused_values` below); "
            "only the environment variable can."
        ),
        surfaces=("cli",),
        restart_required=False,
        risk="caution",
        risk_note=(
            "Setting this to `commit` is refused immediately, at save "
            "time, regardless of any other configuration — this "
            "Settings-layer block is permanent. `commit` stays in the "
            "enum above because it documents a real, "
            "reachable-by-env-var value, not a typo to reject silently."
        ),
        group="learning_loops",
        settings_live=True,
        env_var=PRECEDENT_AUTO_DEMOTE_MODE_ENV,
        enum=("report_only", "commit"),
        refused_values=("commit",),
        refused_reason=(
            "commit mode cannot be set through Settings; only the "
            f"{PRECEDENT_AUTO_DEMOTE_MODE_ENV} environment variable can"
        ),
    ),
    SettingSpec(
        name="learning.precedent_auto_demote.max_demotions_per_run",
        label="Precedent auto-demote: max demotions per run",
        type="int",
        default=3,
        description=(
            "This has no observable effect while commit mode stays "
            "refused or while the master switch above is off. Once both "
            "commit mode (see learning.precedent_auto_demote.mode) and "
            "that switch are enabled, this ceilings how many precedents "
            "one `commit`-mode run may demote, regardless of how many "
            "the evidence gate judges `would_demote` — bounding a single "
            "scheduled run's blast radius, so a bug that makes every "
            "precedent qualify still only demotes a few before the job "
            "stops. A separate floor and default from "
            "learning.candidate_auto_promote.max_promotions_per_run: "
            "promotion and demotion are different blast radii, and "
            "nothing requires they move together."
        ),
        surfaces=("cli",),
        restart_required=False,
        risk="low",
        group="learning_loops",
        settings_live=True,
        env_var=PRECEDENT_AUTO_DEMOTE_MAX_DEMOTIONS_PER_RUN_ENV,
        minimum=MAX_DEMOTIONS_PER_RUN_FLOOR,
    ),
    SettingSpec(
        name="graph_max_subgraph_depth",
        label="Graph subgraph depth cap",
        type="int",
        default=6,
        description=(
            "Caps how many hops a knowledge-graph subgraph query can "
            "follow out from its starting node. A request for a deeper "
            "traversal than this is refused outright rather than served "
            "a slow, truncated result. The default is 6. This is read "
            "only from the environment variable below, once when the "
            "process starts — a restart does not help a stored value "
            "take effect, because enforcement never reads the settings "
            "store at all. Set the environment variable instead; a "
            "write here is refused."
        ),
        surfaces=("mcp", "api"),
        restart_required=True,
        risk="caution",
        risk_note=(
            "Raising this lets a single query follow more hops, and on "
            "a well-connected graph the number of paths a query can "
            "explore grows multiplicatively with each extra hop — a "
            "high value can turn one call into a slow, expensive scan."
        ),
        group="ops",
        settings_live=False,
        env_var="TRELLIS_GRAPH_MAX_SUBGRAPH_DEPTH",
        minimum=1,
        maximum=1000,
        settings_writable=False,
    ),
    SettingSpec(
        name="noise_demotion_min_attributed_packs",
        label="Noise-demotion coverage floor",
        type="int",
        default=5,
        description=(
            "Minimum number of packs, in the analysis window, that must "
            "carry a per-item helpful/unhelpful verdict before any item "
            "can be proposed for demotion to noise. Below this floor "
            "there isn't enough graded coverage to trust a rate computed "
            "from it, so the whole batch is refused rather than demoting "
            "on a guess. The default is 5."
        ),
        surfaces=("api", "cli"),
        restart_required=False,
        risk="caution",
        risk_note=(
            "Lowering this toward 1 lets a demotion decision run against "
            "a window with almost no graded coverage; raising it makes a "
            "thin week refuse to demote anything rather than act on weak "
            "evidence."
        ),
        group="feedback_attribution",
        settings_live=True,
        env_var="TRELLIS_NOISE_DEMOTION_MIN_ATTRIBUTED_PACKS",
        minimum=1,
        # A sanity cap against a typo or an overflow-class value (e.g.
        # 10**400), not an operational ceiling — see
        # minhash_seed_max_docs above for the same posture.
        maximum=1_000_000,
    ),
    SettingSpec(
        name="noise_demotion_min_unhelpful_citations",
        label="Noise-demotion evidence floor",
        type="int",
        default=2,
        description=(
            "Minimum number of explicit unhelpful citations an item must "
            "receive — and it must be cited unhelpful more often than "
            "helpful — before it can be proposed for demotion to noise. "
            "The default is 2: a single unhelpful citation is treated as "
            "too little to act on."
        ),
        surfaces=("api", "cli"),
        restart_required=False,
        risk="caution",
        risk_note=(
            "Setting this to 1 lets a single unhelpful citation demote "
            "an item on its own; the coverage floor above still has to "
            "be met before that can happen."
        ),
        group="feedback_attribution",
        settings_live=True,
        env_var="TRELLIS_NOISE_DEMOTION_MIN_UNHELPFUL_CITATIONS",
        minimum=1,
        maximum=1_000_000,
    ),
    SettingSpec(
        name="advisory_min_sample_size",
        label="Advisory minimum sample size",
        type="int",
        default=5,
        description=(
            "Minimum number of graded observations behind a comparison "
            "before the advisory generator will write it up as an "
            "advisory. Below this, a pattern is coming from too few "
            "examples to trust. The default is 5."
        ),
        surfaces=("api", "cli"),
        restart_required=False,
        risk="caution",
        risk_note=(
            "Lowering this lets an advisory get written from fewer "
            "graded observations, which makes it more likely to "
            "generalize from a small, unrepresentative handful of "
            "outcomes."
        ),
        group="feedback_attribution",
        settings_live=True,
        env_var="TRELLIS_ADVISORY_MIN_SAMPLE_SIZE",
        minimum=1,
        maximum=1_000_000,
    ),
    SettingSpec(
        name="advisory_min_effect_size",
        label="Advisory minimum effect size",
        type="float",
        default=0.15,
        description=(
            "Minimum gap, in success rate, between the agents who "
            "followed an advisory's condition and those who didn't, "
            "before that comparison is worth writing up. A smaller "
            "observed gap is treated as noise, not signal. The default "
            "is 0.15 — a 15 percentage point gap."
        ),
        surfaces=("api", "cli"),
        restart_required=False,
        risk="caution",
        risk_note=(
            "Lowering this toward 0 lets advisories get written from "
            "smaller and smaller observed differences, which are more "
            "likely to be noise rather than a real effect."
        ),
        group="feedback_attribution",
        settings_live=True,
        env_var="TRELLIS_ADVISORY_MIN_EFFECT_SIZE",
        minimum=0.0,
        maximum=1.0,
    ),
    SettingSpec(
        name="capture_warn_threshold",
        label="Capture-warning rejection threshold",
        type="int",
        default=3,
        description=(
            "How many rejected writes a single capture surface needs, "
            "with none accepted in the same window, before a warning is "
            "prepended to every pack it serves. The default is 3, and 0 "
            "turns the check off entirely — no warning is ever raised."
        ),
        surfaces=("mcp",),
        restart_required=False,
        risk="low",
        group="ops",
        settings_live=True,
        env_var="TRELLIS_CAPTURE_WARN_THRESHOLD",
        minimum=0,
        maximum=1_000_000,
    ),
    SettingSpec(
        name="capture_warn_window_hours",
        label="Capture-warning window",
        type="int",
        default=24,
        description=(
            "Trailing window, in hours, that the capture-warning "
            "rejection threshold counts rejections over. The default is "
            "24 hours. A shorter window clears the warning faster once "
            "writes start succeeding again; a longer one keeps it up "
            "longer after a brief outage."
        ),
        surfaces=("mcp",),
        restart_required=False,
        risk="low",
        group="ops",
        settings_live=True,
        env_var="TRELLIS_CAPTURE_WARN_WINDOW_HOURS",
        minimum=0,
        # 87600 hours = 10 years. The real failure mode this bounds is
        # not an operational need past a year or two, it's
        # ``datetime.now() - timedelta(hours=stored)`` landing before
        # year 1 (or overflowing timedelta's own range for a much larger
        # typo) inside capture_health.check_capture_health — see that
        # module for the OverflowError/ValueError fallback kept as
        # defense in depth even with this cap in place.
        maximum=87_600,
    ),
    SettingSpec(
        name="capture_strict",
        label="Strict session-capture sweeps",
        type="bool",
        default=True,
        description=(
            "Whether a session-capture sweep that leaves sessions "
            "unjudged, or skips one that raised, counts as a failed "
            "run. On (the default), such a sweep exits with an error, "
            "so a monitoring job that only watches the exit code still "
            "notices. Off, the sweep reports the counts and exits "
            "cleanly instead — those sessions stay un-watermarked and "
            "are retried on the next sweep. A sweep that captured "
            "nothing at all because no judge was configured always "
            "fails, regardless of this setting."
        ),
        surfaces=("cli",),
        restart_required=False,
        risk="caution",
        risk_note=(
            "Turning this off makes a sweep with partial failures exit "
            "cleanly, so a monitoring job watching only the exit code "
            "stops noticing them — the affected sessions are still "
            "retried on the next sweep, but nothing will flag that they "
            "needed one."
        ),
        group="ops",
        settings_live=True,
        env_var="TRELLIS_CAPTURE_STRICT",
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
            # A refused member (e.g. ``"commit"`` before auto-demotion
            # ships) stays in ``spec.enum`` so the UI can still document
            # it as a real future option (see ``refused_values``'s
            # docstring), but this message tells the caller what they can
            # actually set right now -- listing a value every submission
            # of it will itself refuse is not "allowed".
            allowed = [v for v in spec.enum if v not in spec.refused_values]
            msg = f"{spec.name} must be one of {allowed!r}, got {value!r}"
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
    if spec.refused_values and value in spec.refused_values:
        msg = spec.refused_reason or f"{spec.name}={value!r} is not accepted."
        raise ValueError(msg)
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
