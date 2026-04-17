"""Reference extractors for common external sources.

These domain-specific extractors live outside ``trellis.extract`` (the
generic core) and demonstrate how consumers can plug their own sources
into the tiered extraction pipeline.  Each extractor implements the
:class:`~trellis.extract.base.Extractor` Protocol and is pure — it takes
already-parsed input and returns an :class:`ExtractionResult` with no
I/O or store side-effects.  The caller (CLI / API / worker) owns file
loading and command submission through
:class:`~trellis.mutate.executor.MutationExecutor`.
"""

from trellis_workers.extract.dbt_manifest import DbtManifestExtractor
from trellis_workers.extract.openlineage import OpenLineageExtractor

__all__ = [
    "DbtManifestExtractor",
    "OpenLineageExtractor",
]
