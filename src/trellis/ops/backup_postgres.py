"""Run ``pg_dump`` for a configured Postgres DSN without the DSN on the argv.

A command line is public on a shared host (``ps``, ``/proc/<pid>/cmdline``,
shell history, CI logs). A Postgres DSN carries a password, so this module
never builds an argv that contains any part of one: it takes the DSN apart
into the libpq environment variables ``pg_dump`` itself reads (``PGHOST``,
``PGPORT``, ``PGUSER``, ``PGPASSWORD``, ``PGDATABASE``, ``PGSSLMODE``, ...) and
passes those to the child process through its environment. The argv is the tool,
``--format=custom``, ``--file`` and ``--no-password``.

Nothing is printed from the DSN. A failure's text comes from ``pg_dump``'s
stderr, is redacted of the password and run through
:func:`~trellis.core.error_sanitize.sanitize_error_message`, so a driver that
echoes a connection string cannot carry the credential into a report, a
manifest or a log.

A DSN parameter this module does not know how to hand to ``pg_dump`` is refused
by *name* (never by value) rather than dropped: a dump taken with a silently
ignored ``sslmode`` or ``options`` would be a dump of the wrong connection.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

import structlog

from trellis.core.error_sanitize import sanitize_error_message

logger = structlog.get_logger(__name__)

#: libpq connection parameter -> the environment variable libpq reads for it.
_ENV_BY_PARAM: dict[str, str] = {
    "host": "PGHOST",
    "hostaddr": "PGHOSTADDR",
    "port": "PGPORT",
    "dbname": "PGDATABASE",
    "user": "PGUSER",
    "password": "PGPASSWORD",
    "passfile": "PGPASSFILE",
    "service": "PGSERVICE",
    "options": "PGOPTIONS",
    "application_name": "PGAPPNAME",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "sslcrl": "PGSSLCRL",
    "channel_binding": "PGCHANNELBINDING",
    "gssencmode": "PGGSSENCMODE",
    "krbsrvname": "PGKRBSRVNAME",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
    "requirepeer": "PGREQUIREPEER",
}

#: Seconds ``pg_dump`` waits to connect when the DSN names no timeout.
DEFAULT_CONNECT_TIMEOUT_S = "30"

# Statuses of one dump attempt.
DUMPED = "dumped"
TOOL_MISSING = "tool_missing"
DUMP_FAILED = "failed"

_STDERR_LINES = 3


class UnsupportedDsnError(ValueError):
    """A DSN this module cannot hand to ``pg_dump``. The message names no value."""


@dataclass(frozen=True)
class DumpResult:
    status: str
    size_bytes: int | None = None
    reason: str | None = None


def _split_hostport(item: str) -> tuple[str, str | None]:
    if item.startswith("["):
        host, _, rest = item[1:].partition("]")
        return host, rest.lstrip(":") or None
    host, sep, port = item.rpartition(":")
    return (host, port or None) if sep else (item, None)


def _params_from_uri(dsn: str) -> dict[str, str]:
    parts = urlsplit(dsn)
    userinfo, at, hostlist = parts.netloc.rpartition("@")
    params: dict[str, str] = {}
    if at:
        user, colon, password = userinfo.partition(":")
        if user:
            params["user"] = unquote(user)
        if colon:
            params["password"] = unquote(password)
    hosts: list[str] = []
    ports: list[str | None] = []
    for item in filter(None, hostlist.split(",")):
        host, port = _split_hostport(item)
        hosts.append(unquote(host))
        ports.append(port)
    if hosts:
        params["host"] = ",".join(hosts)
    if any(ports):
        params["port"] = ",".join(p or "5432" for p in ports)
    if parts.path.strip("/"):
        params["dbname"] = unquote(parts.path.lstrip("/"))
    params.update(parse_qsl(parts.query, keep_blank_values=True))
    return params


def _read_value(dsn: str, i: int) -> tuple[str, int]:
    """Read one libpq value starting at ``i``: ``'quoted'`` or a bare word."""
    n = len(dsn)
    quoted = i < n and dsn[i] == "'"
    i += 1 if quoted else 0
    value: list[str] = []
    while i < n and (dsn[i] != "'" if quoted else not dsn[i].isspace()):
        if dsn[i] == "\\" and i + 1 < n:
            i += 1
        value.append(dsn[i])
        i += 1
    return "".join(value), i + (1 if quoted else 0)


def _params_from_keywords(dsn: str) -> dict[str, str]:
    """Parse libpq ``key=value`` pairs, with ``'single quoted'`` values."""
    params: dict[str, str] = {}
    i, n = 0, len(dsn)
    while True:
        while i < n and dsn[i].isspace():
            i += 1
        if i >= n:
            return params
        start = i
        while i < n and dsn[i] not in "= \t\n":
            i += 1
        key = dsn[start:i]
        while i < n and dsn[i].isspace():
            i += 1
        if not key or i >= n or dsn[i] != "=":
            msg = "connection string is not a postgres URI or key=value list"
            raise UnsupportedDsnError(msg)
        i += 1
        while i < n and dsn[i].isspace():
            i += 1
        params[key], i = _read_value(dsn, i)


def pg_env_from_dsn(dsn: str) -> dict[str, str]:
    """The libpq environment variables that say what ``dsn`` says.

    Raises :class:`UnsupportedDsnError` (naming a parameter, never a value) for
    a DSN it cannot express.
    """
    text = dsn.strip()
    if text.startswith(("postgres://", "postgresql://")):
        params = _params_from_uri(text)
    else:
        params = _params_from_keywords(text)
    env: dict[str, str] = {}
    for key, value in params.items():
        name = _ENV_BY_PARAM.get(key)
        if name is None:
            msg = f"connection parameter {key!r} cannot be passed to pg_dump"
            raise UnsupportedDsnError(msg)
        env[name] = value
    env.setdefault("PGCONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT_S)
    return env


def _redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def run_pg_dump(dsn: str, dest: Path) -> DumpResult:
    """Dump the database ``dsn`` names to ``dest`` in ``pg_dump``'s custom format.

    The DSN reaches the child only through its environment. A half-written
    ``dest`` is removed on failure so it cannot be mistaken for a backup.
    """
    tool = shutil.which("pg_dump")
    if tool is None:
        return DumpResult(
            TOOL_MISSING,
            reason=(
                "pg_dump was not found on PATH; install the PostgreSQL client"
                " tools (a version at least as new as the server)"
            ),
        )
    try:
        conn_env = pg_env_from_dsn(dsn)
    except UnsupportedDsnError as exc:
        return DumpResult(DUMP_FAILED, reason=str(exc))
    secrets = [conn_env.get("PGPASSWORD", "")]
    argv = [tool, "--format=custom", "--no-password", f"--file={dest}"]
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        proc = subprocess.run(  # noqa: S603 - argv is the tool plus fixed flags
            argv,
            env={**os.environ, **conn_env},
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        dest.unlink(missing_ok=True)
        return DumpResult(
            DUMP_FAILED, reason=f"pg_dump could not start: {type(exc).__name__}"
        )
    if proc.returncode != 0:
        dest.unlink(missing_ok=True)
        lines = _redact(proc.stderr or "", secrets).strip().splitlines()
        detail = sanitize_error_message("; ".join(lines[:_STDERR_LINES]))
        logger.warning("pg_dump_failed", exit_code=proc.returncode)
        return DumpResult(
            DUMP_FAILED, reason=f"pg_dump exited {proc.returncode}: {detail}"
        )
    if not dest.is_file() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        return DumpResult(DUMP_FAILED, reason="pg_dump exited 0 but wrote no file")
    dest.chmod(0o600)
    return DumpResult(DUMPED, size_bytes=dest.stat().st_size)


__all__ = [
    "DUMPED",
    "DUMP_FAILED",
    "TOOL_MISSING",
    "DumpResult",
    "UnsupportedDsnError",
    "pg_env_from_dsn",
    "run_pg_dump",
]
