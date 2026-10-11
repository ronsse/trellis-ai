"""The job catalog — the only source of a scheduled job's command.

``schedule.json`` (:class:`~trellis.stores.schedule_store.ScheduleStore`,
:class:`~trellis.schemas.schedule.ScheduledJob`) lives in a directory
mounted read-write into every Trellis container, and a later PR (plan p1
PR 6) makes it API-editable. Anything that can write that file therefore
controls its contents — which is fine, because this module, not that
file, decides what runs. A ``schedule.json`` row names a job by
``name``; the argv that name runs comes from :data:`JOB_CATALOG` here, in
source, reviewed the same way any other code change is. Editing
``schedule.json`` — by hand, a compromised container, or the future API —
can change only *whether* and *when* a catalog job runs, never *what*
runs.

Two kinds of catalog entry:

- A **Trellis-native job** (``host_only=False``) carries its argv in
  :attr:`JobSpec.argv`, fixed at import time. ``trellis admin due-jobs``
  reports it directly; a dispatcher execs it as-is.
- A **host-only job** (``host_only=True``: ``capture-nightly``,
  ``curate-nightly``, ``backup-nightly``, ``roadmap-nightly`` — they need
  the docker socket, ``gh`` auth, or the ``/mnt/data`` mount, none of
  which a container has) carries no argv at all. Its executable is
  ``$TRELLIS_HOST_JOBS_DIR/<name>``, placed there by the operator outside
  the shared data mount — never a path recorded in this repo, and never a
  path read from ``schedule.json``. The HOST dispatcher
  (``docs/ops/job-dispatcher.sh.example``) resolves that path itself,
  after checking the name with :func:`validate_host_job_name` (mirrored
  by the dispatcher's own bash regex check); if the env var is unset or
  the file is missing or not executable, the dispatcher must report the
  job as not runnable, never silently skip it.

``trellis.schemas.schedule.ScheduledJob`` validates ``name`` against this
module's :data:`JOB_CATALOG` on every read and write, so an unknown name
degrades the row instead of ever being treated as a job to run.

Deliberately excluded: ``trellis admin migrate-graph``. Every entry below
has one stable default invocation; migrate-graph's whole shape is
``--from-config`` / ``--to-config`` pointing at two YAML files chosen per
migration, so there is no default command to catalog — an operator runs
it directly, by hand, the day they need a one-off cross-backend migration.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from trellis.core.cron import CronSchedule, CronSyntaxError

#: A catalog (and therefore schedule-registry) job name, and the only
#: shape a host-only job's name may take before the dispatcher turns it
#: into ``$TRELLIS_HOST_JOBS_DIR/<name>``. No ``/``, no ``..``, no shell
#: metacharacters — just what a filename component needs to be.
HOST_JOB_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9-]+$")

#: Bounds for :attr:`JobSpec.default_timeout_seconds` (and, by the same
#: validator shape, :class:`~trellis.schemas.schedule.ScheduledJob`'s
#: ``timeout_seconds`` override).
_MIN_TIMEOUT_SECONDS = 1
_MAX_TIMEOUT_SECONDS = 86400


def validate_host_job_name(name: str) -> str:
    """Return *name* unchanged if it is safe as a host-jobs-dir filename.

    Raises :class:`ValueError` otherwise. This is the Python-side mirror
    of the bash regex check ``docs/ops/job-dispatcher.sh.example`` runs
    before building ``$TRELLIS_HOST_JOBS_DIR/<name>`` — the same pattern,
    checked in both places because the dispatcher is documentation, not
    installed code this test suite can import.
    """
    if not HOST_JOB_NAME_PATTERN.fullmatch(name):
        msg = (
            f"job name {name!r} does not match "
            f"{HOST_JOB_NAME_PATTERN.pattern!r} — refusing to treat it as "
            "a host-jobs-dir filename"
        )
        raise ValueError(msg)
    return name


@dataclass(frozen=True)
class JobSpec:
    """One catalog entry: a job's fixed identity, independent of the registry row.

    ``argv`` is ``None`` for a host-only job (its command lives on the
    host filesystem, not in source) and a non-empty tuple otherwise —
    enforced in :meth:`__post_init__`, so a malformed catalog entry fails
    at import time rather than at the first ``due-jobs`` call.
    """

    name: str
    description: str
    host_only: bool = False
    argv: tuple[str, ...] | None = None
    default_cadence: str | None = None
    default_timeout_seconds: int = 3600

    def __post_init__(self) -> None:
        validate_host_job_name(self.name)
        if not self.description.strip():
            msg = f"job {self.name!r} must have a non-blank description"
            raise ValueError(msg)
        if self.host_only:
            if self.argv is not None:
                msg = (
                    f"host-only job {self.name!r} must not carry argv in "
                    "source — its command is $TRELLIS_HOST_JOBS_DIR/<name>"
                )
                raise ValueError(msg)
        elif not self.argv:
            msg = f"non-host-only job {self.name!r} must carry a non-empty argv"
            raise ValueError(msg)
        timeout = self.default_timeout_seconds
        if not _MIN_TIMEOUT_SECONDS <= timeout <= _MAX_TIMEOUT_SECONDS:
            msg = (
                f"job {self.name!r} default_timeout_seconds "
                f"{self.default_timeout_seconds} out of range "
                f"{_MIN_TIMEOUT_SECONDS}..{_MAX_TIMEOUT_SECONDS}"
            )
            raise ValueError(msg)
        if self.default_cadence is not None:
            try:
                CronSchedule.parse(self.default_cadence)
            except CronSyntaxError as exc:
                msg = f"job {self.name!r} default_cadence is invalid: {exc}"
                raise ValueError(msg) from exc


def _build_catalog(specs: Iterable[JobSpec]) -> Mapping[str, JobSpec]:
    catalog: dict[str, JobSpec] = {}
    for spec in specs:
        if spec.name in catalog:
            msg = f"duplicate job name in catalog: {spec.name!r}"
            raise ValueError(msg)
        catalog[spec.name] = spec
    return catalog


def _config_dir() -> Path:
    """Mirror ``trellis_cli.config.get_config_dir`` without importing it.

    ``trellis`` (core) must not import ``trellis_cli`` — the same
    ``TRELLIS_CONFIG_DIR`` env-var pattern already lives at the core
    layer in ``trellis.stores.registry.StoreRegistry.from_config_dir``.
    """
    return Path(os.environ.get("TRELLIS_CONFIG_DIR", str(Path.home() / ".trellis")))


def _data_dir() -> Path:
    return Path(os.environ.get("TRELLIS_DATA_DIR", str(_config_dir() / "data")))


#: Resolved once per process, at import time — same posture as
#: ``trellis.core.write_provenance``'s stamp: environment-specific, but
#: not something that needs re-reading mid-process.
_FEEDBACK_LOG_DIR = str(_data_dir() / "stores" / "feedback")


JOB_CATALOG: Mapping[str, JobSpec] = _build_catalog(
    (
        # -- Host-only: need the docker socket, gh auth, or /mnt/data. --
        JobSpec(
            name="capture-nightly",
            host_only=True,
            default_cadence="0 3 * * *",
            description=(
                "Session capture sweep — turns recent Claude Code sessions "
                "into traces. Host-only: the wrapper script it runs reads "
                "from the host's Claude Code project directories, which no "
                "container has mounted."
            ),
        ),
        JobSpec(
            name="curate-nightly",
            host_only=True,
            default_cadence="30 3 * * *",
            description=(
                "trellis worker curate --reconcile-first — reconciles and "
                "promotes recent traces. Host-only, same posture as "
                "capture-nightly: run by the same host wrapper script."
            ),
        ),
        JobSpec(
            name="backup-nightly",
            host_only=True,
            default_cadence="30 4 * * *",
            description=(
                "pg_dump of both Postgres planes plus an archive to "
                "/mnt/data. Needs the docker socket — host-only."
            ),
        ),
        JobSpec(
            name="roadmap-nightly",
            host_only=True,
            default_cadence="0 5 * * *",
            description=(
                "Posts the nightly roadmap-driver comment to issue #275. "
                "Needs gh auth — host-only."
            ),
        ),
        # -- Trellis-native periodic job. --
        JobSpec(
            name="tune",
            argv=("trellis", "worker", "tune", "--dry-run", "--format", "json"),
            default_cadence="0 4 * * *",
            description=(
                "Parameter tuner, dry-run (#557 D4): proposes promotions "
                "and rollbacks without applying them until a human reviews."
            ),
        ),
        # -- Trellis-native manual-only jobs: cadence=None by default, --
        # -- triggered only via ScheduledJob.run_requested_at. --
        JobSpec(
            name="worker-enrich",
            argv=("trellis", "worker", "enrich", "--format", "json"),
            description="LLM-backed enrichment pass over recent documents.",
        ),
        JobSpec(
            name="worker-mine-precedents",
            argv=("trellis", "worker", "mine-precedents", "--format", "json"),
            description="Mine failure-trace clusters into candidate precedents.",
        ),
        JobSpec(
            name="worker-embed-traces",
            argv=("trellis", "worker", "embed-traces", "--format", "json"),
            description="Backfill trace embeddings from the watermark cursor.",
        ),
        JobSpec(
            name="reconcile-feedback",
            argv=(
                "trellis",
                "admin",
                "reconcile-feedback",
                "--log-dir",
                _FEEDBACK_LOG_DIR,
                "--format",
                "json",
            ),
            description=(
                "Replay pack_feedback.jsonl rows the EventLog is missing "
                "as FEEDBACK_RECORDED events."
            ),
        ),
        JobSpec(
            name="classify-backfill",
            argv=("trellis", "classify", "backfill", "--format", "json"),
            description="Re-run deterministic classifiers over stale-tagged documents.",
        ),
        JobSpec(
            name="classify-shadow",
            argv=("trellis", "classify", "shadow", "--format", "json"),
            description="LLM shadow-tag documents with no shadow record yet.",
        ),
        JobSpec(
            name="classify-shadow-report",
            argv=("trellis", "classify", "shadow-report", "--format", "json"),
            description="Summarize shadow-vs-deterministic tag agreement.",
        ),
        JobSpec(
            name="classify-tag-candidates",
            argv=("trellis", "classify", "tag-candidates", "--format", "json"),
            description="Mine shadow domain tags into human-reviewable candidates.",
        ),
        JobSpec(
            name="classify-domain-candidates",
            argv=("trellis", "classify", "domain-candidates", "--format", "json"),
            description="Propose domain-alias merges from shadow tag co-occurrence.",
        ),
        JobSpec(
            name="admin-reindex-vectors",
            argv=("trellis", "admin", "reindex-vectors", "--format", "json"),
            description="Rebuild vector index rows from document store content.",
        ),
        JobSpec(
            name="admin-resync-vector-metadata",
            argv=(
                "trellis",
                "admin",
                "resync-vector-metadata",
                "--format",
                "json",
            ),
            description=(
                "Re-sync vector row metadata snapshots after a post-embed write."
            ),
        ),
        JobSpec(
            name="admin-backfill-outcomes",
            argv=("trellis", "admin", "backfill-outcomes", "--format", "json"),
            description="Backfill OutcomeStore rows from historical feedback.",
        ),
        JobSpec(
            name="admin-backfill-name-aliases",
            argv=("trellis", "admin", "backfill-name-aliases", "--format", "json"),
            description="Backfill name-alias edges for existing entities.",
        ),
        JobSpec(
            name="admin-migrate-provenance",
            argv=("trellis", "admin", "migrate-provenance", "--format", "json"),
            description=(
                "Backfill write_provenance metadata onto rows written "
                "before it existed."
            ),
        ),
    )
)
