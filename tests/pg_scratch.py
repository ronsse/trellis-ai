"""Refuse to run a state-wiping fixture against a database that holds data.

Eight test modules read ``TRELLIS_TEST_PG_DSN`` and immediately ``TRUNCATE``
or ``DROP`` whatever it points at. Nothing checked what that was. On this
project's own reference host the only Postgres is production, on a port one
digit away from the default, so a mistyped or inherited env var destroys the
live stores — and the suite would report a pass.

The convention that stands in for a control today is "spin up a throwaway
``postgres:16``", written down in ``docs/design/swarm-handoff.md``.
A convention is not a control.

**What this refuses, and why it is not a flag.** An opt-in
(``..._ALLOW_DESTRUCTIVE=1``) is set once by CI and once by a human's shell
profile, after which the control is off forever — the same shape as the
defects this repo keeps finding, where the safe behaviour is asserted and
enforced by nobody. So the claim is recorded *in the database being
destroyed*: a marker table this module mints, and only ever into a database
that has **no user tables at all**.

That gives the three answers the guard needs, from one census:

===============================  =========================================
database state                   verdict
===============================  =========================================
marker table present             allowed — this database was claimed as
                                 scratch by a previous run
no user tables at all            claim it, then allow — a fresh container
                                 has nothing to lose
tables, but no marker            **refused** — somebody's real store
===============================  =========================================

A production database can never be claimed, because claiming requires the
database to be empty. A CI service container and a throwaway
``postgres:16`` are empty on first contact and carry the marker forever
after, so the guard costs one round trip per process and no configuration.

**Why not the port check the issue proposed.** #405 suggests refusing a DSN
that names port 5433. That refuses one host's production and nothing else:
it passes for a second Postgres on 5432, for the same database reached as
``localhost`` versus ``127.0.0.1`` versus a container name, and — measured
below — for the *other* production database on the very port it names. The
census asks what is in the database instead, so the answer does not depend
on how the database was spelled.

Measured against the reference host on 2026-09-23, every database on that
server, through the shipped SQL::

    dbname=trellis_knowledge     5 tables   REFUSED
    dbname=trellis_operational   2 tables   REFUSED
    dbname=litellm              66 tables   REFUSED
    dbname=postgres              0 tables   CLAIMABLE

The residual hole is stated rather than hidden, and the last row is it: a
database that is simultaneously *production* and *completely empty* will be
claimed. The maintenance database on the production server is exactly that
shape. It is benign for the reason the rule is written this way — there is
nothing in it to lose — and the next run is refused if anything has been
written since. What the row is not is hypothetical, which is why it is
recorded as output rather than as a caveat.

``TRELLIS_TEST_PG_ALLOW_DESTRUCTIVE=1`` remains as an escape hatch for the
one case the census cannot serve — a deliberately populated non-production
fixture database. It is an override, not the mechanism.

Nothing here may print the DSN. A DSN carries a password, and a refusal
message is the one part of this module an operator is guaranteed to read;
:func:`describe_dsn` renders host/port/dbname and nothing else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum

#: The env var every Postgres-backed test reads. Declared here so the
#: modules that destroy state take the DSN from :func:`scratch_dsn` and
#: cannot reach the environment behind the guard's back.
TEST_DSN_ENV = "TRELLIS_TEST_PG_DSN"

#: Escape hatch. An override, never the mechanism — see the module docstring.
OVERRIDE_ENV = "TRELLIS_TEST_PG_ALLOW_DESTRUCTIVE"

#: The claim. Deliberately verbose: an operator who finds this table in a
#: database is owed an explanation of how it got there.
MARKER_TABLE = "trellis_scratch_database_marker"

#: Stored in the marker row. The table name alone does not say what was
#: claimed or how to undo it, and this is the only note an operator who
#: stumbles on the table will find.
_CLAIM_NOTE = (
    "Claimed as a scratch database by the Trellis test suite, which had "
    "found it empty. The Postgres suites TRUNCATE and DROP tables here. "
    "Drop this table to revoke the claim."
)


class Verdict(StrEnum):
    """What the census says about a database."""

    #: The marker is present — a previous run claimed this database.
    CLAIMED = "claimed"
    #: No user tables at all. Mint the marker, then proceed.
    CLAIMABLE = "claimable"
    #: Tables exist and none of them is the marker. Refuse.
    REFUSED = "refused"
    #: ``TRELLIS_TEST_PG_ALLOW_DESTRUCTIVE`` was set.
    OVERRIDDEN = "overridden"


@dataclass(frozen=True)
class Census:
    """What a database looks like to the guard.

    ``tables`` is every non-system, non-extension-owned table. It is the
    whole input: ``marker_present`` is derived from it rather than passed
    alongside, so the two cannot disagree.
    """

    tables: frozenset[str]

    @property
    def marker_present(self) -> bool:
        return MARKER_TABLE in self.tables

    @property
    def is_empty(self) -> bool:
        return not self.tables


def classify(census: Census, *, override: bool = False) -> Verdict:
    """Decide whether a destructive fixture may run against this database.

    Pure: no connection, no environment, no import of psycopg. The policy
    is separable from the I/O so it can be tested exhaustively on every
    PR, not only on the ``live-infra`` legs where a Postgres exists.

    ``override`` is checked first and reported as its own verdict rather
    than folded into ``CLAIMED``, so a run that proceeded because someone
    set an env var is distinguishable in a log from one that proceeded
    because the database was actually scratch.
    """
    if override:
        return Verdict.OVERRIDDEN
    if census.marker_present:
        return Verdict.CLAIMED
    if census.is_empty:
        return Verdict.CLAIMABLE
    return Verdict.REFUSED


def override_enabled(environ: object = None) -> bool:
    """True when the escape hatch is set to something truthy.

    ``"0"`` / ``"false"`` / ``""`` are off, so unsetting the variable and
    setting it to zero mean the same thing — an operator who writes
    ``=0`` to disable it is not silently overriding.
    """
    env = os.environ if environ is None else environ
    raw = env.get(OVERRIDE_ENV, "")  # type: ignore[union-attr]
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def describe_dsn(dsn: str) -> str:
    """Render a DSN as host/port/dbname, with no password and no user.

    A DSN carries a credential and this string goes into a refusal that
    lands in CI logs, terminal scrollback and pasted issue comments. So the
    three fields are picked out by name and everything else is dropped;
    nothing here ever echoes the input.

    **No psycopg.** The first cut delegated to ``conninfo_to_dict`` under a
    blanket ``except Exception``, which meant that in an environment without
    psycopg — the repo's own lint/type venv, and any CI leg that installs
    without the cloud extra — *every* DSN rendered as ``"(unparseable
    DSN)"``. That is this project's recurring shape exactly: an exception
    guard collapsing "the library is absent" into "the value is bad", so the
    refusal message loses the one detail an operator needs and does it
    silently. Both spellings libpq accepts are a few lines of string
    handling, and a redaction helper that degrades in the environment where
    it is most needed is worse than none.

    Parsing stays best-effort: anything not recognised yields
    ``"(unparseable DSN)"`` rather than a fragment of the original, because
    the failure mode of a partial parse is leaking the part that was not
    understood.
    """
    dsn = dsn.strip()
    if not dsn:
        return "(unparseable DSN)"

    host = port = dbname = None

    scheme, _, _ = dsn.partition("://")
    if scheme in ("postgresql", "postgres") and "://" in dsn:
        from urllib.parse import urlsplit

        try:
            parts = urlsplit(dsn)
            # ``hostname`` takes the text after the last ``@``, so a
            # password containing one cannot bleed into it; neither
            # accessor can return the credential, but both raise on a
            # malformed netloc or a non-numeric port.
            host = parts.hostname
            port = parts.port
        except ValueError:
            return "(unparseable DSN)"
        dbname = parts.path.lstrip("/").split("?")[0] or None
    elif "=" in dsn:
        # Keyword form: ``host=h port=5432 dbname=db user=u password=pw``.
        # Read by allow-list, so a key this function does not know about
        # cannot reach the output whatever it holds.
        wanted = {"host": None, "port": None, "dbname": None}
        try:
            import shlex

            tokens = shlex.split(dsn)
        except ValueError:
            return "(unparseable DSN)"
        for token in tokens:
            key, sep, value = token.partition("=")
            if sep and key in wanted:
                wanted[key] = value
        host, port, dbname = wanted["host"], wanted["port"], wanted["dbname"]
    else:
        return "(unparseable DSN)"

    return (
        f"host={host or '(default host)'} "
        f"port={port or '(default port)'} "
        f"dbname={dbname or '(default dbname)'}"
    )


#: Every non-system table not owned by an extension.
#:
#: The ``pg_depend`` clause is what keeps ``CREATE EXTENSION vector`` from
#: making a fresh pgvector container look populated: extension-owned
#: objects carry a ``deptype='e'`` dependency and are not the user's data.
_CENSUS_SQL = """
SELECT c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_%'
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend d
      WHERE d.objid = c.oid AND d.deptype = 'e'
  )
