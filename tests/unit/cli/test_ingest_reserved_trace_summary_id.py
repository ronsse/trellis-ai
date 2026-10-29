"""``trellis ingest evidence`` and a ``trace-summary:`` id (#1054).

This command writes through ``put_document`` directly, so the funnel's refusal
(``tests/unit/mutate/test_reserved_trace_summary_id.py``) does not cover it and
it carries its own, with the same code and message and the same exit status
under either ``--format``.

Defective subject: drop the ``is_trace_summary_doc_id`` check in
``ingest_evidence`` and the exit code is 0 with a row written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.errors import ValidationError
from trellis.mutate.handlers import _evidence_document_id
from trellis_cli.main import app
from trellis_cli.stores import get_document_store

runner = CliRunner()

RESERVED_ID = "trace-summary:trace-0001"


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


def _evidence_file(tmp_path: Path, evidence_id: str) -> str:
    path = tmp_path / "evidence.json"
    path.write_text(
        json.dumps(
            {
                "evidence_id": evidence_id,
                "evidence_type": "snippet",
                "content": "a decoy a caller filed under a summary id",
                "source_origin": "trace",
            }
        )
    )
    return str(path)


def test_ingest_evidence_refuses_a_summary_id(tmp_path: Path) -> None:
    path = _evidence_file(tmp_path, RESERVED_ID)

    text = runner.invoke(app, ["ingest", "evidence", path])
    as_json = runner.invoke(app, ["ingest", "evidence", path, "--format", "json"])

    assert text.exit_code == 2
    assert "trace embed worker" in plain(text.stdout)
    assert as_json.exit_code == text.exit_code
    payload = json.loads(as_json.stdout.strip())
    assert payload["status"] == "error"
    assert payload["error_type"] == "ValidationError"
    assert payload["code"] == "reserved_trace_summary_id"
    assert get_document_store().get(RESERVED_ID) is None


def test_the_cli_and_the_handler_refuse_with_one_code_and_message(
    tmp_path: Path,
) -> None:
    path = _evidence_file(tmp_path, RESERVED_ID)
    as_json = runner.invoke(app, ["ingest", "evidence", path, "--format", "json"])
    payload = json.loads(as_json.stdout.strip())

    with pytest.raises(ValidationError) as excinfo:
        _evidence_document_id(RESERVED_ID)

    assert excinfo.value.code == payload["code"]
    assert (
        str(excinfo.value).replace("evidence.doc_id", "evidence_id")
        == (payload["message"])
    )


@pytest.mark.parametrize(
    "evidence_id", ["trace-summary", "Trace-Summary:x", "my-trace-summary:x"]
)
def test_an_id_that_only_resembles_the_prefix_is_written(
    tmp_path: Path, evidence_id: str
) -> None:
    path = _evidence_file(tmp_path, evidence_id)

    result = runner.invoke(app, ["ingest", "evidence", path, "--format", "json"])

    assert result.exit_code == 0, result.stdout
    assert get_document_store().get(evidence_id) is not None
