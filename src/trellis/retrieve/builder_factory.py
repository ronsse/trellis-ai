"""The one wiring of a :class:`~trellis.retrieve.pack_builder.PackBuilder`.

Every surface that assembles a pack — MCP, REST, the CLI's operator
preview, and the pack-quality evaluator — builds the same object from the
same registry. Before #410 it was written out four times, and the copies
had already drifted: ``trellis analyze pack-quality`` passed no advisory
store, so a scenario scored against a builder that differed from the one
production serves, under a comment claiming it mirrored it.

That is this repo's recurring defect (#325/#326, #443): two readers of one
seam, each silently reporting a constant. A fifth surface must call this
function rather than repeat the argument list.

The advisory store is resolved through
:func:`~trellis.stores.advisory_source.load_advisory_store` (#373) — the one
place that decides where advisories live, so a reader and the nightly writer
cannot drift onto two files again. There is no ``if path.exists()`` guard:
a missing file yields an *empty* store plus a log line, not a silent
``None``. ``PackBuilder`` filters advisories by confidence and pack domain
scope, so passing the store unconditionally is safe — an empty one behaves
exactly as the old ``None`` did.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypedDict

import structlog

from trellis.core.write_config import WriteBehaviourConfig
from trellis.ops import ParameterRegistry
from trellis.retrieve.class_prior import resolve_class_prior_config
from trellis.retrieve.disclosure import DEFAULT_BODY_ITEMS, DisclosureConfig
from trellis.retrieve.excerpts import EXCERPT_MAX_CHARS
from trellis.retrieve.pack_builder import (
    PackBuilder,
    SemanticDedupConfig,
    _filter_legacy_advisories_enabled,
)
from trellis.retrieve.rerankers import build_reranker
from trellis.retrieve.strategies import (
    GRAPH_STRATEGY_NAME,
    NamespaceSeedExtractor,
    build_strategies,
    resolve_graph_axis_state,
)
from trellis.stores.advisory_source import load_advisory_store
from trellis.stores.settings_store import load_settings_values
from trellis_wire.axes import (
    format_embedder_failed_note,
    format_failed_axes_note,
    format_misconfigured_semantic_note,
)

if TYPE_CHECKING:
    from trellis.retrieve.strategies import EmbedderResolveFailure, GraphSeedExtractor
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

#: Name of the axis that only exists when the deployment has an embedder.
#: :func:`~trellis.retrieve.strategies.build_strategies` appends
#: :class:`~trellis.retrieve.strategies.SemanticSearch` only when an
#: ``embedding_fn`` resolves, and swallows a vector-backend init failure —
#: so "absent" covers two different situations and a caller reporting the
#: gap must not collapse them. See :func:`describe_axes`.
SEMANTIC_AXIS = "semantic"

#: Kill switch for graph seeding (#375). Unset means **on**.
#:
#: A **read-side** knob, so it deliberately does not live in
#: :mod:`trellis.core.write_config` — that module is the one home for
#: *ingest-time* behaviour, and mixing a retrieval toggle into it would
#: make ``trellis admin write-config`` report something it does not
#: govern. Same reasoning, and the same shape, as
#: ``TRELLIS_CAPTURE_WARN_THRESHOLD`` in :mod:`trellis.ops.capture_health`.
#: The one read-side exception is ``TRELLIS_PACK_HOLDOUT_RATE``, which
#: lives there because the pack-effect measurement needs the rate in force
#: stamped into write provenance on every ``PACK_ASSEMBLED`` row.
#:
#: It defaults **on** because the alternative is an opt-in nobody opts
#: into, which leaves #375's defect — an axis that never consults the
#: intent — true on every deployment while looking addressed. What makes
#: that safe to switch on rather than reckless is that the branch is
#: observable on the served record: every item carries
#: ``metadata["graph_selection"]``, forwarded into
#: ``PACK_ASSEMBLED.injected_items[]`` (#371), so "did seeding run, and did
#: it help?" is answerable from the event log without a second instrument
#: — and only produces data once the branch actually runs.
#:
#: It gates seeds *inferred* from the intent and nothing else. A sectioned
#: pack's ``entity_ids`` (``GRAPH_CALLER_SEED_IDS_KEY`` in
#: :mod:`trellis.retrieve.strategies`) and an explicit ``seed_ids`` are
#: instructions, and apply with it off.
GRAPH_SEEDING_ENV = "TRELLIS_GRAPH_SEEDING"

#: Settings-store key for the same knob (#settings-store PR 2/2) — see
#: ``SettingSpec(name="graph_seeding")`` in :mod:`trellis.core.settings_registry`.
#: Deliberately the same spelling as the setting name so a reader never has
#: to map between the two.
GRAPH_SEEDING_SETTING = "graph_seeding"

#: Opt-in for the graph axis's unseeded recency window (#961). Unset means
#: **off**, the opposite default to :data:`GRAPH_SEEDING_ENV`.
#:
#: When nothing seeds the axis, neither the intent nor the caller, the axis
#: used to serve the newest ``limit * overfetch`` nodes whatever was asked.
#: Measured on the reference deployment over 30 days, that was 846 of 890
#: graph-axis servings, and it drew 0.025 helpful against 0.264 unhelpful
#: citations per serving where non-graph items drew 0.083 against 0.160.
#: Off, an unseeded search serves nothing and the pack's ``axes`` block says
#: so (``axes["graph"] == "unseeded_fallback_off"``); seeded and
#: caller-seeded searches are unchanged. A read-side knob, so it lives here
#: beside :data:`GRAPH_SEEDING_ENV` and not in
#: :mod:`trellis.core.write_config`. ``trellis analyze replay
#: --graph-selection recency_window`` still prices the old behaviour from
#: events that carry the stamp.
GRAPH_RECENCY_FALLBACK_ENV = "TRELLIS_GRAPH_RECENCY_FALLBACK"

#: Settings-store key for :data:`GRAPH_RECENCY_FALLBACK_ENV` — see
#: ``SettingSpec(name="graph_recency_fallback")`` in
#: :mod:`trellis.core.settings_registry`.
GRAPH_RECENCY_FALLBACK_SETTING = "graph_recency_fallback"

#: Accepted spellings, matching :mod:`trellis.stores.registry`. A value in
#: neither set is a typo, and a typo'd kill switch that silently keeps the
#: default is how an operator ends up believing they disabled something
#: they did not — so it warns rather than going quiet.
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off"})

#: Settings-registry wiring for the two pack-shaping int knobs below — env
#: wins over a stored override, which wins over the module default. See
#: ``trellis.core.settings_registry``'s ``disclosure_body_items`` /
#: ``excerpt_max_chars`` entries. Read-side, so (like ``GRAPH_SEEDING_ENV``
#: above) they stay out of :mod:`trellis.core.write_config`.
BODY_ITEMS_ENV = "TRELLIS_DISCLOSURE_BODY_ITEMS"
BODY_ITEMS_SETTING = "disclosure_body_items"
EXCERPT_MAX_CHARS_ENV = "TRELLIS_EXCERPT_MAX_CHARS"
EXCERPT_MAX_CHARS_SETTING = "excerpt_max_chars"


def _resolve_int_knob(
    *,
    env_var: str,
    setting_name: str,
    default: int,
    settings: Mapping[str, Any] | None,
) -> int:
    """Resolve one integer knob: env var, then a stored setting, then default.

    Delegates to :func:`trellis.core.knobs.resolve_int_knob` (the one
    shared implementation; see its docstring for the coercion and
    precedence rules). ``warning_prefix="builder_factory"`` keeps this
    caller's event names — ``builder_factory.bad_env_int`` /
    ``builder_factory.bad_settings_int`` — exactly what they were before.
    """
    from trellis.core.knobs import resolve_int_knob  # noqa: PLC0415

    return resolve_int_knob(
        env_var=env_var,
        setting_name=setting_name,
        default=default,
        settings=settings,
        warning_prefix="builder_factory",
    )


def _resolve_body_items(settings: Mapping[str, Any] | None) -> int:
    """Resolve the disclosure body-item cap: env > settings > default."""
    return _resolve_int_knob(
        env_var=BODY_ITEMS_ENV,
        setting_name=BODY_ITEMS_SETTING,
        default=DEFAULT_BODY_ITEMS,
        settings=settings,
    )


def _resolve_excerpt_max_chars(settings: Mapping[str, Any] | None) -> int:
    """Resolve the per-item excerpt character cap: env > settings > default."""
    return _resolve_int_knob(
        env_var=EXCERPT_MAX_CHARS_ENV,
        setting_name=EXCERPT_MAX_CHARS_SETTING,
        default=EXCERPT_MAX_CHARS,
        settings=settings,
    )


class AxisReport(TypedDict):
    """What :func:`describe_axes` returns. Serialised verbatim into JSON.

    ``embedder_error_type``/``embedder_setting`` are always present (never
    omitted) but only non-``None`` when ``semantic == "embedder_failed"`` —
    the one state whose note is dynamic rather than a fixed string from
    :data:`SEMANTIC_AXIS_NOTES` (Q5-A). ``graph`` is ``"ran"``, ``"failed"``,
    ``"unseeded_fallback_off"`` or ``"absent"`` (:func:`describe_axes`).
    """

    available: list[str]
    ran: list[str]
    failed: list[str]
    semantic: str
    graph: str
    embedder_error_type: str | None
    embedder_setting: str | None


def build_pack_builder(
    registry: StoreRegistry, *, surface: str, project: str | None = None
) -> PackBuilder:
    """Wire a :class:`PackBuilder` for ``registry``.

    Args:
        registry: The deployment's :class:`StoreRegistry`.
        surface: Which caller is building — ``"mcp"``, ``"api.retrieve"``,
            ``"cli.retrieve"``, ``"cli.analyze.pack-quality"``. It rides
            the advisory-store log line so an operator reading the journal
            can tell which surface saw what (#373).
        project: The project every ``PACK_ASSEMBLED`` payload is stamped
            with, or ``None``.
    """
    param_registry = ParameterRegistry(registry.operational.parameter_store)
    # One read, shared by every settings-aware knob resolved below —
    # ``load_settings_values`` degrades to ``{}`` rather than raising, so
    # no caller here needs its own try/except around this read.
    settings = load_settings_values(registry.stores_dir)
    excerpt_max_chars = _resolve_excerpt_max_chars(settings)
    strategies_result = build_strategies(
        registry,
        parameter_registry=param_registry,
        graph_seed_extractor=_build_seed_extractor(registry, settings=settings),
        excerpt_max_chars=excerpt_max_chars,
        graph_recency_fallback=_graph_recency_fallback_enabled(settings),
    )
    return PackBuilder(
        strategies=strategies_result.strategies,
        embedder_configured=strategies_result.embedder_configured,
        embedder_resolve_failure=strategies_result.embedder_resolve_failure,
        event_log=registry.operational.event_log,
        advisory_store=load_advisory_store(registry.stores_dir, surface=surface),
        reranker=build_reranker("rrf", parameter_registry=param_registry),
        # Same env > settings > default resolution as the knobs above, for
        # the disclosure body-item cap (DEFAULT_BODY_ITEMS) — see
        # ``SettingSpec(name="disclosure_body_items")``.
        disclosure=DisclosureConfig(body_items=_resolve_body_items(settings)),
        # F14 (#259): collapse near-duplicate pack items — the same fact
        # stored via save_memory AND via corpus ingestion surfaced both
        # copies in one pack. MinHash/LSH over item excerpts,
        # relevance-ordered so the highest-scoring copy wins. Default 0.85
        # Jaccard per the config's guidance table.
        semantic_dedup=SemanticDedupConfig(),
        project=project,
        # The pack-effect holdout (``TRELLIS_PACK_HOLDOUT_RATE``), read per
        # build like every surface's builder, so no surface escapes it.
        # ``env > settings > default`` (#settings-store PR 1/2): this is
        # a ``WriteBehaviourConfig`` field, so it goes through
        # ``from_env_and_settings`` rather than the hand-rolled resolution
        # ``graph_seeding`` / ``filter_legacy_advisories`` need below —
        # those two are deliberately kept out of ``WriteBehaviourConfig``
        # (see ``GRAPH_SEEDING_SETTING``'s docstring).
        holdout_rate=WriteBehaviourConfig.from_env_and_settings(
            settings=settings
        ).pack_holdout_rate,
        # Same env > settings > default resolution as above, for the
        # advisory-serving kill switch (#settings-store PR 2/2) — see
        # ``SettingSpec(name="filter_legacy_advisories")``.
        filter_legacy_advisories=_filter_legacy_advisories_enabled(settings=settings),
        # Default-off class prior for one document form (see
        # trellis.retrieve.class_prior) — resolved once, with settings,
        # same shape as the two knobs above. Every surface that goes
        # through this factory gets the gate wired in, even while every
        # shipped default keeps it a no-op.
        class_prior_config=resolve_class_prior_config(settings=settings),
        # Lets the collect seam resolve a chunk's parent so an
        # archived/noise verdict recorded against the parent reaches its
        # chunks (trellis.retrieve.parent_verdicts). Every surface that
        # goes through this factory gets the lookup wired in.
        document_store=registry.knowledge.document_store,
        # The same cut the keyword axis was built with, for the public-only
        # path, which re-reads each item's text from the stored document.
        excerpt_max_chars=excerpt_max_chars,
    )


def _build_seed_extractor(
    registry: StoreRegistry, *, settings: dict[str, Any] | None = None
) -> GraphSeedExtractor | None:
    """The graph axis's seed extractor, or ``None`` when disabled.

    This is the wiring #375 asks for, and it is the *only* one: the
    ``build_strategies`` default stays ``None`` so #371's refusal of
    :class:`~trellis.retrieve.semantic_seeds.SemanticSeedExtractor` — a
    different extractor, measured as a no-op — is untouched and its pinned
    tests keep passing. A surface that assembles a pack goes through
    :func:`build_pack_builder` and gets seeding; a caller constructing
    strategies directly opts in by hand.

    :class:`~trellis.retrieve.strategies.NamespaceSeedExtractor` needs no
    embedder and no entity-summary corpus, so unlike the refused wiring it
    has nothing to be silently unsatisfied by — its read of the store is
    indexed and its miss case is the recency window that ran before.
    """
    if not _graph_seeding_enabled(settings):
        logger.info("graph_seeding_disabled", env=GRAPH_SEEDING_ENV)
        return None
    return NamespaceSeedExtractor(registry.knowledge.graph_store)


def _graph_seeding_enabled(settings: dict[str, Any] | None = None) -> bool:
    """Read :data:`GRAPH_SEEDING_ENV`, env > settings > default.

    ``settings`` is the already-loaded ``name -> value`` mapping (e.g. from
    :func:`trellis.stores.settings_store.load_settings_values`); ``None``
    (the default, for any caller with no settings access) keeps the
    env-only behaviour this had before settings existed. An *unset* env var
    falls through to :data:`GRAPH_SEEDING_SETTING`; unset or non-bool there
    means on — the same default either knob shipped with. An *unrecognised*
    value does not fall through: it warns and reads as on, whatever the
    stored setting says.

    Read per build rather than at import, so a long-lived process picks up
    an operator's change between runs — the same posture
    ``_resolve_connectivity_check`` takes in :mod:`trellis.stores.registry`.
    """
    raw = os.environ.get(GRAPH_SEEDING_ENV, "").strip().lower()
    if not raw:
        value = (settings or {}).get(GRAPH_SEEDING_SETTING)
        return value if isinstance(value, bool) else True
    if raw in _FALSEY:
        return False
    if raw not in _TRUTHY:
        logger.warning(
            "graph_seeding_env_unrecognised",
            var=GRAPH_SEEDING_ENV,
            value=raw,
            effective=True,
        )
    return True


def _graph_recency_fallback_enabled(settings: Mapping[str, Any] | None = None) -> bool:
    """Read :data:`GRAPH_RECENCY_FALLBACK_ENV`, env > settings > default off.

    Mirrors :func:`_graph_seeding_enabled` with the opposite default: unset
    is **off**, and a value in neither spelling set warns and also reads as
    off, since an opt-in that a typo silently enables would defeat the
    default. A non-bool stored value is ignored, like there. An unrecognised
    env value does not fall through to settings either: a stored ``true`` is
    ignored and it reads off, so the typo cannot be read as an opt-in from
    either side.
    """
    raw = os.environ.get(GRAPH_RECENCY_FALLBACK_ENV, "").strip().lower()
    if not raw:
        value = (settings or {}).get(GRAPH_RECENCY_FALLBACK_SETTING)
        return value if isinstance(value, bool) else False
    if raw in _TRUTHY:
        return True
    if raw not in _FALSEY:
        logger.warning(
            "graph_recency_fallback_env_unrecognised",
            var=GRAPH_RECENCY_FALLBACK_ENV,
            value=raw,
            effective=False,
        )
    return False


def describe_axes(
    builder: PackBuilder,
    strategies_used: list[str],
    *,
    embedder_configured: bool,
    embedder_resolve_failure: EmbedderResolveFailure | None = None,
    graph_axis: str | None = None,
    public_only: bool = False,
) -> AxisReport:
    """Say which axes this deployment has, which ran, and which did not.

    A pack assembled without the semantic axis is a materially different
    pack, and :func:`build_strategies` drops that axis **silently** — it
    logs and continues, which is a no-op under the CLI's ``WARNING``
    default and invisible to whoever is reading the output. A surface that
    reported the result without reporting the gap would reproduce #410 one
    layer up: an answer presented as the whole answer.

    The four outcomes are kept apart because they call for different
    fixes, the same posture ``capture_coverage``'s ``state`` field takes:

    * **configured and ran** — nothing to say.
    * **not configured** — no ``embeddings`` provider and no
      ``TRELLIS_EMBEDDING_FN``; the deployment cannot run this axis at all.
    * **configured but absent** — an embedder resolved and the axis is
      still missing, which means the vector backend failed to initialise
      and ``build_strategies`` swallowed it.
    * **embedder failed to resolve** — ``registry.embedding_fn`` itself
      raised (Q5-A): a different failure from the one above, which
      needs the operator to fix the *embedder* setting rather than the
      vector backend. ``embedder_resolve_failure`` carries the error type
      and setting name (never the exception message — describe, don't
      quote) and takes precedence over ``embedder_configured`` here,
      since an embedder that failed to resolve was never "configured" in
      the sense that word means for the other three states.
    * **available but did not run** — the strategy raised during *this*
      build; ``PACK_ASSEMBLED.strategy_failures`` carries the exception.

    The graph axis has one more outcome of its own, ``graph``: it ran and
    served nothing because no seed resolved and the recency window is off
    (``"unseeded_fallback_off"``, #961). It is in ``ran`` — the axis did
    not fail — so without this field a pack with no graph items cannot
    tell a withheld axis from an empty graph. It is not rendered as a
    markdown note: it follows from what the intent named, not from a fault
    in the deployment, so a line on every such pack would be one that
    always prints and always gets skipped (see :data:`SEMANTIC_AXIS_NOTES`).
    A builder with no graph strategy at all reads ``"absent"`` (#985): not
    ``ran``, which would pass "never configured" for "ran and found nothing",
    and not ``failed``, which names only an axis the builder has. It gets no
    markdown note either, because no shipped surface builds one.

    Args:
        builder: The :class:`PackBuilder` this pack was assembled from.
        strategies_used: Names of the axes that actually ran this build
            (``RetrievalReport.strategies_used``).
        embedder_configured: Whether an embedder resolved for ``builder``.
            Read ``builder.embedder_configured`` — never re-read
            ``registry.embedding_fn``, which re-raises on every access
            when misconfigured.
        embedder_resolve_failure: ``builder.embedder_resolve_failure``, or
            ``None`` for a builder this never applies to (e.g. a test
            double). ``None`` is also the correct value whenever
            ``embedder_configured`` is accurate on its own.
        graph_axis: The pack's ``retrieval_report.graph_axis``. Every
            caller passes it; ``None`` means the axis was not withheld.
        public_only: ``True`` for a public-only caller (an ``external`` key).
            Its pack never runs the graph axis, and reporting that axis
            (``unseeded_fallback_off`` says whether the intent named a seed
            the graph holds) would describe the graph it may not read, so the
            graph reads ``"absent"`` and is neither ``available`` nor
            ``failed``. Every other caller's report is unchanged.

    Returns a mapping, not prose: the caller renders it (or serialises it
    verbatim into ``--format json``).
    """
    available = list(builder.strategy_names)
    if public_only:
        available = [name for name in available if name != GRAPH_STRATEGY_NAME]
    ran = list(strategies_used)
    failed = [name for name in available if name not in ran]
    embedder_error_type: str | None = None
    embedder_setting: str | None = None
    semantic_state = "ran"
    graph_state = resolve_graph_axis_state(available, ran, graph_axis)
    if SEMANTIC_AXIS not in available:
        if embedder_resolve_failure is not None:
            semantic_state = "embedder_failed"
            embedder_error_type = embedder_resolve_failure.error_type
            embedder_setting = embedder_resolve_failure.setting
        else:
            semantic_state = (
                "misconfigured" if embedder_configured else "not_configured"
            )
    elif SEMANTIC_AXIS in failed:
        semantic_state = "failed"
    return {
        "available": available,
        "ran": ran,
        "failed": failed,
        "semantic": semantic_state,
        "graph": graph_state,
        "embedder_error_type": embedder_error_type,
        "embedder_setting": embedder_setting,
    }


#: Human sentence per :func:`describe_axes` ``semantic`` state. Empty for
#: the healthy state — a warning that always prints is one that always gets
#: skipped, the same rule ``retrieval_availability_note`` (#365) follows.
#:
#: No ``"embedder_failed"`` entry: that state's note names the actual error
#: type and setting (Q5-A), which this table — one fixed string per
#: state — cannot hold. Render it with :func:`format_embedder_failed_note`
#: instead, the same way ``"misconfigured"``'s *dynamic* MCP rendering uses
#: :func:`format_misconfigured_semantic_note` beside this table's static
#: entry for the REST/JSON surfaces that only need the stable string.
SEMANTIC_AXIS_NOTES: dict[str, str] = {
    "ran": "",
    "not_configured": (
        "Semantic axis unavailable: no embeddings provider is configured"
        " (config.yaml 'embeddings:' or TRELLIS_EMBEDDING_FN), so this pack"
        " is keyword + graph only and is NOT what an agent with embeddings"
        " would be served."
    ),
    "misconfigured": (
        "Semantic axis unavailable: an embedder is configured but the vector"
        " backend did not initialise, so this pack has no semantic results."
        " Re-run with TRELLIS_LOG_LEVEL=WARNING to see the backend error."
    ),
    "failed": (
        "Semantic axis failed during this build; the pack was assembled from"
        " the surviving axes. See PACK_ASSEMBLED.strategy_failures."
    ),
}

# ``format_failed_axes_note``, ``format_misconfigured_semantic_note`` and
# ``format_embedder_failed_note`` take no ``PackBuilder``, so they live in
# :mod:`trellis_wire.axes`, where the HTTP-only SDK can import them, and are
# re-exported here for MCP. ``tests/unit/wire/test_axes.py`` pins the
# identity.


__all__ = [
    "GRAPH_RECENCY_FALLBACK_ENV",
    "GRAPH_SEEDING_ENV",
    "SEMANTIC_AXIS",
    "SEMANTIC_AXIS_NOTES",
    "AxisReport",
    "build_pack_builder",
    "describe_axes",
    "format_embedder_failed_note",
    "format_failed_axes_note",
    "format_misconfigured_semantic_note",
]
