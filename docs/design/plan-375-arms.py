#!/usr/bin/env python3
"""Re-derive every number in ``plan-375-graph-candidates.md``.

Read-only. Reproduces the graph axis's candidate window exactly as
production builds it, and replays candidate-selection arms against each
recorded pack's *own* budget.

    export TRELLIS_KNOWLEDGE_PG_DSN=... TRELLIS_OPERATIONAL_PG_DSN=...
    python docs/design/plan-375-arms.py

``trellis-skynet`` already exports both DSNs; source them from it rather
than pasting a password. Requires ``psycopg`` (a core dependency).

The window model, which is the thing an earlier draft of the plan got
wrong (it assumed 80/20):

    limit  = max(20, PACK_ASSEMBLED.budget_max_items)  mcp/server.py:752
    scan   = limit * 4                                 _GRAPH_RECENCY_OVERFETCH
    served = <structural / unconfirmed filter>[:limit] GraphSearch.search

``nodes[:limit]`` is applied *after* the client-side filters, so the cut
that decides servability is post-filter rank, not store-side row number.
"""

from __future__ import annotations

import collections
import os
import statistics
import sys

try:
    import psycopg
except ImportError:  # pragma: no cover - operator-facing script
    sys.exit("psycopg not importable; run under the repo venv")

GRAPH_RECENCY_OVERFETCH = 4
DEFAULT_LIMIT = 20

#: The three gotcha nodes carrying 22 of the 31 cited-helpful graph servings.
TARGETS = (
    "uv run rewrites uv.lock as a side effect in trellis-ai",
    "trellis-ai make lint/format/test need the venv on PATH",
    "trellis-ai contract suites: only SQLite runs on PRs, pgvector never runs at all",
)

#: Fixed projection shared by both node reads. A module constant, never
#: caller input — the two queries below interpolate it and nothing else.
NODE_COLS = (
    "node_id, node_role, node_type, properties->>'name', "
    "properties->>'extraction_status', created_at, "
    "jsonb_array_length(COALESCE(document_ids,'[]'::jsonb)) > 0"
)
# NODE_COLS is a module constant, never caller input; S608 is a false
# positive on both, and the projection is interpolated rather than repeated
# so the two reads cannot drift apart.
SQL_CURRENT = f"SELECT {NODE_COLS} FROM nodes WHERE valid_to IS NULL"  # noqa: S608
SQL_AS_OF = (
    f"SELECT {NODE_COLS} FROM nodes "  # noqa: S608
    "WHERE valid_from <= %s AND (valid_to IS NULL OR valid_to > %s)"
)
ID, ROLE, TYPE, NAME, STATUS, CREATED, DOCLINKED = range(7)


def is_cron(name: str | None) -> bool:
    """A per-invocation cron record, e.g. ``cli.worker.curate.learning@<ts>``."""
    return bool(name) and name.startswith("cli.") and "@" in name


def served_window(
    rows: list[tuple],
    limit: int,
    *,
    suppress_cron: bool = False,
) -> tuple[list[tuple], list[tuple]]:
    """Return (served, post_filter_list) for a candidate population."""
    scan = sorted(rows, key=lambda r: r[CREATED], reverse=True)[
        : limit * GRAPH_RECENCY_OVERFETCH
    ]
    keep = [
        r
        for r in scan
        if r[ROLE] != "structural"
        and r[STATUS] != "unconfirmed"
        and not (suppress_cron and is_cron(r[NAME]))
    ]
    return keep[:limit], keep


