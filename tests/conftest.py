"""Top-level pytest configuration: opt-in marker gating + hypothesis profile.

Markers ``live``, ``slow``, ``neo`` / ``neo4j``, ``arcadedb``,
``postgres``, and ``pgvector`` are excluded from the default ``pytest``
run via the ``-m "not ..."`` expression in
``[tool.pytest.ini_options].addopts``.

Each ``--include-<marker>`` CLI flag (with a ``TRELLIS_TEST_<MARKER>=1``
env-var equivalent) relaxes its corresponding ``not <marker>`` constraint
by rewriting the active ``-m`` expression before collection. This means a
default ``pytest`` run skips heavy / live-dependent tests, while CI and
opt-in local invocations can dial them back in selectively.

Why rewrite the ``-m`` expression instead of just unmarking nodes?
``addopts`` runs before ``pytest_collection_modifyitems`` and pytest's mark
filter is applied at collection. Removing markers from items inside that
hook fights the filter rather than working with it. Editing
``config.option.markexpr`` directly tells pytest "the user wants these
markers in" before the filter ever runs. See docs/agent-guide/testing.md
for the user-facing docs.

Also registers a fast ``hypothesis`` profile so property tests run in a
few seconds — not minutes — during ``make test``.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, settings

from tests.structlog_isolation import (
    IsolatedCliRunner,
    reset_structlog_global_state,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# A short, deterministic-feeling profile for in-tree property tests. Property
# tests in this repo are invariant checks, not soak/fuzz tests — 50 examples
# is enough to catch regressions without slowing the unit suite.
settings.register_profile(
    "fast",
    max_examples=50,
    # mock-only paths are fast; explicit None avoids flakes on cold imports
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("fast")

# (cli_flag, env_var, marker_name(s)) — when multiple marker names are
# listed, the include flag relaxes every "not <name>" segment for each.
# `neo` and `neo4j` share an include flag because the audit asked for
# `neo` while real tests already use `neo4j`; making `--include-neo`
# control both keeps existing tests working and lets new tests use the
# shorter ergonomic name.
_INCLUDE_FLAGS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("--include-live", "TRELLIS_TEST_LIVE", ("live",)),
    ("--include-slow", "TRELLIS_TEST_SLOW", ("slow",)),
    ("--include-neo", "TRELLIS_TEST_NEO", ("neo", "neo4j")),
    ("--include-postgres", "TRELLIS_TEST_POSTGRES", ("postgres",)),
    ("--include-pgvector", "TRELLIS_TEST_PGVECTOR", ("pgvector",)),
    ("--include-arcadedb", "TRELLIS_TEST_ARCADEDB", ("arcadedb",)),
)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register --include-<marker> flags for opt-in test selection."""
    group = parser.getgroup(
        "trellis", "trellis opt-in test markers (see docs/agent-guide/testing.md)"
    )
    for flag, env_var, markers in _INCLUDE_FLAGS:
        group.addoption(
            flag,
            action="store_true",
            default=False,
            help=(
                f"Include tests marked {' / '.join(markers)} "
                f"(also enabled by {env_var}=1)."
            ),
        )


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _strip_not_marker(expr: str, marker: str) -> str:
    """Drop ``not <marker>`` clauses from an ``and``-joined pytest -m expression.

    Splits on ``and``, filters out the segment that matches the marker, and
    rejoins. Whitespace inside each segment is normalised so input like
    ``"not live  and not slow"`` round-trips cleanly. ``or`` expressions are
    not split — addopts only ever ships ``and`` chains today.
    """
    target = f"not {marker}"
    segments = [seg.strip() for seg in re.split(r"\s+and\s+", expr.strip())]
    return " and ".join(seg for seg in segments if seg and seg != target)


