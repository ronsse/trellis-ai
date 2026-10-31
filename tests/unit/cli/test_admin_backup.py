"""``trellis admin backup``: a copy of a live SQLite store that loses nothing.

The defect it exists for: a store keeps acknowledged writes in its write-ahead
log until a checkpoint, so a plain file copy of ``documents.db`` is a valid,
older database. It passes ``integrity_check`` and is missing rows. The tests
below run against real SQLite stores kept open in this process (so their WAL is
not checkpointed), then compare a plain copy with the command's.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli.main import app
from trellis_cli.stores import _get_registry, _reset_registry

runner = CliRunner()

_ARMS = pytest.mark.parametrize("arm", [[], ["--format", "json"]], ids=["text", "json"])

# Few enough small writes that no checkpoint folds the WAL into the main file.
_ROWS = 30


class _Deployment:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.config = root / "config"
        self.data = root / "data"
        self.stores = self.data / "stores"


@pytest.fixture
def deploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Deployment:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COLUMNS", "400")  # Rich must not fold a path
    dep = _Deployment(tmp_path)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(dep.config))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(dep.data))
    init = runner.invoke(app, ["admin", "init"])
    assert init.exit_code == 0, init.output
    _reset_registry()
    return dep


def _seed(count: int = _ROWS) -> None:
    registry = _get_registry()
    for i in range(count):
        registry.knowledge.document_store.put(f"doc-{i}", f"memory body {i}", {})
        registry.operational.event_log.emit(
            EventType.FEEDBACK_RECORDED, source="test", entity_id=f"e-{i}"
        )


def _backup(out: Path, *extra: str):
    return runner.invoke(app, ["admin", "backup", "--output", str(out), *extra])


def _backup_json(out: Path, *, code: int = 0) -> dict[str, Any]:
    result = _backup(out, "--format", "json")
    assert result.exit_code == code, result.output
    return json.loads(result.stdout)


def _by_name(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {i["name"]: i for i in items}


def _count(path: Path, table: str) -> int:
    """Rows in ``table``; a copy with no such table has lost all of them."""
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def _doc_ids(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT doc_id FROM documents")}
    finally:
        conn.close()


def test_command_is_registered() -> None:
    result = runner.invoke(app, ["admin", "backup", "--help"])
    assert result.exit_code == 0
    assert "--output" in plain(result.output)


class TestLiveStore:
    def test_every_acknowledged_row_is_in_the_backup_and_a_file_copy_loses_them(
        self, deploy: _Deployment, tmp_path: Path
    ) -> None:
        _seed()
        # The premise, in this test's own terms: copying the main files loses
        # the rows that are still in the WAL.
        plain = tmp_path / "plain"
        plain.mkdir()
        for name in ("documents.db", "events.db"):
            shutil.copyfile(deploy.stores / name, plain / name)
        assert _count(plain / "documents.db", "documents") < _ROWS
        assert _count(plain / "events.db", "events") < _ROWS

        manifest = _backup_json(tmp_path / "bk")

        assert manifest["status"] == "ok"
        copy = tmp_path / "bk" / "stores"
        assert _count(copy / "documents.db", "documents") == _ROWS
        assert _count(copy / "events.db", "events") == _ROWS

    def test_a_writer_running_during_the_backup_loses_nothing_it_acknowledged(
        self, deploy: _Deployment, tmp_path: Path
    ) -> None:
        registry = _get_registry()
        docs = registry.knowledge.document_store
        acked: list[str] = []
        stop = threading.Event()
        started = threading.Event()
        failures: list[BaseException] = []

        def writer() -> None:
            i = 0
            try:
                while not stop.is_set():
                    doc_id = f"live-{i}"
                    docs.put(doc_id, f"body {i}", {})
                    acked.append(doc_id)  # after put returned: acknowledged
                    i += 1
                    if i >= 20:
                        started.set()
            except BaseException as exc:
                failures.append(exc)
                started.set()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            assert started.wait(timeout=30)
            before = list(acked)
            manifest = _backup_json(tmp_path / "bk")
        finally:
            stop.set()
            thread.join(timeout=30)
        assert not failures, failures

        assert manifest["status"] == "ok"
        copied = _doc_ids(tmp_path / "bk" / "stores" / "documents.db")
        assert set(before) <= copied
        assert len(copied) <= len(acked)
        assert _by_name(manifest["stores"])["document"]["integrity_check"] == "ok"

    def test_the_backup_is_self_contained_and_private(
        self, deploy: _Deployment, tmp_path: Path
    ) -> None:
        _seed()
        out = tmp_path / "bk"
        _backup_json(out)

        names = sorted(p.name for p in (out / "stores").iterdir())
        assert not [n for n in names if n.endswith(("-wal", "-shm"))]
        assert (out.stat().st_mode & 0o777) == 0o700
        assert ((out / "stores").stat().st_mode & 0o777) == 0o700
        assert ((out / "stores" / "documents.db").stat().st_mode & 0o777) == 0o600
        conn = sqlite3.connect(out / "stores" / "documents.db")
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        finally:
            conn.close()


class TestManifest:
    def test_names_files_counts_checks_and_version(
        self, deploy: _Deployment, tmp_path: Path
    ) -> None:
        _seed()
        deploy.stores.joinpath("settings.json").write_text("{}")
        feedback = deploy.stores / "feedback"
        feedback.mkdir()
        feedback.joinpath("pack_feedback.jsonl").write_text('{"a": 1}\n{"a": 2}\n')
        deploy.stores.joinpath("policies.json").write_text('{"policies": []}')
        out = tmp_path / "bk"

        payload = _backup_json(out)

        on_disk = json.loads((out / "manifest.json").read_text())
        assert {k: v for k, v in payload.items() if k != "output"} == on_disk
        stores = _by_name(on_disk["stores"])
        doc = stores["document"]
        assert doc["status"] == "copied"
        assert doc["file"] == "stores/documents.db"
        assert doc["size_bytes"] == (out / "stores" / "documents.db").stat().st_size
        assert doc["integrity_check"] == "ok"
        assert doc["tables"]["documents"] == _ROWS
        # An FTS index's own shadow tables are not "main tables".
        assert not [t for t in doc["tables"] if t.startswith("documents_fts_")]
        assert stores["event_log"]["tables"]["events"] == _ROWS
        assert stores["graph"]["status"] == "absent"
        assert stores["blob"]["status"] == "empty"
        files = _by_name(on_disk["files"])
        assert files["feedback_log"]["rows"] == 2
        assert files["feedback_log"]["file"] == "stores/feedback/pack_feedback.jsonl"
        assert (out / "stores" / "feedback" / "pack_feedback.jsonl").read_text() == (
            feedback / "pack_feedback.jsonl"
        ).read_text()
        assert ((out / "stores" / "feedback").stat().st_mode & 0o777) == 0o700
        assert files["policy"]["status"] == "copied"
        assert (out / "stores" / "policies.json").read_text() == '{"policies": []}'
        assert on_disk["trellis"]["version"]
        # What is under the stores directory and was not copied is named; the
        # databases and WAL sidecars it did copy are not.
        assert "settings.json" in on_disk["not_copied"]
        assert "documents.db" not in on_disk["not_copied"]
        assert not [n for n in on_disk["not_copied"] if n.endswith(("-wal", "-shm"))]

    def test_a_store_with_an_explicit_db_path_is_found(
        self, deploy: _Deployment, tmp_path: Path
    ) -> None:
        elsewhere = tmp_path / "elsewhere" / "docs-live.db"
        deploy.config.joinpath("config.yaml").write_text(
            yaml.safe_dump(
                {
                    "data_dir": str(deploy.data),
                    "knowledge": {
                        "document": {"backend": "sqlite", "db_path": str(elsewhere)}
                    },
                }
            )
        )
        _reset_registry()
        _seed()

        payload = _backup_json(tmp_path / "bk")

        assert _by_name(payload["stores"])["document"]["tables"]["documents"] == _ROWS
        assert _count(tmp_path / "bk" / "stores" / "documents.db", "documents") == _ROWS


class TestRestore:
    def test_a_restored_copy_opens_and_serves(
        self, deploy: _Deployment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed()
        deploy.stores.joinpath("policies.json").write_text('{"policies": []}')
        out = tmp_path / "bk"
        _backup_json(out)

        # The drill documented in operations.md: lay the copy out under a new
        # data dir and point the new config at it.
        restored = tmp_path / "restored"
        shutil.copytree(out / "stores", restored / "data" / "stores")
        new_config = restored / "config"
        new_config.mkdir()
        new_config.joinpath("config.yaml").write_text(
            yaml.safe_dump({"data_dir": str(restored / "data")})
        )
        # TRELLIS_DATA_DIR still names the original: config.yaml must win.
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(deploy.data))

        registry = StoreRegistry.from_config_dir(config_dir=new_config)

        assert registry.stores_dir == restored / "data" / "stores"
        docs = registry.knowledge.document_store
        assert docs.count() == _ROWS
        got = docs.get("doc-7")
        assert got is not None
        assert got["content"] == "memory body 7"
        assert docs.search("memory"), "full-text search must work on the copy"
        assert registry.operational.event_log.count() == _ROWS
        assert (registry.stores_dir / "policies.json").read_text() == (
            '{"policies": []}'
        )

    def test_config_data_dir_beats_the_env_var(
        self, deploy: _Deployment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The trap the restore doc warns about, against the code."""
        config_dir = tmp_path / "trap-config"
        config_dir.mkdir()
        keyed, from_env = tmp_path / "keyed", tmp_path / "from-env"
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(from_env))

        config_dir.joinpath("config.yaml").write_text(
            yaml.safe_dump({"data_dir": str(keyed)})
        )
        with_key = StoreRegistry.from_config_dir(config_dir=config_dir)
        config_dir.joinpath("config.yaml").write_text(yaml.safe_dump({}))
        without_key = StoreRegistry.from_config_dir(config_dir=config_dir)

        assert with_key.stores_dir == keyed / "stores"
        assert without_key.stores_dir == from_env / "stores"


