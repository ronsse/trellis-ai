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

import json
from pathlib import Path
from typing import TYPE_CHECKING

from tests.integration._live_server import assert_subprocess_imports_this_checkout

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


CliRunner = "Callable[[list[str], dict[str, str]], tuple[Any, dict[str, Any]]]"


# ── the checkout under test (#431) ────────────────────────────────────


def test_the_subprocess_under_test_is_this_checkout(
    cli_env: dict[str, str],
) -> None:
    """Every other test in this module is meaningless without this one.

    This module parses a real process's stdout for behavioural assertions —
    the CLI's ``--format json`` payloads — and a real process resolves
    ``trellis`` through the venv's editable install, which points at whichever
    checkout was ``pip install -e``'d, *not* at the worktree pytest is
    collecting from. pytest's ``pythonpath = ["src", "."]`` reaches the
    test-driver process and nothing it spawns; that holds whether the child is
    ``sys.executable`` or a console script, since neither inherits a pytest
    ini setting. (``tests/integration/mcp/test_stdio_stream_hygiene.py``
    parses a child's stdout too — this module is not unique, it is the one
    #431 caught reporting green about another branch.)

    So without the ``PYTHONPATH`` pin in ``cli_env`` every test below
    reports green about another branch's code, with no error, no skip and
    no warning. Measured during #403: the 13 tests then in this module
    passed against ``main`` while the branch under test was being fixed,
    and would have kept passing had the branch broken every one of them
    (#431). Verified again here — dropping the pin turns the whole
    directory red rather than only this test.

    ``tests/integration/mcp/`` has carried this assertion since #428; this
    directory — the one the issue is actually about — did not. Asserting the
    *outcome* (which package the child imported) rather than the presence of
    the env var is the point: an env var can be set to something that does
    not resolve.
    """
    assert_subprocess_imports_this_checkout(cli_env)


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


