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

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from structlog.testing import capture_logs

from tests.cli_output import plain
from trellis.errors import ConfigError
from trellis.ops import backup_postgres
from trellis.ops.backup import backup_stores
from trellis.ops.backup_postgres import (
    DUMP_FAILED,
    DUMPED,
    PG_DUMP_TIMEOUT_S,
    TIMEOUT_REASON,
    TOOL_MISSING,
    UnsupportedDsnError,
    pg_env_from_dsn,
    run_pg_dump,
)
from trellis.stores.registry import StoreRegistry
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

if TYPE_CHECKING:
    from tests.structlog_isolation import IsolatedCliRunner


#: The DSN parser is libpq's, reached through psycopg (the ``cloud`` extra). A
#: store on Postgres cannot run without it either, so what needs it is skipped
#: where it is absent and runs in the ``all`` leg and in live-infra.
needs_psycopg = pytest.mark.skipif(
    importlib.util.find_spec("psycopg") is None, reason="psycopg is not installed"
)

SECRET = "s3ntinel-Pa55w0rd-9f2c"  # noqa: S105 - a sentinel, not a credential
KNOWLEDGE_DSN = f"postgresql://trellis:{SECRET}@kg-db.example.test:5432/knowledge"
OPERATIONAL_DSN = f"postgresql://trellis:{SECRET}@ops-db.example.test:5432/operational"


class FakePgDump:
    """Stands in for ``subprocess.run``; records every call and writes the dump."""

    def __init__(self, *, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.timeouts: list[Any] = []
        self.raises: BaseException | None = None
        self.returncode = returncode
        self.stderr = stderr

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), dict(kwargs["env"])))
        self.timeouts.append(kwargs.get("timeout"))
        if self.raises is not None:
            raise self.raises
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
    @needs_psycopg
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

    @needs_psycopg
    def test_percent_encoding_and_ipv6_hosts_are_decoded(self) -> None:
        env = pg_env_from_dsn("postgres://u:p%40ss%2Fw%3Ard@[::1]:5433/db")

        assert env["PGPASSWORD"] == "p@ss/w:rd"
        assert env["PGHOST"] == "::1"
        assert env["PGPORT"] == "5433"

    @needs_psycopg
    def test_a_key_value_dsn_reads_quoted_and_escaped_values(self) -> None:
        env = pg_env_from_dsn(
            r"host=db.example.test dbname=kg user=u"
            r" password='a b\'c' sslmode=verify-full"
        )

        assert env["PGPASSWORD"] == "a b'c"
        assert env["PGHOST"] == "db.example.test"
        assert env["PGSSLMODE"] == "verify-full"

    @needs_psycopg
    def test_a_connect_timeout_is_always_set_so_an_unreachable_host_ends(self) -> None:
        assert pg_env_from_dsn("postgresql://u@h/db")["PGCONNECT_TIMEOUT"]
        assert (
            pg_env_from_dsn("postgresql://u@h/db?connect_timeout=5")[
                "PGCONNECT_TIMEOUT"
            ]
            == "5"
        )

    @needs_psycopg
    def test_an_unknown_parameter_is_refused_by_name_and_never_by_value(self) -> None:
        with pytest.raises(UnsupportedDsnError) as err:
            pg_env_from_dsn(f"postgresql://u:{SECRET}@h/db?mystery_knob=value-xyz")

        assert "mystery_knob" in str(err.value)
        assert SECRET not in str(err.value)
        assert "value-xyz" not in str(err.value)

    @needs_psycopg
    def test_text_that_is_neither_a_uri_nor_key_values_is_refused(self) -> None:
        with pytest.raises(UnsupportedDsnError):
            pg_env_from_dsn(f"not a dsn {SECRET}")

    @pytest.mark.parametrize(
        ("dsn", "password", "host", "dbname"),
        [
            ("postgresql://u:pa?ss@h.test:5433/db", "pa?ss", "h.test", "db"),
            ("postgresql://u:pa#ss@h.test/db", "pa#ss", "h.test", "db"),
            ("postgresql://u@h.test/db?password=a+b", "a+b", "h.test", "db"),
            ("postgres://u:p%40%3F%23@h.test/db", "p@?#", "h.test", "db"),
        ],
    )
    @needs_psycopg
    def test_a_password_with_uri_delimiters_reads_the_way_libpq_reads_it(
        self, dsn: str, password: str, host: str, dbname: str
    ) -> None:
        env = pg_env_from_dsn(dsn)

        assert env["PGPASSWORD"] == password
        assert env["PGHOST"] == host
        assert env["PGUSER"] == "u"
        assert env["PGDATABASE"] == dbname

    @pytest.mark.parametrize(
        "dsn",
        [
            f"postgresql://u:pa%zz{SECRET}@h.test/db",
            f"postgresql://u:{SECRET}@h.test/db?broken",
            f"host=h.test password={SECRET} nonsense",
            f"not a dsn {SECRET}",
        ],
    )
    @needs_psycopg
    def test_a_connection_string_libpq_rejects_is_refused_with_no_fragment_of_it(
        self, dsn: str
    ) -> None:
        with pytest.raises(UnsupportedDsnError) as err:
            pg_env_from_dsn(dsn)

        text = str(err.value)
        assert SECRET not in text
        assert "pa%zz" not in text
        assert "h.test" not in text

    @needs_psycopg
    def test_a_key_value_dsn_names_an_unknown_parameter_and_not_its_value(self) -> None:
        with pytest.raises(UnsupportedDsnError) as err:
            pg_env_from_dsn(f"host=h password={SECRET} mystery_knob=value-xyz")

        assert "mystery_knob" in str(err.value)
        assert SECRET not in str(err.value)
        assert "value-xyz" not in str(err.value)

    @needs_psycopg
    def test_every_secret_parameter_is_redacted_and_not_only_the_password(
        self,
    ) -> None:
        secrets = backup_postgres._secret_values(
            {
                "password": "pw-1",
                "sslpassword": "pw-2",
                "host": "h",
                "sslmode": "require",
                "user": "u",
            }
        )

        assert sorted(secrets) == ["pw-1", "pw-2"]

    @needs_psycopg
    def test_a_refusal_for_a_secret_parameter_names_it_and_not_its_value(self) -> None:
        with pytest.raises(UnsupportedDsnError) as err:
            pg_env_from_dsn(f"host=h sslpassword={SECRET}")

        assert "sslpassword" in str(err.value)
        assert SECRET not in str(err.value)