def main() -> None:
    kdsn = os.environ.get("TRELLIS_KNOWLEDGE_PG_DSN")
    odsn = os.environ.get("TRELLIS_OPERATIONAL_PG_DSN")
    if not kdsn or not odsn:
        sys.exit("set TRELLIS_KNOWLEDGE_PG_DSN and TRELLIS_OPERATIONAL_PG_DSN")

    with psycopg.connect(odsn) as oc, oc.cursor() as cur:
        cur.execute(
            "SELECT occurred_at, payload FROM events "
            "WHERE event_type='pack.assembled' ORDER BY occurred_at"
        )
        packs = cur.fetchall()
        cur.execute(
            "SELECT payload FROM events WHERE event_type='feedback.recorded'"
        )
        helpful = {
            i
            for (p,) in cur.fetchall()
            for i in (p.get("helpful_item_ids") or [])
        }

    conn = psycopg.connect(kdsn)

    def as_of(t: object) -> list[tuple]:
        with conn.cursor() as cur:
            cur.execute(SQL_AS_OF, (t, t))
            return cur.fetchall()

    print(f"packs={len(packs)}  distinct helpful item ids={len(helpful)}")
    print(
        "budget_max_items:",
        collections.Counter(p.get("budget_max_items") for _, p in packs).most_common(),
    )

    # --- live window: where the three cited gotchas actually sit today -----
    with conn.cursor() as cur:
        cur.execute(SQL_CURRENT)
        live = cur.fetchall()
    print("\n=== live window (limit=50 => served iff post-filter rank < 50)")
    for suppress in (False, True):
        served, keep = served_window(live, 50, suppress_cron=suppress)
        rank = {r[NAME]: i for i, r in enumerate(keep) if r[NAME] in TARGETS}
        print(
            f"  suppress_cron={suppress!s:5s} "
            f"gotchas_served={sum(1 for r in served if r[TYPE] == 'gotcha')} "
            f"doclinked_served={sum(1 for r in served if r[DOCLINKED])} "
            f"cron_slots={sum(1 for r in served if is_cron(r[NAME]))}"
        )
        for t in TARGETS:
            r = rank.get(t)
            print(f"      rank={r if r is not None else '>scan':>6}  {t[:58]!r}")

    # --- arms, each pack at its own budget ---------------------------------
    arms: collections.Counter[str] = collections.Counter()
    total = 0
    cron_slots: list[int] = []
    for t, p in packs:
        limit = max(DEFAULT_LIMIT, p.get("budget_max_items") or DEFAULT_LIMIT)
        rows = as_of(t)
        status_quo, _ = served_window(rows, limit)
        suppressed, _ = served_window(rows, limit, suppress_cron=True)
        doclinked = sorted(
            (r for r in rows if r[DOCLINKED] and r[ROLE] != "structural"
             and r[STATUS] != "unconfirmed"),
            key=lambda r: r[CREATED],
            reverse=True,
        )
        cited = [
            it["item_id"]
            for it in (p.get("injected_items") or [])
            if it.get("strategy_source") == "graph" and it["item_id"] in helpful
        ]
        total += len(cited)
        cron_slots.append(sum(1 for r in status_quo if is_cron(r[NAME])))
        for label, ids in (
            ("A status quo", {r[ID] for r in status_quo}),
            ("B cron-suppressed", {r[ID] for r in suppressed}),
            ("C +knowledge window K=40", {r[ID] for r in status_quo}
             | {r[ID] for r in doclinked[:40]}),
            ("D +knowledge window K=all", {r[ID] for r in status_quo}
             | {r[ID] for r in doclinked}),
        ):
            arms[label] += sum(1 for i in cited if i in ids)

    print(f"\n=== recall of the {total} cited-helpful graph servings")
    for label, hits in arms.items():
        print(f"  {label:28s} {hits:3d}/{total} = {hits / max(1, total):.0%}")
    print(
        f"\ncron slots in the served window: median {statistics.median(cron_slots)}, "
        f"last 8 packs {cron_slots[-8:]}"
    )

    _partitions(conn, packs, live, helpful)


def _partitions(
    conn: psycopg.Connection,
    packs: list[tuple],
    live: list[tuple],
    helpful: set[str],
) -> None:
    """Print the doc-linked partition, edge types, and the §2.5 table."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT node_type, count(*) FROM nodes WHERE valid_to IS NULL "
            "AND jsonb_array_length(COALESCE(document_ids,'[]'::jsonb)) > 0 "
            "GROUP BY 1 ORDER BY 2 DESC"
        )
        print("\ndoc-linked partition:", cur.fetchall())
        cur.execute(
            "SELECT edge_type, count(*) FROM edges WHERE valid_to IS NULL GROUP BY 1"
        )
        edges = cur.fetchall()
        print("edge types:", edges, "total", sum(n for _, n in edges))

    meta = {r[ID]: (r[TYPE], r[DOCLINKED]) for r in live}
    part: collections.Counter = collections.Counter()
    part_cited: collections.Counter = collections.Counter()
    for _, p in packs:
        for it in p.get("injected_items") or []:
            if it.get("strategy_source") != "graph":
                continue
            m = meta.get(it["item_id"])
            if m is None:
                continue
            key = ("doc-linked" if m[1] else "not-doc-linked", m[0] == "gotcha")
            part[key] += 1
            if it["item_id"] in helpful:
                part_cited[key] += 1
    print("\n=== graph servings by partition (doc-linked, is-gotcha)")
    for key in sorted(part):
        n, c = part[key], part_cited[key]
        print(
            f"  {key[0]:15s} gotcha={key[1]!s:5s} "
            f"serv={n:3d} cited={c:3d} rate={c / n:.3f}"
        )


if __name__ == "__main__":
    main()
