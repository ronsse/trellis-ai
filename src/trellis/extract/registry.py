"""ExtractorRegistry — in-memory registry of available extractors.

Extractors are registered at runtime by the consumer.  Core ships a very
small set (just :class:`JSONRulesExtractor`); consumer packages register
their own (dbt, OpenLineage, UC, etc.) at process startup.

An optional entry-point loader (``trellis.extractors``) lets installed
packages advertise extractors declaratively for zero-config discovery.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from trellis.extract.base import Extractor, ExtractorTier

logger = structlog.get_logger(__name__)


class ExtractorRegistry:
    """Holds registered :class:`Extractor` instances.

    Lookup is by ``source_hint``: the first extractor whose
    ``supported_sources`` contains the hint wins.  Within a single
    ``source_hint``, routing priority is controlled at the dispatcher
    level (deterministic > hybrid > llm).
    """

    def __init__(self) -> None:
        self._by_name: dict[str, Extractor] = {}
        # Ordered per source_hint so registration order is preserved when
        # multiple extractors target the same hint at the same tier.
        self._by_source: dict[str, list[Extractor]] = {}

    def register(self, extractor: Extractor) -> None:
        """Register an extractor.  Later registrations override same-name."""
        if extractor.name in self._by_name:
            logger.info(
                "extractor_re_registered",
                name=extractor.name,
                tier=extractor.tier.value,
            )
        self._by_name[extractor.name] = extractor
        for source in extractor.supported_sources:
            bucket = self._by_source.setdefault(source, [])
            # De-dup by name so a re-register moves the extractor to the end
            # of the bucket (most recently registered last).
            self._by_source[source] = [e for e in bucket if e.name != extractor.name]
            self._by_source[source].append(extractor)

    def get(self, name: str) -> Extractor | None:
        """Return an extractor by registry name."""
        return self._by_name.get(name)

    def candidates_for(
        self,
        source_hint: str | None,
    ) -> list[Extractor]:
        """Return all extractors matching ``source_hint`` (may be empty).

        Returns every registered extractor when ``source_hint`` is ``None``
        so the dispatcher can apply tier-preference routing without a hint.
        """
        if source_hint is None:
            return list(self._by_name.values())
        return list(self._by_source.get(source_hint, []))

    def names(self) -> list[str]:
        """All registered extractor names (insertion order)."""
        return list(self._by_name.keys())

    def by_tier(self, tier: ExtractorTier) -> list[Extractor]:
        """All registered extractors at the given tier."""
        return [e for e in self._by_name.values() if e.tier == tier]

    def load_entry_points(self, group: str = "trellis.extractors") -> int:
        """Load extractors advertised via Python entry points.

        Each entry point must resolve to either an :class:`Extractor`
        instance or a zero-arg callable returning one.  Returns the number
        of extractors successfully loaded.  Failures are logged but do not
        raise — a broken third-party package should not take down the
        registry.
        """
        loaded = 0
        for ep in entry_points(group=group):
            try:
                obj = ep.load()
                extractor = obj() if callable(obj) else obj
                self.register(extractor)
            except Exception:
                logger.exception("extractor_entry_point_load_failed", name=ep.name)
                continue
            else:
                loaded += 1
        return loaded