"""

_REFUSAL = """\
Refusing to run a state-wiping test fixture against a database that holds data.

  {where}

This database has {count} table(s) and no {marker!r} marker, so it is not a
scratch database. The Postgres suites TRUNCATE and DROP tables outright;
running them here would destroy whatever is in it.

Point {env} at a throwaway server:

  docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=postgres \\
      --name trellis-test-pg pgvector/pgvector:pg16
  export {env}='postgresql://postgres:postgres@localhost:55432/postgres'

An empty database is claimed automatically on first use and never asks
again. If this database really is a disposable fixture that happens to
hold data, set {override}=1.
"""

#: One census per DSN per process, with the refusal text beside it. The
#: verdict cannot change mid-run without someone else writing to the
#: database, and re-censusing on each of a contract suite's 100 fixtures
#: would be a round trip apiece. The message is cached with it so every
#: test in a refused run gets the actionable text, not only the first.
_verdicts: dict[str, Verdict] = {}
_refusals: dict[str, str] = {}


def _census(dsn: str) -> Census:
    """Connect and list the user tables. Connection errors propagate.

    A server that is unreachable is the caller's problem, and its own
    error is far more useful than anything this module could say about
    it. What must never propagate as *success* is a census that fails
    after connecting — an exception there reaches the fixture and the
    truncate never runs, which is the direction this guard fails in.
    """
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(_CENSUS_SQL)
        return Census(frozenset(row[0] for row in cur.fetchall()))


def _claim(dsn: str) -> None:
    """Mint the marker table into an empty database."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {MARKER_TABLE} ("
            "  claimed_at timestamptz NOT NULL DEFAULT now(),"
            "  note text NOT NULL"
            ")"
        )
        cur.execute(
            f"INSERT INTO {MARKER_TABLE} (note) VALUES (%s)",  # noqa: S608
            (_CLAIM_NOTE,),
        )


