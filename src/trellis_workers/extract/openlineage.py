"""OpenLineage event extractor.

Reference implementation of the :class:`~trellis.extract.base.Extractor`
Protocol for a list of OpenLineage events.  The extractor is pure:
callers parse the file (JSON array or NDJSON) and hand the resulting
list to :meth:`extract`.

A dataset is keyed ``dataset:{namespace}:{name}``, both verbatim: storage paths
and most systems' names are case-sensitive, so nothing is folded.  The exception
is a dataset that names a Unity Catalog table, which is keyed as
:class:`~trellis_workers.extract.unity_catalog.UnityCatalogExtractor` keys that
table, so lineage from a job and metadata from the catalog meet on one node.
See :class:`OpenLineageExtractor`.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any, ClassVar

import structlog

from trellis.extract.base import ExtractorTier
from trellis.schemas import well_known as wk
from trellis.schemas.extraction import (
    AliasDraft,
    EdgeDraft,
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)
from trellis_workers.extract.unity_catalog_identity import (
    DEFAULT_SCOPE,
    SYMLINK_NAMESPACE,
    UC_TABLE_ENTITY_TYPE,
    dataset_entity_id,
    identity_key,
    is_unity_catalog_namespace,
    normalize_scope,
    split_full_name,
    table_entity_id,
    table_properties,
)

if TYPE_CHECKING:
    from trellis.extract.context import ExtractionContext

#: Jobs and datasets live under separate ``source_system`` values so a job and
#: a dataset that share a ``namespace/name`` never contend for one alias.
OPENLINEAGE_JOB_ALIAS_SOURCE_SYSTEM = "openlineage_job"
OPENLINEAGE_DATASET_ALIAS_SOURCE_SYSTEM = "openlineage_dataset"

logger = structlog.get_logger(__name__)


#: Reasons counted in ``skipped_rows`` when a dataset names a Unity Catalog table
#: in a form this extractor cannot read.  The dataset is not dropped: it keeps
#: its storage-path node.
SKIP_UC_PRIMARY_UNPARSED = "uc_primary_unparsed"
SKIP_UC_SYMLINK_UNPARSED = "uc_symlink_unparsed"


def _job_id(namespace: str, name: str) -> str:
    return f"job:{namespace}:{name}"


def _routing_from_namespace(namespace: str, name: str) -> dict[str, str]:
    """Derive cross-database routing properties from an OL dataset id.

    OpenLineage namespaces follow https://github.com/OpenLineage/OpenLineage/
    blob/main/spec/Naming.md — typical shapes are ``<scheme>://<host>``
    (full URI) or a bare system identifier. We extract the scheme as
    ``source_system`` when present; otherwise the bare namespace acts as
    the system identifier. ``physical_uri`` joins namespace and dataset
    name with ``/`` so consumers have a single canonical locator.
    The dataset name itself is not parsed into ``database_name`` /
    ``schema_name`` here — OL dataset names are dotted strings whose
    structure varies by adapter; deterministic extractors should not
    invent that mapping. A more sophisticated downstream extractor (e.g.,
    Unity Catalog) can backfill the parts.
    """
    routing: dict[str, str] = {}
    if "://" in namespace:
        scheme, _, _rest = namespace.partition("://")
        if scheme:
            routing[wk.DATASET_PROP_SOURCE_SYSTEM] = scheme
    elif namespace:
        routing[wk.DATASET_PROP_SOURCE_SYSTEM] = namespace
    if namespace and name:
        sep = "/" if "://" in namespace else ":"
        routing[wk.DATASET_PROP_PHYSICAL_URI] = f"{namespace}{sep}{name}"
    return routing


def _symlink_identifiers(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``facets.symlinks.identifiers`` entries that are objects, else ``[]``."""
    facets = dataset.get("facets")
    symlinks = facets.get("symlinks") if isinstance(facets, dict) else None
    identifiers = symlinks.get("identifiers") if isinstance(symlinks, dict) else None
    if not isinstance(identifiers, list):
        return []
    return [ident for ident in identifiers if isinstance(ident, dict)]