def pytest_configure(config: pytest.Config) -> None:
    """Relax the default -m expression for any opted-in markers.

    Reads --include-<marker> flags and TRELLIS_TEST_<MARKER>=1 env vars,
    then strips matching ``not <marker>`` segments from the active mark
    expression. The rewritten expression is what pytest's collection
    filter sees, so opted-in tests are discovered as normal.
    """
    expr = config.getoption("markexpr") or ""
    for flag, env_var, markers in _INCLUDE_FLAGS:
        # `--include-foo` arrives as `include_foo` on config.option.
        opt_attr = flag.lstrip("-").replace("-", "_")
        if config.getoption(opt_attr, default=False) or _env_truthy(env_var):
            for marker in markers:
                expr = _strip_not_marker(expr, marker)
    config.option.markexpr = expr


@pytest.fixture(scope="session")
def neo4j_vector_search_supported() -> bool:
    """Whether the connected Neo4j can run the vector store's ``SEARCH`` query.

    :meth:`Neo4jVectorStore.query` uses the Cypher 25 ``SEARCH ... IN (
    VECTOR INDEX ... )`` clause. AuraDB supports it; a self-hosted Docker
    instance (community *or* enterprise, through at least 2025.12) does not —
    it raises ``51N26 'not supported in this version'`` — and a
    Cypher-5-default server rejects the keyword at parse time
    (``Invalid input 'SEARCH'``, which is what ``neo4j:2025.12`` actually
    returns). The probe mirrors the production query exactly (no ``CYPHER 25``
    prefix) against a throwaway index name, so it is a faithful "can the real
    query run here?" check: an index-resolution error means SEARCH is
    available, while a feature/parse error means it is not.

    A capability is not a marker. ``--include-neo`` above says an operator
    *configured* a Neo4j; this says what that Neo4j can *do*, which is the
    difference between the AuraDB the integration suite was written against
    and the container CI provisions. It lives in the root conftest rather than
    beside either consumer because it classifies support by string-matching
    three error substrings, and that is exactly the roster-shaped thing this
    repo has watched rot in duplicate: one copy gets a fourth substring, the
    other silently flips to "supported", and a skip becomes a red CI. One
    probe, two suites — ``tests/unit/stores/test_neo4j_vector.py`` and
    ``tests/integration/test_neo4j_e2e.py``, both through
    :func:`require_neo4j_vector_search`. It is separate from that gate because
    it is **session**-scoped: the probe opens a driver and runs a query, and
    doing that per test would cost a connection for every gated case.
    """
    uri = os.environ.get("TRELLIS_TEST_NEO4J_URI", "")
    if not uri:
        return False
    from neo4j import GraphDatabase

    probe = (
        "MATCH (n:Node) SEARCH n IN ( VECTOR INDEX __vs_probe__ "
        "FOR [0.0] LIMIT 1 ) SCORE AS s RETURN n LIMIT 0"
    )
    driver = GraphDatabase.driver(
        uri,
        auth=(
            os.environ.get("TRELLIS_TEST_NEO4J_USER", "neo4j"),
            os.environ.get("TRELLIS_TEST_NEO4J_PASSWORD", ""),
        ),
    )
    try:
        with driver.session(
            database=os.environ.get("TRELLIS_TEST_NEO4J_DATABASE", "neo4j")
        ) as session:
            try:
                session.run(probe).consume()
            except Exception as exc:
                # Classify any driver/Cypher failure: a feature/parse error
                # means SEARCH is unavailable here; anything else (e.g. a
                # missing-index error) means it is available.
                msg = str(exc)
                unsupported = (
                    "51N26" in msg
                    or "not available in this implementation" in msg
                    or "Invalid input 'SEARCH'" in msg
                )
                return not unsupported
            else:
                return True
    finally:
        driver.close()


@pytest.fixture
def require_neo4j_vector_search(neo4j_vector_search_supported: bool) -> None:
    """Skip a test that needs the ``SEARCH`` clause on a backend without it.

    Requesting this fixture by name *is* the gate, which is why it exists
    beside the bool rather than every call site repeating
    ``if not neo4j_vector_search_supported: pytest.skip(...)``. Two things
    follow. The skip reason is written once, so the four gated tests cannot
    drift into four different explanations of the same fact. And the gate is a
    parameter name, which
    ``tests/unit/test_neo4j_vector_live_infra_rule.py`` reads by AST to prove
    every SEARCH-issuing test in the unit suite is gated — an assertion it
    could not make against an ``if``-and-``skip`` idiom without pattern-
    matching statements, which reformatting breaks.

    Every gated test in both suites takes this fixture; nothing branches on the
    bool. An earlier cut of #356 claimed otherwise — that the e2e test read the
    bool "inside a test that also covers non-SEARCH ground" — and that was never
    true: its skip was the first statement in the body, so the whole test went
    either way, and the only thing the hand-rolled branch bought was a fifth
    verbatim copy of the reason string above.
    """
    if not neo4j_vector_search_supported:
        pytest.skip(
            "Neo4j SEARCH vector clause unsupported on this backend "
            "(self-hosted community/enterprise lacks it; requires AuraDB)"
        )


