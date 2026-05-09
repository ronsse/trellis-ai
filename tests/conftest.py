"""Top-level pytest configuration: opt-in marker gating + hypothesis profile.

Markers ``live``, ``slow``, ``neo`` / ``neo4j``, ``postgres``, ``pgvector``,
and ``lancedb`` are excluded from the default ``pytest`` run via the
``-m "not ..."`` expression in ``[tool.pytest.ini_options].addopts``.

Each ``--include-<marker>`` CLI flag (with a ``TRELLIS_TEST_<MARKER>=1``
env-var equivalent) relaxes its corresponding ``not <marker>`` constraint
by rewriting the active ``-m`` expression before collection. This means a
default ``pytest`` run skips heavy / live-dependent tests, while CI and
opt-in local invocations can dial them back in selectively.

Why rewrite the ``-m`` expression instead of just unmarking nodes?
``addopts`` runs before ``pytest_collection_modifyitems`` and pytest's mark
filter is applied at collection. Removing markers from items inside this
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
from typing import TYPE_CHECKING

from hypothesis import HealthCheck, settings

if TYPE_CHECKING:
    import pytest

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
    ("--include-lancedb", "TRELLIS_TEST_LANCEDB", ("lancedb",)),
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
    """Remove ``not <marker>`` segments from a pytest -m expression.

    Handles the three shapes that show up in our addopts string:
      - ``not <marker> and <rest>``     (leading clause)
      - ``<rest> and not <marker>``     (trailing or middle clause)
      - ``not <marker>``                 (sole clause)

    Other shapes pass through unchanged. Whitespace is normalised
    afterward so the resulting expression is what pytest expects.
    """
    # `not foo and ` (leading) or ` and not foo` (trailing / middle) —
    # word boundaries on the marker name prevent `not neo` from also
    # matching `not neo4j`.
    patterns = (
        rf"\bnot\s+{re.escape(marker)}\b\s+and\s+",
        rf"\s+and\s+\bnot\s+{re.escape(marker)}\b",
        rf"\bnot\s+{re.escape(marker)}\b",
    )
    out = expr
    for pat in patterns:
        out = re.sub(pat, " ", out)
    return re.sub(r"\s+", " ", out).strip()


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
    # Trim a dangling `and`/`or` that could remain after stripping.
    expr = re.sub(r"^(and|or)\s+", "", expr)
    expr = re.sub(r"\s+(and|or)$", "", expr)
    config.option.markexpr = expr


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """No-op hook held in place for clarity.

    The actual gating happens in ``pytest_configure`` via mark-expression
    rewriting (which runs before pytest's built-in filter). This stub
    documents the contract for readers who expect to find the logic here
    per the F1 task brief.
    """
    del config, items  # gating is done earlier; nothing to do at this stage.
