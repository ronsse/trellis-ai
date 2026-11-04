"""``admin backup`` for Postgres stores: ``pg_dump`` with the DSN out of sight.

A DSN carries a password. A command line is readable by every user on the host
(``ps``, ``/proc/<pid>/cmdline``) and ends up in shell history and CI logs, so
the contract here is that no part of a DSN is ever on the argv, and the
password is in no output, log line or manifest. The tests run the real
:func:`run_pg_dump` and replace only ``subprocess.run`` and ``shutil.which``,
so the argv and environment they assert on are the ones the tool would get. The
password is a sentinel no other text contains.

The real dump against a live server is in ``test_postgres_ops_live.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from structlog.testing import capture_logs
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.ops import backup_postgres
from trellis.ops.backup_postgres import (
    DUMP_FAILED,
    DUMPED,
    TOOL_MISSING,
    UnsupportedDsnError,
    pg_env_from_dsn,
    run_pg_dump,
)
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = CliRunner()

SECRET = "s3ntinel-Pa55w0rd-9f2c"  # noqa: S105 - a sentinel, not a credential
KNOWLEDGE_DSN = f"postgresql://trellis:{SECRET}@kg-db.example.test:5432/knowledge"
OPERATIONAL_DSN = f"postgresql://trellis:{SECRET}@ops-db.example.test:5432/operational"


class FakePgDump:
    """Stands in for ``subprocess.run``; records every call and writes the dump."""

    def __init__(self, *, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), dict(kwargs["env"])))
        if self.returncode == 0:
            target = next(a for a in argv if a.startswith("--file="))[len("--file=") :]
            Path(target).write_bytes(b"PGDMP-fake-custom-format")
        return subprocess.CompletedProcess(
            argv, self.returncode, stdout="", stderr=self.stderr
        )


@pytest.fixture
def fake_dump(monkeypatch: pytest.MonkeyPatch) -> FakePgDump:
    fake = FakePgDump()
    monkeypatch.setattr(
        backup_postgres.shutil, "which", lambda _name: "/usr/bin/pg_dump"
    )
    monkeypatch.setattr(backup_postgres.subprocess, "run", fake)
    return fake


class TestDsnToEnvironment:
    def test_a_uri_maps_to_libpq_variables(self) -> None:
        env = pg_env_from_dsn(
            "postgresql://alice:pw@db.example.test:6432/mydb?sslmode=require"
        )

        assert env["PGHOST"] == "db.example.test"
        assert env["PGPORT"] == "6432"
        assert env["PGUSER"] == "alice"
        assert env["PGPASSWORD"] == "pw"
        assert env["PGDATABASE"] == "mydb"
        assert env["PGSSLMODE"] == "require"

    def test_percent_encoding_and_ipv6_hosts_are_decoded(self) -> None:
        env = pg_env_from_dsn("postgres://u:p%40ss%2Fw%3Ard@[::1]:5433/db")

        assert env["PGPASSWORD"] == "p@ss/w:rd"
        assert env["PGHOST"] == "::1"
        assert env["PGPORT"] == "5433"

    def test_a_key_value_dsn_reads_quoted_and_escaped_values(self) -> None:
        env = pg_env_from_dsn(
            r"host=db.example.test dbname=kg user=u"
            r" password='a b\'c' sslmode=verify-full"
        )

        assert env["PGPASSWORD"] == "a b'c"
        assert env["PGHOST"] == "db.example.test"
        assert env["PGSSLMODE"] == "verify-full"

    def test_a_connect_timeout_is_always_set_so_an_unreachable_host_ends(self) -> None:
        assert pg_env_from_dsn("postgresql://u@h/db")["PGCONNECT_TIMEOUT"]
        assert (
            pg_env_from_dsn("postgresql://u@h/db?connect_timeout=5")[
                "PGCONNECT_TIMEOUT"
            ]
            == "5"
        )

    def test_an_unknown_parameter_is_refused_by_name_and_never_by_value(self) -> None:
        with pytest.raises(UnsupportedDsnError) as err:
            pg_env_from_dsn(f"postgresql://u:{SECRET}@h/db?mystery_knob=value-xyz")

        assert "mystery_knob" in str(err.value)
        assert SECRET not in str(err.value)
        assert "value-xyz" not in str(err.value)

    def test_text_that_is_neither_a_uri_nor_key_values_is_refused(self) -> None:
        with pytest.raises(UnsupportedDsnError):
            pg_env_from_dsn(f"not a dsn {SECRET}")


class TestRunPgDump:
    def test_the_argv_carries_no_part_of_the_dsn(
        self, fake_dump: FakePgDump, tmp_path: Path
    ) -> None:
        dest = tmp_path / "postgres" / "knowledge.dump"

        result = run_pg_dump(KNOWLEDGE_DSN, dest)

        assert result.status == DUMPED
        assert result.size_bytes == dest.stat().st_size > 0
        ((argv, env),) = fake_dump.calls
        assert argv[0] == "/usr/bin/pg_dump"
        assert "--format=custom" in argv
        assert "--no-password" in argv
        joined = " ".join(argv)
        for part in (SECRET, "kg-db.example.test", "trellis", "knowledge", "5432"):
            assert part not in joined.replace(str(dest), ""), part
        assert "postgres://" not in joined
        assert "postgresql://" not in joined
        # The DSN reached the child through its environment instead.
        assert env["PGPASSWORD"] == SECRET
        assert env["PGHOST"] == "kg-db.example.test"
        assert env["PGDATABASE"] == "knowledge"

    def test_the_dump_file_is_owner_only(
        self, fake_dump: FakePgDump, tmp_path: Path
    ) -> None:
        dest = tmp_path / "out" / "a.dump"

        run_pg_dump(KNOWLEDGE_DSN, dest)

        assert dest.stat().st_mode & 0o077 == 0
        assert dest.parent.stat().st_mode & 0o077 == 0

    def test_a_missing_pg_dump_has_its_own_status_and_says_what_to_install(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(backup_postgres.shutil, "which", lambda _name: None)

        result = run_pg_dump(KNOWLEDGE_DSN, tmp_path / "a.dump")

        assert result.status == TOOL_MISSING
        assert "pg_dump" in (result.reason or "")
        assert "PATH" in (result.reason or "")
        assert not (tmp_path / "a.dump").exists()

    def test_a_failure_redacts_the_password_and_removes_the_partial_file(
        self, fake_dump: FakePgDump, tmp_path: Path
    ) -> None:
        fake_dump.returncode = 1
        fake_dump.stderr = (
            f'pg_dump: error: connection failed: password "{SECRET}" rejected\n'
            f"host=kg-db.example.test password={SECRET}\n"
        )
        dest = tmp_path / "a.dump"
        dest.write_bytes(b"half written")

        with capture_logs() as logs:
            result = run_pg_dump(KNOWLEDGE_DSN, dest)

        assert result.status == DUMP_FAILED
        assert "exited 1" in (result.reason or "")
        assert SECRET not in (result.reason or "")
        assert SECRET not in json.dumps(logs, default=str)
        assert not dest.exists(), "a half-written dump must not look like a backup"

    def test_a_dsn_pg_dump_cannot_express_fails_without_running_it(
        self, fake_dump: FakePgDump, tmp_path: Path
    ) -> None:
        result = run_pg_dump(f"postgresql://u:{SECRET}@h/db?mystery=1", tmp_path / "a")

        assert result.status == DUMP_FAILED
        assert "mystery" in (result.reason or "")
        assert SECRET not in (result.reason or "")
        assert fake_dump.calls == []

    def test_an_exit_zero_that_wrote_nothing_is_a_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(backup_postgres.shutil, "which", lambda _n: "/bin/pg_dump")
        monkeypatch.setattr(
            backup_postgres.subprocess,
            "run",
            lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, "", ""),
        )

        result = run_pg_dump(KNOWLEDGE_DSN, tmp_path / "a.dump")

        assert result.status == DUMP_FAILED
        assert "wrote no file" in (result.reason or "")


class _Deployment:
    def __init__(self, root: Path) -> None:
        self.config = root / "config"
        self.data = root / "data"
        self.stores = self.data / "stores"


@pytest.fixture
def deploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Deployment:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COLUMNS", "400")
    dep = _Deployment(tmp_path)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(dep.config))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(dep.data))
    init = runner.invoke(app, ["admin", "init"])
    assert init.exit_code == 0, init.output
    return dep


def _configure_postgres(dep: _Deployment, monkeypatch: pytest.MonkeyPatch) -> None:
    dep.config.joinpath("config.yaml").write_text(
        yaml.safe_dump(
            {
                "data_dir": str(dep.data),
                "knowledge": {
                    "document": {"backend": "postgres"},
                    "graph": {"backend": "postgres"},
                    "vector": {"backend": "pgvector"},
                },
                "operational": {
                    "trace": {"backend": "postgres"},
                    "event_log": {"backend": "postgres"},
                    "api_key": {"backend": "postgres"},
                },
            }
        )
    )
    monkeypatch.setenv("TRELLIS_KNOWLEDGE_PG_DSN", KNOWLEDGE_DSN)
    monkeypatch.setenv("TRELLIS_OPERATIONAL_PG_DSN", OPERATIONAL_DSN)
    _reset_registry()


def _by_name(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {i["name"]: i for i in items}


class TestBackupCommandWithPostgres:
    def test_each_distinct_dsn_is_dumped_once_and_the_password_is_nowhere(
        self,
        deploy: _Deployment,
        fake_dump: FakePgDump,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _configure_postgres(deploy, monkeypatch)
        out = tmp_path / "bk"

        with capture_logs() as logs:
            result = runner.invoke(
                app, ["admin", "backup", "--output", str(out), "--format", "json"]
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "ok"
        stores = _by_name(payload["stores"])
        for name in ("document", "graph", "vector"):
            assert stores[name]["status"] == "copied", name
            assert stores[name]["file"] == "postgres/postgres-knowledge.dump"
        for name in ("trace", "event_log", "api_key"):
            assert stores[name]["status"] == "copied", name
            assert stores[name]["file"] == "postgres/postgres-operational.dump"
        assert len(fake_dump.calls) == 2, "one pg_dump per distinct DSN"
        assert (out / "postgres" / "postgres-knowledge.dump").is_file()
        assert (out / "postgres" / "postgres-operational.dump").is_file()
        # The sentinel appears in no stdout, stderr, manifest, log line or file name.
        manifest = (out / "manifest.json").read_text()
        everything = "\n".join(
            [
                result.output,
                result.stdout,
                result.stderr if result.stderr_bytes is not None else "",
                manifest,
                json.dumps(logs, default=str),
                "\n".join(str(p) for p in out.rglob("*")),
            ]
        )
        assert SECRET not in everything
        assert "kg-db.example.test" not in everything
        assert "ops-db.example.test" not in everything
        for argv, _env in fake_dump.calls:
            assert SECRET not in " ".join(argv)

    def test_a_missing_pg_dump_is_its_own_status_and_fails_the_run(
        self,
        deploy: _Deployment,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _configure_postgres(deploy, monkeypatch)
        monkeypatch.setattr(backup_postgres.shutil, "which", lambda _name: None)
        out = tmp_path / "bk"

        result = runner.invoke(
            app, ["admin", "backup", "--output", str(out), "--format", "json"]
        )

        assert result.exit_code == 5, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "error"
        doc = _by_name(payload["stores"])["document"]
        assert doc["status"] == "tool_missing"
        assert doc["status"] != "skipped"
        assert "pg_dump" in doc["reason"]
        # The manifest is still written, saying the same.
        assert (
            _by_name(json.loads((out / "manifest.json").read_text())["stores"])[
                "document"
            ]["status"]
            == "tool_missing"
        )

    def test_the_text_report_names_tool_missing_and_exits_the_same(
        self,
        deploy: _Deployment,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _configure_postgres(deploy, monkeypatch)
        monkeypatch.setattr(backup_postgres.shutil, "which", lambda _name: None)

        result = runner.invoke(
            app, ["admin", "backup", "--output", str(tmp_path / "bk")]
        )

        assert result.exit_code == 5, result.output
        assert "TOOL_MISSING" in plain(result.output).upper().replace(" ", "_")

    def test_a_failed_dump_is_failed_with_the_password_redacted(
        self,
        deploy: _Deployment,
        fake_dump: FakePgDump,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _configure_postgres(deploy, monkeypatch)
        fake_dump.returncode = 1
        fake_dump.stderr = f"pg_dump: error: password authentication failed {SECRET}"

        result = runner.invoke(
            app,
            ["admin", "backup", "--output", str(tmp_path / "bk"), "--format", "json"],
        )

        assert result.exit_code == 5, result.output
        payload = json.loads(result.stdout)
        assert _by_name(payload["stores"])["document"]["status"] == "failed"
        assert SECRET not in result.output
        assert SECRET not in (tmp_path / "bk" / "manifest.json").read_text()

    def test_a_postgres_store_with_no_dsn_fails_by_name(
        self,
        deploy: _Deployment,
        fake_dump: FakePgDump,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _configure_postgres(deploy, monkeypatch)
        monkeypatch.delenv("TRELLIS_KNOWLEDGE_PG_DSN")

        result = runner.invoke(
            app,
            ["admin", "backup", "--output", str(tmp_path / "bk"), "--format", "json"],
        )

        assert result.exit_code == 5, result.output
        stores = _by_name(json.loads(result.stdout)["stores"])
        assert stores["document"]["status"] == "failed"
        assert "no DSN" in stores["document"]["reason"]
        assert stores["trace"]["status"] == "copied"
