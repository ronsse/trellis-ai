"""Smoke tests for the ``trellis metrics`` CLI."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import assert_coloured, force_colour, plain
from trellis.ops import record_outcome
from trellis.schemas.outcome import GRAPH_SEARCH_COMPONENT_ID
from trellis.schemas.parameters import (
    ParameterProposal,
    ParameterScope,
    ParameterSet,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.outcome import SQLiteOutcomeStore
from trellis.stores.sqlite.parameter import SQLiteParameterStore
from trellis.stores.sqlite.tuner_state import SQLiteTunerStateStore
from trellis_cli import metrics as metrics_cli
from trellis_cli import worker as worker_cli
from trellis_cli.main import app

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch) -> Iterator[dict[str, Path]]:
    """Point the CLI at a temp data dir and pre-seed the ops stores."""
    stores_dir = tmp_path / "data" / "stores"
    stores_dir.mkdir(parents=True)

    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))

    # Reset any CLI-level cached registry.
    from trellis_cli import stores as cli_stores

    cli_stores._reset_registry()

    # Pre-populate stores by instantiating directly at the same paths the
    # CLI registry will use.
    outcome_store = SQLiteOutcomeStore(stores_dir / "outcomes.db")
    param_store = SQLiteParameterStore(stores_dir / "parameters.db")
    tuner_state = SQLiteTunerStateStore(stores_dir / "tuner_state.db")
    event_log = SQLiteEventLog(stores_dir / "events.db")

    try:
        yield {
            "stores_dir": stores_dir,
            "outcome_store": outcome_store,
            "param_store": param_store,
            "tuner_state": tuner_state,
            "event_log": event_log,
        }
    finally:
        outcome_store.close()
        param_store.close()
        tuner_state.close()
        event_log.close()
        cli_stores._reset_registry()


def _seed_failing_outcomes(
    outcome_store: SQLiteOutcomeStore,
    *,
    n: int = 40,
    component_id: str = "retrieve.strategies.KeywordSearch",
    domain: str = "orders",
) -> None:
    """Seed a cell for the ``metrics outcomes`` aggregation tests.

    Deliberately *not* shaped to trip any ``DEFAULT_RULES`` entry — these
    tests pin how outcomes roll up into cells, and a fixture that also
    happened to fire a rule would couple them to the rule roster.
    """
    base = datetime.now(UTC) - timedelta(hours=1)
    for i in range(n):
        record_outcome(
            outcome_store,
            component_id=component_id,
            success=i % 10 == 0,  # ~10% success
            latency_ms=12.0,
            domain=domain,
            intent_family="plan",
            occurred_at=base + timedelta(seconds=i),
        )


def _seed_uncited_graph_outcomes(
    outcome_store: SQLiteOutcomeStore,
    *,
    n: int = 10,
    served_each: int = 5,
    referenced_total: int = 1,
    intent_family: str | None = None,
    domain: str = "orders",
) -> None:
    """Seed a cell the shipped rule actually fires on.

    Mirrors the production cell ``DEFAULT_RULES`` was calibrated against
    — many graph items served, almost none cited — so these tests pin the
    CLI wiring (exit code, JSON shape, persistence, promotion state) with
    a fixture the *current* roster can reach.  The previous fixture was
    shaped for ``keyword_low_success_rate_boost_recency``, retired in
    #562; when that rule went, four of these tests failed and a fifth
    (``proposals_status_filter``, an ``all()`` over an empty list) passed
    vacuously.

    ``50`` servings clears ``min_items_served=20``, ``10`` rows clears
    ``min_sample_size=3``, and a reference rate of ``1/50 = 0.02`` sits
    under the ``0.07`` threshold.

    It carries **no** ``intent_family``, and that is a calibration term
    like the three above rather than an omission.  ``GraphSearch`` reads
    its parameters at ``(component_id, domain)``, so a snapshot written
    at an ``intent_family`` is one ``ParameterStore.resolve`` can never
    return; since #617 the tuner screens that at emit and the cell would
    yield no proposal at all.  It used to pass ``intent_family="plan"``,
    which is the *second* time this fixture has been shaped for a world
    the tuner had moved on from — so the refusal path gets its own test
    at the bottom of this module rather than being left implicit in a
    seeder nobody re-reads.  Pass ``intent_family=`` to seed the refused
    cell on purpose; that is what those tests do.
    """
    base = datetime.now(UTC) - timedelta(hours=1)
    for i in range(n):
        record_outcome(
            outcome_store,
            component_id=GRAPH_SEARCH_COMPONENT_ID,
            # The pack's bit, fanned out — the rule keys on the reference
            # rate, not on this.
            success=i % 5 == 0,
            latency_ms=12.0,
            domain=domain,
            intent_family=intent_family,
            items_served=served_each,
            items_referenced=1 if i < referenced_total else 0,
            occurred_at=base + timedelta(seconds=i),
        )


# ---------------------------------------------------------------------------
# outcomes
# ---------------------------------------------------------------------------


def test_metrics_outcomes_json(cli_env):
    _seed_failing_outcomes(cli_env["outcome_store"])

    result = runner.invoke(app, ["metrics", "outcomes", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["outcomes_scanned"] == 40
    assert len(payload["cells"]) == 1
    cell = payload["cells"][0]
    assert cell["scope"]["component_id"] == "retrieve.strategies.KeywordSearch"
    assert cell["count"] == 40
    assert cell["success_rate"] == pytest.approx(0.1, abs=0.05)


def test_metrics_outcomes_empty(cli_env):
    result = runner.invoke(app, ["metrics", "outcomes", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["outcomes_scanned"] == 0
    assert payload["cells"] == []


# ---------------------------------------------------------------------------
# tune
# ---------------------------------------------------------------------------


def test_metrics_tune_emits_proposals(cli_env):
    _seed_uncited_graph_outcomes(cli_env["outcome_store"])

    result = runner.invoke(app, ["metrics", "tune", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["tuner_name"] == "rule_tuner"
    assert payload["proposals_persisted"] >= 1
    first = payload["proposals"][0]
    assert first["scope"]["component_id"] == GRAPH_SEARCH_COMPONENT_ID


@pytest.mark.parametrize(
    ("command", "summary"),
    [
        (["metrics", "tune"], "1 proposals persisted"),
        (["worker", "tune"], "1 proposal(s) considered"),
    ],
)
def test_tune_text_renders_the_tuner_name_verbatim(
    cli_env, monkeypatch: pytest.MonkeyPatch, command: list[str], summary: str
) -> None:
    """Both RuleTuner surfaces echo ``--tuner-name`` after the tuner has run.

    So ``[/x]`` raising ``MarkupError`` there exited 1 with the proposals
    already persisted.
    """
    _seed_uncited_graph_outcomes(cli_env["outcome_store"])
    force_colour(monkeypatch, metrics_cli, worker_cli)
    result = runner.invoke(app, [*command, "--tuner-name", "[bold]t[/x]"])
    assert result.exit_code == 0, plain(result.output)
    rendered = " ".join(assert_coloured(result.stdout).split())
    assert "tuner=[bold]t[/x]" in rendered
    assert summary in rendered


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_worker_tune_dry_run_writes_nothing_and_a_live_run_still_promotes(
    cli_env, tmp_path: Path, fmt: str
) -> None:
    """``worker tune --dry-run`` leaves proposals, cursor and events alone.

    It used to persist every proposal and set the cursor.  ``orders`` has a
    baseline and clears the auto gate; ``billing`` has none and stays pending.
    """
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "config.yaml").write_text(
        "learning:\n  auto_promote:\n    enabled: true\n", encoding="utf-8"
    )
    cli_env["param_store"].put(
        ParameterSet(
            scope=ParameterScope(
                component_id=GRAPH_SEARCH_COMPONENT_ID, domain="orders"
            ),
            values={"domain_match_boost": 2.0},
            source="test:baseline",
        )
    )
    for domain in ("orders", "billing"):
        _seed_uncited_graph_outcomes(cli_env["outcome_store"], n=40, domain=domain)
    state, events = cli_env["tuner_state"], cli_env["event_log"]

    dry = runner.invoke(app, ["worker", "tune", "--dry-run", "--format", fmt])

    assert dry.exit_code == 0, plain(dry.output)
    if fmt == "json":
        payload = json.loads(dry.stdout)
        assert (payload["proposals_considered"], payload["pending_manual"]) == (2, 1)
        assert payload["dry_run"] is True
    else:
        out = " ".join(plain(dry.output).split())
        assert "2 proposal(s) considered" in out
        assert "A run without --dry-run leaves pending proposals queued" in out
    assert state.list_proposals() == []
    assert state.get_cursor("rule_tuner") is None
    assert events.get_events(limit=100) == []

    live = runner.invoke(app, ["worker", "tune", "--format", "json"])

    assert live.exit_code == 0, plain(live.output)
    live_payload = json.loads(live.stdout)
    assert live_payload["auto_promoted"] == 1
    assert live_payload["dry_run"] is False
    statuses = sorted(p.status for p in state.list_proposals())
    assert statuses == ["pending", "promoted"]
    assert state.get_cursor("rule_tuner") is not None


@pytest.mark.parametrize(("args", "dry_run"), [([], False), (["--dry-run"], True)])
def test_worker_tune_json_dry_run_is_the_flag_with_auto_promote_off(
    cli_env, args: list[str], dry_run: bool
) -> None:
    """``"dry_run"`` is the ``--dry-run`` flag, not whether the pass promoted.

    With auto-promote off, a run without ``--dry-run`` writes its proposals
    and the cursor, and it used to report ``"dry_run": true``.
    """
    _seed_uncited_graph_outcomes(cli_env["outcome_store"], n=40, domain="orders")
    state = cli_env["tuner_state"]

    result = runner.invoke(app, ["worker", "tune", "--format", "json", *args])

    assert result.exit_code == 0, plain(result.output)
    payload = json.loads(result.stdout)
    assert payload["enabled"] is False
    assert payload["proposals_considered"] >= 1
    assert payload["dry_run"] is dry_run
    if dry_run:
        assert state.list_proposals() == []
        assert state.get_cursor("rule_tuner") is None
    else:
        assert state.list_proposals() != []
        assert state.get_cursor("rule_tuner") is not None


def test_worker_tune_live_run_emits_tune_cycle_completed_event(cli_env) -> None:
    """A live ``worker tune`` leaves a durable, API-readable health record (e167).

    Before this, a pass that promoted nothing emitted zero events, so
    ``GET /admin/loops`` could not tell "ran and found nothing to do"
    from "has never run". The dry-run side of this (no event at all)
    is already pinned by
    ``test_worker_tune_dry_run_writes_nothing_and_a_live_run_still_promotes``.
    """
    _seed_uncited_graph_outcomes(cli_env["outcome_store"], n=40, domain="orders")
    events = cli_env["event_log"]

    result = runner.invoke(app, ["worker", "tune", "--format", "json"])

    assert result.exit_code == 0, plain(result.output)
    payload = json.loads(result.stdout)
    recorded = events.get_events(event_type=EventType.TUNE_CYCLE_COMPLETED, limit=10)
    assert len(recorded) == 1, "exactly one pass ran, so exactly one event"
    event_payload = recorded[0].payload
    assert event_payload["tuner_name"] == payload["tuner_name"]
    assert event_payload["proposals_considered"] == payload["proposals_considered"]
    assert event_payload["auto_promoted"] == payload["auto_promoted"]
    assert event_payload["pending_manual"] == payload["pending_manual"]
    assert "write_provenance" in recorded[0].metadata


def test_tune_text_renders_a_stored_domain_verbatim(
    cli_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each proposal line echoes its cell's domain, a stored value."""
    _seed_uncited_graph_outcomes(cli_env["outcome_store"], domain="[bold]d[/z]")
    force_colour(monkeypatch, metrics_cli)
    result = runner.invoke(app, ["metrics", "tune"])
    assert result.exit_code == 0, plain(result.output)
    rendered = " ".join(assert_coloured(result.stdout).split())
    assert "1 proposals persisted" in rendered
    assert "domain=[bold]d[/z]" in rendered


