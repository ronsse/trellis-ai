"""Run ``pg_dump`` for a configured Postgres DSN without the DSN on the argv.

A command line is public on a shared host (``ps``, ``/proc/<pid>/cmdline``,
shell history, CI logs). A Postgres DSN carries a password, so this module
never builds an argv that contains any part of one: it takes the DSN apart
into the libpq environment variables ``pg_dump`` itself reads (``PGHOST``,
``PGPORT``, ``PGUSER``, ``PGPASSWORD``, ``PGDATABASE``, ``PGSSLMODE``, ...) and
passes those to the child process through its environment. The argv is the tool,
``--format=custom``, ``--file`` and ``--no-password``. The DSN is parsed by
libpq's own parser (``psycopg.conninfo.conninfo_to_dict``), so it means to this
module what it means to a connection.

The child's environment is ours with every ``PG*`` variable removed and the DSN's
added, so nothing inherited can redirect the dump.

Nothing is printed from the DSN. A refusal names a parameter, never a value (libpq's
own parse errors quote a slice of the string, so none of that text is used). A
failure's text comes from ``pg_dump``'s stderr, is redacted of every
secret-bearing parameter (``password``, ``sslpassword``, whatever libpq marks
secret) and run through
:func:`~trellis.core.error_sanitize.sanitize_error_message`, so a driver that
echoes a connection string cannot carry the credential into a report, a
manifest or a log.

A DSN parameter this module does not know how to hand to ``pg_dump`` is refused
by *name* (never by value) rather than dropped: a dump taken with a silently
ignored ``sslmode`` or ``options`` would be a dump of the wrong connection. Only
a real libpq keyword is ever named. A parse failure that quotes a name libpq
does not know (a typo, or a fragment of a password with an unquoted space in it)
is reported without the name, because the two look the same.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

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

#: Seconds one dump may run before it is killed and reported ``failed``. Generous:
#: a large database takes a long time, a stuck connection takes forever.
PG_DUMP_TIMEOUT_S = 3600

#: The ``reason`` of a dump that outran :data:`PG_DUMP_TIMEOUT_S`.
TIMEOUT_REASON = "timeout"

_NO_PSYCOPG = (
    "psycopg is not installed, so the Postgres store cannot run either;"
    " install trellis-ai[cloud]"
)
# The two libpq parse errors that quote a parameter *name* (and nothing else).
_NAMED_IN_ERROR = re.compile(
    r'invalid connection option "([^"]*)"|invalid URI query parameter: "([^"]*)"'
)
_UNRECOGNISED_PARAMETER = "connection string names a parameter libpq does not recognise"

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


def _libpq_keywords() -> frozenset[str]:
    """Every connection keyword the installed libpq knows, or none if it cannot say."""
    try:
        from psycopg import pq  # noqa: PLC0415

        return frozenset(o.keyword.decode() for o in pq.Conninfo.get_defaults())
    except Exception:  # advisory: no keywords means no name is ever shown
        logger.debug("pg_keywords_unavailable")
        return frozenset()


def _parse_failure(exc: Exception) -> str:
    """What to say about a DSN libpq refused, without echoing any of its text.

    libpq's own message quotes the offending token, and for a bad
    percent-escape that token is a slice of the password. The two messages that
    quote a parameter *name* are no safer: libpq splits a key=value list at every
    space, so a password with an unquoted space (``password=abc def=xyz``) reports
    ``def`` as an unknown option. A name is therefore shown only when it is a real
    libpq keyword; any other is left out and the reason says only that one was not
    recognised. libpq rejects a name because it is not a keyword, so in practice
    these two messages always take the second branch; the check is what keeps that
    true if a libpq ever quotes a keyword.
    """
    match = _NAMED_IN_ERROR.search(str(exc))
    if match is None:
        return "connection string is not a postgres URI or key=value list"
    name = match.group(1) or match.group(2)
    if name in _libpq_keywords():
        return f"connection parameter {name!r} cannot be passed to pg_dump"
    return _UNRECOGNISED_PARAMETER


def _read_params(dsn: str) -> dict[str, str]:
    """The connection parameters ``dsn`` carries, as libpq itself reads them.

    ``psycopg`` hands the string to ``PQconninfoParse``, so a ``?`` or ``#`` in an
    unencoded password, a ``+`` in a query value, quoting and escapes in a
    ``key=value`` list and a multi-host URI all resolve the way a connection would.
    A parser of our own disagreed with libpq on exactly those, and a disagreement
    here is a dump of the wrong host or a password fragment in a report.
    """
    try:
        from psycopg.conninfo import conninfo_to_dict  # noqa: PLC0415
    except ImportError:
        # The Postgres store cannot run without psycopg either.
        raise UnsupportedDsnError(_NO_PSYCOPG) from None
    try:
        return {k: str(v) for k, v in conninfo_to_dict(dsn.strip()).items()}
    except Exception as exc:  # psycopg raises ProgrammingError; echo none of it
        raise UnsupportedDsnError(_parse_failure(exc)) from None


def _env_from_params(params: dict[str, str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in params.items():
        name = _ENV_BY_PARAM.get(key)
        if name is None:
            msg = f"connection parameter {key!r} cannot be passed to pg_dump"
            raise UnsupportedDsnError(msg)
        env[name] = value
    env.setdefault("PGCONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT_S)
    return env


def _secret_names() -> frozenset[str]:
    """Parameters whose value must never be printed: ``password`` and whatever
    libpq marks secret (its ``*`` display character: ``sslpassword`` and others)."""
    names = {"password"}
    try:
        from psycopg import pq  # noqa: PLC0415

        names.update(
            o.keyword.decode() for o in pq.Conninfo.get_defaults() if o.dispchar == b"*"
        )
    except Exception:  # advisory: the fixed name above is the floor
        logger.debug("pg_secret_names_unavailable")
    return frozenset(names)


def _secret_values(params: dict[str, str]) -> list[str]:
    names = _secret_names()
    return sorted(
        {v for k, v in params.items() if k in names and v}, key=len, reverse=True
    )


def pg_env_from_dsn(dsn: str) -> dict[str, str]:
    """The libpq environment variables that say what ``dsn`` says.

    Raises :class:`UnsupportedDsnError` (naming a parameter, never a value) for
    a DSN it cannot express.
    """
    return _env_from_params(_read_params(dsn))


#: A secret shorter than this is redacted only as a whole token.
_SHORT_SECRET_CHARS = 4


def _redact(text: str, secrets: list[str]) -> str:
    """Replace each secret in ``text`` with ``[redacted]``.

    A secret of :data:`_SHORT_SECRET_CHARS` or more is replaced wherever it
    occurs: it is long enough that a hit inside another word is a coincidence
    worth losing, and a fragment of a longer echo (``password=<secret>``) goes with
    it. A shorter one is replaced as a whole token only, meaning not touching a
    word character on either side. A one-character password replaced everywhere
    rewrites every occurrence of that letter, which leaves the error unreadable
    (and mangles the ``[redacted]`` marker itself), so a short value is matched
    where an echo of it would stand alone: ``password=a``, ``"a"``, ``a``. Skipping
    short values outright was the other option; it would let a short password
    through whenever ``pg_dump`` echoes it.
    """
    for secret in secrets:
        if len(secret) < _SHORT_SECRET_CHARS:
            token = rf"(?<!\w){re.escape(secret)}(?!\w)"
            text = re.sub(token, "[redacted]", text)
        else:
            text = text.replace(secret, "[redacted]")
    return text


def _child_env(conn_env: dict[str, str]) -> dict[str, str]:
    """The child's environment: ours without any ``PG*`` variable, plus the DSN's.

    An inherited ``PGHOSTADDR``, ``PGSERVICE``, ``PGPASSFILE`` or ``PGOPTIONS``
    would otherwise redirect or reconfigure a connection the DSN described.
    """
    inherited = {k: v for k, v in os.environ.items() if not k.upper().startswith("PG")}
    return {**inherited, **conn_env}


def _spawn(
    argv: list[str], conn_env: dict[str, str], dest: Path
) -> subprocess.CompletedProcess[str] | DumpResult:
    """Run the dump; a timeout or a launch failure is already a :class:`DumpResult`."""
    try:
        return subprocess.run(  # noqa: S603 - argv is the tool plus fixed flags
            argv,
            env=_child_env(conn_env),
            capture_output=True,
            text=True,
            check=False,
            timeout=PG_DUMP_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        dest.unlink(missing_ok=True)
        logger.warning("pg_dump_timeout", timeout_s=PG_DUMP_TIMEOUT_S)
        return DumpResult(DUMP_FAILED, reason=TIMEOUT_REASON)
    except OSError as exc:
        dest.unlink(missing_ok=True)
        return DumpResult(
            DUMP_FAILED, reason=f"pg_dump could not start: {type(exc).__name__}"
        )


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
    params: dict[str, str] = {}
    try:
        params = _read_params(dsn)
        conn_env = _env_from_params(params)
    except UnsupportedDsnError as exc:
        return DumpResult(DUMP_FAILED, reason=_redact(str(exc), _secret_values(params)))
    secrets = _secret_values(params)
    argv = [tool, "--format=custom", "--no-password", f"--file={dest}"]
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    proc = _spawn(argv, conn_env, dest)
    if isinstance(proc, DumpResult):
        return proc
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
    "PG_DUMP_TIMEOUT_S",
    "TIMEOUT_REASON",
    "TOOL_MISSING",
    "DumpResult",
    "UnsupportedDsnError",
    "pg_env_from_dsn",
    "run_pg_dump",
]
