"""Bolt graph reads and writes of a ``node_id`` that has two current rows.

Shared by ``test_neo4j_graph.py`` and ``test_arcadedb_graph.py``. The Bolt
backends hold "at most one current row per ``node_id``" in the
close-then-insert transaction, not in the database, so concurrent writers
can leave two (the module docstring of
``trellis.stores.bolt_opencypher.graph``). Each check writes the second row
directly, as that race would, then requires each read of current node rows
to show one version of the node: the one with the latest ``valid_from``, or
the greater ``version_id`` when the stamps are equal. Those reads are
``get_node``, ``search_nodes`` in either direction, ``count_nodes_by_type``,
``get_nodes_bulk``, ``get_subgraph``, ``query`` and ``execute_node_query``.
The last four are also read ``as_of`` an instant both rows are valid, and a
listing's ``limit`` counts nodes; a filtered listing ``as_of`` an instant
before the two rows shows the version valid then. The write checks require
the next write to continue that version and close both rows, leaving one
current row.

The duplicates are built so that rival rules pick the other row. A later
stamp carries the smaller ``version_id`` and an earlier stamp the greater,
so ranking by ``version_id`` first fails. A duplicate's ``created_at`` moves
with its ``valid_from``, as when both writers create a node's first
version, so keeping the first row in ``created_at`` order fails in one sort
direction, and a write that carries ``created_at`` over from the hidden
row is seen.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from trellis.stores.base.graph import NODE_SEARCH_SORTS
from trellis.stores.base.graph_query import FilterClause, NodeQuery

LATER = timedelta(seconds=5)
EARLIER = -LATER
SAME = timedelta(0)
NODE_TYPES = ("kind-a", "kind-b")


def add_current_row(
    store: Any,
    node_id: str,
    *,
    name: str,
    shift: timedelta,
    greater_version_id: bool,
    node_type: str | None = None,
    created_at_shift: timedelta | None = None,
) -> None:
    """Write a second current row for *node_id*, copied from its current one.

    The copy takes *name*, *node_type* when given, a ``valid_from`` moved by
    *shift*, a ``created_at`` moved by *created_at_shift* (by default
    *shift*), and a ``version_id`` that sorts above the original's when
    *greater_version_id* is true and below it otherwise.
    """
    with store._driver.session(database=store._database) as session:
        record = session.run(
            "MATCH (n:Node {node_id: $node_id}) WHERE n.valid_to IS NULL "
            "RETURN n {.*, embedding: null} AS n",
            node_id=node_id,
        ).single()
        row = dict(record["n"])
        del row["embedding"]
        original = str(row["version_id"])
        row["version_id"] = original + "z" if greater_version_id else "0" + original
        assert (row["version_id"] > original) is greater_version_id
        properties = json.loads(row["properties_json"])
        properties["name"] = name
        row["properties_json"] = json.dumps(properties)
        created_at_move = shift if created_at_shift is None else created_at_shift
        moves = {"valid_from": shift, "created_at": created_at_move}
        for stamp, move in moves.items():
            row[stamp] = (datetime.fromisoformat(str(row[stamp])) + move).isoformat()
        if node_type is not None:
            row["node_type"] = node_type
        session.run("CREATE (m:Node) SET m = $row", row=row).consume()


def _shown(node: dict[str, Any]) -> tuple[str, str]:
    """Which version a read returned: its name and its type."""
    return node["properties"]["name"], node["node_type"]


def _assert_every_read_shows(store: Any, expected: dict[str, tuple[str, str]]) -> None:
    """Every search order and ``get_node`` show *expected*, one row per node."""
    for sort in sorted(NODE_SEARCH_SORTS):
        for descending in (False, True):
            rows, total = store.search_nodes(sort=sort, descending=descending)
            shown = {row["node_id"]: _shown(row) for row in rows}
            assert shown == expected, (sort, descending, shown)
            assert len(rows) == total == len(expected), (sort, descending, total)
    for node_id, version in expected.items():
        shown_by_id = _shown(store.get_node(node_id))
        assert shown_by_id == version, (node_id, shown_by_id)


def check_every_read_shows_the_newest_version(store: Any) -> None:
    """The later duplicate is shown, and the earlier one never is."""
    store.upsert_node("plain-a", "kind-a", {"name": "plain a"})
    store.upsert_node("plain-b", "kind-b", {"name": "plain b"})
    store.upsert_node("dup-later", "kind-a", {"name": "first later"})
    store.upsert_node("dup-earlier", "kind-a", {"name": "first earlier"})
    add_current_row(
        store, "dup-later", name="second later", shift=LATER, greater_version_id=False
    )
    add_current_row(
        store,
        "dup-earlier",
        name="second earlier",
        shift=EARLIER,
        greater_version_id=True,
    )
    _assert_every_read_shows(
        store,
        {
            "plain-a": ("plain a", "kind-a"),
            "plain-b": ("plain b", "kind-b"),
            "dup-later": ("second later", "kind-a"),
            "dup-earlier": ("first earlier", "kind-a"),
        },
    )


def check_type_counts_follow_the_shown_version(store: Any) -> None:
    """Counts and type filters see each node once, as the version shown.

    Each duplicated node has an ``alpha`` row of ``kind-a`` and an
    ``omega`` row of ``kind-b``; the newer row decides its name and type,
    so a search for the older row's name or type must not find it.
    """
    store.upsert_node("plain-a1", "kind-a", {"name": "plain"})
    store.upsert_node("plain-a2", "kind-a", {"name": "plain"})
    store.upsert_node("plain-b1", "kind-b", {"name": "plain"})
    store.upsert_node("dup-type-later", "kind-a", {"name": "alpha later"})
    store.upsert_node("dup-type-earlier", "kind-a", {"name": "alpha earlier"})
    add_current_row(
        store,
        "dup-type-later",
        name="omega later",
        node_type="kind-b",
        shift=LATER,
        greater_version_id=False,
    )
    add_current_row(
        store,
        "dup-type-earlier",
        name="omega earlier",
        node_type="kind-b",
        shift=EARLIER,
        greater_version_id=True,
    )
    expected_counts: dict[str | None, dict[str, int]] = {
        None: {"kind-a": 3, "kind-b": 2},
        "omega": {"kind-b": 1},
        "alpha": {"kind-a": 1},
        "kind-b": {"kind-b": 2},
        "dup-type": {"kind-a": 1, "kind-b": 1},
    }
    for search, expected in expected_counts.items():
        counts = store.count_nodes_by_type(search=search)
        assert counts == expected, (search, counts)
        rows, total = store.search_nodes(search=search)
        assert len(rows) == total == sum(counts.values()), (search, total)
        for node_type in NODE_TYPES:
            typed, typed_total = store.search_nodes(search=search, node_type=node_type)
            assert len(typed) == typed_total == counts.get(node_type, 0), (
                search,
                node_type,
                typed_total,
            )
    for node_id, version in (
        ("dup-type-later", ("omega later", "kind-b")),
        ("dup-type-earlier", ("alpha earlier", "kind-a")),
    ):
        shown_by_id = _shown(store.get_node(node_id))
        assert shown_by_id == version, (node_id, shown_by_id)


def check_equal_stamps_pick_the_greater_version_id(store: Any) -> None:
    """With equal stamps the greater ``version_id`` is shown, either way round.

    ``dup-tie-up``'s rows share ``created_at`` too, as when an upsert carries
    it forward. ``dup-tie-down``'s duplicate is created later, so the two
    search directions meet its rows in opposite orders, and only a rule that
    ranks by ``version_id`` shows one version in both.
    """
    store.upsert_node("plain-a", "kind-a", {"name": "plain a"})
    store.upsert_node("dup-tie-up", "kind-a", {"name": "first up"})
    store.upsert_node("dup-tie-down", "kind-a", {"name": "first down"})
    add_current_row(
        store,
        "dup-tie-up",
        name="second up",
        node_type="kind-b",
        shift=SAME,
        greater_version_id=True,
    )
    add_current_row(
        store,
        "dup-tie-down",
        name="second down",
        node_type="kind-b",
        shift=SAME,
        greater_version_id=False,
        created_at_shift=LATER,
    )
    _assert_every_read_shows(
        store,
        {
            "plain-a": ("plain a", "kind-a"),
            "dup-tie-up": ("second up", "kind-b"),
            "dup-tie-down": ("first down", "kind-a"),
        },
    )
    counts = store.count_nodes_by_type()
    assert counts == {"kind-a": 2, "kind-b": 1}, counts
    counts = store.count_nodes_by_type(search="second")
    assert counts == {"kind-b": 1}, counts


#: Duplicates for the listing checks: ``(node_id, shift, greater_version_id,
#: created_at_shift)``. Each original is ``kind-a`` and named ``<node_id>
#: original``; each copy is ``kind-b`` and named ``<node_id> copy``.
#: ``dup-later``'s copy is shown and is the newest node. ``dup-earlier``'s
#: original is shown, though its hidden copy is the newest row of all.
#: ``dup-tie``'s copy is shown and is the oldest node.
LIST_DUPLICATES = (
    ("dup-later", LATER, False, None),
    ("dup-earlier", EARLIER, True, LATER),
    ("dup-tie", SAME, True, EARLIER),
)
DUPLICATED = tuple(node_id for node_id, *_ in LIST_DUPLICATES)

#: The version each node of the listing checks shows: its name and type.
LIST_SHOWN = {
    "plain-a1": ("plain a1", "kind-a"),
    "plain-b1": ("plain b1", "kind-b"),
    "plain-a2": ("plain a2", "kind-a"),
    "dup-later": ("dup-later copy", "kind-b"),
    "dup-earlier": ("dup-earlier original", "kind-a"),
    "dup-tie": ("dup-tie copy", "kind-b"),
}

#: LIST_SHOWN's nodes by the shown row's ``created_at``, newest first.
NEWEST_FIRST = (
    "dup-later",
    "dup-earlier",
    "plain-a2",
    "plain-b1",
    "plain-a1",
    "dup-tie",
)


def _write_listed_nodes(store: Any) -> datetime:
    """Write LIST_SHOWN's nodes and return an instant both rows of each are valid.

    ``plain-a1`` gets an edge to every other node before the copies are
    written, so each edge is attached to one row of its node.
    """
    for node_id in ("plain-a1", "plain-b1", "plain-a2"):
        name, node_type = LIST_SHOWN[node_id]
        store.upsert_node(node_id, node_type, {"name": name})
    for node_id in DUPLICATED:
        store.upsert_node(node_id, "kind-a", {"name": f"{node_id} original"})
    for node_id in NEWEST_FIRST:
        if node_id != "plain-a1":
            store.upsert_edge("plain-a1", node_id, "relates_to")
    for node_id, shift, greater, created_at_shift in LIST_DUPLICATES:
        add_current_row(
            store,
            node_id,
            name=f"{node_id} copy",
            node_type="kind-b",
            shift=shift,
            greater_version_id=greater,
            created_at_shift=created_at_shift,
        )
        assert len(_current_rows(store, node_id)) == 2, node_id
    both_valid = datetime.now(UTC) + 2 * LATER
    for node_id, version in LIST_SHOWN.items():
        assert _shown(store.get_node(node_id)) == version, node_id
        assert _shown(store.get_node(node_id, as_of=both_valid)) == version, node_id
    return both_valid


def _listed(rows: list[dict[str, Any]]) -> list[tuple[str, tuple[str, str]]]:
    """Each row's ``node_id`` and the version it shows, in the order returned."""
    return [(row["node_id"], _shown(row)) for row in rows]


