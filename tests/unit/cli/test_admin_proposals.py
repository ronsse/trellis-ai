"""Tests for ``trellis admin generate-proposals|list-proposals|show-proposal``.

Item 7 Phase 1 — exercises the three new subcommands end-to-end via
:class:`typer.testing.CliRunner` so registration, output formats, and
exit-code routing are all under contract.

Each test isolates its own ``TRELLIS_CONFIG_DIR`` + ``TRELLIS_DATA_DIR``
via ``monkeypatch`` so the CLI's cached ``StoreRegistry`` is fresh per
test (the autouse ``_reset_cli_registry`` fixture in ``conftest.py``
drops the cache between tests).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from typer.testing import CliRunner

if TYPE_CHECKING:
    import pytest

from trellis.stores.base.event_log import EventType
from trellis_cli.admin import admin_app
from trellis_cli.admin_proposals import (
    _parse_source_file_from_preview,
)
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_OK, EXIT_STORE
from trellis_cli.main import app as root_app
from trellis_cli.stores import get_event_log

# Invoke via the root app so the ``@app.callback`` runs and routes
# structlog output to stderr — invoking ``admin_app`` directly leaves
# logs interleaved on stdout, which corrupts ``--format json`` parsing.
runner = CliRunner()


def _invoke(args: list[str]):  # type: ignore[no-untyped-def]
    """Invoke the trellis CLI against the root app with ``admin`` prefix."""
    return runner.invoke(root_app, ["admin", *args])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the CLI at a fresh tmp_path config + data dir and run ``init``."""
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    init_result = _invoke(["init"])
    assert init_result.exit_code == EXIT_OK, init_result.output


def _emit_extraction_failures(
    *,
    count: int,
    source_file: str = "src/trellis/extract/llm.py",
    failure_kind: str = "parse_error",
) -> None:
    """Seed ``count`` EXTRACTION_FAILED events with a shared cluster key."""
    event_log = get_event_log()
    for i in range(count):
        event_log.emit(
            EventType.EXTRACTION_FAILED,
            source="test.seed",
            payload={
                "source_hint": source_file,
                "failure_kind": failure_kind,
                "extractor_id": "test.extractor",
                "extractor_tier": "deterministic",
                "error_class": "ValueError",
                "error_excerpt": f"seeded failure {i}",
            },
        )


