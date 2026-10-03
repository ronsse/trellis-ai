"""The learning-candidate writers and the Review queue resolve one directory.

``trellis worker curate`` (what a nightly cron runs) and ``trellis analyze
learning-candidates`` write ``intent_learning_candidates.json``;
``GET /api/v1/learning/candidates`` and ``POST /api/v1/learning/promotions``
read it. Each side builds its own ``StoreRegistry`` from the same
``TRELLIS_CONFIG_DIR`` / ``TRELLIS_DATA_DIR``, exactly as a host CLI and an
API container sharing a data directory do, so these tests run the writer
through the CLI and the reader through the real app lifespan rather than
handing either side a path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
import trellis_cli.stores as cli_stores
from tests.cli_output import plain
from trellis.core.path_presence import path_is_present
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app
from trellis_cli.main import app as cli_app
from trellis_cli.stores import _reset_registry

if TYPE_CHECKING:
    from tests.structlog_isolation import IsolatedCliRunner

#: The two commands that write the artifact, run with no ``--output-dir``.
WRITERS = {
    "worker-curate": ["worker", "curate", "--days", "30", "--format", "json"],
    "analyze-learning-candidates": [
        "analyze",
        "learning-candidates",
        "--days",
        "30",
        "--format",
        "json",
    ],
}

#: Synthetic graded packs: two items helpful in successful packs (promote
#: candidates) and one unhelpful in failed packs (a noise candidate), so the
#: artifact carries several candidates of more than one kind.
_SEEDS = (
    ("lrd:doc:alpha", "summarise the release notes", "lrd-alpha", True),
    ("lrd:doc:beta", "debug the failing migration", "lrd-beta", True),
    ("lrd:doc:gamma", "plan the schema change", "lrd-gamma", False),
)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fresh deployment both the CLI and the API resolve from the env."""
    for var in (
        "TRELLIS_API_KEY",
        "TRELLIS_AUTH_MODE",
        "TRELLIS_LEARNING_ARTIFACTS_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    data = tmp_path / "data"
    (data / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data))
    # The lifespan builds the API's registry from the env; start it unset.
    monkeypatch.setattr(app_module, "_registry", None)
    _reset_registry()
    _seed_graded_packs(data / "stores")
    yield data
    _reset_registry()


def _seed_graded_packs(stores_dir: Path, rounds: int = 3) -> None:
    registry = StoreRegistry(stores_dir=stores_dir)
    event_log = registry.operational.event_log
    for item_id, intent, domain, helpful in _SEEDS:
        for i in range(rounds):
            pack_id = f"{domain}-pack-{i}"
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "intent": intent,
                    "domain": domain,
                    "injected_items": [
                        {
                            "item_id": item_id,
                            "item_type": "document",
                            "rank": 0,
                            "strategy_source": "document",
                        }
                    ],
                    "injected_item_ids": [item_id],
                },
            )
            graded = {"pack_id": pack_id, "success": helpful}
            if helpful:
                graded |= {"outcome": "success", "helpful_item_ids": [item_id]}
            else:
                graded |= {"outcome": "failure", "unhelpful_item_ids": [item_id]}
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload=graded,
            )
    registry.close()


def _write_without_output_dir(runner: IsolatedCliRunner, writer: str) -> Path:
    """Run ``writer`` with no ``--output-dir``; return the artifact it wrote."""
    result = runner.invoke(cli_app, WRITERS[writer])
    assert result.exit_code == 0, result.output
    candidates_path = Path(json.loads(result.stdout)["candidates_path"])
    assert candidates_path.is_file()
    _reset_registry()
    return candidates_path


def _written_ids(candidates_path: Path) -> list[str]:
    report = json.loads(candidates_path.read_text(encoding="utf-8"))
    return [c["candidate_id"] for c in report["candidates"]]


@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_writer_without_output_dir_writes_where_the_api_reads(
    data_dir: Path, cli_runner: IsolatedCliRunner, writer: str
) -> None:
    candidates_path = _write_without_output_dir(cli_runner, writer)
    written = _written_ids(candidates_path)
    assert len(written) >= 3

    with TestClient(create_app()) as client:
        served = client.get("/api/v1/learning/candidates").json()
        promote = next(
            c["candidate_id"]
            for c in served["candidates"]
            if c["recommendation_type"] == "promote_guidance"
        )
        promoted = client.post(
            "/api/v1/learning/promotions",
            json={"decisions": [{"candidate_id": promote, "approved": True}]},
        )

    assert served["status"] == "ok"
    assert served["artifacts_dir"] == str(candidates_path.parent)
    assert [c["candidate_id"] for c in served["candidates"]] == written
    assert len({c["recommendation_type"] for c in served["candidates"]}) > 1
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["promoted_count"] == 1
    # The default both sides reach, which the deployment docs name.
    assert candidates_path.parent == data_dir / "learning"


@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_override_moves_the_writer_and_the_api_together(
    data_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli_runner: IsolatedCliRunner,
    writer: str,
) -> None:
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("TRELLIS_LEARNING_ARTIFACTS_DIR", str(elsewhere))

    candidates_path = _write_without_output_dir(cli_runner, writer)
    with TestClient(create_app()) as client:
        served = client.get("/api/v1/learning/candidates").json()

    assert candidates_path.parent == elsewhere
    assert not path_is_present(data_dir / "learning")
    assert served["status"] == "ok"
    assert served["artifacts_dir"] == str(elsewhere)
    assert [c["candidate_id"] for c in served["candidates"]] == _written_ids(
        candidates_path
    )


def test_explicit_output_dir_still_wins(
    data_dir: Path, tmp_path: Path, cli_runner: IsolatedCliRunner
) -> None:
    chosen = tmp_path / "chosen"
    result = cli_runner.invoke(
        cli_app, ["worker", "curate", "--output-dir", str(chosen), "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    assert Path(json.loads(result.stdout)["candidates_path"]).parent == chosen
    assert not path_is_present(data_dir / "learning")


@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_writer_with_nothing_to_resolve_asks_for_output_dir(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli_runner: IsolatedCliRunner,
    writer: str,
) -> None:
    # A registry with no stores_dir and no override: the writer cannot name
    # the directory the API reads, so it refuses rather than guess one.
    monkeypatch.setattr(cli_stores, "_registry", StoreRegistry())
    result = cli_runner.invoke(cli_app, WRITERS[writer])
    assert result.exit_code == 2
    # A usage error is Typer's own rendering, coloured wherever CI sets
    # GITHUB_ACTIONS, so read it through ``plain``.
    assert "--output-dir" in plain(result.output)
    assert not path_is_present(data_dir / "learning")


def test_api_names_the_directory_nothing_was_written_to(data_dir: Path) -> None:
    with TestClient(create_app()) as client:
        served = client.get("/api/v1/learning/candidates").json()
        refused = client.post(
            "/api/v1/learning/promotions",
            json={"decisions": [{"candidate_id": "lrd:any", "approved": True}]},
        )

    expected_dir = str(data_dir / "learning")
    assert served["status"] == "error"
    assert served["code"] == "learning_artifacts_dir_missing"
    assert served["artifacts_dir"] == expected_dir
    assert expected_dir in served["hint"]
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "learning_artifacts_dir_missing"
    assert refused.json()["detail"]["path"] == expected_dir
