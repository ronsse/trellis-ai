"""``trellis retrieve pack`` emits and prints the nearby section.

JSON carries ``pack.metadata["nearby"]`` under ``nearby`` (``null`` when the
pack has none, as ``POST /api/v1/packs`` does); text prints the rendered
section after the items. The exit code does not depend on ``--format``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import trellis_cli.retrieve as retrieve_cli
from tests.cli_output import plain
from tests.unit.retrieve.test_nearby import WEAK_IDS, _builder
from trellis.core.write_config import ENV_VAR_BY_FIELD
from trellis_cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    for name in ENV_VAR_BY_FIELD.values():
        monkeypatch.delenv(name, raising=False)


def _use(monkeypatch: pytest.MonkeyPatch, nearby: str) -> list[Any]:
    builder = _builder(nearby=nearby)
    packs: list[Any] = []
    build = builder.build

    def _build(*a: Any, **k: Any) -> Any:
        packs.append(build(*a, **k))
        return packs[-1]

    builder.build = _build  # type: ignore[method-assign]
    monkeypatch.setattr(retrieve_cli, "build_pack_builder", lambda *_a, **_k: builder)
    return packs


def _run(*extra: str) -> Any:
    return runner.invoke(app, ["retrieve", "pack", "--intent", "q", *extra])


def test_json_carries_the_block(monkeypatch: pytest.MonkeyPatch) -> None:
    packs = _use(monkeypatch, "on")
    result = _run("--format", "json")
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    (pack,) = packs
    assert body["nearby"] == pack.metadata["nearby"]
    assert set(body["nearby"]["listed_item_ids"]) == WEAK_IDS


def test_json_off_is_null(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, "off")
    result = _run("--format", "json")
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert "nearby" in body
    assert body["nearby"] is None


def test_text_prints_the_section_after_the_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packs = _use(monkeypatch, "on")
    result = _run()
    assert result.exit_code == 0, result.output
    out = plain(result.output)
    (pack,) = packs
    # Printed verbatim: Rich neither ate ``[...]`` nor the ids.
    assert pack.metadata["nearby"]["markdown"] in out
    last_item = pack.items[-1].item_id
    assert out.index(f"] {last_item} (") < out.index("## Nearby")


def test_text_off_prints_no_section(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, "off")
    result = _run()
    assert result.exit_code == 0, result.output
    assert "Nearby" not in plain(result.output)


def test_exit_code_does_not_depend_on_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, "on")
    codes = {_run().exit_code, _run("--format", "json").exit_code}
    assert codes == {0}
