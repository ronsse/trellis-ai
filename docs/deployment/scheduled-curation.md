# Scheduled curation — bring-your-own-scheduler recipes

> **Who this is for:** operators wiring Trellis's unattended workers (`worker curate`, `worker tune`, `worker enrich`, `worker mine-precedents`, plus `analyze schema-evolution` and `admin reconcile-feedback`) into an existing scheduler.

> **The boundary:** **Trellis is not a scheduler.** It ships commands that play cleanly with cron, systemd timers, GitHub Actions, Kubernetes CronJobs, Airflow, etc. — you bring the runner. The one exception is `worker curate --interval N`, a plain-sleep convenience loop for single-host setups that don't have a scheduler; it introduces no scheduler dependency (APScheduler / Celery were deliberately rejected).

For *what* each command does, its autonomy tier, and where the human-in-the-loop steps land, read the runbook first: [`../getting-started/running-trellis.md`](../getting-started/running-trellis.md). This page is purely the scheduling recipes.

---

## Recommended cadence

| Command | Cadence | Why | LLM? | Autonomy |
|---|---|---|---|---|
| `trellis admin reconcile-feedback --log-dir DIR` | Before every curate run (or use `worker curate --reconcile-first`) | Backfills file-only `pack_feedback.jsonl` rows into the EventLog so the curate cycle sees every signal. | no | — |
| `trellis worker curate --output-dir DIR` | Daily | Demote (noise-tag) + advisory upkeep run unattended; learning candidates are written for human review. | no | Tier 0 + Tier 2 |
| `trellis worker tune` | Daily, **only where `auto_promote.enabled`** | Re-monitors recent auto-promotions and rolls back any that degraded; promotes newly-qualifying proposals. With auto-promote off it's a cheap no-op — schedule it only once you've opted a scope in. | no | Tier 1 |
| `trellis worker enrich` | Daily, off-peak | LLM tagging of under-tagged documents — costs money/time, so run when the warehouse and API are quiet. | **yes** | — |
| `trellis worker mine-precedents` | Weekly | Failure-trace mining is comparatively expensive and the candidates need human review anyway; weekly keeps the review queue manageable. | **yes** | — |
| `trellis analyze schema-evolution` | Weekly | Surfaces open-string types eligible for canonical promotion. Surface-only — a human authors the ADR amendment. | no | Tier 3 (surface) |

**Ordering within a run:** reconcile → curate → (tune). Reconcile must precede curate so the cycle reads a complete EventLog. `worker curate --reconcile-first` folds the reconcile into the curate invocation if you'd rather not schedule it separately.

**A note on `--format json`:** every command supports it; schedule with `--format json` and pipe to your log aggregator so you can alert on the structured counts rather than scraping human text. Exit code is `0` on success and non-zero on genuine errors — gate your alerting on both.

---

## Recipe 1 — crontab

