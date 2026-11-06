"""Unity Catalog extractor (deterministic tier).

Turns a JSON export of Databricks Unity Catalog metadata and lineage into
catalog, schema, table and column entities with ``contains`` edges down the
hierarchy and ``depends_on`` edges for declared lineage.  The extractor is
pure: it never calls Databricks and never touches a store.  A team produces
the export with the REST API or the CLI, a caller parses the JSON, and the
resulting drafts go through :class:`~trellis.mutate.executor.MutationExecutor`
like every other extractor's.  Lineage comes only from rows the platform
recorded; nothing is inferred from names.

Accepted input, parsed (a dict, or a list of any mix of these):

* the list responses of ``GET /api/2.1/unity-catalog/{catalogs,schemas,tables}``,
  either as the wrapper object (``{"tables": [...], "next_page_token": ...}``)
  or as a bare list of records;
* rows of ``system.access.table_lineage`` / ``system.access.column_lineage``
  as a list of objects (a row with ``source_column_name`` and
  ``target_column_name`` is column lineage);
* a ``GET /api/2.0/lineage-tracking/table-lineage`` response
  (``upstreams[]`` / ``downstreams[]`` with ``tableInfo``).  That response does
  not name the table it was requested for, so the export must add
  ``"table_name": "<catalog>.<schema>.<table>"`` beside the two lists.

An object the export names but does not hold is a placeholder.  A lineage row
can name a table or column the export's metadata lacks, and a table, schema or
column always hangs from a schema and a catalog that a partial export (tables
only, lineage only) may not hold either.  Every such object gets a name-only
draft with ``if_absent=True`` and ``stub=True`` on the same id the real record
would have, so every edge joins two nodes of the batch and the Bolt stores,
which refuse a dangling edge, take it.  ``if_absent`` makes the write a
create-if-absent: the node is created when missing and left alone when it
exists, so a partial run never replaces what a fuller run wrote.  A record the
export does hold is a full create, which replaces the node's properties and so
drops ``stub``: the metadata that arrives after a lineage-only run lands whole.

A wrapper may also carry ``"namespace"``, the OpenLineage dataset namespace for
the workspace (``unitycatalog://<host>``).  It is normalised by
:func:`~trellis_workers.extract.unity_catalog_identity.normalize_scope`: the
scheme and host are lowercased and a trailing ``/`` and a ``:443`` port are
dropped.  The namespace is part of every entity id, so choose it before the first
run.

How a table meets its OpenLineage twin depends on what the OpenLineage producer
emits.  A table's id is ``dataset:{scope}:{catalog.schema.table}``, and
:class:`~trellis_workers.extract.openlineage.OpenLineageExtractor` mints the same
id for a dataset with a Unity Catalog identity, built by the same helper
(:mod:`~trellis_workers.extract.unity_catalog_identity`):

* openlineage-spark 1.51.0 and later on Databricks emits the *storage location*
  as the dataset's own namespace and name and names the table only in a
  ``symlinks`` identifier in the fixed namespace ``unity-catalog``, which has no
  host.  Leave this extractor's namespace unset: the scope is then the fixed
  string ``unity_catalog``, which is what the OpenLineage extractor uses for that
  symlink by default.  A namespace set here is also set there with
  ``OpenLineageExtractor(unity_catalog_scope=...)``, or the two ids differ.
* A producer that emits the table as ``unitycatalog://<host>`` plus
  ``catalog.schema.table`` (the OpenLineage naming spec's form) is read by scope
  and name alone, so set this extractor's namespace to that
  ``unitycatalog://<host>``.

Older OpenLineage nodes keep their storage-path ids; nothing here migrates them.

A table carries ``column_names``, its column names in position order, so an
agent can find a table by a column with the ``contains`` operator
(``adr-source-modeling-discipline.md`` §2.2).  Two bounds keep a very wide table
from becoming thousands of nodes: at most ``max_columns_per_table`` column
entities are minted from a table's own column records (default
:data:`DEFAULT_MAX_COLUMNS_PER_TABLE`), and ``column_names`` holds at most the
larger of that and :data:`MAX_COLUMN_NAMES` names.  ``column_count`` is always
the full count, and every column left out is counted in the result's
``skipped_rows``, never dropped silently.  A column that column lineage
references is always minted, because its edge needs it.

Rows the extractor reads and does not use (a lineage row without two tables,
and the like) are counted by reason in :attr:`ExtractionResult.skipped_rows`.

Unrecognised or malformed input raises a typed
:class:`~trellis.errors.ValidationError` (code ``unity_catalog_input``) that
names a position, never the content.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Final, NoReturn

import structlog

from trellis.errors import ValidationError
from trellis.extract.base import ExtractorTier
from trellis.schemas import well_known as wk
from trellis.schemas.enums import NodeRole
from trellis.schemas.extraction import (
    AliasDraft,
    EdgeDraft,
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)
from trellis_workers.extract.openlineage import (
    OPENLINEAGE_DATASET_ALIAS_SOURCE_SYSTEM,
)
from trellis_workers.extract.unity_catalog_identity import (
    DEFAULT_SCOPE,
    UC_TABLE_ENTITY_TYPE,
    identity_key,
    normalize_scope,
    split_full_name,
    table_entity_id,
    table_properties,
)

if TYPE_CHECKING:
    from trellis.extract.context import ExtractionContext

logger = structlog.get_logger(__name__)

UC_CATALOG_ENTITY_TYPE: Final = "uc_catalog"
UC_SCHEMA_ENTITY_TYPE: Final = "uc_schema"
UC_COLUMN_ENTITY_TYPE: Final = "uc_column"

#: Edge kinds already in use: ``depends_on`` is dbt's "derived from", ``contains``
#: is the parent-to-child kind the source-modeling cookbook names.
EDGE_CONTAINS: Final = "contains"
EDGE_DEPENDS_ON: Final = "depends_on"

#: Typed refusal code for input the extractor cannot read.
INPUT_REFUSAL_CODE: Final = "unity_catalog_input"

#: Catalogs left out unless ``include_system`` is set.
SYSTEM_CATALOGS: Final[frozenset[str]] = frozenset({"system", "__databricks_internal"})

_WRAPPER_KEYS: Final = ("catalogs", "schemas", "tables", "upstreams", "downstreams")

#: Fields a table or a column record carries and a catalog record does not.  A
#: bare record holding one of them is not a catalog, whatever else it has.
_NOT_A_CATALOG_KEYS: Final = frozenset(
    {
        "catalog_name",
        "schema_name",
        "table_type",
        "data_source_format",
        "columns",
        "type_text",
        "type_name",
        "position",
        "nullable",
    }
)
_IGNORED_DOCUMENT_KEYS: Final = frozenset(
    {"next_page_token", "namespace", "table_name"}
)

#: Column entities minted per table from the table's own column records.  The
#: source-modeling ADR names no number; 200 keeps an ordinary table whole and
#: bounds a several-thousand-column outlier at a small fraction of its nodes.
DEFAULT_MAX_COLUMNS_PER_TABLE: Final = 200
#: Floor of the names kept in a table's ``column_names``: names are strings on
#: one node, far cheaper than nodes with their own SCD-2 history.
MAX_COLUMN_NAMES: Final = 1000

_MAX_TEXT: Final = 256
_MAX_DESCRIPTION: Final = 1000


def _refuse(message: str) -> NoReturn:
    raise ValidationError(message, errors=[message], code=INPUT_REFUSAL_CODE)


# ---------------------------------------------------------------------------
# Identity (the id, scope and name rules live in ``unity_catalog_identity``)
# ---------------------------------------------------------------------------


def _fold(value: Any) -> str:
    return str(value).strip().lower()


# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------


def _text(value: Any, cap: int = _MAX_TEXT) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:cap]


def _epoch_ms(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _iso(value: Any) -> str | None:
    """A timestamp as UTC ISO-8601 to the millisecond, or ``None``.

    Epoch milliseconds (an int, as the REST API returns) and ISO strings (as
    the system tables export them) both parse.  A value that parses as neither
    is dropped rather than guessed at.
    """
    try:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(value / 1000, tz=UTC)
        elif isinstance(value, str) and value.strip():
            text = value.strip()
            if text.lstrip("-").isdigit():
                moment = datetime.fromtimestamp(int(text) / 1000, tz=UTC)
            else:
                moment = datetime.fromisoformat(text)
                moment = (
                    moment.replace(tzinfo=UTC)
                    if moment.tzinfo is None
                    else moment.astimezone(UTC)
                )
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    return moment.isoformat(timespec="milliseconds")


def _latest(a: str | None, b: str | None) -> str | None:
    """The later of two ISO stamps of one format (they compare as strings)."""
    if a is None or b is None:
        return a or b
    return max(a, b)


def _pick(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """Of two records for one object, the one with the later ``updated_at``.

    Ties break on the canonical JSON text, so which record wins never depends
    on the order the pages arrived in.
    """
    if old is None:
        return new

    def rank(rec: dict[str, Any]) -> tuple[int, str]:
        return _epoch_ms(rec.get("updated_at")), json.dumps(
            rec, sort_keys=True, default=str
        )

    return new if rank(new) > rank(old) else old


# ---------------------------------------------------------------------------
# Parsed input
# ---------------------------------------------------------------------------

_TableParts = tuple[str, str, str]


@dataclass
class _Collected:
    catalogs: dict[str, dict[str, Any]] = field(default_factory=dict)
    schemas: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    tables: dict[_TableParts, dict[str, Any]] = field(default_factory=dict)
    #: ``(source, target)`` -> latest event time
    table_lineage: dict[tuple[_TableParts, _TableParts], str | None] = field(
        default_factory=dict
    )
    #: ``((table, column), (table, column))`` -> latest event time
    column_lineage: dict[
        tuple[tuple[_TableParts, str], tuple[_TableParts, str]], str | None
    ] = field(default_factory=dict)
    namespaces: set[str] = field(default_factory=set)
    skipped: Counter[str] = field(default_factory=Counter)

    def add_table_edge(
        self, source: _TableParts, target: _TableParts, when: str | None
    ) -> None:
        key = (source, target)
        self.table_lineage[key] = _latest(self.table_lineage.get(key), when)

    def add_column_edge(
        self,
        source: tuple[_TableParts, str],
        target: tuple[_TableParts, str],
        when: str | None,
    ) -> None:
        key = (source, target)
        self.column_lineage[key] = _latest(self.column_lineage.get(key), when)


def _record_list(value: Any, where: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        _refuse(f"{where}: expected a list, got {type(value).__name__}")
    return value


def _table_parts(rec: dict[str, Any]) -> _TableParts | None:
    catalog, schema, name = (
        rec.get("catalog_name"),
        rec.get("schema_name"),
        rec.get("name"),
    )
    if all(isinstance(p, str) and p.strip() for p in (catalog, schema, name)):
        return (_fold(catalog), _fold(schema), _fold(name))
    full = rec.get("full_name")
    parts = split_full_name(full, 3) if isinstance(full, str) else None
    return (parts[0], parts[1], parts[2]) if parts else None


def _schema_parts(rec: dict[str, Any]) -> tuple[str, str] | None:
    catalog, name = rec.get("catalog_name"), rec.get("name")
    if all(isinstance(p, str) and p.strip() for p in (catalog, name)):
        return (_fold(catalog), _fold(name))
    full = rec.get("full_name")
    parts = split_full_name(full, 2) if isinstance(full, str) else None
    return (parts[0], parts[1]) if parts else None


def _add_catalog(rec: Any, where: str, state: _Collected) -> None:
    name = rec.get("name") if isinstance(rec, dict) else None
    if not isinstance(name, str) or not name.strip():
        _refuse(f"{where}: catalog record has no name")
    key = _fold(name)
    state.catalogs[key] = _pick(state.catalogs.get(key), rec)


def _add_schema(rec: Any, where: str, state: _Collected) -> None:
    parts = _schema_parts(rec) if isinstance(rec, dict) else None
    if parts is None:
        _refuse(f"{where}: schema record names no catalog and schema")
    state.schemas[parts] = _pick(state.schemas.get(parts), rec)


def _add_table(rec: Any, where: str, state: _Collected) -> None:
    parts = _table_parts(rec) if isinstance(rec, dict) else None
    if parts is None:
        _refuse(f"{where}: table record names no catalog, schema and table")
    state.tables[parts] = _pick(state.tables.get(parts), rec)


def _row_table(row: dict[str, Any], side: str) -> _TableParts | None:
    full = row.get(f"{side}_table_full_name")
    if isinstance(full, str) and full.strip():
        parts = split_full_name(full, 3)
        return (parts[0], parts[1], parts[2]) if parts else None
    catalog, schema, name = (
        row.get(f"{side}_table_catalog"),
        row.get(f"{side}_table_schema"),
        row.get(f"{side}_table_name"),
    )
    if all(isinstance(p, str) and p.strip() for p in (catalog, schema, name)):
        return (_fold(catalog), _fold(schema), _fold(name))
    return None


def _add_lineage_row(row: dict[str, Any], state: _Collected) -> None:
    """One ``system.access.{table,column}_lineage`` row."""
    source, target = _row_table(row, "source"), _row_table(row, "target")
    if source is None or target is None:
        # A path, a volume or a null on one side: not a table-to-table fact.
        state.skipped["lineage_without_two_tables"] += 1
        return
    when = _iso(row.get("event_time"))
    source_col, target_col = (
        row.get("source_column_name"),
        row.get("target_column_name"),
    )
    if source_col is None and target_col is None:
        state.add_table_edge(source, target, when)
        return
    if not (_text(source_col) and _text(target_col)):
        state.skipped["column_lineage_without_two_columns"] += 1
        return
    state.add_column_edge(
        (source, _fold(source_col)), (target, _fold(target_col)), when
    )


def _add_lineage_api(doc: dict[str, Any], where: str, state: _Collected) -> None:
    """A ``lineage-tracking/table-lineage`` response for ``doc["table_name"]``."""
    queried = doc.get("table_name")
    parts = split_full_name(queried, 3) if isinstance(queried, str) else None
    if parts is None:
        _refuse(
            f"{where}: a lineage-tracking response must also carry "
            '"table_name": "<catalog>.<schema>.<table>", the table it was requested for'
        )
    queried_parts: _TableParts = (parts[0], parts[1], parts[2])
    for key, upstream in (("upstreams", True), ("downstreams", False)):
        for idx, item in enumerate(_record_list(doc.get(key), f"{where}.{key}")):
            info = item.get("tableInfo") if isinstance(item, dict) else None
            if not isinstance(info, dict):
                state.skipped["lineage_item_without_tableInfo"] += 1
                continue
            other = _table_parts(info)
            if other is None:
                _refuse(f"{where}.{key}[{idx}]: tableInfo names no table")
            when = _iso(info.get("lineage_timestamp"))
            if upstream:
                state.add_table_edge(other, queried_parts, when)
            else:
                state.add_table_edge(queried_parts, other, when)


def _is_bare_catalog(node: dict[str, Any]) -> bool:
    """A bare record with a ``name``, none of a table's or column's fields, and
    a ``full_name`` (when it has one) equal to that ``name``.

    A catalog's three-part-free ``full_name`` is its ``name``; a dotted one
    belongs to a schema or a table whose ``catalog_name`` the record lost.
    """
    if "name" not in node or not node.keys().isdisjoint(_NOT_A_CATALOG_KEYS):
        return False
    full_name = node.get("full_name")
    return full_name is None or _fold(full_name) == _fold(node["name"])


def _walk(node: Any, where: str, state: _Collected) -> None:  # noqa: PLR0912 - one dispatch over every accepted shape
    if isinstance(node, list):
        for idx, item in enumerate(node):
            _walk(item, f"{where}[{idx}]", state)
        return
    if not isinstance(node, dict):
        _refuse(f"{where}: expected an object or a list, got {type(node).__name__}")

    if any(key in node for key in _WRAPPER_KEYS) or (
        node.keys() <= _IGNORED_DOCUMENT_KEYS
    ):
        namespace = normalize_scope(node.get("namespace"))
        if namespace:
            state.namespaces.add(namespace)
        for key, add in (
            ("catalogs", _add_catalog),
            ("schemas", _add_schema),
            ("tables", _add_table),
        ):
            for idx, rec in enumerate(_record_list(node.get(key), f"{where}.{key}")):
                add(rec, f"{where}.{key}[{idx}]", state)
        if "upstreams" in node or "downstreams" in node:
            _add_lineage_api(node, where, state)
        return

    # A bare record, told apart by the fields only it carries.
    if "source_table_full_name" in node or "target_table_full_name" in node:
        _add_lineage_row(node, state)
    elif "catalog_name" in node and "schema_name" in node:
        _add_table(node, where, state)
    elif "catalog_name" in node:
        _add_schema(node, where, state)
    elif _is_bare_catalog(node):
        _add_catalog(node, where, state)
    else:
        _refuse(f"{where}: not a catalog, schema, table or lineage record or response")


def _collect(raw_input: Any) -> _Collected:
    """Read ``raw_input`` into records, or raise the typed refusal."""
    state = _Collected()
    try:
        _walk(raw_input, "input", state)
    except RecursionError:
        # Lists nest without bound in JSON; a real export is a few levels deep.
        _refuse("input: nested too deeply to read")
    if len(state.namespaces) > 1:
        _refuse("input: the documents name more than one namespace")
    return state


def check_input(raw_input: Any) -> None:
    """Raise the typed refusal if ``raw_input`` is not readable, else return.

    The CLI calls this before it opens a store, so a malformed export is
    refused up front instead of surfacing from inside the extraction pipeline.
    """
    _collect(raw_input)


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------


def _drop_none(props: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in props.items() if v is not None}


class UnityCatalogExtractor:
    """Deterministic extractor for a Unity Catalog metadata and lineage export.

    Args:
        namespace: The OpenLineage dataset namespace of the workspace
            (``unitycatalog://<host>``, normalised as the module docstring
            says).  Leave it unset to join an OpenLineage ``unity-catalog``
            symlink out of the box.  When set it makes a table's entity id the
            one :class:`~trellis_workers.extract.openlineage.OpenLineageExtractor`
            gives a ``unitycatalog://<host>`` dataset (or a symlink, given the
            same ``unity_catalog_scope``) and binds that OpenLineage alias to the
            table.  Overrides a ``namespace`` in the input.  It is part of every
            id: choose it before the first run.
        include_system: Keep the ``system`` and ``__databricks_internal``
            catalogs (default: leave them out, along with lineage that touches them).
        include_columns: Emit a ``uc_column`` entity per column and column
            lineage (default).  Off, columns are counted (``column_count``)
            and column lineage is skipped.
        max_columns_per_table: Most ``uc_column`` entities minted from one
            table's own column records, in position order (default
            :data:`DEFAULT_MAX_COLUMNS_PER_TABLE`).  Columns past it are counted
            as ``columns_over_cap`` in the result; a column that column lineage
            references is minted regardless.

    An object the export names but does not hold, whether through a lineage row
    or as the parent of a record, becomes a name-only ``if_absent`` placeholder
    marked ``stub`` (see the module docstring).

    A column entity is a structural leaf: ``node_role="structural"``, excluded
    from default retrieval.  ``uc_column`` is not in
    :data:`~trellis.schemas.well_known.ENTITY_TYPE_ANTI_PATTERNS`, so the
    validator has nothing to warn about and ``allow_structural_leaf`` stays off.
    """

    name = "unity_catalog"
    tier = ExtractorTier.DETERMINISTIC
    supported_sources: ClassVar[list[str]] = ["unity-catalog"]
    version = "0.1.0"

    def __init__(
        self,
        *,
        namespace: str | None = None,
        include_system: bool = False,
        include_columns: bool = True,
        max_columns_per_table: int = DEFAULT_MAX_COLUMNS_PER_TABLE,
    ) -> None:
        if max_columns_per_table < 1:
            msg = "max_columns_per_table must be at least 1"
            raise ValueError(msg)
        self._namespace = normalize_scope(namespace)
        self._include_system = include_system
        self._include_columns = include_columns
        self._max_columns = max_columns_per_table

    async def extract(
        self,
        raw_input: Any,
        *,
        source_hint: str | None = None,
        context: ExtractionContext | None = None,
    ) -> ExtractionResult:
        del context  # deterministic — no cost budget

        state = _collect(raw_input)
        namespace = self._namespace or next(iter(state.namespaces), None)

        builder = _Builder(
            state,
            namespace=namespace,
            include_system=self._include_system,
            include_columns=self._include_columns,
            max_columns=self._max_columns,
        )
        entities, edges = builder.build()
        # Unary + drops a reason whose count lineage brought back to zero.
        skipped = dict(sorted((+state.skipped).items()))
        if skipped:
            logger.info("unity_catalog_rows_skipped", **skipped)
        logger.info(
            "unity_catalog_extracted",
            entities=len(entities),
            edges=len(edges),
            source_hint=source_hint,
        )
        return ExtractionResult(
            entities=entities,
            edges=edges,
            extractor_used=self.name,
            tier=self.tier.value,
            skipped_rows=skipped,
            provenance=ExtractionProvenance(
                extractor_name=self.name,
                extractor_version=self.version,
                source_hint=source_hint,
            ),
        )


class _Builder:
    """Turns the collected records into drafts in one stable order."""

    def __init__(
        self,
        state: _Collected,
        *,
        namespace: str | None,
        include_system: bool,
        include_columns: bool,
        max_columns: int,
    ) -> None:
        self._state = state
        self._namespace = namespace
        self._scope = namespace or DEFAULT_SCOPE
        self._include_system = include_system
        self._include_columns = include_columns
        self._max_columns = max_columns
        self._entities: dict[str, EntityDraft] = {}
        self._edges: dict[tuple[str, str, str], EdgeDraft] = {}
        self._held: dict[_TableParts, dict[str, tuple[str, dict[str, Any]]]] = {}

    # -- ids ---------------------------------------------------------------

    def _catalog_id(self, catalog: str) -> str:
        return f"{UC_CATALOG_ENTITY_TYPE}:{self._scope}:{identity_key(catalog)}"

    def _schema_id(self, catalog: str, schema: str) -> str:
        return f"{UC_SCHEMA_ENTITY_TYPE}:{self._scope}:{identity_key(catalog, schema)}"

    def _table_id(self, parts: _TableParts) -> str:
        return table_entity_id(self._scope, identity_key(*parts))

    def _column_id(self, parts: _TableParts, column: str) -> str:
        return f"{UC_COLUMN_ENTITY_TYPE}:{self._scope}:{identity_key(*parts)}/{column}"

    # -- selection ---------------------------------------------------------

    def _kept(self, catalog: str) -> bool:
        return self._include_system or catalog not in SYSTEM_CATALOGS

    # -- ensure ------------------------------------------------------------

    def _contains(self, parent: str, child: str) -> None:
        self._edges[(EDGE_CONTAINS, parent, child)] = EdgeDraft(
            source_id=parent, target_id=child, edge_kind=EDGE_CONTAINS
        )

    def _ensure_catalog(self, catalog: str) -> str:
        cid = self._catalog_id(catalog)
        if cid not in self._entities:
            found = self._state.catalogs.get(catalog)
            rec = found or {}
            key = identity_key(catalog)
            props = _drop_none(
                {
                    "name": _text(rec.get("name")) or catalog,
                    "full_name": key,
                    "source_system": "databricks",
                    "stub": True if found is None else None,
                    "catalog_type": _text(rec.get("catalog_type")),
                    "owner": _text(rec.get("owner")),
                    "description": _text(rec.get("comment"), _MAX_DESCRIPTION),
                    "storage_location": _text(
                        rec.get("storage_location") or rec.get("storage_root")
                    ),
                    "source_created_at": _iso(rec.get("created_at")),
                    "source_updated_at": _iso(rec.get("updated_at")),
                }
            )
            self._entities[cid] = EntityDraft(
                entity_id=cid,
                entity_type=UC_CATALOG_ENTITY_TYPE,
                name=props["name"],
                properties=props,
                if_absent=found is None,
                aliases=[
                    AliasDraft(
                        source_system=wk.UNITY_CATALOG_SOURCE_SYSTEM,
                        raw_id=key,
                        raw_name=props["name"],
                    )
                ],
            )
        return cid

    def _ensure_schema(self, catalog: str, schema: str) -> str:
        sid = self._schema_id(catalog, schema)
        if sid not in self._entities:
            found = self._state.schemas.get((catalog, schema))
            rec = found or {}
            key = identity_key(catalog, schema)
            props = _drop_none(
                {
                    "name": _text(rec.get("name")) or schema,
                    "full_name": key,
                    "database_name": catalog,
                    "source_system": "databricks",
                    "stub": True if found is None else None,
                    "owner": _text(rec.get("owner")),
                    "description": _text(rec.get("comment"), _MAX_DESCRIPTION),
                    "storage_location": _text(
                        rec.get("storage_location") or rec.get("storage_root")
                    ),
                    "source_created_at": _iso(rec.get("created_at")),
                    "source_updated_at": _iso(rec.get("updated_at")),
                }
            )
            self._entities[sid] = EntityDraft(
                entity_id=sid,
                entity_type=UC_SCHEMA_ENTITY_TYPE,
                name=props["name"],
                properties=props,
                if_absent=found is None,
                aliases=[
                    AliasDraft(
                        source_system=wk.UNITY_CATALOG_SOURCE_SYSTEM,
                        raw_id=key,
                        raw_name=props["name"],
                    )
                ],
            )
            self._contains(self._ensure_catalog(catalog), sid)
        return sid

    def _ensure_table(self, parts: _TableParts) -> str:
        tid = self._table_id(parts)
        if tid in self._entities:
            return tid
        catalog, schema, table = parts
        found = self._state.tables.get(parts)
        rec = found or {}
        key = identity_key(*parts)
        columns = rec.get("columns")
        names = self._column_names(parts) if isinstance(columns, list) else None
        props = table_properties(
            parts,
            self._scope,
            name=_text(rec.get("name")) or table,
            stub=found is None,
        )
        props.update(
            _drop_none(
                {
                    "table_type": _text(rec.get("table_type")),
                    "data_source_format": _text(rec.get("data_source_format")),
                    "owner": _text(rec.get("owner")),
                    "description": _text(rec.get("comment"), _MAX_DESCRIPTION),
                    "storage_location": _text(rec.get("storage_location")),
                    "source_created_at": _iso(rec.get("created_at")),
                    "source_updated_at": _iso(rec.get("updated_at")),
                    "column_count": len(columns) if isinstance(columns, list) else None,
                    "column_names": names,
                }
            )
        )
        aliases = [
            AliasDraft(
                source_system=wk.UNITY_CATALOG_SOURCE_SYSTEM,
                raw_id=key,
                raw_name=props["name"],
            )
        ]
        if self._namespace:
            aliases.append(
                AliasDraft(
                    source_system=OPENLINEAGE_DATASET_ALIAS_SOURCE_SYSTEM,
                    raw_id=f"{self._namespace}/{key}",
                    raw_name=key,
                )
            )
        self._entities[tid] = EntityDraft(
            entity_id=tid,
            entity_type=UC_TABLE_ENTITY_TYPE,
            name=props["name"],
            properties=props,
            aliases=aliases,
            if_absent=found is None,
        )
        self._contains(self._ensure_schema(catalog, schema), tid)
        return tid

    def _ensure_column(
        self, parts: _TableParts, column: str, shown: str, found: dict[str, Any] | None
    ) -> str:
        cid = self._column_id(parts, column)
        if cid in self._entities:
            return cid
        rec = found or {}
        position = rec.get("position")
        nullable = rec.get("nullable")
        props: dict[str, Any] = _drop_none(
            {
                "name": shown,
                "table_full_name": identity_key(*parts),
                "source_system": "databricks",
                "stub": True if found is None else None,
                "type_text": _text(rec.get("type_text")),
                "type_name": _text(rec.get("type_name")),
                "position": position
                if isinstance(position, int) and not isinstance(position, bool)
                else None,
                "nullable": nullable if isinstance(nullable, bool) else None,
                "description": _text(rec.get("comment"), _MAX_DESCRIPTION),
            }
        )
        self._entities[cid] = EntityDraft(
            entity_id=cid,
            entity_type=UC_COLUMN_ENTITY_TYPE,
            name=shown,
            properties=props,
            node_role=NodeRole.STRUCTURAL,
            if_absent=found is None,
        )
        self._contains(self._ensure_table(parts), cid)
        return cid

    # -- build -------------------------------------------------------------

    def build(self) -> tuple[list[EntityDraft], list[EdgeDraft]]:  # noqa: PLR0912 - one pass per object kind
        state = self._state
        for catalog in sorted(state.catalogs):
            if self._kept(catalog):
                self._ensure_catalog(catalog)
        for catalog, schema in sorted(state.schemas):
            if self._kept(catalog):
                self._ensure_schema(catalog, schema)

        for parts in sorted(state.tables):
            if not self._kept(parts[0]):
                continue
            self._ensure_table(parts)
            if self._include_columns:
                named = self._named_columns(parts)
                for folded, shown, rec in named[: self._max_columns]:
                    self._ensure_column(parts, folded, shown, rec)
                if len(named) > self._max_columns:
                    self._state.skipped["columns_over_cap"] += (
                        len(named) - self._max_columns
                    )

        for (source, target), when in sorted(state.table_lineage.items()):
            if self._kept(source[0]) and self._kept(target[0]):
                self._table_lineage(source, target, when)

        if self._include_columns:
            for ((s_tab, s_name), (t_tab, t_name)), moment in sorted(
                state.column_lineage.items()
            ):
                if self._kept(s_tab[0]) and self._kept(t_tab[0]):
                    self._column_lineage((s_tab, s_name), (t_tab, t_name), moment)
        elif state.column_lineage:
            state.skipped["column_lineage_with_columns_off"] += len(
                state.column_lineage
            )

        entities = [self._entities[key] for key in sorted(self._entities)]
        edges = [self._edges[key] for key in sorted(self._edges)]
        return entities, edges

    def _table_columns(self, parts: _TableParts) -> list[dict[str, Any]]:
        rec = self._state.tables.get(parts) or {}
        columns = rec.get("columns")
        if not isinstance(columns, list):
            return []
        return sorted(
            (c for c in columns if isinstance(c, dict)),
            key=lambda c: (
                c.get("position") if isinstance(c.get("position"), int) else 0,
                json.dumps(c, sort_keys=True, default=str),
            ),
        )

    def _named_columns(
        self, parts: _TableParts
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """The table's named columns in position order, one per folded name."""
        seen: set[str] = set()
        named: list[tuple[str, str, dict[str, Any]]] = []
        for rec in self._table_columns(parts):
            shown = _text(rec.get("name"))
            if shown and _fold(shown) not in seen:
                seen.add(_fold(shown))
                named.append((_fold(shown), shown, rec))
        return named

    def _column_names(self, parts: _TableParts) -> list[str]:
        """``column_names`` for the table: the shown names, bounded and counted."""
        shown = [name for _, name, _ in self._named_columns(parts)]
        limit = max(self._max_columns, MAX_COLUMN_NAMES)
        if len(shown) > limit:
            self._state.skipped["column_names_over_limit"] += len(shown) - limit
        return shown[:limit]

    def _table_lineage(
        self, source: _TableParts, target: _TableParts, when: str | None
    ) -> None:
        down, up = self._table_id(target), self._table_id(source)
        if down == up:
            self._state.skipped["self_lineage"] += 1
            return
        # An endpoint the export does not hold is a placeholder, so the edge
        # always has two nodes to join and the store never holds it dangling.
        self._ensure_table(target)
        self._ensure_table(source)
        self._depends_on(down, up, when)

    def _column_lineage(
        self,
        source: tuple[_TableParts, str],
        target: tuple[_TableParts, str],
        when: str | None,
    ) -> None:
        down, up = self._column_id(*target), self._column_id(*source)
        if down == up:
            self._state.skipped["self_lineage"] += 1
            return
        for tab, name in (target, source):
            held = self._held_column(tab, name)
            if held is None:
                # A placeholder column is named by the folded name its id carries.
                self._ensure_column(tab, name, name, None)
            elif self._column_id(tab, name) not in self._entities:
                # Held but past the cap: mint it from its record, and it is no
                # longer one the cap left out.
                self._ensure_column(tab, name, *held)
                self._state.skipped["columns_over_cap"] -= 1
        self._depends_on(down, up, when)

    def _held_column(
        self, parts: _TableParts, folded: str
    ) -> tuple[str, dict[str, Any]] | None:
        """The shown name and record of a column the table's export holds."""
        if parts not in self._held:
            self._held[parts] = {
                key: (shown, rec) for key, shown, rec in self._named_columns(parts)
            }
        return self._held[parts].get(folded)

    def _depends_on(self, downstream: str, upstream: str, when: str | None) -> None:
        self._edges[(EDGE_DEPENDS_ON, downstream, upstream)] = EdgeDraft(
            source_id=downstream,
            target_id=upstream,
            edge_kind=EDGE_DEPENDS_ON,
            properties=_drop_none({"last_event_time": when}),
        )
