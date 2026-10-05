"""Bolt graph reads of a ``node_id`` that has two current rows.

Shared by ``test_neo4j_graph.py`` and ``test_arcadedb_graph.py``. The Bolt
backends hold "at most one current row per ``node_id``" in the
close-then-insert transaction, not in the database, so concurrent writers
can leave two (the module docstring of
``trellis.stores.bolt_opencypher.graph``). Each check writes the second row
directly, as that race would, then requires ``get_node``, ``search_nodes``
in either direction and ``count_nodes_by_type`` to show one version of the
node: the one with the latest ``valid_from``, or the greater ``version_id``
when the stamps are equal.

The duplicates are built so that rival rules pick the other row. A later
stamp carries the smaller ``version_id`` and an earlier stamp the greater,
so ranking by ``version_id`` first fails. A duplicate's ``created_at`` moves
with its ``valid_from``, as when both writers create a node's first
version, so keeping the first row in ``created_at`` order fails in one sort
direction.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from trellis.stores.base.graph import NODE_SEARCH_SORTS

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