A single user crontab covering the daily and weekly cadence. Adjust paths and the `trellis` binary location for your install (these examples assume it's on `PATH`).

```cron
# /etc/cron.d/trellis-curation   (or `crontab -e` for a user crontab)
# All times UTC. TRELLIS_CONFIG_DIR / TRELLIS_DATA_DIR point at the deployment.
TRELLIS_CONFIG_DIR=/var/lib/trellis/config
TRELLIS_DATA_DIR=/var/lib/trellis/data
PATH=/usr/local/bin:/usr/bin:/bin

# Daily 02:30 — reconcile-first curate (demote + advisory upkeep + learning artifacts)
30 2 * * *  trellis-user  trellis worker curate --output-dir /var/lib/trellis/review --reconcile-first --days 30 --format json >> /var/log/trellis/curate.log 2>&1

# Daily 02:45 — tuner pass (only meaningful with learning.auto_promote.enabled: true)
45 2 * * *  trellis-user  trellis worker tune --format json >> /var/log/trellis/tune.log 2>&1

# Daily 04:00 (off-peak) — LLM enrichment of under-tagged documents
0 4 * * *   trellis-user  trellis worker enrich --limit 100 --format json >> /var/log/trellis/enrich.log 2>&1

# Weekly Sun 05:00 — precedent mining (LLM; candidates need human review)
0 5 * * 0   trellis-user  trellis worker mine-precedents --min-traces 3 --format json >> /var/log/trellis/mine.log 2>&1

# Weekly Sun 05:30 — schema-evolution scan (surface-only; emits WELL_KNOWN_CANDIDATE events)
30 5 * * 0  trellis-user  trellis analyze schema-evolution --format json >> /var/log/trellis/schema.log 2>&1
```

Monitor the log files; alert on non-zero exit codes. Drop the `tune` line until you've enabled `learning.auto_promote`, and the `enrich` / `mine-precedents` lines until an `llm:` block is configured (both exit non-zero without one).

---

## Recipe 2 — systemd timers

One `oneshot` service + timer pair per cadence. Below is the daily curate pair; clone the pattern (changing `ExecStart`, `OnCalendar`, and the unit name) for `tune`, `enrich`, `mine-precedents`, and `schema-evolution`.

`/etc/systemd/system/trellis-curate.service`:

```ini
[Unit]
Description=Trellis daily curation cycle
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=trellis
Environment=TRELLIS_CONFIG_DIR=/var/lib/trellis/config
Environment=TRELLIS_DATA_DIR=/var/lib/trellis/data
# Secrets via EnvironmentFile (keep out of the unit; e.g. rendered by op run):
# EnvironmentFile=/etc/trellis/secrets.env
ExecStart=/usr/local/bin/trellis worker curate \
  --output-dir /var/lib/trellis/review --reconcile-first --days 30 --format json
```

`/etc/systemd/system/trellis-curate.timer`:

```ini
[Unit]
Description=Run Trellis curation daily at 02:30 UTC

[Timer]
OnCalendar=*-*-* 02:30:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

Enable it:

```bash
systemctl daemon-reload
systemctl enable --now trellis-curate.timer
systemctl list-timers 'trellis-*'        # confirm the next-run schedule
journalctl -u trellis-curate.service     # read a run's output
```

For the weekly jobs use `OnCalendar=Sun *-*-* 05:00:00 UTC`. `Persistent=true` runs a missed job once the host comes back up.

---

## Recipe 3 — GitHub Actions

The runnable workflow lives at [`examples/integrations/github-actions/curation.yml`](../../examples/integrations/github-actions/curation.yml) — copy that file. The two schedules and the commands they run:

```yaml
on:
  schedule:
    - cron: "30 2 * * *"   # daily 02:30 UTC — reconcile-feedback, worker curate, worker tune
    - cron: "0 5 * * 0"    # weekly Sun 05:00 UTC — worker mine-precedents, analyze schema-evolution
```

The daily job reconciles feedback, runs the curation cycle, runs the tuner pass (a no-op unless `learning.auto_promote.enabled`), and uploads the learning-candidate review artifacts. The env/secrets wiring, checkout/install steps, and the weekly job are in the example file — copy it rather than this doc, so there is exactly one source to keep current.

**Promotion stays human-gated** — the workflow uploads `intent_learning_candidates.json` / `promotion_decisions.template.json` as build artifacts; a person downloads them, approves rows, and runs `trellis curate promote-learning` locally or in a separate gated workflow.

---

## Recipe 4 — Kubernetes CronJob

One `CronJob` per cadence. The daily curate job:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: trellis-curate
spec:
  schedule: "30 2 * * *"            # daily 02:30 UTC
  concurrencyPolicy: Forbid         # never overlap two curate runs
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: trellis
              image: trellis-cli:latest   # pin to a real tag in production
              command:
                - sh
                - -c
                - >
                  trellis admin reconcile-feedback --log-dir /data --format json &&
                  trellis worker curate --output-dir /data/review --days 30 --format json
              env:
                - name: TRELLIS_CONFIG_DIR
                  value: /config
                - name: TRELLIS_DATA_DIR
                  value: /data
                - name: TRELLIS_OPERATIONAL_PG_DSN
                  valueFrom:
                    secretKeyRef:
                      name: trellis-config
                      key: operational_pg_dsn
              volumeMounts:
                - { name: config, mountPath: /config }
                - { name: data, mountPath: /data }
          restartPolicy: OnFailure
          volumes:
            - { name: config, configMap: { name: trellis-config } }
            - { name: data, persistentVolumeClaim: { claimName: trellis-data } }
```

Clone the manifest for the other cadences — change `metadata.name`, `spec.schedule`, and the `command`:

```yaml
# trellis-tune     schedule: "45 2 * * *"   command: trellis worker tune --format json
# trellis-enrich   schedule: "0 4 * * *"    command: trellis worker enrich --limit 100 --format json
# trellis-mine     schedule: "0 5 * * 0"    command: trellis worker mine-precedents --format json
# trellis-schema   schedule: "30 5 * * 0"   command: trellis analyze schema-evolution --format json
```

Use `concurrencyPolicy: Forbid` on every Trellis CronJob so a slow run never overlaps its next trigger.

---

## Verifying a recipe before you schedule it

Every command line above was verified against the merged worker code. Before wiring one into production, dry-run it against an isolated config dir + demo data so you see the real output shape (the same checklist as the runbook):

```bash
export TRELLIS_CONFIG_DIR=/tmp/trellis-check/config
export TRELLIS_DATA_DIR=/tmp/trellis-check/data
trellis admin init && trellis demo load

trellis admin reconcile-feedback --log-dir "$TRELLIS_DATA_DIR" --dry-run --format json
#   -> {"status": "ok", "dry_run": true, "scanned": 0, "already_present": 0, "would_emit": 0, "failed": 0}

trellis worker curate --output-dir /tmp/trellis-check/review --dry-run --format json
#   -> {"status": "ok", ..., "candidates_path": null, "decisions_path": null, "dry_run": true}

trellis worker tune --dry-run --format json
#   -> {"enabled": false, "dry_run": true, "proposals_considered": 0, "auto_promoted": 0, ...}
```

> `worker enrich` and `worker mine-precedents` cannot be dry-run without a configured LLM client — they check for a buildable client *before* the dry-run branch and exit non-zero with an actionable message if none is present. Confirm an `llm:` block + the `[llm-openai]` / `[llm-anthropic]` extra are in place before scheduling either.

---

## Further reading

- [`../getting-started/running-trellis.md`](../getting-started/running-trellis.md) — the operating runbook: what each process does, its tier, and the human-in-the-loop steps.
- [`../agent-guide/freshness-and-curation.md`](../agent-guide/freshness-and-curation.md) — the extraction/refresh half (`trellis extract refresh`) and its own scheduler recipes.
- [`../design/adr-autonomy-ladder.md`](../design/adr-autonomy-ladder.md) — the autonomy tiers.
</content>
</invoke>