def _expected(node_ids: Iterable[str]) -> list[tuple[str, tuple[str, str]]]:
    return [(node_id, LIST_SHOWN[node_id]) for node_id in node_ids]


def _newest_of(node_type: str) -> list[str]:
    return [node_id for node_id in NEWEST_FIRST if LIST_SHOWN[node_id][1] == node_type]


def check_get_nodes_bulk_shows_one_version(store: Any) -> None:
    """``get_nodes_bulk`` returns each node once, as ``get_node`` shows it.

    For every node and for the duplicated ones alone, now and at an instant
    both rows of each duplicated node are valid.
    """
    both_valid = _write_listed_nodes(store)
    for node_ids in (list(LIST_SHOWN), list(DUPLICATED)):
        for as_of in (None, both_valid):
            rows = store.get_nodes_bulk(node_ids, as_of=as_of)
            assert sorted(_listed(rows)) == sorted(_expected(node_ids)), as_of


def check_get_subgraph_shows_one_version(store: Any) -> None:
    """``get_subgraph`` returns each node once, as ``get_node`` shows it.

    Each edge comes back once too: the copies carry none. From a plain seed
    and from the duplicated seeds, now and at an instant both rows of each
    duplicated node are valid.
    """
    both_valid = _write_listed_nodes(store)
    cases = (
        (["plain-a1"], 1, list(LIST_SHOWN)),
        (list(DUPLICATED), 1, ["plain-a1", *DUPLICATED]),
        (list(DUPLICATED), 0, list(DUPLICATED)),
    )
    for seeds, depth, node_ids in cases:
        edges = sorted(
            ("plain-a1", node_id) for node_id in node_ids if node_id != "plain-a1"
        )
        if "plain-a1" not in node_ids:
            edges = []
        for as_of in (None, both_valid):
            subgraph = store.get_subgraph(seeds, depth=depth, as_of=as_of)
            listed = sorted(_listed(subgraph["nodes"]))
            assert listed == sorted(_expected(node_ids)), (seeds, depth, as_of)
            got = sorted((e["source_id"], e["target_id"]) for e in subgraph["edges"])
            assert got == edges, (seeds, depth, as_of)


