"""``trellis curate rejudge-withheld``: report-only by default, one exit code (#1134).

The withheld state is seeded directly (a labelled row marked ``skipped`` and
the ``RECONCILE_DEGRADED`` event ``save_memory`` writes beside it); the
library-level suite in ``tests/unit/mcp/test_reconcile_rejudge.py`` produces it
through ``save_memory`` itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import trellis.mcp.reconcile_rejudge as rejudge_mod
import trellis_cli.curate_rejudge as command_mod
from tests.cli_output import plain
from tests.llm_routes import TierRoutedRegistry, per_consumer_config
from trellis.core.document_write import put_document
from trellis.errors import StoreError
from trellis.llm.routing import LLMConsumer
from trellis.llm.types import LLMResponse
from trellis.mcp.reconcile import (
    ReconcileDecision,
    ReconcileFallbackReason,
    ReconcileOutcome,
    emit_reconcile_degraded,
)
from trellis.schemas.memory_op import REF_TYPE_DOCUMENT
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli.exit_codes import EXIT_OK, EXIT_STORE
from trellis_cli.main import app

runner = CliRunner()

_BASE = "The staging gateway listens on port 9450 for inbound sync jobs."
_NEAR = "The staging gateway listens on port 9451 for inbound sync jobs."
_ARGV = ["curate", "rejudge-withheld"]


class _Judge:
    """A mock ``LLMClient`` that answers SUPERSEDE and counts its calls."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **_kwargs: Any) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content=json.dumps({"decision": "supersede", "confidence": 0.9}),
            model="m",
        )


def _withheld(registry: StoreRegistry) -> tuple[str, str]:
    """An older unlabelled-or-labelled row, and a newer one left under ``skipped``."""
    docs = registry.knowledge.document_store
    label = {"data_classification": "confidential"}
    put_document(docs, None, "old", _BASE, dict(label), preserve_updated_at=False)
    put_document(
        docs,
        None,
        "new",
        _NEAR,
        {**label, "reconciliation": "skipped"},
        preserve_updated_at=False,
    )
    emit_reconcile_degraded(
        registry.operational.event_log,
        outcome=ReconcileOutcome(
            decision=ReconcileDecision.ADD,
            confidence=0.0,
            model_id="m",
            fallback=True,
            fallback_reason=ReconcileFallbackReason.EGRESS_WITHHELD,
        ),
        subject_ref_type=REF_TYPE_DOCUMENT,
        subject_ref_id="new",
    )
    return "old", "new"


@pytest.fixture
def judge(monkeypatch: pytest.MonkeyPatch) -> _Judge:
    client = _Judge()
    monkeypatch.setattr(rejudge_mod, "_build_llm_client", lambda _r, _c: client)
    return client


def _install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, local: bool
) -> TierRoutedRegistry:
    stores = tmp_path / "stores"
    stores.mkdir()
    registry = TierRoutedRegistry(
        StoreRegistry(stores_dir=stores),
        per_consumer_config(LLMConsumer.RECONCILE, local=local),
    )
    _withheld(registry)  # type: ignore[arg-type]
    monkeypatch.setattr(command_mod, "_get_registry", lambda: registry)
    return registry


def _invoke(*extra: str) -> Any:
    return runner.invoke(app, [*_ARGV, *extra])


def _lifecycle(registry: Any, doc_id: str) -> Any:
    return (registry.knowledge.document_store.get(doc_id)["metadata"]).get("lifecycle")


class TestReportOnlyByDefault:
    def test_nothing_is_written_without_apply(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, judge: _Judge
    ) -> None:
        registry = _install(monkeypatch, tmp_path, local=True)
        docs = registry.knowledge.document_store
        before = {d: docs.get(d) for d in ("old", "new")}
        events = len(registry.operational.event_log.get_events(limit=100))

        result = _invoke("--format", "json")

        payload = json.loads(result.output)
        assert result.exit_code == EXIT_OK
        assert payload["status"] == "ok"
        assert payload["dry_run"] is True
        # The pair is one --apply would judge; report-only does not call the judge.
        assert (payload["would_judge"], payload["judged"], payload["applied"]) == (
            1,
            0,
            0,
        )
        assert judge.calls == 0
        assert {d: docs.get(d) for d in ("old", "new")} == before
        assert len(registry.operational.event_log.get_events(limit=100)) == events

    def test_the_text_report_names_the_same_counter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, judge: _Judge
    ) -> None:
        _install(monkeypatch, tmp_path, local=True)

        result = _invoke("--format", "text")

        out = plain(result.output)
        assert result.exit_code == EXIT_OK
        assert "would be judged by --apply" in out
        assert "judge was not called" in out
        assert judge.calls == 0

    def test_apply_writes_and_the_next_run_finds_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, judge: _Judge
    ) -> None:
        registry = _install(monkeypatch, tmp_path, local=True)

        result = _invoke("--apply", "--format", "json")

        payload = json.loads(result.output)
        assert result.exit_code == EXIT_OK
        assert payload["dry_run"] is False
        assert (payload["judged"], payload["applied"]) == (1, 1)
        assert _lifecycle(registry, "old")["state"] == "superseded"
        assert (
            len(
                registry.operational.event_log.get_events(
                    event_type=EventType.MEMORY_OP_JUDGED, limit=10
                )
            )
            == 1
        )
        again = json.loads(_invoke("--apply", "--format", "json").output)
        assert (again["found"], again["no_longer_pending"], again["judged"]) == (
            1,
            1,
            0,
        )
        assert judge.calls == 1


class TestPendingIsNotAnError:
    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_a_pair_still_withheld_exits_zero_in_both_formats(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        judge: _Judge,
        fmt: str,
    ) -> None:
        _install(monkeypatch, tmp_path, local=False)

        result = _invoke("--apply", "--format", fmt)

        assert result.exit_code == EXIT_OK
        assert judge.calls == 0
        if fmt == "json":
            payload = json.loads(result.output)
            assert (payload["status"], payload["still_withheld"]) == ("ok", 1)
        else:
            assert "still withheld" in plain(result.output)


class TestOperationalFailureExitsAlikeInBothFormats:
    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_a_refused_write_exits_store_in_both_formats(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        judge: _Judge,
        fmt: str,
    ) -> None:
        registry = _install(monkeypatch, tmp_path, local=True)

        def refuse(*_a: Any, **_k: Any) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr(rejudge_mod, "mark_document_superseded", refuse)

        result = _invoke("--apply", "--format", fmt)

        assert result.exit_code == EXIT_STORE
        if fmt == "json":
            payload = json.loads(result.output)
            assert (payload["status"], payload["apply_failures"]) == ("error", 1)
        # The row kept its pending state, so the next run retries it.
        assert _lifecycle(registry, "old") is None

    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_an_unreadable_store_exits_store_in_both_formats(
        self, monkeypatch: pytest.MonkeyPatch, fmt: str
    ) -> None:
        def boom(*_a: Any, **_k: Any) -> None:
            msg = "event log unreadable [type=missing, loc=x]"
            raise StoreError(msg)

        monkeypatch.setattr(command_mod, "_get_registry", object)
        monkeypatch.setattr(command_mod, "rejudge_withheld", boom)

        result = _invoke("--format", fmt)

        assert result.exit_code == EXIT_STORE
        if fmt == "json":
            assert json.loads(result.output)["status"] == "error"
        else:
            # Rich would delete a bracketed word from unescaped exception text.
            assert "[type=missing, loc=x]" in plain(result.output)
