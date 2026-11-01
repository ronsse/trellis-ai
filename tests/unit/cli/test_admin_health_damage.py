"""``admin health`` does not read a damaged store file as healthy.

Random bytes in ``documents.db`` used to read ``true`` and exit 0, because the
check was "does the file exist". A present file is now opened read-only and
must read as a database; a damaged one exits 5 in both formats. A missing file
still exits 0 (``admin init`` creates none, so absence is the normal state of a
fresh install) unless ``--require-stores`` asks for it to fail.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from trellis_cli.exit_codes import EXIT_OK, EXIT_STORE
from trellis_cli.main import app

runner = CliRunner()


@pytest.fixture
def stores_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    assert runner.invoke(app, ["admin", "init"]).exit_code == EXIT_OK
    return tmp_path / "data" / "stores"


def _health(*args: str) -> tuple[int, dict, str]:
    as_json = runner.invoke(app, ["admin", "health", *args, "--format", "json"])
    as_text = runner.invoke(app, ["admin", "health", *args])
    assert as_json.exit_code == as_text.exit_code, (as_json.output, as_text.output)
    return as_json.exit_code, json.loads(as_json.stdout), as_text.output


def _wal_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()


def _listing(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def test_random_bytes_in_a_store_file_fail_health_in_both_formats(
    stores_dir: Path,
) -> None:
    junk = bytes(range(256)) * 16
    (stores_dir / "documents.db").write_bytes(junk)

    exit_code, payload, text = _health()

    assert exit_code == EXIT_STORE
    assert payload["documents.db"] is True  # the file is there; it is not valid
    assert payload["damaged"] == {"documents.db": "not a valid SQLite database"}
    assert "DAMAGED" in text
    # Read-only: the bytes are untouched and nothing else appeared beside them.
    assert (stores_dir / "documents.db").read_bytes() == junk
    assert _listing(stores_dir) == ["documents.db"]


def test_a_truncated_database_fails_health(stores_dir: Path) -> None:
    _wal_database(stores_dir / "graph.db")
    whole = (stores_dir / "graph.db").read_bytes()
    (stores_dir / "graph.db").write_bytes(whole[:60])

    exit_code, payload, _ = _health()

    assert exit_code == EXIT_STORE
    assert list(payload["damaged"]) == ["graph.db"]


def test_a_healthy_wal_database_passes_and_leaves_no_side_files(
    stores_dir: Path,
) -> None:
    _wal_database(stores_dir / "events.db")
    (stores_dir / "traces.db").touch()  # empty is a valid empty database
    before = _listing(stores_dir)

    exit_code, payload, _ = _health()

    assert exit_code == EXIT_OK
    assert "damaged" not in payload
    assert payload["events.db"] is True
    assert payload["traces.db"] is True
    assert _listing(stores_dir) == before


def test_a_missing_store_file_stays_exit_zero_and_is_never_created(
    stores_dir: Path,
) -> None:
    exit_code, payload, _ = _health()

    assert exit_code == EXIT_OK
    assert payload["documents.db"] is False
    assert _listing(stores_dir) == []


def test_require_stores_fails_on_a_missing_store_file(stores_dir: Path) -> None:
    exit_code, payload, text = _health("--require-stores")

    assert exit_code == EXIT_STORE
    assert payload["documents.db"] is False
    assert "MISSING" in text
    assert _listing(stores_dir) == []


def test_require_stores_passes_when_every_sqlite_file_is_there(
    stores_dir: Path,
) -> None:
    for name in ("documents", "graph", "vectors", "events", "traces"):
        _wal_database(stores_dir / f"{name}.db")

    exit_code, payload, _ = _health("--require-stores")

    assert exit_code == EXIT_OK
    assert all(payload[f"{n}.db"] for n in ("documents", "graph", "vectors", "events"))


def test_an_absent_file_on_a_store_for_another_backend_is_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    data_dir = tmp_path / "data"
    config = {
        "data_dir": str(data_dir),
        "knowledge": {
            "document": {"backend": "postgres"},
            "graph": {"backend": "postgres"},
            "vector": {"backend": "pgvector"},
        },
        "operational": {
            "trace": {"backend": "postgres"},
            "event_log": {"backend": "postgres"},
        },
    }
    (config_dir / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    (data_dir / "stores").mkdir(parents=True)

    exit_code, payload, _ = _health("--require-stores")

    assert exit_code == EXIT_OK
    assert not [k for k in payload if k.endswith(".db")]
    assert payload["backends"]["document"] == "postgres"