@pytest.fixture(autouse=True)
def _reset_write_provenance() -> Iterator[None]:
    """Drop the memoized write-provenance stamp around every test.

    The stamp snapshots the write-behaviour environment once per process
    (see :mod:`trellis.core.write_provenance`). Without this, a test that
    monkeypatches a flag and then asserts on an emitted event's stamp would
    pass or fail depending on whether some earlier test had already warmed
    the cache — the classic order-dependent flake.

    ``resolve_stamp_staleness`` is the stamp's second memo — one
    ``git rev-parse`` per process — and is cleared alongside it, so a test
    that simulates a stale install cannot leave that verdict behind for
    every later test's stamp.
    """
    from trellis.core.version import resolve_stamp_staleness
    from trellis.core.write_provenance import get_write_provenance

    get_write_provenance.cache_clear()
    resolve_stamp_staleness.cache_clear()
    yield
    get_write_provenance.cache_clear()
    resolve_stamp_staleness.cache_clear()


@pytest.fixture
def pin_source_tree(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Pin what the write-provenance staleness probe sees.

    Patches the three seams :func:`trellis.core.version.resolve_stamp_staleness`
    reads — the resolved build identity, the editable source tree, and that
    tree's live ``HEAD`` — and drops both memos, so every surface that reads
    the stamp (an emitted event, ``trellis admin write-config``,
    ``GET /api/version``) sees one consistent answer instead of each holding
    its own bound reference. Never touches the real repository, so the verdict
    does not depend on the git state of whatever tree the test run is
    installed from.
    """
    from trellis.core import version as version_mod
    from trellis.core import write_provenance as provenance_mod
    from trellis.core.version import CodeVersion

    def _pin(
        *,
        commit: str | None,
        head: str | None,
        tree: str | None = "/src/tree",
        source: str = "dist-metadata",
    ) -> None:
        resolved = CodeVersion(
            version=f"0.9.1.dev1+g{commit}" if commit else "0.9.1",
            source=source,
            commit=commit,
        )
        monkeypatch.setattr(version_mod, "resolve_code_version", lambda: resolved)
        monkeypatch.setattr(provenance_mod, "resolve_code_version", lambda: resolved)
        monkeypatch.setattr(version_mod, "_editable_source_tree", lambda: tree)
        monkeypatch.setattr(version_mod, "_git_head", lambda _tree: head)
        version_mod.resolve_stamp_staleness.cache_clear()
        provenance_mod.get_write_provenance.cache_clear()

    return _pin


@pytest.fixture
def cli_runner() -> Iterator[IsolatedCliRunner]:
    """A ``CliRunner`` that is safe to use from any test directory.

    Invoking a Trellis CLI command reconfigures structlog's *global* config.
    Since #377 that no longer pins a dead stream — the stream is resolved
    per write — but it does leave the command's **log level** memoised into
    every live lazy proxy, which makes a later ``capture_logs`` or
    log-asserting test silently see nothing.

    ``tests/unit/cli/`` has a package-scoped fixture covering its own tests.
    **Use this fixture for CLI invocations anywhere else** — see
    :class:`tests.structlog_isolation.IsolatedCliRunner` for the mechanism
    and the incident it comes from.
    """
    with _isolated_structlog():
        yield IsolatedCliRunner()


@contextmanager
def _isolated_structlog() -> Iterator[None]:
    """Belt-and-braces reset around a whole test, on top of per-invoke resets."""
    try:
        yield
    finally:
        reset_structlog_global_state()
