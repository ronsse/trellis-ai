"""Tests for ``trellis classify label-candidates``.

Runs the command through CliRunner against real SQLite stores in a tmp
config/data dir, the harness ``test_classify_backfill`` uses.

The command's contract is mostly negative, so most of this file proves an
absence: nothing written, and the matched value on no channel. An absence test
that cannot fail proves nothing, so the leak and write checks are run against
deliberately defective subjects in ``TestTheChecksCanFail``.

Fixture values are fake: ``example.com``, the ``555-01xx`` range and a card
number the networks publish for testing.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
import typer
from structlog.testing import capture_logs
from typer.testing import CliRunner

from tests.cli_output import plain
from tests.structlog_isolation import clear_cached_logger_proxies
from trellis.classify.label_proposer import (
    LabelFinding,
    LabelScanReport,
    scan_for_label_candidates,
)
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY
from trellis.stores.base.event_log import EventType
from trellis_cli.admin import admin_app
from trellis_cli.classify import classify_app
from trellis_cli.main import app
from trellis_cli.stores import _get_registry, _reset_registry

runner = CliRunner()

#: Values the report must never carry. Each is matched by one rule, so each
#: shows up in a finding's *count* and must not show up as a *value*.
EMAIL = "sentinel.leak.c0ffee@example.com"
PHONE = "(212) 555-0147"
SSN = "123-45-6789"
CARD = "4111 1111 1111 1111"

#: Words sitting beside the matches. A report that quoted "the text around the
#: match" would carry these.
CONTEXT_BEFORE = "PRECONTEXTALPHA"
CONTEXT_AFTER = "POSTCONTEXTOMEGA"

#: Fragments of the above that no part of the report may print: the values,
#: pieces of them, and the digits-only spelling of the card and phone.
FORBIDDEN_FRAGMENTS = (
    EMAIL,
    "sentinel.leak",
    "c0ffee",
    "example.com",
    PHONE,
    "555-0147",
    "5550147",
    SSN,
    "45-6789",
    CARD,
    "4111",
    "4111111111111111",
    CONTEXT_BEFORE,
    CONTEXT_AFTER,
)

SENSITIVE_TEXT = (
    f"{CONTEXT_BEFORE} email {EMAIL} phone {PHONE} ssn {SSN} card {CARD} "
    f"{CONTEXT_AFTER}"
)


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated config/data dirs with initialised SQLite stores."""
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    init = runner.invoke(admin_app, ["init"])
    assert init.exit_code == 0, init.output
    _reset_registry()
    return tmp_path


def _put(doc_id: str, content: str, metadata: dict[str, Any] | None = None) -> None:
    _get_registry().knowledge.document_store.put(doc_id, content, metadata or {})


def _invoke(*args: str, fmt: str = "json") -> Any:
    return runner.invoke(classify_app, ["label-candidates", "--format", fmt, *args])


def _run_json(*args: str) -> dict[str, Any]:
    result = _invoke(*args)
    assert result.exit_code == 0, result.output
    # All of stdout, as ``| jq`` reads it.
    return json.loads(result.stdout)


@pytest.fixture
def captured_logs() -> Iterator[list[dict[str, Any]]]:
    """Every structlog event the command emits, at any level.

    The CLI conftest mutes structlog to CRITICAL and caches that on each lazy
    proxy, which would leave ``capture_logs`` empty and a leak check over it
    vacuous. Resetting first lets it see everything; the tests assert the
    command's own event arrived, so an empty capture fails rather than passes.
    """
    structlog.reset_defaults()
    clear_cached_logger_proxies()
    with capture_logs() as logs:
        yield logs
    clear_cached_logger_proxies()
    structlog.reset_defaults()


def _prose(text: str) -> str:
    """Rich output with its colour and its 80-column wrapping undone."""
    return " ".join(plain(text).split())


def _leaked(*channels: str) -> list[str]:
    """Forbidden fragments found in any channel, as a sorted list.

    A channel is also read with its whitespace removed, so a value that Rich's
    line wrapping split across two lines is still found.
    """
    found: set[str] = set()
    for text in channels:
        squeezed = "".join(text.split())
        for frag in FORBIDDEN_FRAGMENTS:
            if frag in text or "".join(frag.split()) in squeezed:
                found.add(frag)
    return sorted(found)


def _snapshot() -> dict[str, Any]:
    """Everything the stores and the data dir hold that a write could change."""
    registry = _get_registry()
    docs = registry.knowledge.document_store.list_documents(limit=10_000)
    data_dir = Path(str(registry.stores_dir)).parent
    return {
        "documents": json.dumps(docs, sort_keys=True, default=str),
        "events": registry.operational.event_log.count(),
        "traces": registry.operational.trace_store.count(),
        "nodes": registry.knowledge.graph_store.count_nodes(),
        "edges": registry.knowledge.graph_store.count_edges(),
        "vectors": registry.knowledge.vector_store.count(),
        "files": sorted(
            str(p.relative_to(data_dir))
            for p in data_dir.rglob("*")
            if p.is_file() and not p.name.endswith(("-wal", "-shm"))
        ),
    }