# ---------------------------------------------------------------------------
# proposals
# ---------------------------------------------------------------------------


def test_metrics_proposals_lists_stored(cli_env):
    _seed_uncited_graph_outcomes(cli_env["outcome_store"])
    runner.invoke(app, ["metrics", "tune", "--format", "json"])

    result = runner.invoke(app, ["metrics", "proposals", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) >= 1


def test_metrics_proposals_status_filter(cli_env):
    _seed_uncited_graph_outcomes(cli_env["outcome_store"])
    runner.invoke(app, ["metrics", "tune", "--format", "json"])

    result = runner.invoke(
        app, ["metrics", "proposals", "--status", "pending", "--format", "json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    # An ``all()`` over an empty list is True, so the filter has to be
    # shown something to filter before it can be shown to filter right.
    assert len(payload) >= 1
    assert all(p["status"] == "pending" for p in payload)


def test_proposals_text_renders_the_stored_tuner_and_the_filters_verbatim(
    cli_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tuner name ``tune --format json`` stored reaches the table as written."""
    _seed_uncited_graph_outcomes(cli_env["outcome_store"])
    tuned = runner.invoke(
        app, ["metrics", "tune", "--tuner-name", "[b]t[/x]", "--format", "json"]
    )
    assert tuned.exit_code == 0, tuned.output
    monkeypatch.setenv("COLUMNS", "200")  # the table must not fold the cell
    force_colour(monkeypatch, metrics_cli)

    result = runner.invoke(app, ["metrics", "proposals"])
    assert result.exit_code == 0, plain(result.output)
    assert "[b]t[/x]" in assert_coloured(result.stdout)

    filtered = runner.invoke(
        app, ["metrics", "proposals", "--tuner", "[b]q[/z]", "--status", "[b]s[/w]"]
    )
    assert filtered.exit_code == 0, plain(filtered.output)
    rendered = " ".join(assert_coloured(filtered.stdout).split())
    assert "tuner=[b]q[/z] status=[b]s[/w]" in rendered


def test_proposals_text_renders_filters_that_form_a_tag_together(
    cli_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Escaping each filter apart misses the tag the two form together.

    Neither ``[bold]q[/z`` nor ``s]`` ends in a whole closing tag, and no
    bracket sits between them, so ``[/z status=s]`` raised ``MarkupError``.
    """
    force_colour(monkeypatch, metrics_cli)
    result = runner.invoke(
        app, ["metrics", "proposals", "--tuner", "[bold]q[/z", "--status", "s]"]
    )
    assert result.exit_code == 0, plain(result.output)
    rendered = " ".join(assert_coloured(result.stdout).split())
    assert "tuner=[bold]q[/z status=s]" in rendered


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------


def test_metrics_versions_for_scope_without_history(cli_env):
    result = runner.invoke(
        app,
        [
            "metrics",
            "versions",
            "retrieve.strategies.KeywordSearch",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["active_version"] is None
    assert payload["versions"] == []


def test_metrics_versions_after_seed(cli_env):
    cli_env["param_store"].put(
        ParameterSet(
            scope=ParameterScope(
                component_id="retrieve.strategies.KeywordSearch", domain="a"
            ),
            values={"recency_half_life_days": 20.0},
        )
    )
    result = runner.invoke(
        app,
        [
            "metrics",
            "versions",
            "retrieve.strategies.KeywordSearch",
            "--domain",
            "a",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["active_version"] is not None
    assert len(payload["versions"]) == 1


# ---------------------------------------------------------------------------
# promote dry-run + commit
# ---------------------------------------------------------------------------


def test_metrics_promote_dry_run_by_default(cli_env):
    _seed_uncited_graph_outcomes(cli_env["outcome_store"])
    tune_result = runner.invoke(app, ["metrics", "tune", "--format", "json"])
    proposal_id = json.loads(tune_result.stdout)["proposals"][0]["proposal_id"]

    result = runner.invoke(app, ["metrics", "promote", proposal_id, "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True

    # Proposal should still be pending (no mutation).
    assert cli_env["tuner_state"].get_proposal(proposal_id).status == "pending"


def test_metrics_promote_commit_changes_state(cli_env):
    _seed_uncited_graph_outcomes(cli_env["outcome_store"])
    tune_result = runner.invoke(app, ["metrics", "tune", "--format", "json"])
    proposal_id = json.loads(tune_result.stdout)["proposals"][0]["proposal_id"]

    result = runner.invoke(
        app,
        [
            "metrics",
            "promote",
            proposal_id,
            "--commit",
            "--min-sample-size",
            "5",
            "--min-effect-size",
            "0.01",
            "--allow-no-baseline",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "promoted"
    assert payload["params_version"] is not None

    assert cli_env["tuner_state"].get_proposal(proposal_id).status == "promoted"


def test_metrics_promote_commit_refuses_no_baseline_by_default(cli_env):
    """The scope's first proposal (no ParameterSet snapshot yet) is refused
    by bare ``--commit`` — ``PromotionPolicy.allow_no_baseline`` now
    defaults to ``False``. Before this default flip, a bare ``--commit``
    promoted a first proposal vacuously (#823's gate finding).

    The refusal must also leave the proposal's stored status untouched
    (not ``"rejected"``) so a later ``--allow-no-baseline`` call on the
    *same* proposal can still succeed — see
    ``test_metrics_promote_commit_with_allow_no_baseline_bootstraps_scope``.
    """
    _seed_uncited_graph_outcomes(cli_env["outcome_store"])
    tune_result = runner.invoke(app, ["metrics", "tune", "--format", "json"])
    proposal_id = json.loads(tune_result.stdout)["proposals"][0]["proposal_id"]

    result = runner.invoke(
        app,
        [
            "metrics",
            "promote",
            proposal_id,
            "--commit",
            "--min-sample-size",
            "5",
            "--min-effect-size",
            "0.01",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "rejected"
    assert payload["reason"] == "no_baseline_snapshot_for_scope"
    assert payload["params_version"] is None

    # Non-terminal: the stored proposal is still promotable, not "rejected".
    assert cli_env["tuner_state"].get_proposal(proposal_id).status == "pending"


def test_metrics_promote_no_baseline_refusal_text_hints_allow_no_baseline(cli_env):
    """The text (non-JSON) refusal names the actual remedy.

    Follow-up from the #828 gate: before this, the CLI's text output for
    a ``no_baseline_...`` refusal said only the bare reason string, and
    an operator reading it had no way to tell ``--allow-no-baseline``
    (surgical) apart from ``--force`` (skips everything) without reading
    the source. Both the dry-run preview and the ``--commit`` refusal
    must show the hint; machine consumers reading ``--format json``
    already see the ``no_baseline_...`` prefix in ``reason`` and need no
    hint, so the JSON payload is unchanged (checked above).
    """
    _seed_uncited_graph_outcomes(cli_env["outcome_store"])
    tune_result = runner.invoke(app, ["metrics", "tune", "--format", "json"])
    proposal_id = json.loads(tune_result.stdout)["proposals"][0]["proposal_id"]

    dry_run = runner.invoke(
        app,
        ["metrics", "promote", proposal_id, "--min-sample-size", "5"],
    )
    assert dry_run.exit_code == 0, plain(dry_run.output)
    assert "--allow-no-baseline" in plain(dry_run.output)

    committed = runner.invoke(
        app,
        [
            "metrics",
            "promote",
            proposal_id,
            "--commit",
            "--min-sample-size",
            "5",
        ],
    )
    assert committed.exit_code == 0, plain(committed.output)
    assert "--allow-no-baseline" in plain(committed.output)

    # A refusal that is NOT bootstrap-shaped gets no such hint — it would
    # misdirect an operator toward a flag that cannot fix a sample-size or
    # effect-size shortfall.
    bootstrapped = runner.invoke(
        app,
        [
            "metrics",
            "promote",
            proposal_id,
            "--commit",
            "--min-sample-size",
            "5",
            "--min-effect-size",
            "0.01",
            "--allow-no-baseline",
        ],
    )
    assert bootstrapped.exit_code == 0, plain(bootstrapped.output)
    assert "--allow-no-baseline" not in plain(bootstrapped.output)


def test_metrics_promote_commit_with_allow_no_baseline_bootstraps_scope(cli_env):
    """A no-baseline refusal is recoverable: the *same* proposal promotes
    on a second call that adds ``--allow-no-baseline``, because the first
    call's refusal did not mark it ``"rejected"``.
    """
    _seed_uncited_graph_outcomes(cli_env["outcome_store"])
    tune_result = runner.invoke(app, ["metrics", "tune", "--format", "json"])
    proposal_id = json.loads(tune_result.stdout)["proposals"][0]["proposal_id"]

    refused = runner.invoke(
        app,
        [
            "metrics",
            "promote",
            proposal_id,
            "--commit",
            "--min-sample-size",
            "5",
            "--min-effect-size",
            "0.01",
            "--format",
            "json",
        ],
    )
    assert json.loads(refused.stdout)["status"] == "rejected"

    bootstrapped = runner.invoke(
        app,
        [
            "metrics",
            "promote",
            proposal_id,
            "--commit",
            "--min-sample-size",
            "5",
            "--min-effect-size",
            "0.01",
            "--allow-no-baseline",
            "--format",
            "json",
        ],
    )
    assert bootstrapped.exit_code == 0, bootstrapped.output
    payload = json.loads(bootstrapped.stdout)
    assert payload["status"] == "promoted"
    assert payload["params_version"] is not None
    assert cli_env["tuner_state"].get_proposal(proposal_id).status == "promoted"


def test_metrics_promote_allow_no_baseline_still_enforces_sample_size_floor(cli_env):
    """``--allow-no-baseline`` is surgical: it lifts only the baseline
    rule, not ``--min-sample-size``.

    With the floor raised above the seeded proposal's sample size,
    ``--allow-no-baseline --commit`` still refuses on the sample-size
    gate rather than promoting, and the stored status is terminally
    ``"rejected"`` — a sample-size refusal, unlike a no-baseline one, has
    no recovery path. A regression that routed ``--allow-no-baseline``
    through ``force`` (skipping the whole policy gate, like ``--force``
    does) would instead promote here.
    """
    _seed_uncited_graph_outcomes(cli_env["outcome_store"])
    tune_result = runner.invoke(app, ["metrics", "tune", "--format", "json"])
    proposal_id = json.loads(tune_result.stdout)["proposals"][0]["proposal_id"]

    result = runner.invoke(
        app,
        [
            "metrics",
            "promote",
            proposal_id,
            "--commit",
            "--min-sample-size",
            "999",
            "--allow-no-baseline",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "rejected"
    assert payload["reason"].startswith("sample_size=")

    # Terminal: unlike a no-baseline refusal, a sample-size refusal marks
    # the stored proposal "rejected".
    assert cli_env["tuner_state"].get_proposal(proposal_id).status == "rejected"


def test_metrics_promote_force_alone_still_bypasses_baseline_rule(cli_env):
    """Decision (see PR body): ``--force`` keeps skipping the *whole*
    policy gate, baseline rule included, rather than gaining a carve-out.

    ``_apply_policy`` — sample size, effect size, non-numeric, and
    baseline — has always been entirely inside promote_proposal's
    ``if not force:`` branch; only the immutable-core and reachability
    gates are force-proof. Pulling the baseline check out of that branch
    so ``--force`` could no longer skip it would restructure a function
    two other test files assert on directly, for a surface that is
    already non-default and logged — the vacuous-default defect #823
    found is fixed by the default flip alone. This test pins the
    (unchanged) behaviour rather than silently letting it drift.
    """
    _seed_uncited_graph_outcomes(cli_env["outcome_store"])
    tune_result = runner.invoke(app, ["metrics", "tune", "--format", "json"])
    proposal_id = json.loads(tune_result.stdout)["proposals"][0]["proposal_id"]

    result = runner.invoke(
        app,
        [
            "metrics",
            "promote",
            proposal_id,
            "--commit",
            "--force",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "promoted"


def test_metrics_promote_missing_proposal(cli_env):
    result = runner.invoke(
        app,
        [
            "metrics",
            "promote",
            "prop_nonexistent",
            "--commit",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "skipped"
    assert payload["reason"] == "proposal_not_found"


# ---------------------------------------------------------------------------
# reachability (#617)
# ---------------------------------------------------------------------------


def test_tune_persists_nothing_for_an_unreachable_cell(cli_env):
    """The same cell, one axis different, yields no proposal at all.

    ``test_metrics_tune_emits_proposals`` above seeds the identical cell
    without an ``intent_family`` and gets one proposal, so this pins the
    screen and not some other property of the fixture.  The refusal is at
    *emit*: nothing reaches the store to be approved later.
    """
    _seed_uncited_graph_outcomes(cli_env["outcome_store"], intent_family="plan")

    result = runner.invoke(app, ["metrics", "tune", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["proposals_persisted"] == 0
    assert payload["proposals"] == []
    assert cli_env["tuner_state"].list_proposals() == []


def test_proposals_json_carries_the_reachability_verdict(cli_env):
    """A stored proposal is screened at render too, not only at emit.

    The store outlives the screen: a row written by a build from before
    #617, or by a tuner that does not screen, is still approvable from
    this surface — so the verdict is computed here rather than read back
    off the row.  Written directly for that reason; the tuner itself can
    no longer produce one.
    """
    cli_env["tuner_state"].put_proposal(
        ParameterProposal(
            scope=ParameterScope(
                component_id=GRAPH_SEARCH_COMPONENT_ID,
                domain="orders",
                intent_family="plan",
            ),
            proposed_values={"domain_match_boost": 1.4},
            tuner="legacy_tuner",
        )
    )

    result = runner.invoke(app, ["metrics", "proposals", "--format", "json"])

    assert result.exit_code == 0, result.output
    [row] = json.loads(result.stdout)
    assert row["reachability"]["checked"] is True
    assert row["reachability"]["reachable"] is False
    assert [r["axis"] for r in row["reachability"]["reasons"]] == ["intent_family"]
    assert row["reachability"]["reasons"][0]["kind"] == "unsupplied_axis"


def test_proposals_json_reports_no_claim_for_an_unrostered_component(cli_env):
    """``checked: false`` is not ``reachable: false``.

    ``component_id`` is an open string and ``ParameterRegistry`` is a
    public facade, so a component absent from a scan of ``src/`` may
    still have a reader out of tree.  An approval script that read the
    two as the same thing would suppress a working tuning loop it cannot
    see, which is why the JSON carries both keys.
    """
    cli_env["tuner_state"].put_proposal(
        ParameterProposal(
            scope=ParameterScope(
                component_id="tests.cli.unrostered_component",
                intent_family="plan",
            ),
            proposed_values={"some_key": 1.0},
            tuner="legacy_tuner",
        )
    )

    result = runner.invoke(app, ["metrics", "proposals", "--format", "json"])

    assert result.exit_code == 0, result.output
    [row] = json.loads(result.stdout)
    assert row["reachability"]["checked"] is False
    assert row["reachability"]["reachable"] is None
    assert row["reachability"]["reasons"] == []


def test_proposals_text_prints_the_reason_below_the_table(cli_env):
    """The human surface is where approval happens, so it carries the why.

    Asserted on ``ParameterStore.resolve``, which appears only in a
    reason detail — the four scope columns render their own headers, so
    asserting on ``intent_family`` would pass against a table that
    printed no reason at all.
    """
    cli_env["tuner_state"].put_proposal(
        ParameterProposal(
            scope=ParameterScope(
                component_id=GRAPH_SEARCH_COMPONENT_ID,
                domain="orders",
                intent_family="plan",
            ),
            proposed_values={"domain_match_boost": 1.4},
            tuner="legacy_tuner",
        )
    )

    result = runner.invoke(app, ["metrics", "proposals"])

    assert result.exit_code == 0, result.output
    assert "unreachable" in result.output
    assert "ParameterStore.resolve" in result.output