def check_query_shows_one_version(store: Any) -> None:
    """``query`` lists each node once, as ``get_node`` shows it, newest first.

    A ``limit`` counts nodes, and a type or property filter matches the
    version shown, never the hidden row.
    """
    both_valid = _write_listed_nodes(store)
    for as_of in (None, both_valid):
        assert _listed(store.query(as_of=as_of)) == _expected(NEWEST_FIRST), as_of
    for limit in range(1, len(NEWEST_FIRST) + 1):
        rows = store.query(limit=limit)
        assert _listed(rows) == _expected(NEWEST_FIRST[:limit]), limit
    for node_type in NODE_TYPES:
        newest = _newest_of(node_type)
        rows = store.query(node_type=node_type)
        assert _listed(rows) == _expected(newest), node_type
        rows = store.query(node_type=node_type, limit=2)
        assert _listed(rows) == _expected(newest[:2]), node_type
    for node_id in DUPLICATED:
        for name in (f"{node_id} original", f"{node_id} copy"):
            rows = store.query(properties={"name": name})
            found = [node_id] if LIST_SHOWN[node_id][0] == name else []
            assert _listed(rows) == _expected(found), name


def check_execute_node_query_shows_one_version(store: Any) -> None:
    """``execute_node_query`` lists each node once, as ``get_node`` shows it.

    Newest first, as ``query`` does: a ``limit`` counts nodes, and a type or
    property filter matches the version shown, never the hidden row.
    """
    both_valid = _write_listed_nodes(store)
    for as_of in (None, both_valid):
        rows = store.execute_node_query(NodeQuery(as_of=as_of))
        assert _listed(rows) == _expected(NEWEST_FIRST), as_of
    for limit in range(1, len(NEWEST_FIRST) + 1):
        rows = store.execute_node_query(NodeQuery(limit=limit))
        assert _listed(rows) == _expected(NEWEST_FIRST[:limit]), limit
    for node_type in NODE_TYPES:
        newest = _newest_of(node_type)
        typed = (FilterClause("node_type", "eq", node_type),)
        rows = store.execute_node_query(NodeQuery(filters=typed))
        assert _listed(rows) == _expected(newest), node_type
        rows = store.execute_node_query(NodeQuery(filters=typed, limit=2))
        assert _listed(rows) == _expected(newest[:2]), node_type
    by_id = (FilterClause("node_id", "in", DUPLICATED),)
    rows = store.execute_node_query(NodeQuery(filters=by_id))
    duplicated_newest = [node_id for node_id in NEWEST_FIRST if node_id in DUPLICATED]
    assert _listed(rows) == _expected(duplicated_newest), rows
    for node_id in DUPLICATED:
        for name in (f"{node_id} original", f"{node_id} copy"):
            named = (FilterClause("properties.name", "eq", name),)
            rows = store.execute_node_query(NodeQuery(filters=named))
            found = [node_id] if LIST_SHOWN[node_id][0] == name else []
            assert _listed(rows) == _expected(found), name


