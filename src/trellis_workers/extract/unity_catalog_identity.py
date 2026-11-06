"""Unity Catalog table identity, shared by two extractors.

:class:`~trellis_workers.extract.unity_catalog.UnityCatalogExtractor` and
:class:`~trellis_workers.extract.openlineage.OpenLineageExtractor` must give one
Unity Catalog table one entity id, byte for byte, or the table becomes two
nodes.  Everything that goes into that id lives here, once: the scope, the
normalised three-part name, the id format, and the name-only placeholder
properties either extractor writes for a table it can name but not describe.

The id is ``dataset:{scope}:{catalog.schema.table}``.  The scope is the
workspace's OpenLineage namespace (``unitycatalog://<host>``) when one is
known, else :data:`DEFAULT_SCOPE`.  The three-part name is case-folded and
unquoted by :func:`trellis.schemas.well_known.normalize_alias_raw_id`, the
function ``save_knowledge`` and the alias handler look a Unity Catalog id up
with.  This module imports nothing from either extractor, so both can import
it.
"""

from __future__ import annotations

from typing import Any, Final

from trellis.schemas import well_known as wk

#: ``dataset`` (lowercase, as OpenLineage writes it) so a table and the
#: OpenLineage dataset naming it are one node of one type.
UC_TABLE_ENTITY_TYPE: Final = "dataset"

#: Scope used in entity ids when no workspace namespace is known.
DEFAULT_SCOPE: Final = "unity_catalog"

#: ``source_system`` property on every Unity Catalog object.
UC_SOURCE_SYSTEM: Final = "databricks"

#: The OpenLineage dataset namespace Databricks puts on a ``symlinks`` identifier
#: for a Unity Catalog table.  A fixed string with no host.
SYMLINK_NAMESPACE: Final = "unity-catalog"

_UC_SCHEME: Final = "unitycatalog"
_HTTPS_PORT: Final = ":443"


def dataset_entity_id(namespace: str, name: str) -> str:
    """The id of a ``dataset`` entity: OpenLineage's, and a Unity Catalog table's."""
    return f"dataset:{namespace}:{name}"


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def is_unity_catalog_namespace(value: Any) -> bool:
    """Whether ``value`` is a ``unitycatalog://`` namespace (any case)."""
    return isinstance(value, str) and value.strip().lower().startswith(
        f"{_UC_SCHEME}://"
    )


def normalize_scope(value: Any) -> str | None:
    """A scope as a string, or ``None`` when ``value`` holds none.

    Surrounding space and a trailing ``/`` are dropped from any value.  A
    ``unitycatalog://`` namespace also has its scheme and host lowercased (host
    names are not case-sensitive) and an explicit ``:443`` port dropped, which
    is the default for the HTTPS API it names, so ``UnityCatalog://Host:443/``
    and ``unitycatalog://host`` are one scope.  Any other port is kept.  Any
    other scheme is left as written: a scope that is not a Unity Catalog
    namespace is the caller's own string.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not is_unity_catalog_namespace(text):
        return text.rstrip("/").strip() or None
    _, _, rest = text.partition("://")
    authority, _, path = rest.partition("/")
    host = authority.strip().lower().removesuffix(_HTTPS_PORT)
    if not host:
        return None  # ``unitycatalog://`` alone names no workspace
    path = path.strip("/")
    scope = f"{_UC_SCHEME}://{host}"
    return f"{scope}/{path}" if path else scope


def scope_host(scope: str | None) -> str | None:
    """The host of a ``scheme://host`` scope, or ``None`` for any other."""
    if not scope or "://" not in scope:
        return None
    return scope.partition("://")[2].strip("/") or None


# ---------------------------------------------------------------------------
# Name
# ---------------------------------------------------------------------------


def _quote_part(part: str) -> str:
    return f"`{part.replace('`', '``')}`" if "." in part or "`" in part else part


def identity_key(*parts: str) -> str:
    """The canonical dotted name of a catalog, schema or table.

    Case-folded and unquoted by
    :func:`trellis.schemas.well_known.normalize_alias_raw_id`, so
    ``Main.Sales.Orders``, ``main.sales.orders`` and
    ``` `main`.`sales`.`orders` ``` are one table.
    """
    joined = ".".join(_quote_part(part) for part in parts)
    return wk.normalize_alias_raw_id(wk.UNITY_CATALOG_SOURCE_SYSTEM, joined)


def split_full_name(full_name: str, expected: int) -> tuple[str, ...] | None:
    """The ``expected`` parts of a dotted Unity Catalog name, or ``None``.

    ``None`` for a name that does not split into exactly ``expected`` non-empty
    parts, including one whose part holds a dot (which Unity Catalog does not
    allow).  Parts come back case-folded and unquoted.
    """
    key = wk.normalize_alias_raw_id(wk.UNITY_CATALOG_SOURCE_SYSTEM, full_name)
    if "`" in key:  # a part holding a dot, which Unity Catalog does not allow
        return None
    parts = tuple(key.split("."))
    # A value the normaliser could not parse comes back unchanged, so a blank
    # part (``a. .b``) is refused here rather than read as a name.
    ok = len(parts) == expected and all(part.strip() for part in parts)
    return parts if ok else None


def table_entity_id(scope: str, key: str) -> str:
    """The entity id of the table whose :func:`identity_key` is ``key``."""
    return dataset_entity_id(scope, key)


def table_properties(
    parts: tuple[str, str, str], scope: str, *, name: str, stub: bool
) -> dict[str, Any]:
    """The properties every Unity Catalog table node carries, placeholder or not.

    The placeholder is exactly this with ``stub`` set: both extractors write it
    for a table they can name but not describe, so it reads the same whichever
    wrote it.  ``physical_uri`` is set only when the scope names a host.
    """
    catalog, schema, table = parts
    props: dict[str, Any] = {
        "name": name,
        "full_name": identity_key(*parts),
        "database_name": catalog,
        "schema_name": schema,
        "source_system": UC_SOURCE_SYSTEM,
    }
    if stub:
        props["stub"] = True
    host = scope_host(scope)
    if host:
        props[wk.DATASET_PROP_PHYSICAL_URI] = (
            f"databricks://{host}/{catalog}/{schema}/{table}"
        )
    return props
