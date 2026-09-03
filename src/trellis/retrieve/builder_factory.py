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

from typing import TYPE_CHECKING, TypedDict

from trellis.ops import ParameterRegistry
from trellis.retrieve.pack_builder import PackBuilder, SemanticDedupConfig
from trellis.retrieve.rerankers import build_reranker
from trellis.retrieve.strategies import build_strategies
from trellis.stores.advisory_source import load_advisory_store

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

#: Name of the axis that only exists when the deployment has an embedder.
#: :func:`~trellis.retrieve.strategies.build_strategies` appends
#: :class:`~trellis.retrieve.strategies.SemanticSearch` only when an
#: ``embedding_fn`` resolves, and swallows a vector-backend init failure —
#: so "absent" covers two different situations and a caller reporting the
#: gap must not collapse them. See :func:`describe_axes`.
SEMANTIC_AXIS = "semantic"


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
        strategies=build_strategies(registry, parameter_registry=param_registry),
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
    "SEMANTIC_AXIS",
    "SEMANTIC_AXIS_NOTES",
    "AxisReport",
    "build_pack_builder",
    "describe_axes",
]