def _unity_catalog_candidates(
    dataset: dict[str, Any], symlink_scope: str
) -> list[tuple[str, str, str]]:
    """Each place ``dataset`` names a Unity Catalog table, best first.

    ``(scope, name, skip_reason)`` per place.  Three forms count:

    * the dataset's own namespace is ``unitycatalog://<host>`` (the OpenLineage
      naming spec's form), so the scope is that namespace;
    * a ``symlinks`` identifier has a ``unitycatalog://`` namespace, likewise;
    * a ``symlinks`` identifier of type ``TABLE`` has the fixed namespace
      ``unity-catalog`` (openlineage-spark 1.51.0 and later on Databricks, whose
      own namespace and name are then the storage location).  That namespace
      has no host, so the scope is ``symlink_scope``.
    """
    found: list[tuple[str, str, str]] = []
    namespace, name = dataset.get("namespace"), dataset.get("name")
    scope = (
        normalize_scope(namespace) if is_unity_catalog_namespace(namespace) else None
    )
    if scope and isinstance(name, str):
        found.append((scope, name, SKIP_UC_PRIMARY_UNPARSED))
    for ident in _symlink_identifiers(dataset):
        ident_ns, ident_name = ident.get("namespace"), ident.get("name")
        if not isinstance(ident_ns, str) or not isinstance(ident_name, str):
            continue
        if is_unity_catalog_namespace(ident_ns):
            ident_scope = normalize_scope(ident_ns)
            if ident_scope:
                found.append((ident_scope, ident_name, SKIP_UC_SYMLINK_UNPARSED))
        elif (
            ident_ns.strip().lower() == SYMLINK_NAMESPACE
            and str(ident.get("type", "")).strip().upper() == "TABLE"
        ):
            found.append((symlink_scope, ident_name, SKIP_UC_SYMLINK_UNPARSED))
    return found


def _ensure_dataset(
    dataset: dict[str, Any],
    seen: dict[str, EntityDraft],
    *,
    symlink_scope: str,
    skipped: Counter[str],
) -> str | None:
    """Register a dataset entity if not already seen.  Returns its id."""
    ds_ns = dataset.get("namespace", "")
    ds_name = dataset.get("name", "")
    if not ds_ns or not ds_name:
        return None
    storage_alias = AliasDraft(
        source_system=OPENLINEAGE_DATASET_ALIAS_SOURCE_SYSTEM,
        raw_id=f"{ds_ns}/{ds_name}",
        raw_name=ds_name,
    )

    for scope, uc_name, reason in _unity_catalog_candidates(dataset, symlink_scope):
        parts = split_full_name(uc_name, 3)
        if parts is None:
            skipped[reason] += 1
            continue
        table = (parts[0], parts[1], parts[2])
        did = table_entity_id(scope, identity_key(*table))
        if did not in seen:
            # A placeholder: ``if_absent`` leaves a table the Unity Catalog
            # extractor already wrote alone, and its full record replaces this
            # one.  The dataset's own facets are not carried: they describe the
            # storage object, the catalog owns the table's properties, and a
            # facet written here would be replaced by the next catalog run and
            # kept by none.  The storage identity survives as an alias.
            stub_props = table_properties(table, scope, name=table[2], stub=True)
            seen[did] = EntityDraft(
                entity_id=did,
                entity_type=UC_TABLE_ENTITY_TYPE,
                name=table[2],
                properties=stub_props,
                if_absent=True,
                aliases=[storage_alias],
            )
        elif all(
            (a.source_system, a.raw_id)
            != (storage_alias.source_system, storage_alias.raw_id)
            for a in seen[did].aliases
        ):
            seen[did].aliases.append(storage_alias)
        return did

    did = dataset_entity_id(ds_ns, ds_name)
    if did not in seen:
        props: dict[str, Any] = {
            "namespace": ds_ns,
            "name": ds_name,
        }
        props.update(_routing_from_namespace(ds_ns, ds_name))
        facets = dataset.get("facets") or {}
        if facets:
            props["facets"] = facets
        seen[did] = EntityDraft(
            entity_id=did,
            entity_type="dataset",
            name=ds_name,
            properties=props,
            aliases=[storage_alias],
        )
    return did


