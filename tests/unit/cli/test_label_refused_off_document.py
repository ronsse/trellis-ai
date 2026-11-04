"""``trellis curate`` refuses a sensitivity label on a non-document id (#1131).

The CLI is trusted-local and clears every label, so the only refusal here is the
id: a graph node, a trace and a deleted document are not documents, and a label
on one would be stored where no read path looks. The exit code is the same under
``--format json`` and ``--format text``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from tests.off_document_ids import (
    LIVE,
    OFF_DOCUMENT_KINDS,
    assert_nothing_written,
    seed_ids,
    stored_label,
)
from trellis_cli.main import app
from trellis_cli.stores import _get_registry

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


def _classify(doc_id: str, fmt: str) -> int:
    return runner.invoke(
        app,
        [
            "curate",
            "classify-sensitivity",
            doc_id,
            "--sensitivity",
            "restricted",
            "--format",
            fmt,
        ],
    ).exit_code


@pytest.mark.parametrize("fmt", ["json", "text"])
@pytest.mark.parametrize("kind", OFF_DOCUMENT_KINDS)
def test_classify_sensitivity_refuses_an_id_that_is_not_a_document(
    kind: str, fmt: str
) -> None:
    ids = seed_ids(_get_registry())
    assert _classify(ids[kind], fmt) != 0
    assert_nothing_written(_get_registry(), ids)


@pytest.mark.parametrize("kind", OFF_DOCUMENT_KINDS)
def test_tag_regulatory_refuses_an_id_that_is_not_a_document(kind: str) -> None:
    ids = seed_ids(_get_registry())
    result = runner.invoke(
        app, ["curate", "tag-regulatory", ids[kind], "--tag", "pii", "--format", "json"]
    )
    assert result.exit_code != 0, result.output
    assert_nothing_written(_get_registry(), ids)


def test_the_exit_code_does_not_depend_on_the_format() -> None:
    ids = seed_ids(_get_registry())
    assert _classify(ids["graph_node"], "json") == _classify(ids["graph_node"], "text")


def test_a_live_document_still_takes_a_label() -> None:
    seed_ids(_get_registry())
    assert _classify(LIVE, "json") == 0
    assert stored_label(_get_registry(), LIVE) == "restricted"