def _seed_mixed() -> None:
    _put("doc-flagged", SENSITIVE_TEXT)
    _put("doc-plain", "release notes for the widget service")
    _put(
        "doc-labelled",
        SENSITIVE_TEXT,
        {DATA_CLASSIFICATION_KEY: {"sensitivity": "restricted"}},
    )


class TestWiring:
    def test_the_command_is_on_the_classify_group(self) -> None:
        names = {command.name for command in classify_app.registered_commands}
        assert "label-candidates" in names

    def test_classify_is_on_the_root_app(self) -> None:
        assert "classify" in [group.name for group in app.registered_groups]


class TestReport:
    def test_json_names_the_document_the_rule_and_a_count(self, cli_env: Path) -> None:
        _seed_mixed()

        payload = _run_json()

        assert payload["status"] == "ok"
        assert payload["findings"] == [
            {"doc_id": "doc-flagged", "rule": "email_address", "matches": 1},
            {"doc_id": "doc-flagged", "rule": "phone_number", "matches": 1},
            {"doc_id": "doc-flagged", "rule": "us_ssn_shape", "matches": 1},
            {"doc_id": "doc-flagged", "rule": "card_number_luhn", "matches": 1},
        ]
        assert payload["documents_flagged"] == 1

    def test_labelled_documents_are_skipped_and_counted(self, cli_env: Path) -> None:
        _seed_mixed()

        payload = _run_json()

        assert payload["documents_read"] == 3
        assert payload["skipped_labelled"] == 1
        assert payload["documents_scanned"] == 2
        assert "doc-labelled" not in {f["doc_id"] for f in payload["findings"]}

    def test_the_report_says_it_is_not_a_pii_guarantee(self, cli_env: Path) -> None:
        _seed_mixed()

        payload = _run_json()
        text = _prose(_invoke(fmt="text").output)

        assert "A miss proves nothing" in payload["notice"]
        assert "not a PII guarantee" in payload["notice"]
        assert "A miss proves nothing" in text
        assert "not a PII guarantee" in text

    def test_limit_stops_the_scan_and_the_report_says_so(self, cli_env: Path) -> None:
        for i in range(4):
            _put(f"doc-{i}", f"alice{i}@example.com")

        payload = _run_json("--limit", "2")

        assert payload["documents_read"] == 2
        assert payload["limit"] == 2
        assert payload["stopped_at_limit"] is True
        assert payload["status"] == "partial"
        text = _prose(_invoke("--limit", "2", fmt="text").output)
        assert "Stopped at --limit 2" in text
        assert "not the whole store" in text

    def test_a_whole_store_scan_does_not_claim_to_have_stopped(
        self, cli_env: Path
    ) -> None:
        for i in range(2):
            _put(f"doc-{i}", f"alice{i}@example.com")

        payload = _run_json("--limit", "2")

        assert payload["stopped_at_limit"] is False
        assert payload["status"] == "ok"
        assert "Stopped at" not in _prose(_invoke("--limit", "2", fmt="text").output)

    def test_an_empty_store_reports_a_complete_empty_scan(self, cli_env: Path) -> None:
        payload = _run_json()

        assert payload["findings"] == []
        assert payload["documents_read"] == 0
        assert payload["stopped_at_limit"] is False

    def test_text_output_lists_the_document_and_its_rules(self, cli_env: Path) -> None:
        _seed_mixed()

        result = _invoke(fmt="text")

        assert result.exit_code == 0, result.output
        text = _prose(result.stdout)
        assert (
            "doc-flagged: email_address x1, phone_number x1, "
            "us_ssn_shape x1, card_number_luhn x1"
        ) in text
        assert "2 scanned" not in text  # the wording is "of N documents scanned"
        assert "1 of 2 documents scanned" in text
        assert "1 already labelled" in text
        assert "classify-sensitivity" in text

    def test_a_document_id_prints_verbatim_through_rich(self, cli_env: Path) -> None:
        """An id with ``[tag]`` and ``:name:`` in it is copyable, not mangled."""
        doc_id = "corpus:notes:[draft]:book:0001"
        _put(doc_id, "mail alice@example.com")

        text = _prose(_invoke(fmt="text").stdout)

        assert doc_id in text


class TestExitParity:
    """An exit code does not depend on ``--format``."""

    @pytest.mark.parametrize("args", [(), ("--limit", "1")])
    def test_a_scan_exits_zero_on_both_surfaces(
        self, cli_env: Path, args: tuple[str, ...]
    ) -> None:
        _seed_mixed()

        assert _invoke(*args, fmt="json").exit_code == 0
        assert _invoke(*args, fmt="text").exit_code == 0

    def test_uninitialised_stores_exit_alike_and_create_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        _reset_registry()

        as_json = _invoke(fmt="json")
        _reset_registry()
        as_text = _invoke(fmt="text")

        assert as_json.exit_code == as_text.exit_code
        assert as_json.exit_code != 0
        assert not (tmp_path / "data").exists()