class TestWhatIsNotCopied:
    @_ARMS
    def test_a_non_sqlite_backend_is_skipped_loudly(
        self, deploy: _Deployment, tmp_path: Path, arm: list[str]
    ) -> None:
        deploy.config.joinpath("config.yaml").write_text(
            yaml.safe_dump(
                {
                    "data_dir": str(deploy.data),
                    "knowledge": {"document": {"backend": "postgres"}},
                }
            )
        )
        _reset_registry()
        _get_registry().operational.event_log.emit(
            EventType.FEEDBACK_RECORDED, source="test", entity_id="e"
        )
        out = tmp_path / "bk"

        result = _backup(out, *arm)

        assert result.exit_code == 5, result.output
        if arm:
            payload = json.loads(result.stdout)
            assert payload["status"] == "incomplete"
            doc = _by_name(payload["stores"])["document"]
            assert doc["status"] == "skipped"
            assert doc["backend"] == "postgres"
            assert "pg_dump" in doc["reason"]
            assert _by_name(payload["stores"])["event_log"]["status"] == "copied"
        else:
            assert "SKIPPED" in result.output
            assert "INCOMPLETE" in result.output
        # What was copied is still there, with a manifest saying the rest was not.
        assert (out / "stores" / "events.db").exists()
        assert not (out / "stores" / "documents.db").exists()
        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["status"] == "incomplete"

    def test_a_store_that_stores_nothing_is_not_a_gap(
        self, deploy: _Deployment, tmp_path: Path
    ) -> None:
        deploy.config.joinpath("config.yaml").write_text(
            yaml.safe_dump(
                {
                    "data_dir": str(deploy.data),
                    "operational": {"event_log": {"backend": "null"}},
                }
            )
        )
        _reset_registry()
        _get_registry().knowledge.document_store.put("d", "body", {})

        payload = _backup_json(tmp_path / "bk")

        assert payload["status"] == "ok"
        assert _by_name(payload["stores"])["event_log"]["status"] == "not_applicable"

    def test_local_blobs_are_named_and_make_the_backup_incomplete(
        self, deploy: _Deployment, tmp_path: Path
    ) -> None:
        _seed(1)
        blob = deploy.stores / "blobs" / "ab" / "abcdef"
        blob.parent.mkdir(parents=True)
        blob.write_bytes(b"payload")

        payload = _backup_json(tmp_path / "bk", code=5)

        assert payload["status"] == "incomplete"
        item = _by_name(payload["stores"])["blob"]
        assert item["status"] == "skipped"
        assert item["rows"] == 1
        assert "blobs" not in " ".join(payload["not_copied"])

    @_ARMS
    def test_an_empty_stores_dir_is_an_error_not_an_empty_success(
        self, deploy: _Deployment, tmp_path: Path, arm: list[str]
    ) -> None:
        result = _backup(tmp_path / "bk", *arm)

        assert result.exit_code == 5, result.output
        if arm:
            assert json.loads(result.stdout)["status"] == "error"
        else:
            assert "FAILED" in result.output