def _resolve(dsn: str) -> Verdict:
    """Census the database once and remember the answer."""
    if override_enabled():
        return Verdict.OVERRIDDEN
    census = _census(dsn)
    verdict = classify(census)
    if verdict is Verdict.CLAIMABLE:
        _claim(dsn)
    elif verdict is Verdict.REFUSED:
        _refusals[dsn] = _REFUSAL.format(
            where=describe_dsn(dsn),
            count=len(census.tables),
            marker=MARKER_TABLE,
            env=TEST_DSN_ENV,
            override=OVERRIDE_ENV,
        )
    return verdict


def require_scratch_database(dsn: str) -> None:
    """Fail the test unless *dsn* names a database the suite may destroy.

    Called by every fixture that truncates or drops. Cached per DSN, so
    the cost is one connection per process however many fixtures ask —
    and a refused DSN keeps failing rather than being censused again.
    """
    import pytest

    verdict = _verdicts.get(dsn)
    if verdict is None:
        verdict = _verdicts[dsn] = _resolve(dsn)
    if verdict is Verdict.REFUSED:
        pytest.fail(_refusals[dsn], pytrace=False)


def configured_dsn() -> str:
    """The raw env var, for ``skipif`` markers. Reads nothing, destroys nothing.

    Separate from :func:`scratch_dsn` on purpose: a module-level
    ``skipif`` runs at import, and opening a connection at import time
    would make collection depend on a live server.
    """
    return os.environ.get(TEST_DSN_ENV, "")


def scratch_dsn() -> str:
    """The DSN, checked. What a destructive fixture calls.

    Never call this at module scope — it connects.
    """
    dsn = configured_dsn()
    require_scratch_database(dsn)
    return dsn
