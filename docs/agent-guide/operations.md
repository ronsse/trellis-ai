# Operations Reference

Complete CLI and Python API reference for the Trellis.

All CLI commands support `--format json` for machine-readable output. Use `--format json` when calling from scripts or agent tool adapters.

---

## Admin Commands

### `trellis admin init`

Initialize Trellis stores and configuration.

```bash
trellis admin init [--data-dir PATH] [--force] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--data-dir` | `$TRELLIS_DATA_DIR`, else `data/` in the config dir (`$TRELLIS_CONFIG_DIR`, else `~/.trellis`) | Custom data directory path |
| `--force` | `false` | Overwrite existing config |
| `--format` | `text` | Output format |

`init` writes the data directory into `config.yaml` as `data_dir`. Every surface that opens the stores (CLI commands, `admin health`, the MCP server, the REST API and the capture sweep) reads it from there first, then from `$TRELLIS_DATA_DIR`, then uses `data/` in the config dir. With `--force`, the `--data-dir` default above applies: `init` does not read the `config.yaml` it replaces.

When `config.yaml` exists and `--force` is not given, `init` leaves the file alone. If the `stores/` directory in that data directory is missing, `init` recreates it and reports `"stores_created": true`. That is the fix for `Stores not initialized at PATH`.

**JSON output (success):**

```json
{"status": "initialized", "config_dir": "/home/user/.trellis", "data_dir": "/home/user/.trellis/data", "next_steps_doc": "docs/getting-started/setup-decisions.md"}
```

**JSON output (already exists):**

```json
{"status": "exists", "config_dir": "/home/user/.trellis", "data_dir": "/home/user/.trellis/data", "stores_created": false}
```

### `trellis admin health`

Check health of Trellis stores. Read-only: it resolves each store's backend from `config.yaml` without opening the store, so it makes no connection and creates no file.

```bash
trellis admin health [--format text|json]
```

**JSON output** (knowledge plane on Postgres, operational plane on SQLite):

```json
{
  "config": true,
  "data_dir": true,
  "stores_dir": true,
  "events.db": true,
  "traces.db": false,
  "backends": {
    "document": "postgres",
    "graph": "postgres",
    "vector": "pgvector",
    "event_log": "sqlite",
    "trace": "sqlite"
  }
}
```

`backends` is always present and names the configured backend of each store health reports on: `document`, `graph`, `vector`, `event_log` and `trace`. The blob, outcome, parameter, tuner_state and api_key stores are not reported. Only a `sqlite` store gets a `.db` key, and `false` means its file does not exist. That file is the store's `db_path` when `config.yaml` sets one, otherwise the key's name under the stores directory. `trellis admin init` creates no store files: each is created the first time its store is used, so `false` is normal on a fresh install. A store on any other backend has no `.db` key, and text output lists it as, for example, `postgres (not checked)`. A value that names no registered backend (a typo, `null`, or a DSN written where a name belongs) is `null` in `backends` and `unknown backend (not checked)` in text output; health never prints the value. Health opens no connection, so it cannot tell you whether that store is reachable.

### `trellis admin stats`

Show store counts.

```bash
trellis admin stats [--format text|json]
```

**JSON output:**

```json
{"status": "ok", "traces": 42, "documents": 15, "document_rows": 37, "nodes": 23, "edges": 31, "events": 127}
```

