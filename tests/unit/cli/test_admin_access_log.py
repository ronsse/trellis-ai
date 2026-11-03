"""``trellis admin access-log``: per-key reads from the access-log events."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.cli_output import plain
from tests.unit.cli.test_admin_api_keys import _create_key, _init_stores, _invoke
from trellis.core.access_log import build_payload
from trellis.stores.base.event_log import EventType
from trellis_cli.exit_codes import EXIT_OK, EXIT_VALIDATION
from trellis_cli.stores import get_event_log

if TYPE_CHECKING:
    from pathlib import Path


def _read(key_id: str, *, surface: str = "rest", ids: list[str] | None = None) -> None:
    ids = ids if ids is not None else ["d1", "d2"]
    get_event_log().emit(
        EventType.KEY_READ,
        "access_log",
        entity_id=key_id,
        entity_type="api_key",
        payload=build_payload(
            key_id=key_id,
            surface=surface,
            route="GET /api/v1/search",
            item_ids=ids,
            item_count=len(ids),
        ),
    )


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    _init_stores(tmp_path, monkeypatch)
    key = _create_key("read", name="reader")
    _read(key["key_id"])
    _read(key["key_id"], surface="mcp", ids=["d3"])
    return key


def test_json_reports_reads_and_last_used_per_key(seeded: dict) -> None:
    result = _invoke(["access-log", "--format", "json"])
    assert result.exit_code == EXIT_OK, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    (row,) = payload["keys"]
    assert row["key_id"] == seeded["key_id"]
    assert row["name"] == "reader"
    assert row["revoked"] is False
    assert row["reads"] == 2
    assert row["items_returned"] == 3
    assert row["by_surface"] == {"mcp": 1, "rest": 1}
    assert row["last_read_at"]
    assert payload["failure_signals"] == 0
    assert payload["dropped_reads"] == 0
    assert payload["failed_reads"] == 0
    assert "log_failures" not in payload


def test_text_names_the_key_and_its_counts(seeded: dict) -> None:
    result = _invoke(["access-log"])
    assert result.exit_code == EXIT_OK, result.output
    text = plain(result.output)
    assert seeded["key_id"] in text
    assert "reader" in text


def test_a_key_filter_narrows_the_report(seeded: dict) -> None:
    result = _invoke(["access-log", "--key-id", "nope", "--format", "json"])
    assert result.exit_code == EXIT_OK
    assert json.loads(result.stdout)["keys"] == []


@pytest.mark.parametrize("hours", ["0", "-1", "999999"])
def test_a_window_out_of_range_exits_validation_in_both_formats(
    seeded: dict, hours: str
) -> None:
    as_text = _invoke(["access-log", "--hours", hours])
    as_json = _invoke(["access-log", "--hours", hours, "--format", "json"])
    assert as_text.exit_code == as_json.exit_code == EXIT_VALIDATION
    assert json.loads(as_json.stdout)["status"] == "error"


def test_failed_log_steps_are_counted_beside_the_keys(seeded: dict) -> None:
    get_event_log().emit(
        EventType.ACCESS_LOG_FAILED,
        "access_log",
        payload={
            "surface": "rest",
            "error_type": "RuntimeError",
            "dropped_reads": 2,
            "failed_reads": 1,
        },
    )
    payload = json.loads(_invoke(["access-log", "--format", "json"]).stdout)
    assert payload["failure_signals"] == 1
    assert payload["dropped_reads"] == 2
    assert payload["failed_reads"] == 1
    text = " ".join(plain(_invoke(["access-log"]).output).split())
    assert "1 access.log_failed signals" in text
    assert "a count of signals, not of reads" in text
    assert "at least 2 dropped" in text
    assert "at least 1 failed" in text
