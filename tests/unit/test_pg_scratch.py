"""The scratch-database guard's policy, tested without a database.

Everything here is pure: :func:`classify` decides from a census,
:func:`override_enabled` reads one string, :func:`describe_dsn` redacts one.
The I/O half (``_census``, ``_claim``) needs a live Postgres and is
exercised by the suites the guard protects — which is the right split,
because the part that can be wrong *silently* is the policy, and the part
that can be wrong *loudly* is the connection.
"""

from __future__ import annotations

import pytest

from tests.pg_scratch import (
    MARKER_TABLE,
    Census,
    Verdict,
    classify,
    describe_dsn,
    override_enabled,
)


class TestCensus:
    def test_marker_present_is_the_claim(self) -> None:
        assert Census(frozenset({MARKER_TABLE})).marker_present
        assert not Census(frozenset({"nodes", "edges"})).marker_present

    def test_empty_means_no_user_tables(self) -> None:
        assert Census(frozenset()).is_empty
        assert not Census(frozenset({"nodes"})).is_empty

    def test_a_claimed_database_is_not_empty(self) -> None:
        """The two properties are independent, and the order they are read in
        matters: a claimed database has a table in it, so a ``classify`` that
        tested emptiness first would refuse every second run."""
        census = Census(frozenset({MARKER_TABLE}))
        assert census.marker_present
        assert not census.is_empty


class TestClassify:
    def test_claimed(self) -> None:
        assert classify(Census(frozenset({MARKER_TABLE}))) is Verdict.CLAIMED

    def test_claimed_even_beside_real_tables(self) -> None:
        """A scratch database fills up with Trellis tables after one run, and
        must stay claimed. This is the case that makes the marker worth
        minting at all rather than re-testing emptiness every time."""
        census = Census(frozenset({MARKER_TABLE, "nodes", "edges", "documents"}))
        assert classify(census) is Verdict.CLAIMED

    def test_empty_is_claimable(self) -> None:
        assert classify(Census(frozenset())) is Verdict.CLAIMABLE

    def test_populated_without_the_marker_is_refused(self) -> None:
        census = Census(frozenset({"nodes", "edges", "documents", "traces"}))
        assert classify(census) is Verdict.REFUSED

    def test_override_wins_over_every_state(self) -> None:
        """The escape hatch is unconditional by design — it exists for a
        disposable fixture that happens to hold data, which is exactly the
        state the census cannot distinguish from production."""
        for tables in (frozenset(), frozenset({"nodes"}), frozenset({MARKER_TABLE})):
            assert classify(Census(tables), override=True) is Verdict.OVERRIDDEN


class TestOverrideEnabled:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "anything"])
    def test_on(self, value: str) -> None:
        assert override_enabled({"TRELLIS_TEST_PG_ALLOW_DESTRUCTIVE": value})

    @pytest.mark.parametrize("value", ["", "0", "false", "FALSE", "no", "off"])
    def test_off(self, value: str) -> None:
        """``TRELLIS_TEST_PG_ALLOW_DESTRUCTIVE=0`` must not arm the override.

        An operator who sets a variable to ``0`` has said no. Reading any
        non-empty string as true is the common shortcut and it turns the
        one deliberate way to *disable* the hatch into the way to enable it.
        """
        assert not override_enabled({"TRELLIS_TEST_PG_ALLOW_DESTRUCTIVE": value})

    def test_unset(self) -> None:
        assert not override_enabled({})


class TestDescribeDsn:
    """A DSN carries a password, and this string goes into a failure message.

    ``pytest.fail`` output reaches CI logs, terminal scrollback and — on this
    project — pasted issue comments. Redaction here is the whole reason the
    refusal names a host and not a URL.
    """

    PASSWORD = "sup3rs3cr3t"  # noqa: S105 - the thing under test, not a credential
    DSN = f"postgresql://trellis:{PASSWORD}@127.0.0.1:5433/trellis"

    def test_names_enough_to_act_on(self) -> None:
        described = describe_dsn(self.DSN)
        assert "127.0.0.1" in described
        assert "5433" in described
        assert "trellis" in described

    def test_never_carries_the_password(self) -> None:
        assert self.PASSWORD not in describe_dsn(self.DSN)

    @pytest.mark.parametrize(
        "dsn",
        [
            "postgresql://u:pw@h:5432/db",
            "postgres://u:pw@h/db",
            "postgresql://u:pw@h:5432/db?sslmode=require",
            "host=h port=5432 dbname=db user=u password=pw",
            "",
            "not a dsn at all",
            "postgresql://",
            "postgresql://u:p%40w@h:5432/db",
        ],
    )
    def test_no_shape_leaks_a_secret(self, dsn: str) -> None:
        """Including the shapes that do not parse.

        A partial parse leaks the part that was not understood, so failure
        returns a fixed string rather than a fragment — the same reason
        ``path_is_present`` reads exactly one errno as absence.
        """
        described = describe_dsn(dsn)
        assert "pw" not in described
        assert "password" not in described.lower()
        assert "p%40w" not in described

    def test_unparseable_says_so(self) -> None:
        assert describe_dsn("not a dsn at all") == "(unparseable DSN)"