class TestWritesNothing:
    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_no_store_and_no_file_changes(self, cli_env: Path, fmt: str) -> None:
        _seed_mixed()
        before = _snapshot()

        result = _invoke(fmt=fmt)

        assert result.exit_code == 0, result.output
        assert _snapshot() == before

    def test_no_label_is_written_to_a_flagged_document(self, cli_env: Path) -> None:
        _seed_mixed()

        _run_json()

        doc = _get_registry().knowledge.document_store.get("doc-flagged")
        assert doc is not None
        assert DATA_CLASSIFICATION_KEY not in doc["metadata"]
        assert "content_tags" not in doc["metadata"]


class TestNoMatchedValueOnAnyChannel:
    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_stdout_stderr_and_logs_carry_no_value_or_context(
        self,
        cli_env: Path,
        captured_logs: list[dict[str, Any]],
        fmt: str,
    ) -> None:
        _seed_mixed()

        result = _invoke(fmt=fmt)

        assert result.exit_code == 0, result.output
        # Not vacuous: the command's own event is in the capture, and the
        # report really flagged the document.
        assert any(
            log["event"] == "label_candidates_scan_completed" for log in captured_logs
        )
        assert "doc-flagged" in result.stdout
        assert (
            _leaked(
                result.stdout,
                result.stderr,
                result.output,
                json.dumps(captured_logs, default=str),
            )
            == []
        )

    def test_the_findings_in_json_have_exactly_three_keys(self, cli_env: Path) -> None:
        _seed_mixed()

        for finding in _run_json()["findings"]:
            assert set(finding) == {"doc_id", "rule", "matches"}


class TestTheChecksCanFail:
    """The absence checks above, run against subjects built to break them."""

    @staticmethod
    def _defect(
        monkeypatch: pytest.MonkeyPatch,
        change: Callable[[LabelScanReport], None],
    ) -> None:
        def scan(store: Any, **kwargs: Any) -> LabelScanReport:
            report = scan_for_label_candidates(store, **kwargs)
            change(report)
            return report

        monkeypatch.setattr("trellis_cli.classify.scan_for_label_candidates", scan)

    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_a_finding_that_quotes_the_value_is_caught(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch, fmt: str
    ) -> None:
        _seed_mixed()

        def quote(report: LabelScanReport) -> None:
            report.findings[0] = LabelFinding("doc-flagged", f"email:{EMAIL}", 1)

        self._defect(monkeypatch, quote)
        result = _invoke(fmt=fmt)

        assert set(_leaked(result.stdout)) == {
            "c0ffee",
            "example.com",
            "sentinel.leak",
            EMAIL,
        }

    def test_a_finding_that_quotes_the_text_around_a_match_is_caught(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_mixed()

        def quote(report: LabelScanReport) -> None:
            report.findings[0] = LabelFinding("doc-flagged", CONTEXT_AFTER, 1)

        self._defect(monkeypatch, quote)

        assert _leaked(_invoke().stdout) == [CONTEXT_AFTER]

    def test_a_leak_on_stderr_is_caught(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_mixed()
        self._defect(monkeypatch, lambda _report: typer.echo(SSN, err=True))

        result = _invoke()

        assert result.exit_code == 0, result.output
        assert _leaked(result.stdout) == []
        assert set(_leaked(result.stderr)) == {"45-6789", SSN}

    def test_a_leak_in_a_log_line_is_caught(
        self,
        cli_env: Path,
        captured_logs: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_mixed()
        self._defect(
            monkeypatch,
            lambda _report: structlog.get_logger("defect").warning("leak", value=PHONE),
        )

        result = _invoke()

        assert _leaked(result.stdout, result.stderr) == []
        assert set(_leaked(json.dumps(captured_logs, default=str))) == {
            "555-0147",
            PHONE,
        }

    @pytest.mark.parametrize(
        "write",
        [
            pytest.param(
                lambda: _put("doc-flagged", SENSITIVE_TEXT, {"label": "x"}),
                id="metadata-rewrite",
            ),
            pytest.param(
                lambda: _get_registry().operational.event_log.emit(
                    next(iter(EventType)), "defect"
                ),
                id="event",
            ),
            pytest.param(
                lambda: (
                    Path(str(_get_registry().stores_dir))
                    .joinpath("queue.json")
                    .write_text("{}")
                ),
                id="queue-file",
            ),
        ],
    )
    def test_a_write_is_caught_by_the_snapshot(
        self,
        cli_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        write: Callable[[], object],
    ) -> None:
        _seed_mixed()
        before = _snapshot()
        self._defect(monkeypatch, lambda _report: write())

        result = _invoke()

        assert result.exit_code == 0, result.output
        assert _snapshot() != before