def check_filtered_listing_as_of_shows_the_version_then(store: Any) -> None:
    """A filtered listing ``as_of`` a past instant shows the version valid then.

    ``dup-history`` is ``kind-a`` until it is rewritten as ``kind-b`` and
    given a second current row. At an instant before that only its first
    row is valid, so a filter that row passes lists it, after the newer
    plain node, and never one of the later rows, even when they pass too.
    """
    store.upsert_node("dup-history", "kind-a", {"name": "dup-history then"})
    store.upsert_node("plain-then", "kind-a", {"name": "plain then"})
    time.sleep(0.005)
    then = datetime.now(UTC)
    time.sleep(0.005)
    store.upsert_node("dup-history", "kind-b", {"name": "dup-history original"})
    add_current_row(
        store,
        "dup-history",
        name="dup-history copy",
        shift=LATER,
        greater_version_id=False,
    )
    expected = [
        ("plain-then", ("plain then", "kind-a")),
        ("dup-history", ("dup-history then", "kind-a")),
    ]
    assert _listed(store.query(node_type="kind-a", as_of=then)) == expected
    for clause in (
        FilterClause("node_type", "eq", "kind-a"),
        FilterClause("node_type", "in", NODE_TYPES),
    ):
        rows = store.execute_node_query(NodeQuery(filters=(clause,), as_of=then))
        assert _listed(rows) == expected, clause