class OpenLineageExtractor:
    """Deterministic extractor for OpenLineage event streams.

    Accepts a list of OpenLineage event dicts.  Produces job and dataset
    entities plus ``reads_from`` / ``writes_to`` edges, deduplicated by
    ``(source_id, target_id, edge_kind)``.

    **Unity Catalog tables.**  A dataset keeps its verbatim
    ``dataset:{namespace}:{name}`` id unless it names a Unity Catalog table in
    one of three forms (see :func:`_unity_catalog_candidates`): a
    ``unitycatalog://<host>`` namespace of its own, a ``symlinks`` identifier in
    that namespace, or a ``symlinks`` identifier of type ``TABLE`` in the fixed
    namespace ``unity-catalog``.  The last is what openlineage-spark 1.51.0 and
    later emits on Databricks, where the dataset's own namespace and name are
    the *storage location* (``s3://bucket`` and ``/path``) and only the symlink
    names the table.  Older versions put a storage-derived namespace on the
    symlink, which is not recognised: those datasets keep their storage-path ids.

    Such a dataset gets the id
    :class:`~trellis_workers.extract.unity_catalog.UnityCatalogExtractor` gives
    the table, built by the same helper, so the lineage edges land on the node
    the catalog metadata does.  The entity is a name-only placeholder (``stub``,
    ``if_absent``): it is created when missing and a table the catalog extractor
    already wrote is left alone, and the catalog's full record replaces it
    whichever ran first.  The dataset's facets are not carried (the catalog owns
    the table's properties).  With ``TRELLIS_KNOWLEDGE_LINK_OR_MINT`` on, the
    storage identity ``{namespace}/{name}`` is bound as an ``openlineage_dataset``
    alias of the table, on an existing table too.  A name that is not exactly
    ``catalog.schema.table`` is counted in ``skipped_rows``
    (``uc_symlink_unparsed``, ``uc_primary_unparsed``) and the dataset keeps its
    storage-path node.  Nothing else is changed: storage paths are
    case-sensitive and are never folded.

    Args:
        unity_catalog_scope: The scope of a table named by a ``unity-catalog``
            symlink, which carries no host.  It must equal the Unity Catalog
            extractor's ``namespace`` (default: unset, which is its default
            scope), or the two mint different ids.  A ``unitycatalog://`` scope is
            normalised as the Unity Catalog extractor normalises its own.  A
            ``unitycatalog://`` namespace on the dataset or a symlink carries its
            own scope and ignores this one.
    """

    name = "openlineage"
    tier = ExtractorTier.DETERMINISTIC
    supported_sources: ClassVar[list[str]] = ["openlineage"]
    version = "0.2.0"

    def __init__(self, *, unity_catalog_scope: str | None = None) -> None:
        self._symlink_scope = normalize_scope(unity_catalog_scope) or DEFAULT_SCOPE

    async def extract(
        self,
        raw_input: Any,
        *,
        source_hint: str | None = None,
        context: ExtractionContext | None = None,
    ) -> ExtractionResult:
        del context  # deterministic — no cost budget

        if not isinstance(raw_input, list):
            msg = (
                "OpenLineageExtractor expects a list of event dicts; "
                f"got {type(raw_input).__name__}"
            )
            raise TypeError(msg)

        seen: dict[str, EntityDraft] = {}
        raw_edges: list[EdgeDraft] = []
        skipped: Counter[str] = Counter()

        for event in raw_input:
            if not isinstance(event, dict):
                continue
            job_info = event.get("job") or {}
            job_ns = job_info.get("namespace", "")
            job_name = job_info.get("name", "")
            if not job_ns or not job_name:
                continue

            jid = _job_id(job_ns, job_name)
            if jid not in seen:
                seen[jid] = EntityDraft(
                    entity_id=jid,
                    entity_type="job",
                    name=job_name,
                    properties={
                        "namespace": job_ns,
                        "name": job_name,
                    },
                    aliases=[
                        AliasDraft(
                            source_system=OPENLINEAGE_JOB_ALIAS_SOURCE_SYSTEM,
                            raw_id=f"{job_ns}/{job_name}",
                            raw_name=job_name,
                        )
                    ],
                )

            # OpenLineage events stream in chunks: a job emitted in one
            # batch may reference a dataset already extracted in a prior
            # batch (or vice versa).  Mark edges allow_dangling so the FK
            # pre-flight in LinkCreateHandler doesn't reject cross-batch
            # references.  Within a single batch _ensure_dataset still
            # registers the entity, so the strict path is also satisfied.
            for inp in event.get("inputs", []) or []:
                did = _ensure_dataset(
                    inp, seen, symlink_scope=self._symlink_scope, skipped=skipped
                )
                if did:
                    raw_edges.append(
                        EdgeDraft(
                            source_id=jid,
                            target_id=did,
                            edge_kind="reads_from",
                            allow_dangling=True,
                        )
                    )

            for out in event.get("outputs", []) or []:
                did = _ensure_dataset(
                    out, seen, symlink_scope=self._symlink_scope, skipped=skipped
                )
                if did:
                    raw_edges.append(
                        EdgeDraft(
                            source_id=jid,
                            target_id=did,
                            edge_kind="writes_to",
                            allow_dangling=True,
                        )
                    )

        # Deduplicate edges by (source, target, kind).
        unique_edges: list[EdgeDraft] = []
        seen_edge_keys: set[tuple[str, str, str]] = set()
        for edge in raw_edges:
            key = (edge.source_id, edge.target_id, edge.edge_kind)
            if key not in seen_edge_keys:
                seen_edge_keys.add(key)
                unique_edges.append(edge)

        entities = list(seen.values())
        skipped_rows = dict(sorted(skipped.items()))
        if skipped_rows:
            logger.info("openlineage_rows_skipped", **skipped_rows)
        logger.info(
            "openlineage_extracted",
            entities=len(entities),
            edges=len(unique_edges),
            source_hint=source_hint,
        )

        return ExtractionResult(
            entities=entities,
            edges=unique_edges,
            extractor_used=self.name,
            tier=self.tier.value,
            skipped_rows=skipped_rows,
            provenance=ExtractionProvenance(
                extractor_name=self.name,
                extractor_version=self.version,
                source_hint=source_hint,
            ),
        )
