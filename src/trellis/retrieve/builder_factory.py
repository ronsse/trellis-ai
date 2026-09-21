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
from typing import TYPE_CHECKING, TypedDict

import structlog

from trellis.ops import ParameterRegistry
from trellis.retrieve.pack_builder import PackBuilder, SemanticDedupConfig
from trellis.retrieve.rerankers import build_reranker
from trellis.retrieve.strategies import NamespaceSeedExtractor, build_strategies
from trellis.stores.advisory_source import load_advisory_store

if TYPE_CHECKING:
    from trellis.retrieve.strategies import GraphSeedExtractor
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
GRAPH_SEEDING_ENV = "TRELLIS_GRAPH_SEEDING"

#: Accepted spellings, matching :mod:`trellis.stores.registry`. A value in
#: neither set is a typo, and a typo'd kill switch that silently keeps the
#: default is how an operator ends up believing they disabled something
#: they did not — so it warns rather than going quiet.
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off"})


class AxisReport(TypedDict):
    """What :func:`describe_axes` returns. Serialised verbatim into JSON."""

    available: list[str]
    ran: list[str]
    failed: list[str]
    semantic: str


def build_pack_builder(registry: StoreRegistry, *, surface: str) -> PackBuilder:
    """Wire a :class:`PackBuilder` for ``registry``.

    Args:
        registry: The deployment's :class:`StoreRegistry`.
        surface: Which caller is building — ``"mcp"``, ``"api.retrieve"``,
            ``"cli.retrieve"``, ``"cli.analyze.pack-quality"``. It rides
            the advisory-store log line so an operator reading the journal
            can tell which surface saw what (#373).
    """
    param_registry = ParameterRegistry(registry.operational.parameter_store)
    return PackBuilder(
        strategies=build_strategies(
            registry,
            parameter_registry=param_registry,
            graph_seed_extractor=_build_seed_extractor(registry),
        ),
        event_log=registry.operational.event_log,
        advisory_store=load_advisory_store(registry.stores_dir, surface=surface),
        reranker=build_reranker("rrf", parameter_registry=param_registry),
        # F14 (#259): collapse near-duplicate pack items — the same fact
        # stored via save_memory AND via corpus ingestion surfaced both
        # copies in one pack. MinHash/LSH over item excerpts,
        # relevance-ordered so the highest-scoring copy wins. Default 0.85
        # Jaccard per the config's guidance table.
        semantic_dedup=SemanticDedupConfig(),
    )


def _build_seed_extractor(registry: StoreRegistry) -> GraphSeedExtractor | None:
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
    if not _graph_seeding_enabled():
        logger.info("graph_seeding_disabled", env=GRAPH_SEEDING_ENV)
        return None
    return NamespaceSeedExtractor(registry.knowledge.graph_store)


def _graph_seeding_enabled() -> bool:
    """Read :data:`GRAPH_SEEDING_ENV`; unset or unrecognised means on.

    Read per build rather than at import, so a long-lived process picks up
    an operator's change between runs — the same posture
    ``_resolve_connectivity_check`` takes in :mod:`trellis.stores.registry`.
    """
    raw = os.environ.get(GRAPH_SEEDING_ENV, "").strip().lower()
    if not raw:
        return True
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


def describe_axes(
    builder: PackBuilder,
    strategies_used: list[str],
    *,
    embedder_configured: bool,
) -> AxisReport:
    """Say which axes this deployment has, which ran, and which did not.

    A pack assembled without the semantic axis is a materially different
    pack, and :func:`build_strategies` drops that axis **silently** — it
    logs and continues, which is a no-op under the CLI's ``WARNING``
    default and invisible to whoever is reading the output. A surface that
    reported the result without reporting the gap would reproduce #410 one
    layer up: an answer presented as the whole answer.

    The three outcomes are kept apart because they call for different
    fixes, the same posture ``capture_coverage``'s ``state`` field takes:

    * **configured and ran** — nothing to say.
    * **not configured** — no ``embeddings`` provider and no
      ``TRELLIS_EMBEDDING_FN``; the deployment cannot run this axis at all.
    * **configured but absent** — an embedder resolved and the axis is
      still missing, which means the vector backend failed to initialise
      and ``build_strategies`` swallowed it.
    * **available but did not run** — the strategy raised during *this*
      build; ``PACK_ASSEMBLED.strategy_failures`` carries the exception.

    Returns a mapping, not prose: the caller renders it (or serialises it
    verbatim into ``--format json``).
    """
    available = list(builder.strategy_names)
    ran = list(strategies_used)
    failed = [name for name in available if name not in ran]
    semantic_state = "ran"
    if SEMANTIC_AXIS not in available:
        semantic_state = "misconfigured" if embedder_configured else "not_configured"
    elif SEMANTIC_AXIS in failed:
        semantic_state = "failed"
    return {
        "available": available,
        "ran": ran,
        "failed": failed,
        "semantic": semantic_state,
    }


#: Human sentence per :func:`describe_axes` ``semantic`` state. Empty for
#: the healthy state — a warning that always prints is one that always gets
#: skipped, the same rule ``retrieval_availability_note`` (#365) follows.
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
        " backend did not initialise, so this pack is keyword + graph only."
        " Re-run with TRELLIS_LOG_LEVEL=WARNING to see the backend error."
    ),
    "failed": (
        "Semantic axis failed during this build; the pack was assembled from"
        " the surviving axes. See PACK_ASSEMBLED.strategy_failures."
    ),
}


__all__ = [
    "GRAPH_SEEDING_ENV",
    "SEMANTIC_AXIS",
    "SEMANTIC_AXIS_NOTES",
    "AxisReport",
    "build_pack_builder",
    "describe_axes",
]