#: Duplicates for the write checks: ``(node_id, shift, greater_version_id,
#: created_at_shift, copy_is_shown)``. The copy is shown for a later stamp
#: with the smaller ``version_id``, or an equal stamp with the greater; the
#: original is shown against an earlier stamp with the greater. The equal
#: stamps get an earlier ``created_at`` so the two rows still differ in it.
WRITE_DUPLICATES = (
    ("dup-later", LATER, False, None, True),
    ("dup-earlier", EARLIER, True, None, False),
    ("dup-tie", SAME, True, EARLIER, True),
)


def _row_name(row: dict[str, Any]) -> str:
    return str(json.loads(row["properties_json"])["name"])


def _instant(stamp: Any) -> datetime:
    """A stored stamp as a datetime, however the engine spells it.

    ArcadeDB returns a stamp written from a parameter as it was sent, and
    one carried over in Cypher (``created_at``) in its ``Z`` spelling.
    """
    return datetime.fromisoformat(str(stamp))


def _current_rows(store: Any, node_id: str) -> list[dict[str, Any]]:
    """Every current row of *node_id*, as stored."""
    with store._driver.session(database=store._database) as session:
        records = session.run(
            "MATCH (n:Node {node_id: $node_id}) WHERE n.valid_to IS NULL "
            "RETURN n {.*, embedding: null} AS n",
            node_id=node_id,
        ).data()
    return [record["n"] for record in records]