def test_analyze_learning_candidates_writes_artifacts_on_empty_log(
    cli_runner: Callable[..., Any],
    initialized_cli_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """``analyze learning-candidates`` writes both review files on an empty log."""
    review_dir = tmp_path / "learning_review"
    _, payload = cli_runner(
        [
            "analyze",
            "learning-candidates",
            "--output-dir",
            str(review_dir),
            "--format",
            "json",
        ],
        initialized_cli_env,
    )
    assert payload["status"] == "ok"
    assert payload["observation_count"] == 0
    assert payload["candidate_count"] == 0
    assert payload["candidates"] == []
    assert Path(payload["candidates_path"]).exists()
    assert Path(payload["decisions_template_path"]).exists()


def test_curate_promote_learning_dry_run_no_approvals(
    cli_runner: Callable[..., Any],
    initialized_cli_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """``curate promote-learning --dry-run`` parses cleanly with nothing approved."""
    candidates_path = tmp_path / "candidates.json"
    decisions_path = tmp_path / "decisions.json"
    candidates_path.write_text(
        json.dumps(
            {
                "artifact_version": "1.0",
                "candidate_count": 0,
                "candidates": [],
            }
        ),
        encoding="utf-8",
    )
    decisions_path.write_text(
        json.dumps(
            {
                "artifact_version": "1.0",
                "generated_from": "test",
                "decisions": [],
            }
        ),
        encoding="utf-8",
    )
    _, payload = cli_runner(
        [
            "curate",
            "promote-learning",
            "--candidates",
            str(candidates_path),
            "--decisions",
            str(decisions_path),
            "--dry-run",
            "--format",
            "json",
        ],
        initialized_cli_env,
    )
    assert payload["status"] == "ok"
    assert payload["dry_run"] is True
    assert payload["approved_count"] == 0
    assert payload["ready_count"] == 0


# ── structlog routing contract ────────────────────────────────────────


def test_structlog_routes_to_stderr_not_stdout(
    cli_runner: Callable[..., Any],
    initialized_cli_env: dict[str, str],
) -> None:
    """Structlog log lines land on stderr, never on stdout.

    Pins the ``configure_stderr_logging`` callback contract directly
    so a future regression that drops the callback would fail this
    test outright (rather than only failing transitively when stdout
    json-decode breaks). ``admin stats`` is the simplest command that
    forces store init, which emits ``store_instantiated`` /
    ``store_initialized`` log lines for every backing store.

    The CLI defaults to ``TRELLIS_LOG_LEVEL=WARNING`` (see
    ``trellis_cli.main._root`` and PR #101), which would filter out the
    INFO-level ``store_instantiated`` event this test relies on. Set
    ``TRELLIS_LOG_LEVEL=INFO`` explicitly so the test asserts the
    *routing* contract (stderr vs stdout) independently of whatever
    default level the CLI chooses today.
    """
    env = {**initialized_cli_env, "TRELLIS_LOG_LEVEL": "INFO"}
    completed, payload = cli_runner(["admin", "stats", "--format", "json"], env)
    assert payload["status"] == "ok"
    stderr = completed.stderr.decode(errors="replace")
    assert "store_instantiated" in stderr, (
        f"expected structlog event ``store_instantiated`` on stderr; "
        f"got stderr={stderr!r}"
    )
    # Belt-and-braces: nothing that looks like a structlog timestamp
    # should ever appear on stdout. ``cli_runner`` already json-decodes
    # stdout, but assert the renderer prefix is absent so the contract
    # holds even if a future test changes the parsing path.
    stdout = completed.stdout.decode(errors="replace")
    assert "[info" not in stdout, f"structlog leaked onto stdout: {stdout!r}"


# ── machine-output fidelity (#403) ────────────────────────────────────


#: A ``--source-system`` that is also a Rich emoji shortcode. Not contrived:
#: ``trellis ingest corpus ~/notes --source-system notes`` is the documented
#: invocation, and corpus ids are ``corpus:<source_system>:<sha1>`` by
#: construction — so the emoji name sits inside the id with colons on both
#: sides, which is exactly what Rich substitutes.
_TRAP_SOURCE_SYSTEM = "notes"

#: Narrow enough to force wrapping. The width is the point: Rich folds at
#: the console width, so before the fix whether ``--format json`` parsed at
#: all depended on the terminal it ran in — and in CI, on neither.
_NARROW_COLUMNS = "40"


def test_format_json_survives_a_narrow_terminal_and_emoji_ids(
    cli_runner: Callable[..., Any],
    initialized_cli_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """End-to-end guard for #403, in the one place a real process is parsed.

    ``tests/unit/test_machine_output_rule.py`` enforces the rule
    *structurally* — no Rich ``print`` may carry a serialized payload — and
    that is the enforceable half. This is the behavioural half, and it is
    here rather than in a unit test for two reasons: ``CliRunner`` does not
    reproduce a real terminal width, and a subprocess is where the console
    script, the entry point and the actual stdout all exist.

    It is also the test that would have caught the one site this branch
    initially missed. ``retrieve trace --format json`` emitted through
    ``console.print(result.model_dump_json())`` — six lines below a call the
    same commit had fixed — and shipped a trace id with the ``:notes:``
    replaced by an emoji, at exit code 0.
    """
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "widget.md").write_text(
        "Widget retention policy. The pruner removes stale rows nightly.\n"
    )

    env = {**initialized_cli_env, "COLUMNS": _NARROW_COLUMNS}

    _, ingested = cli_runner(
        [
            "ingest",
            "corpus",
            str(corpus),
            "--source-system",
            _TRAP_SOURCE_SYSTEM,
            "--format",
            "json",
        ],
        env,
    )
    doc_id = ingested["files"][0]["doc_id"]
    assert doc_id.startswith(f"corpus:{_TRAP_SOURCE_SYSTEM}:"), (
        f"fixture no longer produces a colon-delimited id: {doc_id!r}"
    )

    # ``run_cli`` already fails the test if stdout is not parseable JSON,
    # which covers the wrapping mode. The value check covers the two silent
    # modes — emoji substitution and markup stripping — which parse fine.
    for argv in (
        ["retrieve", "search", "widget", "--format", "json"],
        ["retrieve", "pack", "--intent", "widget", "--format", "json"],
    ):
        completed, payload = cli_runner(argv, env)
        raw = completed.stdout.decode()
        assert doc_id in raw, (
            f"{argv[1]} corrupted the document id under COLUMNS="
            f"{_NARROW_COLUMNS}: expected {doc_id!r} in {raw[:400]!r}"
        )
        assert json.dumps(payload).count(doc_id) >= 1