class TestRefusals:
    @_ARMS
    def test_a_non_empty_target_is_refused_and_untouched(
        self, deploy: _Deployment, tmp_path: Path, arm: list[str]
    ) -> None:
        _seed(1)
        out = tmp_path / "bk"
        out.mkdir()
        keep = out / "earlier-backup.db"
        keep.write_bytes(b"precious")

        result = _backup(out, *arm)

        assert result.exit_code == 2, result.output
        assert [p.name for p in out.iterdir()] == ["earlier-backup.db"]
        assert keep.read_bytes() == b"precious"
        if arm:
            payload = json.loads(result.stdout)
            assert payload["status"] == "error"
            assert "not empty" in payload["message"]

    @_ARMS
    def test_a_file_as_target_is_refused(
        self, deploy: _Deployment, tmp_path: Path, arm: list[str]
    ) -> None:
        _seed(1)
        target = tmp_path / "a-file"
        target.write_text("x")

        result = _backup(target, *arm)

        assert result.exit_code == 2, result.output
        assert target.read_text() == "x"

    def test_an_existing_empty_directory_is_accepted(
        self, deploy: _Deployment, tmp_path: Path
    ) -> None:
        _seed(1)
        out = tmp_path / "empty"
        out.mkdir()

        assert _backup_json(out)["status"] == "ok"


class TestFailure:
    @_ARMS
    def test_an_unreadable_store_exits_non_zero_in_both_formats(
        self, deploy: _Deployment, tmp_path: Path, arm: list[str]
    ) -> None:
        _seed()
        deploy.stores.joinpath("graph.db").write_bytes(b"this is not a database" * 50)
        out = tmp_path / "bk"

        result = _backup(out, *arm)

        assert result.exit_code == 5, result.output
        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["status"] == "error"
        graph = _by_name(manifest["stores"])["graph"]
        assert graph["status"] == "failed"
        assert "DatabaseError" in graph["reason"]
        # No half-copy is left behind to be mistaken for a backup, and the
        # other stores were still copied.
        assert not (out / "stores" / "graph.db").exists()
        assert _count(out / "stores" / "documents.db", "documents") == _ROWS
        if arm:
            assert json.loads(result.stdout)["status"] == "error"
        else:
            assert "FAILED" in result.output