def _expected_proposal_id(source_file: str, failure_kind: str) -> str:
    """Re-compute the deterministic proposal_id from the cluster key.

    Mirrors :func:`trellis_workers.code_authoring.compute_cluster_signature`
    + :func:`trellis_workers.code_authoring.compute_proposal_id` so tests
    can assert against a stable ID without importing the generator into
    the assertion path.
    """
    signature = hashlib.sha256(
        f"{source_file}|{failure_kind}".encode()
    ).hexdigest()
    return hashlib.sha256(signature.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Registration + helper unit tests (no CLI invocation)
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_all_three_commands_registered(self) -> None:
        names = {cmd.name for cmd in admin_app.registered_commands}
        assert "generate-proposals" in names
        assert "list-proposals" in names
        assert "show-proposal" in names


class TestParseSourceFile:
    """``_parse_source_file_from_preview`` — pure helper, no IO."""

    def test_parses_canonical_title(self) -> None:
        preview = (
            "# Proposal: address parse_error in src/trellis/extract/llm.py\n"
            "\n## Cluster summary\n"
        )
        assert (
            _parse_source_file_from_preview(preview)
            == "src/trellis/extract/llm.py"
        )

    def test_returns_none_for_empty(self) -> None:
        assert _parse_source_file_from_preview("") is None

    def test_returns_none_for_unknown_shape(self) -> None:
        assert _parse_source_file_from_preview("# Some other title\n") is None


# ---------------------------------------------------------------------------
# generate-proposals
# ---------------------------------------------------------------------------


class TestGenerateProposals:
    def test_empty_text_run_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no signal events, the run returns 0 and logs the empty case."""
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(["generate-proposals"])
        assert result.exit_code == EXIT_OK, result.output
        assert "proposals_returned=0" in result.output

    def test_empty_json_run_emits_zero_proposals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JSON shape is ``{proposals, proposals_returned, window_hours}``."""
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(["generate-proposals", "--format", "json"])
        assert result.exit_code == EXIT_OK, result.output
        data = json.loads(result.stdout.strip())
        assert data == {
            "proposals": [],
            "proposals_returned": 0,
            "window_hours": 24.0,
        }

    def test_populated_json_run_returns_drafted_proposal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Seeded failure cluster surfaces as a single drafted proposal."""
        _init_stores(tmp_path, monkeypatch)
        _emit_extraction_failures(count=3)

        result = _invoke(
            ["generate-proposals", "--format", "json", "--window-hours", "48"]
        )
        assert result.exit_code == EXIT_OK, result.output
        data = json.loads(result.stdout.strip())
        assert data["proposals_returned"] == 1
        assert data["window_hours"] == 48.0
        assert len(data["proposals"]) == 1
        proposal = data["proposals"][0]
        assert proposal["proposal_id"] == _expected_proposal_id(
            "src/trellis/extract/llm.py", "parse_error"
        )
        assert proposal["source_event_count"] == 3
        assert proposal["source_file"] == "src/trellis/extract/llm.py"

    def test_rerun_emits_zero_new_drafted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Idempotency — second run over same cluster reports 0 new drafts."""
        _init_stores(tmp_path, monkeypatch)
        _emit_extraction_failures(count=2)

        first = _invoke(["generate-proposals", "--format", "json"])
        assert first.exit_code == EXIT_OK, first.output
        first_data = json.loads(first.stdout.strip())
        assert len(first_data["proposals"]) == 1

        second = _invoke(["generate-proposals", "--format", "json"])
        assert second.exit_code == EXIT_OK, second.output
        second_data = json.loads(second.stdout.strip())
        # Second run sees the same cluster but emits PROPOSAL_UPDATED
        # instead of PROPOSAL_DRAFTED → 1 returned, 0 newly drafted.
        assert second_data["proposals_returned"] == 1
        assert second_data["proposals"] == []

    def test_dry_run_warns_and_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--dry-run`` short-circuits with a warning (Phase 0 limitation)."""
        _init_stores(tmp_path, monkeypatch)
        _emit_extraction_failures(count=2)

        result = _invoke(["generate-proposals", "--dry-run"])
        assert result.exit_code == EXIT_OK, result.output
        assert "not supported" in result.output


# ---------------------------------------------------------------------------
# list-proposals
# ---------------------------------------------------------------------------


class TestListProposals:
    def test_empty_text_lists_no_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(["list-proposals"])
        assert result.exit_code == EXIT_OK, result.output
        assert "No PROPOSAL_DRAFTED" in result.output

    def test_empty_json_emits_zero_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(["list-proposals", "--format", "json"])
        assert result.exit_code == EXIT_OK, result.output
        data = json.loads(result.stdout.strip())
        assert data == {"proposals": [], "count": 0}

    def test_populated_json_returns_drafted_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        _emit_extraction_failures(count=2)
        # Generate a proposal so list has something to surface.
        gen = _invoke(["generate-proposals", "--format", "json"])
        assert gen.exit_code == EXIT_OK, gen.output

        result = _invoke(["list-proposals", "--format", "json"])
        assert result.exit_code == EXIT_OK, result.output
        data = json.loads(result.stdout.strip())
        assert data["count"] == 1
        row = data["proposals"][0]
        assert row["proposal_id"] == _expected_proposal_id(
            "src/trellis/extract/llm.py", "parse_error"
        )
        assert row["source_event_count"] == 2
        assert row["source_file"] == "src/trellis/extract/llm.py"
        assert "generated_at" in row
        assert "event_id" in row

    def test_limit_caps_returned_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--limit`` caps the row count (each cluster ⇒ one proposal)."""
        _init_stores(tmp_path, monkeypatch)
        # Two distinct clusters → two distinct proposals.
        _emit_extraction_failures(count=1, source_file="a.py")
        _emit_extraction_failures(count=1, source_file="b.py")
        _invoke(["generate-proposals", "--format", "json"])

        result = _invoke(
            ["list-proposals", "--format", "json", "--limit", "1"]
        )
        assert result.exit_code == EXIT_OK, result.output
        data = json.loads(result.stdout.strip())
        assert data["count"] == 1


# ---------------------------------------------------------------------------
# show-proposal
# ---------------------------------------------------------------------------


class TestShowProposal:
    def test_unknown_id_returns_internal_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(["show-proposal", "deadbeef"])
        assert result.exit_code == EXIT_INTERNAL, result.output
        assert "No PROPOSAL_DRAFTED" in result.output

    def test_unknown_id_json_returns_error_envelope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(
            ["show-proposal", "deadbeef", "--format", "json"]
        )
        assert result.exit_code == EXIT_INTERNAL, result.output
        data = json.loads(result.stdout.strip())
        assert data["error"] == "not_found"
        assert "deadbeef" in data["message"]

    def test_known_id_json_returns_markdown_and_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        _emit_extraction_failures(count=2)
        _invoke(["generate-proposals", "--format", "json"])

        proposal_id = _expected_proposal_id(
            "src/trellis/extract/llm.py", "parse_error"
        )

        result = _invoke(
            ["show-proposal", proposal_id, "--format", "json"]
        )
        assert result.exit_code == EXIT_OK, result.output
        data = json.loads(result.stdout.strip())
        assert data["proposal_id"] == proposal_id
        assert data["markdown"].startswith("# Proposal: address parse_error in ")
        assert data["markdown_truncated"] is True
        assert data["source_event_count"] == 2

    def test_known_id_text_prints_markdown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        _emit_extraction_failures(count=1)
        _invoke(["generate-proposals", "--format", "json"])

        proposal_id = _expected_proposal_id(
            "src/trellis/extract/llm.py", "parse_error"
        )
        result = _invoke(["show-proposal", proposal_id])
        assert result.exit_code == EXIT_OK, result.output
        # Plain print emits the markdown body directly to stdout.
        assert "# Proposal: address parse_error in" in result.stdout


# ---------------------------------------------------------------------------
# Exit-code-routing smoke test
# ---------------------------------------------------------------------------


class TestExitCodeRouting:
    """Verifies EXIT_STORE (5) is reached on store-shaped failure.

    Patches :func:`get_event_log` to raise; the command must convert
    the exception into the documented exit code rather than crashing
    out with a stack trace.
    """

    def test_generate_proposals_returns_exit_store_on_store_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _init_stores(tmp_path, monkeypatch)

        def _boom(*_args: object, **_kwargs: object) -> object:
            msg = "simulated store failure"
            raise RuntimeError(msg)

        monkeypatch.setattr(
            "trellis_cli.admin_proposals.get_event_log",
            _boom,
        )
        result = _invoke(["generate-proposals"])
        assert result.exit_code == EXIT_STORE, result.output

    def test_list_proposals_returns_exit_store_on_store_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _init_stores(tmp_path, monkeypatch)

        def _boom(*_args: object, **_kwargs: object) -> object:
            msg = "simulated store failure"
            raise RuntimeError(msg)

        monkeypatch.setattr(
            "trellis_cli.admin_proposals.get_event_log",
            _boom,
        )
        result = _invoke(["list-proposals", "--format", "json"])
        assert result.exit_code == EXIT_STORE, result.output
        data = json.loads(result.stdout.strip())
        assert data["error"] == "store_error"

    def test_show_proposal_returns_exit_store_on_store_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _init_stores(tmp_path, monkeypatch)

        def _boom(*_args: object, **_kwargs: object) -> object:
            msg = "simulated store failure"
            raise RuntimeError(msg)

        monkeypatch.setattr(
            "trellis_cli.admin_proposals.get_event_log",
            _boom,
        )
        result = _invoke(
            ["show-proposal", "anything", "--format", "json"]
        )
        assert result.exit_code == EXIT_STORE, result.output
        data = json.loads(result.stdout.strip())
        assert data["error"] == "store_error"
