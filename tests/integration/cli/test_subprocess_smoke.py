"""Black-box subprocess tests for the installed ``trellis`` CLI binary.

Each test runs the wheel's console-script entry point as a real
subprocess against a per-test ``tmp_path`` config dir, asserts exit
0, and parses stdout as JSON. The contract every ``--format json``
subcommand must honour:

  - exit 0 on success
  - JSON payload on stdout (structlog logs are routed to stderr)
  - load-bearing fields are present in the payload

This is the layer that catches problems ``CliRunner`` can't see —
missing entry-point declarations, lazy imports that explode at
process boot, environment-variable wiring that the entry-script
forgets to set up.

Skipped only when ``trellis`` isn't on ``PATH``. Runs against
SQLite tmp_path so contributors without ``.env`` can still exercise
the CLI surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


CliRunner = "Callable[[list[str], dict[str, str]], tuple[Any, dict[str, Any]]]"


# ── admin ─────────────────────────────────────────────────────────────


def test_admin_init_emits_initialized_status(
    cli_runner: Callable[..., Any],
    cli_env: dict[str, str],
) -> None:
    """``trellis admin init --format json`` returns ``status=initialized``."""
    _, payload = cli_runner(["admin", "init", "--format", "json"], cli_env)
    assert payload["status"] == "initialized"
    assert payload["config_dir"]
    assert payload["data_dir"]


def test_admin_health_after_init(
    cli_runner: Callable[..., Any],
    initialized_cli_env: dict[str, str],
) -> None:
    """``admin health`` reports config + data + stores dirs as present."""
    _, payload = cli_runner(
        ["admin", "health", "--format", "json"], initialized_cli_env
    )
    assert payload["config"] is True
    assert payload["data_dir"] is True
    assert payload["stores_dir"] is True


def test_admin_stats_returns_zero_counts_on_fresh_init(
    cli_runner: Callable[..., Any],
    initialized_cli_env: dict[str, str],
) -> None:
    """A freshly initialized registry has zero of every store kind.

    Anything > 0 means tmp_path isn't really clean — that's a real
    test isolation bug, not a CLI bug.
    """
    _, payload = cli_runner(["admin", "stats", "--format", "json"], initialized_cli_env)
    assert payload["status"] == "ok"
    for field in ("traces", "documents", "nodes", "edges", "events"):
        assert payload[field] == 0, f"{field} should be 0, got {payload}"


def test_admin_version_returns_handshake_fields(
    cli_runner: Callable[..., Any],
    cli_env: dict[str, str],
) -> None:
    """``admin version`` mirrors the ``GET /api/version`` handshake.

    No init required — the version block is static.
    """
    _, payload = cli_runner(["admin", "version", "--format", "json"], cli_env)
    assert isinstance(payload["api_major"], int)
    assert isinstance(payload["api_minor"], int)
    assert payload["api_version"]
    assert payload["wire_schema"]
    assert payload["package_version"]


def test_admin_graph_health_on_empty_graph(
    cli_runner: Callable[..., Any],
    initialized_cli_env: dict[str, str],
) -> None:
    """``admin graph-health`` returns ``status=empty`` on a fresh registry."""
    _, payload = cli_runner(
        ["admin", "graph-health", "--format", "json"], initialized_cli_env
    )
    assert payload["status"] == "empty"
    assert payload["total_nodes"] == 0


# ── retrieve ──────────────────────────────────────────────────────────


def test_retrieve_search_empty_corpus(
    cli_runner: Callable[..., Any],
    initialized_cli_env: dict[str, str],
) -> None:
    """``retrieve search`` against an empty document store returns no results."""
    _, payload = cli_runner(
        ["retrieve", "search", "anything", "--format", "json"],
        initialized_cli_env,
    )
    assert payload["status"] == "ok"
    assert payload["query"] == "anything"
    assert payload["count"] == 0
    assert payload["results"] == []


def test_retrieve_traces_empty_after_init(
    cli_runner: Callable[..., Any],
    initialized_cli_env: dict[str, str],
) -> None:
    """``retrieve traces`` returns an empty list on a fresh registry."""
    _, payload = cli_runner(
        ["retrieve", "traces", "--format", "json"], initialized_cli_env
    )
    assert payload["status"] == "ok"
    assert payload["count"] == 0
    assert payload["traces"] == []


def test_retrieve_precedents_empty_after_init(
    cli_runner: Callable[..., Any],
    initialized_cli_env: dict[str, str],
) -> None:
    """``retrieve precedents`` returns no items on a fresh registry."""
    _, payload = cli_runner(
        ["retrieve", "precedents", "--format", "json"], initialized_cli_env
    )
    assert payload["status"] == "ok"
    assert payload["count"] == 0
    assert payload["items"] == []


# ── analyze + metrics ─────────────────────────────────────────────────


def test_analyze_extractor_fallbacks_on_empty_event_log(
    cli_runner: Callable[..., Any],
    initialized_cli_env: dict[str, str],
) -> None:
    """``analyze extractor-fallbacks`` returns a parseable report on no events."""
    _, payload = cli_runner(
        ["analyze", "extractor-fallbacks", "--format", "json"],
        initialized_cli_env,
    )
    # Empty corpus → zero rate + empty per-source aggregates. The route
    # must always return a parseable JSON document, never 500 or crash.
    assert isinstance(payload, dict)


def test_metrics_outcomes_empty_on_fresh_registry(
    cli_runner: Callable[..., Any],
    initialized_cli_env: dict[str, str],
) -> None:
    """``metrics outcomes --format json`` returns a parseable empty report."""
    _, payload = cli_runner(
        ["metrics", "outcomes", "--format", "json"], initialized_cli_env
    )
    assert payload["outcomes_scanned"] == 0
    assert payload["cells"] == []
