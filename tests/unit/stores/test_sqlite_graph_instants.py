"""``SQLiteGraphStore`` compares a time bound by instant, in any zone.

Every ``valid_from``, ``valid_to`` and ``created_at`` the store writes is
``utc_now().isoformat()``, so the columns hold one offset and need no generated
column. What arrives from outside is the bound: ``as_of`` on every read and
``before`` on compaction are caller datetimes, and the text of one in another
zone (or none) sorts by its own digits against a UTC stamp. These tests pin both
halves: the stored side stays UTC, and a bound compares by its instant.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from trellis.stores.sqlite.graph import SQLiteGraphStore

_BEHIND = timezone(timedelta(hours=-11))
_AHEAD = timezone(timedelta(hours=14))

#: v1 is current in [T1, T2), v2 from T2. T1 has no fraction, T2 has one, so
#: both stamp shapes ``isoformat()`` writes are on the page.
_T1 = datetime(2026, 3, 4, 5, 0, 0, tzinfo=UTC)
_T2 = datetime(2026, 3, 4, 20, 0, 0, 250000, tzinfo=UTC)
_TICK = timedelta(microseconds=1)

#: ``as_of`` -> the version it must see (None: nothing yet), as an instant.
_CASES = [
    (_T1 - _TICK, None),
    (_T1, 1),
    (_T2 - _TICK, 1),
    (_T2, 2),
    (_T2 + _TICK, 2),
    (_T1 + timedelta(hours=6), 1),
    (_T2 + timedelta(hours=3), 2),
]


def _zones(moment: datetime) -> list[tuple[str, datetime]]:
    return [
        ("utc", moment),
        ("behind", moment.astimezone(_BEHIND)),
        ("ahead", moment.astimezone(_AHEAD)),
        ("naive", moment.replace(tzinfo=None)),
    ]


@pytest.fixture
def store(tmp_path: Path):
    s = SQLiteGraphStore(tmp_path / "graph.db")
    with patch("trellis.stores.sqlite.graph.utc_now", return_value=_T1):
        s.upsert_node("a", "service", {"v": 1})
        s.upsert_node("b", "service", {"v": 1})
        s.upsert_edge("a", "b", "depends_on", {"v": 1})
        s.upsert_alias("a", "sys", "raw-a")
    with patch("trellis.stores.sqlite.graph.utc_now", return_value=_T2):
        s.upsert_node("a", "service", {"v": 2})
        s.upsert_edge("a", "b", "depends_on", {"v": 2})
        s.upsert_alias("a", "sys", "raw-a")
    yield s
    s.close()


def _node_version(store: SQLiteGraphStore, as_of: datetime) -> int | None:
    node = store.get_node("a", as_of=as_of)
    return None if node is None else node["properties"]["v"]


def _bulk_version(store: SQLiteGraphStore, as_of: datetime) -> int | None:
    (found,) = list(store.get_nodes_bulk(["a"], as_of=as_of)) or [None]
    return None if found is None else found["properties"]["v"]


def _edge_version(store: SQLiteGraphStore, as_of: datetime) -> int | None:
    (edge,) = store.get_edges("a", direction="outgoing", as_of=as_of) or [None]
    return None if edge is None else edge["properties"]["v"]


def _query_version(store: SQLiteGraphStore, as_of: datetime) -> int | None:
    nodes = [
        n for n in store.query(node_type="service", as_of=as_of) if n["node_id"] == "a"
    ]
    return None if not nodes else nodes[0]["properties"]["v"]


def _subgraph_version(store: SQLiteGraphStore, as_of: datetime) -> int | None:
    sub = store.get_subgraph(["a"], depth=1, as_of=as_of)
    nodes = [n for n in sub["nodes"] if n["node_id"] == "a"]
    return None if not nodes else nodes[0]["properties"]["v"]


def _alias_seen(store: SQLiteGraphStore, as_of: datetime) -> int | None:
    # The alias row is rewritten at T2; "seen" is 1 before it and 2 from it.
    versions = store.resolve_alias("sys", "raw-a", as_of=as_of)
    return (
        None
        if versions is None
        else (2 if versions["valid_from"] >= _T2.isoformat() else 1)
    )


_READS: dict[str, Callable[[SQLiteGraphStore, datetime], int | None]] = {
    "get_node": _node_version,
    "get_nodes_bulk": _bulk_version,
    "get_edges": _edge_version,
    "query": _query_version,
    "get_subgraph": _subgraph_version,
    "resolve_alias": _alias_seen,
}


@pytest.mark.parametrize("read", sorted(_READS))
def test_as_of_selects_the_version_valid_at_that_instant_in_any_zone(
    store: SQLiteGraphStore, read: str
) -> None:
    fn = _READS[read]
    checked = 0
    for instant, want in _CASES:
        for zone, as_of in _zones(instant):
            assert fn(store, as_of) == want, (read, zone, instant.isoformat())
            checked += 1
    assert checked == len(_CASES) * 4


def test_every_stored_stamp_is_utc_so_only_the_bound_needs_normalising(
    store: SQLiteGraphStore,
) -> None:
    """The premise the bound-only fix rests on. A writer that stamped another
    offset would need the column, not just the bound, and this test names it."""
    with patch("trellis.stores.sqlite.graph.utc_now", return_value=_T2):
        store.upsert_nodes_bulk(
            [
                {"node_id": "c", "node_type": "service", "properties": {"v": 1}},
                {"node_id": "a", "node_type": "service", "properties": {"v": 3}},
            ]
        )
        store.upsert_edges_bulk(
            [
                {
                    "source_id": "a",
                    "target_id": "c",
                    "edge_type": "depends_on",
                    "properties": {},
                }
            ]
        )
    store.upsert_node("live", "service", {})  # the real ``utc_now``
    utc_text = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{6})?\+00:00$")
    stamps = 0
    for table, columns in (
        ("nodes", ("created_at", "updated_at", "valid_from", "valid_to")),
        ("edges", ("created_at", "valid_from", "valid_to")),
        ("entity_aliases", ("created_at", "updated_at", "valid_from", "valid_to")),
    ):
        for column in columns:
            for (value,) in store._conn.execute(
                f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"  # noqa: S608
            ):
                assert utc_text.match(value), (table, column, value)
                stamps += 1
    assert stamps >= 20


def test_a_naive_as_of_is_read_as_utc_and_is_inclusive(
    store: SQLiteGraphStore,
) -> None:
    # ``valid_from == as_of`` is visible: the instant a version starts is in it.
    assert _node_version(store, _T1.replace(tzinfo=None)) == 1
    assert _node_version(store, _T2.replace(tzinfo=None)) == 2


class TestCompactVersions:
    """``compact_versions(before)`` deletes closed versions: a ``before`` in
    another zone must cut at its instant, not at the digits of its offset."""

    def test_a_cutoff_in_another_zone_cuts_at_its_instant(
        self, store: SQLiteGraphStore
    ) -> None:
        # Node ``a`` v1 closed at T2 (20:00:00.25Z); so did the edge and alias.
        closed = 3
        for zone, before in _zones(_T2 + _TICK):
            report = store.compact_versions(before, dry_run=True)
            assert (
                report.nodes_compacted
                + report.edges_compacted
                + report.aliases_compacted
                == closed
            ), zone
        for zone, before in _zones(_T2):
            report = store.compact_versions(before, dry_run=True)
            assert (
                report.nodes_compacted
                + report.edges_compacted
                + report.aliases_compacted
                == 0
            ), zone

    def test_a_cutoff_just_before_the_close_keeps_the_history(
        self, store: SQLiteGraphStore
    ) -> None:
        # 19:00Z written in +14 is 09:00 the next day: past every digit of the
        # stored 20:00Z, so a text comparison deleted history it should keep.
        cutoff = datetime(2026, 3, 4, 19, 0, tzinfo=UTC).astimezone(_AHEAD)
        store.compact_versions(cutoff)
        assert len(store.get_node_history("a")) == 2
        assert _node_version(store, _T1) == 1

    def test_a_cutoff_after_the_close_deletes_it(self, store: SQLiteGraphStore) -> None:
        # 21:00Z written in -11 is 10:00 the same day: below every digit of the
        # stored 20:00Z, so a text comparison kept history it should delete.
        cutoff = datetime(2026, 3, 4, 21, 0, tzinfo=UTC).astimezone(_BEHIND)
        report = store.compact_versions(cutoff)
        assert report.nodes_compacted == 1
        assert report.before == cutoff
        assert len(store.get_node_history("a")) == 1