def _duplicated(
    store: Any,
    node_id: str,
    *,
    shift: timedelta,
    greater_version_id: bool,
    created_at_shift: timedelta | None,
    copy_is_shown: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Give *node_id* two current rows; return the shown row, then the hidden.

    The two rows differ in ``created_at``, the field a write carries over.
    """
    store.upsert_node(node_id, "kind-a", {"name": f"{node_id} original"})
    add_current_row(
        store,
        node_id,
        name=f"{node_id} copy",
        shift=shift,
        greater_version_id=greater_version_id,
        created_at_shift=created_at_shift,
    )
    rows = {_row_name(row): row for row in _current_rows(store, node_id)}
    shown_name = f"{node_id} copy" if copy_is_shown else f"{node_id} original"
    assert store.get_node(node_id)["properties"]["name"] == shown_name
    shown = rows.pop(shown_name)
    (hidden,) = rows.values()
    assert _instant(shown["created_at"]) != _instant(hidden["created_at"]), node_id
    return shown, hidden


def _all_duplicated(store: Any) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    return {
        node_id: _duplicated(
            store,
            node_id,
            shift=shift,
            greater_version_id=greater,
            created_at_shift=created_at_shift,
            copy_is_shown=copy_is_shown,
        )
        for node_id, shift, greater, created_at_shift, copy_is_shown in (
            WRITE_DUPLICATES
        )
    }


def _assert_healed(
    store: Any,
    node_id: str,
    *,
    name: str,
    shown: dict[str, Any],
    hidden: dict[str, Any],
) -> None:
    """*node_id* has one current row, named *name*, that continues *shown*.

    The new row carries ``created_at`` over from the shown row, and the
    history holds both earlier rows closed where the new one starts.
    """
    current = _current_rows(store, node_id)
    assert [_row_name(row) for row in current] == [name], (node_id, current)
    (new,) = current
    assert _instant(new["created_at"]) == _instant(shown["created_at"]), (
        node_id,
        new,
        shown,
    )
    history = store.get_node_history(node_id)
    closed = sorted(
        (version["properties"]["name"], _instant(version["valid_to"]))
        for version in history
        if version["valid_to"] is not None
    )
    starts = _instant(new["valid_from"])
    expected = sorted((_row_name(row), starts) for row in (shown, hidden))
    assert closed == expected, (node_id, closed)
    assert len(history) == 3, (node_id, history)


def check_upsert_node_heals_a_duplicate(store: Any) -> None:
    """``upsert_node`` closes both current rows and writes one version."""
    for node_id, (shown, hidden) in _all_duplicated(store).items():
        name = f"{node_id} healed"
        assert store.upsert_node(node_id, "kind-a", {"name": name}) == node_id
        _assert_healed(store, node_id, name=name, shown=shown, hidden=hidden)


def check_upsert_nodes_bulk_heals_a_duplicate(store: Any) -> None:
    """``upsert_nodes_bulk`` heals duplicated nodes written beside plain ones.

    Each duplicate is built twice. One node gets new content; the other
    (``-hidden``) gets exactly its hidden row's content, which differs from
    the shown version and so is a write, not an unchanged no-op.
    """
    store.upsert_node("plain-old", "kind-a", {"name": "plain old"})
    duplicated = _all_duplicated(store)
    for node_id, shift, greater, created_at_shift, copy_is_shown in WRITE_DUPLICATES:
        duplicated[f"{node_id}-hidden"] = _duplicated(
            store,
            f"{node_id}-hidden",
            shift=shift,
            greater_version_id=greater,
            created_at_shift=created_at_shift,
            copy_is_shown=copy_is_shown,
        )
    names = {"plain-old": "plain old updated", "plain-new": "plain new"}
    for node_id, (_, hidden) in duplicated.items():
        names[node_id] = _row_name(hidden) if node_id.endswith("-hidden") else node_id
    written = store.upsert_nodes_bulk(
        [
            {"node_id": node_id, "node_type": "kind-a", "properties": {"name": name}}
            for node_id, name in names.items()
        ]
    )
    assert written == list(names)
    for node_id, (shown, hidden) in duplicated.items():
        _assert_healed(store, node_id, name=names[node_id], shown=shown, hidden=hidden)
    for node_id in ("plain-old", "plain-new"):
        rows = _current_rows(store, node_id)
        assert [_row_name(row) for row in rows] == [names[node_id]], node_id


def check_update_node_if_current_heals_a_duplicate(store: Any) -> None:
    """The compare-and-set checks the shown version and closes both rows.

    A token naming the hidden row is refused and writes nothing. (With
    equal stamps the two rows share one token, which names the shown row.)
    """
    for node_id, (shown, hidden) in _all_duplicated(store).items():
        if _instant(hidden["valid_from"]) != _instant(shown["valid_from"]):
            refused = store.update_node_if_current(
                node_id,
                str(hidden["valid_from"]),
                "kind-a",
                {"name": "via the hidden token"},
                node_role="semantic",
            )
            assert refused is False, node_id
            rows = sorted(_row_name(row) for row in _current_rows(store, node_id))
            assert rows == sorted(_row_name(row) for row in (shown, hidden))
        token = store.get_node(node_id)["valid_from"]
        name = f"{node_id} healed"
        written = store.update_node_if_current(
            node_id, token, "kind-a", {"name": name}, node_role="semantic"
        )
        assert written is True, node_id
        _assert_healed(store, node_id, name=name, shown=shown, hidden=hidden)
