"""``trellis analyze health`` carries the retrieval-adoption section."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.ops.retrieval_adoption import RETRIEVAL_ADOPTION_MIN_SESSIONS
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = CliRunner()


@pytest.fixture(autouse=True)
def temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()
    return StoreRegistry(stores_dir=stores_dir)


def _sessions(registry: StoreRegistry, *, with_packs: int, without: int) -> None:
    at = datetime.now(tz=UTC) - timedelta(hours=1)
    for n in range(with_packs + without):
        registry.operational.event_log.append(
            Event(
                event_type=EventType.CAPTURE_SESSION_PACKS,
                source="worker:session-capture",
                entity_id=f"session-{n}",
                entity_type="capture_session",
                occurred_at=at,
                payload={"pack_ids": ["p"] if n < with_packs else []},
            )
        )


def test_json_carries_the_section_and_a_warn(temp_stores: StoreRegistry) -> None:
    _sessions(temp_stores, with_packs=2, without=10)

    result = runner.invoke(app, ["analyze", "health", "--format", "json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["retrieval"]["state"] == "warn"
    assert data["retrieval"]["sessions"] == 12
    assert data["retrieval"]["sessions_with_packs"] == 2
    assert data["retrieval"]["adoption"] == pytest.approx(2 / 12)
    assert data["status"] == "warn"
    assert any("retrieval adoption" in reason for reason in data["reasons"])


def test_text_prints_the_rate_and_the_warn(temp_stores: StoreRegistry) -> None:
    _sessions(temp_stores, with_packs=2, without=10)

    result = runner.invoke(app, ["analyze", "health"])

    assert result.exit_code == 0, result.output
    line = next(ln for ln in plain(result.stdout).splitlines() if "Retrieval:" in ln)
    assert "2/12" in line
    assert "17%" in line


def test_below_the_minimum_prints_a_state_not_a_rate(
    temp_stores: StoreRegistry,
) -> None:
    _sessions(temp_stores, with_packs=0, without=RETRIEVAL_ADOPTION_MIN_SESSIONS - 1)

    text = runner.invoke(app, ["analyze", "health"])
    as_json = runner.invoke(app, ["analyze", "health", "--format", "json"])

    assert text.exit_code == as_json.exit_code == 0
    line = next(ln for ln in plain(text.stdout).splitlines() if "Retrieval:" in ln)
    assert "insufficient_data" in line
    assert "%" not in line
    data = json.loads(as_json.stdout)
    assert data["retrieval"]["state"] == "insufficient_data"
    assert data["retrieval"]["adoption"] is None
    assert data["status"] == "ok"