class TestWithoutPsycopg:
    def test_the_store_is_failed_with_a_fixed_message_naming_no_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_dump: FakePgDump,
    ) -> None:
        monkeypatch.setitem(sys.modules, "psycopg", None)
        monkeypatch.setitem(sys.modules, "psycopg.conninfo", None)

        result = run_pg_dump(KNOWLEDGE_DSN, tmp_path / "a.dump")

        assert result.status == DUMP_FAILED
        assert "psycopg" in (result.reason or "")
        for part in (SECRET, "kg-db.example.test", "knowledge"):
            assert part not in (result.reason or "")
        assert fake_dump.calls == []


class TestRunPgDump:
    @needs_psycopg
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

    @needs_psycopg
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

    @needs_psycopg
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

    @needs_psycopg
    def test_a_dsn_pg_dump_cannot_express_fails_without_running_it(
        self, fake_dump: FakePgDump, tmp_path: Path
    ) -> None:
        result = run_pg_dump(f"postgresql://u:{SECRET}@h/db?mystery=1", tmp_path / "a")

        assert result.status == DUMP_FAILED
        assert "mystery" in (result.reason or "")
        assert SECRET not in (result.reason or "")
        assert fake_dump.calls == []

    @needs_psycopg
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


class TestRunPgDumpChildAndLimits:
    @needs_psycopg
    def test_a_timeout_is_a_failure_with_reason_timeout_and_no_file(
        self, fake_dump: FakePgDump, tmp_path: Path
    ) -> None:
        fake_dump.raises = subprocess.TimeoutExpired(["pg_dump"], PG_DUMP_TIMEOUT_S)
        dest = tmp_path / "a.dump"
        dest.write_bytes(b"half written")

        result = run_pg_dump(KNOWLEDGE_DSN, dest)

        assert result.status == DUMP_FAILED
        assert result.reason == TIMEOUT_REASON == "timeout"
        assert not dest.exists()
        assert fake_dump.timeouts == [PG_DUMP_TIMEOUT_S]
        assert PG_DUMP_TIMEOUT_S >= 600, "generous: a big database must finish"

    @needs_psycopg
    def test_no_inherited_pg_variable_reaches_the_child(
        self,
        fake_dump: FakePgDump,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        inherited = {
            "PGHOSTADDR": "203.0.113.9",
            "PGSERVICE": "elsewhere",
            "PGPASSFILE": "/nonexistent/pgpass",
            "PGOPTIONS": "-c search_path=evil",
            "PGSSLMODE": "disable",
            "PGPASSWORD": "inherited-password",
        }
        for name, value in inherited.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("TRELLIS_SENTINEL", "kept")
        dsn = "postgresql://u:dsn-password@db.example.test/kg?sslmode=require"

        run_pg_dump(dsn, tmp_path / "a.dump")

        ((_argv, env),) = fake_dump.calls
        for name in ("PGHOSTADDR", "PGSERVICE", "PGPASSFILE", "PGOPTIONS"):
            assert name not in env, name
        # What the DSN says wins, and what it does not say is not inherited.
        assert env["PGPASSWORD"] == "dsn-password"
        assert env["PGSSLMODE"] == "require"
        assert env["PGHOST"] == "db.example.test"
        assert {k for k in env if k.startswith("PG")} == {
            "PGHOST",
            "PGUSER",
            "PGPASSWORD",
            "PGDATABASE",
            "PGSSLMODE",
            "PGCONNECT_TIMEOUT",
        }
        assert env["TRELLIS_SENTINEL"] == "kept", "non-PG variables are inherited"
        assert "PATH" in env
        # The parent is untouched: nothing leaked into this process's environment.
        assert os.environ["PGPASSWORD"] == "inherited-password"
        assert os.environ["PGHOSTADDR"] == "203.0.113.9"

    @needs_psycopg
    def test_stderr_is_redacted_for_a_password_with_uri_delimiters(
        self, fake_dump: FakePgDump, tmp_path: Path
    ) -> None:
        fake_dump.returncode = 1
        fake_dump.stderr = "pg_dump: error: password authentication failed: pa?ss#x\n"

        result = run_pg_dump("postgresql://u:pa?ss#x@h.test/db", tmp_path / "a.dump")

        assert result.status == DUMP_FAILED
        assert "pa?ss#x" not in (result.reason or "")
        assert "[redacted]" in (result.reason or "")


class _Deployment:
    def __init__(self, root: Path) -> None:
        self.config = root / "config"
        self.data = root / "data"
        self.stores = self.data / "stores"


@pytest.fixture
def deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: IsolatedCliRunner
) -> _Deployment:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COLUMNS", "400")
    dep = _Deployment(tmp_path)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(dep.config))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(dep.data))
    init = cli_runner.invoke(app, ["admin", "init"])
    assert init.exit_code == 0, init.output
    return dep


