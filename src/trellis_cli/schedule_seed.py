"""Default schedule-registry entries, seeded by ``trellis admin init-schedule``.

Five periodic jobs mirror the nightly cron already running on skynet
(read-only research against ``~/projects/skynet-hub/stacks/trellis/
*-nightly.sh`` and ``crontab -l`` — skynet-hub is the deployment owner's
repo, not this one). Fourteen manual-only jobs (``cadence=None``, run only
via ``run_requested_at``) cover the existing CLI commands the UI gap
inventory names as "Run now" candidates.

Deliberately excluded: ``trellis admin migrate-graph``. Every job below
has one stable default invocation; migrate-graph's whole shape is
``--from-config`` / ``--to-config`` pointing at two YAML files chosen per
migration, so there is no default command to seed — an operator runs it
directly, by hand, the day they need a one-off cross-backend migration.
"""

from __future__ import annotations

from trellis.schemas.schedule import ScheduledJob
from trellis_cli.config import get_data_dir

#: Where the real nightly wrapper scripts live. Each one already does its
#: own ``op read`` env setup before invoking the real binary, so the
#: registry entry is just the script path — one argv element, no
#: interpolation, no shell. See ``docs/ops/job-dispatcher.sh.example``.
_SKYNET_HUB_TRELLIS_STACK = "/home/nronsse/projects/skynet-hub/stacks/trellis"


def default_scheduled_jobs() -> list[ScheduledJob]:
    """Build the default registry rows.

    A function, not a module-level constant: the ``reconcile-feedback``
    entry's ``--log-dir`` is resolved from
    :func:`~trellis_cli.config.get_data_dir`, which reads
    ``TRELLIS_CONFIG_DIR`` / ``config.yaml`` — environment-specific, not a
    value this module can bake in once at import time.
    """
    feedback_log_dir = str(get_data_dir() / "stores" / "feedback")
    stack = _SKYNET_HUB_TRELLIS_STACK

    return [
        # -- Periodic jobs: mirror the real nightly crontab. --
        ScheduledJob(
            name="capture-nightly",
            command=[f"{stack}/capture-nightly.sh"],
            cadence="0 3 * * *",
            description=(
                "Session capture sweep — turns recent Claude Code sessions "
                "into traces. Pure-Python once a container-side dispatcher "
                "exists; host-run for now because the host crontab already "
                "runs it."
            ),
            host_only=False,
        ),
        ScheduledJob(
            name="curate-nightly",
            command=[f"{stack}/curate-nightly.sh"],
            cadence="30 3 * * *",
            description=(
                "trellis worker curate --reconcile-first — reconciles and "
                "promotes recent traces. Pure-Python, same posture as "
                "capture-nightly."
            ),
            host_only=False,
        ),
        ScheduledJob(
            name="backup-nightly",
            command=[f"{stack}/backup-nightly.sh"],
            cadence="30 4 * * *",
            description=(
                "pg_dump of both Postgres planes plus an archive to "
                "/mnt/data. Needs the docker socket — host-only."
            ),
            host_only=True,
        ),
        ScheduledJob(
            name="roadmap-nightly",
            command=[f"{stack}/roadmap-nightly.sh"],
            cadence="0 5 * * *",
            description=(
                "Posts the nightly roadmap-driver comment to issue #275. "
                "Needs gh auth — host-only."
            ),
            host_only=True,
        ),
        ScheduledJob(
            name="tune",
            command=["trellis", "worker", "tune", "--dry-run", "--format", "json"],
            cadence="0 4 * * *",
            description=(
                "Parameter tuner, dry-run (#557 D4): proposes promotions "
                "and rollbacks without applying them until a human reviews."
            ),
            host_only=False,
        ),
        # -- Manual-only jobs: cadence=None, triggered by run_requested_at. --
        ScheduledJob(
            name="worker-enrich",
            command=["trellis", "worker", "enrich", "--format", "json"],
            cadence=None,
            description="LLM-backed enrichment pass over recent documents.",
            host_only=False,
        ),
        ScheduledJob(
            name="worker-mine-precedents",
            command=["trellis", "worker", "mine-precedents", "--format", "json"],
            cadence=None,
            description="Mine failure-trace clusters into candidate precedents.",
            host_only=False,
        ),
        ScheduledJob(
            name="worker-embed-traces",
            command=["trellis", "worker", "embed-traces", "--format", "json"],
            cadence=None,
            description="Backfill trace embeddings from the watermark cursor.",
            host_only=False,
        ),
        ScheduledJob(
            name="reconcile-feedback",
            command=[
                "trellis",
                "admin",
                "reconcile-feedback",
                "--log-dir",
                feedback_log_dir,
                "--format",
                "json",
            ],
            cadence=None,
            description=(
                "Replay pack_feedback.jsonl rows the EventLog is missing "
                "as FEEDBACK_RECORDED events."
            ),
            host_only=False,
        ),
        ScheduledJob(
            name="classify-backfill",
            command=["trellis", "classify", "backfill", "--format", "json"],
            cadence=None,
            description="Re-run deterministic classifiers over stale-tagged documents.",
            host_only=False,
        ),
        ScheduledJob(
            name="classify-shadow",
            command=["trellis", "classify", "shadow", "--format", "json"],
            cadence=None,
            description="LLM shadow-tag documents with no shadow record yet.",
            host_only=False,
        ),
        ScheduledJob(
            name="classify-shadow-report",
            command=["trellis", "classify", "shadow-report", "--format", "json"],
            cadence=None,
            description="Summarize shadow-vs-deterministic tag agreement.",
            host_only=False,
        ),
        ScheduledJob(
            name="classify-tag-candidates",
            command=["trellis", "classify", "tag-candidates", "--format", "json"],
            cadence=None,
            description="Mine shadow domain tags into human-reviewable candidates.",
            host_only=False,
        ),
        ScheduledJob(
            name="classify-domain-candidates",
            command=["trellis", "classify", "domain-candidates", "--format", "json"],
            cadence=None,
            description="Propose domain-alias merges from shadow tag co-occurrence.",
            host_only=False,
        ),
        ScheduledJob(
            name="admin-reindex-vectors",
            command=["trellis", "admin", "reindex-vectors", "--format", "json"],
            cadence=None,
            description="Rebuild vector index rows from document store content.",
            host_only=False,
        ),
        ScheduledJob(
            name="admin-resync-vector-metadata",
            command=[
                "trellis",
                "admin",
                "resync-vector-metadata",
                "--format",
                "json",
            ],
            cadence=None,
            description=(
                "Re-sync vector row metadata snapshots after a post-embed write."
            ),
            host_only=False,
        ),
        ScheduledJob(
            name="admin-backfill-outcomes",
            command=["trellis", "admin", "backfill-outcomes", "--format", "json"],
            cadence=None,
            description="Backfill OutcomeStore rows from historical feedback.",
            host_only=False,
        ),
        ScheduledJob(
            name="admin-backfill-name-aliases",
            command=["trellis", "admin", "backfill-name-aliases", "--format", "json"],
            cadence=None,
            description="Backfill name-alias edges for existing entities.",
            host_only=False,
        ),
        ScheduledJob(
            name="admin-migrate-provenance",
            command=["trellis", "admin", "migrate-provenance", "--format", "json"],
            cadence=None,
            description=(
                "Backfill write_provenance metadata onto rows written "
                "before it existed."
            ),
            host_only=False,
        ),
    ]