`documents` counts **whole documents** and reconciles with
`GET /api/v1/documents`' `total`; `document_rows` counts **every stored row**,
including the `<parent>#chunk-N` fragments corpus ingestion writes, and
reconciles with `GET /api/v1/documents?include_chunks=true` (#412). The same
two keys are returned by `GET /api/v1/stats`, field for field.

> **Breaking, since #451:** `documents` used to be the row count. A consumer
> that wants that number reads `document_rows`. Nothing errors on the change —
> a reader that is not updated silently reports a different population.

### `GET /api/v1/loops`

Is each curation / learning loop actually doing anything? Admin scope,
REST-only — no CLI wrapper. Reads existing events only
(`trellis.ops.loop_health.summarize_loop_health`), plus one documented
live read of `TunerStateStore.list_proposals(status="pending")` for the
tuner row's backlog size.

Seven rows, in nightly-cycle order: `noise_demotion`,
`advisory_generation`, `advisory_fitness`, `learning_candidate_scoring`,
`precedent_promotion`, `tuner`, `feedback_intake`. Each carries
`actuates: bool` and a one-line `what_it_changes`, so a caller can tell
"writes automatically" apart from "sensing only, a human still approves
every change" without reading source.

**Never-run vs ran-and-counted-zero**, the one load-bearing distinction:
a loop with no visible event reports `last_run_at` / `last_status` /
`counters` all `null` — "never run (or ran before this build)". A loop
that ran and found nothing to do reports a real `last_run_at` and a
`counters` dict whose values are legitimately `0`. The two must never
render the same; collapsing them is exactly the failure CLAUDE.md's
health-signal rule exists to prevent.

```json
{
  "generated_at": "2026-10-10T23:00:00+00:00",
  "loops": [
    {
      "name": "tuner",
      "description": "Runs a RuleTuner pass over outcomes. The scheduled run always passes --dry-run, so the schedule itself never auto-promotes; learning.auto_promote.enabled only takes effect on a manual `trellis worker tune` run made without --dry-run.",
      "actuates": true,
      "what_it_changes": "The scheduled run writes nothing but pending proposals. Auto-promoting a proposal needs both learning.auto_promote.enabled set AND a manual run without --dry-run; until both hold, every proposal stays pending for a human to run `trellis metrics promote --commit`.",
      "job_name": "tune",
      "last_run_at": null,
      "last_status": null,
      "counters": {"pending_count": 3, "auto_promoted_total": 0}
    }
  ]
}
```

The nightly cycle makes this durable by emitting `CURATE_CYCLE_COMPLETED`
(end of every live `trellis worker curate` run) and `TUNE_CYCLE_COMPLETED`
(end of every live `trellis worker tune` run) — never on a `--dry-run`
pass, which must still write nothing to the EventLog. Both emits are
wrapped whole (CLAUDE.md's advisory-step rule): a broken EventLog write
logs a warning but never fails the cycle it is reporting on.

The operator UI's **Loops** tab (`src/trellis_api/static/index.html`)
renders this report: `last_run_at` labelled UTC with relative age, a row
flagged `stale` when that age exceeds 36h regardless of its own
`last_status`, `null` rendered as "Not measured", and a fetch failure
rendered as a visible error rather than a silent empty list.

### `trellis admin show-prompt`

Print the retrieve, record, grade loop as text for a consumer that reads no skill: a model called with an API key, a framework's system message, or a dispatched subagent.

```bash
trellis admin show-prompt [--variant system|claude-md|agent-rule] [--format text|json]
```

| Variant | For |
|---|---|
| `system` (default) | the system message of a model that has the Trellis tools |
| `claude-md` | a section of `CLAUDE.md`, `AGENTS.md` or a Cursor rule |
| `agent-rule` | one paragraph for every agent definition that gets dispatched |

The text is the packaged file in `trellis_cli/prompts/`, printed byte for byte (no Rich markup, no wrapping), so it can be piped straight into a config file. Where to paste each variant is in [`docs/getting-started/system-prompt.md`](../getting-started/system-prompt.md).

**JSON output** (one variant per call):

```json
{
  "variant": "system",
  "text": "You have Trellis memory tools. ..."
}
```

An unknown `--variant` exits `EXIT_VALIDATION` (2) under both formats. `trellis admin install-skills` and `trellis admin quickstart --with-skills` end their text output with the `agent-rule` paragraph and a pointer to this command, and their `--format json` payload carries the same paragraph as an additive `agent_rule` key.

### `trellis admin serve`

Start the REST API server.

```bash
trellis admin serve [--port PORT] [--host HOST]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--port` | `8420` | Port to listen on |
| `--host` | `127.0.0.1` | Host to bind to |

Both `trellis admin serve` and `trellis serve` refuse to bind a
non-loopback host (`0.0.0.0`, `::`, a LAN address, …) unless
`TRELLIS_AUTH_MODE=required` — `off` and `optional` both grant every
caller full scopes when no credential is presented, so exposing either
of those off loopback serves the whole memory system unauthenticated
(#189). The refusal is a `ConfigError` (`EXIT_STORE`) naming both ways
out: set `TRELLIS_AUTH_MODE=required` and mint a key with `trellis admin
api-keys create`, or set `TRELLIS_API_ALLOW_INSECURE_BIND=1` when the
listener is genuinely on a trusted interface (a sidecar proxy
terminating auth, a private network with no other tenants).

### Schedule registry + job runs

`schedule.json` (`<data_dir>/stores/schedule.json`) is the pieces a **host-side
dispatcher cron** needs so schedules can be edited from a UI without the
container ever touching crontab. The REST routes, CLI parity and UI tab
that edit it are documented below, under
["Admin job schedules: REST, CLI and UI"](#admin-job-schedules-rest-cli-and-ui).

**The command boundary.** `schedule.json` holds only operator-tunable state —
it never holds a command. Each row is a `ScheduledJob`: `name` (must be a key
in `trellis.schedule.catalog.JOB_CATALOG`, validated on every read and write —
an unknown name degrades the row, it is never treated as a job to run),
`cadence` (a 5-field cron expression, or `null` for manual-only — triggered
later by setting `run_requested_at`), `enabled`, an optional `timeout_seconds`
override (bounded `1..86400`; `null` defers to the catalog default), and
`run_requested_at`. The command, operator-facing description, and `host_only`
flag all come from [`trellis.schedule.catalog`](../../src/trellis/schedule/catalog.py)
under that same `name` — the **only** source of what a job runs. A
Trellis-native job carries its argv fixed in source; a host-only job
(`capture-nightly`, `curate-nightly`, `backup-nightly`, `roadmap-nightly` —
they need the host's project directories, the docker socket, or `gh` auth,
none of which the API container has) carries no argv at all — its executable
is `$TRELLIS_HOST_JOBS_DIR/<name>`, placed there by the operator outside the
shared data mount, resolved by the host dispatcher itself after checking
`name` against `^[a-z0-9-]+$` (`trellis.schedule.catalog.validate_host_job_name`).
Because the model is `extra="forbid"`, a legacy or tampered row still
carrying the old `command` field fails validation the same way an unknown
`name` does — it degrades, it does not run. So editing `schedule.json`, by
hand or over the PR 6 API below, can change only *whether* and *when* a
catalog job runs, never *what* runs; see
[`catalog.py`](../../src/trellis/schedule/catalog.py)'s module docstring and
[`schedule_store.py`](../../src/trellis/stores/schedule_store.py)'s for the
full argument. The one thing that execs a job is the reference dispatcher at
[`docs/ops/job-dispatcher.sh.example`](../ops/job-dispatcher.sh.example) —
documentation, not installed by this repo — which reads the command from
`due-jobs`' catalog-joined output (never from `schedule.json`), builds it as
a bash array, and execs it directly, never through `sh -c` or `eval`.

`ScheduleStore` is a third [`DegradableJsonStore`](#if-the-file-is-damaged)
subclass, alongside `PolicyStore` and `AdvisoryStore`: **reads degrade,
writes refuse.** A duplicate `name` degrades the file (`list()`/`due-jobs`
still serve what parsed) rather than silently collapsing two rows to one.

#### `trellis admin init-schedule`

Seed the registry from the catalog's defaults: the nightly
capture/curate/backup/roadmap sweeps, the tuner dry-run, and the manual-only
commands from the UI gap inventory (`trellis_cli.schedule_seed`, which builds
one minimal row per `trellis.schedule.catalog.JOB_CATALOG` entry). Adds only
jobs whose `name` is not already present — never overwrites an
operator-edited row, so it is safe to re-run. `trellis admin migrate-graph`
is deliberately not in the catalog: its whole shape is a pair of
`--from-config` / `--to-config` YAML paths chosen per migration, so there is
no stable default command to seed.

```bash
trellis admin init-schedule [--format text|json]
```

```json
{"status": "ok", "added": ["capture-nightly", "curate-nightly", "..."], "skipped": []}
```

Exits `EXIT_STORE` (5) on an already-degraded file, under both formats.

Seeding a row here does not itself make a job wait: a job that has never
completed a run is always due (`is_job_due`, below), so the very first time
a dispatcher polls `due-jobs` after `init-schedule`, every freshly-seeded
enabled job reports due — there is no grace period before the first tick.

#### `trellis admin due-jobs`

List registry jobs due to run now. Combines, per job
(`trellis.core.cron.is_job_due`): `enabled` (false always wins),
`run_requested_at` (newer than the last completed run is always due — the
only way a `cadence: null` job ever becomes due), and `cadence` against the
most recent `JOB_RUN_COMPLETED` for that job name in the EventLog. A job
that has never completed is always due.

```bash
trellis admin due-jobs [--format text|json]
```

```json
{"status": "ok", "count": 2, "due_jobs": [{"name": "tune", "command": ["trellis", "worker", "tune", "--dry-run", "--format", "json"], "host_only": false, "cadence": "0 4 * * *", "timeout_seconds": 3600, "last_completed_at": null, "run_requested_at": null}, {"name": "backup-nightly", "command": null, "host_only": true, "cadence": "30 4 * * *", "timeout_seconds": 3600, "last_completed_at": null, "run_requested_at": null}], "schedule_file": "/path/to/schedule.json", "schedule_file_present": true, "store_degradation": null}
```

`command` is joined from `trellis.schedule.catalog.JOB_CATALOG` by `name`,
never read from `schedule.json`. A host-only row reports `command: null` — a
marker, not a path — so the dispatcher resolves
`$TRELLIS_HOST_JOBS_DIR/<name>` itself; see "The command boundary" above.

Works on a degraded file — jobs that parsed are still checked — but reports
`"status": "degraded"` and exits `EXIT_STORE` (5) under both formats, same
posture as `trellis policy list`: a partial due-jobs answer must not reach a
dispatcher looking like a complete one.

#### `trellis admin record-job-run`

Record one job execution as a `JOB_RUN_STARTED` / `JOB_RUN_COMPLETED` event
pair, modeled on `CAPTURE_SWEEP_COMPLETED`: plain EventLog telemetry, not a
governed mutation — there is no state these events gate. Call once,
immediately after the job process exits.

```bash
trellis admin record-job-run --job NAME --duration-ms MS --exit-code N [--status STR] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--job` | — (required) | Job name (schedule registry key). |
| `--duration-ms` | — (required) | Wall-clock duration of the run, in ms. |
| `--exit-code` | — (required) | The job process's own exit code. |
| `--status` | derived | Free-form label; defaults to `ok` / `failed` from `--exit-code`. Use e.g. `skipped_overlap` when the dispatcher's `flock` meant the job never ran. |
| `--format` | `text` | `text` or `json`. |

`started_at` is derived as `now - duration_ms`, not taken as a second
argument — the dispatcher already measured the duration, so there is no
second number that could disagree with it.

```json
{"status": "ok", "job": "tune", "run_id": "01M...", "exit_code": 0, "job_status": "ok", "recorded": true}
```

**Recording a *failed* job's run is itself a successful call and exits 0** —
"the dispatcher ran this job and it failed" is exactly the fact this command
exists to durably record. Only a failure to write the EventLog pair itself
(store unavailable) is this command's own failure, and exits `EXIT_STORE`
(5) under both formats.

### Admin job schedules: REST, CLI and UI

The write surface onto `schedule.json` promised above: `GET`/`PATCH
/api/v1/admin/jobs`, `POST /api/v1/admin/jobs/{name}/run`
(`src/trellis_api/routes/admin.py`), CLI parity (`trellis admin schedule set`
/ `run`, `src/trellis_cli/admin_schedule.py`), and the web UI's **Schedules**
tab. All three REST routes require admin scope. **The writable surface is
exactly `enabled`, `cadence` (cron-validated) and `timeout_seconds` (bounded
`1..86400`) via the PATCH, and `run_requested_at` via the run route — the
same fields `ScheduledJob` exposes, and only for a `name` already present in
`JOB_CATALOG`.** No route creates, deletes, or can ever carry a `command`,
`argv`, `path` or environment variable: `AdminJobPatchRequest` has no such
field, and `extra="forbid"` refuses any other field name outright (422).
Both writes refuse with 409 and the schedule store's own error text (never a
generic message) while it is degraded or stale; both emit an audit event
(`SCHEDULE_UPDATED` / `JOB_RUN_REQUESTED`) carrying `old`/`new` dicts with
only the fields that actually changed, plus the caller's identity
(`key_id`/`key_name` over REST, `cli:<os user>` from the CLI).

**Both writes route through `MutationExecutor`.** The PATCH and run routes,
and the two CLI subcommands below, submit `Operation.SCHEDULE_UPDATE` /
`Operation.JOB_RUN_REQUEST` `Command`s (stamped `target_type="schedule"`,
`target_id=<job name>`) to `trellis.mutate.build_schedule_executor` — a
sibling of `build_curate_executor` with the same `policy_gate`, but a
disjoint handler dict. `ScheduleUpdateHandler` / `JobRunRequestHandler`
(`src/trellis/mutate/schedule_handlers.py`) are the only code that calls
`ScheduleStore.put`; every boundary described above (field allowlist,
catalog membership, degraded/stale refusal) is enforced there, before the
store write. These two operations are deliberately absent from
`create_curate_handlers`, so neither `POST /api/v1/commands/batch` nor MCP
`execute_mutation` can reach them — a `mutate`-scoped credential cannot
edit a schedule or request a run; that is admin-scoped only, via a
separate authorization axis. A policy with `entity_type="schedule"` denies
the PATCH/run routes and the CLI the same way it denies any other governed
mutation. `changed_by` is always the authenticated principal; no request
body or CLI flag can set it.

#### `GET /api/v1/admin/jobs`

Lists every `JOB_CATALOG` job, joined with its `schedule.json` row
(materialized with catalog defaults if the row doesn't exist yet — this
route never creates one) and its last-run status. `last_run` is derived from
the EventLog's own `JOB_RUN_STARTED`/`JOB_RUN_COMPLETED` pair for that job
name — `null` means no record at all, never a hardcoded placeholder; an
in-flight start newer than the last completion reports that start with every
other field `null`, rather than falling back to an older finished run.
Each row's `stored` is `false` for a materialized catalog default (no row in
`schedule.json` yet, or one that failed to parse out of a degraded store) —
such a row's `enabled`/`cadence` are catalog defaults, never the operator's
own choice, and `next_due_at` is unconditionally `null` for it, because
`due-jobs` only ever evaluates a *stored* row. `default_cadence` is always
the catalog's own default, shown alongside the effective `cadence` so an
operator can tell "customized" from "still the default". `store_degradation`
is `null` on a clean read and otherwise the same shape `GET /api/v1/policies`
and `GET /api/v1/advisories` already use — a caller must not read catalog
defaults as the operator's own stored choices with no signal the file
behind them is damaged.

`dispatcher_seen` (and `dispatcher_last_seen_at`) reflects only whether any
job has actually *run* recently — the most recent `JOB_RUN_STARTED` across
every job, `occurred_at` (when the EventLog recorded it, not necessarily
the payload's own `started_at`) — never "is any host dispatcher polling
`due-jobs` at all": a dispatcher can poll and find nothing due, which
leaves no event and reads identically to no dispatcher running (gate
#860b, follow-up 3; this is why the UI banner hedges rather than claiming
"nothing will pick it up"). The staleness window is **not** a fixed 15
minutes: a missing poll record is not evidence of a dead dispatcher, and a flat window
false-positives against any cadence slower than it (a daily job's
dispatcher would read "not reporting" ~23 hours a day). The window is the
longest enabled, stored cadence's own tick interval plus two hours of
slack, falling back to a 15-minute floor only when no enabled cadenced job
exists. Serves catalog defaults even when `schedule.json` is degraded (200,
not 409 — a read is never refused, only a write).

```json
{"count": 1, "jobs": [{"name": "tune", "description": "...", "host_only": false, "spends_llm": true, "heavy": false, "enabled": true, "cadence": "0 4 * * *", "default_cadence": "0 4 * * *", "timeout_seconds": 3600, "run_requested_at": null, "last_run": {"run_id": "01M...", "started_at": "...", "finished_at": "...", "duration_ms": 12000, "exit_code": 0, "status": "ok"}, "next_due_at": "2026-10-12T04:00:00+00:00", "stored": true}], "dispatcher_last_seen_at": "2026-10-11T02:55:00+00:00", "dispatcher_seen": true, "store_degradation": null}
```

#### `PATCH /api/v1/admin/jobs/{name}`

Edits `enabled`/`cadence`/`timeout_seconds`. 404 for a name not in
`JOB_CATALOG` — this never creates a job. Only fields present in the body
(`model_fields_set`) are changed; an empty body `{}` is a no-op that writes
nothing and emits no event. A bad cron expression or an out-of-range
timeout is 422. `cadence: null` clears it to manual-only (triggered only by
the run route below).

```bash
curl -X PATCH https://host/api/v1/admin/jobs/tune -H "Authorization: Bearer $KEY" -d '{"enabled": false, "timeout_seconds": 60}'
```

#### `POST /api/v1/admin/jobs/{name}/run`

Requests an out-of-band run ("Run now"). **Writes only `run_requested_at` —
this route never executes the job inline and never claims to.**
`is_job_due` treats a `run_requested_at` newer than the job's last completed
run as always due, so a host dispatcher polling `due-jobs` on its own
cadence is what actually picks it up from there — typically within ~5
minutes, if one is installed at all. The response's `dispatcher_hint` field
states exactly that; it is never worded as "started" or "completed".
`run_requested_at` in the response is exactly what the handler wrote to
`schedule.json` — re-read after the write, never a timestamp computed by
this route — so it is `null` in the (expected-unreachable) case the row
didn't stick.

Refuses rather than runs for two cases, on both REST and the CLI (same
handler, `trellis.mutate.schedule_handlers.JobRunRequestHandler`): a
**disabled** job ("enable it first") and a job with **no stored row yet**
(run `init-schedule` first). `job_disabled`/`not_scheduled` are the
handler's internal `rejection_reason`, visible only in the audit trail's
`MUTATION_REJECTED.reason` — neither reaches the caller as a response
`code`. REST answers 422 with a bare string `detail` (no `code` field;
`_schedule_result_http_error` reads this case as plain caller-input
rejection, same as a bad cadence). The CLI answers exit 2 with JSON
`{"code": "validation_error", ...}` (`_cli_result_failure`'s single code
for "the request you typed was bad"); the message text still names which
case it was. Neither silently writes the catalog's default cadence into
`schedule.json` just because "Run now" was clicked — a deferred run of an
LLM-spending job would otherwise be a surprise spend the operator never
asked for.

```json
{"status": "ok", "name": "tune", "run_requested_at": "2026-10-11T03:00:00+00:00", "dispatcher_hint": "Accepted. A host dispatcher polling 'due-jobs' picks this up, typically within ~5 minutes, if one is installed."}
```

#### `trellis admin schedule set` / `trellis admin schedule run`

CLI parity for the two writes above — same validation, same refusal
posture, same audit events — for an operator at a terminal rather than a
browser or the REST API.

```bash
trellis admin schedule set <name> [--enabled | --disabled] [--cadence CRON | --cadence none] [--timeout SECONDS] [--format text|json]
trellis admin schedule run <name> [--format text|json]
```

`set` requires at least one of `--enabled`/`--disabled`, `--cadence` or
`--timeout` (exits `EXIT_VALIDATION` otherwise, "no_fields") — omitting a
flag leaves that field unchanged, it is never read as "clear it". Passing
the literal `--cadence none` is how an operator clears cadence to
manual-only; there is no other way to spell `null` on a command line. Both
subcommands exit `EXIT_STORE` (5) under both formats on a degraded or stale
schedule store, matching the REST routes' 409.

#### The Schedules tab

The operator UI's **Schedules** tab (`src/trellis_api/static/index.html`)
renders `GET /admin/jobs`: cadence shown both as its raw cron expression and
as hand-formatted text (explicitly labelled **UTC** — `trellis.core.cron`
evaluates every production caller's `utc_now()`, never the dispatcher
host's local clock), an enable toggle (disabled, with its own label, while
the store reads degraded), inline cadence/timeout editing that reads the
current cadence from a data attribute rather than the rendered cell's text
(so saving with no changes round-trips instead of clobbering it with
whatever the display happened to show) with client-side bounds validation
before the PATCH, a two-step "Run now" confirmation (with an extra warning
when the job is flagged `spends_llm` or `heavy`), last-run times rendered in
UTC, a banner for a degraded schedule store (`store_degradation`), and a
dispatcher-activity banner that states only what the EventLog actually
shows rather than asserting "nothing will pick this up" — a missing poll
record is not proof the dispatcher is dead. A row with no stored
`schedule.json` entry (`stored: false`) is labelled "not scheduled, no
stored row" rather than shown as an enabled, catalog-default job. A job
that has never completed a run is due at the dispatcher's very first tick
after being scheduled (see `due-jobs` above); the next-due cell's tooltip
says so rather than implying a wait.

---

## Ingest Commands

### `trellis ingest trace`

Ingest a trace from a JSON file or stdin.

```bash
trellis ingest trace <file> [--format text|json]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `file` | No | Path to trace JSON file. Use `-` or omit for stdin. |

**From file:**

```bash
trellis ingest trace /tmp/my-trace.json --format json
```

**From stdin:**

```bash
cat <<'EOF' | trellis ingest trace - --format json
{
  "source": "agent",
  "intent": "Refactored database connection pooling",
  "steps": [
    {
      "step_type": "tool_call",
      "name": "edit_file",
      "args": {"file": "src/db/pool.py"},
      "result": {"status": "applied"},
      "duration_ms": 200
    }
  ],
  "outcome": {"status": "success", "summary": "Replaced manual connections with pool"},
  "context": {"agent_id": "code-orchestrator", "domain": "backend"}
}
EOF
```

**JSON output (success):**

```json
{"status": "ingested", "trace_id": "01JRK5N7QF8GHTM2XVZP3CWD9E", "source": "agent", "intent": "Refactored database connection pooling", "already_ingested": false}
```

A `trace_id` the store already holds is not stored again, because traces are immutable. The command still exits `0` with `status` `"ingested"`, but `already_ingested` is `true` and nothing is extracted. `source` and `intent` echo the submitted trace, not the stored one.

**JSON output (validation error):**

```json
{"status": "error", "error_type": "ValidationError", "message": "1 validation error for Trace\nsource\n  Field required [type=missing, input_value={'intent': 'Refactored da...', 'domain': 'backend'}}, input_type=dict]\n    For further information visit https://errors.pydantic.dev/2.13/v/missing"}
```

**Error cases:**
- A path that does not exist, a directory, an unreadable file, invalid JSON or a schema failure: exit code `2`, JSON `{"status": "error", "error_type": …, "message": …}`.
- A policy refusal (`deny` or `require_approval`): exit code `3`. Any other refusal: `2`. A failed write: `5`. JSON `{"status": "error", "message": …}`.
- Each case exits with the same code in both formats.

#### Trace → graph extraction (opt-in)

By default trace ingestion is write-only to the TraceStore — the trace is stored but no graph nodes/edges are created. Set the environment variable `TRELLIS_ENABLE_TRACE_EXTRACTION=1` (also accepts `true`/`yes`/`on`) to turn on a **post-ingest** deterministic extraction stage that mines the trace's structured fields into the knowledge graph through the governed `MutationExecutor`.

The flag applies identically across all three trace-ingest paths: the CLI `trellis ingest trace`, the REST `POST /api/v1/traces`, and the MCP `save_experience` tool. Extraction always runs *after* the trace is durably stored, only ever *reads* the trace (traces stay immutable), and is fully fail-soft — a broken extraction is logged and swallowed, never failing the ingest. A submission whose `trace_id` is already stored is not extracted on any of the three paths, since its fields would land on the stored trace's node: REST answers `"already_ingested": true` and `save_experience` replies `Trace already ingested: <id>`. Re-extract a stored trace with `trellis extract traces`.

What gets extracted (deterministic, structured fields only) is documented in [trace-format.md → Graph Extraction](trace-format.md#graph-extraction-opt-in). Every emitted node and edge carries property-based provenance: `source_trace_id`, `agent_id`, `extractor_tier`, and `extraction_confidence`.

A second, separately opt-in variable gates weak drafts: `TRELLIS_TRACE_EXTRACTION_MIN_CONFIDENCE=<0.0-1.0>` drops drafts scoring below the floor, plus any edge left pointing at a dropped entity. Unset (the default) means no gate — enabling extraction never also enables a silent drop. It applies to both the live hook and `trellis extract traces`, and the reported entity/edge counts are counted *after* the gate.

When the flag is on, the CLI JSON output for a trace this call stored gains an `extraction` block:

```json
{"status": "ingested", "trace_id": "01JRK5...", "source": "agent", "intent": "...", "already_ingested": false, "extraction": {"entities": 5, "edges": 4, "failed": 0, "executed": true}}
```

`entities` / `edges` count the commands *submitted*; `failed` counts those that came back `rejected` or `failed`. The batch runs `CONTINUE_ON_ERROR`, so a non-zero `failed` is not an error for the ingest — the trace is stored either way — but it does mean some drafts did not land. Persistent non-zero `failed` is worth investigating; the `trace_extraction_commands_failed` log line carries the executor messages.

### `trellis extract traces` (backfill)

Backfill the graph from traces that were already ingested before the flag was enabled (or that need re-extraction). Iterates the TraceStore and runs the same `TraceExtractor` + governed-batch path the live hook uses.

```bash
trellis extract traces [--since <days>] [--domain <name>] [--limit <n>] [--dry-run] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--since` | `7` | Backfill traces ingested within the last N days. |
| `--domain` | (none) | Optional `TraceContext.domain` filter. |
| `--limit` | `1000` | Max traces to scan. |
| `--dry-run` | off | Tally and print per-trace draft counts without executing the mutation batch. |
| `--format` | `text` | `text` or `json`. |

This command does **not** require the `TRELLIS_ENABLE_TRACE_EXTRACTION` flag — it is the explicit, operator-driven backfill path. `--dry-run` previews the graph a real run would create without writing anything.

**JSON output:**

```json
{"status": "backfilled", "traces_scanned": 12, "total_entities": 58, "total_edges": 44, "succeeded": 102, "failed": 0, "rejected": 0, "duplicates": 0, "dry_run": false, "per_trace": [{"trace_id": "01JRK5...", "domain": "backend", "entities": 5, "edges": 4}]}
```

`total_entities`, `total_edges` and the `per_trace` counts are drafts, the commands submitted; text output labels them `Drafts`. `succeeded`, `failed`, `rejected` and `duplicates` count what the executor answered, one key per status as `POST /api/v1/extract/drafts` reports them. So `failed` here is store failures alone, where the ingest hook's `failed` above sums both. A dry run executes nothing and omits the four keys. When every command is refused or fails, `status` is `"error"` with the first failure's sanitized `message`, and the command exits by that result: `3` for a policy refusal, `2` for any other refusal, `5` for a failure. A run that wrote anything stays `"backfilled"` and exits `0`, as `trellis ingest dbt-manifest` does; text output then names the failure count and the first failure's message, and JSON carries that message, sanitized, as `message`.

#### Document → vector embedding (opt-in)

By default document ingestion is write-only to the DocumentStore — the document is retrievable by keyword FTS but invisible to `SemanticSearch`, because nothing embeds it. Set `TRELLIS_ENABLE_EMBED_ON_INGEST=1` (also accepts `true`/`yes`/`on`) to turn on a **post-ingest** embedding stage that runs the stored content through the registry's configured `embedding_fn` and upserts a vector keyed by the document's `doc_id`.

The flag applies identically across every document-ingest path: the corpus / conversation / session-capture seam (parents *and* chunks), MCP `save_memory`, `trellis ingest dbt-manifest` (#568), and the governed `evidence.ingest` command — which is what the REST `POST /api/v1/documents`, the REST `POST /api/v1/evidence` and the evidence document MCP `save_knowledge` creates all submit. That is the same set of surfaces `TRELLIS_ENABLE_CLASSIFY_ON_INGEST` covers, and the one document-writing ingest command outside both is `trellis ingest evidence` (see "Document → content tags" below). The two flags are nonetheless read independently, so a deployment can run either, both or neither. It requires an `embeddings:` block in `config.yaml` (or `TRELLIS_EMBEDDING_FN`) *and* a configured vector store — when either is missing the hook logs a warning and no-ops. Embedding always runs *after* the document is durably stored and is fully fail-soft: a broken or unreachable embedder is logged and swallowed, never failing the ingest.

The vector row's metadata carries a `content` excerpt (500 chars — what `SemanticSearch` renders as the pack excerpt), the document metadata, `doc_id`, and a `created_at` recency stamp.

**That metadata is a snapshot taken at embed time.** A metadata-only re-put to the document store does not re-embed, so anything written to a document *after* it was embedded is invisible to `SemanticSearch` unless something mirrors it across. Two writers now do: retention's archive/restore mirrors the `lifecycle` stamp (#337), and the classify layer — `apply_noise_tags` and `reclassify_item` — mirrors `content_tags` / `auto_importance` (#338), both through `trellis.core.vector_metadata.sync_vector_metadata`, a metadata-only re-upsert that re-embeds nothing. Rows that diverged **before** those writers existed are repaired by `trellis admin resync-vector-metadata` (below), not by `reindex-vectors --force`, which would pay one embedding call per document to fix metadata already sitting in the document store for free.

That stored excerpt is produced by `truncate_excerpt` **at embed time**, not at retrieval time (#310): `build_vector_row` is the last point on the semantic path that still holds the full document, so it is the only point where the cut can break on a boundary and can name how much it dropped (`… [+12.4k chars]`). Retrieval re-runs `truncate_excerpt` over the stored string, which is a no-op for rows this hook wrote. Rows embedded before #310 hold a raw `content[:500]` slice and keep their mid-word cut until re-embedded — `trellis admin reindex-vectors --force` rewrites them through the same shared builder.

On the retrieval side, embedded documents are visible to every `SemanticSearch`/`PackBuilder` consumer. Since #262 the MCP `get_context` and `search` macro tools route through `PackBuilder`, which fuses the keyword, graph and **semantic** axes with Reciprocal Rank Fusion (deduplicated by `item_id`): when the embedder + vector store pair is configured, the query is embedded and vector hits join the fusion. The semantic axis is additive and degrades gracefully — a down embedder means keyword + graph results only, never a failed tool call. A single row whose metadata cannot be read is skipped, on all three axes (a vector row on the semantic axis, a document on the keyword axis, a node on the graph axis), with a `semantic_row_skipped_malformed_metadata` / `keyword_row_skipped_malformed_metadata` / `graph_row_skipped_malformed_metadata` warning naming the item id and field, never the value, and the rest of the axis is still served. A row is malformed when its `auto_importance` is not a number (a non-numeric string, a list, `null`, or a `bool`), is not finite (`nan`, `inf`, `"1e400"`), or is above the decay threshold with an `importance_scored_at` that is missing or is not a timestamp (`"garbage"`, `""`, `0`); on the semantic axis also when `content` or `excerpt` is not a string. A numeric string such as `"0.9"` is not malformed and is coerced. What stays loud, failing the axis into `strategy_failures`: a registry importance parameter that is not a number, and a store score or rank that is not a number. The skip is also recorded: a flat pack's `rejected_items` carries the row with `reason: malformed_metadata` (`strategy_source` the axis; `item_type` `vector`, `document` or `entity`), the Withheld note says `malformed_metadata N`, `PACK_ASSEMBLED.malformed_row_skips` counts it per strategy, and `trellis analyze health` turns `warn` while a pack in the window skipped one. A row whose successor was skipped does not lose a supersession verdict to it: the loser is still served.

#### `save_memory` fuzzy dedup — seeding the MinHash index (opt-in)

`save_memory` dedups in two stages: an exact content-hash match, then a
MinHash/LSH near-duplicate check that catches typos, casing and punctuation
variants. **Stage 2 only sees documents the index holds**, and by default the
index is seeded with nothing — so it holds only the memories written by that
same server process. A fresh MCP server cannot detect a fuzzy duplicate of
anything already in your store. Before #402 that was a defect (the seed read
`search("")`, which returns zero rows on every backend); it is now the
documented default, with a switch.

`TRELLIS_MINHASH_SEED_MAX_DOCS=<n>` loads the newest `n` whole documents into
the index the first time it is built. Chunk rows are excluded — a chunk is not
in fact a near-duplicate of its parent (measured over the reference
deployment's 740 chunk rows: median Jaccard 0.294, max 0.641, zero pairs over
the 0.85 threshold), but a `Fuzzy duplicate: <parent>#chunk-3` verdict names a
fragment when the whole document is in the index too.

The number is both the switch and the bound, because the cost is the point:

- Seeding is O(corpus). A MinHash signature costs roughly **32 ms per
  document**, so a 735-document corpus takes about **24 seconds of blocking
  CPU** on the first `save_memory` of a process, growing linearly. Under the
  `stdio` transport that is once per session, in every repository the server
  is wired into. Under `http` the prewarm pays it once at boot.
- What it buys, measured on the same deployment over eight weeks: a complete
  seed would have rejected **13 of 735** whole-document writes (1.8%), every
  one verified a genuine near-duplicate. Roughly 1.6 rejections a week.
- Newest-first ordering means a bound below your corpus size misses *old*
  duplicates. On that corpus a bound of 500 covers 68% of it and misses 8 of
  its 17 near-duplicate pairs.

Unset, `0`, negative and unparseable all mean **seed nothing** — a typo must
not switch on a rejection path, nor an unbounded walk. `trellis admin
write-config --format json` reports the effective value; the server logs
`minhash_index_initialized` at INFO with `size`, `rows_read` and `seconds`,
warns `minhash_index_seed_disabled` when a stocked store is running unseeded,
and warns `minhash_index_seed_empty` when a seed read rows but indexed none.

The same index backs the reconcile-on-write verdict tier
(`TRELLIS_ENABLE_RECONCILE_ON_WRITE`), which adjudicates near — not exact —
matches with a model. That tier is equally blind across process boundaries
without a seed.

### `trellis admin reindex-vectors` (backfill)

Backfill embeddings for documents that were ingested before the flag was enabled (or that need re-embedding). Pages through the DocumentStore and builds the same vector rows the live hook writes.

```bash
trellis admin reindex-vectors [--batch-size <n>] [--limit <n>] [--force] [--dry-run] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--batch-size` | `100` | Documents per page / vectors per bulk upsert. |
| `--limit` | `0` (all) | Stop after scanning this many documents. |
| `--force` | off | Re-embed documents that already have a vector. |
| `--dry-run` | off | Count what would be embedded without calling the embedder. |
| `--format` | `text` | `text` or `json`. |

This command does **not** require the `TRELLIS_ENABLE_EMBED_ON_INGEST` flag — it is the explicit, operator-driven backfill path. It **does** require the embedder and vector store to be configured, and exits non-zero when they are not. Content-less documents are skipped; per-document embed failures are counted and logged, never fatal. Rerunning is always safe (rows are keyed by `doc_id`).

**JSON output:**

```json
{"status": "ok", "scanned": 240, "embedded": 198, "skipped_existing": 30, "skipped_empty": 12, "errors": 0, "dry_run": false}
```

### `trellis admin resync-vector-metadata` (divergence repair)

Re-sync stale vector-row metadata from the document store. Pages the DocumentStore, compares each document against its vector row on `content_tags` / `auto_importance`, and metadata-only re-upserts the ones that disagree — **calling no embedder and needing none configured**.

```bash
trellis admin resync-vector-metadata [--batch-size <n>] [--limit <n>] [--dry-run] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--batch-size` | `500` | Documents per `list_documents` page. |
| `--limit` | `0` (all) | Stop after scanning this many documents. |
| `--dry-run` | off | Count divergent rows without rewriting any. |
| `--format` | `text` | `text` or `json`. |

This is the one-time repair for rows written before the write-through shipped (#338 measured 45 noise-tagged documents in production, none of whose vector rows agreed — so the default `signal_quality="noise"` exclusion could not hold for any of them on the semantic axis). Documents with no vector row are counted under `no_vector_row`, not as errors — embed-on-ingest is opt-in. Per-row failures are counted and logged, never fatal.

It is **idempotent**: a row already in agreement is not rewritten, so a second run reports `repaired: 0`. That makes it safe to schedule and turns "is the write-through holding?" into a question with an answer — a steady-state run reporting non-zero means some writer is still bypassing it.

**JSON output:**

```json
{"status": "ok", "scanned": 1019, "divergent": 45, "repaired": 45, "no_vector_row": 109, "errors": 0, "keys": ["content_tags", "auto_importance"], "dry_run": false}
```

### `trellis admin backfill-name-aliases`

Bind normalized entity display names into the indexed `name` alias namespace for
nodes created before write-time binding shipped.

```bash
trellis admin backfill-name-aliases [--max-nodes <n>] [--format text|json]
```

The default bound is the current node count plus one. The command reads one row
beyond the bound before writing; if that extra row exists, it binds nothing and
exits with validation code `2`. Rerun with a larger bound to resume. Unique
normalized names are idempotent, exact duplicate names are reported as
contested, and blank names are skipped.

Every binding is an `alias.upsert` command with `if_absent=true`, so validation,
policy, idempotency, execution, and audit emission run for each bounded batch.
Concurrent contenders are serialized by `GraphStore.bind_alias_if_absent`;
losers report a live existing winner and never replace it. If that winner's
current node no longer has the normalized name, the same atomic operation closes
the stale alias version and rebinds it; the report counts this as `rebound`.
Policy denials exit `3`; lookup, query, or alias-store failures are reported
separately from genuine skips and exit `5`. JSON failure details identify only
the stage, reason, and entity id — they never echo entity names.

Entity create/update binds the final merged name inside its already-governed
command. An explicitly supplied name is checked even when it equals the stored
value, so retrying after a transient alias failure repairs the index. Alias
maintenance remains fail-soft for that primary entity write; the backfill is
strict and never reports `status: ok` when alias maintenance fails.

#### Document → content tags (opt-in)

By default document ingestion writes no retrieval-shaping tags — the document is retrievable, but `PackBuilder`'s `tag_filters`, the `signal_quality="noise"` exclusion and the tag-derived importance boost have nothing to work with. Set `TRELLIS_ENABLE_CLASSIFY_ON_INGEST=1` (also accepts `true`/`yes`/`on`) to turn on **classify-on-write**: an inline, deterministic pass (structural + keyword-domain + source-system classifiers, no LLM, microseconds) that stamps `metadata.content_tags` and `metadata.auto_importance` as the document is written.

One flag, six write seams:

| Surface | Seam |
|---------|------|
| `trellis ingest corpus`, `trellis ingest conversations`, the session-capture sweep | `ingest_corpus.sync.sync_records` — parents *and* chunks (the chunk is the retrievable unit, so it inherits the parent's tags) |
| MCP `save_memory` | after both dedup stages, before the put — including every reconcile-on-write verdict that stores a document |
| MCP `save_knowledge` | the evidence document it auto-creates (`mutate.evidence.ensure_evidence_document`) |
| REST `POST /api/v1/documents` | before the store put |
| REST `POST /api/v1/evidence` | before the store put |
| `trellis ingest dbt-manifest` | each model/source description, before the store put (#590) — one call per description, so it takes the single-document seam's guarantees rather than the bulk one's |

The five single-document seams call one helper, `trellis.classify.ingest.classify_metadata_on_write`, so the persisted tag shape does not drift: flag-gated (off returns the caller's metadata untouched), fill-if-absent (existing `content_tags` — an earlier write, or the LLM enrichment pass — are never clobbered), fail-soft on *any* error including a `metadata` that is not a mapping, no auto-`domain` (see below), and a no-op on empty or whitespace-only content, matching the embed-on-ingest skip. The bulk seam calls the same `classify_for_ingest` core inline and shares the flag gate, fill-if-absent and no-auto-`domain`; it has neither the empty-content skip nor the non-mapping guard, because it classifies against metadata already merged with the stored document's.

**Not covered.** `trellis ingest evidence` puts its document straight to the store with no classify hook and no embed, so it stays untagged and keyword-only whatever either flag says. Note the asymmetry with its own REST twin: `POST /api/v1/evidence` submits the governed `evidence.ingest` command and is covered by both flags, while the CLI command of the same name bypasses it. `trellis classify backfill` and `trellis admin reindex-vectors` are the fixes for it, exactly as for pre-flag rows. (`trellis ingest dbt-manifest` was in this paragraph until #590 tagged it and #568 routed it through the embed hook.)

One deliberate omission: the classifier-derived **`domain` facet is dropped** before persisting. `domain` is the only facet that *hard-excludes* a document from a domain-scoped query on mismatch, and the deterministic keyword / source-system classifiers will confidently assign a code-flavoured domain to personal content. The operator-set scalar `metadata['domain']` (the `--domain` flag) is a separate key and is untouched. Every facet that *is* persisted only shapes ranking, sectioning, or noise exclusion — a wrong value degrades ranking at worst, it never hides content.

The drop is persisted as `content_tags.domain: []`, not as a missing key — so **an empty list facet default-passes a tag filter exactly like an absent one** (both document stores, since #282). This is load-bearing rather than incidental: the stores originally default-passed only a `NULL` facet, so `[]` read as a value and every classify-on-write-tagged document was hard-excluded from every domain-scoped query. Turning tagging on would have hidden content. The rule now is *empty carries no value, and no value cannot exclude*; it also repairs rows already written with `domain: []` without a backfill. See [schemas.md → ContentTags](schemas.md#contenttags).

### `trellis classify backfill`

Backfill tags for documents ingested before the flag was enabled, and refresh tags that have drifted (the keyword vocabulary grew, the graph around a document changed). Pages the DocumentStore and re-runs the deterministic tagging pipeline over every item whose `content_tags.classified_at` is missing or older than `--max-age-days`, through the same `reclassify_item` core the programmatic path uses.

```bash
trellis classify backfill [--max-age-days <n>] [--limit <n>] [--page-size <n>] [--include-domain] [--dry-run] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--max-age-days` | `30` | Re-tag items whose tags are older than this. `0` re-tags every scanned item. |
| `--limit` | `0` (all) | Stop after scanning this many documents. |
| `--page-size` | `100` | Documents fetched per store round-trip. |
| `--include-domain` | off | **Dangerous** — let the classifiers (re)assign the hard-excluding `domain` facet. See below. |
| `--dry-run` | off | Report what would change without writing tags or emitting events. |
| `--format` | `text` | `text` or `json`. |

Tag writes pass `preserve_updated_at=True`: re-tagging is derived metadata and does not make a document's information fresher, while `updated_at` drives the recency decay in `retrieve.strategies`. Without it a first backfill over an untagged corpus would rewrite every row and flatten recency ordering across the whole store.

Like the two backfills above, this command does **not** require the `TRELLIS_ENABLE_CLASSIFY_ON_INGEST` flag — invoking it *is* the opt-in. It never deletes tags: an item the pipeline produces no signal for keeps whatever it had, and is counted under `skipped_no_signal`. Each write emits a `TAGS_REFRESHED` event carrying the before/after tag diff; a dry run emits nothing.

Every scanned document lands in exactly one bucket: `refreshed`, `skipped_fresh` (stamp newer than `--max-age-days`), `skipped_unchanged` (stale stamp, but re-running the pipeline produces the same tags — so nothing is written and no event is emitted), `skipped_no_signal`, `skipped_missing_content`, `skipped_precedent` (a promoted precedent's row, whose lifecycle and tags belong to the precedent handlers), or `errors`. That means **`--dry-run` previews what would *change*, not what is merely stale**, and the two runs agree. An unchanged document keeps its old `classified_at` and is re-scanned (never rewritten) by the next backfill.

The scan is fail-soft per document: a row that fails to process — a hand-edited `auto_importance`, a `content_tags` value of the wrong shape — is logged with a traceback, counted in `errors`, and skipped; the rest of the store is still backfilled. `status` is then `"partial"` rather than `"ok"`. The exit code stays `0` (matching `admin reindex-vectors`) — read `errors` / `status`, not the exit code, to detect a partial run. A malformed `classify:` block in `config.yaml` is a different failure: the command exits `1` with `{"status": "error", ...}` before scanning anything.

`--include-domain` re-enables the facet classify-on-write deliberately drops, with the same hazard: a deterministic keyword match that assigns the wrong `domain` will hide the document from domain-scoped retrieval rather than merely mis-rank it. Leave it off for a backfill over mixed content; use it only with a vocabulary you trust (`classify.domain_keywords` in `config.yaml`) or an enrichment-mode pipeline.

Two things to know about how this differs from classify-on-write:

- **The backfill honours `classify.domain_keywords`; classify-on-write does not.** The write path uses built-in defaults because it drops the `domain` facet anyway. Here a keyword hit still contributes `retrieval_affinity`, adds the classifier to `classified_by`, and raises that classifier's confidence (which drives per-facet merge precedence) — so a backfilled document can carry slightly different tags than the same document tagged at ingest, even with `--include-domain` off. That is intentional: a backfill is an explicit operator action, and reading the operator's configured vocabulary is the point of writing it.
- **Tag writes are a sanctioned exception to the governed-mutation pipeline.** Both this command and classify-on-write write `metadata.content_tags` / `metadata.auto_importance` straight to the DocumentStore and emit `TAGS_REFRESHED` by hand rather than routing through `MutationExecutor`. A refresh rewrites derived metadata on an existing row — no entity created, no content changed, fully reconstructible by re-running the pipeline — while per-row validate/policy/idempotency stages are uneconomical at whole-store scale. The `TAGS_REFRESHED` event preserves the audit trail. See the module docstring of `trellis/classify/refresh.py`.

**JSON output:**

```json
{"status": "ok", "scanned": 240, "refreshed": 31, "skipped_fresh": 200, "skipped_unchanged": 0, "skipped_no_signal": 6, "skipped_missing_content": 3, "skipped_precedent": 0, "errors": 0, "dry_run": false, "include_domain": false, "item_ids_refreshed": ["doc-1", "doc-2"]}
```

### `trellis classify shadow`

Record what an LLM says about stored documents **without serving it** — the precondition for teaching the deterministic classifier a vocabulary it has never observed ([#321](https://github.com/ronsse/trellis-ai/issues/321) Phase 1).

```bash
trellis classify shadow [--limit <n>] [--max-age-days <n>] [--page-size <n>] [--model-id <label>] [--dry-run] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--limit` | `0` (all) | Stop after scanning this many documents. |
| `--max-age-days` | `-1` | Re-judge shadow records older than this. The default judges only documents with **no** shadow record — each one costs a model call. |
| `--page-size` | `100` | Documents fetched per store round-trip. |
| `--model-id` | `""` | Label recorded on each record and event (e.g. `hermes3:8b`). Defaults to the classifier's name. |
| `--dry-run` | off | Compute records without writing them or emitting events. |

Requires an `llm:` block in `config.yaml`; without one the command exits `1` naming what is missing rather than silently shadowing nothing. A **local** model is the intended configuration — the pass is ~1.6 s per document against a whole corpus, which is free and private on-host and expensive anywhere else.

**Why a separate key.** Verdicts are written to `metadata.content_tags_shadow`, never to `content_tags`. Two things follow, and both are the point:

- **Retrieval does not move.** No pack ranking changes while the corpus accrues, so this is safe to run against a production store. The guarantee is enforced in two independent places: tag filters address the JSON path `$.content_tags.<facet>`, so a sibling top-level key is structurally unaddressable; and `trellis.retrieve.servable.strip_non_servable` runs where `PackBuilder` collects every strategy's results, so it covers strategies added later and out of tree, not just the built-ins. Note the scope of that claim — shadow tags never reach a *pack*; they are not confidential. `GET /api/v1/documents` returns them to a read-scoped caller, correctly, since that is the same access path as the content they describe.
- **It avoids destroying data on a vocabulary collision.** The deterministic path emits `ContentType` values (`error-resolution` / `procedure` / `code`); the enrichment path emits `DEFAULT_CLASSIFICATIONS` (`reference` / `research` / `notes` / …). The two overlap in exactly one value, `documentation`. Writing LLM output into `content_tags` would replace one taxonomy with another — and would in fact raise `ValidationError`, since `ContentTags.content_type` is a closed `Literal`. The shadow record stores the LLM's vocabulary verbatim, because the disagreement is the measurement Phase 2 mines.

Each judged document also emits a leak-safe `MEMORY_OP_JUDGED` event ([#264](https://github.com/ronsse/trellis-ai/issues/264)'s substrate — this is its classify-layer instance, not a parallel channel): digest, `content_type` verdict label, confidence, and a subject pointer. The open-vocabulary `domain` tags stay **off** the event — a value like `yellowstone-national-park` reveals subject matter, and the event log has a different access/retention profile than the doc store. A document that produces no tags still emits a verdict of `unclassified`: "the model looked and produced nothing" is the coverage signal the pass exists to measure.

This is a batch pass only, never an ingest hook. Classify-on-write is deterministic and inline for a reason (microseconds, no network); a model call there would spend the write path's latency budget on a measurement. Documents written since the last pass are covered by re-running it.

**JSON output:**

```json
{"status": "ok", "scanned": 999, "written": 964, "skipped_fresh": 0, "skipped_no_signal": 31, "skipped_missing_content": 4, "skipped_redacted": 0, "errors": 0, "stale_snapshot": 0, "dry_run": false, "item_ids_written": ["doc-1", "doc-2"]}
```

`stale_snapshot` ([#421](https://github.com/ronsse/trellis-ai/issues/421)) counts documents another writer changed while the model was judging them. The model call sits between the row's read and its write, so the write re-reads and overlays only the shadow key — the record still lands, on the current content, and every live tag written in the meantime survives. It is a **subset of `written`**, not a failure bucket, so it does not move `status`. A row deleted inside that window is not re-inserted and is counted in `errors`.

`skipped_redacted` counts documents redacted inside the same window. The verdict was derived from text that no longer exists, so it is dropped rather than written onto the blank tombstone: no shadow record, no `MEMORY_OP_JUDGED` event, and not an `error`, so `status` and the exit code do not move. Every scanned document still lands in exactly one bucket. The text summary prints a line for it only when it is non-zero.

### `trellis classify shadow-report`

Compare shadow tags against live tags, per facet and per document. Read-only.

```bash
trellis classify shadow-report [--limit <n>] [--page-size <n>] [--per-document] [--format text|json]
```

Answers the two questions that decide whether a shadow corpus is worth promoting from: where does the LLM produce a facet the deterministic pipeline leaves empty (`live_missing` — the coverage gain), and where do the two disagree outright (`disagreed`).

`agreement_rate` is `null` when nothing is comparable, rather than `0.0` or `1.0`. A rate over an empty denominator is not a measurement, and reporting one as a number is how a metric ends up wired to a constant.

An empty `domain` list counts as **absent**, not as a value. Every document classify-on-write tags stores `domain: []` deliberately, so counting it as "live said something different" would report near-total disagreement on the one facet where the live side has, by design, said nothing.

`out_of_vocabulary_content_types` counts shadow `content_type` values outside the live `ContentType` vocabulary — the collision above, made countable. A non-empty map means promoting shadow `content_type` wholesale would mean *adopting a different taxonomy*, not refining the current one.

**JSON output:**

```json
{"status": "ok", "scanned": 999, "with_shadow": 964, "facets": {"content_type": {"agreed": 88, "disagreed": 42, "live_missing": 780, "shadow_missing": 12, "both_missing": 42, "agreement_rate": 0.677}}, "out_of_vocabulary_content_types": {"reference": 310, "notes": 204}}
```

### `trellis classify tag-candidates`

Mine the shadow corpus for keyword rules the deterministic classifier could own, so the LLM can be switched off for what has been learned ([#321](https://github.com/ronsse/trellis-ai/issues/321) Phase 2) — "documents containing `todoist` were tagged `task-management` in 40 of 41 cases; propose that keyword".

```bash
trellis classify tag-candidates [--facet <name>] [--limit <n>] [--emit/--no-emit] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--facet` | `domain` | Shadow facet to mine. `domain` is the only facet with a config write target. |
| `--limit` | `50000` | Cap on documents scanned. |
| `--emit / --no-emit` | emit | Emit a `TAG_KEYWORD_CANDIDATE` event per candidate. `--no-emit` is a dry run and does not advance the cooldown. |

**Surface-only, never auto-applied.** The command proposes; it never writes `config.yaml`. The text output ends with a ready `trellis classify apply-rule tag_keyword <keyword> <tag> --source-event-id <event-id>` line per candidate — not a `config.yaml` fragment to hand-paste. The event id is looked up via `GET /api/v1/classify/candidates` (or the Tag Rules UI tab); promoting (or undoing) then goes through the governed Apply/Revoke review below. That is not a missing feature: `domain` is the one facet that *hard-excludes* a document from a domain-scoped query, so a wrong promoted keyword **hides** content rather than merely mis-ranking it (the #282 failure mode). A human approves every domain promotion.

Thresholds come from the `ParameterRegistry` under component `learning.tag_evolution`. The **analyzer** never substitutes a default — a missing key raises, naming it and its recommended seed. The **command** seeds the recommended values for that run and warns on stderr, matching `trellis analyze schema-evolution`; nothing seeds this component automatically, so hard-failing would mean failing on every deployment. Seed them for real (making runs reproducible) from `trellis.learning.tag_evolution.RECOMMENDED_SEED_VALUES`:

| Key | Seed | Gate |
|-----|------|------|
| `tag_keyword_min_support` | `30` | Documents containing the keyword **and** carrying the tag. |
| `tag_keyword_min_precision` | `0.75` | `support / keyword_documents` — how reliably the keyword predicts. |
| `tag_keyword_min_lift` | `0.25` | Minimum `lift - 1` over the tag's base rate. |
| `tag_keyword_min_corpus` | `30` | Shadow-corpus floor; below it, nothing is surfaced at all. |
| `tag_keyword_cooldown_days` | `7` | Days before an unchanged candidate re-surfaces. |

`min_support` and `min_lift` follow the precedent in `learning/tuners/auto_promote.py` (30 samples / 0.25 effect) rather than inventing a second statistical convention.

An out-of-range threshold (`min_precision` outside `(0, 1]`, a negative `min_lift`) or a malformed `classify.domain_keywords` block exits `1` with the message, not a traceback.

**Lift is the load-bearing gate.** In a corpus where one tag is on most documents, *every* keyword has high precision for it — precision alone would surface the whole vocabulary as "perfectly predictive". Lift measures how much better than the tag's own base rate the keyword does, so a keyword that only matches the base rate dies.

The analyzer shares the four constraints of the well-known promotion loop it is modelled on: read-only against the corpus (the only write is the event), thresholds-or-raise, idempotent across runs (a candidate re-surfaces only on ≥20% support growth or an elapsed cooldown — shared with `schema_evolution` via `trellis.learning.cooldown`), and it **filters its own writes** — a keyword the live classifier already owns is excluded before counting, so a promoted keyword can neither be re-proposed nor read its own prior promotion as fresh evidence.

Two properties of `KeywordDomainClassifier` shape the safety story. It matches keywords as *substrings* while the analyzer mines whole *tokens*, so a mined keyword always fires (the mining is strictly more conservative than the matching). And a domain needs **two** distinct keyword hits before it is assigned — so promoting a single keyword cannot by itself make the classifier assign a domain, and one bad promotion cannot hide a document on its own.

**What this does not measure.** Promotion is gated on *agreement with the LLM*, not on retrieval outcome. Agreement shows the deterministic rule imitates the model; it does not show either was useful. Closing that requires joining promoted tags back to pack feedback — the same attribution machinery `learning/pack_observations.py` runs. Until that join exists this is a distillation step with a human gate, not a closed learning loop, and every candidate's `notes` say so.

**JSON output:**

```json
{"status": "ok", "facet": "domain", "emitted": true, "candidates": [{"candidate_id": "3f2a...", "facet": "domain", "keyword": "todoist", "tag": "task-management", "support": 40, "keyword_documents": 41, "tag_documents": 44, "corpus_documents": 964, "precision": 0.976, "recall": 0.909, "lift": 21.4, "has_write_target": true, "example_item_ids": ["doc-1"], "recurrence_count": 0, "notes": ["gated on agreement with the LLM, not on retrieval outcome — this shows the rule imitates the model, not that either helped", "domain hard-excludes on mismatch: a wrong keyword hides documents from domain-scoped queries rather than re-ranking them — human approval required"]}], "domain_keywords_fragment": {"task-management": ["todoist"]}}
```

### Classify rule review — Apply / Revoke 

`tag-candidates` / `domain-candidates` (above) only *propose*: their text
output ends with a ready `apply-rule` invocation per candidate rather than a
`config.yaml` fragment to hand-paste (that path had no audit trail). This
section is the governed "approve" step that sits on top of mining — an
**operator overlay**, not a third mining command.

**Precedence.** `trellis.classify.factory.effective_domain_keywords()` /
`effective_domain_aliases()` merge three layers, lowest to highest: built-in
defaults < `config.yaml` < `classify_overrides.json` (`<data_dir>/stores/`,
a `DegradableJsonStore` subclass — reads degrade row-by-row, writes refuse
whole, same posture as `PolicyStore` / `AdvisoryStore`). An applied row
always wins over the same key in `config.yaml`: an operator approving a
mined candidate through this path is a more specific, more recent decision
than whatever is sitting in a host-local file Trellis never writes to
itself.

**Governed, not a direct write.** Apply and revoke route through
`Operation.CLASSIFY_RULE_APPLY` / `CLASSIFY_RULE_REVOKE`, via a dedicated
admin-only executor (`build_classify_rules_executor`) rather than the
general curate executor — this changes what the classifier does to every
future document, not one piece of content. Both the CLI and the admin-scoped
REST route submit the same `Command`, so a denying policy, a degraded
overlay store, or a malformed candidate is refused identically on either
surface (Stage 2 always runs, CLAUDE.md).

```bash
trellis classify apply-rule <kind> <key> <target> --source-event-id <id> \
    [--support <n>] [--precision <f>] [--lift <f>] [--by <who>] [--format text|json]
trellis classify revoke-rule <kind> <key> [--by <who>] [--format text|json]
trellis classify effective-rules [--format text|json]
```

| Argument / option | Description |
|---|---|
| `kind` | `tag_keyword` or `domain_alias`. |
| `key` | Keyword (`tag_keyword`) or alias (`domain_alias`) to apply/revoke. |
| `target` (apply only) | Domain (`tag_keyword`) or canonical tag (`domain_alias`). |
| `--source-event-id` (apply, required) | The `TAG_KEYWORD_CANDIDATE` / `DOMAIN_ALIAS_CANDIDATE` event this rule was mined from. `tag-candidates` / `domain-candidates` print a `candidate_id`, not this id — read it from `GET /api/v1/classify/candidates`'s `source_event_id` field for the same candidate. Kept so the applied row traces back to the event that proposed it, without a reviewer having to join back to it by hand. |
| `--support` / `--precision` / `--lift` (apply) | The candidate's own mining metrics, carried onto the audit event. |

`revoke-rule` is **refused, not a no-op**, when `(kind, key)` has no overlay
row — including when the effective rule instead comes from `config.yaml`:
that row was never written by this store and cannot be un-applied through
it. `effective-rules` is read-only parity for `GET /api/v1/classify/rules`
(see below): both call `effective_classify_rule_rows()`, badging every
effective keyword/alias row by `source` (`default`, `config`, `overlay`).

**REST** (`src/trellis_api/routes/classify_rules.py`; admin scope
(`SCOPE_ADMIN`) on every write and on the candidate listing, read scope on `GET /classify/rules`, mirroring
`policies.py`'s mixed-scope mounting):

| Method & path | Scope | Description |
|---|---|---|
| `GET /classify/candidates` | admin | List mined candidates (`?limit=`). No `status` filter server-side — filter client-side on `status == "open"`. Admin because the keyword and alias text is mined from every document, hidden ones included, and carries no document ids to filter a read key by; a read key gets `403`. |
| `POST /classify/candidates/mine` | admin | Run the tag-keyword / domain-alias miners inline and emit candidate events — reuses the same analyzer functions `tag-candidates` / `domain-candidates` call, not a reimplementation. Body: `{"kinds": ["tag_keyword", "domain_alias"], "scan_limit": null, "use_recommended_defaults": true}`. |
| `POST /classify/candidates/{candidate_id}/dismiss` | admin | Mark one candidate dismissed (no body). `404` if no such candidate. |
| `POST /classify/rules` | admin | Apply one candidate. Body is `ClassifyRuleApplyRequest`: `kind`, `key`, `target`, `source_event_id` (required — the candidate's `source_event_id`, **not** its `candidate_id`), optional `support`/`precision`/`lift`. |
| `DELETE /classify/rules/{kind}/{key}` | admin | Revoke one applied (overlay-sourced) rule. |
| `GET /classify/rules` | read | Every effective rule, badged by `source`. |

**JSON shapes:**

```json
{"status": "ok", "tag_keyword_count": 2, "domain_alias_count": 0, "used_recommended_defaults": [], "candidates": [{"candidate_id": "3f2a...", "kind": "tag_keyword", "key": "todoist", "target": "task-management", "status": "open", "recurrence_count": 0, "example_count": 3, "notes": [], "metrics": {"support": 40, "precision": 0.976, "lift": 21.4}, "emitted_at": "2026-10-11T00:00:00Z", "source_event_id": "evt-9c1..."}]}
```

```json
{"count": 2, "rules": [{"kind": "tag_keyword", "key": "todoist", "target": "task-management", "source": "overlay", "applied_at": "2026-10-11T00:00:00Z", "applied_by": "cli:alice", "source_event_id": "evt-9c1..."}]}
```

**UI:** the "Tag rules" tab (`src/trellis_api/static/index.html`) lists open
candidates with Apply/Dismiss actions and the effective-rule table with
Revoke restricted to `source === "overlay"` rows — the same refusal the
`DELETE` route enforces, surfaced before the request rather than only after
it fails.

### `trellis ingest evidence`

Ingest evidence from a JSON file.

```bash
trellis ingest evidence <file> [--reingest-redacted] [--format text|json]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `file` | **Yes** | Path to evidence JSON file |
| `--reingest-redacted` | No | Write over an `evidence_id` that was redacted. Without it a write to a redacted id exits `2` (`redaction_tombstone_overwrite`, in text and JSON alike) and the row stays blank; with it the row is written and stamped `reingested_after_redaction` (see `trellis curate redact`) |

An `evidence_id` starting `trace-summary:` exits `2` (`reserved_trace_summary_id`, in text and JSON alike) and nothing is written: that prefix belongs to the trace embed worker.

**Example:**

```bash
cat <<'EOF' > /tmp/evidence.json
{
  "evidence_type": "snippet",
  "content": "The connection pool should use a max of 20 connections per process.",
  "source_origin": "manual",
  "uri": "https://wiki.internal/db-guidelines"
}
EOF

trellis ingest evidence /tmp/evidence.json --format json
```

**JSON output (success):**

```json
{"status": "ingested", "evidence_id": "01JRK6M3QF8GHTM2XVZP3CWD9E", "evidence_type": "snippet"}
```

A path that does not exist, a directory, an unreadable file, invalid JSON or a schema failure exits `2`, with the same JSON shape as `ingest trace`'s.

### `trellis ingest corpus`

Sync a directory of `.md`/`.markdown` files (e.g. a notes vault) into
the document store, idempotently. Any other file is reported as
`skipped_unsupported`, never converted — format conversion (PDF, audio,
per-tool exports) is the client's pre-step, per the normalization
boundary in [`adr-corpus-ingestion.md`](../design/adr-corpus-ingestion.md) §8.
Some paths never reach that report at all: paths `--include` does not
match, and dot-files, dot-directories (tool state such as `.obsidian/`)
and symlinked directories below the root (never followed). A directory
that cannot be read, the root included, is skipped and reported as an
`unreadable_directory` warning. A path that does not exist exits `2`.

```bash
trellis ingest corpus <path> [--source-system corpus] [--domain X] \
    [--tag k=v ...] [--include '*.md'] [--dry-run] [--prune] [--extract] [--format json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `path` | — | Directory (or single file) to ingest |
| `--source-system` | `corpus` | Corpus namespace — part of every `doc_id` (`corpus:<source_system>:<sha1(relpath)>`); the classification layer keys on it (e.g. `obsidian`) |
| `--domain` | — | Domain tag applied to every written document |
| `--tag k=v` | — | Extra metadata (repeatable) |
| `--include` | all files | Glob filter over relative paths (repeatable) |
| `--dry-run` | off | Report the full plan (files, chunk counts, skips) without writing |
| `--prune` | off | Delete documents whose source file is gone; one it cannot check is kept and the run exits `5` (see *Pruning fails closed*) |
| `--extract` | off | Mine entities/edges from prose into the graph (see below) |

Re-running over an unchanged tree performs zero writes (`content_hash`
comparison). Edited files re-put under the same `doc_id` and re-embed
changed chunks; moved files are re-keyed via `get_by_hash`, not
duplicated. Documents longer than the 8,000-char embed cap are split
into paragraph-aware **chunk documents**
(`<parent_doc_id>#chunk-<i>`, metadata `{parent_doc_id, chunk_index,
chunk_count, source_path, char_span}`) — with
`TRELLIS_ENABLE_EMBED_ON_INGEST=1` the chunks are what gets embedded,
so long-document content is semantically retrievable. Markdown YAML
frontmatter becomes document metadata and `[[wikilinks]]` are collected
into `metadata.wikilinks` (candidates only — no graph writes).
Cross-file near-duplicates are warned about in the report, never
skipped. Every new/changed file emits `MEMORY_STORED`; each run emits a
`CORPUS_SYNCED` summary event.

**Pruning fails closed** ([#633](https://github.com/ronsse/trellis-ai/issues/633)).
`--prune` deletes a document only when its source file is verifiably
gone. A source it cannot check — one under a directory that lost its
search (`x`) permission,
a path component that became a file, a symlink loop — is kept, listed
under `prune_withheld` with the OS's reason (text output:
`withheld <path>: <reason>`), and the run exits **`5`** with
`status: "partial"`. A `--dry-run` exits the same way, since its plan is
just as incomplete. The rest of the run still happens, including the
prune of files that are verifiably gone. To recover, fix the path or
permission (or move the blocker aside) and re-run; there is no CLI
document delete. A file replaced by a dangling symlink is kept with an
`unreadable_file` warning (a link is a declaration), and a single-file
path withholds every other document, because one file cannot vouch for
the rest of the corpus. Prune assumes **one root per
`--source-system`**: a document synced from another root under the same
namespace is checked against this root. `ingest conversations --prune`
assumes **a complete export**: if the reader could not read all of it,
nothing is pruned, and each conversation missing from what it did read
is withheld.

**Entity extraction (`--extract`)** is **double-gated**: the `--extract`
flag *and* the `TRELLIS_ENABLE_MEMORY_EXTRACTION` env flag must both be
set (at corpus scale it's a per-run LLM-cost decision). When on and an
LLM client is configured, each new/changed document's prose is mined for
entity/edge drafts (the same `build_save_memory_extractor` pipeline the
MCP `save_memory` path uses — deterministic alias-match + LLM residue)
and routed through the governed `MutationExecutor`; the run report gains
`entities_extracted` / `edges_extracted`. Fully fail-soft — a broken
extractor never fails the ingest. With either gate off (the default), no
extraction runs.

**JSON output (abridged):**

```json
{"status": "synced", "counts": {"files_seen": 3, "ingested": 2, "updated": 1, "moved": 0, "skipped_unchanged": 0, "skipped_unsupported": 1, "pruned": 0, "prune_withheld": 0, "chunks_written": 6, "warnings": 0}, "files": [{"path": "runbooks/deploy.md", "doc_id": "corpus:obsidian:7417df…", "action": "new", "chunks": 6}]}
```

### `trellis ingest conversations`

Sync a **Claude conversation export** — the personal context in your
everyday Claude chat — into the document store as one document per
conversation. This is the capture path for real usage: the memories you
accumulate by talking to Claude, which the Claude Code / MCP path never
sees. Shares the idempotent sync core with `ingest corpus`. A path that
does not exist exits `2`.

```bash
trellis ingest conversations <path> [--source-system claude-ai] \
    [--domain X] [--tag k=v ...] [--dry-run] [--prune] [--extract] [--format json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `path` | — | A `conversations.json`, the `.zip` export containing it, or a directory holding it |
| `--source-system` | `claude-ai` | Corpus namespace — part of every `doc_id` |
| `--domain` / `--tag k=v` | — | Metadata applied to every written document |
| `--dry-run` | off | Report the plan without writing |
| `--prune` | off | Delete conversations no longer present in the export. If the export was not read whole, ones it cannot check are kept and listed as withheld, and the run exits `5` (see `ingest corpus`) |
| `--extract` | off | Mine entities/edges from conversation prose into the graph (double-gated with `TRELLIS_ENABLE_MEMORY_EXTRACTION`; see `ingest corpus`) |

**Getting the export:** in claude.ai, Settings → Privacy → *Export data*;
you'll be emailed a `.zip`. Point this command at the `.zip` (or the
unzipped `conversations.json`). Each conversation becomes a document with
`doc_id = conversation:<source_system>:<uuid>`, a speaker-labelled
transcript (`**You:**` / `**Claude:**`), and metadata `{conversation_id,
title, created_at, updated_at, message_count, document_form:
"conversation"}`. (`document_form` was a flat `content_type` key before
#288, which collided with the closed `ContentTags.content_type` facet;
read it with `document_form_of(metadata)`, which accepts both spellings —
stored documents are not migrated, only normalised when the ingest seam
next rewrites them. See [schemas.md → DocumentMetadata](schemas.md#documentmetadata).)
Long conversations are chunked and (with
`TRELLIS_ENABLE_EMBED_ON_INGEST=1`) embedded, so a topic buried deep in a
chat is retrievable. Re-exporting and re-syncing is idempotent:
unchanged conversations skip, conversations that grew new turns re-put and
re-embed. Identity is the conversation's own uuid, so re-imports never
duplicate. The reader tolerates both claude.ai message shapes (top-level
`text` and `content` block lists) and skips thinking / tool-use blocks and
empty turns. Mining entities (people, accounts, preferences) out of the
prose into the graph is the flag-gated `--extract` follow-up (ADR §5).

### `trellis ingest dbt-manifest`

Import a dbt manifest into the knowledge graph.

```bash
trellis ingest dbt-manifest <manifest-path> [--format text|json]
```

Creates entities for models, seeds, snapshots, sources, and tests. Creates `depends_on` edges from the manifest's dependency graph. Indexes every non-empty model/source description into the document store, tagged when `TRELLIS_ENABLE_CLASSIFY_ON_INGEST=1` (#590) and embedded when `TRELLIS_ENABLE_EMBED_ON_INGEST=1` (#568) — both flag-gated, both fail-soft, so the worst case is an untagged or keyword-only document rather than a failed ingest.

`embedded` is `null` when the embed flag is off, which is the default and is not a fault: a count cannot distinguish "embedded 0 of 12" from "the embed never ran", and those call for completely different actions (#410). The text arm prints the flag name instead of a number in that case.

**JSON output:**

```json
{"status": "ingested", "nodes": 12, "edges": 8, "documents": 9, "embedded": 9, "skipped_redacted": 0}
```

`skipped_redacted` is always present. It counts descriptions whose `dbt:<id>` document was redacted: a write over a redaction tombstone is refused (see `trellis curate redact`), so the description is skipped, not counted in `documents` and not embedded, and the run still exits `0` with the other descriptions indexed. A warning-level `dbt_description_skipped_redacted` log line names the document id, and the text arm prints `Skipped (redacted): N` only when `N` is non-zero.

An input that cannot be read or parsed as JSON exits `2`, and a typed store or configuration error exits by the exit-code map (e.g. a damaged `policies.json`, `5`). `nodes` and `edges` count only writes that succeeded. When every write is refused or fails, the command exits by the map for the first one (`3` for a policy refusal, `2` for any other refusal, `5` for a failure) with `"status": "error"` and its `message` beside the counts; descriptions are still indexed, so `documents` can be non-zero. A batch that wrote anything exits `0`.

### `trellis ingest openlineage`

Import OpenLineage events into the knowledge graph.

```bash
trellis ingest openlineage <events-path> [--format text|json]
```

Reads a JSON array or newline-delimited JSON file of OpenLineage events. Creates dataset and job entities with `reads_from` and `writes_to` edges. Writes no documents, so neither ingest flag applies.

**JSON output:**

```json
{"status": "ingested", "nodes": 6, "edges": 4}
```

An input that cannot be read or parsed as JSON exits `2`, and a typed store or configuration error exits by the exit-code map (e.g. a damaged `policies.json`, `5`). `nodes` and `edges` count only writes that succeeded. When every write is refused or fails, the command exits by the map for the first one (`3` for a policy refusal, `2` for any other refusal, `5` for a failure) with `"status": "error"` and its `message` beside the counts. A batch that wrote anything exits `0`.

---

## Curate Commands

Every command in this section shows its warnings whether the write succeeds, is
refused or fails: a `Warning:` line each in text, and in JSON a `warnings` array
that is present on all three, `[]` when nothing warned. A warning is an
`Enforcement.WARN` policy's verdict, or the note that the command's audit event
was not recorded (it starts with `audit_event_not_recorded`).

### `trellis curate promote`

Promote a trace to a precedent (reusable institutional knowledge).

```bash
trellis curate promote <trace_id> --title <title> --description <description> [--by <who>] [--format text|json]
```

| Argument/Option | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `trace_id` | **Yes** | -- | Trace ID to promote |
| `--title` | **Yes** | -- | Title for the precedent |
| `--description` | **Yes** | -- | Description of the pattern |
| `--by` | No | `cli:promote` | Who is promoting |
| `--format` | No | `text` | Output format |

**Example:**

```bash
trellis curate promote 01JRK5N7QF8GHTM2XVZP3CWD9E \
  --title "Database pool configuration pattern" \
  --description "When configuring connection pools, use max 20 connections per process with 30s idle timeout" \
  --by code-orchestrator \
  --format json
```

**JSON output (success):**

```json
{
  "status": "success",
  "command_id": "01JRK7A3QF8GHTM2XVZP3CWD9E",
  "operation": "precedent.promote",
  "message": "Precedent promoted",
  "created_id": "01JRK7A4QF8GHTM2XVZP3CWD9E",
  "warnings": []
}
```

`label` and `feedback` share this output. All three exit `3` when a policy rejects the command, `2` when it is rejected for any other reason and `5` when it fails, with `status` set to `"rejected"` or `"failed"`. The code is the same in both formats.

### `trellis curate link`

Create a directed edge between two entities.

```bash
trellis curate link <source_id> <target_id> [--kind <edge_kind>] [--format text|json]
```

| Argument/Option | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `source_id` | **Yes** | -- | Source node ID |
| `target_id` | **Yes** | -- | Target node ID |
| `--kind` | No | `entity_related_to` | Edge kind (any string; well-known values below) |
| `--format` | No | `text` | Output format |

**Well-known EdgeKind values** (custom domain-specific values are also accepted):

| Value | Meaning |
|-------|---------|
| `trace_used_evidence` | Trace consumed this evidence |
| `trace_produced_artifact` | Trace created this artifact |
| `trace_touched_entity` | Trace interacted with this entity |
| `trace_promoted_to_precedent` | Trace was promoted to this precedent |
| `entity_related_to` | General entity relationship |
| `entity_part_of` | Entity is part of another |
| `entity_depends_on` | Entity depends on another |
| `evidence_attached_to` | Evidence is attached to a target |
| `evidence_supports` | Evidence supports a claim |
| `precedent_applies_to` | Precedent applies to this domain/entity |
| `precedent_derived_from` | Precedent was derived from this source |

**Example:**

```bash
trellis curate link 01JRK5N7QF auth_service --kind entity_depends_on --format json
```

**JSON output (success):**

```json
{
  "status": "ok",
  "edge_id": "01JRK8B3QF8GHTM2XVZP3CWD9E",
  "source_id": "01JRK5N7QF",
  "target_id": "auth_service",
  "edge_kind": "entity_depends_on",
  "warnings": []
}
```

A refused or failed link exits `3` (rejected by a policy), `2`
(rejected otherwise — e.g. a source or target that does not exist) or
`5` (failed), with
`{"status": "rejected"|"failed", "command_id": ..., "message": ..., "warnings": [...]}`.

### `trellis curate label`

Add a label to an entity.

```bash
trellis curate label <target_id> <label> [--format text|json]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `target_id` | **Yes** | Entity ID to label |
| `label` | **Yes** | Label string to add |

**Example:**

```bash
trellis curate label 01JRK5N7QF critical-path --format json
```

**JSON output (success):**

```json
{
  "status": "success",
  "command_id": "01JRK9C1QF8GHTM2XVZP3CWD9E",
  "operation": "label.add",
  "message": "Label 'critical-path' added to 01JRK5N7QF",
  "created_id": "01JRK5N7QF",
  "warnings": []
}
```

A `target_id` that names no node, including one redacted while the command
runs, exits `2` with `"status": "rejected"` and the message
`Node not found: <target_id>`, and no label is written.

### `trellis curate classify-sensitivity`

Relabel a stored document's sensitivity (`public`, `internal`, `confidential`,
`restricted`). A metadata-only write through the governed `classification.set`
operation: the content and the row's `updated_at` are untouched, and the vector
row carries the new label, so the document is withheld from (or served to)
readers on the next pack, keyword and semantic alike. See
[Sensitivity clearance](#sensitivity-clearance).

```bash
trellis curate classify-sensitivity <doc_id> --sensitivity <level> [--by <who>] [--format text|json]
```

| Argument / option | Required | Description |
|-------------------|----------|-------------|
| `doc_id` | **Yes** | Document ID to relabel |
| `--sensitivity` | **Yes** | One of `public`, `internal`, `confidential`, `restricted` |
| `--by` | No | Caller recorded on the command (default `cli:classify-sensitivity`) |

**Example:**

```bash
trellis curate classify-sensitivity doc-1 --sensitivity restricted --format json
```

**JSON output (success):**

```json
{
  "status": "success",
  "command_id": "01JRK9C1QF8GHTM2XVZP3CWD9E",
  "operation": "classification.set",
  "message": "Classification set: doc-1 internal -> restricted",
  "created_id": null,
  "warnings": []
}
```

Re-applying the stored label answers `Classification unchanged: ...` and writes
nothing. A document that does not exist, a chunk id (a chunk reads its parent's
label, so relabel the parent) and a value outside the vocabulary exit `2` with
`"status": "rejected"`. The exit code is the same under `--format text` and
`--format json`. The CLI is a trusted-local surface and holds `restricted`
clearance; the REST route is the credentialed equivalent:

`POST /api/v1/documents/{doc_id}/sensitivity` with body `{"sensitivity": "restricted"}`
returns a `CommandResponse`. It needs the `mutate` scope, and the key's clearance
must cover both the document's current label and the requested one, otherwise
the answer is `400` and nothing is written.

### `trellis curate redact`

Redact (hard-purge) a graph entity through the governed pipeline. Irreversible:
removes **all** SCD-2 versions of the node, every edge touching it, its aliases,
and its vector entry. The `REDACTION_APPLIED` audit event records the shape of
what was removed (counts + id pointers), never the content.

Scope boundaries, stated plainly:

- **The target's own document is blanked, not deleted.** A node whose id is a
  document id (memory extraction mints one per document; a promoted precedent
  has one) stops serving its text through keyword search and `get_items`: the
  row keeps its id with empty content, no title, no other metadata and an
  archived stamp (`deprecation_reason: redacted`), because the document plane
  has no governed delete. A document's `<document>#chunk-N` chunks are blanked
  with it (a chunk target blanks only that chunk) and each blanked row's vector
  row is deleted. The ids ride the payload as `documents_tombstoned` (absent
  when none). This happens before the precedent scrub and the purge, so a
  failure leaves the target in place, every precedent unscrubbed and the
  redaction re-runnable.
- **A write over a redacted id is refused unless it says it means to (#1034).**
  `put_document` refuses a non-blank write to a document id whose row is a
  redaction tombstone (`redaction_tombstone_overwrite`), on every surface
  (`precedent.promote` refuses earlier, with `precedent_document_redacted`; see
  below); an
  unflagged write passes only if the row it writes is still a tombstone (a
  second redaction, or a metadata write that keeps the blank content and the
  lifecycle stamp), so a blank write that drops or replaces the stamp is refused
  too. The check reads the row and then writes it, so it is not atomic: a
  redaction that lands between the two can still be overwritten, and a read that
  fails stops the write. The
  deliberate way back is `reingest_redacted`: `trellis ingest evidence
  --reingest-redacted`, `evidence.reingest_redacted=true` on
  `evidence.ingest` (MCP `execute_mutation`, `POST /api/v1/commands/batch`), or
  a boolean `reingest_redacted` in the body of `POST /api/v1/evidence` or
  `POST /api/v1/documents` (a non-boolean is a 422, nothing written; the refusal
  without it is a 400 whose message names these ways). On the command door
  (`execute_mutation`, `POST /api/v1/commands/batch`) a flag that is present
  and not a boolean, `null`, `0` and `""` included, is refused the same way
  (`evidence_ingest_shape`, nothing written); only absent or `false` is the
  unflagged write (#1049). Each REST way needs the
  `mutate` scope: an `ingest`-only key that sets the flag on
  `POST /api/v1/evidence` is a 403 with nothing written. A reused
  `idempotency_key` answers as the write it first named, so a flagged retry
  needs a fresh one, and a chunk id never reaches the flag on either route (the
  reserved-shape check refuses it, 400, for every key). The flag is no way past
  the clearance rules: a key that may not read a labelled document, or a
  tombstoned chunk of one, still gets the overwrite refusal (409), and the write
  must still carry the document's label. A redaction blanks every metadata key
  but a class label, so a real tombstone carries no label of its own; the cases
  that remain are a label stamped on a tombstone afterwards and a chunk whose
  parent is labelled. Setting it on an id that was not redacted changes nothing
  and stamps nothing. A replay of an earlier write is refused the same way
  (#1049): the idempotency stage answers a key it has seen before the handler's
  guard runs, so an unflagged re-put whose derived key matches an earlier
  successful write to an id that was later redacted (same content, metadata and
  options) would have been answered as a duplicate while the row was a
  tombstone. The executor now asks the handler's optional `replay_refusal`
  on a hit, in the in-memory cache and the event log alike and never on a miss,
  and `evidence.ingest` answers with the guard's own predicate, read-only: the
  replay gets the first write's refusal (`redaction_tombstone_overwrite`, a 400
  on the two REST routes, `rejected` on the command door, one `mutation.rejected`
  event). A hook that raises, or answers with anything but nothing or a
  validation error, fails the command closed (`idempotency_check_failed`); the
  executor looks the hook up on the handler's type. Only a tombstone is asked: a
  replay over a live row or an id with no row is still a duplicate, even one
  whose metadata carries a re-ingest stamp. So is a flagged replay: the flagged key digests the flag,
  so it replays a write that already put content back, and a replay writes
  nothing; if the id was redacted again since, the tombstone stays, and putting
  content back again takes a fresh `idempotency_key`.
  A flagged write stamps the row `reingested_after_redaction` (an ISO time; a
  caller may not set that key, `reingest_stamp_reserved`), and promotion's
  re-ingest exemption (`refused_redacted`) reads the stamp, so a row put back
  by some other route is not mistaken for the owner's re-ingest. `corpus sync`
  never writes over a tombstone: it skips the row (or chunk) and reports a
  `{"kind": "redaction_tombstone", "path", "doc_id"}` warning, and a file that
  shrinks keeps a redacted chunk's row (with the same warning) rather than
  deleting it as an orphan. A move (a file renamed to a new path) and
  `--prune` do the same: they delete the document's other chunks and its
  parent row, keep a redacted chunk's row and report the warning, and
  `--dry-run --prune` predicts it. `--prune` also keeps a parent row that is a
  redaction tombstone, with a `redaction_tombstone` warning and no entry under
  `pruned` (#1067); `--dry-run --prune` predicts that too. A redaction does not
  follow the file. Renaming or copying a redacted corpus file re-ingests it live
  under the new path, so remove or edit the source file when you redact. A parent brought back with `reingest_redacted` gets its row
  back but not its chunks: their tombstones stay, sync reports each as a
  `redaction_tombstone` warning on every run, and corpus sync has no flag to
  re-ingest them (#1039). `trellis ingest dbt-manifest` skips a description whose
  `dbt:<id>` row is redacted and counts it as `skipped_redacted`.
  `worker embed-traces` reports a redacted trace
  summary as `skipped_redacted` and moves its watermark past it; `--dry-run`
  reads the summary row to report the same count (and the same `embedded`) the
  live pass will, and the text output prints a `redacted:` line when it is
  non-zero.
  A promoted precedent's row can be a tombstone too: `precedent.promote`
  over it is refused with `precedent_document_redacted` (re-run `redaction.apply`
  if the node still exists; promotion has no re-ingest flag), and
  `precedent.demote` / `precedent.reinstate` leave it alone, adding a result
  warning and `document_row: "skipped_redaction_tombstone"` on their event.
- **Linked documents are not cascaded** — documents the node merely links
  through `document_ids` are untouched (one may back many entities); the union
  of those links across all versions rides the audit payload for a follow-up
  document-level redaction.
- **Policy sees the precedents the scrub rewrites.** Before the command runs,
  the executor asks the handler which precedents sourced from the target it
  may rewrite and puts one `precedent` command per id to the policy gate, so a
  policy scoped to the `precedent` entity type is consulted; deny wins and
  nothing is written. This is a superset of the precedents actually rewritten.
  The set is read for this only when the gate holds an `entity_type` policy
  with a non-allow rule for the operation (`scopes_by_target_type`; a warn or
  audit-only rule counts); an empty gate is not asked, so it stays transparent.
  A gate without that method is asked every time. If the precedent set cannot
  be read, what happens depends on whether the gate could have refused for a
  precedent (`could_deny_by_target_type`: an `entity_type` policy with a `deny`
  or `require_approval` rule under `enforce`; a gate without that method counts
  as able to). If it could, the command is refused (`policy_scope_unresolved`,
  exit `5`). If it could only warn, the command proceeds, the event payload
  carries `policy_scope_unevaluated: "warn_only"` (key absent otherwise) to say
  the per-precedent warnings were not evaluated, and a `policy_scope_unevaluated`
  warning is logged. The result says it too: `CommandResult.warnings` (the CLI's
  `warnings` JSON key and its `Warning:` lines, and the REST response's
  `warnings`) gains one entry, after any gate warnings, that starts with
  `policy_scope_unevaluated:` and names the exception's type, never its message.
  The event's `policy_warnings` carries the same entry. A precedent promoted
  from the target between that check and the scrub is scrubbed without having
  been gated.
- **A scrubbed precedent reads as new.** The scrub purges and re-creates the
  node, and the graph API cannot set `valid_from`, so a recency window ranks it
  as just written. Its vector row is re-embedded from the scrubbed text when
  embed-on-ingest is on and the precedent has a document row; otherwise the id
  rides the payload as `precedent_vectors_to_reembed` (absent when none) and the
  `redaction.precedent_vector_not_reembedded` log line names it.
- **Precedents that copied the target's text are scrubbed.** A promoted
  precedent's description opens with an excerpt of its source document
  (`source_item_id`), and a precedent with no readable name takes the first
  line of that document as its name. When the target is that document or one
  of its `<document>#chunk-N` chunks (a chunk target matches only that chunk),
  the excerpt is removed from every version of the precedent, its
  `source_excerpt_status` becomes `source_redacted`, and a source-derived name
  becomes `Learning: <intent family> :: <precedent id>` with `name_source`
  `neutral`. The rest of the precedent stays; `get_lessons` serves the node's
  text, not the immutable promotion event's. Promotion records how the name
  was derived as `name_source` (`source_text`, `item_title`, `item_id`,
  `reviewer`) on the node and in the `PRECEDENT_PROMOTED` payload; a bare id and
  a reviewer's rewrite of either hold no source text and are kept. A name
  copied from the item's title is renamed like one copied from its text,
  because the redaction blanks the document row's title too. A name with no
  marker (promoted before it existed) fails closed: the scrub renames it
  unless it is already a bare-id name. The ids ride the payload as `scrubbed_precedent_ids`
  (absent when none). Only a graph node can be a target, so this reaches a
  document that has one; a document with none is refused like any missing
  target. The lookup reads every precedent, page by page, before anything is
  purged; if a page read fails the command fails with the target and every
  precedent untouched, so a redaction never succeeds on a partial scan. A
  scrub purges and re-creates the precedent: if the re-create fails the
  command fails with the target intact and the precedent gone, and
  `get_lessons` serves the promotion event's text with the excerpt cut out
  rather than the copy, and a neutral title when the event marks its title
  `source_text`; if a link fails to come back, the error and the
  `redaction.precedent_links_not_restored` log line name each edge and alias,
  because a re-run no longer matches the scrubbed precedent. A promotion title
  with no marker cannot be told from an ordinary title, so the listing serves
  it as promoted for a node that is missing or never scrubbed; only a node a
  scrub touched serves its own name. A reviewer's edit of a source-derived
  name keeps `source_text`, so the scrub still renames it.
- **Observation/Measurement nodes about the subject are not cascaded** — they
  are independent governed nodes and property-based queries
  (`query_observations`, `GET /measurements`) keep serving them after the
  purge. Their ids ride the payload (`linked_observation_ids` /
  `linked_measurement_ids`); redact each individually if needed.
- **Prior Operational-Plane events are immutable** — the original
  `entity.created` / `entity.updated` payloads (which include the name) remain
  readable via the explore API. Redaction clears the Knowledge-Plane serving
  path; it does not rewrite the audit log.

Prompts for confirmation unless `--yes` is passed (required for scripted use).
Exit codes follow the `exit_codes` map: `3` (rejected by a policy), `2`
(rejected otherwise — e.g. blank/over-long reason, or a missing target), `5`
(failed — e.g. a concurrent purge or a store error), `0` success.

```bash
trellis curate redact <target_id> --reason <text> [--yes] [--by <caller>] [--format text|json]
```

| Argument/Option | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `target_id` | **Yes** | -- | Entity/node ID to hard-purge |
| `--reason` | **Yes** | -- | Audit-trail justification (non-empty, ≤2000 chars, keep it content-free) |
| `--yes`, `-y` | No | `false` | Skip the interactive confirmation |
| `--by` | No | `cli:redact` | Audit-trail identifier for the caller |
| `--format` | No | `text` | Output format |

**Example:**

```bash
trellis curate redact 01JRK5N7QF --yes --reason "defect-minted entity (#299)" --format json
```

**JSON output (success):**

```json
{
  "status": "success",
  "command_id": "01JRK9C1QF8GHTM2XVZP3CWD9E",
  "target_id": "01JRK5N7QF",
  "message": "Entity redacted: 01JRK5N7QF (1 version(s), 0 edge(s), 0 alias(es), vector_deleted=False)",
  "warnings": []
}
```

On failure/rejection the JSON is `{"status": "failed"|"rejected", "command_id": ..., "message": ..., "warnings": [...]}` — `command_id` joins the attempt to its audit event.

A `target_id` that names no node, including one already redacted, exits `2`
with the message `Node not found: <target_id>` (`"status": "rejected"` in the
JSON).

### `trellis curate demote-precedent`

Demote a precedent, stamping its graph node `Lifecycle.state="demoted"`
through the governed pipeline. An SCD-2 rewrite, not a purge — contrast
`redact` above — so the pre-demotion version stays readable via
`get_node_history` / `as_of`. The reversal verb is
`trellis curate reinstate-precedent` (below), **not** `entity.update`:
`EntityUpdateHandler` refuses a write to `properties.lifecycle` on a
precedent node, naming `precedent.demote`/`precedent.reinstate` as the
way to change it, so a demotion can no longer be undone silently with no
reason recorded and no vector re-sync. Once demoted, the precedent stops
reaching `GET /precedents`, `trellis retrieve precedents` and
`get_lessons`, is withheld from packs at the collect seam —
`rejected_items` records it with reason `"demoted"`, and the pack's
withholding statement names it — and its vector row is re-synced in the
same call so a semantic hit stops carrying the pre-demotion snapshot.
Re-approving the demoted precedent's learning candidate from the Review
tab or the CLI is refused too, naming `reinstate-precedent` as the way
back, rather than silently rewriting the node back to `"current"` — see
`trellis.learning.scoring.submit_learning_promotion`.

Only a precedent promoted from a learning candidate has a graph node and
can be demoted. A trace- or miner-sourced precedent id — one with a
`PRECEDENT_PROMOTED` event but no graph entity — is refused with a message
naming that gap (`target_not_graph_backed`), not a generic "Node not
found".

Re-demoting an already-demoted precedent is an idempotent no-op: no new
SCD-2 version, no new event, the stored reason unchanged — the CLI prints
a message saying so and exits `0`. Demoting a precedent whose lifecycle
state is anything other than `"current"` (superseded, archived, or
anything else) is refused, naming the actual state.

This command is also the governed write
`trellis curate evaluate-precedent-demotion`'s `commit` mode uses, once
an operator has enabled that automatic path (see below) — both routes
end at the same handler. Who is demoting governs which precedents may
be demoted:

- A caller whose `--by` identity is the automatic requester identity
  (`auto:learning_candidate_demote`, `AUTO_DEMOTE_REQUESTED_BY` —
  `trellis curate evaluate-precedent-demotion`'s commit mode uses this
  automatically; a human never needs to pass it) may demote only a
  precedent whose own `precedent.promote` event was itself automatic —
  re-derived from the audit trail, never from a caller-supplied flag.
- Every other caller (a human, via CLI/MCP/API) may demote any
  graph-backed precedent that is currently `"current"`.

Exit codes follow the `exit_codes` map: `3` (rejected by a policy), `2`
(rejected otherwise — e.g. blank/over-long reason, a missing target, a
non-precedent target, a non-graph-backed precedent id, a non-`current`
lifecycle state, or an auto caller targeting a human-approved precedent),
`5` (failed), `0` success (including the already-demoted no-op).

```bash
trellis curate demote-precedent <target_id> --reason <text> [--by <caller>] [--format text|json]
```

| Argument/Option | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `target_id` | **Yes** | -- | Precedent entity ID to demote |
| `--reason` | **Yes** | -- | Audit-trail justification (non-empty, ≤2000 chars, keep it content-free) |
| `--by` | No | `cli:demote-precedent` | Audit-trail identifier for the caller |
| `--format` | No | `text` | Output format |

**Example:**

```bash
trellis curate demote-precedent 01JRK5N7QF --reason "superseded by a broader pattern" --format json
```

**JSON output (success):**

```json
{
  "status": "success",
  "command_id": "01JRK9C1QF8GHTM2XVZP3CWD9E",
  "operation": "precedent.demote",
  "message": "Precedent demoted: 01JRK5N7QF",
  "created_id": "01JRK5N7QF",
  "warnings": []
}
```

Also reachable via `POST /precedents/{target_id}/demote` (`{"reason": ...,
"requested_by"?}`, see REST Endpoints below) and MCP `execute_mutation`
(`operation="precedent.demote"`, `args={"target_id", "reason"}`).

### `trellis curate reinstate-precedent`

Reinstate a demoted precedent, stamping its graph node back to
`Lifecycle.state="current"` through the governed pipeline — the reversal
verb `demote-precedent` shipped without. An SCD-2 rewrite in the other
direction, so the demoted version stays readable via `get_node_history` /
`as_of` too. Only a precedent whose current state is `"demoted"` may be
reinstated: a target that is `"current"`, `"archived"`, not found, or not
graph-backed is refused legibly, naming its actual state rather than
quietly no-opping or re-demoting. The precedent's vector row is re-synced
in the same call, so a semantic hit stops carrying the demoted snapshot
and the pack collect seam serves the item again. Human-only: a caller
whose `--by` identity is the automatic demotion policy's own identity
(the same automatic-requester string `demote-precedent` recognizes) is
refused outright, since the automatic path has no legitimate reason to
reverse a demotion.

After a successful reinstate every reader serves the precedent again:
`get_lessons`, `GET /precedents`, `GET /precedents/domains`,
`trellis retrieve precedents`, and a flat pack at the collect seam.

The document and vector re-stamps are fail-soft once the node is current: a
store outage leaves the reinstatement standing and writes
`document_sync_failed` / `vector_sync_failed` on the `PRECEDENT_REINSTATED`
event (and a warning on the result). `trellis analyze health` reads those
events into `precedent_sync` and warns, naming the precedents, until the
latest lifecycle event for each is no longer a failed reinstatement: a later
clean reinstate repairs it (re-issuing this command re-stamps a withheld
document row and re-syncs the vector row too), and a later demotion
supersedes it. A tie at one microsecond resolves toward the failure. The
window bounds the read, so an unrepaired failure stops warning when its event
ages out. When the demote scan hit its cap the oldest demotes are dropped, so
a failed reinstate older than the oldest one kept is listed as `uncertain`
(a dropped demotion may have superseded it) rather than as unresolved. A
vector-only failure leaves the vector row's snapshot at `demoted`, which the
collect seam withholds, and it is refused on re-issue as `target_not_demoted`,
so the warning names no repair for it.

Exit codes follow the `exit_codes` map: `2` (rejected — e.g. blank/
over-long reason, a missing target, a non-precedent target, a
non-graph-backed precedent id, a non-`"demoted"` lifecycle state, or the
automatic-requester identity), `5` (failed), `0` success.

```bash
trellis curate reinstate-precedent <target_id> --reason <text> [--by <caller>] [--format text|json]
```

| Argument/Option | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `target_id` | **Yes** | -- | Precedent entity ID to reinstate |
| `--reason` | **Yes** | -- | Audit-trail justification (non-empty, ≤2000 chars, keep it content-free) |
| `--by` | No | `cli:reinstate-precedent` | Audit-trail identifier for the caller |
| `--format` | No | `text` | Output format |

**Example:**

```bash
trellis curate reinstate-precedent 01JRK5N7QF --reason "demotion was a mistake" --format json
```

**JSON output (success):**

```json
{
  "status": "success",
  "command_id": "01JRK9C1QF8GHTM2XVZP3CWD9E",
  "operation": "precedent.reinstate",
  "message": "Precedent reinstated: 01JRK5N7QF",
  "created_id": "01JRK5N7QF",
  "warnings": []
}
```

Also reachable via `POST /precedents/{target_id}/reinstate` (`{"reason":
..., "requested_by"?}`, see REST Endpoints below) and MCP
`execute_mutation` (`operation="precedent.reinstate"`,
`args={"target_id", "reason"}`).

### `trellis curate promote-learning`

Promote the learning candidates a human approved in a decisions file into
`precedent` nodes, through the governed pipeline (`ENTITY_CREATE`, a
`precedent_applies_to` `LINK_CREATE` per target, then `PRECEDENT_PROMOTE`),
with `requested_by="cli:promote-learning"`.

```bash
trellis curate promote-learning --candidates PATH --decisions PATH \
  [--dry-run] [--format text|json]
```

Only rows with `approved: true` are acted on; a row left unapproved is not
an outcome and never affects the verdict. Each approved row that names a
candidate gets one entry in `results`:

| Entry `status` | Class | Meaning |
|---|---|---|
| `promoted` | landed | the entity, every edge and the `PRECEDENT_PROMOTE` event all landed |
| `promoted_incomplete` | failed | the entity was written but an edge or the event was not; `get_lessons` may not show it |
| `entity_failed` | failed | the entity write did not land; no edge was tried |
| `refused_demoted`, `refused_archived`, `refused_not_current` | refused | a precedent already exists in that lifecycle state; re-approval would erase it, so nothing is written |
| `refused_redacted` | refused | a redaction already took this precedent id (its document row is a redaction tombstone, or a `REDACTION_APPLIED` event names the id); re-promoting it would serve the redacted text again, so nothing is written. A redaction is final: promote a new candidate under a new id if the lesson is still wanted. It is also the answer when the precedent's *source document* was redacted (the precedent survives, scrubbed: neutral name, `source_excerpt_status` `source_redacted`; the source counts as redacted by a tombstone or a `REDACTION_APPLIED` event on the row's `source_item_id` or its chunk parent) and the name may hold that source's text (the source text or the item's title): a candidate captured before the redaction, or a decisions file's `promotion_name` kept from before it (the review template pre-fills the derived name) or edited from it (a name that contains the earlier name's distinctive text is refused too), would write the old name back. Regenerate the candidate **and** the decisions file, or clear the decision's `promotion_name`, and promote that; a new title you write still goes through. If the source id was then written again with new content, a name derived from that content and a reviewer's title promote; only a name the precedent carried at or before the redaction stays refused. The message names the precedent id and never the name |
| `missing_candidate` | refused | the approved id is not in `--candidates` |
| `skipped_non_promotable` | refused | the candidate's `recommendation_type` is not `promote_precedent` or `promote_guidance` |

The exit code and the payload's top-level `status` come from one reading of
those entries, taken before either format renders, so neither depends on
`--format`; the text form ends with a `Verdict: <status> (exit N)` line naming
the same status and code. `review-ready --approve` reads its outcomes the same
way and prints the same line.

| Exit | `status` | When |
|---|---|---|
| `0` | `ok` | every approved row landed, or none was approved |
| `2` | `refused` | at least one row was refused, or an approved row has a blank `candidate_id`, and no write failed |
| `5` | `failed` | at least one write did not land, whether or not another row was refused |

`promoted_count` counts the rows this run fully wrote. A row whose precedent
is already current, complete and unchanged (same values bar `approved_at`,
every named edge with its properties, the precedent's document row, a non-miner
`PRECEDENT_PROMOTED` event) is reported
`already_promoted`, writes no new version and no second event, counts as
satisfied for the verdict, and is carried in `already_promoted_count`
instead; this is what makes re-running a refused file after fixing its bad
row safe. A half-landed (`promoted_incomplete`) row is not skipped, so the
same re-run repairs it. `approved_count` counts
the rows marked `approved: true`, including a row with a blank
`candidate_id`; such a row names nothing to promote, so it is skipped without
a result, **and it refuses the run**: an approved row that names nothing is an
error, not a no-op, so a decisions file whose only approved rows are blank
exits `2` / `refused` in both formats. `named_count` counts the approved rows
that do name a candidate, which is exactly the rows `results` answers; the
rows skipped for want of an id are `approved_count - named_count`, and the
text form says how many. A run where some rows promoted and some approved rows
were blank is `refused` too: the rows that promoted still landed and stay
(`promoted_count` counts them; nothing is undone), and a write that did not
land still outranks the refusal (`failed`, exit `5`). A blank row that is not
approved is ignored. The text header reads `promoted/named_count` (the rows
that could run, as the REST response and the Review toast count them), plus
`, N already promoted` when any were skipped. `POST /learning/promotions`
states the same `status` (HTTP stays 200) and `already_promoted_count`;
`review-ready --approve` refuses a blank id as `refused_not_ready`, and a
precedent that already exists as `refused_already_promoted`, not as a skip.

`--dry-run` submits nothing, so it has no outcome to read: it **always exits
`0`** and reports `ok`, whatever it previews. It adds a preview of the verdict
a real run would reach, named so it cannot be read as one:

| Field | Meaning |
|---|---|
| `would_verdict`, `would_exit_code` | the `status` and exit code a real run would give if every ready write landed, a blank-id approved row included (text form: a `Would-be verdict:` line, never `Verdict:`) |
| `results[].would_status` | the status a real run would give that row: `promoted`, or its refusal |

The preview reads, and never writes. It sees a refusal the planner decides
(`missing_candidate`, `skipped_non_promotable`) and one an existing
precedent's lifecycle state decides (`refused_demoted`, `refused_archived`,
`refused_not_current`), or a redaction decides (`refused_redacted`, either kind). It cannot see a policy-gate refusal or a write that
fails, so a preview of `ok` can still become `refused` or `failed`. A row it would refuse is printed without
its `entity_payload.name`, which may be the text a redaction removed.

### `trellis curate review-ready`

Review the promotion-ready learning candidates and approve the ones you
name, in one command. It is the human path beside `promote-learning`: no
decisions file, and the candidate's own content on screen.

```bash
trellis curate review-ready [--candidates PATH] [--limit N] \
  [--approve ID ...] [--rationale TEXT] [--force-contradicted] \
  [--format text|json]
```

**Without `--approve` it only reads.** It lists the candidates the
promotion-ready digest counts (`is_promotion_ready`, the one predicate the
digest and this command share), up to `--limit` (default 20; `ready_count`
is always the full count). `--candidates` defaults to the
learning-artifacts directory the Review queue reads. Each row carries:

- `candidate_id`, `intent_family`, the source `item_id`, and a
  `source_excerpt` of at most 300 characters, resolved as a promotion
  resolves it. `source_excerpt_status` is `included` or the reason there is
  none, including `source_withheld` for a noise or archived source;
- `helpful_count`, `unhelpful_count`, `distinct_helpful_sessions` and
  `sessionless_helpful_count` (`null` when an older artifact never wrote the
  field), and `target_entity_count`;
- `cross_family_noise`, with `noise_intent_families`: the same `item_id` is
  an `investigate_noise` candidate under another intent family;
- `already_promoted`: a successful `precedent.promote` event or a
  `precedent` node already exists for the candidate.

Candidates the digest would rank first can still sort last: clean
candidates come first, then contradicted ones, then already-promoted ones,
each group in digest order.

**With `--approve ID`** (repeat the flag for several) each id is promoted
through `prepare_learning_promotions` and `submit_learning_promotion` with
`requested_by="cli:review-ready"`, exactly as `promote-learning` would,
with `--rationale` stored as `approved_rationale` on each precedent. An id
is refused, and nothing is written for it, when it is:

| Outcome | Meaning |
|---|---|
| `refused_not_ready` | not in the ready set: unknown id, not `promote_guidance`, no helpful citation, or an unhelpful one |
| `refused_already_promoted` | a precedent already exists for it |
| `refused_contradicted` | cross-family noise; `--force-contradicted` lifts this refusal and no other |

A precedent in any lifecycle state (demoted, archived, superseded) answers
`refused_already_promoted`; bring one back with `trellis curate
reinstate-precedent` or the `retention.restore` operation.
`promoted_incomplete` means the entity was written but an edge or the
`PRECEDENT_PROMOTE` event was not; a re-run here then answers
`refused_already_promoted`, so retry it with `trellis curate
promote-learning`. Every id gets its own entry in `results`.

The exit code does not depend on `--format`, and the payload's `status`
comes from the same reading: `0` / `ok` when every named id landed, `2` /
`refused` when any was refused and none failed, `5` / `failed` when a write
did not land. With `--approve` the text form ends with the same
`Verdict: <status> (exit N)` line `promote-learning` prints. Listing (no
`--approve`) always exits `0` and prints no verdict: nothing was promoted, so
there is no outcome to judge.

### `trellis curate auto-promote-candidates`

Evaluate learning candidates for automatic promotion to a precedent —
default-off, report-only unless an operator enables commit mode. Every
candidate gets a disposition: `would_promote`, `already_promoted` (a
precedent already exists for it, whether a human approved it on the
Review tab or an earlier automatic run created it), or
`ineligible:<reason>` (not-promotable recommendation type, unhelpful
citations when `require_zero_unhelpful`, or below the
`min_helpful_count`/`min_distinct_sessions` floors). Evaluation order
checks already-promoted first, so a candidate past the finish line is
never re-counted toward `max_promotions_per_run`.

`learning.candidate_auto_promote.mode` can only be set to `commit` once
`learning.precedent_auto_demote.enabled` is also true —
`evaluate-precedent-demotion` below is commit mode's safety rollback,
so running one loop's commit mode without the other enabled would leave
a wrong unattended promotion with no automatic undo.
`LearningCandidateAutoPromotePolicy.__post_init__` raises
`ConfigError` at construction if `mode="commit"` is requested without
that sibling policy enabled; `--dry-run` sidesteps this by forcing the
run's `effective_mode` to `report_only` *before* construction, so a
stored or env-configured `commit` never reaches the check — the run
still truthfully reports the pre-`--dry-run` value as `configured_mode`,
distinct from the `mode` that actually ran, in the CLI output, the
emitted event, and the Loop Health row. In `commit` mode each
`would_promote` candidate is promoted through the same governed path a
human Review-queue approval uses (`submit_learning_promotion`), capped
by `max_promotions_per_run`.

This command only ever reads the sibling demotion policy's `enabled`
flag, never its `mode`. A refused stored or env value for
`learning.precedent_auto_demote.mode` (a hand-edited `settings.json`,
or an env override the setting's `refused_values` rejects) still blocks
a non-`--dry-run` run of this command with the same `ConfigError` it
would raise against `evaluate-precedent-demotion` itself — resolving
the sibling policy's settings is all-or-nothing, so a refused `mode`
has nowhere to go but up, even though `mode` itself plays no part in
this command's own check. `--dry-run` holds that refusal back the same
way it holds back this command's own.

Every run — including one that fails before evaluating a single
candidate — emits exactly one `LEARNING_AUTO_PROMOTE_EVALUATED` event.
For a completed run, `outcome` (and the matching `--format json`
`status`) is one of `"ok"` / `"degraded"` / `"failed"`, shared with the
demotion command below: `"failed"` means every candidate this run
actually attempted to submit failed submission (and at least one was
attempted — a run with nothing to attempt is never `"failed"`);
`"degraded"` means some, but not all, attempted submissions failed, or
a candidate was skipped for having no usable id; otherwise `"ok"`. A
candidate that was never attempted (`would_promote`,
`already_promoted`, `ineligible:run_cap_reached`, ...) affects neither
count. For a run that never reached evaluation at all, `outcome` is
instead `"refused"` (the `ConfigError` case, exit 5) or `"failed"` (a
malformed `--candidates` file or similar, exit 2 or 1) — never zero
events, so a silent exit cannot read as "this job never ran" to
`trellis.ops.loop_health`.

Both policies ship `enabled=False` by default: nothing commits
unattended until an operator turns both on.

This command builds a real governed `MutationExecutor` (via
`build_curate_executor`) on every run, including a `report_only` or
`--dry-run` one: the executor supplies the fail-closed behavior (Stage
2's policy gate, idempotency, audit emission), not a conditionally
constructed one. `report_only` means the policy never issues a
`precedent.promote` command — the executor that would run it is built
regardless.

```bash
trellis curate auto-promote-candidates [--candidates <path>] [--dry-run] [--format text|json]
```

| Argument/Option | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `--candidates` | No | `resolve_learning_artifacts_dir()`'s `intent_learning_candidates.json` | Path to the candidates file the Review queue also reads |
| `--dry-run` | No | off | Force this run's effective mode to `report_only`, regardless of configured mode |
| `--format` | No | `text` | Output format |

**Example:**

```bash
trellis curate auto-promote-candidates --dry-run --format json
```

Catalog job `auto-promote-candidates` runs this on a daily cadence (see
[Schedule registry + job runs](#schedule-registry--job-runs) below); the job's own stored mode governs
whether that scheduled run can commit.

### `trellis curate evaluate-precedent-demotion`

Evaluate current precedents for demotion against the evidence they have
accumulated since promotion — default-off, report-only unless an
operator enables commit mode. This is `auto-promote-candidates`'
safety rollback: joining `PACK_ASSEMBLED.injected_items` to
`FEEDBACK_RECORDED` by pack id, a precedent demotes only on a floor of
unhelpful citations (`--min-unhelpful-citations`, provisional — borrowed
from `trellis.classify.demotion_gate`, measured for noise documents, not
for precedents) — never on the absence of helpful ones — and any
precedent cited helpful at least as often as unhelpful is refused
regardless of the unhelpful count. An already-demoted or archived
precedent, a human-promoted one (`ineligible:not_auto_promoted` /
`refused:not_auto_promoted` — pre-filtered via the same
`_was_auto_promoted` derivation the handler itself checks, so it is
never listed `would_demote` and commit mode never submits a command
for it), or one otherwise not in the current lifecycle state, is
reported `already_demoted` or `ineligible:<reason>` /
`refused:<reason>` without being judged.

Each precedent's evidence window starts at the later of its last
successful `precedent.promote` and its last successful
`precedent.reinstate`, so citations from before a human reinstatement
never count toward demoting it again — a reinstated precedent with no
new citations since reports `ineligible:no_evidence_supplied` /
`refused:no_evidence_supplied`, not `would_demote`. When the citation
join is truncated, a precedent demotes (or is reported `would_demote`)
only if its evidence window start is at or after the scan's
`covered_since`; every other current precedent whose window falls
outside what was actually read is reported
`skipped_incomplete_evidence` instead, and counted via
`skipped_incomplete_evidence_count` — never silently dropped and never
demoted on a partial read.

Only a precedent served through a `get_context` or sectioned pack can
accumulate evidence this way: `get_lessons` emits no per-item
attribution (nor does a sectioned pack assembled before #958), so a precedent reachable only through those
surfaces always refuses for want of evidence, regardless of how useful
it actually is — `attribution_caveat` in this command's own output
(every format) says so.

`learning.precedent_auto_demote.mode` only commits once
`learning.precedent_auto_demote.enabled` is also true
(`effective_mode`). Unlike the promotion policy above, this policy
carries no construction-time gate on a sibling — demotion is itself the
rollback a safe commit mode needs. In `commit` mode each `would_demote`
precedent is demoted through the same governed path
`trellis curate demote-precedent` uses, capped by
`max_demotions_per_run` and rate-limited per run
(`skipped_rate_limited`). The submitted command carries an
`idempotency_key` derived from the precedent id and its evidence
window start, so two overlapping runs cannot both count a demotion or
spend rate-limit cap on the same no-op; the losing run reports that
precedent `deduplicated` (`PrecedentAutoDemoteReport.deduplicated_count`)
rather than `demoted`. A hand-edited `settings.json` cannot set
`learning.precedent_auto_demote.mode` (or its promotion twin) to
`commit`: resolving a stored value against the setting's
`refused_values` raises `ConfigError` naming the setting, the same as
the environment-variable path; only the environment variable can
select `commit`.

Every run — including one that fails before evaluating a single
precedent — emits exactly one `LEARNING_AUTO_DEMOTE_EVALUATED` event,
mirroring the promotion event's `outcome`/`configured_mode`/
`policy_snapshot` shape, including the completed-run `"ok"` /
`"degraded"` / `"failed"` vocabulary described above:
`"failed"` means every demotion this run actually attempted was
refused (and at least one was attempted); `"degraded"` means some,
but not all, attempted demotions were refused — a precedent that was
never attempted (`would_demote` in report-only mode,
`skipped_rate_limited`, `deduplicated`, `already_demoted`,
`ineligible:*`, `skipped_incomplete_evidence`) affects neither count,
and this gate never folds a dropped-silently case into `"degraded"`
the way the promotion job's skipped-id count does, because this scan
never drops a precedent silently — see `skipped_incomplete_evidence`
above. Both this report and the promotion report's
JSON carry a top-level `status` key derived from the same flag that
drives the exit code.

Like `auto-promote-candidates` above, this command builds a real
governed `MutationExecutor` on every run, `report_only` included: the
executor is the fail-closed mechanism, not a stand-in that is skipped
until commit mode is reached.

```bash
trellis curate evaluate-precedent-demotion [--min-unhelpful-citations <n>] [--dry-run] [--format text|json]
```

| Argument/Option | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `--min-unhelpful-citations` | No | `MIN_UNHELPFUL_CITATIONS` | Per-precedent unhelpful-citation floor |
| `--dry-run` | No | off | Force this run's effective mode to `report_only`, regardless of configured mode |
| `--format` | No | `text` | Output format |

**Example:**

```bash
trellis curate evaluate-precedent-demotion --dry-run --format json
```

Catalog job `evaluate-precedent-demotion` runs this on a daily cadence
(see [Schedule registry + job runs](#schedule-registry--job-runs) below); the job's own stored mode
governs whether that scheduled run can commit.

### `trellis curate feedback`

Record feedback (rating and optional comment) on a trace or precedent.

```bash
trellis curate feedback <target_id> <rating> [--comment <text>] [--pack-id <id>] [--format text|json]
```

| Argument/Option | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `target_id` | **Yes** | -- | Trace or precedent ID |
| `rating` | **Yes** | -- | Rating, 0.0 to 1.0 inclusive. Anything else, NaN included, exits 2 and records nothing |
| `--comment` | No | `null` | Optional text comment |
| `--pack-id` | No | `null` | Context pack the feedback is about — the join key |
| `--format` | No | `text` | Output format |

`--pack-id` is what lets the learning loop see the event at all:
`join_pack_feedback` matches `FEEDBACK_RECORDED` to `PACK_ASSEMBLED` strictly
on `payload.pack_id` and skips events without it. This command still records a
**pack-level grade only** — the per-item `helpful_item_ids` /
`unhelpful_item_ids` attribution the promote half consumes comes from the MCP
`record_feedback` tool or `POST /packs/{pack_id}/feedback`.

**Example:**

```bash
trellis curate feedback 01JRK5N7QF 0.9 --comment "Solid pattern, well-documented" --format json
```

**JSON output (success):**

```json
{
  "status": "success",
  "command_id": "01JRKAB1QF8GHTM2XVZP3CWD9E",
  "operation": "feedback.record",
  "message": "Feedback recorded",
  "created_id": null,
  "warnings": []
}
```

### `trellis curate entity`

Create an entity node in the knowledge graph. The Neo4j deployment guides run
it as their smoke check.

```bash
trellis curate entity <entity_type> <name> [--properties <json>] [--format text|json]
```

| Argument/Option | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `entity_type` | **Yes** | -- | Entity type (any string, e.g. `concept`, `person`, `system`) |
| `name` | **Yes** | -- | Entity name |
| `--properties`, `-p` | No | `null` | JSON object of properties |
| `--format` | No | `text` | Output format |

**JSON output (success):**

```json
{
  "status": "ok",
  "node_id": "01JRKAC1QF8GHTM2XVZP3CWD9E",
  "entity_type": "concept",
  "name": "smoke-check",
  "properties": {"name": "smoke-check"},
  "warnings": []
}
```

It exits `3` when a policy rejects the command, `2` when it is rejected
for any other reason and `5` when it fails, with
`{"status": "rejected"|"failed", "command_id": ..., "message": ..., "warnings": [...]}`
and no `node_id`. Invalid `--properties` JSON exits `2` with `"status": "error"`
and no `warnings` key, because nothing was submitted.

### `trellis curate advisory restore`

Undo an advisory suppression: flip the row back to `active`, clear
`suppressed_at` / `suppression_reason`, and clear the keys a rule retirement
wrote to its metadata (`retired_by`, `retired_rule`, `retired_ceiling`). The
way back for a QUERY advisory the generator retired under the
document-frequency rule, which un-retires on its own only when the generator
emits that word again (it must clear `min_sample_size`, `min_effect_size` and
the comparison-arm floor, which a word whose share has fallen may no longer do).

```bash
trellis curate advisory restore <advisory_id> [--reason <text>] [--dry-run] [--format text|json]
trellis curate advisory restore --rule <rule> --all [--reason <text>] [--dry-run] [--format text|json]
```

| Argument/Option | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `advisory_id` | One of this or `--rule --all` | -- | Restore this row, however it was suppressed (retirement or fitness loop) |
| `--rule` | With `--all` | -- | `stop_list`, `document_frequency` or `unnormalised_keyword`: the `retired_rule` metadata value to restore |
| `--all` | With `--rule` | off | Restore every suppressed row that rule retired, in one file write |
| `--dry-run` | No | off | List what would be restored; write nothing and emit no event |
| `--reason` | No | -- | Audit-trail justification, recorded in each event payload |
| `--format` | No | `text` | Output format |

`--all` without `--rule` is refused, so a bulk restore cannot reverse the fitness
loop's evidence-based suppressions; `--rule` without `--all`, an id together with
`--all`, nothing named, and an unknown rule are refused too. A bulk restore
matches on `retired_rule`, so a row the fitness loop suppressed (it carries no
`retired_rule`) is only restored by naming its id.

Each restored row emits an `ADVISORY_RESTORED` event (`source="cli:curate"`,
`entity_type="advisory"`; payload `advisory_id`, `restored_via`,
`previous_suppression_reason`, `previous_retired_rule` and `reason` when given,
and no `new_confidence`, which the fitness-loop events carry and the dashboard
averages). The write is not routed through `MutationExecutor`: no advisory write
is, and `AdvisoryStore`'s own degraded and stale guards are what protect the
file. The event follows the same posture as suppression's: the store write is
the source of truth, the event is emitted after it and fail-soft, and a miss is
reported in `audit_events_failed` (a `Warning:` line in text) with the restore
still landed and the exit unchanged.

A restore is not a pardon. The next `generate-advisories` run retires a
restored row again while the rule that retired it still excludes the word (a
stop-list word stays retired; a frequency retirement holds only while
`--max-keyword-df` is set and the word's share stays over it), and the fitness
loop scores the row again as it does any active one.

Exit codes, the same in both formats (the exit sits below the format branch):

| Exit | Meaning |
|------|---------|
| `0` | Restored, previewed, or nothing to do: an already-active id (`"status": "noop"`) or a rule with no suppressed rows (`"count": 0`) |
| `2` | A refused argument combination or unknown rule (`"status": "invalid"`), or an unknown advisory id (`"status": "not_found"`) |
| `5` | The advisory file is degraded (`"status": "degraded"`, with `store_degradation`) or another process rewrote it since this command read it (`"status": "refused"`, `"code": "STALE_STORE_WRITE"`, re-run); nothing is written. `--dry-run` refuses on a degraded file too, since it would preview a set missing the rows that failed to parse |

**Example:**

```bash
trellis curate advisory restore --rule document_frequency --all --dry-run --format json
```

**JSON output:**

```json
{
  "status": "dry_run",
  "dry_run": true,
  "advisory_id": null,
  "rule": "document_frequency",
  "count": 1,
  "audit_events_failed": 0,
  "advisories": [
    {
      "advisory_id": "01JRK5N7QF",
      "category": "query",
      "scope": "global",
      "message": "Include 'deploy' in your context query",
      "previous_suppression_reason": "query keyword excluded by document_frequency: 'deploy'",
      "previous_retired_rule": "document_frequency"
    }
  ]
}
```

`status` is `restored` on a real run, and `advisories[]` lists the rows restored,
each as it was before the restore (a dry run lists the same shape). This command
has no `warnings` array, unlike the rest of this section, because it submits no
`Command`.

---

## Settings Store

`<data_dir>/stores/settings.json` holds per-deployment overrides for the
write-behaviour knobs reported by `trellis admin write-config` and
`GET /api/version`, plus `graph_seeding` — 13 tunables total
(`src/trellis/core/settings_registry.py`'s `SETTINGS_REGISTRY`: the 12
`WriteBehaviourConfig` fields plus `graph_seeding`, explicitly **not**
`learning.auto_promote.*`, which stays out by owner decision).

**The write surface**: `trellis admin settings set <name> <value>` /
`settings unset <name>` and `PUT`/`POST /api/v1/settings/{name}` (admin
scope) both submit `Operation.SETTINGS_UPDATE` through the same narrow
`build_settings_executor` — a denying policy, a degraded store or an
out-of-range value is refused identically on either surface, and nothing
is silently clamped. These two are the *only* surfaces that can reach
this operation at all: the generic `POST /commands/batch` route and
MCP's `execute_mutation` build `build_curate_executor` instead, which
never registers `Operation.SETTINGS_UPDATE`, so a `settings.update`
command submitted there fails — `status: "failed"`, not `"rejected"`,
because nothing was evaluated and no `MUTATION_REJECTED` event exists
to name a reason — rather than being evaluated and refused by policy;
there is no shared refusal path to be "identical" to from those two.
Both surfaces rewrite the message (not the status) through
`redirect_unreachable_settings_update`, so the failure names where the
write *is* reachable (`PUT`/`POST /api/v1/settings/{name}`, or
`trellis admin settings set`/`unset`) instead of a bare "no handler
registered for: settings.update".
`value: null` (CLI: `settings unset`) removes the
override; there is no separate reset operation. A write for a setting
whose environment variable is currently set still stores the override —
the REST response says `accepted: true` plus the env value actually in
force, rather than claiming the write just took effect. `GET
/api/v1/settings` lists every registry entry's metadata, resolved value
and provenance; **no CLI equivalent ships yet** — `trellis admin
settings list` is not a real subcommand today even though
`SettingsStore`'s stale-write recovery text below still names it (#854),
so follow that text's advice with the REST route, or `trellis admin
write-config` for the twelve `WriteBehaviourConfig` fields, until one is
added.

### Precedence: env > settings > default

Each knob resolves through `WriteBehaviourConfig.from_env_and_settings()` in
that order: a real (non-blank) environment variable always wins; a settings
override applies only where the environment is unset or blank; the shipped
default applies when neither is set. `resolve_overridden_by()` answers the
same question per field independently, as `"env"` / `"settings"` /
`"default"` — `describe(overridden_by=...)` carries it into
`trellis admin write-config --format json`'s `knobs[].overridden_by` and
into `GET /api/version`'s `write_behaviour_settings[].overridden_by`.
Callers that omit `overridden_by` keep the older two-valued behaviour
(`describe()` falls back to `"env"` / `"default"` from the `overridden`
flag), so this is additive, not a breaking change to `describe()`'s shape.

**`settings_live` tells you whether an override does anything yet.** The
registry catalogs every tunable `write_config.py` knows about plus
retrieval-side knobs resolved outside it (`graph_seeding`,
`graph_recency_fallback`, `filter_legacy_advisories`, `class_prior_mode`);
`graph_seeding` gates only
seeds *inferred* from the intent, never a sectioned pack's `entity_ids` or an
explicit `seed_ids`, which are instructions; `graph_recency_fallback`
(`TRELLIS_GRAPH_RECENCY_FALLBACK`, default **off**, #961) is the opposite
default and gates only the graph axis's *unseeded* branch — see "The graph
axis's unseeded window" below; it
is not a claim that each one has a settings-aware enforcement site, only
that this one does. Every registered knob now resolves env > settings >
default at its real enforcement site — each per-knob reader
(`classify/ingest.py`, `extract/memory_ingest_hook.py`,
`extract/trace_ingest_hook.py`, `retrieve/embed_ingest_hook.py`,
`mcp/reconcile.py`, `mcp/server.py`) delegates to
`write_config.resolve_write_behaviour()`, `graph_seeding` /
`graph_recency_fallback` / `filter_legacy_advisories` each carry their own settings-aware read in
`builder_factory.py` / `pack_builder.py`, and `class_prior_mode`
resolves the same way through
`trellis.retrieve.class_prior.resolve_class_prior_config`, wired in by
`builder_factory.py` — so a settings override reaches runtime the moment
you set one. The lone exception is
`minhash_seed_max_docs`: it is read once, lazily, at an MCP process's first
`save_memory` call and cached for that process's lifetime, so a settings
write after that first call has no effect until the next process starts.
`SettingSpec.settings_live` is `False` only for that one knob, which
instead carries `restart_required=True`.

### Pack-size and excerpt knobs

Seven `group="retrieval"` tunables, all `settings_live=True`, cover the
pack-size and excerpt defaults each surface previously hardcoded. Every one
resolves `env > settings > default` at its real enforcement site, and an
explicit caller argument (where a surface accepts one) always wins over
both:

| Setting | Env var | Default | What it bounds |
|---|---|---|---|
| `get_context_max_tokens` | `TRELLIS_GET_CONTEXT_MAX_TOKENS` | 2000 | mcp `get_context`'s flat-layout token budget when the caller omits `max_tokens`. Does **not** affect `get_context`'s `sections=` layout, which keeps resolving its budget from `config.yaml`'s `retrieval.budgets` via `registry.budget_config` — the two are deliberately decoupled. |
| `get_context_max_items` | `TRELLIS_GET_CONTEXT_MAX_ITEMS` | 50 | mcp `get_context`'s flat-layout candidate-item ceiling. `get_context` exposes no caller-facing `max_items`, so this setting is the only way to change it without an env var. |
| `search_max_tokens` | `TRELLIS_SEARCH_MAX_TOKENS` | 2000 | mcp `search`'s token budget when the caller omits `max_tokens`. Resolved independently of `get_context_max_tokens` — the shared 2000 default is coincidence, not unification. `search`'s item ceiling is always the caller's `limit` argument; there is no settings knob for it. |
| `rest_pack_max_items` | `TRELLIS_REST_PACK_MAX_ITEMS` | 50 | `POST /api/v1/packs`'s `max_items` when the request body omits the field. Resolved from the request's own `model_fields_set`, not from `PackRequest`'s field default, so `PackRequest`'s OpenAPI schema is untouched and a caller that supplies `max_items` — even the value 50 — always wins. |
| `rest_pack_max_tokens` | `TRELLIS_REST_PACK_MAX_TOKENS` | 8000 | `POST /api/v1/packs`'s `max_tokens` when the request body omits the field. Same `model_fields_set` resolution as `rest_pack_max_items`. |
| `disclosure_body_items` | `TRELLIS_DISCLOSURE_BODY_ITEMS` | 12 | How many ranked pack items get a full-body excerpt before graduated disclosure demotes the rest to a summary instead of dropping them. Applies to every surface that assembles a pack through `build_pack_builder` (mcp, REST, CLI). |
| `excerpt_max_chars` | `TRELLIS_EXCERPT_MAX_CHARS` | 500 | Character cap on each retrieved item's excerpt (keyword, semantic and graph axes) before it enters a pack. Applies to every surface through `build_pack_builder`. |

**The three `max_tokens` defaults (2000 / 2000 / 8000) and the two
`max_items` defaults (50 / 50) are deliberately not unified across
surfaces** — each was tuned for its own caller independently, and this PR
only made the existing values operator-tunable; it did not reconcile them.

`get_context_max_tokens`/`get_context_max_items`/`search_max_tokens` are
read via a private per-module int resolver in `mcp/server.py`;
`rest_pack_max_items`/`rest_pack_max_tokens` via one in
`trellis_api/routes/retrieve.py`; `disclosure_body_items`/
`excerpt_max_chars` via one in `retrieve/builder_factory.py` — each mirrors
`capture_health._resolve_int_knob`'s coerce-and-bounds-check-through-
`validate_value` shape rather than sharing code across modules.

### Failure posture: degrades, never raises

Unlike `policies.json` below, a damaged `settings.json` **does not fail a
read.** `SettingsStore` (`src/trellis/stores/settings_store.py`, built on
the shared `DegradableJsonStore`) degrades per row: rows that parsed are
served, a row that didn't parse is simply absent from the override set,
which makes that one knob fall back through the same
`env > settings > default` chain as if it had never been overridden — not a
`ConfigError`. A corrupt settings file silently reverting one tunable to its
environment value or default is no worse than that tunable never having
been overridden, so nothing here needs to fail closed the way a damaged
`policies.json` does. The degradation is still visible, not silent: a
`settings_load_degraded` **error**-level log line names the path and reason,
because a reverted override an operator can't see is still a surprise, just
not an access-control one. Every write (once a write surface ships) is
refused while degraded or stale, for the same whole-file-rewrite reason
`policies.json` refuses writes: replacing the file from a partial view would
make the damaged file *valid* and erase whatever didn't parse, for good.
Recovery: `trellis admin settings list`.

---

## Policy Commands

Stage 2 of the [governed mutation pipeline](#python-only-mutation-api) evaluates
governance policies against every command. These commands declare them.

**Default posture: Trellis ships zero policies, and a deployment with none
behaves exactly as if no gate existed.** An empty gate allows every command and
adds nothing to the audit payload. You have to write a policy for anything here
to have an effect. Wiring the gate is not itself a restriction.

### Where policies live

`<data_dir>/stores/policies.json` — written by `trellis policy add` and by
`POST /policies`, read by the mutation pipeline. All three are resolved through
one helper (`trellis.mutate.resolve_policy_path`) so the file you write is the
file that gets enforced.

A file at the pre-unification CLI path `<data_dir>/policies.json` is still
honoured, with a warning naming both paths; the canonical path wins if both
exist. Move it when you see the warning.

The file is **loaded strictly**: if it exists but is corrupt, unparseable, has
no `"policies"` key, or contains an invalid policy, mutation surfaces raise
`ConfigError` rather than degrading to zero policies. Silently falling back
would mean a damaged access-control file disables access control while the
caller believes it is governed. A deployment with *no* policy file is
unaffected — strictness only begins once you have declared something.

Policies are read per call, so an edit takes effect without a restart.

### If the file is damaged

The CRUD store (`trellis policy`, `/api/policies`) and the enforcement reader
treat a damaged file differently, on purpose:

* **Enforcement fails closed.** Every mutation surface raises `ConfigError`
  naming the file. The pipeline stops; it does not run ungoverned.
* **`trellis policy list` still works**, showing whatever parsed — that is the
  point of a separate CRUD reader — but it prints a `POLICY STORE DEGRADED`
  banner above the listing and **exits 5**, because a partial view of an
  access-control file must not be scriptable as the whole ruleset. `--format
  json` carries a `store_degradation` object with the same detail.
* **Every write is refused**, on the CLI (exit 5) and over REST (HTTP 409),
  including `remove`. A store that could not read its own file has no business
  rewriting it: the rewrite would produce a *valid* file containing only what
  parsed, which enforcement would then accept as the truth (#413).
* Recovery is yours to run, and every surface prints it:
  `mv <path> <path>.corrupt`. Nothing moves the file for you — the readers here
  include pack assembly, and a read must not mutate your filesystem. After the
  `mv` the path is absent, which is a legitimate transparent zero-policy
  deployment, so **re-declare your policies**.

### How you find out that the file is damaged

Failing closed is right; failing closed *invisibly* was the gap (#425). The
raise happens at gate-build time, **before** a `MutationExecutor` exists, so
there is no `MUTATION_REJECTED`; every surface builds its executor outside its
`WRITE_REJECTED` boundary, so there was no boundary rejection either. Both are
what `trellis analyze health` and the capture-health banner count, so the one
failure that darkens *every* write surface at once was the one they could not
see.

A gate that will not load now emits a `WRITE_REJECTED` event under the surface
label **`config:policy_file`** before the `ConfigError` propagates, and an
`error`-level `policy_gate_load_failed` log line naming the path. So:

* `trellis analyze health` reports it under `by_tool["config:policy_file"]`,
  with `boundary_kinds` showing `config_unreadable@policies.json` and the
  `ConfigError` text — including its recovery command — in the event's
  `rejections[0].msg`.
* The **capture-health banner** fires on the pack-serving surfaces once the
  threshold is crossed, so an agent is told memory capture is down rather than
  inferring greenfield from an empty pack.

The label is not an MCP tool and does not get an `mcp:` prefix, because the
failure is not a property of any one surface — it is every surface at once.
For the same reason it clears differently: no accepted write can ever carry
that `requested_by`, so the banner clears on the first accepted write from
*any* surface after the last rejection, which is exactly what a loaded gate
produces.

Note this is **latent on a deployment that has declared no policies** — there
is no file to damage. It becomes live the moment you declare your first policy.

### If another process wrote the file first

Two processes write `policies.json` on a typical deployment — `trellis
policy add` on the host and `POST /api/policies` in a container, against
the same data dir. Both stores whole-file-rewrite, so a write from a view
that predates the other's write would silently delete it.

A write is therefore refused when the file changed after the store read it:
exit **5** on the CLI, HTTP **409** with `"code": "stale_store_write"` over
REST. Unlike the damaged case this one is **transient** — re-read and redo
the operation. It is a compare-and-swap rather than a lock, so it closes
the wide window and narrows the tiny one; last-writer-wins is still the
model for two writes that genuinely race.

### `--format json` shapes

`trellis policy list` and `show` carry the house `status` key (`"ok"` /
`"degraded"`), plus `store_degradation` when degraded. `list` also reports
`policy_file` and `policy_file_present`.

**`show` returns an envelope** — `{"status", "policy"}` — not a bare
`Policy` dump. Every Trellis schema is `extra="forbid"`, so a dump with a
`store_degradation` key alongside it is a payload `Policy.model_validate`
rejects; the envelope is what lets the degradation ride along. Read the
policy from `.policy`.

`trellis policy list` also distinguishes the two ways of getting an empty
answer: an absent file (the shipped default) says nothing extra, while a file
containing `{"policies": []}` says so and notes that Stage 2 is transparent.
Enforcement deliberately does not make that distinction — every *dangerous*
route to zero policies raises, so what is left is you having declared zero.

### Scope levels — which commands a policy matches

| Level | Matches when |
|-------|--------------|
| `global` | always |
| `domain` | `command.metadata["domain"]` equals the scope value |
| `team` | `command.metadata["team"]` equals the scope value |
| `entity_type` | `command.target_type` equals the scope value |

**Deny wins.** Every matching policy is evaluated, broadest first, and the first
blocking rule stops the command. Specificity orders evaluation; it is **not** an
override mechanism — a narrow `allow` cannot carve an exception out of a broad
`deny`, because the broad policy is evaluated first and returns. This is the
explicit-deny-wins posture of an IAM policy, and it is deliberate: for access
control, the conservative resolution is the safe one. Express an exception by
narrowing the `deny` rule's `operation` pattern or its scope.

`allow` rules are therefore inert — a command matching no policy is already
allowed, so an `allow` documents intent without granting anything.

### Enforcement levels — what a matching rule does

| `rule.action` | `--enforcement enforce` | `--enforcement warn` | `--enforcement audit_only` |
|---|---|---|---|
| `allow` | pass | pass | pass |
| `deny` | **block** | warn | log only |
| `require_approval` | **block** | warn | log only |
| `warn` | warn | warn | log only |

"Warn" means the command **succeeds** and the warning is returned on
`CommandResult.warnings` and recorded as `policy_warnings` on the
`MUTATION_EXECUTED` audit event. "Log only" is silent to the caller and leaves
only a structlog record. Use `warn` to stage a policy before enforcing it.

### `trellis policy add`

```bash
trellis policy add --operation <pattern> [--action ...] [--scope ...] [--enforcement ...]
```

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `--operation` | **Yes** | -- | Operation pattern: `entity.create`, `entity.*`, or `*` |
| `--action` | No | `deny` | `allow`, `deny`, `require_approval`, `warn` |
| `--scope` | No | `global` | `global`, `domain`, `team`, `entity_type` |
| `--scope-value` | No | `null` | Required for non-global scopes |
| `--type` | No | `mutation` | `mutation`, `access`, `retention`, `redaction` |
| `--condition` | No | `always` | Human-readable label; echoed in the rejection message |
| `--enforcement` | No | `enforce` | `enforce`, `warn`, `audit_only` |
| `--format` | No | `text` | Output format |

`--condition` is a **label, not a predicate** — it is not evaluated. It exists
so the rejection message explains itself to whoever hits it. Write it for that
reader.

**Example — freeze entity creation, warning first:**

```bash
trellis policy add --operation entity.create --action deny \
  --enforcement warn --condition "entity freeze pending schema review" --format json
```

Promote to blocking by removing that policy and re-adding it with
`--enforcement enforce`.

### `trellis policy list` / `show` / `remove`

```bash
trellis policy list [--format text|json]
trellis policy show <policy_id_or_prefix> [--format text|json]
trellis policy remove <policy_id_or_prefix> [--format text|json]
```

`show` and `remove` accept an ID prefix.

### What a blocked command looks like

A denied command returns a `CommandResult` with `status="rejected"` and a
message naming the condition, and emits a `MUTATION_REJECTED` event with
`payload.reason="policy_violation"`. The handler is never reached, so there is
no partial write.

That rejection is **loud by design**: it lands in the same channel
`trellis analyze health` counts (`executor_reasons.policy_violation`) and that
the MCP capture-health banner reads. A policy that blocks every write on a
surface will therefore trip the banner — that is correct, not a false positive.
A policy silently dropping writes is the failure mode this observability exists
to prevent, so check `trellis analyze health` after adding an enforcing policy.

---

## Retrieve Commands

### `trellis retrieve trace`

Retrieve a specific trace by ID.

```bash
trellis retrieve trace <trace_id> [--format text|json]
```

**JSON output (found):** Full trace JSON as defined in [trace-format.md](trace-format.md).

**JSON output (not found):**

```json
{"status": "not_found", "trace_id": "01JRK5N7QF8GHTM2XVZP3CWD9E"}
```

Exit code 1 when not found.

### `trellis retrieve traces`

List recent traces.

```bash
trellis retrieve traces [--domain DOMAIN] [--limit N] [--fields FIELDS] [--truncate N] [--quiet] [--format text|json|jsonl|tsv]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--domain` | `null` | Domain filter |
| `--limit` | `20` | Maximum results |
| `--fields` | all | Comma-separated field list |
| `--truncate` | `null` | Max chars per text field |
| `--quiet` | `false` | Suppress Rich formatting on the **text** branch. A no-op under `--format json` / `jsonl` / `tsv`, which never reach Rich (#403) |
| `--format` | `text` | Output format |

### `trellis retrieve search`

Full-text search across the document store.

```bash
trellis retrieve search <query> [--limit N] [--domain DOMAIN] [--include-chunks] [--format text|json]
```

| Argument/Option | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `query` | **Yes** | -- | Search query string |
| `--limit` | No | `20` | Maximum results |
| `--domain` | No | `null` | Domain scope filter |
| `--include-chunks` | No | `false` | Include `<parent>#chunk-N` fragment rows. Excluded by default (#396): they are slices of documents the same search already ranks. Pushed into the store, so the row cap refills with whole documents rather than the list simply getting shorter |
| `--format` | No | `text` | Output format |

> **`--format json` is safe on its own; `--quiet` is no longer needed for it.**
> It used to be. The payload printed through Rich, which rewrites `:name:` emoji
> shortcodes inside string values — `corpus:notes:<sha>` came back naming a
> document that exists in no store — and line-wraps at the console width, folding
> literal newlines into JSON strings so a wide payload did not parse at all.
> Whether `--format json` parsed depended on the terminal it ran in
> ([#403](https://github.com/ronsse/trellis-ai/issues/403)). Every machine-output
> path now writes straight to stdout, and `tests/unit/test_machine_output_rule.py`
> fails the build if a serialized payload is handed back to Rich. `--quiet` still
> does what it says for the **text** branch.

> **The text branch is safe now too** ([#492](https://github.com/ronsse/trellis-ai/issues/492)).
> It was not: `dataset:snowflake://db/schema/table` printed as `dataset❄//…`
> and a `[document]` in the same line was read as a style tag and **deleted**.
> A mangled id is not cosmetic — it is not the id, so copying it into
> `trellis retrieve get` or `get_items` fails for a reason nothing on screen
> shows. Every CLI console is now built by `trellis_cli.output.build_console`
> with `emoji=False`, and every identifier or path reaching a Rich renderer is
> wrapped in `rich.markup.escape`. `tests/unit/test_rich_id_markup_rule.py`
> derives both halves from the AST and fails the build on a new bare
> `Console()` or a new unescaped id. An error line that prints the exception
> it caught escapes that text the same way, because pydantic's
> `[type=missing, …]` and an install hint's `".[cloud]"` were deleted and a
> quoted `[/x]` raised `MarkupError`;
> `tests/unit/test_rich_exception_markup_rule.py` fails the build on a new one.
>
> Note **where the two corruptions sat in that one line**, because the fixes
> differ: the emoji was in the id and the eaten `[document]` was in the
> *preview* — document content, which is where a markdown link or a `[TODO]`
> actually lives. Escaping the id does nothing for the body text, so this
> command's result line passes `markup=False` outright, as `retrieve pack`'s
> item line already did. The AST rule polices handles; a line whose whole
> content is untrusted wants the flag.

**Example:**

```bash
trellis retrieve search "connection pool configuration" --limit 5 --format json
```

**JSON output:**

```json
{
  "status": "ok",
  "query": "connection pool configuration",
  "count": 3,
  "results": [
    {"doc_id": "01JRK5N7QF", "content": "...", "snippet": "...", "metadata": {}}
  ]
}
```

### `trellis retrieve entity`

Retrieve a specific entity by ID.

```bash
trellis retrieve entity <entity_id> [--format text|json]
```

**JSON output (found):**

```json
{
  "node_id": "01JRK5N7QF",
  "node_type": "service",
  "properties": {"name": "auth-service", "domain": "platform"}
}
```

**JSON output (not found):**

```json
{"status": "not_found", "entity_id": "01JRK5N7QF"}
```

### `trellis retrieve precedents`

List precedents, optionally filtered by domain. Excludes any precedent
whose graph node is stamped `Lifecycle.state="demoted"` or `"archived"`.
The filter applies before `--limit`, not after: a page is never shorter
than `--limit` while that many servable (non-demoted, non-archived)
precedents exist, even when demoted/archived rows sit inside the
underlying scan window.

```bash
trellis retrieve precedents [--domain DOMAIN] [--limit N] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--domain` | `null` | Filter by domain |
| `--limit` | `20` | Maximum results |
| `--format` | `text` | Output format |

**JSON output:**

```json
{
  "status": "ok",
  "count": 2,
  "items": [
    {
      "event_id": "01JRKBC1QF",
      "entity_id": "01JRKBC2QF",
      "payload": {"title": "Database pool configuration pattern", "domain": "backend"}
    }
  ]
}
```

### `trellis retrieve file-context`

Show what memory already holds about specific files — the shell-callable surface behind read-time file context (#307). Matches stored `metadata.source_path` values (exact, or a `/`-boundary suffix match in either direction where the suffix spans at least one directory, so an absolute path finds the relpath corpus ingest stored) and adds the graph entities doc-linked to those documents.

A **single-segment** stored value matches by equality only. `README.md` / `TODO.md` sit at the root of the vault and of every repo, so treating a bare basename as a suffix would answer a read of one project's file with another's notes.

**Two keys are matched, and each entry says which one hit** (#549). `source_path` is a self-description — *this is that file*. A trace summary's is `trace/<id>`, a namespace rather than a repo path, so no path rule could ever reach the records of work done *on* a file; `metadata.files_touched` — the repo paths a trace's tool calls demonstrably modified — is therefore matched too, by the same rule per member. Every document entry carries `matched_via` (`"source_path"` | `"files_touched"`), unconditionally and on both branches, and `source_path` wins when a document satisfies both. The text renderer annotates a `files_touched` hit as *record of changing this file*; parse the key, not the sentence.

Only the attested key is read. `files_touched` is the evidence-only half of #308's deterministic override, so a model's *claim* about what it edited lives under `files_touched_unverified` and is deliberately never matched — a file join is where an unattested claim would do the most damage.

```bash
trellis retrieve file-context <path> [<path>...] [--include-unconfirmed] [--format text|json|jsonl] [--quiet]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--include-unconfirmed` | `false` | Also surface unconfirmed extraction mints (#301) |
| `--format` | `text` | `text`, `json`, or `jsonl` (one object per path). `tsv` is refused with exit 2 — a path entry carries nested document and entity lists, and flattening them into cells would emit Python reprs |
| `--quiet` / `-q` | `false` | Write raw lines instead of Rich output. Use this from a hook: Rich hard-wraps at 80 columns when stdout is a pipe, splitting long absolute paths across two lines |

Batch the paths into one call — the lookup scans the document store once per call, regardless of how many paths it is given.

**JSON output:**

```json
{
  "status": "ok",
  "count": 1,
  "paths": [
    {
      "path": "src/trellis/retrieve/pack_builder.py",
      "documents": [
        {
          "doc_id": "corpus:vault:9f2c",
          "source_path": "src/trellis/retrieve/pack_builder.py",
          "source_system": "vault",
          "title": "Pack builder notes",
          "excerpt": "Gotcha: the two-stage budget truncates before scoring.",
          "created_at": "2026-08-01T09:12:00+00:00",
          "updated_at": "2026-08-14T10:00:00+00:00",
          "matched_via": "source_path"
        }
      ],
      "entities": [
        {
          "entity_id": "01JRK5N7QF",
          "name": "PackBuilder",
          "entity_type": "concept",
          "node_role": "semantic",
          "description": "Assembles token-budgeted packs",
          "document_ids": ["corpus:vault:9f2c"],
          "created_at": "2026-08-02T00:00:00+00:00",
          "updated_at": "2026-08-03T00:00:00+00:00"
        }
      ],
      "newest_item_at": "2026-08-14T10:00:00+00:00"
    }
  ],
  "graph_scan_truncated": false
}
```

`newest_item_at` is the newest stamp across a path's documents and entities, or `null` when nothing matched. It is what a client staleness gate compares against the file's mtime: an mtime newer than `newest_item_at` means the stored context describes an older version of the file.

`graph_scan_truncated` is `true` when the graph carries more doc-linked nodes than the scan cap, i.e. the entity lists may be short. It exists so "no entities" and "couldn't look" don't read the same. The scan is narrowed store-side to doc-linked nodes (`FilterClause("document_ids", "exists")`), so unlinked nodes — the bulk of a mature graph — never consume the cap.

### `trellis retrieve pack`

Assemble a retrieval pack for a given intent — the same `PackBuilder` that MCP `get_context` and `POST /api/v1/packs` run, so this is an operator-facing preview of what an agent is actually served.

That was not true until [#410](https://github.com/ronsse/trellis-ai/issues/410). The command called `DocumentStore.search` directly: one keyword axis, no graph or semantic axis, no RRF fusion, no recency/importance decay, no advisories, no token budget, and no `PACK_ASSEMBLED` event — therefore no `pack_id`, so its output could never be graded and never reached the learning loop. It returned a materially different result set from every surface an agent uses, under the same name.

```bash
trellis retrieve pack --intent <text> [--domain DOMAIN] [--agent AGENT_ID] [--max-items N] [--max-tokens N] [--format text|json]
```

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `--intent` | **Yes** | -- | Intent for context assembly |
| `--domain` | No | `null` | Domain scope. Default-pass: a memory carrying no domain is never hard-excluded |
| `--agent` | No | `null` | Agent ID recorded on the pack and its `PACK_ASSEMBLED` event |
| `--max-items` | No | `50` | Item ceiling (first budget stage) |
| `--max-tokens` | No | `8000` | Token ceiling (second budget stage) |
| `--format` | No | `text` | Output format |

**`--include-chunks` was removed by #410, not inverted.** #396 classified this command as a whole-row surface *because* it bypassed `PackBuilder`; on a pack surface the `<parent>#chunk-N` row is the retrievable unit and its excerpt is what the token budget prices, so suppressing chunks here would make the preview diverge from the agent-facing path in the opposite direction. `retrieve search` keeps the flag and its default.

**What `--quiet` actually prints, measured ([#494](https://github.com/ronsse/trellis-ai/issues/494)).** One `item_id` per line for every item in the pack — and since #410 that population is the *pack's*, not the document store's. Before #410 this command called `DocumentStore.search(..., include_chunks=False)` and `--quiet` printed whole-document ids only. The shape did not change and the contents did. Replayed over the 85 distinct intents this deployment has assembled packs for, with the shipped `build_pack_builder` wiring and the command's own defaults (4,250 lines, every pack saturating `--max-items 50`, all three axes running on all 85):

| Property | Measured |
|---|---|
| `item_type` | `vector` 42.7%, `document` 37.9%, `entity` 19.5% |
| `<parent>#chunk-N` fragments | **18.5%** of lines, in **75 of 85 packs** (median 20% of a pack, max 46%) |
| Resolves in the document store | 80.5% |
| Resolves in the knowledge graph | 19.5% |
| Resolves nowhere | 1 line of 4,250 |

**The stream is two id namespaces interleaved, and no prefix rule separates them.** `item_type` is not a namespace label — `vector` is the semantic axis's name for its own rows, and all 1,813 of them resolve in the *document* store. What actually splits is document-vs-graph, and the two prefix vocabularies overlap: document ids ran `capture:` 522, `conversation:` 424, bare 113, `corpus:` 14, `kb:` 1, while graph ids ran bare 7 and `trace:` 7. A bare id can be either. The consumers disagree about whether that matters: MCP `get_items` is type-agnostic and resolves ids against the document store, the graph and the trace store in one call, while REST is type-split (`GET /api/v1/documents/{doc_id:path}` vs `GET /api/v1/entities/{entity_id:path}`) and needs the caller to know which. **No CLI command consumes a document id at all** — an AST sweep of every `typer` command parameter in `src/trellis_cli/` finds `trace_id`, `entity_id`/`source_id`/`target_id` (entity or node, never a document), archived `item_id` from the `RETENTION_PRUNED` payload, plus `key_id` / `policy_id` / `proposal_id` / `candidate_id` / `component_id`, and nothing that takes a `doc_id`. The only command that can consume a `--quiet` line is `trellis retrieve entity`, which takes exactly the 19.5% entity minority.

**A chunk id is not URL-safe, and the failure is silent.** `#` appears in 18.5% of lines and is a URL fragment delimiter, so an unencoded id pasted into `GET /api/v1/documents/…` is truncated at the `#` by the *client* before the request leaves. Verified against this deployment: the raw form returned **HTTP 200 with the parent document** — a different document, reported as success — and only `%23` returned the chunk. Percent-encode the id, or use MCP `get_items`, which takes ids as JSON values and never routes them through a URL. `:` appears in 78.9% of lines and is safe in a path segment; `/` and whitespace appear in none.

**`--quiet` means a different population on `retrieve search`.** Same flag, same one-id-per-line shape, and `search` excludes `<parent>#chunk-N` rows by pushing the exclusion into the store — so its line count refills with whole documents rather than shrinking. Pipelines that consume both surfaces cannot assume one id vocabulary.

**The semantic axis is reported, not assumed.** `build_strategies` adds it only when an embedder resolves, and drops it with an `info` log line the CLI's `WARNING` default never prints — except when resolving the embedder itself raises (Q5-A, #838), which logs `semantic_search_embedder_resolve_failed` at `WARNING`, once per distinct cause per process, so it prints at the default level. The `axes` block says which of five things happened:

| `axes.semantic` | Meaning |
|---|---|
| `ran` | The axis exists and ran |
| `not_configured` | No `embeddings:` provider and no `TRELLIS_EMBEDDING_FN` — this pack is keyword + graph only |
| `misconfigured` | An embedder resolved but the vector backend failed to initialise |
| `embedder_failed` | `registry.embedding_fn` raised instead of resolving — keyword and graph still run; `axes.embedder_error_type` names the exception type and, for a `ConfigError`, `axes.embedder_setting` names the setting, never the message |
| `failed` | The axis exists and raised during *this* build; see `PACK_ASSEMBLED.strategy_failures` |

Text output prints a sentence for every state but `ran`.

**The graph axis's unseeded window is off by default** (#961). When nothing seeds the graph axis, neither an entity the intent names nor a section's `entity_ids`, it used to serve the newest ~80 nodes whatever the intent was (`metadata["graph_selection"] == "recency_window"`). It now serves nothing unless `TRELLIS_GRAPH_RECENCY_FALLBACK` is truthy (`1`/`true`/`yes`/`on`) or the `graph_recency_fallback` setting is `true`; env > settings > default off, and an unrecognised env value warns and reads as off, whatever the setting says. Seeded and caller-seeded searches are unchanged. The reason is in the pack's `axes` block, on every surface that returns one (`POST /api/v1/packs`, `/packs/sectioned`, `trellis retrieve pack --format json`). MCP returns markdown only, so it has no `axes` block and renders no line for this state; an MCP caller sees a withheld graph axis only as the absence of graph items:

| `axes.graph` | Meaning |
|---|---|
| `ran` | The axis ran (it may still have found nothing) |
| `unseeded_fallback_off` | The axis ran and served nothing because no seed resolved and the recency window is off. It is also in `axes.ran`: it did not fail |
| `failed` | The axis raised during this build |
| `absent` | The builder has no graph strategy at all (#985), so a pack with no graph items cannot be read as an empty graph. It is in neither `axes.ran` nor `axes.failed`. No shipped surface builds one, since every surface goes through `builder_factory`, which always includes the graph strategy |

There is no markdown note for `unseeded_fallback_off`: it follows from what the intent named, not from a deployment fault, and a line on every such pack would be one that always prints. A held-out pack carries the same value, so it stays indistinguishable from a served one. Historical `PACK_ASSEMBLED` events keep their `graph_selection` stamps, so `trellis analyze replay --graph-selection recency_window` and `analyze value`'s `by_graph_selection` still read them. The event records the state too, as `graph_axis_state` (same value, same `trellis.retrieve.strategies.resolve_graph_axis_state`), on flat, sectioned and held-out rows; an event from before that key shipped has none, and a withheld pack in one shows only by the absence of graph items.

**Example:**

```bash
trellis retrieve pack --intent "deploy checklist for staging" --domain platform --max-items 10 --format json
```

**JSON output** (same shape as `POST /api/v1/packs`, plus `intent_family` and `budget`):

```json
{
  "status": "ok",
  "pack_id": "01M1JRS7FABTXV96WKFT76GR0R",
  "intent": "deploy checklist for staging",
  "domain": "platform",
  "agent_id": null,
  "intent_family": "general_context",
  "count": 5,
  "items": [
    {
      "item_id": "01JRK5N7QF",
      "item_type": "document",
      "excerpt": "# Deployment Guide ...",
      "relevance_score": 0.0164,
      "rank": 1,
      "strategy_source": "keyword",
      "estimated_tokens": 100,
      "score_breakdown": {"rrf_keyword": 0.0164, "rrf_total": 0.0164},
      "metadata": {"recency_clock": "row"}
    }
  ],
  "advisories": [],
  "retrieval_report": {"strategies_used": ["keyword", "graph"], "candidates_found": 55, "items_selected": 5, "rejected_items": [], "budget_trace": []},
  "budget": {"max_items": 10, "max_tokens": 8000},
  "withholding": {"total": 5, "by_reason": {"max_items": 5}, "withheld_item_ids": ["01JRKA…"], "non_absence_reasons": [], "section_filtered": 0, "served_count": 5, "class_prior_detail": []},
  "axes": {"available": ["keyword", "graph"], "ran": ["keyword", "graph"], "failed": [], "semantic": "not_configured", "graph": "ran", "embedder_error_type": null, "embedder_setting": null}
}
```

`withholding` is the builder's stamped summary verbatim, so it carries `withheld_item_ids` as well as the counts — #404's counts-and-reasons-only rule scopes the *rendered note* an agent reads, not this payload, whose reader already holds the stores. `retrieval_report` is abbreviated above; it also carries `queries_run`, `duration_ms` and `schema_version`, and its `rejected_items` / `budget_trace` are populated on any pack that hit a budget. `class_prior_detail` breaks the `class_prior` row in `by_reason` (when present) down by document form and retrieval axis — `[{"form": "conversation", "axis": "keyword", "count": 2}]` — and is always present (empty list when the gate withheld nothing, or on a pack it never ran against); the rendered note (`format_withholding_note`) uses it to name the form and axis instead of printing the bare reason code.

**`POST /api/v1/packs/sectioned` carries the same `axes` block** (#783). `SectionedPackResponse.axes` is the identical `{available, ran, failed, semantic, graph, embedder_error_type, embedder_setting}` shape, read off the first section's `strategies_used` — `build_sectioned` runs every strategy once and gives every section that one list, so the first stands for all. `sections=[]` is refused before any build with `422` and `detail: "sections must not be empty"`, the reason MCP's `get_context` / `get_sectioned_context` give for the same input, so every `200` has a section to read `axes` off.

**MCP renders a markdown line, not a JSON block.** `get_context`, `search`, and — since #789 — `get_context(sections=...)`, `get_objective_context`, `get_task_context` and `get_sectioned_context` all add one line naming any axis in `axes.failed`, to both a populated and an empty reply:

```
**Retrieval axis failed:** <names>.
```

Axis names only, never the exception text — that stays in `PACK_ASSEMBLED.strategy_failures`. A reply with no failed axis has no such line.

A `misconfigured` semantic axis (an embedder resolved but the vector backend never initialised) never lands in `axes.failed` — it's absent from `axes.available` entirely, so the line above says nothing about it. The same tools add a second, independent line for that state:

```
**Semantic retrieval misconfigured:** the vector store did not initialise, so this pack has no semantic results.
```

Same facts as the CLI's `misconfigured` sentence (`SEMANTIC_AXIS_NOTES`), never the exception text. A failed axis and a misconfigured semantic axis are independent states a single build can hit together, so both lines render when both apply; a reply with neither has no such line.

An `embedder_failed` semantic axis (Q5-A, #838 — `registry.embedding_fn` itself raised, before any strategy was built) adds a third, independent line:

```
**Semantic retrieval unavailable:** the embedder failed to resolve (<error_type>[: <setting>]), so this pack has no semantic results. Fix the setting, then run `trellis admin reindex-vectors`.
```

`<error_type>` and, for a `ConfigError`, `<setting>` name the cause, never the exception message. All three lines are independent and render together when more than one applies.

**The Python SDK renders the same three lines from the response's `axes` block.** `TrellisClient.get_objective_context` / `get_task_context`, sync and async, pass it to `trellis_wire.axes.axis_note_from_payload`, which renders MCP's wording through the same three formatters (`format_failed_axes_note`, `format_misconfigured_semantic_note`, `format_embedder_failed_note` — defined in `trellis_wire.axes`, the first two re-exported from `trellis.retrieve.builder_factory`). A response without `axes`, or with `axes: null`, renders no line.

> **This is a CLI contract change.** Before #410 the payload was
> `{"status", "intent", "domain", "agent_id", "count", "include_chunks", "items"}`
> with `items` a flat list of **doc-id strings**. `items` is now a list of
> **pack-item objects**; scripts reading ids want `[i["item_id"] for i in items]`,
> or `--quiet` without `--format json`, which still prints one id per line.
> **`--quiet`'s shape survived that change and its population did not** — the
> ids are now the pack's items, so chunk fragments and entity ids appear where
> only whole-document ids used to. See *What `--quiet` actually prints* above
> before treating an existing pipeline as unaffected.

> **`--format json` is safe on its own** — the Rich mangling described under
> `retrieve search` above is fixed ([#403](https://github.com/ronsse/trellis-ai/issues/403)),
> so `--quiet` is no longer required to parse the output. The text renderer is
> safe too: this command's item line disables Rich markup and emoji outright,
> and since [#492](https://github.com/ronsse/trellis-ai/issues/492) the same
> guarantee holds for every id-bearing line in the CLI — an `item_id` like
> `dataset:snowflake://…` is printed, not emojified.

#### `PACK_ASSEMBLED` event payload

Every `PackBuilder` build with an `event_log` configured emits one `PACK_ASSEMBLED` event with `entity_id` = `pack_id`. It is the read side of three loops — `trellis analyze pack-telemetry` reads the rejection/budget/strategy keys, the learning join in `learning/pack_observations.py` joins it to `FEEDBACK_RECORDED` on `pack_id`, and `trellis analyze health` reads `strategy_failures[]` for its failed-strategy count, per-strategy breakdown and latest-occurrence timestamp. It also groups the window's served packs (the holdout dropped, so the groups sum to `serve.packs`) by the `(version, commit)` in each row's `metadata["write_provenance"]` into `serve.serving_builds`, and warns `mixed builds` when two builds served concurrently: the newest build (the group whose first pack is latest, not the one that served the last pack) began at some instant, and another group assembled a pack at or after it, so an old build kept serving once a newer one had started. A clean redeploy, where the old build's last pack precedes the new build's first, does not warn however many days of both the window holds. `serve.serving_builds.packs_from_other_builds` counts only those concurrent packs. A row with no stamp or a null commit is its own labelled group, never dropped, and follows the same rule.

**Two payload shapes, told apart by `entity_type`.** `build()` emits `"pack"`; `build_sectioned()` — behind `get_objective_context` / `get_task_context` / `get_sectioned_context` — emits `"sectioned_pack"` with a *different* key set (below). Branch on `entity_type`, or read with `.get()`. `_emit_telemetry` / `_emit_sectioned_telemetry` in `retrieve/pack_builder.py` are the exhaustive key lists; the table covers the flat payload's non-obvious semantics.

| Key | Notes |
|-----|-------|
| `run_id` | **Request-scoped attribution.** The unit of work the pack was served to — narrower than `session_id`. The learning join prefers the feedback payload's `run_id` and falls back to this one; when both are absent the observation buckets under `unknown-run`. |
| `project` | The MCP server's project, by the rule in [trace-format.md § Project stamp](trace-format.md#project-stamp); a stdio server resolves it once per process. Always present; `null` when unknown and on the REST and CLI surfaces. Filter with `get_events(payload_filters={"project": ...})`. |
| `intent_family` | Canonical intent bucket, read by the metrics `group_by=intent_family` axis. Never null on a `PackBuilder`-assembled pack: when the caller supplies none it is derived via `normalize_intent_family(intent=...)`, which falls back to `general_context` rather than an empty value. |
| `injected_item_ids` | The served set — and the **fallback** source for the `reference_rate` metric, not the first one: `compute_timeseries` reads the feedback payload's `items_served` and only uses this when that is empty. `PackFeedback.from_agent_signal` deliberately leaves `items_served` empty (cited ids are what the agent *referenced*, not what the pack contained), so agent-graded packs do land here. |
| `injected_item_hashes` | `{item_id: hash}` of each item's **excerpt**, so a later build in the same session can re-serve an item whose content changed rather than suppressing it as already-seen (#258). Additive — older events without the key fall back to id-only suppression. |
| `injected_items[]` | Per-item detail — `item_id`, `item_type`, `rank`, `selection_reason`, `score_breakdown`, `document_form` (`trellis.schemas.document_metadata.document_form_of`, omitted when absent), `estimated_tokens`, `strategy_source`, `injected_advisory_ids`, plus the **attribution fields** below. This is the per-item row the learning join reads, and `trellis.retrieve.pack_replay.replay_class_prior` reads `document_form` off it to grade the class-prior gate counterfactually over already-served items — it does not re-run the budget walk, since `budget_trace[]` carries no form for non-served candidates. Under `TRELLIS_CLASS_PRIOR_SCOPE=parents_and_chunks` a chunk's row carries no form (nor does any historical row), so replay resolves it through the document store as it is *now*: for a chunk id on a configured axis (any `item_type`, `vector` included), the stored row's `parent_doc_id` (else the id's prefix, as serving reads it), then that parent's `document_form`, memoized (one `get` per distinct id and parent); a non-chunk id is never looked up; the report adds `matched_via_parent`, `parent_lookups`, `parent_form_resolved` and `parent_form_unresolved`, and an unresolvable row counts as not matched. Price it with `TRELLIS_CLASS_PRIOR_MODE=withhold TRELLIS_CLASS_PRIOR_SCOPE=parents_and_chunks trellis analyze replay --class-prior --format json` (add `TRELLIS_CLASS_PRIOR_AXES=keyword,semantic,graph` for every axis). The gate is axis-scoped, so a matched row for which the off pack has a dedup row for a copy of the same id on an axis outside `config.axes` (see `rejected_items[]` below) is counted under `still_served_by_other_axis`, not as removed: withhold would let that other copy win the pack instead. |
| `rejected_items[]` | `{item_id, item_type, relevance_score, reason, strategy_source, estimated_tokens}`. `estimated_tokens` is the budget walk's charge for the item and is set only on the two budget cuts, `max_items` and `token_budget`; it is `null` on every other reason and absent from an event written before the field existed (read both as unpriced, never zero). A `max_items` row never reaches the walk, so `budget_trace[]` lists it nowhere and this is the only record of its cost; it is priced as the walk would have charged it (the excerpt, or the index line in `index_mode`), not as a graduated-disclosure pointer, since disclosure runs after the cut. A sectioned pack emits no `rejected_items[]` payload. Known `reason` values: `dedup`, `archived`, `noise`, `structural_filter`, `meta_activity_filter`, `max_items`, `token_budget`, `session_dedup`, `semantic_dedup`, `content_floor`, `superseded`, `class_prior` — `archived` and `noise` are the collect-seam gates #404 started recording; `superseded` is the pairwise gate below; `class_prior` is `class_prior_mode="withhold"` dropping an imported-conversation-document candidate, a class-level prior rather than a per-item judgment (`trellis.retrieve.class_prior`), off by default. Withholds conversation parent documents; their chunks are untouched unless `TRELLIS_CLASS_PRIOR_SCOPE=parents_and_chunks` (env-only, default `parents`; an unrecognised value degrades to `parents` with a `class_prior_scope_env_unrecognised` warning), which also withholds a chunk whose *parent* carries a configured form. Only a chunk id (`<parent>#chunk-N`) resolves through a parent, by the same `ParentVerdictCache.resolve` call the archived and noise gates make (the item's `parent_doc_id`, else the id's own prefix for a vector snapshot that predates that key), so it adds no document-store read and a plain item that merely names a parent is not withheld; an unresolvable parent serves the item. The rejection reason and the `withholding` summary are the same, and the summary does not tell a chunk from a parent. On a sectioned pack this gate's rejections have no single section to be blamed on, so they are not copied into any `PackSection.retrieval_report` — they land in `SectionedPack.metadata["withholding"]` instead, same as the other pool-level gates. Rows are per **(strategy, item)**, so one id retrieved by two axes and gated on both appears twice; the `withholding` summary counts distinct ids and is the per-item number. |
| `class_prior` | `{mode, forms, axes}`, plus `scope` only when it is not the default `parents` (absent reads as `parents`, which covers every row written before #986) (`trellis.retrieve.class_prior.class_prior_telemetry`) — the gate's own *configuration*, not its outcome; the outcome is the `class_prior` rows above and in `withholding`. `forms`/`axes` are sorted lists (JSON has no frozenset). Emitted on every flat and sectioned build, including `mode: "off"`, so a reader can tell whether the gate was on for this pack without re-deriving it from env/settings state that may have since changed. |
| `content_floor` | `{mode, min_distinct_words, penalty, exempt_item_types, penalized_count, penalized_item_ids, excluded_count, excluded_item_ids}`. Emitted even when nothing tripped, so "floor ran, nothing thin" is distinguishable from "floor never ran". |
| `index_mode` | `true` when the pack was assembled for the one-line-per-item index rendering (`get_context(index=True)` / `search(index=True)`, #305) instead of excerpt bodies. Always present on packs built after #305; read it with a `False` default. **Branch on it before aggregating token numbers** — see the two rows below. |
| `retrieval_phase` | `"pre_work"`, `"mid_task"`, `"post_hoc"`, or `null` when the caller stated none. Always present on packs built after this field was added; `null` is not "unknown", it is "not stated". A `post_hoc` pack — most often a stop hook retrieving after the work is already done — is a hindsight relevance check, not a pack that could have changed the outcome; exclude it before grading retrieval against a holdout. |
| `refresh` | `true` when the caller bypassed session dedup for this call (`get_context(refresh=True)`, #258). Always present, `false` by default — so an unchanged re-serve (`refresh=false`, same content) is distinguishable from a dedup miss (`refresh=true` forcing re-injection). |
| `budget_trace[]` | One `{item_id, item_tokens, running_total, included}` row per candidate the token stage weighed, against the `budget_max_items` / `budget_max_tokens` alongside it. `item_tokens` is whichever cost was *paid*: the item's excerpt normally, its rendered index line when `index_mode` is `true`, and a one-line pointer for a row graduated disclosure demoted (disclosure runs after the walk and the builder re-prices the served steps to what the pack carries). `walk_tokens` is the charge the **walk** made, written only on a pack where disclosure demoted something (on every included step of it); when the key is absent, `item_tokens` is the walk's charge, which is also how every event written before the field reads. `trellis analyze replay` re-runs the walk over the walk's charges: a `max_items`-cut row (`rejected_items[].estimated_tokens`) is priced at the walk's charge, never a pointer's, so summing pointer prices against it would free more budget than the pack ever paid. The graph-selection replay refuses an older event whose `disclosure.pointer_item_ids` names an included step with no `walk_tokens` (counted unmodelled as `disclosure_repriced`), and refuses a step whose `item_tokens` or `walk_tokens` is missing, fractional, `nan` or `inf` (`trace_unreadable`). |
| `token_*` | `token_counter`, `token_budget_safety_margin`, `token_budget_effective`, `token_total_estimated` always. `token_counter_validator`, `token_total_validated`, `token_count_delta`, `token_count_delta_pct` **only when a `token_budget_validator` is configured** — check before reading. `token_total_estimated` sums the same charge `budget_trace[].item_tokens` records, so on an `index_mode` pack it is index-line tokens, not excerpt tokens — averaging it across a mixed corpus mixes two units. The per-item excerpt read cost stays on `injected_items[].estimated_tokens` in both modes. |
| `strategy_failures[]` | Strategies that raised but did not block the build (a sibling succeeded). Empty on a clean build. Each row's `message` is sanitized, bounded, and never a raw `str(exc)` that could echo a DSN or credential into this stored event — but the exact derivation differs by caller: most rows (a strategy's own init or search raising) are `summarize_exception(exc)["message"]`, computed by the `_audit_message` helper, which falls back to the suppression marker if summarizing itself raises; the one in-tree exception is the embedder-resolve row (`EmbedderResolveFailure`, #838), whose `message` is `<ErrorType>[: <setting>]` — a type name and a config key, never driver text. |
| `malformed_row_skips` | `{strategy: rows}`: stored rows a strategy skipped because their metadata could not be read, each one also listed in `rejected_items` as `malformed_metadata` on a flat pack (a sectioned pack has no `rejected_items`; its withholding summary counts the skip). `{}` on a clean build, and present on a held-out row, which still ran its strategies. `trellis analyze health` tallies it over every scanned pack under `serve.malformed_rows` (packs, per-strategy counts, latest) and adds a `warn` reason while it is nonzero; counts and strategy names only, never a row id or value. |
| `graph_axis_state` | `ran`, `failed`, `unseeded_fallback_off` (#961) or `absent` (#985): the same value as the response's `axes.graph`, on both pack shapes and on a held-out row, which carries the build's state, not the empty pack the caller received. `unseeded_fallback_off` means the graph axis ran and served nothing because no seed resolved and `TRELLIS_GRAPH_RECENCY_FALLBACK` is off, so a pack with no graph items can be told from a graph with nothing in it. `absent` means the builder had no graph strategy at all, which no shipped surface builds. Additive; the key is missing from events written before it shipped, so read it with `.get`. |
| `meta_filtered_count` | Graph nodes dropped by the default meta-`Activity` filter — the same drops `rejected_items` records under `meta_activity_filter`. **Not a count of meta-Activities suppressed.** Since #375/#436 the recorder mints them `node_role="structural"` and `GraphSearch` drops them *inside the strategy*, before the candidate slice, so they never become candidates and this field reads `0` while suppression happens on every pack. It counts only what reached `PackBuilder`: pre-#436 `semantic` rows, and any meta-Activity reached by a non-graph axis. |
| `holdout` / `holdout_rate` | The pack holdout (`TRELLIS_PACK_HOLDOUT_RATE`, default `0`): whether this pack was withheld from its caller, and the rate in force when it was drawn. On every build of both pack kinds, flag off included, so a served row (`false`) is told apart from one an older build wrote (key absent). Read it as `payload.get("holdout") is True`. See [the exception to stated withholding](#withholding-is-stated-not-implied-404). |
| `holdout_items[]` / `holdout_sections[]` / `holdout_advisory_ids` | Only on a withheld pack: the would-be pack, its items in the `injected_items[]` row shape (sectioned: `holdout_sections[]` in the `sections[]` shape), and its advisory ids. Every other key describes the empty pack the caller received, so `injected_items`, `injected_item_ids` and `advisory_ids` are empty and no reader counts a withheld item as served. |

Plus the scope keys `intent` / `domain` / `agent_id` / `session_id`, the roll-ups `items_count` / `strategies_used` / `candidates_found`, and `advisory_ids` / `reranker` / `semantic_dedup_enabled` / `semantic_dedup_rejected`, all as named.

`advisories_matched` (#392) is how many advisories matched the pack's scope **before** the delivery cap cut them; `advisory_ids` is what actually rode the pack, at most `PackBuilder._ADVISORY_MAX_COUNT` (5). Equal whenever the cap did not bind, so a widening gap between the two is the signal that the advisory corpus is outgrowing what a pack can carry. Emitted on both pack kinds.

`advisories_filtered_legacy` (decision-ledger D-4, option B) is how many scope-matching advisories were withheld for predating the #394 generator repair — rows with no `evidence_confidence`, dropped before ranking and the cap, so a legacy row can never occupy a slot a post-repair row would have won. `advisories_matched` is counted *after* this filter. Controlled by `TRELLIS_FILTER_LEGACY_ADVISORIES` (default on, same accepted/rejected-value vocabulary as `TRELLIS_GRAPH_SEEDING`); `0` when the knob is off. Emitted on both pack kinds.

**The sectioned payload is not a superset.** It carries `section_count`, `total_items` and `sections[]` (`{name, items_count, item_ids, injected_advisory_ids}`) *instead of* `items_count`, `injected_item_ids`, `injected_items[]`, `strategies_used`, `candidates_found`, `budget_max_items`, `budget_max_tokens`, `budget_trace[]` and `rejected_items[]` — none of which it emits. Everything else above is shared. Two consequences to know before reading the numbers: `trellis analyze pack-telemetry` counts a sectioned pack as a **0-item pack**, because it reads `injected_item_ids`; and since the learning join reads `injected_items[]`, a sectioned pack yields **zero per-item rows** — `run_id` and `intent_family` are emitted there for symmetry but stay inert until that gap is closed.

**Attribution fields (`title` / `category` / `domain_system`).** Added in #285 because the promotion candidates written to `intent_learning_candidates.json` previously carried only opaque `item_id`s. All three are derived from metadata the strategies already attach — nothing new is computed. `title` reads `title`, then `capture_title` (what the session-capture ingest writes), then `name`. `category` is the `ContentTags.content_type` facet and **only** that: a flat `metadata["content_type"]` is deliberately not read as a fallback, because ingest handlers stamp their own vocabulary there (`"conversation"`, `"entity_summary"`), and mixing the taxonomies would make the column ambiguous. An empty value is omitted rather than emitted as `null`, so thin items keep the pre-existing payload shape.

**Graph-row fields (`node_type` / `node_role`).** Added in #375 (gate 4). Items sourced from a graph node — the `graph` and `observation` axes — also carry the row's stored `node_type` and its `node_role` (`structural` / `semantic` / `curated`). **Only graph-sourced items carry them, and absence is recorded by omitting the key**, the same convention the fields above use: a document has no `node_role`, and filling one in as `"semantic"` would make a per-role split a statement about the default rather than about the corpus. Before this, splitting the graph axis's citations by type or role required joining every served `item_id` back to `nodes WHERE valid_to IS NULL` — a mutable table being asked to describe a past serving, which answers for a different row once a node is deleted, re-typed or re-minted. The graph axis's value is sharply concentrated by type (see `docs/design/plan-375-graph-candidates.md` §2.1), so that split is a standing question, not a one-off. `graph_selection` (`seeded` / `caller_seeded` / `recency_window`, #371, #950) sits alongside them and answers who chose the candidates: `caller_seeded` means a sectioned pack's `entity_ids` named an entity the graph holds, and only a sectioned pack can carry it. Since #958 a sectioned pack's `injected_items[]` forwards it like a flat one's, each row adding `section` (the section's name) and a `rank` that is 1-based within that section, so read the two together; rows run sections in request order, then served order, and an `item_id` appears once per pack. A sectioned row carries no `injected_item_ids` beside it, so readers of that key (`analyze_effectiveness`, advisory entity correlation, the MCP feedback gate) still see no membership for a sectioned pack. A separate key, `graph_slice_prioritised: true` (#949), is set on the items of a seeded search whose neighbourhood was larger than the axis limit and whose kept slice changed because seeds and the `precedent_applies_to` precedents that apply to them (demoted or archived ones excepted) were ordered ahead of the rest; it is absent otherwise, never `false`, and it sits on the item's metadata rather than in `injected_items[]`. It applies to either seeded value, so a caller-named hub gets the same precedent partition as a derived one, and it is never a third `graph_selection` value. Read the pair beside `strategy_source` rather than instead of it: a vector row written from a graph node (the Neo4j shape-#2 layout, where the vector `item_id` *is* the `node_id`) carries them onto a `semantic` item legitimately.

**Recency clock (`recency_clock`).** Added in #465. Items from the two document-backed axes (`keyword`, `semantic`) carry which clock recency decay actually read for them: `source` (the metadata bag's stamp — the content's own clock, propagated by an ingest path that knew the content predates its write), `row` (the store row's write-clock column), or `none` (no usable candidate, so the item was scored **undecayed** — the *maximum* multiplier, not a neutral one). The three are far apart: `source` vs `row` was a median 2.20x (max 3.17x) in the resulting multiplier across the 148 rows #417 measured, and `none` fails open at `1 / floor` = 3.3x, so an item's rank can be dominated by which branch fired. `none` is the value worth querying — it is how the semantic-axis residual #462 left open (`VectorStore.query` returns no columns, so a document whose own `created_at` is malformed or in the future has nothing to fall through to, while the keyword axis floors the same row off its column) shows up as a property of served records rather than as a code-reading finding. **Only those two axes carry it, and absence is recorded by omitting the key** — the same convention as `node_role` above; a graph or observation item labelled `row` would describe the filler rather than the corpus. It reveals nothing about content: three enum values naming a mechanism.

#### Pack excerpts and the content floor

Excerpts are cut at a **sentence boundary, else a word boundary, else hard** — never mid-word — and the cut is marked `… [+2.3k chars]`, naming how much was dropped so a consumer can judge whether fetching the full source is worth the tokens (#310). A boundary is only honoured if it retains at least half the budget, so a leading `"Note. "` cannot gut an excerpt. The 500-character cap (`EXCERPT_MAX_CHARS`) matches the raw slice this replaced, so per-item token estimates did not shift.

Three things to know about the size note. It is charged **against** the cap, not added on top of it — a truncated excerpt gives up ~14 characters of body to carry it, and is still never longer than the raw `text[:500]` slice it replaces. Its count is exact: the truncator reserves the widest possible note before choosing the cut, so what it quotes is what was actually dropped. And `count_substance_words` strips it before measuring, so it can never lift a name-only stub over the content floor below. Limits under 100 characters (`ELISION_NOTE_MIN_LIMIT`) get the bare `…` instead — at those budgets the note would crowd out the text it annotates.

The **content floor** handles substance-free items — the name-only graph stubs whose "excerpt" is a three-word entity name. An item with fewer than `min_distinct_words` (default `5`) distinct words in its excerpt is, by default, **demoted, not dropped**: its `relevance_score` is multiplied by `0.35`.

| Knob | Default | Notes |
|------|---------|-------|
| `mode` | `penalize` | `penalize` \| `exclude` \| `off`. Set via `PackBuilder(content_floor=ContentFloorConfig(mode=...))`; there is no env var or config key. |
| `min_distinct_words` | `5` | Sits in the gap between a name-shaped stub (1–4 tokens) and the tersest real memory (a full clause, ~9 words). |
| `penalty` | `0.35` | Multiplier in `penalize` mode. |
| `exempt_item_types` | `{"observation"}` | Item types whose excerpt is structured rather than prose — a Measurement excerpt is `"row_count = 41823"`, complete and two words by construction. |

Why the default demotes rather than excludes: the shortest genuinely useful memory this system serves — a one-line gotcha, a two-clause procedure — is exactly the shape a hard length filter would silently delete. A multiplicative penalty pushes a stub behind anything substantive while leaving it eligible when the candidate pool is thin. Either way the decision is observable — `PACK_ASSEMBLED.payload["content_floor"]` above, plus `content_floor_penalty` / `content_floor_substance_words` on the item's `score_breakdown`; `mode="exclude"` additionally emits a `rejected_items` row with reason `content_floor`.

#### Withholding is stated, not implied (#404)

A pack that withheld candidates now says so, in one line **in its header**:

```
**Withheld:** 3 items matched this intent but were not served (noise 2, archived 1). Counts only — no ids or content.
```

It renders on every pack surface a caller reads — `get_context` (full and `index=True`), `search`, `get_task_context` / `get_objective_context` / `get_sectioned_context` — and, most importantly, on the **empty** pack, where `No context found for: …` is otherwise identical whether the corpus was empty or every match was demoted.

Three properties worth knowing:

- **Withheld means absent, computed as `{rejected ids} − {served ids}`.** A rejection is not an absence: the losing copy of a dedup, or a row one axis gated and another served, is *on screen* and is never reported as withheld. Reasons seen only on such rejections are recorded under `non_absence_reasons` in telemetry, so a `total` of zero reads as "nothing was withheld", not "nothing was rejected". An id removed by more than one gate is attributed to the **first** one.
- **Counts and reasons only — never ids, never excerpts.** Naming the ids would invite a re-fetch of exactly what a gate decided not to serve. The ids stay in `PACK_ASSEMBLED.payload["withholding"]["withheld_item_ids"]`, which is an operator surface with a different access path.
- **Graduated disclosure (#359) contributes nothing here.** A pointer is a *served* item — ranked, cited, fetchable via `get_items` — and calling it withheld would blur the distinction that change was careful to make.

**One deliberate exception: the pack holdout.** `TRELLIS_PACK_HOLDOUT_RATE` (default `0`, off) withholds a whole built pack, items and advisories, from a random share of retrieval calls, so pack effect can be measured against a control arm. A pack is held out when a SHA-256 draw on its `pack_id` falls below the rate, so its arm is fixed by the id and is the same in every process. The caller receives an ordinary empty pack with its `pack_id` and no `Withheld:` line, because an agent that can tell it is in the control arm is no longer a control; a held-out pack reads exactly like a greenfield one. The record is complete on the operator path: see the `holdout` keys in the [`PACK_ASSEMBLED` payload table](#pack_assembled-event-payload). `GET /api/v1/events`, `GET /api/v1/events/{event_id}`, `GET /api/v1/packs` and `GET /api/v1/packs/{pack_id}` require the `admin` scope to call at all, so the would-be pack they return is never blinded — every caller who reaches these routes already has `admin`. The aggregate readers of `PACK_ASSEMBLED` (promotion, effectiveness and advisory fitness, advisory generation, pack telemetry and sections, serve attribution) drop held-out packs and the feedback naming them, so they report the served arm. At every rate, including the default `0`, `get_context` (full and `index=True`) and `search` render every empty pack with its `pack_id` header and feedback footer rather than a bare `No context found for: …` (or `No results found for: …`) line, so a genuinely empty pack can be graded, the capture join reads both kinds of empty pack alike, and a held-out pack and a greenfield one still look the same. Each serving process reads the rate from its own environment on every build, so set it where the MCP server and the API run. `trellis admin write-config --format json` and `GET /api/version` report it, and an unparseable or out-of-range value means `0` with a warning (`pack_holdout_rate_unparseable` / `pack_holdout_rate_out_of_range`). A [settings-store](#settings-store) override reaches it at runtime (`SettingSpec.settings_live`, true for this knob and every other registered one — see "Precedence: env > settings > default" above); both surfaces' `overridden_by` names whether `env`, `settings` or `default` is in force. Why the exception: [`claude-md-rationale.md`](../design/claude-md-rationale.md).

`PACK_ASSEMBLED.payload["withholding"]` is emitted on both pack kinds **even when nothing was withheld** (`{"total": 0, "by_reason": {}, ...}`), so an analyzer can tell "the summary ran and found nothing" from "the summary never ran". The two collect-seam gates — `signal_quality="noise"` and `Lifecycle.state="archived"` — now write `rejected_items` rows (reasons `noise` and `archived`) where previously their only observable was a `logger.debug` line that fires under **no shipped default** (`configure_stderr_logging` pins INFO, the CLI pins WARNING). `trellis -vv` or `TRELLIS_LOG_LEVEL=DEBUG` does enable it — which is precisely the point: the signal existed only for someone who already suspected the problem and went looking.

A third gate runs **after** collection, over the whole pool (#613): an item whose `Lifecycle` is `superseded` is withheld under reason `superseded` only when the `superseded_by` it names is itself a candidate — by `item_id`, or as a graph node whose `evidence_ref` property is that document id. With the successor absent the loser is served as before (recency-demoted, fetchable); a malformed stamp never hides; a supersession cycle is served whole with one `supersession_cycle_kept` warning. The gate runs before the budget walk, so a successor that is a candidate but is then cut by `max_items` / `max_tokens` (or by the rendered pack's own token budget) leaves neither version served; the withholding note still counts the loser under `superseded`.

The stamp's first deterministic producer is an explicit declaration by the agent that wrote the replacement (#613). `save_memory(content, supersedes=T)` marks document `T` superseded by the memory it stores — or, on an exact-content hit, by the document that already holds that content. `save_knowledge(name, content=… or evidence_ref=…, supersedes=N)` marks entity `N` **and** `N`'s evidence document superseded by the *new entity's evidence document*, because that document id is what a pack carries for the new version and what the gate matches. Both stamps are governed writes: `evidence.ingest` for a document (metadata-only — the content goes back unchanged, the write clock is kept, nothing is re-embedded, and the lifecycle is mirrored onto the vector row) and `entity.update` for a node, each with `requested_by` set to the calling surface (`mcp:save_memory` / `mcp:save_knowledge`). Every check runs before the first write, and a refusal writes nothing and says so: an id that is not a document (`save_memory`) or not an entity (`save_knowledge`), an archived target (the stamp replaces the whole `lifecycle` value, so stamping would un-archive it), a target already superseded by something else, a self-reference, a revert that would close a cycle the gate keeps whole, and — for `save_memory` only — a target that is part of a chunked document (its chunks are separate rows the gate would keep serving) or content that near-duplicates a memory other than the target. `save_knowledge` does not refuse over `N`'s evidence document: one that is missing, chunked, archived or already superseded by something else is left unchanged and named in the reply, and `N` is still stamped. On `save_knowledge` the stamps go on once the new evidence document exists and before the entity is created, so a failure never leaves a new entity beside an old version that is still served. The stamps carry no idempotency key and check state instead: re-sending a call writes nothing already in place, finishes a call that failed partway, and re-applies a stamp an operator has undone. An explicit `supersedes=` bypasses the reconcile-on-write judge (`TRELLIS_ENABLE_RECONCILE_ON_WRITE`), since the caller has already said what the write replaces. The parameter is MCP-only; REST, the CLI and the SDK have no equivalent yet.

Undoing a stamp:

- **Entity:** `execute_mutation("entity.update", {"entity_id": N, "properties": {"lifecycle": {"state": "current"}}})` — the same operation and top-level property merge as the stamp. It leaves `lifecycle` set to `current` rather than removing the key, which the gate reads exactly as an unstamped node. **Not for a precedent node**: `entity.update` refuses any write to `properties.lifecycle` where the current node type is `"precedent"`, naming `precedent.demote`/`precedent.reinstate` as the way to change it instead — those are the only path for a precedent, supersession stamp or not.
- **Document (operator-only):** read the metadata bag with `GET /api/v1/documents/{doc_id}`, set its `lifecycle` to `{"state": "current"}`, and submit `execute_mutation("evidence.ingest", {"evidence": {"doc_id": …, "content": <unchanged>, "metadata": <bag>, "preserve_updated_at": true}}, idempotency_key=<fresh>)`, adding `"uri": <the bag's uri>` when the content is empty (the handler refuses empty content without one). `execute_mutation` runs the default `soft` executor, so with `TRELLIS_ENABLE_EMBED_ON_INGEST=1` this re-embeds the document and restamps its vector row's `created_at`. There is no restore tool.

---

## Python-Only Mutation API

The following operations exist in the `MutationExecutor` and `OperationRegistry` but are not yet exposed as CLI commands. Use them via the Python API.

### Operations

All operations go through the governed mutation pipeline: validate, policy check, idempotency check, execute, emit event.

```python
from trellis.mutate.commands import Command, Operation
from trellis.mutate.executor import MutationExecutor

executor = MutationExecutor(event_log=event_log)
result = executor.execute(command)
```

### Trace Operations

| Operation | Required Args | Description |
|-----------|---------------|-------------|
| `trace.ingest` | `trace` (dict) | Ingest a full trace via the mutation pipeline |
| `trace.append_step` | `trace_id`, `step` (dict) | Append a step to an existing trace |
| `trace.record_outcome` | `trace_id`, `outcome` (dict) | Record the outcome of a trace |

**Example -- append step:**

```python
cmd = Command(
    operation=Operation.TRACE_APPEND_STEP,
    args={
        "trace_id": "01JRK5N7QF8GHTM2XVZP3CWD9E",
        "step": {
            "step_type": "tool_call",
            "name": "run_tests",
            "result": {"passed": 42, "failed": 0},
            "duration_ms": 15000,
        },
    },
    target_id="01JRK5N7QF8GHTM2XVZP3CWD9E",
    target_type="trace",
    requested_by="code-orchestrator",
)
result = executor.execute(cmd)
```

### Evidence Operations

| Operation | Required Args | Description |
|-----------|---------------|-------------|
| `evidence.ingest` | `evidence` (dict) | Ingest evidence via the mutation pipeline. A caller-chosen `evidence.doc_id` may not start with `trace-summary:` (the trace embed worker's namespace: a row there makes the trace it names look already embedded, so its real summary is never written; code `reserved_trace_summary_id`), may not sit in the `precedent://learning/` namespace, and may not be a minted `<x>#chunk-<digits>` chunk id (both `reserved_document_id`). The prefix is exact and case-sensitive. `POST /api/v1/evidence`, `POST /api/v1/documents`, MCP `save_memory` and `trellis ingest evidence` refuse it the same way |
| `classification.set` | `doc_id`, `sensitivity` | Relabel a document's `DataClassification.sensitivity` (metadata only; `updated_at` preserved, vector row re-mirrored). A document above the caller's clearance is `target_not_found`, as a missing one is; a requested label above the caller's clearance is refused (`policy_violation`, nothing written); a chunk id is `classification_set_chunk`; a malformed `doc_id`/`sensitivity` is `classification_set_shape`. Re-applying the stored label is a successful no-op. Also available via `trellis curate classify-sensitivity` and `POST /api/v1/documents/{doc_id}/sensitivity` |
| `evidence.attach` | `evidence_id`, `target_id`, `target_type` | Attach evidence to a trace, entity, or precedent |

### Entity Operations

| Operation | Required Args | Description |
|-----------|---------------|-------------|
| `entity.create` | `entity_type`, `name` | Create a new entity. Upserting over an existing precedent is refused unless the precedent is current *and* the supplied `entity_type` is `"precedent"`. A non-current precedent is refused (`entity_create_over_existing_precedent`): an unconditional property replace would drop its lifecycle, silently coming back `"current"` with no reason recorded and no `PRECEDENT_REINSTATED` event -- use `precedent.reinstate`, or `entity.update` for a non-lifecycle edit. Any other `entity_type` is refused regardless of lifecycle state (`entity_create_retypes_existing_precedent`), mirroring the `entity.update` retype guard below. A **current** precedent is upserted normally -- this is what lets re-approving an already-promoted learning candidate succeed. |
| `entity.update` | `entity_id` | Update entity properties (merged, not replaced; `properties` may be a dict or an iterable of `(key, value)` pairs -- both are normalised to a dict before either guard below runs, so neither shape bypasses them). An `entity_id` that names no node, including one redacted while the command runs, is rejected (`target_not_found`). A precedent's `node_type` may never be changed away from `"precedent"` (`precedent_entity_type_change_refused`), lifecycle write or not -- this closes a two-step bypass (retype away with no lifecycle payload, then retype back with one). A write to `properties.lifecycle` is refused when the node's current type is `"precedent"`, *or* when the supplied `entity_type` names `"precedent"` in the same call (`precedent_lifecycle_via_entity_update`) -- use `precedent.demote`/`precedent.reinstate` instead, which keep the audit trail and the vector row in sync. |
| `entity.merge` | `source_id`, `target_id` | Merge two entities |

**Example -- create entity:**

```python
cmd = Command(
    operation=Operation.ENTITY_CREATE,
    args={
        "entity_type": "service",
        "name": "auth-service",
        "properties": {"language": "python", "team": "platform"},
    },
    requested_by="code-orchestrator",
)
result = executor.execute(cmd)
# result.created_id contains the new entity ID
```

### Precedent Operations

| Operation | Required Args | Description |
|-----------|---------------|-------------|
| `precedent.promote` | `trace_id`, `title`, `description` | Promote a trace to a precedent (also available via CLI) |
| `precedent.demote` | `target_id`, `reason` | Demote a precedent (also available via CLI and REST) |
| `precedent.reinstate` | `target_id`, `reason` | Reinstate a demoted precedent, human-only (also available via CLI and REST) |

### Link Operations

| Operation | Required Args | Description |
|-----------|---------------|-------------|
| `link.create` | `source_id`, `target_id`, `edge_kind` | Create a directed edge (also available via CLI) |
| `link.remove` | `edge_id` | Remove an edge |

### Label Operations

| Operation | Required Args | Description |
|-----------|---------------|-------------|
| `label.add` | `target_id`, `label` | Add a label (also available via CLI). A `target_id` that names no node, including one redacted while the command runs, is rejected (`target_not_found`). |
| `label.remove` | `target_id`, `label` | Remove a label. A `target_id` that names no node, including one redacted while the command runs, is rejected (`target_not_found`). |

### Feedback Operations

| Operation | Required Args | Description |
|-----------|---------------|-------------|
| `feedback.record` | `target_id`, `rating` | Record feedback (also available via CLI). A `rating` that is not a number from 0.0 to 1.0 inclusive, NaN included, is rejected (`validate`). |

### Maintenance Operations

| Operation | Required Args | Description |
|-----------|---------------|-------------|
| `redaction.apply` | `target_id`, `reason` | Hard-purge a graph entity: all SCD-2 versions, its edges, aliases, and vector entry. Emits `REDACTION_APPLIED` (counts + id pointers, never content). Also available via `trellis curate redact`. |
| `retention.prune` | `criteria`, `reason` | Archive low-value derived items. **Phase one is archival, not deletion**: candidates are stamped `Lifecycle.state="archived"` and retrieval stops serving them; content stays in the store, so a wrong prune is walked back by re-stamping. **`dry_run` defaults to `true`** — a run writes nothing unless it is passed `false` (CLI: `--apply`). `criteria` selects `noise_documents` / `unconfirmed_mints` / `lifecycle_states`, with `older_than_days` gating the age-based two only. `lifecycle_states` accepts the whole vocabulary but phase one **cannot select `archived` or `current`** — the first is already archived, the second is an explicit assertion that the item belongs in service — so naming either returns zero by construction. Those states are reported back on `unselectable_lifecycle_states` (in the `RETENTION_PRUNED` payload and the operator message) and skip the scan entirely, so an inert criterion never reads as an empty corpus ([#419](https://github.com/ronsse/trellis-ai/issues/419)). Traces and event-log rows are never candidates (the resolver cannot reach them); confirmed entities are never candidates at any age, and neither is a promoted precedent: a document row or graph node whose id is in the `precedent://learning/` namespace is skipped before any criterion is read and counted in `skipped_precedent` (a node already archived counts in `skipped_already_archived` instead). A candidate that vanishes before it is written, including a node redacted while the run is in progress, is counted in `skipped`, not re-created. Emits `RETENTION_PRUNED` in both modes, and also when an applied run fails part-way, counting what it archived before the failure. See [`adr-retention-prune.md`](../design/adr-retention-prune.md) §6. |
| `retention.restore` | `item_ids`, `reason` | Return archived items to `Lifecycle.state="current"`. The governed inverse of `retention.prune`, and what makes phase-one archival's reversibility real — direct store writes are forbidden and there is no governed document-update verb. Takes **explicit ids** (they ride the `RETENTION_PRUNED` payload), not criteria: re-deriving the set would re-run the selection that was wrong. Ids that are not archived or name no item, including a node redacted while the command runs, are skipped (`skipped_ids`), not errors. A graph-node restore also re-stamps the node's vector row with its restored lifecycle (a document restore gets this for free from `put_document`'s mirror); a sync failure does not undo the restore — it is logged and appended to the result's warnings, the same way a `precedent.demote`/`precedent.reinstate` sync failure is. Emits `RETENTION_RESTORED` unconditionally — also when the command fails part-way (listing what it restored before the failure) and when every id in the batch was refused or skipped — the same audit-completeness posture as `RETENTION_PRUNED`. A precedent is **not** unconditionally restored to `"current"`: it comes back to whatever lifecycle state it held immediately before that archival, read from SCD-2 history, so a precedent that was demoted and then pruned comes back demoted — prune-then-restore is not a second, unaudited way to undo a demotion. Only a precedent outside the `precedent://learning/` namespace, or one archived before promoted precedents were exempted from `retention.prune`, can have been pruned; a promoted precedent in the namespace is never a prune candidate. When that pre-archival state cannot be read from history, that one item is refused individually -- named in `refused_ids` / `refused_items` and a warning -- while the rest of the batch still restores or is skipped; the batch result is SUCCESS, with a partial count in the message, whenever at least one id was restored or the batch has no refusals at all (an all-skipped batch still succeeds). It is REJECTED only when at least one id was refused *and* none was restored, naming every refused id *and* every skipped id in the message; the rejection code is the refusal code when every refused id carries the same one (for example `retention_restore_history_unavailable`), and `retention_restore_nothing_restored` when the codes are mixed. Two more refusals cover a document row that holds nothing servable, each through the same path (named in `refused_items` and the warnings, the rest of the batch still restores). `retention_restore_precedent_node_missing`: a promoted precedent's document row is archived while its graph node no longer exists, so a `current` row would have no node behind it. `retention_restore_redaction_tombstone`: the row is the blank archived row `redaction.apply` leaves (`deprecation_reason` `redacted` *and* empty content, so an ordinary note archived with that free-text reason still restores), in or out of the `precedent://learning/` namespace. This includes a redaction whose purge failed after the row was blanked, where the node is still live; re-run `redaction.apply` on it. The node check runs first, so a redacted precedent reports the missing node. |

### Batch Execution

Execute multiple commands as a batch:

```python
from trellis.mutate.commands import CommandBatch, BatchStrategy

batch = CommandBatch(
    commands=[cmd1, cmd2, cmd3],
    strategy=BatchStrategy.STOP_ON_ERROR,
    requested_by="code-orchestrator",
)
results = executor.execute_batch(batch)
```

| Strategy | Behavior |
|----------|----------|
| `sequential` | Execute all commands in order |
| `stop_on_error` | Stop on first failure or rejection |
| `continue_on_error` | Execute all, collect all results |

### CommandResult

Every mutation returns a `CommandResult`:

| Field | Type | Description |
|-------|------|-------------|
| `command_id` | `string` | ID of the executed command |
| `status` | `CommandStatus` | `success`, `rejected`, `failed`, or `duplicate` |
| `operation` | `string` | The operation that was executed |
| `target_id` | `string` or `null` | Target entity ID |
| `created_id` | `string` or `null` | ID of newly created object |
| `message` | `string` | Human-readable result message |
| `warnings` | `list[string]` | Policy warnings |
| `metadata` | `dict` | Additional metadata |
| `executed_at` | `datetime` | When the command was executed |

### Idempotency

Set `idempotency_key` on a `Command` to prevent duplicate execution:

```python
cmd = Command(
    operation=Operation.ENTITY_CREATE,
    args={"entity_type": "service", "name": "auth-service"},
    idempotency_key="create_auth_service_20260310",
    requested_by="code-orchestrator",
)
```

If a command with the same key has already succeeded (in-memory or in the event log), the executor returns `CommandStatus.DUPLICATE` without re-executing. A command that was rejected or failed leaves its key free, so a corrected retry under the same key runs.

---

## Analyze Commands

### `GET /api/v1/analyze` and `GET /api/v1/analyze/{report}`

Gap G15: every `trellis analyze <report>` command had a CLI only — the UI
could not ask "are our learning loops performing or doing anything?" at
all. Requires the `admin` scope: every report replays other callers'
pack telemetry. Each report calls the *exact* library function its CLI
counterpart calls (never a subprocess, never a re-derivation), so a route
and its CLI command can disagree about transport but never about
arithmetic.

`GET /api/v1/analyze` lists every allowed report name, an operator-facing
description (what it measures, what a good vs. a bad value looks like,
which loop it informs), that report's default query params, and
`params_schema` — the params model as JSON Schema
(`params_model.model_json_schema()`), the same `minimum`/`maximum`/
`exclusiveMinimum`/`exclusiveMaximum` bounds, types, and (where a field
restricts to a fixed set, such as the holdout report's `outcome`) `enum`
that the run route below itself enforces, so a caller can render a typed
control instead of a plain text box per param. `GET
/api/v1/analyze/{report}` runs one. An unrecognized `{report}` is a 404
naming every allowed name (never a 500 from an unhandled import); an
unrecognized or out-of-range query param is a 422 (`extra="forbid"` on
every params model, so a typo'd param name is rejected rather than
silently ignored).

Eleven reports: `holdout`, `judged-outcomes`, `advisory-effectiveness`,
`value`, `dimension-predictiveness`, `learning-candidates`, `domains`,
`extractor-fallbacks`, `pack-telemetry`, `token-usage`, `cost`. Three
families are deliberately absent, not forgotten: `replay` takes a change
spec rather than bounded query params; `generate-advisories` and
`apply-noise-tags` are write paths and this router requires the `admin`
scope end to end regardless; `pack-quality` requires a `--scenarios` YAML file that must
already exist on the server's local disk, with no shipped default set —
there is no bounded query-param shape for "a path on the server's
filesystem," so it is a premise refutation rather than unfinished work.

**Finite JSON, always.** Every response passes through a recursive
non-finite-float sanitizer before `JSONResponse`, which also sets
`allow_nan=False`: a latent `inf`/`nan` becomes `null` rather than
crashing the request or putting a non-JSON token on the wire. This is
defense in depth — every library report already types an unmeasured
quantity as `float | None`.

**Caching.** A per-process, 60s TTL cache covers every report (several
aggregate the whole requested window per request, at the same cost
report to report). Every response names `computed_at` (when the report
was actually computed) and `cached` (whether this response reused that
computation), so a caller can tell a stale read from a fresh one.

```json
{
  "report": "advisory-effectiveness",
  "params": {"days": 30, "min_presentations": 3},
  "computed_at": "2026-10-10T23:00:00+00:00",
  "cached": false,
  "data": {"status": "degraded", "store_degradation": {"reason": "..."}}
}
```

`advisory-effectiveness` can answer `{"status": "degraded", ...}` instead
of a scored report — the same gate the CLI's `advisory-effectiveness` and
`generate-advisories` commands use: a degraded advisory file scores a set
that is missing whatever failed to parse, so the route reports the
degradation itself rather than a silently-incomplete measurement.
`learning-candidates` answers the pure aggregator's output plus a `note`
field explaining what it deliberately does not do: no
candidates/decisions file is written (a GET route must not write, and
must not race the Review queue's own reader/writer over that directory),
so there is no `promotion_ready` field and candidate names fall back to
bare item ids rather than the document-store-backed readable name — both
are computed only by `trellis analyze learning-candidates` or the nightly
worker.

The operator UI's **Reports** tab (`src/trellis_api/static/index.html`)
renders this: a report picker populated from `GET /api/v1/analyze`, a
params form typed from each report's `params_schema` — an integer/number
field as a bounded number input, a boolean as a checkbox, an enum field
(`outcome` on the holdout report) as a select, seeded from each report's
defaults — client-side bounds validation before a run request leaves the
browser, `computed_at` and a `cached` badge, scalar fields as cards and
nested structures as collapsible JSON, a degraded-store payload rendered
as a visible refusal rather than folded into the generic card view, and a
fetch failure (including a 422 that still reaches the server, rendered as
its field-by-field message rather than a bare status line) as a visible
error rather than a silently empty panel. Opening the tab populates the
picker and the first report's param form but does not run one — the
operator picks a report and clicks Run.

### `trellis analyze context-effectiveness`

Analyze which context items correlate with task success.

```bash
trellis analyze context-effectiveness [--days N] [--min-appearances N] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--days` | `30` | Days of history to analyze |
| `--min-appearances` | `2` | Minimum item appearances to include |

Shows per-item success rates and flags noise candidates (items correlating with failure).

### `trellis analyze token-usage`

Analyze token usage across CLI, MCP, and SDK layers.

```bash
trellis analyze token-usage [--days N] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--days` | `7` | Days of history to analyze |

Shows total tokens, average per response, breakdown by layer and operation, and over-budget alerts.

### `trellis analyze cost`

Estimate **Trellis's cost overhead** — the dollar cost of the context
Trellis injects into agent turns. This is the "what does memory cost on
top of my agent bill?" number.

```bash
trellis analyze cost [--days N] [--model claude-opus] \
    [--price-per-mtok 3.0] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--days` | `7` | Days of history to analyze |
| `--model` | `claude-sonnet` (or `TRELLIS_COST_MODEL`) | Consuming model for input pricing; resolves to the longest matching key, so `claude-opus-4-8` → the `claude-opus` family while `claude-sonnet-4-6` → its own key (a family is no longer one price) |
| `--price-per-mtok` | model table (or `TRELLIS_COST_PRICE_PER_MTOK`) | Explicit input price override, USD per 1M tokens |

Prices the `response_tokens` of every `TOKEN_TRACKED` event (emitted by
`get_context` / `get_lessons` / the other MCP macro tools) at the
consuming model's **input** rate — those injected tokens land in the
agent's next prompt. The **token count is exact**; the **dollar figure is
an estimate** (the ~4-chars/token counter, and an operator-owned price —
`price_source` in the output says which input won: explicit / env /
model-table / default — an id matching no key prices at the
`claude-sonnet` default and says so as `default_fallback`, rather than
implying a match). Table prices were verified against Anthropic's published
list on **2026-09-04**; they drift, so treat the number as an operator-owned
assumption and override it when it matters. It reports the *absolute* overhead in tokens and
dollars, not a ratio: Trellis never sees the agent's own generation, so
compare `overhead_dollars` against your provider's input-token bill for
the same window to get the overhead fraction. Local models (Ollama) price
at `--model local` → $0.

**JSON output (abridged):**

```json
{"period_days": 7, "overhead_events": 28, "overhead_tokens": 34800, "model": "claude-opus", "price_per_mtok": 5.0, "price_source": "model_table", "overhead_dollars": 0.174, "by_operation": [{"operation": "get_context", "layer": "mcp", "calls": 20, "tokens": 30000, "dollars": 0.15}], "estimator": "estimate_4_chars_per_token"}
```

### `trellis analyze judged-outcomes`

Read-only join from each judged memory operation to what followed it.
`MEMORY_OP_JUDGED` records the system's own decision (a classification label, a
distillation `keep` or `discard`); this follows its `subject_ref.ref_id` into the
packs that later served that memory (`PACK_ASSEMBLED.injected_items[]`) and the
per-item verdicts graders gave those packs (`FEEDBACK_RECORDED`).

```bash
trellis analyze judged-outcomes [--days N] [--limit N] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--days` | `30` | Days of history to analyze, for all three event types |
| `--limit` | `5000` | Max events scanned per event type |

Each judged row lands on one rung — `never_served`, `served_ungraded`,
`graded_uncited`, `cited_unhelpful`, `cited_helpful`, or `unservable` for a
subject no pack can serve (a distillation `discard` refs a session) — reported
per `op_type` × `decision`. Only servings at or after the judgment count; the
`*_any_order` fields drop that constraint. A document subject also matches its
own `<id>#chunk-N` servings, while a chunk subject matches only itself.

The **Phase 1 gate** asks for more than 500 graded rows per 30 days, where graded
means joined to an outcome. `gate_verdict` is decided on rows cited after their
judgment; `gate_readings` lists every looser reading, so `fails_strict_reading`
means a looser count would have passed. When a scan truncates, rows older than
the evidence start are dropped and rates use the covered window.

### `trellis analyze holdout`

Read-only analysis of the pack holdout experiment (`TRELLIS_PACK_HOLDOUT_RATE`,
see the [`PACK_ASSEMBLED` payload](#pack_assembled-event-payload)). It reads `PACK_ASSEMBLED`,
`capture.session_packs` and `capture.sweep_completed` and writes nothing, not
even a meta-trace.

```bash
trellis analyze holdout [--days N] [--until ISO] [--rate R] [--outcome NAME] [--itt] \
    [--permutations N] [--bootstraps N] [--power-sims N] [--planned-mde X] \
    [--planned-n N] [--settle-hours H] [--seed N] [--limit N] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--days` | `60` | Window length; a task belongs to the window by its first pack |
| `--until` | now | Window end, ISO-8601 (naive is UTC) |
| `--rate` | the single rate present | The `holdout_rate` to analyse; several rates in the window without it exit `2` |
| `--outcome` | `log1p_turns` | `log1p_turns`, `prs_created`, `log1p_tokens`, `commits`, or the guardrails `log1p_commits` and `any_commit` |
| `--itt` | off | Keep usage-limit cut-offs (the first sensitivity analysis) |
| `--permutations` | `10000` | Within-stratum permutations for the two-sided p-value |
| `--bootstraps` | `4000` | Stratified bootstrap replicates for the 95% CI |
| `--power-sims` | `600` | Simulated experiments per power estimate |
| `--planned-mde` | `0.33` | Planned MDE in outcome units (the pre-registration's) |
| `--planned-n` | `114` | Planned analysed tasks (the pre-registration's) |
| `--settle-hours` | `3` | Hours a completed capture sweep must follow a task's latest join before the task counts as finished |
| `--seed` | `20261004` | Seed for every random draw, so a report reproduces |
| `--limit` | `5000` | Max events scanned per event type |

A unit is a finished sub-agent task (its join names a parent session) whose
first pack is non-empty in its own arm's terms: a withheld row's
`holdout_items` or `holdout_sections` count. Its arm is that first pack's
`holdout` (ITT), and tasks with calls in both arms are counted. The funnel
counts every join that is not a unit, by reason, including first packs from
builds that write no `holdout` key.

The statistic is the stratum-weighted difference in mean outcome, served minus
withheld, with weights `n1*n0/(n1+n0)` over strata of parent session x ISO
week; a stratum holding one arm carries zero weight and is counted. The
p-value comes from permuting arm labels within strata, the 95% CI from a
bootstrap that resamples tasks within each stratum, and power from re-running
the simulation on the analysed outcomes at the realised N. A non-significant
result reads "no effect larger than the MDE detected", never "no effect".

The descriptive block is reported whatever the flag state, and it prints every
figure the pre-registration marks [R]: eligible and analysed tasks per 30 days,
the outcome's within-parent SD, the between-parent share of variance
(bias-adjusted: epsilon-squared, floored at 0), the largest parent session's
share of eligible tasks, packs per task, cut-off share by arm, the served arm's
PR base rate (`pr_base_rate_served`), the analytic MDE at 30, 60 and 90 days
(`mde_by_horizon`: 80% power, t at `N - 2` degrees of freedom, `N` = analysed
tasks per 30 days x days / 30), the N a 10% effect needs (`n_for_10pct_effect`),
and under `sessions` the main sessions with their sub-agent tasks rolled up:
how many reach a retrieval per 30 days, their outcome SD and their MDE at the
same horizons. A main session is dated by when capture wrote its latest join,
and it counts every pack whatever its rate, including those from builds that
write no `holdout` key, where the task figures keep only the analysed rate's.
So the session figures per 30 days divide by the whole window, and the task
figures and the task MDE horizons by the task window (`task_window_since` to
the window end, `task_window_days`): it opens at the window's first row at the
analysed rate unless the newest pack row before the window was already at that
rate, and a note says when it is shorter than `--days`.
It also gives arm counts and binomial checks of the arm ratio
(overall and per ISO week; the pre-registration pauses below p 0.001). A figure
the rows cannot give reads "not measurable: <reason>" in text; in JSON it is
`null` and the nearest `not_measurable` field names the reason. Run with the
flag off, the block is the pre-registration's re-measure and the inference
reads "no withheld arm". The funnel also counts eligible tasks whose capture
reported a pack id it could not parse (`eligible_with_unparsed_pack_ids`),
because their first parsed pack may not be their first call. The non-ephemeral
rule is listed as applied upstream, because capture writes no join for a
session in an ephemeral project. The pre-treatment exclusion drops a task whose
first retrieval came after a stop hook's feedback, read from the join's
`retrieval_timing` (the first retrieval's ordinal among the session's tool
calls, the tool-call count, and whether hook feedback preceded it); the funnel
counts those as `pre_treatment_excluded`. A join written before capture
recorded the timing is undecidable: it stays in, `pre_treatment_unknown` counts
it, and the exclusion reads "partially applied" until none are left (a join is
rewritten with the timing only when its session is parsed again, so a
watermark reset back-fills old ones; a back-fill also re-dates each rewritten
join, so the main-session rollup, which windows on the join's `occurred_at`,
pulls those old sessions into the current window). The rule is per session:
an earlier stop's feedback in a multi-prompt session also excludes a later
task's retrieval, which sub-agent units rarely meet. `pre_treatment_hook_feedback_seen`
counts the decided tasks whose session held any hook feedback; zero across
sessions that ran under a stop hook means capture's `HOOK_FEEDBACK_PATTERN`
no longer matches the harness's wording. The rule's other half is applied
from the join's `first_commit_ordinal`: a first retrieval after the session's
first commit is also excluded, and `pre_treatment_excluded_after_commit` counts
those. `no_retrieval` is split into
`never_called`, `errored`, `unparsed` and `empty_answer`, so a transport-failed
`get_context` no longer reads as never called. The post-hoc share is the
fraction of decided tasks whose first retrieval came after hook feedback, after
the first commit or in the last tenth of the session's tool calls; it is `null`
with its reason when no task carries every position.

The report also carries the pre-registered covariate-adjusted permutation as a
secondary analysis, `covariate_adjusted`. It decides nothing: the primary, the
power simulation and the funnel do not depend on it, and its own verdict is
always `secondary_not_decisive`, never the primary's. Capture records the two
pre-treatment covariates in each join's `covariates` block: `brief_chars`, the
length of the first user turn it counts as a person's, and `agent_type_digest`,
a 12-character pseudonymous hash of the sub-agent's type that stays in the
local event log (no text is kept, and the report carries neither the digest nor
an id). The exact specification:

- **Outcome.** The primary's outcome (`--outcome`, default `log1p_turns`), one
  value per analysed task.
- **Subset.** The analysed tasks whose join carries every covariate the
  adjustment needs. The rest are dropped, counted in `n_missing_covariate` and
  never imputed, so `n_used` of `funnel.analysed` tasks are used, and an
  adjusted figure's gap to the primary mixes the adjustment with the subset.
  `unadjusted_same_subset` is the primary's test run on the same tasks' raw
  outcomes, so the gap between the two point estimates is the adjustment
  alone; their p-values and intervals use separate resampling streams and also
  differ by Monte Carlo noise. It is `null` when the fit is refused for too few
  residual degrees of freedom or for no stratum holding both arms.
- **Covariates.** `log1p(brief_chars)` (`brief_length`, pre-registered), and
  that plus the sub-agent type (`brief_length_and_agent_type`, exploratory).
- **Fit.** Least squares of the outcome on one indicator per stratum (parent
  session x ISO week), the covariates and, for the agent type, one indicator per
  level. Types with fewer than 3 tasks pool into a single level first, and the
  most common level (ties broken by digest order, the pooled level last) is the
  reference and is dropped. The
  arm is never a column of the fit, so the residuals do not depend on the
  assignment.
- **Test.** The primary's within-stratum permutation test, with its statistic
  and strata, run on the residuals. It is exact under the sharp null because the
  residuals are the same whichever way the arms are shuffled. The interval is
  the primary's stratified bootstrap of the residual difference with the fitted
  coefficients held fixed, so it ignores their uncertainty and is narrower than
  a refit would give. The text output says so on the Secondary line.
- **Balance gate.** `arm_balance_r2` is the share of the arm indicator's
  within-stratum variance the whole design explains (the drop in its residual
  sum of squares from strata alone to the full design, over the strata-alone
  figure). Above 0.9 the covariates stand in for the arm, the adjusted
  difference is not identified, and the adjustment reads `not_measurable` with
  that reason; `unadjusted_same_subset` is still reported.
- **Other refusals.** `not_measurable` also when no analysed task has the
  covariates, when fewer than 4 residual degrees of freedom remain (tasks used
  less the design's rank), or when no stratum holds both arms. Joins written
  before capture recorded the block stay missing until their session is parsed
  again. A refused fit keeps its `n_used` and `n_missing_covariate`, and the
  text report prints them under its reason; `arm_balance_r2` is `null` unless
  the refusal came after it was computed, and the text line omits it then.
- **Reading the means.** An adjusted run's `mean_residual_served` and
  `mean_residual_withheld` are means of residuals, not of the outcome, and sit
  under no outcome-mean key; `mean_served` and `mean_withheld` are there only on
  the unadjusted run, where they are outcome means.

The guardrails block
(`guardrails`) gives each pre-registered guardrail per arm with its n and no
p-value or verdict: PRs created and any commit (each left out when it is the
primary), log1p commits, tool errors and the re-call rate (tasks whose capture
parsed a second pack id) over the primary's analysed tasks, and the cut-off
rate over every eligible task, cut-offs included. Output is counts and
statistics only, never an id, intent or text. Exit `0` on any report, `2` on an
invalid option or an unnamed choice between rates, in both formats.

### `trellis analyze domains`

Read-only usage report for the primary retrieval slice, `domain`. Joins observed
domain values across three sources — `TraceContext.domain` (TraceStore),
`ContentTags.domain` in document metadata (DocumentStore), and pack + feedback
events (EventLog, grouped by the pack payload's `domain`).

```bash
trellis analyze domains [--days N] [--limit N] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--days` | `30` | Days of pack/feedback history to analyze |
| `--limit` | `1000` | Max traces, documents, and events per source to scan |

Per domain it reports: document count, trace count, packs served, graded packs,
and success rate from `FEEDBACK_RECORDED`. Items, traces, and packs with **no**
domain are surfaced under `(none)` so coverage gaps stay visible. Text mode
prints a Rich table sorted by document count descending; JSON mode emits
`{"status": "ok", "days": N, "domains": [...]}`.

Use this to decide which domains to seed in
[`classify.domain_keywords`](tagging-for-retrieval.md#seeding-your-own-domains).

> **Out of scope:** automatic domain discovery/clustering and a domain
> *promotion* analyzer. Those follow the column-leaf pattern (contract first,
> gated on production telemetry) — see
> [`adr-column-leaf-modeling-guardrails.md`](../design/adr-column-leaf-modeling-guardrails.md)
> and [`adr-autonomy-ladder.md`](../design/adr-autonomy-ladder.md) tier 2.

---

## Worker Commands

Unattended learning/curation workers. See [`../design/adr-autonomy-ladder.md`](../design/adr-autonomy-ladder.md) for the four-tier autonomy model these commands operate under.

### `trellis worker tune`

Run one `RuleTuner` pass and, when **Tier-1 auto-promotion** is enabled, auto-promote every qualifying proposal through the same governance pipeline `trellis metrics promote --commit` uses — then arm post-promotion monitoring so degradation auto-rolls-back.

```bash
trellis worker tune [--tuner-name NAME] [--since-days N] [--dry-run] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--tuner-name` | `rule_tuner` | Logical tuner name (cursor + proposal scope). |
| `--since-days` | 30-day window | Read the last N days instead of the tuner's trailing window. When auto-promotion is enabled, a live run also re-monitors earlier auto-promotions over the same N days (30 by default). |
| `--dry-run` | off | Report what *would* auto-promote without mutating stores or emitting events. |
| `--format` | `text` | `text` or `json`. |

**Default behaviour is a pure tuner pass.** Auto-promotion is **off by default** (global default OFF, per Tier-1 invariant (d)). With it disabled and no `--dry-run`, `worker tune` is byte-identical to `trellis metrics tune`: it produces/refreshes proposals and promotes nothing. Without `--dry-run`, non-qualifying proposals stay `pending` for manual review via `trellis metrics promote` — they are reported, never rejected. `--dry-run` persists no proposal and leaves the tuner cursor alone.

#### Enabling Tier-1 auto-promotion

Add a `learning.auto_promote` section to `~/.trellis/config.yaml` (or `$TRELLIS_CONFIG_DIR/config.yaml`):

```yaml
learning:
  auto_promote:
    enabled: true              # default false — master switch, global default OFF
    min_sample_size: 30        # stricter than manual promote (5); must be >= 5
    min_effect_size: 0.25      # stricter than manual promote (0.15); must be >= 0.15
    require_baseline: true      # no baseline => nothing to roll back to => left for a human
    post_min_samples: 20        # min post-promotion outcomes before a degradation verdict
    post_regression_threshold: 0.10  # success-rate drop (abs) that triggers auto-rollback
    post_lookback_days: 7       # monitoring window on either side of the promotion
```

The auto thresholds **must be at least as strict as the manual-promote defaults** — the config loader (and `AutoPromotePolicy`) rejects looser values loudly rather than silently weakening the autonomous gate. Monitoring is always armed (`auto_demote` is forced on); you cannot auto-promote without an armed rollback.

#### Audit trail

Each autonomous action emits a **dedicated, self-identifying** event in addition to the normal governance event:

| Event | Emitted when |
|-------|--------------|
| `parameters.auto_promoted` (`PARAMS_AUTO_PROMOTED`) | A qualifying proposal is auto-promoted (alongside `PARAMS_UPDATED`). |
| `parameters.auto_rolled_back` (`PARAMS_AUTO_ROLLED_BACK`) | Post-promotion monitoring demotes a degraded snapshot (alongside the rollback's `PARAMS_UPDATED` and `PARAMETERS_DEGRADED`). |

Degradation that accrues *after* the promoting pass is caught on a later pass: each `worker tune` run re-monitors recent auto-promotions and rolls back any that have since degraded.

The manual `trellis metrics promote` path is unchanged and emits only `PARAMS_UPDATED` — the dedicated events distinguish "a human promoted this" from "the system promoted this on its own."

### `trellis worker curate`

Run one **full curation cycle** (Tier-2). Calls the curation library functions directly — no shelling out — in this fixed order:

1. **effectiveness feedback** (`run_effectiveness_feedback`) — demote: noise-tag low-value items;
2. **advisory generation** (`AdvisoryGenerator.generate`);
3. **advisory fitness loop** (`run_advisory_fitness_loop`) — adjust confidence / suppress weak advisories;
4. **learning candidates** (`build_learning_observations_from_event_log` → `analyze_learning_observations` → `write_learning_review_artifacts`) — writes promote-half review artifacts to `--output-dir`, by default the directory the API's Review queue reads (see **Learning artifacts directory** under [Review queue](#review-queue-admin-scope)).

```bash
trellis worker curate [--output-dir DIR] [--days N] [--interval SECONDS] \
  [--dry-run] [--reconcile-first] \
  [--skip-noise-tags] [--skip-advisories] [--skip-learning] \
  [--no-meta-trace] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--output-dir` / `-o` | `$TRELLIS_LEARNING_ARTIFACTS_DIR`, else `<data_dir>/learning` | Directory for the learning-candidate review artifacts. The default is the directory `GET /api/v1/learning/candidates` reads; pass this only to write somewhere the API does not look. |
| `--days` | `30` | Days of EventLog history to scan. |
| `--interval` | (off) | Loop mode: re-run the cycle every N seconds until SIGINT/SIGTERM. Plain sleep — **no scheduler dependency** (APScheduler/Celery deliberately rejected). |
| `--dry-run` | off | Analyze only — no noise tags, no advisory mutations, no artifacts written. Each stage that runs still records its meta-Activity but no finding; add `--no-meta-trace` to skip that. |
| `--reconcile-first` | off | Backfill `pack_feedback.jsonl` into the EventLog (`reconcile_feedback_log_to_event_log`) before the cycle. Refused with `--dry-run` (exit 2); preview with `trellis admin reconcile-feedback --log-dir DIR --dry-run`. |
| `--skip-noise-tags` | off | Skip stage 1. |
| `--skip-advisories` | off | Skip stages 2 + 3. |
| `--skip-learning` | off | Skip stage 4. |
| `--no-meta-trace` | off | Skip recording each stage as a meta-Activity. |
| `--format` | `text` | `text` or `json`. |

**Promotion stays human-gated.** This command writes learning candidates for review and **never promotes** (Tier-2 invariant). To promote, review the emitted `promotion_decisions.template.json`, set `approved: true` on the rows you want, then run `trellis curate promote-learning`. In `--interval` mode each cycle logs one structured `worker_curate.cycle` line with the headline counts (noise-tagged, advisories generated/suppressed, candidates written); SIGINT/SIGTERM drains the current cycle and exits cleanly.

**Promotion-ready digest (#845).** Both `--format json`'s `learning_promotion_ready` field and the `intent_learning_candidates.json` artifact's `promotion_ready` key carry a surfacing-only summary — `count` of `promote_guidance` candidates with `helpful_count >= LEARNING_PROMOTION_READY_MIN_HELPFUL_COUNT` (currently `1`) and `unhelpful_count == 0`, plus `top` (up to 5, ranked by `helpful_count` desc, then `success_rate` desc, then `times_served` desc). Text output prints `"N candidate(s) promotion-ready — review with trellis curate promote-learning or the Review tab"` when `N > 0` (singular at `N == 1`). Nothing here promotes anything; it only tells a reader that candidates are waiting. A candidate's `precedent_name` also gets a readable fallback name (first non-empty line of its document, frontmatter and heading markers stripped) when it has no `title` and its item resolves — kept as a bare item id only when the item can't be resolved.

**What a promoted precedent carries.** Its description opens with a bounded excerpt of the source document (`PRECEDENT_SOURCE_EXCERPT_MAX_CHARS`, read from the document store when `promote-learning` builds the command; `properties.source_excerpt_status` is `included` or the reason there is none), and it gets a `precedent_applies_to` edge to each entity the serving packs were seeded from (`PACK_ASSEMBLED.seed_entity_ids`, recorded only when the graph axis expanded seeds). A pack whose graph axis expanded no seeds records none, so its candidate gets no edges. An unseeded graph axis serves nothing by default (#961), so a flat pack then reaches that precedent through its document row: by keyword always, by embedding only when embed-on-ingest is on (it defaults off). With the recency window opted in, it can also reach it while it is among the newest nodes.

### `trellis worker enrich`

Batch-enrich under-tagged documents via the LLM `EnrichmentService`, writing the suggested tags / classification / importance back into each document's `metadata.content_tags`.

```bash
trellis worker enrich [--concurrency N] [--limit N] \
  [--reenrich] [--dry-run] [--scan-cap N] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--concurrency` | `3` | Parallel enrichment requests. |
| `--limit` | `50` | Max documents to enrich this run. |
| `--reenrich` | off | Also take documents that have already been through this path. |
| `--dry-run` | off | Select + report candidates without calling the LLM. |
| `--scan-cap` | `20000` | Max rows read looking for candidates (at least `1`); the scan stops there even if older un-enriched documents remain. |
| `--format` | `text` | `text` or `json`. |

**Selection predicate.** A document is a candidate when its `metadata.content_tags` is missing/empty, **or** when `content_tags.classified_mode != "enrichment"` — i.e. it has never been through this path. `--reenrich` takes the already-enriched too. (This used to select on `content_tags.tag_confidence`, a key `ContentTags` does not define and nothing ever wrote, so the threshold could never skip anything.) A promoted precedent's document row (an id in the `precedent://learning/` namespace) is **never** a candidate, with or without `--reenrich`: its `document_form` and `decision` tag belong to the precedent handlers, and the row is skipped before any model call. Neither is a redaction tombstone (the blank archived row a redaction leaves): there is nothing to enrich, and it never carries tags, so it would otherwise be selected on every pass and cost a model call each time.

**How far selection reads (#1068).** Documents are read newest-first in pages (at least 1,000 rows each, or `5 × --limit` when that is larger) until `--limit` candidates are found, the store ends, or `--scan-cap` rows have been read. Enriched rows, promoted precedents and tombstones are skipped but still count as read, so a newest stretch made only of them no longer hides the older documents behind it. `--format json` reports `scanned` (rows read, in whole pages) and `scan_capped` in both the dry-run and the real-run payload; text mode prints one yellow line before the summary when the scan was capped. `scan_capped: true` with fewer than `--limit` selected is not "nothing left to enrich": older documents were not examined, so raise `--scan-cap`. It is a report, not a failure, and the exit code is the same in both formats. A store of exactly `--scan-cap` rows also reads as capped, since a full last page cannot say whether more lie behind it. Every page is a full-row read, so a store with nothing left to enrich reads up to `--scan-cap` rows on every run. A scheduled run therefore enriches older un-enriched documents it used to pass over, and writes `auto_importance` and `document_form` to them, `--limit` rows per run.

**Concurrency counters in `--format json` (#421).** The candidate page is a snapshot taken before *N* model calls, so the write-back re-reads each row and merges only the keys this path owns. `stale_snapshot` counts rows another writer changed inside that window — those are still enriched, onto their current content, so it is a concurrency signal rather than a failure count. `vanished` counts rows deleted inside the window, which are **skipped** (writing them back would resurrect a deleted document). A standing non-zero `stale_snapshot` means the batch is racing another writer and wants a smaller `--limit`. `skipped_redacted` (always present) counts only rows **redacted inside the window** (a tombstone that already existed at selection time is never selected, so it does not appear here): the tags came from text that is gone, so they are dropped rather than written onto the blank tombstone, and the row is neither `enriched` nor `vanished` and emits no `MEMORY_OP_JUDGED` event. The text summary's "concurrent writes" line prints when it is non-zero.

**Requires an LLM extra.** Enrichment needs a configured `llm:` block plus the matching `[llm-openai]` / `[llm-anthropic]` extra. With no buildable client the command **exits non-zero with an actionable message** naming the missing config/extra — it never silently no-ops.

### `trellis worker mine-precedents`

Mine precedent candidates from failure / partial traces (wraps `PrecedentMiner.generate_precedent_candidates`).

```bash
trellis worker mine-precedents [--domain D] [--min-traces N] \
  [--limit N] [--dry-run] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--domain` | (all) | Restrict mining to this trace domain. |
| `--min-traces` | `3` | Minimum failure/partial traces required to mine. |
| `--limit` | `100` | Max traces to analyze. |
| `--dry-run` | off | Report how many failure/partial traces are in scope without calling the LLM. |
| `--format` | `text` | `text` or `json`. |

Candidates are **surfaced** as `PRECEDENT_CANDIDATE_MINED` events (one per candidate) but **not promoted**: `get_lessons`, `trellis retrieve precedents` and `GET /precedents` read `PRECEDENT_PROMOTED` only and never serve a mined candidate, and nothing routes one into the Review queue yet. Older versions emitted candidates as `PRECEDENT_PROMOTED` with `source="precedent_miner"`; readers skip those rows. Like `enrich`, this requires an LLM extra and exits loudly when none is configured.

### `trellis worker capture-sessions`

Run one Claude Code session-capture sweep — the `trellis worker` front door for the same code path as the `trellis-session-capture` console script and `python -m trellis_workers.session_capture`.

```bash
trellis worker capture-sessions [--dry-run] [--format text|json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--dry-run` | off | Plan the sweep without writing memories or advancing the watermark. |
| `--format` | `text` | `text` or `json`. |

Everything else is configured by the `TRELLIS_CAPTURE_*` environment variables documented in [session-auto-capture.md](session-auto-capture.md). **Requires a distillation judge** (an `llm:` block): distillation fail-closes, so a sweep without one captures nothing — the command exits non-zero rather than reporting a clean no-op. A judge that goes away *mid*-sweep is counted in `sessions_judge_unavailable`, reported as `"status": "partial"`, and also exits non-zero unless `TRELLIS_CAPTURE_STRICT=0`. A session that raises mid-sweep is skipped and left for retry rather than ending the sweep; it is counted in `sessions_errored` and handled the same way (`"partial"`, non-zero unless `TRELLIS_CAPTURE_STRICT=0`).

---

## API Authentication

The REST API authenticates with scoped API keys (roadmap item E.5, issue #191).

### Modes

`TRELLIS_AUTH_MODE` controls the posture:

| Mode | No credential | Invalid credential | Valid credential |
|------|---------------|--------------------|------------------|
| `off` | passes, all scopes | passes, all scopes | passes, all scopes |
| `optional` | passes, all scopes (migration mode) | 401 | scoped |
| `required` | 401 | 401 | scoped |

When `TRELLIS_AUTH_MODE` is **unset**, the mode is inferred for backwards
compatibility: `required` if `TRELLIS_API_KEY` is set, else `off`. An invalid
value crashes at startup — there is no silent fallback. `off` and `optional`
log a loud startup warning.

### Token format and headers

Tokens look like `trellis_ak_<key_id>.<secret>` — `key_id` is 12 hex chars,
the secret half is never stored (only its SHA-256). Present a token on either
header (`X-API-Key` wins when both are sent):

```
X-API-Key: trellis_ak_4f3a2b1c0d9e.zJx...
Authorization: Bearer trellis_ak_4f3a2b1c0d9e.zJx...
```

**Legacy shared secret:** a token exactly matching `TRELLIS_API_KEY` is
granted all scopes, so single-secret deployments keep working while
migrating to scoped keys.

### Scopes

Six scopes: `read`, `ingest`, `mutate`, `admin`, plus the two read-clearance
scopes `confidential` and `restricted` (see [Sensitivity](#sensitivity-clearance)).
`admin` implies all others. The router → scope map:

| Router (`/api/v1`) | Required scope |
|--------------------|----------------|
| `retrieve` (search, packs, entities, traces, precedents) | `read` |
| `ingest` (traces, evidence, vectors, bulk) | `ingest` |
| `mutations` (`/commands/batch`) | `mutate`, plus `admin` per operation — see below |
| `curate` (precedents, links, entities, documents, feedback) | `mutate` |
| `extract` (`/extract/drafts`) | `mutate` |
| `admin` (health, stats, effectiveness, advisories, vector reset) | `admin` |
| `explore` (`/documents`, `/graph/history`) | `read` |
| `explore` (`/events`, `/events/{event_id}`, `/packs`, `/packs/{pack_id}`) | `admin` (route-level, replays other callers' pack telemetry) |
| `analyze` (`/analyze`, `/analyze/{report}`) | `admin` (every report replays other callers' pack telemetry) |
| `observations` GETs | `read` |
| `observations` POSTs (`/observations`, `/measurements`) | `mutate` |
| `policies` GETs | `read` |
| `policies` POST / DELETE | `admin` |

`/commands/batch` accepts any `Operation`, and `mutate` alone is not enough
for all of them: a batch containing any operation outside
`NON_DESTRUCTIVE_OPERATIONS` (entity merge, redaction, retention prune,
alias rebind, link/label removal, schedule/settings/classify-rule writes —
see `trellis.mutate.immutable_core`) is refused whole, with no command
executed, unless the key carries `admin`. The 403 body names every refused
operation. `requested_by` on a command stays a caller-chosen audit label;
holding `admin` does not make it verified.

`/healthz`, `/readyz`, `/api/version`, `/metrics`, and `/ui` stay reachable
without a credential by default (see [Production exposure](#production-exposure)
for what `/readyz` and `/metrics` reveal to anonymous callers). A valid key
lacking the required scope gets 403; missing or invalid credentials get an
undifferentiated 401 (the failure category is logged server-side only).

### Sensitivity clearance

A document can carry a sensitivity label (`public`, `internal`, `confidential`,
`restricted`), set with the optional `sensitivity` argument of MCP `save_memory`
and `save_knowledge` (for `save_knowledge` it labels the evidence document
created from `content`). A reader is served a labelled document only when its
key holds the matching scope:

| Key scopes | Reads up to |
|------------|-------------|
| `read` (and `ingest`, `mutate`) | `internal` |
| `confidential` | `confidential` |
| `restricted`, `admin` | `restricted` |

An unlabelled document reads as `internal`, so a store with no labels behaves
exactly as before. A key can only label up to its own clearance. Withheld items
appear in a pack's withholding note as `sensitivity N`, as counts only. MCP over
stdio, auth mode `off`, and the CLI are unrestricted. A document labelled above
`internal` is not memory-extracted, and the `save_memory` reply says so.
Graph nodes and entities carry no label.

Two answers no longer reveal a document the caller cannot read. A superseded
document whose declared successor the caller may not read is served, as it would
be if the successor did not exist (the successor is judged the way the pack's
own gate judges it: by its own label, and a chunk by its parent's too). The
served items are the same with and without that successor among the candidates;
the pack's counters (`candidates_found`, `total`, `by_reason.sensitivity`) still
count it as a sensitivity withholding, as for any hidden candidate. A served
document's own reconcile pointers are dropped from the served copy when they
name a document the caller cannot read: `lifecycle.superseded_by` (on a loser,
naming its successor), `supersedes_doc_id` (on a successor, naming its loser)
and `updates_doc_id` (on an UPDATE addendum, naming the candidate it
annotates); the last two are written by the reconcile-on-write path and by
session capture. All three are single id strings, and there is no other
metadata key that stores one. The
drop applies to every surface that serves stored metadata: flat and sectioned
packs, `GET /api/v1/documents/{id}`, the rows of `GET /api/v1/documents` (the
list and `?q=`), and `GET /api/v1/search`. The store judges the named id, so the
answer does not depend on whether it matched the query, and a store error reads
as unreadable (a chunk under a parent-fetch outage reads as unreadable, so the
pointer is dropped). One store judgement runs per response, over the distinct
ids its rows name. The stored record is never rewritten, and a reader of
everything (`restricted`, `admin`, auth off) does no reads and sees the record
as stored. The loser's `lifecycle.state` stays `superseded`, so such a caller
can tell that some document it cannot read supersedes this one, though not
which; likewise the `reconciliation` marker (`supersede` or `update`) stays when
its pointer is dropped, so a reader can tell a hidden counterpart exists but not
which. The concealment is itself an existence answer: a caller that can write a
document carrying a guessed pointer id and read it back learns, from whether the
id survives, whether a document by that id exists above its clearance. That is
nothing beyond the accepted 409 posture on `POST /api/v1/documents` below. A
malformed pointer (anything but a non-empty string) is left as stored.
A graph node names a document id the same way, in three places: `document_ids`,
and in its properties the string `evidence_ref` (`save_knowledge` fills both with
the same id) and `lifecycle.superseded_by` (`supersede_entity` stamps it with the
successor's evidence document). A node is served whole, since it carries no
label, but without any id the caller may not read in any of them: on `GET
/api/v1/graph/history` (every version), `GET /api/v1/entities/{id}` (the entity
and every node of its `subgraph`), `GET /api/v1/graph/search` (every hit's
`properties`), MCP `get_graph` (the root and each neighbour), MCP `get_items` on an
entity id, the entities `build_file_context` returns, and the observation reads: `GET
/api/v1/observations` and MCP `query_observations` (every observation's
`evidence_ref`, which is normally a document id). A pointer key is removed rather than blanked, `lifecycle.state` and every
other property stay, and a `document_ids` member that is not a string, a
`document_ids` that is not a list and an `evidence_ref` that is not a string are
left as stored. The store judges the ids as above, in one pass over the distinct
ids the whole response names. Depth,
neighbours and relationships do not change. A graph-axis pack item spreads its
node's properties into its `metadata`, so `metadata.evidence_ref` on a pack item
is a concealed carrier too, on the same footing as the supersession pointers: it
is dropped from `POST /api/v1/packs` and `/packs/sectioned` for a caller below
`restricted`, the item is still served in the same position, and supersession
that resolves through the ref (a loser whose successor a node names) is decided
before the drop. `GET /api/v1/graph/search/facets` counts nodes per `node_type` and
names no property, so there is nothing in it to conceal, and a concealed pointer
never removes a node, so its counts and `/graph/search`'s `total` are the same for
every caller. An edge is concealed on the same terms in the `subgraph` of `GET
/api/v1/entities/{id}`: a string top-level `evidence_ref` (the provenance
column, not a node property) reads `null` when it names a document the caller may
not read, and a pointer inside its `properties` (`evidence_ref`,
`lifecycle.superseded_by`, `supersedes_doc_id`, `updates_doc_id`) is removed;
every other field and property stays, and a value that is not a string is left as
stored. The column is blanked rather than removed because every stored edge row
carries it (null when unset), so a missing key would say the edge cites a
document the caller may not read; `properties` keys are absent when unset, so
their removal says nothing. The
entity, its nodes and its edges are judged in one pass, so a response costs one
store read per distinct id whichever kind of row names it. MCP `get_graph` reads
the same edges and renders only their endpoints and type, so it names no edge
pointer. A learning-sourced precedent node's `properties.source_item_id` (the
item it was promoted from, often a document) is withheld like the node's other
pointers, and so is `metadata.source_item_id` on the pack item the graph axis
builds from that node: it is dropped from `POST /api/v1/packs` and
`/packs/sectioned` for a caller below `restricted`, and the item is still served
in the same position.
With `TRELLIS_REQUIRE_PACK_ATTRIBUTION` or `TRELLIS_REQUIRE_BODIED_ATTRIBUTION` on,
MCP `record_feedback` names, and counts, only the served ids the caller may
read; a pack that served none reads as an unknown pack and the call is
recorded. A trusted-local caller sees every id.

The same clearance governs writes, under one rule: **a key cannot write a label
above its own clearance, and cannot touch a document it may not read.**

- `classification.set` (CLI `trellis curate classify-sensitivity`, REST `POST
  /api/v1/documents/{doc_id}/sensitivity`) relabels a document. A document above
  the caller's clearance answers `target_not_found`, in the same words as a
  missing one, so a key cannot learn that it exists by trying to relabel it. The
  new label must be within the caller's clearance too (`policy_violation`,
  nothing written). Any cleared key with the `mutate` scope may lower a label it
  can read; the scope is not `admin`.
- `evidence.ingest` refuses a label above the caller's clearance, on a new id
  and an existing one, in the words `classification.set` uses. At an id that
  already names a document it also refuses to overwrite one above the caller's
  clearance and to lower a stored label (relabel with `classification.set`),
  as `save_memory`/`save_knowledge` do. These checks live in the handler, so
  `POST /api/v1/commands/batch`, `POST /api/v1/documents` and MCP
  `execute_mutation` are covered, and so is every trusted caller (the CLI,
  workers, extractors), for whom they are transparent while no document carries
  a label.
- MCP `save_memory(supersedes=)` and `save_knowledge(supersedes=)` stamp their
  target, so they are writes to it: a target above the caller's clearance is
  answered as a missing one and left unchanged.
- `retention.restore`, `redaction.apply`, `precedent.demote`,
  `precedent.reinstate` and a learning-path `precedent.promote`
  (`entity_type="precedent"`, which rewrites the precedent's document row)
  aimed at a document above the caller's clearance are denied at Stage 2 as a
  `policy_violation`, recorded as a `MUTATION_REJECTED` event. That answer
  differs from the one for a missing id, so these five, and an `evidence.ingest`
  overwrite, tell a key that the document exists; the read side's by-id
  not-found parity does not extend to them.
- REST `POST /api/v1/documents` takes an optional `sensitivity` beside `doc_id`,
  `content` and `metadata`, under the rules `POST /api/v1/evidence` and the MCP
  save tools use: a label above the key's own clearance is a 403, an unknown
  label (or one that is not a string) is a 422, and so is a raw
  `data_classification` key in `metadata` (for every caller, `admin` included:
  the label has one door). `metadata` must be an object (422 otherwise). A
  `lifecycle` or `evidence_ref` key in `metadata` is also a 422, whatever its
  value and the caller: the pack builder withholds by id, so a `lifecycle` would
  withhold every item under this document's id (a graph node's included) and an
  `evidence_ref` the item whose successor it names. Supersede with MCP `save_memory(supersedes=...)`; the
  refusal names the writer for each key. The route holds this rule, not
  `evidence.ingest`, which takes the keys from `save_memory(supersedes=)`, so
  `POST /evidence`, `POST /commands/batch` and `execute_mutation` still accept
  them. Nothing is written on any refusal. The overwrite check compares the label this request
  writes, so a re-put that omits `sensitivity` over a labelled document is the
  downgrade it always was.
- REST `POST /api/v1/vectors` judges every row before it writes any, so a refused
  batch stores nothing. A row whose `item_id` names a document above the key's
  clearance, or a chunk of one (a chunk id is judged by its parent's label,
  whether or not the chunk has a row of its own), is a 409 with nothing written.
  The route stores a vector for any id, so it has no "missing document" answer to
  give instead, and an id that names no document is written as before. A raw
  `data_classification` in a row's `metadata` is a 422 for every caller, auth
  `off` and `admin` included (a vector row's label is mirrored from its
  document). A row that is not an object, or whose `metadata` is neither an
  object nor `null`, is a 422 too, as is a row whose `item_id` is present but
  not a string (an id the gate cannot read as text is not coerced to one). A key
  at `restricted`, auth `off` and a shared secret are never refused, and the gate
  reads no document for them.
- Whatever the caller, a row whose `item_id` names a stored document is written
  carrying that document's mirrored metadata (`data_classification`,
  `lifecycle`, `content_tags`, `auto_importance`; the same keys, and the same
  copy-if-present, remove-if-absent rule, as `sync_vector_metadata`, plus
  `evidence_ref`, which names a successor to the pack builder), over
  anything the row's own `metadata` says. The semantic axis builds a pack item,
  excerpt included, from this row, so a rewrite that left it unlabelled would
  serve a restricted document at a lower clearance. A chunk id is mirrored from
  its own stored document, as the chunk embed path does; the parent's label is
  not copied onto it but read through the chunk id when the row is served. A row
  at a stored document also takes that document's content (the excerpt a
  semantic hit serves, cut as the embed path cuts it) and role (`node_role`,
  `node_type`, `agent_id`, `document_form`, the legacy flat `content_type`
  that `document_form_of` reads as a form, removed where the document has
  none), not the caller's. An id that names no document is written as sent. The trace and memory extraction
  hooks keep running at `restricted`: they write graph drafts, which target no
  document.
- `POST /api/v1/vectors` also refuses, with a 422 and nothing written, a row
  the semantic axis could not serve (it skips and logs such a row,
  `semantic_row_skipped_malformed_metadata`, so an accepted one would answer 200
  and then never appear in a pack): a `metadata.content` or
  `metadata.excerpt` that is not a string (serving reads `content`, else
  `excerpt`, and truncates it; a non-string `excerpt` is refused whether or not
  `content` is present); a `metadata.auto_importance` that is not a finite number
  (a bool is not one); and an `auto_importance` at or above the importance decay
  threshold (0.5 by default) with no `importance_scored_at`, in `metadata` or in
  `metadata.content_tags`. The detail names the row and field
  (`vectors[1].metadata.content must be a string`). It is judged on what the
  caller sent, whether or not the row's document will overwrite the key
  (`auto_importance` is mirrored). An `item_id` starting `trace-summary:` is a 422
  for every caller, `admin` and auth `off` included: that namespace is the
  trace-embed worker's, and a vector row under it makes the worker treat the trace
  as embedded and skip it for good.
- A `vector` that is present must be a list of numbers (a bool is not one), each
  finite and within float32 range, or the row is a 422 (`vectors[1].vector[3]
  must be a finite number within float32 range`): the store keeps float32, where
  a larger value becomes `inf` and the row's score `NaN`, which also breaks the
  pack's audit event. Its squared length must stay under 1e38 as well, because
  the store's float32 norm overflows past about 3.4e38 even when every element
  fits (`[3e38] * 8` scores `NaN` against an ordinary query). Its float32 norm
  may not be zero (`[0.0] * 8`, `[1e-30] * 8` and `[1e-23] * 8` all are, the last
  two because the sum of squares underflows), as the SQLite store skips a norm-0
  row and it would never be served. Its length must equal the store's width when
  the store declares one (`VectorStore.dimensions`; pgvector, ArcadeDB and Neo4j
  do). SQLite declares none and the store interface has no way to ask what width
  its rows hold, so there the first non-empty vector of the batch sets the width
  the rest must match and a batch is not checked against rows already stored: a
  later batch of another width is accepted on SQLite. A query there scores only
  the rows of its own width and skips the rest (#1058), logging one
  `vector_query.width_mismatch` warning when no row of its width is left. Only a missing, `null` or empty-list `vector` is not
  judged (the second pass counts it in `errors`); `0`, `false`, `""` and `{}`
  are not lists and are a 422.
- A row whose `item_id` names no stored document (a chunk id with no chunk
  document included) may not carry `metadata.lifecycle` or `metadata.evidence_ref`
  (422, whatever the value and the caller): the pack builder withholds by id, so a
  `lifecycle` of `{"state": "superseded", "superseded_by": ...}` at an id would
  withhold every item holding that id, the real one from another axis included,
  and an `evidence_ref` makes the row the successor another item's
  `superseded_by` resolves to. At a stored document the keys are not refused: the
  document's own `lifecycle` and `evidence_ref` are written over them. These two
  keys are the only metadata refused at such an id; every other key is stored as
  sent. Only a row that carries one of these keys costs the first pass a
  document read. The write drops them again, without an error, if the document
  has been deleted by the time the row is mirrored, so a delete between the two
  reads cannot let them through.
- For a chunk id (`<parent>#chunk-<digits>`) a written row's `parent_doc_id` is
  the chunk document's own, else the parent the id names, over whatever the row
  supplied; for any other id it is kept as supplied. Serving trusts a row's
  `parent_doc_id` before the id when it judges a chunk's parent, so a supplied
  one could lower a chunk's effective label. Only a `restricted` key or auth
  `off` can write a restricted chunk's row (a lower key is refused 409 first), so
  those are the callers this closes it for.
- `POST /api/v1/vectors` is all-or-nothing only in its first pass (the 422s, the
  409 and the unreadable-id error above). The second pass writes row by row. A
  row with no `item_id` (absent, `null` or empty) or no `vector` (absent, `null`
  or empty) is counted in `errors` and stores nothing, and so is a row whose
  document read or upsert fails, with the rest still landing
  (`{"upserted": 1, "errors": 1}`, status 200). If a relabel's own vector sync
  runs between the route's read of the document and its upsert, the upsert
  overwrites the synced row, and it stays stale until a later sync or
  `trellis admin resync-vector-metadata`.
- The credentialed write routes `POST /api/v1/links`, `/entities`, `/feedback`,
  `/traces`, `/observations` and `/measurements` build their executor at the
  key's clearance, as `/documents` and `/evidence` do, instead of at
  `restricted`. None of those operations names a document, so the floor has
  nothing to refuse on them today; the clearance is threaded so a later rule
  that does cannot be bypassed by a route that forgot it.
- **Accepted posture: the 409 tells a key the document exists.** An
  `evidence.ingest` overwrite refusal (`POST /api/v1/evidence`,
  `POST /api/v1/documents`, `POST /api/v1/commands/batch`) and the
  `POST /api/v1/vectors` refusal name the document id, so a key below a
  document's label can learn that the id is taken by trying to write it. This is
  the same posture as the Stage-2 refusals above and is accepted for the same
  reason: answering as for a fresh id would mean either writing over the
  document or reporting a success that wrote nothing, and a write that
  silently does not happen is worse than a refusal that names its target. A
  document's label, content and metadata are never in the refusal.

A trusted-local caller (stdio, auth `off`, the CLI) holds `restricted` and is
never refused. The clearance is a required keyword of `build_curate_executor`,
so a surface that forgets to name one fails at the call.

### Managing keys

```bash
# Mint a key — the token is printed ONCE and never stored
trellis admin api-keys create --name ci-reader --scopes read --format json

# Multiple scopes
trellis admin api-keys create --name ingest-bot --scopes read,ingest

# List keys (key_id, name, scopes, created_at, revoked — never the hash)
trellis admin api-keys list --format json

# Revoke (exits non-zero if unknown or already revoked)
trellis admin api-keys revoke <KEY_ID> --format json
```

### Production exposure

Three env toggles control what an unauthenticated caller can reach.
All three validate at startup — an unrecognized value crashes
`create_app` rather than guessing a posture.

| Env var | Values | Default | Effect |
|---------|--------|---------|--------|
| `TRELLIS_UI_ENABLED` | `true` / `false` | `true` | `false`: `/ui` is not mounted and `/` redirects to `/api/version` instead of `/ui/`. |
| `TRELLIS_OPS_DETAIL` | `authenticated` / `public` | `authenticated` | Who sees the per-backend `/readyz` breakdown (backend names, latencies, raw error strings). The `{"status": ...}` line is always public, so orchestrator probes need zero credentials. `public` restores the pre-gating full body for everyone. |
| `TRELLIS_METRICS_PUBLIC` | `true` / `false` | `true` | `false`: `/metrics` requires a valid credential (any scope) and returns 401 otherwise. `true` keeps it open for credential-less Prometheus scrape jobs. |

"Authenticated" for `/readyz` detail and gated `/metrics` means
*any* valid credential — no specific scope is required. The check
follows the effective `TRELLIS_AUTH_MODE`: in `off` every request
counts as authenticated (dev behavior unchanged), in `optional`
anonymous callers pass with full scopes, so the gates only bite in
`required` mode. A presented-but-invalid credential always gets 401
on these endpoints — it is never silently downgraded to the
anonymous minimal response.

**UI key flow.** The static UI at `/ui` stores an API key in browser
`localStorage` and sends it as `X-API-Key` on every API fetch. Use the
key icon in the nav bar to set or clear the key; when any API call
returns 401, the UI surfaces the key-entry banner automatically. Mint
a key with `trellis admin api-keys create` — the dashboard's stats /
health / effectiveness cards, the Events and Packs tabs, and the
Reports tab are all `admin`-scoped, while the graph, traces,
precedents, memories, and policies views need only `read`, so an
`admin` key covers the whole UI.

---

## REST API

Start with `trellis admin serve` or `trellis-api`. Base path: `/api/v1/`.

### Ingest

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| POST | `/traces` | Trace JSON | Ingest a trace |
| POST | `/evidence` | Evidence JSON | Ingest evidence |

### Retrieve

| Method | Endpoint | Params/Body | Description |
|--------|----------|-------------|-------------|
| GET | `/search` | `?q=...&domain=...&limit=20&include_chunks=false` | Full-text search; `limit` is 1 to 500, anything else a 422. `<parent>#chunk-N` fragment rows are **excluded by default** (#396) — they are slices of documents the same search already ranks, and on the reference deployment 56% of rows are chunks. The exclusion is pushed into the store rather than applied to the response, so the result set refills with the documents the fragments were sliced from instead of simply getting shorter — a short result set still means "that is all there is". Pass `include_chunks=true` for the unfiltered set; the applied setting is echoed on the response. |
| POST | `/packs` | `{intent, domain?, max_items?, max_tokens?, run_id?, intent_family?}` | Assemble context pack. `run_id` / `intent_family` ride the `PACK_ASSEMBLED` event so the learning loop can credit the run and bucket the intent instead of falling back to `unknown-run` / `general_context`; `intent_family` is derived from `intent` when omitted. |
| GET | `/entities/{id}` | — | Get entity with subgraph |
| GET | `/graph/search` | `?q=...&node_type=...&sort=created_at&order=desc&limit=50&offset=0` | Page current graph nodes, with their `total`. `q` matches a substring of the name, `node_id` or `node_type` in any case (ASCII letters only on SQLite); `%`, `_` and `\` are literal. |
| GET | `/graph/search/facets` | `?q=...` | Count current graph nodes per stored `node_type` under `/graph/search`'s `q`; the counts sum to its `total`. |
| GET | `/traces` | `?domain=...&limit=20` | List traces; `limit` is 1 to 500, anything else a 422 |
| GET | `/traces/{id}` | — | Get trace by ID, with `evidence_links`: for each evidence ref that something can show, keyed by `evidence_id`, `{document_id}` when a document holds the evidence record (the evidence writers store it under its `evidence_id`), else `{entity_id}` when trace extraction wrote its `evidence:<id>` graph node. A ref with neither, or with an empty `evidence_id`, has no entry. `evidence_links` is `null` when the knowledge stores cannot be read; the trace is still served. |
| GET | `/precedents` | `?domain=...&limit=20` | List precedents. Excludes precedents whose graph node is stamped `Lifecycle.state="demoted"` or `"archived"`; the filter applies before `limit`. A `payload.source_item_id` naming a document above the key's clearance is left out of that row's `payload`. |
| GET | `/precedents/domains` | — | Count promoted precedents per domain, for a domain filter's options. Excludes demoted/archived precedents, same filter as above. |

### Explore

A mixed-scope router: `/documents` and `/graph/history` stay `read`;
the four pack-telemetry routes below (`/events`, `/events/{event_id}`,
`/packs`, `/packs/{pack_id}`) each carry their own route-level `admin`
requirement, because they replay other callers' pack telemetry —
intents, served item ids and titles, attribution.

| Method | Endpoint | Scope | Params/Body | Description |
|--------|----------|-------|-------------|-------------|
| GET | `/documents` | `read` | `?q=...&limit=50&offset=0&include_chunks=false` | List or search documents, previews only. A search (`q` set) is paged the same way the unfiltered list is — `offset`/`limit` both apply to the matches, not just to the unfiltered case. Nothing in `DocumentStore` counts a query's total matches, so a search captures up to a bounded scan window once and pages that capture; `total_exact: false` on the response means the query matched at least that many documents, so `total` is a floor, not an exact count. |
| GET | `/documents/{doc_id}` | `read` | — | Get one document by id. 404 when absent. |
| GET | `/graph/history` | `read` | `?entity_id=...` | All versions of a graph node, newest first (SCD-2 audit trail). 404 when the entity has none. |
| GET | `/events` | `admin` | `?event_type=...&entity_id=...&since=...&until=...&order=desc&limit=100&include_payload=false` | Tail or filter the event log, newest first by default. `GET /events` has no offset — page older history by passing the oldest `occurred_at` you have as `until`. |
| GET | `/events/{event_id}` | `admin` | — | Get one event by id, independent of whatever recency window a list caller has loaded (`GET /events` pages by `until`, with no `event_id` filter, so an older row is otherwise unreachable by id alone). 404 when absent. |
| GET | `/packs` | `admin` | `?until=...&limit=50` | List assembled context packs (newest first), summary fields only. |
| GET | `/packs/{pack_id}` | `admin` | — | Full pack telemetry: what was injected, rejected, and why, plus joined feedback (capped at 50 rows; `feedback_total` and `feedback_truncated` report the true count). 404 when absent. |

### Curate

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| POST | `/precedents` | `{trace_id, title, description}` | Promote trace |
| POST | `/precedents/{target_id}/demote` | `{reason, requested_by?}` | Demote precedent (SCD-2 rewrite, not a purge; reversal is `/precedents/{target_id}/reinstate` below -- see `trellis curate demote-precedent` above) |
| POST | `/precedents/{target_id}/reinstate` | `{reason, requested_by?}` | Reinstate a demoted precedent (SCD-2 rewrite back to `"current"`, vector row re-synced; refuses a non-`"demoted"` target or the automatic-requester identity -- see `trellis curate reinstate-precedent` above) |
| POST | `/links` | `{source_id, target_id, edge_kind?}` | Create edge |
| POST | `/entities` | `{entity_type, name, properties?}` | Create entity |
| POST | `/feedback` | `{target_id, rating, comment?, pack_id?}` | Record feedback (pass `pack_id` so it joins) |
| POST | `/packs/{pack_id}/feedback` | `{rating, success?, comment?}` | Pack-specific feedback |

### Admin

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| GET | `/stats` | Store statistics |
| GET | `/effectiveness` | Context effectiveness report |
| GET | `/metrics/timeseries` | Improvement-metric trend series (see below) |

### Review queue (admin scope)

The Review view in the static UI (`/ui/` → **Review**) is a human-decision
inbox. Every endpoint below is on the admin router, so it requires the
`admin` scope, respects `TRELLIS_UI_ENABLED` / ops-gating, and — for the
write actions — emits a `REVIEW_DECISION_RECORDED` audit event stamped
with the authenticated key identity (in addition to the surface-specific
event the underlying pipeline already emits). The autonomy tiers that
decide which surfaces are human-gated are described in
`docs/design/adr-autonomy-ladder.md`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/proposals` | List pending tuner proposals with `effect_size` / `sample_size` / `baseline_values` / `proposed_values` |
| GET | `/proposals/{id}/preview` | Dry-run a promotion — predict promote/reject, mutate nothing |
| POST | `/proposals/{id}/promote` | Promote through the governed pipeline (same logic as `trellis metrics promote --commit`) |
| POST | `/proposals/{id}/reject` | Reject (human-gated); body `{reason?}` |
| GET | `/learning/candidates` | Serve the most-recent `intent_learning_candidates.json` artifact (`status: "error"` + `code` + a `hint` naming the path when there is none to read) |
| POST | `/learning/promotions` | Promote approved candidates via `MutationExecutor`; body `{decisions: [{candidate_id, approved, rationale?}]}`. The response's `status` is the verdict `promote-learning` states (`ok` / `refused` / `failed`, from the same function; an approved row with a blank `candidate_id` makes it `refused`, and rows that promoted still landed), `named_count` the approved rows that name a candidate, and `promoted_count` the rows this run fully wrote, `already_promoted_count` the rows whose precedent already held the promotion and were written not at all (re-submitting is safe; the row reads `already_promoted`); the HTTP status is 200 for every verdict |
| GET | `/schema-evolution/candidates` | List latest `WELL_KNOWN_CANDIDATE` event per `candidate_id` |
| POST | `/schema-evolution/{id}/draft-adr` | Render the promotion-ADR markdown (copyable/downloadable in the UI). **Only** action — there is no promote endpoint; promotion is a one-way ADR commitment |
| GET | `/code-proposals` | List recent `PROPOSAL_DRAFTED` events with `markdown_preview` (read-only) |

**Sections in the UI.** The Review view has four collapsible queue sections,
each with a live count:

1. **Tuner proposals** — Approve / Reject buttons. Approve first runs the
   dry-run preview and shows the predicted decision before a confirm step,
   and disables the confirm button when that preview predicts a
   rejection (nothing it could do but spend the one proposal). Approve
   wraps `promote_proposal`; Reject wraps `reject_proposal`. Both routes
   and the CLI (`trellis metrics promote`) share the same
   `trellis.learning.tuners.promote_proposal` / `preview_promotion`
   functions, the same unconditional reachability check (refused even
   under `force`), and — since both now default `allow_no_baseline` to
   `False` — the same baseline requirement: a scope's first promotion is
   refused by default on either surface. A no-baseline refusal is not
   terminal (the proposal stays `pending`, not `rejected`), so it can
   still be bootstrapped later — but only from the CLI, via
   `trellis metrics promote --allow-no-baseline`, which skips only
   `PromotionPolicy.allow_no_baseline`, not min-sample-size, min-effect-
   size, reachability, or the immutable-core check. The Review queue
   exposes no equivalent override, so a scope with no snapshot yet can
   never clear its own first refusal from the UI alone. The CLI's
   `--force` is the coarser, pre-existing escape hatch: it skips the
   whole policy gate, including the baseline requirement, but never the
   reachability or immutable-core refusals (which no flag skips).
2. **Learning promotion candidates** — candidate cards with metrics, an
   approve checkbox + rationale field, and a single submit that runs the
   existing `prepare_learning_promotions` → `MutationExecutor` path.
3. **Schema-evolution candidates** — only a **Draft ADR** action, which
   returns the rendered ADR markdown in a copyable / downloadable panel.
   No approve/promote button (a tooltip explains why).
4. **Code-authoring proposals** — read-only list with markdown preview.

**Learning artifacts directory.** `GET /learning/candidates` and `POST
/learning/promotions` read the `intent_learning_candidates.json` artifact
that `trellis worker curate` and `trellis analyze learning-candidates`
write. The writers and the server resolve the directory the same way
(`trellis.learning.resolve_learning_artifacts_dir`):
`TRELLIS_LEARNING_ARTIFACTS_DIR` when set, else `<data_dir>/learning`,
beside `stores/`. A writer run with no `--output-dir` against the API's
data directory therefore lands where the queue reads; a deployment that
sets the variable must set it on the writer and the API alike. The list
response names the directory in `artifacts_dir`. When there is nothing to
serve, it still answers `200`, but with `status: "error"`, a `code`
(`learning_artifacts_dir_missing`, `learning_candidates_missing`,
`learning_candidates_unreadable` or `stores_dir_unconfigured`) and a
`hint` naming the path, so an unwritten artifact never reads as an empty
queue. The promote endpoint answers `409` with `detail: {code, message,
path}` for the same four conditions.

### Improvement-metrics dashboard (admin scope)

The **Metrics** view in the static UI (`/ui/` → **Metrics**) charts five
improvement metrics over time. Every series is computed **server-side from
the EventLog on read** — there is no new storage and no caching layer (POC
scale). The endpoint is on the admin router, so it requires the `admin`
scope and respects `TRELLIS_UI_ENABLED` / ops-gating like the rest of
admin. It is read-only and never mutates a store.

```
GET /api/v1/metrics/timeseries?metric=<name>&days=<n>&bucket=day&group_by=<axis>
```

| Param | Values | Default | Notes |
|-------|--------|---------|-------|
| `metric` | one of the five below | — (required) | Unknown value → `422` |
| `days` | positive int | `30` | Look-back window; non-positive → `422` |
| `bucket` | `day` | `day` | Only daily buckets are implemented; anything else → `422` |
| `group_by` | `domain` \| `intent_family` \| `none` | `none` | Unknown value → `422` |

The five metrics (priority order; each a named `metric` value):

| Metric | Definition |
|--------|------------|
| `pack_success_rate` | Share of graded packs with a positive outcome per bucket (`PACK_ASSEMBLED ⋈ FEEDBACK_RECORDED` on `pack_id`, same join as `learning/pack_observations.py`). |
| `reference_rate` | `items_referenced / items_served` per bucket — the best "are packs getting better" proxy. Pooled per bucket (sum referenced / sum served). |
| `advisory_fitness` | Mean advisory confidence per bucket (from the fitness loop's `ADVISORY_SUPPRESSED` / `ADVISORY_RESTORED` `new_confidence`); `sample_count` is the suppressed-advisory count. |
| `noise_tag_volume` | Items flipped to `signal_quality="noise"` per bucket, counted from `TAGS_REFRESHED` audit events whose `after` tags carry the noise label. |
| `parameter_promotions` | Governance event counts per bucket (`PARAMS_UPDATED` + `TUNER_PROPOSAL_CREATED` / `_REJECTED` + `PARAMETERS_DEGRADED`). There are no `PARAMS_AUTO_*` events in this tree; `parameter_promotions` groups by event **type**, not by `domain` / `intent_family`. |

**Response shape.** A list of series, each with a `group_key` and a list of
`{bucket_start, value, sample_count}` points sorted ascending. **Buckets with
no data are omitted** (not zero-filled) — clients infer gaps from the missing
`bucket_start` keys (an absent day means "no signal", not "zero"). Grouping
resolves `domain` from the `PACK_ASSEMBLED` payload and `intent_family` from
the `FEEDBACK_RECORDED` payload; events lacking the requested dimension fall
under `"all"`. The aggregation lives in
`trellis.retrieve.metrics_timeseries.compute_timeseries` (the route is a thin
adapter).

**Definitional parity.** Where a metric overlaps with the agent-loop
convergence scenario, the formula matches that scenario's helpers in
`eval/scenarios/_convergence_common.py` (`pack_success_rate` ↔
`round_success_rate`; `reference_rate` ↔ the useful-fraction in
`_base_round_metrics` / `_convergence_stats`; `advisory_fitness`'s suppressed
count ↔ `advisories_suppressed_total`). Each shared formula is cross-referenced
in a code comment at its call site.

**UI.** Metrics 1–4 render as inline-SVG line charts (zero dependencies, no
build step); `parameter_promotions` renders as an annotated events strip
(grouped bars by event type). A domain / intent-family group-by selector and a
day-window selector drive all charts.

---

## MCP Macro Tools

Start with `trellis-mcp`. 16 tools: 10 core tools and 3 sectioned-context tools for richer pack assembly return markdown, and 3 structured tools (observations + the mutation escape hatch) return JSON.

**Core tools**

| Tool | Args | Returns |
|------|------|---------|
| `get_context` | `intent`, `domain?`, `max_tokens?`, `session_id?`, `run_id?`, `sections?`, `index?`, `refresh?`, `retrieval_phase?` | Markdown pack fusing keyword + graph + semantic axes (RRF, recency/importance decay, session dedup) with a citable `pack_id`. Pass `sections` for the sectioned layout. Pass `run_id` (the unit of work this context is for — narrower than `session_id`) so later feedback can credit the runs a memory actually helped; without it the learning join buckets the pack under `unknown-run`. Pass `index=True` for the id index (see "Progressive disclosure" below); mutually exclusive with `sections`. Pass `retrieval_phase="post_hoc"` when retrieving after the work is done, e.g. to check conclusions — `"pre_work"` and `"mid_task"` are the other allowed values; an unrecognised value is refused (`INVALID_PARAMS`), never silently stored. |
| `save_experience` | `trace_json` | Confirmation with trace_id. The server sets `metadata.project` before ingest ([trace-format.md § Project stamp](trace-format.md#project-stamp)) |
| `save_knowledge` | `name`, `entity_type?`, `properties?`, `relates_to?`, `edge_kind?`, `content?`, `evidence_ref?`, `supersedes?` | Confirmation with entity_id. `supersedes` names an entity this one replaces (see "The stamp's first deterministic producer" above). |
| `save_memory` | `content`, `metadata?`, `doc_id?`, `supersedes?` | Confirmation with doc_id. `supersedes` names a document this memory replaces. Tags the document inline when `TRELLIS_ENABLE_CLASSIFY_ON_INGEST=1` (see "Document → content tags"), embeds it when `TRELLIS_ENABLE_EMBED_ON_INGEST=1`. A `doc_id` that names a redacted document is refused (`redaction_tombstone_overwrite`) with nothing written: an agent's save is not the owner's decision to bring a redacted id back, and `save_memory` has no parameter for it. The error names the ways that can: `trellis ingest evidence --reingest-redacted`, `execute_mutation` with `evidence.reingest_redacted=true`, or `reingest_redacted: true` in the body of `POST /api/v1/evidence` or `POST /api/v1/documents`. |
| `get_lessons` | `domain?`, `limit?`, `max_tokens?` | Markdown list of precedents, excluding demoted/archived ones |
| `get_graph` | `entity_id`, `depth?`, `max_tokens?` | Markdown subgraph. Renders each node's `document_ids` as copy-pastable evidence pointers — an `Evidence documents` section for the root entity, an inline `docs:` suffix per neighbor — so entity → evidence is followable with `get_items`. |
| `get_items` | `item_ids`, `pack_id?`, `max_tokens?` | Markdown bodies for known ids (max 50), resolved against the document store, the graph, then the trace store. Items over budget are omitted whole with their ids listed for a follow-up call — never truncated; unknown ids are listed as not found. Emits `PACK_ITEMS_FETCHED` with the served ids; pass the `pack_id` that surfaced them to keep the fetch attributable. |
| `record_feedback` | `trace_id?`, `pack_id?`, `success?`, `rating?`, `notes?`, `helpful_item_ids?`, `unhelpful_item_ids?`, `followed_advisory_ids?` | Confirmation |
| `search` | `query`, `limit?`, `max_tokens?`, `index?`, `retrieval_phase?` | Markdown search results |
| `get_file_context` | `paths`, `include_unconfirmed?`, `max_tokens?` | Markdown context per file path (#307): documents that name the path — as their own `metadata.source_path` (exact, or a `/`-boundary suffix match so absolute paths find stored relpaths) **or** as a member of `metadata.files_touched`, the repo paths a trace's tool calls demonstrably modified (#549) — plus graph entities doc-linked to them. Each document reports `matched_via`, and a `files_touched` hit renders annotated: a record of *changing* the file is a different claim from the file's own documentation. Every item carries store timestamps and each path a `Newest memory` line so a client can staleness-gate against the file's mtime. Unconfirmed extraction mints are excluded unless `include_unconfirmed=true` (#301). |

**Sectioned-context tools (deprecated aliases — #262)**

All three now route through the same one retrieval path as `get_context` and are retained as thin aliases for one release. Prefer `get_context` — for custom sections pass `get_context(intent, sections=[...])` (identical schema to `get_sectioned_context`); the objective/task presets are fixed section layouts over the same path.

| Tool | Args | Returns |
|------|------|---------|
| `get_objective_context` | `intent`, `domain?`, `max_tokens?`, `session_id?`, `retrieval_phase?` | Markdown pack with fixed `Domain Knowledge` + `Operational Context` sections; designed to be called once at workflow start. |
| `get_task_context` | `intent`, `entity_ids?`, `domain?`, `max_tokens?`, `session_id?`, `retrieval_phase?` | Markdown pack scoped to specific entities; designed for per-step retrieval inside a workflow. The ids also seed the graph axis (one hop, merged with seeds derived from the intent, at most 8 in all, the ids you name first), so a precedent attached to a named entity is reachable however many newer nodes exist; an id the graph does not hold is ignored. |
| `get_sectioned_context` | `intent`, `sections`, `domain?`, `max_tokens?`, `session_id?`, `retrieval_phase?` | Markdown pack with caller-defined sections (custom affinities, content types, scopes, per-section budgets). |

**Structured tools (JSON, not markdown)**

| Tool | Args | Returns |
|------|------|---------|
| `record_observation` | `subject_entity_id`, `subject_entity_type`, `observer_agent_id`, `content`, `confidence`, `evidence_ref?`, `metadata?` | JSON `{"status": "ok", "observation_id": ...}`. Lands an `Observation` node with a `hasObservation` edge from the subject — see [schemas.md → Observation](schemas.md#observation). Routed through the governed pipeline (`observation.record`). |
| `query_observations` | `subject_entity_id?`, `observer_agent_id?`, `limit?` | JSON `{"status": "ok", "observations": [...]}` — each entry is the Observation property dict plus `node_id` and `node_type`. |
| `execute_mutation` | `operation`, `args`, `idempotency_key?`, `actor?` | JSON `CommandResult`. MCP parity with `POST /commands/batch` for a single command; same five-stage governed pipeline, so policy gates and audit events apply identically. One difference: for `trace.ingest` the server sets `metadata.project` before ingest and targets the command at the trace (`target_type: "trace"`, the trace's id), as for `save_experience` ([trace-format.md § Project stamp](trace-format.md#project-stamp)). `operation` accepts the wire value (`"link.create"`) or the enum key (`"LINK_CREATE"`). Any operation outside `NON_DESTRUCTIVE_OPERATIONS` (entity merge, redaction, retention prune, alias rebind, link/label removal, schedule/settings/classify-rule writes) is refused before a `Command` is built unless the caller holds `admin`: over HTTP transport this reads the FastMCP access token's scopes; on stdio, the trusted local transport, it is allowed unconditionally. The refusal is the tool's normal error reply (`POLICY_DENIED`), not a fabricated success. `actor` stays a caller-chosen audit label; `admin` does not verify it. |

`session_id` lets `get_context` and the three sectioned tools deduplicate items returned by recent calls in the same session. `max_tokens` defaults to 2000, and `get_items` to 4000. The three sectioned tools default to `retrieval.budgets` in `config.yaml`, or 4000 if it is unset; pass `max_tokens > 0` to override it.

All read tools track token usage in the event log for observability.

### Progressive Disclosure — index, traverse, fetch

Reading a full pack to decide which items were worth reading is the expensive way round. Three layers let the model filter ids before paying for bodies:

1. **Index** — `get_context(intent, index=True)` (or `search(query, index=True)`) renders one line per item: `- `item_id` (type, ~N tok) Title`. No excerpt bodies, so the same `max_tokens` surveys far more items. The label is the item's `title` metadata when it has one, otherwise the head of its excerpt; `~N tok` is what serving that item in a full pack *would* have cost.
2. **Traverse** — `get_graph(entity_id)` renders each node's `document_ids` as evidence pointers, so an interesting entity leads to the documents that attest it.
3. **Fetch** — `get_items(item_ids=[...], pack_id=...)` returns the bodies actually wanted.

The index is a **real pack**, not a preview: same `pack_id`, same `PACK_ASSEMBLED` telemetry (per-item hashes, `injected_items`, session dedup), so `record_feedback(pack_id=..., helpful_item_ids=[...])` attributes exactly as it does for a full pack. Only the budget charge and the rendering differ — the payload carries `index_mode: true` so analysis can tell the two serve shapes apart, and an index serve counts as a serve for session dedup (`refresh=True` is still the re-injection escape hatch).

Passing `pack_id` to `get_items` is what keeps the fetch attributable: the `PACK_ITEMS_FETCHED` event records `served_item_ids` against the pack whose index surfaced them, so "fetched but graded unhelpful" is distinguishable from "never fetched at all".

```
get_context("pgbouncer failover", index=True)
  → # Context index for: pgbouncer failover
    **pack_id:** `01HABCDEF...`
    - `doc-7` (document, ~410 tok) Failover runbook
    - `svc-api` (entity, ~28 tok) API Gateway

get_graph("svc-api")   → ## Evidence documents (1)
                         - `doc-7`

get_items(["doc-7"], pack_id="01HABCDEF...")   → full body
```

### Capture-health banner

Every pack-returning tool — `get_context` (flat and sectioned), `search`, and the three sectioned aliases — prepends a one-block warning when a **write** surface has gone dark (#309):

```markdown
> **WARNING: memory capture is failing.** 5 write attempt(s) rejected and 0 accepted in the last 24h from: mcp:save_experience (since 2026-08-21 04:12 UTC). New experience from this session is NOT being saved. Diagnose with `trellis analyze health`.
```

The rule is **per surface**, evaluated over a trailing window: a surface warns when it has at least `TRELLIS_CAPTURE_WARN_THRESHOLD` rejected writes — boundary `WRITE_REJECTED` (#297) plus executor `MUTATION_REJECTED`, aggregated under one `mcp:<tool>` label — and **no** accepted write of its own. Per-surface, not global, because the incident this exists to catch has successful writes in the same window by construction: every MCP `save_*` call rejected while a nightly `trellis ingest corpus` keeps landing rows. A global "zero accepted anywhere" rule would stay silent through exactly that outage.

Executor rejections with `reason="idempotency_replay"` are excluded — a replayed command is a duplicate submission of a write that already landed, not a write that went dark.

**Which surfaces warn, and what counts as an accept** (#461):

| Surface | Raises the banner? | Accept event that clears it |
|---|---|---|
| `mcp:save_experience`, `mcp:save_knowledge` | yes | `MUTATION_EXECUTED` with that `requested_by` |
| `mcp:save_memory` | yes | `MUTATION_EXECUTED` **or** `MEMORY_STORED`, either carrying that `requested_by` |
| `mcp:record_feedback` | **no** — grading, not capture | n/a; rejections still counted by `trellis analyze health` |
| `config:policy_file` | yes | any accepted **governed** write (`MUTATION_EXECUTED`, any `requested_by`) newer than the last rejection (see [Policy Commands](#policy-commands)). `MEMORY_STORED` does **not** clear it — widening the accept set for `save_memory` did not widen this rule |
| `(unknown)` | yes | nothing — deliberately unclearable |

Two labels used to be structurally unclearable. `mcp:save_memory`'s only `MUTATION_EXECUTED` then came from memory extraction (a stored memory now carries its own, from the `evidence.ingest` command it submits, so the premise no longer holds, but `MEMORY_STORED` stays the executor-independent signal), which is gated on `TRELLIS_ENABLE_MEMORY_EXTRACTION` (**off** by default) and emits nothing when extraction yields no drafts — so on a default deployment a perfectly healthy `save_memory` cleared nothing and three rejections pinned the banner for a full window. `MEMORY_STORED` is its unconditional success signal and now clears it. And `mcp:record_feedback` captures no experience at all: its success path is `FEEDBACK_RECORDED`, which no `MUTATION_EXECUTED` accompanies, so the headline *"New experience from this session is NOT being saved"* was false in both halves. It is out of the banner and unchanged in `trellis analyze health`. Exclusion is a deny-list — a surface is watched unless explicitly named — and `tests/unit/mcp/test_capture_surface_roster.py` enumerates every boundary-rejection site, fails if its tool cannot demonstrate an accept by actually making a successful call, and separately sweeps all of `src/` — both call spellings — so no module outside `mcp/server.py` can raise a banner no roster has classified, while inside `mcp/server.py` every emitter call must route through the wrapper the roster scans for. The hand-written success recipes are asserted against the scan too: every other guard divides by the scan's own output, so only a hand count catches the scan silently shrinking.

One residue, named rather than hidden: `save_memory` emits nothing on a *dedup* hit, so a surface whose every successful call is a duplicate has no accept to clear with. It also produces no rejections, so it cannot raise the banner on its own.

| Knob | Default | Notes |
|------|---------|-------|
| `TRELLIS_CAPTURE_WARN_THRESHOLD` | `3` | Rejections one surface needs before it warns (claude-mem's three-consecutive-failures rule). **`0` disables the check entirely** — and skips the store round-trip. |
| `TRELLIS_CAPTURE_WARN_WINDOW_HOURS` | `24` | Trailing window the counts are taken over. |

These are read-side knobs — they shape what retrieval *serves*, not what ingest *writes* — so they live in `src/trellis/ops/capture_health.py`, not `write_config.py`, and do **not** appear in `trellis admin write-config`.

Two properties worth knowing before you read a banner:

- **It rides outside `max_tokens`.** The pack is budgeted first and the banner prepended after, because a warning the budget can evict is a warning that disappears on exactly the small packs a dark capture path produces. Budget for ~60 extra tokens while capture is down.
- **The empty state carries it too.** A pack with no items and no banner means greenfield; a pack with no items and a banner means the writes never landed. Distinguishing those is the point.

The check is advisory telemetry and fails soft: if it raises, the pack is served unbannered (the `track_token_usage` GRACEFUL-DEGRADATION posture). Indeterminate presents as healthy. Run `trellis analyze health` for the full per-tool accept/reject breakdown and rejection-kind taxonomy.

### Citing Pack Elements in Feedback

Every pack-assembling tool — the three sectioned-context tools and the flat `get_context` / `search` paths alike (#392, decision-ledger D-4) — renders each response with a `pack_id` header and full item/advisory IDs in backticks so agents can cite specific elements when calling `record_feedback`. Empty packs are no exception: the header and footer appear at every holdout rate, so an empty pack's advisories can be cited too. Advisories sit above the footer, so the footer is the last thing in the response:

```markdown
# Context for: deploy checklist
**pack_id:** `01HABCDEF...`

## Domain Knowledge
- `doc_ownership_rules` (document, 0.82): Ownership rules for platform...

## Advisories
1. `adv_01HXYZ` **[pattern]** Always run dry-run first (n=12, effect=+18%) — cite it in `followed_advisory_ids` if you act on it.

---
*Cite feedback via `record_feedback(pack_id="01HABCDEF...", rating=..., helpful_item_ids=[...], unhelpful_item_ids=[...], followed_advisory_ids=[...])`.*
```

When an agent finishes the task, it calls `record_feedback` with the copied IDs:

- `pack_id` (preferred over `trace_id` when feedback follows a context retrieval) — attributes the outcome to the pack.
- `rating` (0.0–1.0) — grades how useful the pack actually was. Omit `success` when grading: it is derived from the rating (≥ 0.5 counts as a success), so a mediocre pack is not recorded as an unqualified win. Passing `success` alone still works and is recorded as `rating` 1.0 / 0.0.
- `helpful_item_ids` / `unhelpful_item_ids` — cite the specific pack items that actually helped or were noise.
- `followed_advisory_ids` — cite the advisories whose guidance was acted on.

These element-level signals land in the `FEEDBACK_RECORDED` event payload so the fitness loops (`trellis analyze apply-noise-tags`, `trellis analyze advisory-effectiveness`) can attribute outcomes more precisely than pack-level success alone.

The cited ids are *not* written as `items_served` — they are what the agent referenced, not what the pack contained. The payload leaves `items_served` empty so the reference-rate metric keeps joining against the pack's own `PACK_ASSEMBLED` `injected_item_ids`; synthesizing it from the citations would report a 100% reference rate for every attributed pack.

The MCP tool and `POST /api/v1/packs/{pack_id}/feedback` share both the mapping (`PackFeedback.from_agent_signal`) and the writer (`trellis.feedback.recording.record_feedback`), so identical inputs mean the same thing on either surface: one call appends the durable `pack_feedback.jsonl` row under `<stores_dir>/feedback/` **and** emits the authoritative `FEEDBACK_RECORDED` event. The emit fails soft — when the event sink is down the tool still returns, the audit row is on disk, and `trellis admin reconcile-feedback --log-dir <stores_dir>/feedback` (or `worker curate --reconcile-first`, which scans that directory) replays it.

### Retrieval Budgets

The three sectioned-context tools (`get_objective_context`, `get_task_context`, `get_sectioned_context`) resolve their token budgets from the `retrieval.budgets` section of `~/.trellis/config.yaml`. This lets you right-size budgets per tool and per domain without touching code.

```yaml
retrieval:
  budgets:
    default:
      max_tokens: 4000
      max_items: 30
    by_tool:
      get_objective_context:
        max_tokens: 5000
        max_items: 25
      get_task_context:
        max_tokens: 2500
      get_sectioned_context:
        max_tokens: 8000
    by_domain:
      orders:
        max_tokens_multiplier: 1.25
```

**Resolution order** (highest to lowest precedence):

1. Caller-supplied `max_tokens` argument (when positive — `0` is the sentinel for "use config").
2. `by_tool.<tool_name>` entry (complete `BudgetSpec`; unspecified fields fall back to the spec's built-in defaults, *not* to the `default` section).
3. `default` section.
4. Hardcoded fallback: `max_tokens=4000`, `max_items=30`.

A `by_domain` multiplier, when present, scales the resolved `max_tokens`/`max_items` before caller overrides are applied. Caller overrides bypass domain multipliers.

### OpenClaw Setup

OpenClaw has native MCP client support. Add Trellis to your `openclaw.json`:

```json
{
  "mcpServers": {
    "trellis-ai": {
      "command": "trellis-mcp",
      "args": []
    }
  }
}
```

Or install via ClawHub:

```bash
clawhub install trellis-ai
```

After restarting OpenClaw, the agent has access to all 16 macro tools above. See [`examples/integrations/openclaw/`](../../examples/integrations/openclaw/) for the full setup guide and skill definition.

---

## Python SDK

```python
from trellis_sdk import TrellisClient

# HTTP client against a running trellis-api
client = TrellisClient(base_url="http://localhost:8420")

# Tests: in-process ASGI client, no network listener
from trellis.testing import in_memory_client
with in_memory_client(tmp_path / "stores") as client:
    ...
```

### Client Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `ingest_trace(trace: dict)` | `str` (trace_id) | Ingest a trace |
| `search(query, domain?, limit?)` | `list[dict]` | Search documents |
| `get_trace(trace_id)` | `dict \| None` | Get trace by ID |
| `list_traces(domain?, limit?)` | `list[dict]` | List recent traces |
| `assemble_pack(intent, **kwargs)` | `dict` | Assemble context pack |
| `get_entity(entity_id)` | `dict \| None` | Get entity |
| `create_entity(name, entity_type?, properties?)` | `str` (node_id) | Create entity |
| `create_link(source_id, target_id, edge_kind?)` | `str` (edge_id) | Create edge |
| `record_feedback(pack_id, success, helpful_item_ids?, unhelpful_item_ids?, followed_advisory_ids?, target_id?, rating?, comment?)` | `PackFeedbackResponse` | Record element-level pack feedback |
| `close()` | — | Release resources |

`record_feedback` mirrors the MCP `record_feedback` tool and routes through
`trellis.feedback.recording.record_feedback` server-side: it appends the durable
`pack_feedback.jsonl` audit row and emits the authoritative `FEEDBACK_RECORDED`
event to the operational EventLog. The returned `PackFeedbackResponse` carries
`event_log_in_sync` — check it to confirm the event reached the log. `False`
means only the JSONL row landed and a reconcile is owed (the emission
soft-failed); the SDK does not swallow that signal. `AsyncTrellisClient` exposes
the same method as a coroutine.

### Skill Functions

Pre-summarized markdown for LLM context injection:

```python
from trellis_sdk.skills import (
    get_context_for_task,
    get_latest_successful_trace,
    save_trace_and_extract_lessons,
    get_recent_activity,
)

context = get_context_for_task(client, "implement retry logic", domain="backend")
```

All skill functions return `str` (markdown), not data objects.

### Workflow Integration Hooks

`trellis_sdk.hooks` packages the pre-task / post-task integration points a
workflow engine needs into three classes. Each wraps a `TrellisClient` and
**degrades gracefully** — a hook method never raises into the host workflow.
On a Trellis outage (server down, 4xx, version mismatch, mid-call drop) the
hook logs a `structlog` warning and returns a sentinel, so the host agent's
task never fails because Trellis is down. Pass `raise_errors=True` to the
constructor to opt into exceptions instead.

| Class | When | Key method(s) | Sentinel on failure |
|-------|------|---------------|---------------------|
| `ContextInjector` | pre-task | `for_intent(intent, domain?, max_tokens?)`, `for_entities(entity_ids, intent?, domain?, max_tokens?)` | `""` (empty markdown) |
| `TraceRecorder` | post-task | `record(step_name, status, duration_ms, entity_ids?, summary?, metrics?, error?, domain?)` | `None` (no trace_id) |
| `ResultFeedback` | post-task | `record_success(target_entity_id, result_name, summary, full_content?, metadata?, pack_id?, helpful_item_ids?)`, `record_failure(target_entity_id, error_summary, trace_id?, pack_id?, unhelpful_item_ids?)` | `HookResult(ok=False)` |

```python
from trellis_sdk import ContextInjector, TraceRecorder, ResultFeedback, TrellisClient

client = TrellisClient(base_url="http://127.0.0.1:8420")
injector = ContextInjector(client)
recorder = TraceRecorder(client, workflow_id="run-42", agent_id="planner")
feedback = ResultFeedback(client)

brief = injector.for_intent("add rate limiting", domain="backend")   # -> markdown
trace_id = recorder.record("plan", "success", 1200, summary="done")   # -> trace_id | None
result = feedback.record_success(                                     # -> HookResult
    target_entity_id="entity:orders-api",
    result_name="rate-limit config",
    summary="token bucket",
    pack_id="pack:abc",          # also grades the supporting pack (positive)
    helpful_item_ids=["doc:1"],
)
```

`ContextInjector` calls `assemble_pack` and falls back to per-entity lookups
(`for_entities`) when the pack is empty, splitting the token budget across
included entities. `TraceRecorder` builds a `source="workflow"` trace tying
every step to `workflow_id` and records failures as well as successes
(`status` is coerced to `unknown` if not one of `success|failure|partial`).
`ResultFeedback` creates a `DOCUMENT` entity + `DESCRIBED_BY` edge on success
and routes pack grading through the SDK's `record_feedback` method (the
authoritative EventLog path) — never a hand-rolled HTTP call. An
`AsyncResultFeedback` variant wraps `AsyncTrellisClient`.

Because the SDK is HTTP-only, the hooks require a running `trellis-api`
server. For tests and examples without a network listener, construct the
hooks against the in-process client from `trellis.testing.in_memory_client`.
A runnable end-to-end demo lives at `examples/hooks_generic_workflow.py`.
