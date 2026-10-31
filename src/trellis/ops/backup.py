"""Online backup of a deployment's SQLite stores.

A running store keeps acknowledged writes in its write-ahead log until a
checkpoint folds them into the main file, so ``cp documents.db`` of a live
store copies a file that is missing them, and that file still passes
``PRAGMA integrity_check``: it is a valid, older database. Nothing in
the copy says rows are gone.

:func:`backup_stores` copies each SQLite store with SQLite's own online
backup API (``sqlite3.Connection.backup``), which reads one consistent
snapshot through a normal connection, WAL included, while writers keep
writing. Each file is consistent as of its own instant. The stores are
copied one after another, so the set is **not** a cross-store snapshot:
quiesce writers when documents and events must agree.

The set of stores comes from the registry
(:meth:`~trellis.stores.registry.StoreRegistry.configured_backend` and
``configured_sqlite_path``), never from a filename glob, so a store with an
explicit ``db_path`` is found and a stray file in the directory is not
mistaken for one. A store on any other backend is reported ``skipped`` and
makes the backup ``incomplete``; the command never reports success for data
it did not copy. It also copies the policy file and the feedback audit log.
Every other file under the stores directory is named in ``not_copied``.

There is no restore here: a restore is placing the files back and
pointing the config at them (``docs/agent-guide/operations.md``).
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from trellis.core.base import utc_now
from trellis.core.path_presence import path_is_present
from trellis.core.version import resolve_code_version
from trellis.errors import ConfigError, ValidationError
from trellis.feedback.recording import feedback_log_dir
from trellis.mutate.policy_source import POLICY_FILENAME, resolve_policy_path
from trellis.stores.registry import _PLANE_OF, _SQLITE_DB_NAMES, StoreRegistry

logger = structlog.get_logger(__name__)

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1
FEEDBACK_LOG_NAME = "pack_feedback.jsonl"

#: Where files land under the output directory, mirroring ``<data_dir>/stores``
#: so a restore is one copy of this directory's contents.
OUTPUT_STORES_SUBDIR = "stores"

# Seconds a read of a busy store waits for a lock before it is a failure.
_BUSY_TIMEOUT_S = 30.0

#: What the not-SQLite pointer says. Named once so the text and the docs agree.
NON_SQLITE_HINT = (
    "not backed up by this command; use the backend's own tool"
    " (pg_dump for Postgres, neo4j-admin for Neo4j, the ArcadeDB backup"
    " command, bucket versioning or replication for S3)"
)

# ``copied`` and ``empty`` are the only outcomes that mean "nothing was lost".
# ``absent`` is a SQLite store whose file does not exist (stores are created
# when first opened), ``skipped`` is data on a backend this command does not
# copy, ``not_applicable`` is a backend that stores nothing.
COPIED = "copied"
ABSENT = "absent"
SKIPPED = "skipped"
FAILED = "failed"
EMPTY = "empty"
NOT_APPLICABLE = "not_applicable"

STATUS_OK = "ok"
STATUS_INCOMPLETE = "incomplete"
STATUS_ERROR = "error"


@dataclass
class BackedUpItem:
    """One store or state file, and what became of it."""

    name: str
    status: str
    backend: str | None = None
    file: str | None = None
    size_bytes: int | None = None
    integrity_check: str | None = None
    tables: dict[str, int] | None = None
    rows: int | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class BackupReport:
    """The manifest: everything the command did, and did not, copy."""

    created_at: datetime
    trellis: dict[str, Any]
    stores: list[BackedUpItem] = field(default_factory=list)
    files: list[BackedUpItem] = field(default_factory=list)
    not_copied: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        """``ok`` | ``incomplete`` | ``error``; the exit code reads this alone."""
        items = [*self.stores, *self.files]
        if any(i.status == FAILED for i in items):
            return STATUS_ERROR
        copied = any(i.status == COPIED for i in self.stores)
        skipped = any(i.status == SKIPPED for i in self.stores)
        if not copied and not skipped:
            # Every store file is missing: a wrong data directory looks
            # exactly like this, and an empty backup must not read as success.
            return STATUS_ERROR
        return STATUS_INCOMPLETE if skipped else STATUS_OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": MANIFEST_VERSION,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "trellis": self.trellis,
            "stores": [i.as_dict() for i in self.stores],
            "files": [i.as_dict() for i in self.files],
            "not_copied": self.not_copied,
        }


def prepare_target(output: Path) -> Path:
    """Create ``output`` if needed and refuse anything but an empty directory.

    A backup written over the previous one would mix two moments' files, so a
    non-empty target is refused rather than merged into. The refusal happens
    before any store is read.
    """
    if path_is_present(output):
        if not output.is_dir():
            msg = f"--output {output} exists and is not a directory"
            raise ValidationError(msg)
        if any(output.iterdir()):
            msg = (
                f"--output {output} is not empty; give an empty or new directory,"
                " since a backup must not be mixed into an earlier one"
            )
            raise ValidationError(msg)
    else:
        # The copy holds every memory and the API key hashes: owner only.
        output.mkdir(parents=True, mode=0o700)
        with contextlib.suppress(OSError):
            output.chmod(0o700)
    return output


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _count_tables(conn: sqlite3.Connection) -> dict[str, int]:
    """Row counts of the tables a store owns, read from the copy.

    A virtual table's shadow tables (an FTS index's ``*_data``, ``*_idx``,
    ...) are internals of it and are left out.
    """
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master"
        " WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    virtual = [n for n, sql in rows if (sql or "").upper().startswith("CREATE VIRTUAL")]
    counts: dict[str, int] = {}
    for name, _sql in rows:
        if any(name.startswith(f"{v}_") for v in virtual):
            continue
        (count,) = conn.execute(f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()  # noqa: S608
        counts[name] = count
    return counts


def _copy_sqlite(source: Path, dest: Path) -> tuple[str, dict[str, int]]:
    """Back ``source`` up to ``dest`` and check the copy.

    Returns the copy's ``integrity_check`` answer and its row counts. The
    source is opened read-only and never modified. The copy is left in
    rollback-journal mode, a single self-contained file: the store re-enters
    WAL the first time it opens it.
    """
    uri = f"{source.resolve().as_uri()}?mode=ro"
    try:
        with (
            contextlib.closing(
                sqlite3.connect(uri, uri=True, timeout=_BUSY_TIMEOUT_S)
            ) as src,
            contextlib.closing(sqlite3.connect(dest)) as dst,
        ):
            # pages=-1 is one step: a single read snapshot of the whole file.
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE").fetchone()
            rows = dst.execute("PRAGMA integrity_check").fetchall()
            verdict = (
                "ok" if rows == [("ok",)] else "; ".join(str(r[0]) for r in rows[:5])
            )
            tables = _count_tables(dst)
    except BaseException:
        # A half-written copy must not be mistaken for a backup.
        dest.unlink(missing_ok=True)
        raise
    dest.chmod(0o600)
    return verdict, tables


def _failure_reason(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _not_sqlite(
    registry: StoreRegistry, store_type: str, backend: str | None
) -> BackedUpItem | None:
    """The report row for a store this command does not copy, or ``None`` for SQLite."""
    if backend == "sqlite":
        return None
    if backend is None:
        return BackedUpItem(
            store_type,
            SKIPPED,
            reason=(
                "configured backend is not a registered one; the store cannot"
                " be opened either. Fix the config, then back up"
            ),
        )
    if backend == "null":
        return BackedUpItem(
            store_type, NOT_APPLICABLE, backend=backend, reason="stores nothing"
        )
    if backend == "local" and store_type == "blob":
        return _blob_dir_item(registry)
    return BackedUpItem(store_type, SKIPPED, backend=backend, reason=NON_SQLITE_HINT)


def _backup_store(
    registry: StoreRegistry, store_type: str, out_stores: Path
) -> BackedUpItem:
    backend = registry.configured_backend(store_type)
    other = _not_sqlite(registry, store_type, backend)
    if other is not None:
        return other

    source = registry.configured_sqlite_path(store_type)
    if source is None or not path_is_present(source):
        return BackedUpItem(
            store_type,
            ABSENT,
            backend=backend,
            reason="no file yet: a store is created when it is first opened",
        )
    rel = f"{OUTPUT_STORES_SUBDIR}/{_SQLITE_DB_NAMES[store_type]}"
    dest = out_stores / _SQLITE_DB_NAMES[store_type]
    try:
        verdict, tables = _copy_sqlite(source, dest)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("backup_store_failed", store=store_type, error=str(exc))
        return BackedUpItem(
            store_type, FAILED, backend=backend, reason=_failure_reason(exc)
        )
    item = BackedUpItem(
        store_type,
        COPIED,
        backend=backend,
        file=rel,
        size_bytes=dest.stat().st_size,
        integrity_check=verdict,
        tables=tables,
    )
    if verdict != "ok":
        item.status = FAILED
        item.reason = "integrity_check failed on the copy"
    return item


def _blob_dir_item(registry: StoreRegistry) -> BackedUpItem:
    """The local blob directory: a tree of files this command does not copy."""
    blobs = None if registry.stores_dir is None else registry.stores_dir / "blobs"
    count = 0
    if blobs is not None and blobs.is_dir():
        count = sum(1 for p in blobs.rglob("*") if p.is_file())
    if count == 0:
        return BackedUpItem("blob", EMPTY, backend="local", reason="no blobs stored")
    return BackedUpItem(
        "blob",
        SKIPPED,
        backend="local",
        rows=count,
        reason=(
            f"{count} files under the local blob directory are not copied; copy"
            " that directory with ordinary file tools (rsync, cp -a)"
        ),
    )


def _backup_file(
    name: str, source: Path | None, dest: Path, rel: str, *, count_lines: bool = False
) -> BackedUpItem:
    if source is None or not path_is_present(source):
        return BackedUpItem(name, ABSENT, reason="no such file")
    try:
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        dest.chmod(0o600)
        rows = None
        if count_lines:
            with dest.open("rb") as fh:
                rows = sum(1 for _ in fh)
    except OSError as exc:
        logger.warning("backup_file_failed", file=name, error=str(exc))
        dest.unlink(missing_ok=True)
        return BackedUpItem(name, FAILED, reason=_failure_reason(exc))
    return BackedUpItem(
        name, COPIED, file=rel, size_bytes=dest.stat().st_size, rows=rows
    )


def _list_not_copied(
    stores_dir: Path, accounted_dbs: set[str], *, blobs_accounted: bool
) -> list[str]:
    """Names under ``stores_dir`` that this backup neither copied nor accounted for."""
    left: list[str] = []
    sidecars = tuple(f"{n}{s}" for n in accounted_dbs for s in ("-wal", "-shm"))
    for entry in sorted(stores_dir.iterdir()):
        name = entry.name
        if name in accounted_dbs or name in sidecars or name == POLICY_FILENAME:
            continue
        if entry.is_dir():
            if name == "blobs" and blobs_accounted:
                continue
            if name == feedback_log_dir(stores_dir).name:
                left.extend(
                    f"{name}/{child.name}"
                    for child in sorted(entry.iterdir())
                    if child.name != FEEDBACK_LOG_NAME
                )
                continue
            files = sum(1 for p in entry.rglob("*") if p.is_file())
            left.append(f"{name}/ ({files} files)")
        else:
            left.append(name)
    return left


def backup_stores(registry: StoreRegistry, output: Path) -> BackupReport:
    """Back up ``registry``'s SQLite stores into ``output`` and write the manifest.

    ``output`` must already be prepared (:func:`prepare_target`). A store that
    fails does not stop the others: each outcome is in the report, and the
    manifest is written whatever the status.
    """
    stores_dir = registry.stores_dir
    if stores_dir is None:
        msg = "the registry has no stores directory to back up"
        raise ConfigError(msg, setting="stores_dir")
    if not path_is_present(stores_dir):
        msg = f"stores directory {stores_dir} does not exist"
        raise ConfigError(msg, setting="data_dir")

    report = BackupReport(
        created_at=utc_now(), trellis=resolve_code_version().as_dict()
    )
    out_stores = output / OUTPUT_STORES_SUBDIR
    out_stores.mkdir(mode=0o700, exist_ok=True)

    for store_type in _PLANE_OF:
        report.stores.append(_backup_store(registry, store_type, out_stores))

    report.files.append(
        _backup_file(
            "policy",
            resolve_policy_path(stores_dir),
            out_stores / POLICY_FILENAME,
            f"{OUTPUT_STORES_SUBDIR}/{POLICY_FILENAME}",
        )
    )
    report.files.append(
        _backup_file(
            "feedback_log",
            feedback_log_dir(stores_dir) / FEEDBACK_LOG_NAME,
            out_stores / feedback_log_dir(stores_dir).name / FEEDBACK_LOG_NAME,
            f"{OUTPUT_STORES_SUBDIR}/{feedback_log_dir(stores_dir).name}"
            f"/{FEEDBACK_LOG_NAME}",
            count_lines=True,
        )
    )

    accounted: set[str] = set()
    for store_type in _PLANE_OF:
        path = registry.configured_sqlite_path(store_type)
        if path is not None and path.parent == stores_dir:
            accounted.add(path.name)
    report.not_copied = _list_not_copied(
        stores_dir,
        accounted,
        blobs_accounted=registry.configured_backend("blob") == "local",
    )

    manifest = output / MANIFEST_NAME
    manifest.write_text(json.dumps(report.as_dict(), indent=2) + "\n")
    manifest.chmod(0o600)
    logger.info("backup_completed", status=report.status, output=str(output))
    return report


__all__ = [
    "FEEDBACK_LOG_NAME",
    "MANIFEST_NAME",
    "NON_SQLITE_HINT",
    "BackedUpItem",
    "BackupReport",
    "backup_stores",
    "prepare_target",
]
