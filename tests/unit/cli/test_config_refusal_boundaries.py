"""A refused config.yaml reaches the CLI and the API as a ``ConfigError``.

``StoreRegistry.from_config_dir`` refuses a config no store reads (a
misspelled plane, a store name no plane has, a store under the wrong plane,
the removed flat ``stores:`` block). Each case here goes through the real
load path from both boundaries and is held to the answer the project names
for a ``ConfigError``:

* the CLI exits ``EXIT_STORE`` (5), whichever ``--format`` is asked for;
* the API refuses to start, so no request is answered, and a ``ConfigError``
  that does surface inside a request is answered 409 ``config_error``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import trellis_api.app as app_module
from trellis.errors import ConfigError
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app, register_exception_handlers
from trellis_cli.exit_codes import EXIT_STORE
from trellis_cli.main import app

runner = CliRunner()

_CASES = {
    "typo_plane": "knowlege:\n  document:\n    backend: postgres\n",
    "storage_plane": "storage:\n  graph:\n    backend: neo4j\n",
    "unknown_store": "knowledge:\n  documents:\n    backend: postgres\n",
    "wrong_plane": "knowledge:\n  trace:\n    backend: postgres\n",
    "flat_stores": "stores:\n  graph:\n    backend: neo4j\n",
}


@pytest.fixture
def refused_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(_CASES[request.param], encoding="utf-8")
    # ``stores/`` exists, so the CLI's "not initialized" guard is not what
    # exits: only the refusal can.
    (tmp_path / "data" / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_module, "_registry", None)
    return config_dir


@pytest.mark.parametrize("refused_config", _CASES, indirect=True, ids=_CASES)
@pytest.mark.parametrize("fmt", ["text", "json"])
def test_the_cli_exits_store_whatever_the_format(
    refused_config: Path, fmt: str
) -> None:
    result = runner.invoke(app, ["admin", "stats", "--format", fmt])

    assert result.exit_code == EXIT_STORE == 5, result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("refused_config", _CASES, indirect=True, ids=_CASES)
def test_the_api_refuses_to_start(refused_config: Path) -> None:
    with pytest.raises(ConfigError) as info, TestClient(create_app()):
        pass

    assert str(refused_config / "config.yaml") in str(info.value)
    assert app_module._registry is None


@pytest.mark.parametrize("refused_config", _CASES, indirect=True, ids=_CASES)
def test_the_error_the_load_raises_answers_409_inside_a_request(
    refused_config: Path,
) -> None:
    probe = FastAPI()
    register_exception_handlers(probe)

    @probe.get("/probe")
    def _probe() -> None:
        StoreRegistry.from_config_dir()

    response = TestClient(probe, raise_server_exceptions=False).get("/probe")

    assert response.status_code == 409
    assert response.json()["code"] == "config_error"