def _configure_postgres(
    dep: _Deployment, monkeypatch: pytest.MonkeyPatch, *, graph: str = "postgres"
) -> None:
    dep.config.joinpath("config.yaml").write_text(
        yaml.safe_dump(
            {
                "data_dir": str(dep.data),
                "knowledge": {
                    "document": {"backend": "postgres"},
                    "graph": {"backend": graph},
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
    @needs_psycopg
    def test_each_distinct_dsn_is_dumped_once_and_the_password_is_nowhere(
        self,
        deploy: _Deployment,
        fake_dump: FakePgDump,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        cli_runner: IsolatedCliRunner,
    ) -> None:
        _configure_postgres(deploy, monkeypatch)
        out = tmp_path / "bk"

        with capture_logs() as logs:
            result = cli_runner.invoke(
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
        cli_runner: IsolatedCliRunner,
    ) -> None:
        _configure_postgres(deploy, monkeypatch)
        monkeypatch.setattr(backup_postgres.shutil, "which", lambda _name: None)
        out = tmp_path / "bk"

        result = cli_runner.invoke(
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
        cli_runner: IsolatedCliRunner,
    ) -> None:
        _configure_postgres(deploy, monkeypatch)
        monkeypatch.setattr(backup_postgres.shutil, "which", lambda _name: None)

        result = cli_runner.invoke(
            app, ["admin", "backup", "--output", str(tmp_path / "bk")]
        )

        assert result.exit_code == 5, result.output
        assert "TOOL_MISSING" in plain(result.output).upper().replace(" ", "_")

    @needs_psycopg
    def test_a_failed_dump_is_failed_with_the_password_redacted(
        self,
        deploy: _Deployment,
        fake_dump: FakePgDump,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        cli_runner: IsolatedCliRunner,
    ) -> None:
        _configure_postgres(deploy, monkeypatch)
        fake_dump.returncode = 1
        fake_dump.stderr = f"pg_dump: error: password authentication failed {SECRET}"

        result = cli_runner.invoke(
            app,
            ["admin", "backup", "--output", str(tmp_path / "bk"), "--format", "json"],
        )

        assert result.exit_code == 5, result.output
        payload = json.loads(result.stdout)
        assert _by_name(payload["stores"])["document"]["status"] == "failed"
        assert SECRET not in result.output
        assert SECRET not in (tmp_path / "bk" / "manifest.json").read_text()

    @needs_psycopg
    def test_a_postgres_store_with_no_dsn_fails_by_name(
        self,
        deploy: _Deployment,
        fake_dump: FakePgDump,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        cli_runner: IsolatedCliRunner,
    ) -> None:
        _configure_postgres(deploy, monkeypatch)
        monkeypatch.delenv("TRELLIS_KNOWLEDGE_PG_DSN")

        result = cli_runner.invoke(
            app,
            ["admin", "backup", "--output", str(tmp_path / "bk"), "--format", "json"],
        )

        assert result.exit_code == 5, result.output
        stores = _by_name(json.loads(result.stdout)["stores"])
        assert stores["document"]["status"] == "failed"
        assert "no DSN" in stores["document"]["reason"]
        assert stores["trace"]["status"] == "copied"

    def test_skip_postgres_leaves_the_postgres_stores_to_the_operator_and_exits_zero(
        self,
        deploy: _Deployment,
        fake_dump: FakePgDump,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        cli_runner: IsolatedCliRunner,
    ) -> None:
        _configure_postgres(deploy, monkeypatch)
        # No DSN is read when the stores are skipped, so none need be set.
        monkeypatch.delenv("TRELLIS_KNOWLEDGE_PG_DSN")
        monkeypatch.delenv("TRELLIS_OPERATIONAL_PG_DSN")
        out = tmp_path / "bk"

        result = cli_runner.invoke(
            app,
            [
                "admin",
                "backup",
                "--output",
                str(out),
                "--skip-postgres",
                "--format",
                "json",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "ok"
        stores = _by_name(payload["stores"])
        for name in ("document", "graph", "vector", "trace", "event_log", "api_key"):
            assert stores[name]["status"] == "skipped", name
            assert stores[name]["reason"] == "skip_postgres_flag", name
            assert "file" not in stores[name], name
        assert fake_dump.calls == [], "no pg_dump runs"
        assert not (out / "postgres").exists() or not any((out / "postgres").iterdir())
        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["status"] == "ok"

    def test_the_text_report_exits_the_same_with_skip_postgres(
        self,
        deploy: _Deployment,
        fake_dump: FakePgDump,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        cli_runner: IsolatedCliRunner,
    ) -> None:
        _configure_postgres(deploy, monkeypatch)

        result = cli_runner.invoke(
            app,
            ["admin", "backup", "--output", str(tmp_path / "bk"), "--skip-postgres"],
        )

        assert result.exit_code == 0, result.output
        assert "skip_postgres_flag" in plain(result.output)

    def test_without_the_flag_a_missing_pg_dump_still_fails_the_run(
        self,
        deploy: _Deployment,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        cli_runner: IsolatedCliRunner,
    ) -> None:
        """Same deployment, same missing tool: the flag is the only difference."""
        _configure_postgres(deploy, monkeypatch)
        monkeypatch.setattr(backup_postgres.shutil, "which", lambda _name: None)

        plain_run = cli_runner.invoke(
            app,
            ["admin", "backup", "--output", str(tmp_path / "a"), "--format", "json"],
        )
        flagged = cli_runner.invoke(
            app,
            [
                "admin",
                "backup",
                "--output",
                str(tmp_path / "b"),
                "--skip-postgres",
                "--format",
                "json",
            ],
        )

        assert plain_run.exit_code == 5
        assert _by_name(json.loads(plain_run.stdout)["stores"])["document"][
            "status"
        ] == ("tool_missing")
        assert flagged.exit_code == 0

    def test_skip_postgres_does_not_hide_another_backend_left_out(
        self,
        deploy: _Deployment,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        cli_runner: IsolatedCliRunner,
    ) -> None:
        _configure_postgres(deploy, monkeypatch, graph="neo4j")

        result = cli_runner.invoke(
            app,
            [
                "admin",
                "backup",
                "--output",
                str(tmp_path / "bk"),
                "--skip-postgres",
                "--format",
                "json",
            ],
        )

        assert result.exit_code == 5, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "incomplete"
        graph = _by_name(payload["stores"])["graph"]
        assert graph["status"] == "skipped"
        assert graph["reason"] != "skip_postgres_flag"

    @needs_psycopg
    def test_a_postgres_deployment_with_no_stores_dir_is_refused_before_any_dump(
        self,
        deploy: _Deployment,
        fake_dump: FakePgDump,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        cli_runner: IsolatedCliRunner,
    ) -> None:
        """The stores directory is required whatever the backends: the CLI refuses
        an uninitialised one before ``backup`` runs, and the library raises."""
        shutil.rmtree(deploy.stores)
        _configure_postgres(deploy, monkeypatch)

        result = cli_runner.invoke(
            app,
            ["admin", "backup", "--output", str(tmp_path / "bk"), "--format", "json"],
        )

        assert result.exit_code != 0
        assert fake_dump.calls == []
        registry = StoreRegistry(
            config={"knowledge": {"document": {"backend": "postgres"}}},
            stores_dir=deploy.stores,
        )
        with pytest.raises(ConfigError, match="does not exist"):
            backup_stores(registry, tmp_path / "bk2")
