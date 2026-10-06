# Changelog

All notable changes to Trellis will be documented in this file.

## [Unreleased]

### Added

- **Write provenance on emitted events.** Every event emitted through
  `EventLog.emit` now carries `metadata["write_provenance"]`: the build that
  wrote it (version, git sha, dirty flag, and which mechanism resolved it) plus
  the write-behaviour environment the process was launched with (`env_flags`)
  and a short `env_flags_digest` for cheap bucketing. Answers "was this row
  written before or after the fix?", which was previously unanswerable —
  nothing a write left behind recorded which build produced it, and the same
  database is written concurrently by an editable install off a working tree
  and by container images of varying age. The stamp rides the already-free-form
  `Event.metadata`, so it is additive: payload models keep their
  `extra="forbid"` contract, rows written before it existed still parse, and no
  emitter is required to supply one. Resolved once per process (~390 bytes per
  event). `env_flags` is named for what it is — one flag (`memory_extraction`)
  is additionally gated on a caller's `--extract`, so `true` there means the
  environment permitted the behaviour, not that a given write performed it. New
  module `trellis.core.write_provenance`; version resolution in
  `trellis.core.version`.
- **`make docker-build`** — builds the API image with the working tree's
  git-derived version passed in as the `TRELLIS_BUILD_VERSION` build arg. The
  Docker build context excludes `.git`, so `hatch-vcs` has nothing to read and
  every image otherwise bakes the same `fallback-version` — making one image
  indistinguishable from another, which is exactly the drift the stamp exists
  to detect. An image built without it reports
  `version_source: "fallback-version"` and `commit: null` rather than asserting
  a version it cannot vouch for. `docker compose` forwards
  `$TRELLIS_BUILD_VERSION` if it is exported.
- **`trellis.core.write_config`** — one home for the write-behaviour knobs
  (`TRELLIS_ENABLE_CLASSIFY_ON_INGEST`, `..._EMBED_ON_INGEST`,
  `..._MEMORY_EXTRACTION`, `..._RECONCILE_ON_WRITE`, `..._TRACE_EXTRACTION`,
  `TRELLIS_TRACE_EXTRACTION_MIN_CONFIDENCE`, `TRELLIS_RECONCILE_MODEL`,
  `TRELLIS_RECONCILE_TIMEOUT_S`). They were uncorrelated env vars read at five
  different call sites, so which semantics a write received depended on which
  wrapper on which host performed it. `WriteBehaviourConfig.from_env()` reads
  them as one structured value and `describe()` reports the effective
  configuration. Every variable name, default, and parsing quirk is unchanged
  and the existing per-module readers still work. One visible difference: a
  malformed `TRELLIS_TRACE_EXTRACTION_MIN_CONFIDENCE` now warns under the
  `trellis.core.write_config` logger rather than
  `trellis.extract.trace_ingest_hook`, and once per distinct value per process
  rather than once per read — every flag reader builds the whole config now, so
  an uncached warning would fire per ingested document.
- **`trellis admin write-config`** (`--format text|json`) — reports the build
  and the effective write-behaviour environment of the invoking process, and
  both MCP transports plus the API log the same stamp once at startup.
  `GET /api/version` gained it as an optional `write_provenance` field (API
  minor 1 → additive) so a *running container* can be asked directly. That
  field is ops detail — build sha plus enabled ingest behaviours — so it is
  gated exactly like the `/readyz` backend breakdown: authenticated callers
  always see it, anonymous callers see it unless `TRELLIS_AUTH_MODE=required`
  (and `TRELLIS_OPS_DETAIL=public` opts back in). The compatibility fields stay
  public and unchanged.

- **`trellis classify backfill`** — the missing front door for the tagging
  backfill. `trellis.classify.refresh.reclassify_stale` shipped tested but had
  no caller outside comments, so re-tagging documents ingested before
  `TRELLIS_ENABLE_CLASSIFY_ON_INGEST` was enabled (or whose tags have since
  drifted) required an ad-hoc script. Exposes `--max-age-days`, `--limit`
  (`0` = whole store), `--page-size`, `--dry-run`, and the deliberately
  dangerous `--include-domain` (off by default — `domain` is the only facet
  that hard-excludes a document from a domain-scoped query). `reclassify_stale`
  gained offset paging so one invocation covers a store larger than one page,
  per-document fail-soft (one bad row is counted in `errors` and skipped, not
  fatal to the run), and both refresh entry points gained `dry_run`.
  Documented in [`operations.md`](docs/agent-guide/operations.md).
- **`trellis-session-capture` console script** and **`trellis worker
  capture-sessions`** — the capture sweep advertised the console-script name in
  its own `--help` but never shipped it, and was the only worker with no
  `trellis worker` front door. Both delegate to the same `run_sweep`.
- **Claude Code session auto-capture** (`trellis_workers.session_capture`) —
  client-side nightly sweep that reads local Claude Code transcript JSONL,
  distils durable operator memories with a local model, and writes them
  through the existing `sync_records` seam (no transcript parser in core, per
  ADR #257). F8-safe parser (skips malformed lines; tolerates sidechains,
  tool-result content arrays, compaction summaries, and unknown record types),
  a per-file `(mtime, size)` watermark for incremental sweeps (pre-read stat
  snapshot, so a tail appended mid-sweep re-processes instead of being
  skipped), a deterministic secret-scan gate (key=value / bearer /
  secrets-manager-URI / PEM / connection-string DSN / AWS-access-key-id /
  high-entropy classes) that drops any candidate before write, a
  capture-instruction injection guard ("remember this…" shapes and
  worthiness-rubric stuffing rejected and counted), the lifecycle-plan §2
  worthiness gate (non-derivable / durable / actionable / attributed) with
  failure-bias triggers, fail-closed distillation (model down → capture
  nothing), flag-gated near-duplicate reconcile reusing #263, and a leak-safe
  `MEMORY_OP_JUDGED` (`distillation`) training-pair per written memory.
  Runbook: [`docs/agent-guide/session-auto-capture.md`](docs/agent-guide/session-auto-capture.md).
  ([#255](https://github.com/ronsse/trellis-ai/issues/255))
- **`matplotlib>=3.8`** — dev-only dependency added for the
  ``program_convergence`` master scenario's PNG chart renderer at
  ``eval/reports/program_convergence_chart.py`` (9-axis 3x3 subplot
  grid). Headless `Agg` backend; selected before any pyplot import so
  CI / containers without an X server render correctly. Imported only
  from the eval package; core library and CLI runtime have no
  matplotlib dependency.
- **The stdio MCP server stamps its session's project on every pack and
  every trace it ingests, through `save_experience` or
  `execute_mutation`'s `trace.ingest`.** `PACK_ASSEMBLED` payloads gain a
  `project` key (always present, `null` when unknown), and the trace's
  `metadata["project"]` is set before ingest. The value is
  `TRELLIS_PROJECT` when set, else the git repository containing the
  server's working directory, read from `.git` without a subprocess; a
  linked worktree reports its main repository. Over HTTP only the
  override counts. REST/CLI packs carry `null`, and REST/CLI traces are
  not stamped. An agent's disagreeing value is kept as
  `project_unverified`. New module `trellis.core.project`.
- **The session-capture sweep records which context packs each Claude Code
  session was served.** Nothing joined a pack to the session that used it:
  a pack's `session_id` is a label the agent chooses. Each parsed transcript
  with a turn or a tool call now gets one `capture.session_packs` event
  keyed on its session id, naming the pack ids its Trellis retrieval
  results printed (`get_context`, `search`, `get_objective_context`,
  `get_task_context`, `get_sectioned_context`, `get_items`), with
  `retrieval_results`, `retrieval_errors`, `pack_ids_unparsed` and, for a
  sub-agent transcript, `parent_session_id`. An empty list with
  `retrieval_results` 0 means the session never retrieved; no event means
  it has not been parsed. A re-parse writes only when the join changes.
  `CaptureReport` gains `pack_joins_recorded`, `pack_joins_unchanged` and
  `pack_ids_unparsed`. Only the id is read out of a result, and only once
  it matches the pack-id alphabet.
- **`GraphStore.update_node_if_current` writes a new node version only over
  the version its caller read.** The caller passes the `valid_from` that
  `get_node` returned. While that version is current the call replaces it
  and returns `True`; once another write has replaced it, or `delete_node`
  has purged the node, it writes nothing and returns `False`. It never
  creates a node, so unlike `upsert_node` it cannot bring back an id that a
  purge removed between a caller's read and its write. Of concurrent calls
  holding one token exactly one writes, on SQLite, Postgres, Neo4j and
  ArcadeDB; a concurrent `upsert_node` is not checked against the token, and
  on Neo4j the two can leave two current versions. A token that is not a
  timestamp string raises `TypeError` or `ValueError` before anything is
  read, on every backend. **Out-of-tree `GraphStore`
  backends must implement it**: the method is abstract, so a subclass
  without it no longer instantiates.
- **The `capture.session_packs` join now carries each session's outcome.**
  The payload gains `outcome`, read from the transcript and from no pack,
  so it can measure a pack the agent's own grade cannot: `tool_calls`,
  `tool_errors`, `assistant_turns` (one per API message),
  `assistant_turns_with_usage`, `user_turns` (a person's text, not the
  harness's notices), the four token totals (`null`, not 0, where no
  message recorded them), `wall_clock_seconds`, `commits`, `prs_created`,
  `prs_merged` (successful `git commit` / `gh pr create` / `gh pr merge`
  Bash calls), `pr_urls` (distinct PR URLs those calls printed),
  `ended_on_error` and `ended_interrupted`. Counts, one duration and two
  flags only. A record a resumed session writes into its file again counts
  once; one copied into the new session's file counts in both. A sub-agent
  transcript has its own outcome and a parent's holds none of it. The
  outcome is part of the "write only when the join changes" comparison.
  New module `trellis_workers.session_capture.outcome`.
- **A randomised pack holdout, off by default.** `TRELLIS_PACK_HOLDOUT_RATE`
  (a write-behaviour knob, default `0`) withholds a whole built pack, items
  and advisories, when a SHA-256 draw on its `pack_id` falls below the rate.
  The caller gets an ordinary empty pack with its `pack_id` and nothing that
  names a holdout. Every `PACK_ASSEMBLED` row now carries `holdout` and
  `holdout_rate`, flag off included; a withheld row keeps the would-be pack
  under `holdout_items` (sectioned: `holdout_sections`) and
  `holdout_advisory_ids`, with `injected_items` empty. The aggregate readers
  drop withheld packs and the feedback naming them. While the rate is above
  `0`, an empty flat MCP pack renders with its `pack_id` header. At `0`
  nothing an agent sees changes. New module `trellis.core.pack_holdout`.
- **`trellis analyze holdout` reads the pack holdout experiment.** A
  read-only command over `PACK_ASSEMBLED` and the capture joins and sweeps;
  it writes nothing. One unit per finished sub-agent task whose first pack
  is non-empty, a withheld row's would-be items included, and its arm is
  that first pack's `holdout` (ITT). Rows from builds without `holdout` are
  counted and left out, and several rates in the window are refused until
  `--rate` names one. Usage-limit cut-offs are excluded unless `--itt`; the
  pre-treatment exclusion and the covariate-residualised permutation are
  reported as not applied, because capture records neither where the first
  retrieval fell nor a brief length, and the non-ephemeral rule as applied
  upstream by capture. The statistic is the
  stratum-weighted difference, served minus withheld (weights `n1*n0/n`,
  strata parent session x ISO week), of `log1p(assistant_turns)` or another
  `--outcome`, with a within-stratum permutation p-value, a stratified
  bootstrap CI and achieved against planned power (`--planned-mde`,
  `--planned-n`). A descriptive block, the pre-registration's re-measure, is
  reported whatever the flag state, and without a withheld arm the
  inference reads "no withheld arm". Counts and statistics only. New module
  `trellis.analyze.holdout`.
- **`trellis analyze holdout` reports the guardrails by arm, and divides
  task rates by the span their rows can occupy.** A `guardrails` block gives
  each pre-registered guardrail per arm with its n and no p-value or
  verdict: PRs created and any commit (each left out when it is the
  primary), log1p commits, tool errors and the re-call rate after the first
  retrieval over the primary's analysed tasks, and the cut-off rate over
  every eligible task, cut-offs included, read as the exclusion reads it.
  Any other field the capture join lacks reads "not measurable", and with
  no withheld arm the block shows the served arm only. The task figures per
  30 days and the task MDE horizons divide by the task window
  (`task_window_since`, `task_window_days`), from the window's first row at
  the analysed rate unless that rate was already running as the window
  opened, so a window that opens before the holdout build was deployed does
  not read the rate low and the MDE high. The main-session figures keep the
  whole window and say so. Existing JSON keys and the inferential
  statistics are unchanged.

### Changed

- **BREAKING: `trellis admin quickstart` no longer writes Claude Code settings
  files; it prints the `claude mcp add` command that registers `trellis-mcp`.**
  Claude Code never read the `mcpServers` entry it wrote there, so entries
  written by earlier versions are inert and can be deleted. `--format json`
  drops `settings_path` and adds `mcp_register_command`, the command as a
  list of arguments. `--scope project` now initializes `<cwd>/.trellis/`
  instead of the global config dir, and a `--scope` other than `root` or
  `project` exits `2` before writing anything, where it ran the global setup.
  ([#651](https://github.com/ronsse/trellis-ai/pull/651),
  [#669](https://github.com/ronsse/trellis-ai/pull/669))

- **BREAKING: a `config.yaml` value that is only a `${VAR}` placeholder now
  refuses to load.** Trellis never expands environment variables in
  `config.yaml`, so `password: ${TRELLIS_NEO4J_PASSWORD}` reached the driver
  as those characters and surfaced, at first use, as an authentication
  failure that named neither the key nor the cause.
  `StoreRegistry.from_config_dir` now raises `ConfigError` before any store is
  built, naming every such key (`knowledge.graph.password is the literal text
  ${TRELLIS_NEO4J_PASSWORD}`). The CLI, `trellis-api`, the MCP server and the
  session-capture sweep all build through it, so a config carrying a dormant
  placeholder, in a key today's command would never read, now refuses too;
  the CLI and the standalone `trellis-session-capture` exit `5`. The
  operator forms (`${VAR:-x}`, `${VAR:?x}`, `${VAR:+x}`, ...) count, and the
  message renders only the variable name, never the text after the operator.
  A placeholder inside a longer value (`${DATA}/kuzu`) is not refused and
  still reaches the store literally. To recover, write the value itself, or
  delete the key where Trellis reads an environment variable in its place
  (`TRELLIS_NEO4J_PASSWORD` for a neo4j password; for an `llm` or
  `embeddings` API key, name the variable with `api_key_env`).

- **A flat top-level `stores:` block in `config.yaml` now logs
  `registry_config_flat_stores_removed`.** The block was removed in 0.6.0 and
  nothing reads it, so a store configured only there ran on its default
  backend without a word. It is still ignored; the warning names the file
  and says to move the entries under `knowledge:` / `operational:`.

- **BREAKING: `ingest corpus --prune` and `ingest conversations --prune` now
  exit `5` with `status: "partial"` when they keep a document they could not
  check, instead of deleting it and exiting `0`** (#633). Corpus prune used to
  delete every document whose file the walk did not yield, and the walk
  silently skipped whatever it could not read, so a directory that lost its
  permissions, a path component that became a file, or a symlink loop deleted
  the documents it hid. Each candidate is now checked against the filesystem:
  only a source that is verifiably gone is pruned, and one that cannot be
  checked is listed under the new `prune_withheld` report key (and counted in
  `counts.prune_withheld` and the `CORPUS_SYNCED` payload). Dry runs exit the
  same way. Conversation prune runs only when the reader read the whole
  export. Unreadable directories, the root included, are now reported as
  `unreadable_directory` warnings instead of being skipped silently. **If a
  wrapper treats any non-zero exit as a failed sync, note that a `5` here means
  the rest of the run was written**; fix the path it names and re-run.

- **BREAKING: `sync_records(prune=)` is replaced by `prune_check=`**, a
  callable that says for each prune candidate whether its source is
  `"vanished"`, `"present"`, or `SourceUnverified(detail)` (#633). `None`
  still prunes nothing. A caller passing `prune=` now gets a `TypeError`.
  `sync_corpus` and `sync_conversations` keep their `prune: bool`.

- **BREAKING: a damaged `advisories.json` now exits `5`, not `2`.** Affects
  `trellis analyze generate-advisories`, `trellis analyze
  advisory-effectiveness` and `trellis worker curate`, on both `--format`
  arms, for both refusals the advisory store raises — the file loaded
  degraded, and another process wrote it between this command's load and its
  save. One unusable config file under `stores_dir` was producing two
  different exit codes depending on which file it was: these three exited `2`
  while `trellis policy list` exited `5` meeting the sibling `policies.json`
  damaged the same way (#489). `2` is the one value at which the two rules in
  play conflict — it means "the input failed a check, fix it and retry", and a
  wrapper that retries with corrected arguments against a file no argument can
  fix loops forever, which is the argument `docs/design/adr-cli-exit-codes.md`
  already made canonical when `ConfigError` was mapped to `EXIT_STORE` (#459).
  `EXIT_STORE` satisfies both rules: the advisory surfaces still agree with
  each other, and one root cause now has one code. Note that
  `StoreWriteRefusedError` is a `StoreError`, so `exit_code_for` answered `5`
  all along — these call sites were *overriding* the canonical map, and the
  fix removes the override rather than picking a new number. **If your wrapper
  branches on `2` for these commands, change it to `5`**; one that only tests
  for non-zero (`set -e`, `if ! trellis …`) needs no change. A genuine bad
  argument — `trellis analyze replay --body-items 0` — still exits `2`.

- **BREAKING: `StatsResponse.documents` changed meaning in place** — it was
  the physical document *row* count and is now the *whole-document* count,
  with the old number moved to a new `document_rows` field. Affects
  `GET /api/v1/stats` and `trellis admin stats --format json`. Nothing errors
  on the change, which is what makes it worth calling out: a reader that is
  not updated silently reports a different population. On the reference
  deployment the two numbers are 579 and 1,319 — the same 2.3x disagreement
  `GET /api/v1/documents`' `total` already had with the old stats field, which
  is why both are now named rather than one being picked (#412, #451). A
  consumer that wants the storage number reads `document_rows`; one that wants
  the number reconciling with the documents listing keeps reading `documents`.

- **BREAKING: `trellis policy show --format json` now emits an envelope**
  — `{"status", "policy"[, "store_degradation"]}` — where it emitted a bare
  `Policy` dump. Forced by `extra="forbid"`: a dump plus a `store_degradation`
  key is a payload `Policy.model_validate` *rejects*, so round-tripping callers
  would have broken precisely when the store is degraded. Read the policy from
  `.policy`. This matches the nesting `GET /api/v1/policies/{id}` already used.
  `policy list` and `policy show` also gain the house `status` key
  (`docs/design/adr-cli-exit-codes.md`), and every `trellis policy` command
  exits 5 (`EXIT_STORE`) rather than 0 when the policy file loaded degraded —
  including the read-only ones, because a partial view of an access-control
  file must not be scriptable as the whole ruleset (#413).

- **`get_version()` now returns a real version.** It read `trellis._version`,
  a module no configured build hook ever writes, so it always fell through to
  `0.0.0-dev`. It now resolves from the installed distribution metadata that
  `hatch-vcs` already populates; the `0.0.0-dev` sentinel is kept for the
  genuinely unresolvable case. `trellis admin version` and `GET /api/version`
  report a real version — and a git sha for an editable install, or for an
  image built via `make docker-build`.
- **A capture sweep that leaves sessions unjudged now exits non-zero.**
  Previously a mid-sweep judge outage was a `warnings[]` entry and a clean
  exit `0`. Adopters running the shipped systemd timer will see the unit fail
  on a single transient model timeout, even though the other sessions were
  captured and the unjudged one stays un-watermarked for automatic retry. Set
  `TRELLIS_CAPTURE_STRICT=0` to keep the reported
  `sessions_judge_unavailable` count with the old zero exit. The *total*
  no-op (no judge at all) fails regardless — nothing ran, so nothing is
  retried.
- **A capture session that raises no longer aborts the whole sweep.** An
  exception out of any one session — reading its transcript, building the
  judge prompt, parsing or hashing the judge's reply — used to propagate out
  of `run_capture` before the write seam, the watermark save and
  `CAPTURE_SWEEP_COMPLETED`, so no memory from that night's sweep was written
  and the watermark did not advance; a deterministic fault did the same every
  night. The session is now skipped, left un-watermarked for retry, and
  counted in a new `sessions_errored` field of the report and the sweep event
  (traceback logged as `capture_session_failed`). Under the default strict
  mode the run exits non-zero: `3` from `trellis-session-capture` (`1` stays
  the judge outage and wins when both apply) and `1` from `trellis worker
  capture-sessions`, which reports `"status": "partial"`. With
  `TRELLIS_CAPTURE_STRICT=0` it exits `0` where it used to crash.
  A document store the registry refuses on first use (an unknown backend,
  a missing DSN) now stops the sweep, dry runs included, before any session
  is judged. It used to surface only after the judge had run on every
  session, if at all; with the watermark unsaved, the next sweep paid for
  the same sessions again.
  ([#644](https://github.com/ronsse/trellis-ai/pull/644),
  [#674](https://github.com/ronsse/trellis-ai/pull/674))
- **MinHash shingle hashing switched from MD5 to truncated SHA-256**
  (`classify/dedup/minhash.py`). Non-cryptographic use (similarity
  estimation, not secret protection), but CodeQL's sensitive-data-hashing
  check flags MD5 regardless of `usedforsecurity=False`. Signatures are
  in-memory only (never persisted), so the swap has no compatibility impact;
  similarity behavior is statistically equivalent.
  ([#255](https://github.com/ronsse/trellis-ai/issues/255))
- **Bolt graph reads no longer fetch the node embedding.** On Neo4j and
  ArcadeDB the vector store keeps each node's embedding on its graph row,
  and every graph read that returned whole nodes shipped that vector over
  the wire and decoded it in the driver, although no `GraphStore` method
  returns it. `get_node`, `get_nodes_bulk`, `get_node_history`,
  `get_subgraph`, `query`, `search_nodes`, `execute_node_query` and the
  reads inside `upsert_nodes_bulk` and `bind_alias_if_absent` now leave it
  out. At 10,000 nodes with 1536-float embeddings, one `search_nodes` call
  took 0.8 s on ArcadeDB and 1.0 s on Neo4j instead of 10 s, and one
  `get_subgraph` call returning 1,001 nodes took 0.14 to 0.23 s instead of
  1.1 to 1.2 s. Returned values, stored rows and vector search are
  unchanged.
  ([#716](https://github.com/ronsse/trellis-ai/pull/716))

### Fixed

- **A policy refusal exits `3` on every single-command `trellis curate`
  write, and `curate link` refuses like the rest.** A refused write exited
  `2`, whatever refused it. A policy refusal, by `deny` or `require_approval`,
  now exits `3` ("get approval, don't retry"), and any other refusal, such
  as the unattended-writer roster or a blank `--reason`, still exits `2`.
  `curate link` exited `1`, the code for a bug, on every refusal and
  failure, with `"status": "error"` and no `command_id`. It now exits `3`,
  `2` or `5` and reports `"rejected"` or `"failed"` with a `command_id`, as
  the other writes do, so a missing source or target is a `2`. `prune` with
  no criteria and `restore` with no ids answer `--format json` with
  `{"status": "error", "message": ...}`, a `restore --from-file` path the OS
  cannot read prints a message and exits `2` instead of a traceback, and invalid
  `entity --properties` JSON exits `2`, not `1`. A REJECTED `CommandResult`
  names its audit reason in `metadata["rejection_reason"]`. No REST or MCP
  response carries `metadata`, so neither changes. `curate promote-learning`
  is unchanged: it reports each candidate's outcome and exits `0` even when
  policy refuses every one.
  ([#681](https://github.com/ronsse/trellis-ai/pull/681))

- **Registry errors and warnings stop repeating config.yaml keys and URIs.**
  A key the backend does not accept, a DSN inside YAML flow braces included,
  was quoted by Python's `TypeError` (exit 1); it is now a `ConfigError`
  (exit 5). That refusal, the `registry_config_unknown_store_type` warning
  and the `${VAR}` refusal name a key only when it is shaped like one (a
  lowercase letter, then lowercase letters, digits, `_` or `-`, at most 30
  characters in all), and otherwise give its length, or its type when it is
  not a string. A malformed `dsn` or `uri` is named by its setting, without
  the URI or its scheme. At API startup, a `backend` written as a mapping or
  list is a `ConfigError` instead of a `TypeError` crash. The CLI turns off
  Typer's traceback locals, which Typer before 0.23 printed.
  ([#670](https://github.com/ronsse/trellis-ai/pull/670))

- **`trellis curate entity`, `prune`, `restore`, `redact` and `link` show
  their warnings, as `label` already did.** Their output carried neither
  kind of warning a result holds: an `Enforcement.WARN` policy's verdict,
  or the note that the command's audit event was not recorded
  (`audit_event_not_recorded`). Without `-v`, nothing else on the CLI
  showed the policy's verdict. Each now prints a `Warning:` line per
  warning in text, and its JSON carries a `warnings` array on success,
  refusal and failure, `[]` when nothing warned. No exit code and no
  existing key changes. `link` now escapes the message it prints on a
  refusal or failure, as the other four already did, so an id or a policy
  condition containing `[/x]` prints instead of raising `MarkupError`
  before the warning.
  ([#665](https://github.com/ronsse/trellis-ai/pull/665))

- **`trellis worker curate --dry-run` records no finding and refuses
  `--reconcile-first`.** A dry run recorded `NoiseTagsApplied` and
  `LearningCandidatesReport` meta-trace findings for noise tags and review
  files it never wrote, and `--reconcile-first` emitted a
  `FEEDBACK_RECORDED` event per file-only feedback row under it. A dry run
  now records the meta-Activity of each stage it runs but no finding,
  `--dry-run --reconcile-first` exits 2 (preview the backfill with
  `trellis admin reconcile-feedback --log-dir DIR --dry-run`), and
  `--dry-run --no-meta-trace` writes nothing beyond the `write.rejected`
  event a degraded advisory file still emits. Live runs are unchanged.
  ([#663](https://github.com/ronsse/trellis-ai/pull/663))

- **`trellis extract refresh` prints one error when the stores are not
  initialized.** After the refusal on stderr, it printed a second, empty
  error on stdout: `Refresh failed: ` in text, and
  `{"status": "error", "error_type": "Exit", "message": ""}` under
  `--format json`. Stdout now stays empty, as it does for every command
  that needs the stores. The exit code is still `1`.
  ([#676](https://github.com/ronsse/trellis-ai/pull/676))

- **`admin health` and config parse errors stop printing config.yaml
  values.** `trellis admin health` printed a backend value that names no
  registered backend, a DSN written as a name included; it now reports
  `null` / `unknown backend (not checked)`. The registry's refusal of such
  a backend and its schema fingerprint no longer repeat it either. The
  config parse errors of the registry, `admin migrate-graph` and
  `worker tune` no longer quote the file: they give the parser's reason,
  with the line and column when it has them. A `TrellisConfig` validation
  error or a `worker tune` setting check no longer repeats the value. In
  the registry a top-level list or scalar, invalid UTF-8 or a tag that
  cannot construct is now a `ConfigError` (exit 5), not a traceback, and
  `admin migrate-graph` refuses a top-level list or scalar, invalid UTF-8,
  a directory, a path it lacks permission to reach or a file name too long
  with exit `2`, where it printed a traceback. For a symlink loop or a path
  through a regular file it gives the operating system's reason, where it
  said the file was not found.
  ([#662](https://github.com/ronsse/trellis-ai/pull/662),
  [#673](https://github.com/ronsse/trellis-ai/pull/673),
  [#677](https://github.com/ronsse/trellis-ai/pull/677))

- **The example curation workflow parses.** Two lines of stray markup
  followed the last step of
  `examples/integrations/github-actions/curation.yml`, so the copy that
  `docs/deployment/scheduled-curation.md` tells you to put in
  `.github/workflows/` was an invalid workflow. A test now parses every
  YAML file under `examples/`.
  ([#661](https://github.com/ronsse/trellis-ai/pull/661))

- **`trellis curate entity`, `promote`, `label` and `feedback` exit non-zero
  when a write is refused or fails, in both formats.** They exited `0`,
  and `curate entity` also printed "Entity created: None" with
  `"status": "ok"`, so the Neo4j guides' smoke check passed on a failed write.
  ([#660](https://github.com/ronsse/trellis-ai/pull/660))

- **`trellis worker tune --dry-run` writes nothing.** It persisted every
  proposal as `pending` and advanced the tuner cursor, although its help
  said "without mutating or emitting"; it never emitted events. The tuner
  now runs with `RuleTuner.run(persist=False)` on a dry run. To queue
  proposals for `trellis metrics promote`, run `worker tune` without
  `--dry-run` while auto-promote is off: that pass promotes nothing, and
  its JSON now reports `"dry_run": false` where it reported `true`.
  ([#659](https://github.com/ronsse/trellis-ai/pull/659),
  [#671](https://github.com/ronsse/trellis-ai/pull/671))

- **A store whose optional extra is missing is refused with its install
  command.** Opening a `postgres`, `pgvector`, `s3` or `neo4j` store without
  its dependencies raised `ModuleNotFoundError`, `ImportError`, or a
  `NameError` for the Neo4j driver, so a command that does not catch the
  error printed a traceback and exited `1`. `StoreRegistry` now raises
  `BackendNotInstalledError`, which names the extra to install, and such a
  command exits `5`. A command that catches it keeps its exit code, and the
  API server (`trellis serve`, `trellis-api`), the only opener of an `s3`
  store, still exits `3` at startup.
  ([#679](https://github.com/ronsse/trellis-ai/pull/679))

- **Error lines that print a caught exception keep the brackets in its
  text.** They passed that text through Rich markup, which deleted any
  `[word…]` and raised `MarkupError` on a stray `[/x]`.
  `trellis ingest trace` on an invalid trace now shows pydantic's
  `[type=missing, …]` detail, an install hint in such a line keeps its extra
  (`uv pip install -e ".[cloud]"`, not `-e "."`), and an input that quotes
  `[/x]`, such as a trace intent or a line of `learning_params.yaml`, prints
  its error where it printed a traceback. Thirty error lines across `admin`,
  `analyze`, `classify`, `curate`, `extract`, `ingest` and `worker` escape the
  text, and `tests/unit/test_rich_exception_markup_rule.py` fails the build on
  a new one. A message carried on a result object, such as `ingest trace`'s
  report of a failed write, still prints through markup.
  ([#680](https://github.com/ronsse/trellis-ai/pull/680))

- **An ArcadeDB graph store without the `neo4j` driver is refused before
  the registry calls the server.** Registry preparation created the database
  and migrated its schema over HTTP, and only then found the driver missing;
  with the server down it reported a connection error instead of the extra
  to install. It now checks for the driver first, as the constructor does.
  ([#682](https://github.com/ronsse/trellis-ai/pull/682))

- **A label on a node that does not exist is refused, not reported as
  done.** `label.add` and `label.remove` on a missing `target_id` returned
  success with the message `Node not found: <target_id>`, audited as a
  `mutation.executed` for a label never written. Both now raise a
  `ValidationError` with code `target_not_found`, as `link.create` does
  with `orphan_edge`: `trellis curate label`, MCP `execute_mutation` and
  REST `POST /api/v1/commands/batch` report `rejected`, audited as a
  `mutation.rejected` with that reason; the CLI exits `2`, not `0`, and a
  `stop_on_error` batch stops there. `feedback.record` and
  `precedent.promote` still accept an id that names nothing: a feedback
  target may be a trace or precedent id, so no one store can check it,
  and a promoted precedent lives in its own event.
  ([#683](https://github.com/ronsse/trellis-ai/pull/683))

- **Every `trellis ingest` command exits by the exit-code map.** A path that
  does not exist, a file `ingest trace`, `evidence`, `dbt-manifest` or
  `openlineage` cannot read or parse as JSON, and a trace or evidence file
  that fails its schema exited `1`; they now exit `2`, and `ingest trace`
  on a directory or an unreadable file prints an error, JSON under
  `--format json`, instead of a traceback. An `ingest trace` refusal
  exits `3` for a policy, `2` for any other reason and `5` for a failed
  write, as `trellis curate` does since #681; its payload is unchanged. A
  typed store or configuration error that stops `dbt-manifest` or
  `openlineage` exits by `exit_code_for` (`5` for a damaged `policies.json`),
  not `1`. The trace's intent and a refusal's message print verbatim: an
  intent quoting `[/x]` crashed the text output after the write, and
  bracketed text such as `[x]` was deleted from a refusal. The
  `--file` examples, an option `ingest trace` does not have, now pass the
  path positionally.
  ([#684](https://github.com/ronsse/trellis-ai/pull/684))

- **`trellis ingest dbt-manifest` and `openlineage` exit by the map when
  every write is refused, and an ingest path `stat` cannot read exits `2`.**
  Both batch commands printed "ingested" and exited `0` with no node or
  edge written. When every write is refused or fails they now exit the first
  one's code (`3` for a policy, `2` for another refusal, `5` for a failure)
  with `"status": "error"` and its message beside the counts; a batch that
  wrote anything still exits `0`. A path whose `stat` fails with `EACCES`
  or `ENAMETOOLONG` raised a traceback (exit `1`) in every `trellis ingest`
  command; it now exits `2` with the operating system's reason, and
  `ELOOP` or `ENOTDIR` no longer reads as "not found".
  ([#687](https://github.com/ronsse/trellis-ai/pull/687))

- **MCP `execute_mutation`'s `trace.ingest` targets its trace.** The
  `Command` it built carried no `target_id` or `target_type`, so a policy
  scoped `entity_type: trace` never matched it, and its
  `MUTATION_EXECUTED` / `MUTATION_REJECTED` event named no entity. A trace
  that validates now gets `target_type: "trace"` and its own id, as in
  `save_experience`, `trellis ingest trace` and `POST /api/v1/traces`. A
  trace that does not validate goes on untargeted, and the handler refuses
  it as before. Other operations are unchanged.
  ([#688](https://github.com/ronsse/trellis-ai/pull/688))

- **`POST /api/v1/traces` answers a refused trace with `400`.** A policy
  refusal (`REJECTED`) fell through to the success path: the route ran
  trace extraction (when enabled) on the trace it had not stored and
  answered `200` `{"status": "ok"}` with the unstored `trace_id`. It now
  answers `400` with the refusal's message before extraction, as
  `POST /api/v1/evidence` and the curate routes do. A failed write keeps
  its `409`.
  ([#690](https://github.com/ronsse/trellis-ai/pull/690))

- **A missing redaction or update target is refused, not failed.**
  `redaction.apply` and `entity.update` on an id that names no node raised
  `NotFoundError`, which the executor reports as a store failure: `trellis
  curate redact` exited `5`, and the audit event carried
  `status: "failed"` and no reason. Both now raise a `ValidationError`
  with code `target_not_found`, as `label.add` and `label.remove` do
  (#683): `trellis curate redact`, MCP `execute_mutation` and REST
  `POST /api/v1/commands/batch` report `rejected` with the message
  `Node not found: <id>`, audited with that reason, and the CLI exits `2`.
  Re-redacting an already-redacted id is refused the same way. A redaction
  that loses a concurrent purge between its read and its delete still
  fails.
  ([#691](https://github.com/ronsse/trellis-ai/pull/691))
- **Learning candidates written by the nightly curate reach the Review queue.**
  `trellis worker curate` and `trellis analyze learning-candidates`
  required `--output-dir`, and nothing tied it to the directory the API's
  Review queue reads, so a cron could write every night's candidates where
  the queue never looked: the queue answered `status: "ok"` with an empty
  list, and Submit a `409` that named no path. Both writers now default
  `--output-dir` to the directory the API reads
  (`TRELLIS_LEARNING_ARTIFACTS_DIR` when set, else `<data_dir>/learning`),
  through one resolver, `trellis.learning.resolve_learning_artifacts_dir`;
  an explicit flag still wins. `GET /api/v1/learning/candidates` names
  that directory in a new `artifacts_dir` field and, when there is nothing
  to serve, answers `status: "error"` with a `code` and a `hint` naming
  the missing path. `POST /api/v1/learning/promotions` answers `409` with
  `detail: {code, message, path}`, including for a malformed artifact,
  which it answered with a `500`. A cron that passes `--output-dir` must
  drop the flag, or name the same directory, to feed the queue.
  ([#693](https://github.com/ronsse/trellis-ai/pull/693))

- **On Postgres, graph reads through `get_subgraph` return node names and
  properties.** Since v0.4.0, `PostgresGraphStore.get_subgraph` left
  `document_ids` out of its node `SELECT`, so every later column landed one
  slot off: each node came back with empty `properties` and
  `document_ids`, shifted timestamps, and its traversal depth as
  `valid_to`. Every reader on Postgres lost node names: the UI graph view
  and `GET /api/v1/entities/{id}`, MCP `get_graph`, and graph-axis
  seeding, which rejected every alias-derived seed (it checks the node's
  current name) and served seeded graph items with empty excerpts. Those
  reads now return the whole node, **so packs served on Postgres
  deployments change**: alias seeds confirm, and seeded graph items carry
  their names and properties, which also changes their scores.
  `execute_node_query` read `SELECT *`, which on a `nodes` table created
  before v0.4.0 returns `document_ids` last and shifted the same way. Every
  node read now names its columns from one list, and a row whose width
  disagrees with that list raises instead of shifting. SQLite, Neo4j and
  ArcadeDB were not affected.
  ([#692](https://github.com/ronsse/trellis-ai/pull/692))

- **The UI graph page's type chips count every type, and its legend,
  colours and labels follow the data.** The chips counted only the first
  500 matches in type order, so when the type that sorts first had 500 or
  more nodes, it was the only chip offered. They now read a new
  `GET /api/v1/graph/search/facets`, which counts current nodes per stored
  `node_type` under the list's `q` through a new
  `GraphStore.count_nodes_by_type` on every backend, and the active chip
  follows the filter. The legend lists the types on the canvas
  with counts, and every entry hides its type; it listed ten fixed
  lowercase names, and an entry acted only when the canvas held a type
  matching one of them. A type's colour comes from its stored name: the
  sixteen canonical types have fixed colours and every other type a stable
  hashed one, where any type not spelled as one of those ten names was the
  accent indigo. Search group headers keep the stored case, so `concept`
  and `Concept` read apart, Activity labels are shortened with the full
  text on hover, and edge labels reach 5.25:1 contrast (from 2.41:1). A
  node's detail lists its `document_ids` as links that open each document
  in the Memories view. A node id reaches the search list's and the
  detail panel's click handlers as data, not inline JavaScript, so an id
  containing a quote no longer runs as script. `GET /api/v1/graph/search`
  also failed with a `500` on every SQLite store: a `sqlite3.Connection` is
  callable, so the route took it for the Postgres store. **Out-of-tree
  `GraphStore` backends must implement `count_nodes_by_type`**: the method
  is abstract, so a subclass without it no longer instantiates.
  ([#695](https://github.com/ronsse/trellis-ai/pull/695))

- **A label, entity update or retention write that loses to a redaction
  no longer brings the node back.** A `redaction.apply` that committed
  between `label.add`, `label.remove`, `entity.update`, `retention.prune`
  or `retention.restore` reading a node and writing its next version was
  undone: the node came back with its pre-redaction properties, and an
  `entity.update` that set a name bound its alias again. All five now
  write through `GraphStore.update_node_if_current` against the version
  they read. A node purged in between is refused as a missing one is
  (`rejected`, reason `target_not_found`, no `LABEL_*` or `ENTITY_UPDATED`
  event and no alias); the retention verbs count it as skipped (`skipped`,
  and restore's `skipped_ids`). A version that another write replaced in
  between is re-read and the change applied again, so none of the five
  overwrites a write that landed after its read (writers that still call
  `upsert_node` are not checked). After five such attempts the command
  fails, and a retention run that fails part-way still emits
  `RETENTION_PRUNED` or `RETENTION_RESTORED` for what it wrote first.
  `label.add` and `label.remove` also carry the node's `document_ids`
  forward, where they wrote every new version with none.
  ([#698](https://github.com/ronsse/trellis-ai/pull/698))
- **`delete_node` also removes a version written by a write it waited on,
  on Postgres and Neo4j.** A purge whose delete waited on a concurrent
  write's lock missed the version that write added, so the node, one of its
  edges or one of its aliases kept a current version while `delete_node`
  returned `True`. Each delete now repeats inside the same transaction
  until it removes nothing. ArcadeDB was not affected: the purge's commit
  conflicts with the write, and the driver re-runs the purge. A new edge or
  alias row whose writer touched nothing the purge holds can still commit
  after the purge's last delete, and a writer that still calls
  `upsert_node` after the purge re-creates the node (the handlers that
  write through `update_node_if_current` refuse a purged node).
  ([#699](https://github.com/ronsse/trellis-ai/pull/699))

- **A quote in an id no longer runs as script on the UI page.** Thirteen
  handlers took their id through inline JavaScript: Review's Approve,
  Reject, Confirm approve, Draft ADR, Copy and Download, the Traces,
  Memories, Events and Packs rows, the trace detail's evidence links, the
  Precedents entity link and Inspect pack. `escHtml` escaped `&`, `<` and
  `>` but not quotes, and the browser decodes an attribute before it runs
  an inline handler, so an agent-written trace, document or entity id
  containing `'` ran as script on a click, and one containing `"` added
  its own handler to the element. A template now names the function in
  `data-action` and carries the id in `data-id`, and one listener makes
  the call. `escHtml` now encodes quotes as well (next entry), so a `"`
  cannot end any other attribute that interpolates an id, such as the
  Memories and Events rows' `title`.
  ([#700](https://github.com/ronsse/trellis-ai/pull/700))
- **A `"` in an agent-written value no longer adds attributes on the UI
  page.** `escHtml` encoded `&`, `<` and `>` but not quotes, and 27
  attributes, among them the table cells' `title`, the Review page's
  element ids and the filter options' `value`, took its output between
  double quotes. A `"` in an id, intent, domain, tag or document text
  ended the attribute and the rest of the value became attributes of its
  own, such as an `onmouseover` handler that ran as script on hover.
  `escHtml` now encodes `"` and `'`, and every attribute value read from
  the API goes through it. The graph search results now cut an id before
  escaping it, so the cut no longer splits an entity.
  ([#703](https://github.com/ronsse/trellis-ai/pull/703))

- **A Postgres purge that cannot finish fails the redaction instead of
  escaping as a database error.** When PostgreSQL aborted a `delete_node`
  as a deadlock victim, which happens when it and another purge or a writer
  lock the same rows in opposite orders, psycopg's `DeadlockDetected` was
  not a `TrellisError` and escaped `MutationExecutor`:
  `trellis curate redact` exited `1` with a traceback and no JSON,
  `POST /api/v1/commands/batch` answered `500`, and no `MUTATION_REJECTED`
  event was written. The abort rolls the purge back whole, so a purge
  aborted as a deadlock victim or on a serialization failure now runs
  again, up to three attempts in all; one that then finds the node already
  purged returns `False`, so the redaction fails as the loser of a race
  without a deadlock does. A purge aborted on every attempt, or stopped by
  any other database error, raises `StoreError` naming the node, so the
  redaction is `failed` (exit `5` in both formats) and audited. The other
  Postgres graph writes still raise psycopg's own errors.
  ([#702](https://github.com/ronsse/trellis-ai/pull/702))
- **A withheld pack's would-be pack reaches `admin` callers only.**
  On `GET /api/v1/events` and `GET /api/v1/packs/{pack_id}`, a caller
  without `admin` reads a withheld pack's `PACK_ASSEMBLED` row without
  `holdout_items`, `holdout_sections` and `holdout_advisory_ids`;
  `holdout` and `holdout_rate` stay. Admin keys and the shared secret
  read it whole, as does every caller in auth mode `off` and an
  anonymous one in `optional`. Serve attribution also drops feedback
  naming a withheld pack whose row falls before its window or past its
  scan cap. `TRELLIS_PACK_HOLDOUT_RATE="-0"` records `0.0`, not `-0.0`.
  ([#705](https://github.com/ronsse/trellis-ai/pull/705))

- **The trace detail's evidence entries link to what can show them.** Each
  entry linked to `/entities/` followed by the evidence ref as JSON,
  because an `EvidenceRef` holds only `evidence_id` and `role`, and every
  click answered 404. `GET /api/v1/traces/{trace_id}` now returns
  `evidence_links`, naming for each ref the document that holds its
  evidence record or, failing that, the `evidence:<id>` graph node that
  trace extraction writes. The entry opens that document in Memories or
  that node in Graph. A ref with neither, as when nothing ingested its
  evidence and trace extraction is off (the default), shows its evidence id
  as text. A knowledge store that cannot be read costs the links, not the
  trace: `evidence_links` is then `null`.
  ([#706](https://github.com/ronsse/trellis-ai/pull/706))
- **On Neo4j, the loser of two concurrent purges of one node fails.**
  `GraphStore.delete_node` returned `True` to both purges, because a
  `DETACH DELETE` that waited on the other purge's lock still counted the
  rows it had matched before the wait. Both redactions reported `success`
  and each wrote a `REDACTION_APPLIED` event. The Bolt purge now locks the
  node's rows before it counts, so the purge that waited finds nothing and
  returns `False`, and its redaction is `failed` with no second
  `REDACTION_APPLIED`, as on Postgres and ArcadeDB. A version a writer
  creates after one of the purges has taken its locks can still be counted
  by both.
  ([#709](https://github.com/ronsse/trellis-ai/pull/709))

- **`trellis analyze holdout` prints every figure the pre-registration
  re-measures, and its PR base rate is the served arm's.** The descriptive
  block gives the largest parent session's share of eligible tasks
  (`top_parent_share`), the MDE at 30, 60 and 90 days (`mde_by_horizon`, t
  at `N - 2` degrees of freedom), the N a 10% effect needs
  (`n_for_10pct_effect`) and main sessions with their sub-agent tasks rolled
  up (`sessions`). A figure the rows cannot give reads "not measurable:
  <reason>" in text and is `null` with its reason in JSON. `pr_base_rate`
  becomes `pr_base_rate_served`, over the served arm only.
  `between_parent_share` is bias-adjusted (epsilon-squared, floored at 0),
  so it never reads above raw eta-squared. The unfinished-tasks note names
  the sweep it checks for, and the funnel counts eligible tasks with an
  unparsed pack id. The inferential statistics are unchanged.
  ([#707](https://github.com/ronsse/trellis-ai/pull/707))

- **`GET /api/v1/graph/search` answers on Neo4j and ArcadeDB.** The route
  picked its SQL by probing the store's private `_conn`, so on either Bolt
  store every request answered `500`. It now calls a new
  `GraphStore.search_nodes`, which returns one page of matching current
  nodes and how many match, natively on SQLite, PostgreSQL and the shared
  Bolt store. Its count equals `count_nodes_by_type` for the same `q`, so
  the graph page's type chips sum to the list's total on every backend. On
  Neo4j and ArcadeDB `q` is a plain substring in any case, so `%` and `_`
  are literal there; SQLite and PostgreSQL answer as before. **Out-of-tree
  `GraphStore` backends must implement `search_nodes`**: the method is
  abstract, so a subclass without it no longer instantiates.
  ([#708](https://github.com/ronsse/trellis-ai/pull/708))

- **Trace extraction mints no graph node for an evidence ref with an empty
  `evidence_id`.** Every such ref of every trace became the one node
  `evidence:`, with a `used` edge from each trace, so unrelated traces were
  graph neighbours through it. The ref is now skipped, writing neither node
  nor edge, and the extractor logs `trace_extraction_evidence_id_empty` at
  info. The schema strips whitespace, so a whitespace-only id is skipped
  too. The schema still accepts the ref, so stored traces load unchanged,
  and an `evidence:` node already in a graph stays until it is removed.
  ([#712](https://github.com/ronsse/trellis-ai/pull/712))
- **A Bolt purge that cannot finish fails the redaction instead of
  escaping as a driver error.** On Neo4j and ArcadeDB, an error the neo4j
  driver raised during `delete_node`, such as a lost connection or a
  server `ClientError`, was not a `TrellisError` and escaped
  `MutationExecutor`: `trellis curate redact` exited `1` with a traceback
  and no JSON, and no `MUTATION_REJECTED` event was written. The driver
  still runs the purge again on an error it can retry. When it gives up,
  or the error is one it does not retry, the purge raises `StoreError`
  naming the node and the error's type, without the server's message, so
  the redaction is `failed` (exit `5` in both formats) and audited. A
  connection lost while the commit was outstanding (`IncompleteCommit`)
  is reported as an unknown outcome, because the purge may have
  committed. The other Bolt graph writes still raise the driver's own
  errors, as do the Bolt calls a redaction makes before its purge, such
  as its reads.
  ([#713](https://github.com/ronsse/trellis-ai/pull/713))
- **Trace extraction mints no graph node for an empty `artifact_id`.** Such a
  ref, including a whitespace-only one, gets no node or edge and is logged at
  info as `trace_extraction_artifact_id_empty`, so unrelated traces are no
  longer neighbours through the node `artifact:`. An existing one stays.
  ([#715](https://github.com/ronsse/trellis-ai/pull/715))
- **`GET /api/v1/graph/search` pages no longer skip or repeat rows that
  tie.** The route pages with `LIMIT`/`OFFSET`, and on PostgreSQL rows that
  tie on the sort key had no stable order between queries, so walking every
  page never showed some nodes and showed others twice. `search_nodes` now
  breaks ties by `node_id`, in the direction of the sort, on every backend,
  so while the graph does not change each match appears once and a
  descending walk is the ascending one reversed. A page holding ties can
  differ from the same request before this change, on every backend except
  for a `created_at` sort on Neo4j and ArcadeDB, which already broke ties
  this way.
  ([#714](https://github.com/ronsse/trellis-ai/pull/714))
- **A trace with a blank `trace_id` is refused.** An empty or whitespace-only
  id is rejected as `trace_id_empty` and nothing is stored:
  `POST /api/v1/traces` answers 400, `trellis ingest trace` exits `2`, and
  MCP `save_experience` raises the refusal. Omit the id to have one
  generated. A trace already stored under `""` stays.
  ([#717](https://github.com/ronsse/trellis-ai/pull/717))
- **A typed handler failure is logged without its chained traceback.**
  `handler_typed_error` is logged without a traceback, so the stderr log
  of the CLI, the MCP server and the API no longer prints the driver error
  that a Postgres or Bolt purge chains to its `StoreError`, whose server
  text can include query text and values. The line gains the exception's
  message as `error`, beside its type, the command id and the operation.
  `handler_failed_unexpected` keeps its traceback; the `failed` result and
  the `MUTATION_REJECTED` event are unchanged.
  ([#718](https://github.com/ronsse/trellis-ai/pull/718))
- **On Neo4j and ArcadeDB, a node with two current rows reads as one
  version.** Concurrent writers can leave a node two current rows, and
  `get_node`, `search_nodes` and `count_nodes_by_type` did not agree on
  which one is the node: an ascending and a descending search could show
  different versions, a `node_type` filter could list the node under both
  types, and the graph page's type chips could sum past the list's total.
  All three now show the row with the later `valid_from`, or the greater
  `version_id` between equal stamps, and a search matches only the name of
  the version shown. The race itself is unchanged, as are
  `get_nodes_bulk`, `query` and `get_subgraph`.
  ([#719](https://github.com/ronsse/trellis-ai/pull/719))
- **A refused or failed command no longer uses up its idempotency key.**
  Only a command whose handler succeeded makes its key answer `duplicate`,
  from the executor's in-process cache or from the event log. A command
  that is `rejected` or `failed` leaves its key free, so a corrected retry
  under the same key runs, including one later in the same
  `POST /api/v1/commands/batch` or `POST /api/v1/ingest/bulk` request,
  each of which runs on one executor.
  ([#721](https://github.com/ronsse/trellis-ai/pull/721))
- **A trace submitted under a stored `trace_id` is no longer extracted onto
  the stored trace.** Such a submission stores nothing, since traces are
  immutable, but with `TRELLIS_ENABLE_TRACE_EXTRACTION` on the three ingest
  surfaces still extracted it, attaching its agent and artifacts to the
  stored trace's `trace:<id>` node. They now skip extraction for an id the
  trace store already holds and say so, still succeeding:
  `POST /api/v1/traces` and `trellis ingest trace --format json` answer
  `"already_ingested": true`, and the CLI text and MCP `save_experience`
  reply `Trace already ingested: <id>`. Re-extract a stored trace with
  `trellis extract traces`. Nodes and edges already attached stay.
  ([#720](https://github.com/ronsse/trellis-ai/pull/720))
- **A Neo4j or ArcadeDB store that is down fails a redaction without the
  server's text.** Opening a Neo4j or ArcadeDB graph store, or a Neo4j
  vector store, runs schema statements, and a driver error there, such as
  an unreachable server or refused credentials, escaped
  `MutationExecutor`: `trellis curate redact` exited `1` with a traceback
  and no JSON, and no `MUTATION_REJECTED` event was written. ArcadeDB's
  HTTP calls (database creation, the edge-schema migration and every
  ArcadeDB vector statement) raised urllib's error, or a `RuntimeError`
  holding the server's reply and the command or URL, and that text reached
  the failed redaction's result and its `MUTATION_REJECTED` event; a reply
  that was not HTTP escaped. Each now raises `StoreError` naming the
  operation and the HTTP status or the error's type, chained to the error
  where there is one, so the redaction is `failed` (exit `5` in both
  formats) and audited without the text. Other commands that open such a
  store, such as `trellis admin graph-health`, exit `5` with the error
  envelope instead of a traceback, and `StoreRegistry.validate` and API
  startup report the failure the same way; the
  `TRELLIS_VALIDATE_CONNECTIVITY` check still prints the driver's text.

## [0.9.0] - 2026-05-13

The second wave of the **self-improvement program** scoped in [`docs/design/plan-self-improvement-program.md`](docs/design/plan-self-improvement-program.md). 27 PRs landed across Items 1, 2, 6, 7 Cohort 1, all 8 phases of the C2 silent-fallback cleanup, and 7 follow-ups. Item 7 Cohort 2 (sandboxed Claude Code spawn) remains deferred per the plan.

### Added — Self-improvement program

**Item 1 — Observation / Measurement entity vocabulary**

- **Phase 0:** Pydantic schemas `Observation` and `Measurement` + well-known registration (`HAS_OBSERVATION` edge kind, schema.org alignment URIs); well-known schema version `1.1.0` constant introduced. ([#121](https://github.com/ronsse/trellis-ai/pull/121))
- **Phase 1:** Mutation handlers (`ObservationRecordHandler`, `MeasurementRecordHandler`), sync + async SDK methods, REST endpoints (`POST/GET /v1/observations`, same for measurements), MCP tools (Observation only — Measurement deferred from MCP). New event types `OBSERVATION_RECORDED` / `MEASUREMENT_RECORDED`. ([#124](https://github.com/ronsse/trellis-ai/pull/124))
- **Phase 2:** `ObservationSearch` retrieval strategy (opt-in, confidence threshold + freshness decay per [`adr-importance-score-freshness.md`](docs/design/adr-importance-score-freshness.md)), `QueryPatternObserver` deterministic-tier extractor producing paired `Observation` + `Measurement` drafts from query logs, cross-backend eval scenario `eval/scenarios/observation_retrieval.py`. ([#125](https://github.com/ronsse/trellis-ai/pull/125))

**Item 2 — Provenance columns**

- **Schema half:** five provenance columns (`source_trace_id`, `agent_id`, `confidence`, `evidence_ref`, `extractor_tier`) added to the `edges` table on all four backends (SQLite, Postgres, Neo4j, ArcadeDB) via the shared `bolt_opencypher` base. Shared validator in `src/trellis/stores/base/edge_provenance.py`. ArcadeDB hardening: typed `STRING`/`FLOAT` properties with `MIN 0.0, MAX 1.0` constraint on `confidence`, idempotent `CREATE PROPERTY IF NOT EXISTS` over HTTP. ([#126](https://github.com/ronsse/trellis-ai/pull/126))
- **DSL + CLI:** edge query DSL extended with `lt` / `lte` / `gt` / `gte` operators on the five provenance columns; new `EdgeQuery` dataclass mirrors `NodeQuery`. `trellis admin migrate-provenance` lifts legacy `properties`-JSON provenance into the typed columns (idempotent, batched, fail-loud above 1% malformed). 47 new tests (8 in the cross-backend contract suite × 4 backends). ([#127](https://github.com/ronsse/trellis-ai/pull/127))

**Item 6 — Dogfooding meta-traces**

- **Phase 0 primitive:** `record_meta_analysis()` context manager records `Activity` nodes with PROV-O edges (`wasAssociatedWith` / `wasInformedBy` / `wasGeneratedBy`). Uses Item 2's provenance columns on every edge. 5-minute merge window, deterministic reservoir sampling (first 10 + last 10 + reservoir 30), synthetic `Agent` factory under the reserved `trellis_meta_` prefix. `TRELLIS_META_TRACES=on|off` env var (default `on`; invalid raises). New package: `src/trellis/meta/`. ([#132](https://github.com/ronsse/trellis-ai/pull/132))
- **Phases 1+2:** 12 `trellis analyze` subcommands + 3 analytical `admin` subcommands wrap their handlers in `record_meta_analysis()`. New `--no-meta-trace` flag disables per-invocation. `PackBuilder.assemble(..., include_meta=False)` is the new default — meta-Activities are filtered out; `meta_filtered_count` added to the `PACK_ASSEMBLED` event payload. Cross-backend eval scenario `eval/scenarios/meta_trace_round_trip.py`. ([#133](https://github.com/ronsse/trellis-ai/pull/133))

**Item 7 Cohort 1 — Coding-agent self-improvement loop (proposal generation only)**

- **Phase 0 generator:** `ProposalGenerator` clusters `EXTRACTION_FAILED` events by `(source_file, failure_class, time_window)` and folds `WELL_KNOWN_CANDIDATE` events in as single-event clusters; emits `PROPOSAL_DRAFTED` / `PROPOSAL_UPDATED` events. Stable `proposal_id` (SHA-256 of cluster signature) makes re-runs over the same window idempotent — no duplicate drafts. Each run wraps in `record_meta_analysis()`. New package: `src/trellis_workers/code_authoring/`. 37 new tests. ([#134](https://github.com/ronsse/trellis-ai/pull/134))
- **Phase 1 CLI + eval:** `trellis admin generate-proposals` / `list-proposals` / `show-proposal` with `--format text|json` per the Trellis machine-output rule. First CLI surface to actually adopt the `EXIT_*` constants from #123. End-to-end eval scenario `eval/scenarios/proposal_generation.py`. ([#135](https://github.com/ronsse/trellis-ai/pull/135))

**Cohort 2 deferred.** Sandboxed Claude Code spawn + GitHub PR proposer + budget ledger + file allowlist + secret scrubbing remain deferred per the plan — needs operator review of real Cohort 1 proposals first, then a separate ADR amendment authorizing autonomous spawn.

### Added — Tooling

- **`trellis.meta` package** (`src/trellis/meta/`) — public API: `record_meta_analysis`, `MetaAnalysisRecord`, `ensure_meta_agent`, `reservoir_sample`. ([#132](https://github.com/ronsse/trellis-ai/pull/132))
- **`trellis_workers.code_authoring` package** — public API: `ProposalGenerator`, `Cluster`, `Proposal`, `cluster_failures`. ([#134](https://github.com/ronsse/trellis-ai/pull/134))
- **`trellis.stores.base.edge_provenance`** — shared validator + `EDGE_PROVENANCE_FIELDS` tuple consumed by all four graph backends. ([#126](https://github.com/ronsse/trellis-ai/pull/126))

### Changed (breaking — POC stage)

- **`Observation.confidence` is now `float | None`** (was required `float`) per [`adr-observation-entity-type.md`](docs/design/adr-observation-entity-type.md). ([#136](https://github.com/ronsse/trellis-ai/pull/136))
- **`Measurement` edges now use `hasMeasurement`** — a new canonical edge kind. Previously reused `hasObservation`. Well-known schema version bumped `1.1.0` → `1.2.0`. ([#136](https://github.com/ronsse/trellis-ai/pull/136))
- **`PackBuilder.assemble()` / `build_sectioned()` default to `include_meta=False`** — meta-Activities are filtered out of agent-facing packs unless callers explicitly opt in. ([#133](https://github.com/ronsse/trellis-ai/pull/133))
- **CLI exit codes follow [`adr-cli-exit-codes.md`](docs/design/adr-cli-exit-codes.md).** `2` now means *validation error*, never *critical operational finding* — those moved to `1` (`EXIT_INTERNAL`). Migration step errors are `5` (`EXIT_STORE`). Operators with CI gates around specific codes should re-read the ADR. ([#123](https://github.com/ronsse/trellis-ai/pull/123), [#139](https://github.com/ronsse/trellis-ai/pull/139))

### Cleanup — C2 silent-fallback program (complete)

The cleanup track defined in [`docs/design/plan-cleanup-silent-fallbacks.md`](docs/design/plan-cleanup-silent-fallbacks.md) finished all eight scheduled phases.

- **Phase 1.5** — retention malformed-date surfacing with `RetentionDriftError` + 1% threshold ([#116](https://github.com/ronsse/trellis-ai/pull/116))
- **Phase 2** — `StoreRegistry` `BackendNotInstalledError` with `trellis[<extra>]` install hints ([#118](https://github.com/ronsse/trellis-ai/pull/118))
- **Phase 3** — MCP structured error protocol via the `_raise_*` helper family (19 of 31 flagged sites converted; 12 annotated as legitimate GRACEFUL / GUARD) ([#119](https://github.com/ronsse/trellis-ai/pull/119))
- **Phase 4** — `MigrationStepError` + `PackAssemblyError` + `strategy_failures` field on `PACK_ASSEMBLED` event ([#120](https://github.com/ronsse/trellis-ai/pull/120))
- **Phase 5** — telemetry per-site review across `trellis_api/observability.py`, `feedback/recording.py`, `classify/refresh.py`, `extract/dispatcher.py` (12 GRACEFUL annotated, 1 DEFECT fixed) ([#122](https://github.com/ronsse/trellis-ai/pull/122))
- **Phase 6** — CLI `typer.Exit` exit codes + ADR + `MutationExecutor` narrowed typed catches + SDK HTTP exception hierarchy (`TrellisHttpError`, `TrellisClientError`, `TrellisServerError`, `TrellisRateLimitError`, `TrellisTransportError`) ([#123](https://github.com/ronsse/trellis-ai/pull/123))
- **Audit script helper-awareness** — `_raise_*` / `NoReturn` / `sys.exit` / `typer.Exit` / `click.Abort` now recognized as "raises"; `--literal-only` retained for back-compat with the historical baseline ([#128](https://github.com/ronsse/trellis-ai/pull/128))
- **Phase 7** — verification PR captures the post-cleanup audit state ([#129](https://github.com/ronsse/trellis-ai/pull/129))
- **Phase 8** — final closeout: every DEFECT site in `src/` now carries a canonical inline justification or has been fixed. **34 unjustified survivors → 0** (target was ≤ 10). 1 FIX (`budget_config.from_dict`), 41 GRACEFUL annotations (28 new + 13 promotions from informal to canonical form), 6 GUARD, 7 AGGREGATE. ([#140](https://github.com/ronsse/trellis-ai/pull/140))

Net DEFECT delta in `src/`: 113 literal-only → 85 literal-only / 67 helper-aware, of which **0 are unjustified**.

### Fixed

- **Section routing was an unreported eleventh gate, so a sectioned pack could
  serve zero items and state that nothing was withheld.**
  `PackBuilder.build_sectioned` filtered the shared candidate pool through
  `TierMapper.matches_section` and discarded the losing side without a
  `RejectedItem`, so `withholding` reported `total: 0` on a pack whose every
  candidate had been routed away — an affirmative wrong signal, and a stronger
  one than the silence #404 replaced. It is recorded now, as
  `withholding.section_filtered` on the `PACK_ASSEMBLED` payload of every
  sectioned build. It is deliberately **not** a `by_reason` group and does not
  enter `total`: replaying the two shipped section presets over the reference
  deployment's 47 flat packs, the routing removes at least one served item on
  46 and 47 of them (median 10 and 16), so an unconditional line would print on
  essentially every sectioned pack and become the noise `format_withholding_note`
  exists to avoid. The caller-facing sentence therefore renders **only when the
  pack served nothing** — the case the ambiguity actually bites, and the case
  where the count is small and load-bearing (one of the four sectioned packs
  this deployment has ever assembled served zero items). The replay is a
  *sound* basis for the first of those numbers and not for the second: it
  reads `injected_items[]`, which sees only what a flat budget already kept
  (under-counts routing) and carries neither `retrieval_affinity` nor `scope`
  though `matches_section` reads the first before any heuristic (over-counts
  it). The per-pack medians rise to 28 and 53 once the real tags and the
  budget-cut candidates are restored, so the `by_reason` conclusion holds a
  fortiori; the simulated *empty-pack* rate does not survive the same
  correction (10 of 47 → 7 → 0) and is an upper bound only, which is why
  `section_filtered` is emitted on every build rather than left to
  simulation. Cross-section survivors are subtracted by
  the existing `{rejected} − {served}` definition; the routed set is computed as
  "matched no section at all" rather than per-section, so an item a section
  matched and then cut on `max_items` keeps that attribution instead of losing
  it to whichever row landed first. (#440)
- **`build_sectioned`'s rejection telemetry was unpinned: every `_reject` call
  in it could be deleted with the full suite green.** The *filtering* half of
  each gate was covered; the *emission* half was not, on the sectioned path
  only — four of five calls survived deletion against 972 retrieval tests, and
  a missing one silently under-reports what a sectioned pack withheld. Missing
  tests added for the structural, meta-Activity, session-dedup and
  per-section token-budget gates, each verified to fail against the un-emitted
  source. Two fixtures that hid it are fixed: the sectioned meta-Activity count
  test used one dropped and one kept item, so `len(dropped) == len(deduped)`
  and the assertion could not separate the two quantities, and no test asserted
  a rejection row's `item_type` or `relevance_score` against a pool carrying
  more than one of either — both mutants (a hard-coded `item_type`, a
  hard-coded score) survived. That second fix is scoped to the shared
  `_reject` helper; the six sites that build a `RejectedItem` by hand still
  take a hard-coded `item_type` or score against the green 992-test retrieval
  suite (11 of 12 such mutants survive it; the full suite was not run against
  them). Latent in the narrow sense that nothing *branches* on either field —
  `summarize_withheld` reads only `item_id` and `reason`, and both are pinned.
  **The wider reading of that sentence — that nothing reads the two fields at
  all — is wrong, and #456 below corrects it**: both are returned by
  `POST /packs` as `retrieval_report.rejected_items[]` and rendered by the
  Memory Explorer. (#447)
- **The same uniformity flaw at the six sites `_reject` did not reach.**
  `max_items` and `token_budget` on the flat path, both `dedup` branches,
  `semantic_dedup`, and the content floor in `excerpts.py` each hand-wrote the
  identical four-field copy off a `PackItem`, and **eleven of the twelve `item_type` / `relevance_score` mutants across them survived the full default selection (6,468 passing on `a40b027`)** — the identical count #456 measured over the 992-test retrieval subset, so widening the selection caught nothing extra; the one that dies is `dedup`'s `existing.relevance_score`, held by a single pre-existing assertion in `test_pack_builder.py::TestRejectionTracking::test_dedup_rejection_tracked`. So all six
  could have been constant-folded, mistyped, or copied off the wrong object
  and stayed green — the #447 flaw again, six times over. **#456 called it
  purely latent on the grounds that nothing reads either field, and that is
  not quite right**: `summarize_withheld` does key only on `item_id` and
  `reason`, but both fields are serialised into
  `PACK_ASSEMBLED.payload["rejected_items"]`, returned to every REST/SDK caller
  by `POST /packs` (`trellis_api/routes/retrieve.py` hands back
  `pack.retrieval_report.model_dump()` whole), and rendered by the Memory
  Explorer's pack view as the *Type* and *Relevance* columns of its "Rejected
  items" table. The REST surface is the load-bearing half of that — it is a
  programmatic contract, not a screen someone has to open. Nothing *branches* on
  them — which is why every mutant stayed green — but a wrong value was being
  handed to a caller and shown to an operator as fact, not left unread. The six
  are now one constructor, `RejectedItem.from_pack_item`. It sits on the
  **schema** rather than on `PackBuilder` because the content floor is a gate
  too and `trellis.retrieve.excerpts` is imported *by* `pack_builder` — a
  builder-side helper is unreachable from there without an import cycle;
  `PackBuilder._reject` stays as its plural form, for the gates that reject a
  whole slice at once. Consolidation alone would not have made the fields
  observable, only reduced the number of places they can go wrong, so the
  tests are written **per gate** and assert through a real `build` against
  pools carrying two distinct `item_type`s and two distinct scores: reapplied
  to the pre-consolidation source, all twelve mutants now die — each to the
  test for its own gate — as do all four mutants on the single constructor
  (both copied fields, plus dropping the `strategy_source` override and
  dropping its fall-back). Behaviour is unchanged, and the equivalence proof is
  the new tests **reapplied to the old six-copy source**: every test that can
  run there passes (6,474; the only three failures are the direct unit tests of
  `from_pack_item` itself, which cannot exist against a source that does not
  define it), *and* all twelve mutants die against that same old source with
  those same tests. The second half is what makes the first half mean anything
  — a suite green on both sources proves equivalence only if it can tell the
  two apart, and this one demonstrably can. (#456)

- **Tag refresh rewrote every stale document, even when nothing changed.** The
  tags-unchanged early-out in `classify/refresh.py` dropped only
  `importance_scored_at` from its before/after comparison, but
  `to_content_tags()` mints a fresh `classified_at` on every call — so the two
  dicts always differed and the branch could never fire on the batch path.
  Consequences: `trellis classify backfill --dry-run` reported what was
  *stale* rather than what would *change*, and a live run rewrote every stale
  row and emitted an empty-diff `TAGS_REFRESHED` for each, defeating the audit
  trail. Both stamps are now excluded, and unchanged items are counted under
  the new `skipped_unchanged`.
- **A scalar `content_tags.domain` was shredded into one domain per
  character.** On the safe `include_domain=False` path the prior domain was
  carried forward with `list(...)`, so a legacy `"payments"` became
  `['p','a','y',...]` — which `ContentTags` validates happily and no domain
  filter ever matches, silently hiding the document from every domain-scoped
  query. The flat scalar shape is legal elsewhere in the repo
  (`analyze.domains`, `retrieve.evaluate` both handle it). Now normalised;
  a `content_tags` value that is not a mapping at all is treated as untagged
  (warned, re-classified) instead of raising.
- **Session capture no longer no-ops silently on a misconfigured judge.**
  `_build_llm_client` swallowed every failure and returned `None`, and
  `distill_session` fail-closes on `None` — so a broken `llm:` block produced a
  sweep that judged nothing, wrote nothing, advanced no watermark, and exited
  `0`. It now raises `CaptureJudgeUnavailableError`; every front door exits
  non-zero with the remediation, and a judge that disappears *mid*-sweep is
  reported as `sessions_judge_unavailable` (also a non-zero exit) rather than a
  warning in the log. The entry point also pins structlog to stderr so the JSON
  report on stdout stays parseable.
- **SQLite concurrent ingest race.** Partial fix in [#117](https://github.com/ronsse/trellis-ai/pull/117) (`busy_timeout=10s`) closed the named "database is locked" symptom; the deeper Python-level `sqlite3.ProgrammingError: cannot commit - no transaction is active` race remained. Full fix in [#131](https://github.com/ronsse/trellis-ai/pull/131): WAL mode + thread-local `Connection` pool via the existing `_conn` property forwarder; `_ensure_wal_mode` retry helper covers the Windows WAL-transition race that `busy_timeout` alone misses. `test_concurrent_ingests` goes from ~12% flaky to **100/100 deterministic** under tight repeat-loops.
- **ArcadeDB registry-path schema-migration bypass.** The new typed-property + `FLOAT (MIN 0.0, MAX 1.0)` constraint installed only on the direct-construct test path; in production deployments using `StoreRegistry.from_config()` the server-side constraint never landed. Fixed in two passes: [#126](https://github.com/ronsse/trellis-ai/pull/126)'s reviewer caught the new-driver bypass (registry now runs the migration itself before injecting the driver); [#137](https://github.com/ronsse/trellis-ai/pull/137) closed the cached-driver short-circuit AND stopped stripping `http_url` from forwarded params (kept the `password` strip — preserves the constructor's driver-XOR-password mutex).
- **Phase 5 test-ordering flake.** Seven tests in `test_dispatcher_phase5.py` / `test_recording_phase5.py` passed in isolation but failed in full-suite ordering. Root cause: `structlog.cache_logger_on_first_use=True` interacting with the CLI conftest's `TRELLIS_LOG_LEVEL=CRITICAL` monkeypatch — module-level loggers cached a CRITICAL-only bind on `BoundLoggerLazyProxy` that `structlog.configure()` and `reset_defaults()` do not evict. Fix: package-scoped autouse finalizer in `tests/unit/cli/conftest.py` walks `gc.get_objects()` for live proxies and evicts cached attrs at package teardown. 3/3 consecutive full-suite runs green. ([#138](https://github.com/ronsse/trellis-ai/pull/138))

### Refactored

- **`src/trellis/learning/observations.py` → `src/trellis/learning/pack_observations.py`.** Disambiguates the EventLog-bridge module (plural, dict-keyed on `pack_id`) from the new singular `Observation` entity type. One source import + three doc references updated; `git mv` preserves history. No back-compat shim per POC directive. ([#130](https://github.com/ronsse/trellis-ai/pull/130))
- **`EXIT_*` constants adopted across 9 CLI modules** (47 sites converted). `admin.py`'s `code=2 "critical"` semantic conflict with the ADR's `2 = validation error` resolved per site: `graph-health` / `check-extractors` → `EXIT_INTERNAL`; migration step errors → `EXIT_STORE`; capacity-exceeded → `EXIT_INTERNAL`; config-file / YAML / graph-block input errors → `EXIT_VALIDATION`. Module-local `_EXIT_*` literals in `admin_migrate_provenance.py` replaced with imports. ([#139](https://github.com/ronsse/trellis-ai/pull/139))
- **`PackBuilder._raise_if_blocking_strategy_failures` helper** extracted to dedup the required-strategy / all-strategies-failed raise logic between `build()` and `build_sectioned()` so the two surfaces stay in lockstep. ([#136](https://github.com/ronsse/trellis-ai/pull/136))

### Documentation

- **`docs/design/plan-cleanup-silent-fallbacks.md`** §5 updated with all phase PR cross-references and helper-aware DEFECT counts.
- **`docs/design/adr-cli-exit-codes.md`** — new ADR (status: accepted), introduced in [#123](https://github.com/ronsse/trellis-ai/pull/123).
- **`audit/silent_fallbacks_2026-05-12-baseline.md`** + **`audit/silent_fallbacks_2026-05-12-final.md`** + **`audit/silent_fallbacks_2026-05-13-phase8-final.md`** capture the rolling audit history (baseline → post-cleanup → post-Phase-8).
- **`TODO.md`** — Self-improvement Cohort 1 items marked complete; new "Deferred / gated to next cycle" subsection captures Item 7 Cohort 2 + conditional Phase 8.1. ([#141](https://github.com/ronsse/trellis-ai/pull/141))

### Notes for adopters

- The `EXIT_*` constants are now the canonical exit-code surface for the CLI. Operators wiring CI gates around exit codes should read [`docs/design/adr-cli-exit-codes.md`](docs/design/adr-cli-exit-codes.md).
- `Observation.confidence` is now optional; if you were relying on the field being present, set a default at the consumer.
- `Measurement` edges now use `hasMeasurement`; downstream graph queries that filtered on `hasObservation` for measurement nodes must update.
- Meta-Activities filtered from packs by default. To surface them (e.g., when debugging the meta-analysis loop), pass `include_meta=True` to `PackBuilder.assemble()`.
- Item 7 Cohort 2 (autonomous Claude Code spawn) is deferred. Proposal generation in this release is markdown-only; nothing in this release writes code on the user's behalf.

## [0.8.0] - 2026-05-12

First wave of the **self-improvement program** scoped in [`docs/design/plan-self-improvement-program.md`](docs/design/plan-self-improvement-program.md). Five PRs landed in one batch.

### Added

- **`EXTRACTION_FAILED` event type + `emit_extraction_failure()` helper** ([`src/trellis/extract/telemetry.py`](src/trellis/extract/telemetry.py)) — sampling cap (10 per `(extractor_id, prompt_hash, failure_kind)` cluster per 10-minute window, env-tunable), PII redaction (email / UUID / SSN-shape) bounded at 200 chars. Replaces silent JSON-parse swallows in `LLMExtractor.extract()` and `trellis_workers.learning.miner._parse_candidates` with emit-then-raise. `ExtractionDispatcher` is the *one* legitimate degrader — catches the new raises, emits a `tier_fallback` event with the original failure_kind on `error_class`, continues. ADR: [`docs/design/adr-extraction-failure-telemetry.md`](docs/design/adr-extraction-failure-telemetry.md). Item 4 of the self-improvement program. ([#110](https://github.com/ronsse/trellis-ai/pull/110))
- **Well-known promotion loop** ([`src/trellis/learning/schema_evolution.py`](src/trellis/learning/schema_evolution.py)) — `WELL_KNOWN_CANDIDATE` event type + `analyze_well_known_candidates()` analyzer. Surfaces open-string `node_type` / `edge_kind` values that meet promotion thresholds (count, distinct extractors, distinct domains, signal quality, time window). **Surface-only**: never auto-mutates `well_known.py`. Promotion is human-gated via ADR amendment. Includes `trellis analyze schema-evolution` + `trellis admin draft-promotion-adr <candidate_id>` CLI subcommands. Cooldown + recurrence handling deduplicates re-emission on growth / threshold-cross. Filters out `extractor_id startswith "trellis_meta_"` so future dogfooding writes won't feed back into promotion counts. ADR: [`docs/design/adr-well-known-promotion-loop.md`](docs/design/adr-well-known-promotion-loop.md). Item 5. ([#111](https://github.com/ronsse/trellis-ai/pull/111))
- **Self-improvement program docs** — umbrella plan + 5 ADR/plan pairs (Items 1, 4, 5, 6, 7) + plan-only entries for Items 2 + 3 + 2 cleanup tracks + 9-axis program-level eval spec + follow-on `adr-graph-shape-constraints.md` (lightweight SHACL-inspired declarative validation, scoped after this program). All in [`docs/design/`](docs/design/). ([#108](https://github.com/ronsse/trellis-ai/pull/108))
- **Silent-fallback audit script + 2026-05 baseline report** ([`scripts/audit_silent_fallbacks.py`](scripts/audit_silent_fallbacks.py), [`audit/silent_fallbacks_2026-05.md`](audit/silent_fallbacks_2026-05.md)) — AST-based deterministic scanner, classifies each `except` clause into DEFECT / GRACEFUL-DEGRADATION / GUARD / TEST-ONLY. 153 sites flagged, 112 DEFECT (73%). Surfaced an invisible retention-drift bug at `retention.py:169` (silently masks `datetime.fromisoformat` errors) tracked as a standalone P0 fix. Pre-audit speculation about embedder / policy-gate concentration turned out wrong; actual top files are `mcp/server.py` (31 DEFECT), `stores/registry.py` (16 DEFECT), `migrate/graph_migrator.py` (9), `retrieve/pack_builder.py` (9). C2 Phase 0. ([#112](https://github.com/ronsse/trellis-ai/pull/112))
- **`trellis admin init-learning-params` subcommand** ([`src/trellis_cli/admin.py`](src/trellis_cli/admin.py)) — seeds `~/.config/trellis/learning_params.yaml` with the recommended noise / promote thresholds so `trellis analyze learning-candidates` stops WARNing about defaulted values.

### Changed (breaking — POC stage)

- **`analyze_learning_observations()` now requires a `registry: ParameterRegistry` kwarg.** Calling without it raises `TypeError`. A registry that lacks the four required keys (`noise_success_threshold`, `noise_retry_threshold`, `promote_success_threshold`, `promote_retry_threshold`) raises `KeyError` naming the missing key + remediation command. Removes the silent-fallback path to hard-coded module constants. Item 3. ([#109](https://github.com/ronsse/trellis-ai/pull/109))
- **`LLMExtractor.extract()` and `PrecedentMiner._parse_candidates()` now raise `ExtractionFailureError`** on parse / validation failure instead of returning empty results. The dispatcher catches and degrades explicitly via a `tier_fallback` event; direct callers must do the same if they want graceful degradation. ([#110](https://github.com/ronsse/trellis-ai/pull/110))
- **`LEARNING_*_KEY` + `LEARNING_SCORING_COMPONENT` + `REQUIRED_LEARNING_PARAMETER_KEYS` exported from `trellis.learning`** as the single source of truth for the registry-key strings (previously duplicated across `scoring.py`, `analyze.py`, and test fixtures). ([#109](https://github.com/ronsse/trellis-ai/pull/109))

### Removed

- **`_NOISE_SUCCESS_THRESHOLD`, `_NOISE_RETRY_THRESHOLD`, `_PROMOTE_SUCCESS_THRESHOLD`, `_PROMOTE_RETRY_THRESHOLD`** hard-coded module constants from [`src/trellis/learning/scoring.py`](src/trellis/learning/scoring.py). Values now live in the operator-facing `LEARNING_PARAMETER_SEED_DEFAULTS` in `trellis_cli/analyze.py` (CLI seed) and in the ParameterRegistry the library requires. ([#109](https://github.com/ronsse/trellis-ai/pull/109))

### Cleanup

- Per-file simplify pass over each of the four code PRs in this wave: −104/+46 (#109), −42/+19 (#110), −18 net (#111), −125 (#112). Dead-code removals plus a POC-directive violation caught **inside the audit script itself** (a bogus `except Exception` around `ast.unparse`).

### Notes for adopters

POC directives now apply across this surface: no silent fallbacks, no backwards-compat shims, loud on misuse, no half-finished implementations. See [`docs/design/plan-self-improvement-program.md`](docs/design/plan-self-improvement-program.md) §2 for the full spec; the four cleanup tracks ([`plan-cleanup-dead-code.md`](docs/design/plan-cleanup-dead-code.md), [`plan-cleanup-silent-fallbacks.md`](docs/design/plan-cleanup-silent-fallbacks.md)) sequence the broader sweep.

## [0.7.0] - 2026-05-11

**ArcadeDB becomes the blessed graph + vector substrate** for self-hosted AWS deployments (Apache 2.0, Bolt + openCypher 25 at 97.8% TCK, native HNSW via jVector).

### Added

- **ArcadeDB graph backend** ([`src/trellis/stores/arcadedb/`](src/trellis/stores/arcadedb/)) — thin adapter over a shared [`BoltOpenCypherGraphStore`](src/trellis/stores/bolt_opencypher/graph.py) base class. Neo4j now subclasses the same base; ~1000 LOC of Cypher payload + SCD-2 logic shared between the two backends. (commits `ae410aa`, `5d85a27`)
- **ArcadeDB vector backend** — SQL-over-HTTP path with `LSM_VECTOR` index + `vectorNeighbors` function. Graph and vector see the same `(:Node)` rows but use different protocols. (commit `08714f3`)
- **ADR: [`adr-arcadedb-blessed-substrate.md`](docs/design/adr-arcadedb-blessed-substrate.md)** documenting the substrate decision (replaces LanceDB; preserves Neo4j as a supported alternative).
- **[`docs/deployment/recommended-config.yaml`](docs/deployment/recommended-config.yaml)** — three blessed shapes: local Neo4j + SQLite, cloud AuraDB + Postgres, ArcadeDB + Postgres. Smoke test pins the per-block backend contract.

### Removed

- **LanceDB substrate** ([commit `29175d3`](https://github.com/ronsse/trellis-ai/commit/29175d3)) — removed in favor of ArcadeDB for the blessed self-hosted graph + vector path. LanceDB worked but pinned a non-standard wire format; ArcadeDB's Bolt + openCypher matches the rest of the stack.

## [0.6.0] - 2026-05-11

Two themes ship together: the v0.5.x deprecation window finally closes (Phase 6 PR 2 removals), and the cold-start / Reading-B story lands as the spec + supporting code surface a green-field user needs to feed Trellis from scratch.

### Added — cold-start specification + supporting code

- **Cross-database routing properties** on dataset-shaped entities ([`src/trellis/schemas/well_known.py`](src/trellis/schemas/well_known.py)). New canonical convention: `source_system`, `connection_ref`, `database_name`, `schema_name`, `physical_uri`. Populated automatically by `DbtManifestExtractor` (from manifest `metadata.adapter_type`) and `OpenLineageExtractor` (from namespace URI scheme). Query-engine agents now read routing from the entity properties rather than getting it from their prompt or out-of-band config.
- **`"dataset"` → `Dataset` canonical alias** in [`src/trellis/schemas/well_known.py`](src/trellis/schemas/well_known.py). OpenLineage's lowercase output now buckets correctly with the canonical Dataset type at retrieval.
- **`sources.yaml` schema + loader** ([`src/trellis/extract/sources.py`](src/trellis/extract/sources.py)). Declarative source registry: one entry per upstream system, path-or-endpoint XOR, env-var-only credential refs (never inline secrets), unique-name validation, optional `enabled` and `tier_override` fields. Consumed by the new refresh CLI; ad-hoc per-source invocations still work without it.
- **`trellis extract refresh` CLI** ([`src/trellis_cli/extract_refresh.py`](src/trellis_cli/extract_refresh.py)). Two invocation forms: `--source <name>` (looks up `sources.yaml`) or `--type <type> --path <path>` (one-shot). For each entity touched, computes a property-level diff against the prior state and emits a `TAGS_REFRESHED` event with the structured before/after payload. Wires cleanly into cron / GitHub Actions / Airflow / K8s CronJob — Trellis remains the substrate, your scheduler runs the loop.
- **Demo migration** ([`src/trellis_cli/demo.py`](src/trellis_cli/demo.py) + [`examples/cold-start-fixture/`](examples/cold-start-fixture/)). `trellis demo load` now also runs a dbt + OpenLineage fixture through the *real* extractor + governed mutation pipeline alongside the legacy hand-coded narrative content. Same code path a production deployment uses — kills drift between "demo" and "real ingestion." Fixture is hand-editable for drift-detection demos.
- **Sample query-engine agent + Makefile** ([`examples/docker-demo/`](examples/docker-demo/)). `make -C examples/docker-demo demo` runs an annotated end-to-end script in under 60 seconds: seeds the cold-start fixture in-process, prints the routing properties on dataset entities, sketches the closing of the feedback loop. No Docker required for v1; the in-memory ASGI shim does the job.

### Added — cold-start documentation (four cornerstone guides)

- **[`docs/agent-guide/modeling-guide.md`](docs/agent-guide/modeling-guide.md)** — extended with five new sections: the four-store mental model (graph / document / blob / vector), reference-vs-summary decision matrix, cross-database routing properties contract, a third worked example covering curated knowledge derivation from SQL query logs (`JoinPattern` / `AccessPattern` / `HotDataset`), and the freshness-signals model (`valid_from` / `importance_scored_at` / `TAGS_REFRESHED` / `Lifecycle.state`).
- **[`docs/agent-guide/source-modeling-cookbook.md`](docs/agent-guide/source-modeling-cookbook.md)** — new doc. Per-source recipes for Markdown docs, Jira, Confluence, SQL query logs, Unity Catalog, and git repos. Entity types, edges, reference-vs-summary tradeoffs, recommended curated derivations, refresh cadence.
- **[`docs/agent-guide/extractor-authoring.md`](docs/agent-guide/extractor-authoring.md)** — new doc. The `Extractor` Protocol contract, tier semantics (`DETERMINISTIC` / `HYBRID` / `LLM`), purity rule, idempotency keys, entry-point plugin registration, telemetry contract, annotated walks of the dbt + OpenLineage reference implementations, a MVP skeleton.
- **[`docs/agent-guide/freshness-and-curation.md`](docs/agent-guide/freshness-and-curation.md)** — new doc. The two refresh modes (periodic pull vs pushed events), `trellis extract refresh` CLI walkthrough, scheduler patterns (cron / GHA / Airflow / K8s CronJob), curator workflows, lifecycle transitions, the variation → selection loop.
- **[`docs/agent-guide/quickstart-query-agent.md`](docs/agent-guide/quickstart-query-agent.md)** — new doc. Install → seed → CLI verify → run sample agent → MCP integration → drift test. The "5-minute from `git clone` to working query-engine agent" walkthrough.

### Removed

- **Flat `StoreRegistry` properties** — `trace_store`, `document_store`, `graph_store`, `vector_store`, `event_log`, `blob_store`. Use `registry.knowledge.<store>` (graph, vector, document, blob) or `registry.operational.<store>` (trace, event_log). Deprecated since v0.4.0.
- **Flat `stores:` config block** in `~/.trellis/config.yaml`. Use `knowledge:` / `operational:` plane blocks. Deprecated since v0.4.0.
- **`TRELLIS_PG_DSN` env-var fallback.** Set `TRELLIS_KNOWLEDGE_PG_DSN` and `TRELLIS_OPERATIONAL_PG_DSN` instead (both can point at the same DSN). Deprecated since v0.4.0.
- **`trellis admin migrate-config` CLI.** The flat → plane-split migrator was a one-shot helper for the deprecation window; with the flat block gone there is nothing to migrate.
- **`trellis_api/models.py` re-export shim** and **`trellis_api/deprecation.py`** infrastructure (the `DeprecationNotice` DTO and `ROUTE_DEPRECATIONS` registry that drove `Sunset` / `Deprecation` response headers on legacy routes). All API DTOs now live in `trellis_wire.dtos`; legacy route paths are gone.
- **`PACK_PUBLISH` / `PACK_INVALIDATE` mutation operations.** Both were declared in the operation enum but had no handlers and no callers — dead surface.

## [0.5.1] - 2026-04-29

### Added

- **`PgVectorStore` dim-mismatch fail-fast** ([#64](https://github.com/ronsse/trellis-ai/pull/64)). On `_init_schema`, after `CREATE TABLE IF NOT EXISTS vectors` no-ops against an existing table, the store reads the actual column dim from `pg_attribute` and raises `ValueError` if it doesn't match `self._dimensions`. Pre-fix the store silently inherited the old dim and crashed on the first upsert with `DataException: expected N dimensions, not M`. Error message offers two resolutions — pass the matching dim, or DROP TABLE.
- **AuraDB vector-index cohabitation documentation** ([#64](https://github.com/ronsse/trellis-ai/pull/64)). New section in [`docs/deployment/neo4j-auradb.md`](docs/deployment/neo4j-auradb.md) covering the "one vector index per `(label, property)` pair" constraint, what each consumer (unit tests, eval scenarios, loader) does, and the recommendation to use separate AuraDB Free instances. Two new troubleshooting rows.
- **Scenario 5.4 — agent-loop convergence** ([`eval/scenarios/agent_loop_convergence/`](eval/scenarios/agent_loop_convergence/scenario.py)). Synthetic agent runs N rounds of build-pack → grade-coverage → record-feedback. Periodic effectiveness + advisory fitness loops tag noise items and score advisories. Convergence delta = mean useful-fraction on last quarter minus first quarter. Default 30 rounds × 3 domains × 4 traces / domain on SQLite completes in ~1.4s. Plan §5.4.
- **Scenario 5.5 — multi-backend feedback loop** ([`eval/scenarios/multi_backend_feedback/`](eval/scenarios/multi_backend_feedback/scenario.py)). Runs the convergence loop scenario 5.4 measures against three handles (sqlite / postgres / neo4j_op_postgres) and diffs loop counters + convergence deltas. `vector_store` + `document_store` pinned to SQLite across all handles so cross-backend drift is attributable to the feedback path under test (event_log + trace + graph). Live 3-handle run on Neon + AuraDB Free showed identical loop counters across all three. Plan §5.5.2 row 3.
- **EventLog → learning.scoring promote bridge** ([`src/trellis/learning/pack_observations.py`](src/trellis/learning/pack_observations.py)). `build_learning_observations_from_event_log` joins `PACK_ASSEMBLED` + `FEEDBACK_RECORDED` events on `pack_id` and produces the observation shape `analyze_learning_observations` consumes. Closes the §5.5.2 row 2 gap where the dual-loop's *promote* half was implementation-only with zero callers in the source tree. The file-only JSONL variant is logged in TODO.md as a deferred ADR-shaped item — `PackFeedback` carries no per-item shape so a JSONL bridge would need either a schema extension or a sibling `pack_assembly.jsonl`.
- **Live-backend wipe orchestrator** ([`eval/_live_wipe.py`](eval/_live_wipe.py)). Single `wipe_live_state(registry)` call that dispatches by store type so scenarios 5.1, 5.3, and 5.5 all share one hygiene path. SQLite is a no-op via type-name short-circuit. Replaces three handle-name-coupled helpers in 5.5 and adds wipe to 5.1 + 5.3 (which previously had none and were silently contaminated by stale rows on the shared Neon + AuraDB test DBs).
- **Regime-shift demo mode for scenario 5.4** — `regime_shift_round` + `advisory_min_sample_size` kwargs make the advisory suppression branch fire end-to-end on a controlled corpus (3 anti-pattern advisories suppressed at the pre-row-3 corpus baseline). Restoration is unit-test-only by architectural fence — see TODO.md "Advisory restoration unreachable in scenario context".
- **`helpful_item_ids`-driven `usage_rate` in `analyze_effectiveness`** ([`src/trellis/retrieve/effectiveness.py`](src/trellis/retrieve/effectiveness.py)). Switches noise tagging from pack-level success rate to per-item agent reference signal when the corpus carries it; back-compat fallback to the old success-rate heuristic when `helpful_item_ids` is absent. Flipped scenario 5.4's `convergence.useful_delta` from -0.131 to +0.652 on the baseline run.

### Changed

- **Bulk fast paths for `upsert_nodes_bulk` + `upsert_edges_bulk` across all three graph backends** ([#60](https://github.com/ronsse/trellis-ai/pull/60), [#62](https://github.com/ronsse/trellis-ai/pull/62), [#63](https://github.com/ronsse/trellis-ai/pull/63)). Pre-fix: the bulk paths looped per-row `upsert_node` / `upsert_edge` with per-row commits; on managed Postgres + AuraDB the round trips dominated wall time. Post-fix: pre-validate, bulk-fetch existing rows once, close priors in a single statement, INSERT all new versions via bulk syntax, single commit at end. Same atomicity story (one transaction wraps the batch, strictly stronger than the prior per-row commit loop). Measured: SQLite **32 → 33,464 nodes/sec** on fresh-bulk (~1000×); Postgres **1–5 → 1794 nodes/sec** on Neon (~300–1000×); Neo4j **45 → 3643 nodes/sec** on AuraDB Free (~80×) via a CREATE-only branch when the role-immutability pre-fetch returns empty.
- **Eval scenarios 5.1 + 5.3 use `vector_store.upsert_bulk`** ([#61](https://github.com/ronsse/trellis-ai/pull/61)). Both `populated_graph_performance` and `multi_backend_equivalence` were doing per-row `vector_store.upsert()` in Python loops — 200 round trips at ~70ms each on AuraDB Free dominated each scenario's ingest metric. Switched to the bulk method: `ingest_nodes_per_sec.neo4j` in scenario 5.3 climbed from 40 to 219.86 and the scenario reports `pass` for the first time.
- **`eval/generators/graph_generator.py` default `embedding_dim` 16 → 3** to align with the pgvector contract suite's `DIMS=3` constant. The shared Neon test DB has a single `vectors` table; PR #64 added the construction-time fail-fast on dim mismatch but didn't align defaults — eval scenarios at default settings would always trip the new check. Cosine similarity at dim=3 still surfaces cross-backend equivalence drift; vector quality is not what 5.1 measures.
- **Scenario 5.4 corpus generator anchors required entities** in trace sampling, and `DOMAIN_TEMPLATES.query_intent` strings rewritten to mention every required entity by name. Levels per-domain `success_rate` from skewed (`software_engineering=0.0`, `data_pipeline=1.0`, `customer_support=0.0`) to uniform `1.0`. Pivots scenario 5.4's primary convergence gate from `weighted_delta` to `useful_delta` (the post-fix corpus is uniform enough that the breadth-weighted score under-credits successful noise tagging).
- **Scenario 5.4 advisory wiring** — `PackBuilder` now receives `advisory_store` so attached advisories show up in `PACK_ASSEMBLED.advisory_ids`, and advisories generate only on the first periodic pass so IDs stay stable for presentation accumulation. Production gates (`_ADVISORY_MIN_PRESENTATIONS = 3`, `_MIN_SAMPLE_SIZE = 5`) **were not changed** — the original symptom was scenario-driven, not threshold-driven.

### Fixed

- **`eval/runner.py` UTF-8 encoding** — `Path.write_text` defaulted to cp1252 on Windows and crashed on Unicode characters in finding / decision text. Reports are machine artifacts; UTF-8 is the only sane wire format.

### Removed

- **`EvalQuery.expected_categories` field** in [`eval/generators/trace_generator.py`](eval/generators/trace_generator.py). Defined but never set or read — scenarios pass `expected_categories=["entity_summary"]` directly to `EvaluationScenario` at score time. Surfaced by the live-data revisit's dead-code audit.

## [0.4.0] - 2026-04-20

### Added

- **`trellis serve` CLI subcommand** — runs the REST API + UI with configurable `--host`, `--port`, `--config-dir`. Replaces the hardcoded `0.0.0.0:8420` in `trellis_api.app.main` and configures structured logging before uvicorn starts. Suitable for container ENTRYPOINTs.
- **`/healthz` and `/readyz` probe endpoints** — liveness (never touches stores) and readiness (calls `registry.operational.event_log.count()`, returns 503 until initialized). Wired for ECS, Kubernetes, and ALB target-group health checks. Unversioned, outside `/api/v1`.
- **Structured JSON logging** (`trellis_api.logging.configure_logging()`) controlled by `TRELLIS_LOG_FORMAT=json|console` and `TRELLIS_LOG_LEVEL`. JSON is the container default for CloudWatch / container log-driver ingestion.
- **Multi-stage Dockerfile** — `python:3.12-slim` base, `uv` builder, non-root runtime user, `[cloud,llm-openai]` extras, container-level `HEALTHCHECK` on `/healthz`. Plus `.dockerignore`.
- **Local `docker-compose.yml`** — offline rehearsal of the AWS ECS + RDS path. Boots the API container against `pgvector/pgvector:pg16` with the same code paths the cloud deployment uses. Exercises `trellis_knowledge` + `trellis_operational` schemas via the committed [`deploy/init-db.sql`](deploy/init-db.sql) init script and a mounted [`deploy/config.compose.yaml`](deploy/config.compose.yaml).
- **Cloud deployment documentation** — [`docs/deployment/aws-ecs.md`](docs/deployment/aws-ecs.md) runbook (ECR push, RDS + pgvector, S3 + VPC gateway endpoint, Secrets Manager, full task-definition JSON, bastion + MCP-stays-local note, backups), [`docs/deployment/local-compose.md`](docs/deployment/local-compose.md) smoke-test runbook, and [`docs/deployment/config.yaml.aws.example`](docs/deployment/config.yaml.aws.example) as a reference production config.
- **Client-repo starter scaffold** ([`examples/client_starter/`](examples/client_starter/)) — complete extract → ingest → retrieve loop showing the recommended layout for a consumer integrating Trellis from a separate Python repo. Demonstrates namespaced entity/edge types, a wrapped `TrellisClient` factory (remote or in-memory), a pure-function `DraftExtractor`, evidence ingestion, and pack retrieval. Verified end-to-end locally.

### Changed

- `trellis_api.app.main()` now accepts `host` and `port` parameters and configures logging before starting uvicorn (preserves the `DEFAULT_HOST = "0.0.0.0"`, `DEFAULT_PORT = 8420` behavior for existing callers).

### Resolved

- **SurrealDB BSL-1.1 license question** (previously tracked as an open item in [TODO.md](TODO.md)). SurrealDB 3.0 is BSL 1.1 with Change Date 2030-01-01 → auto-converts to Apache 2.0. The Additional Use Grant forbids only offerings "that enable third parties to create, manage, or control schemas or tables" — i.e. competing DBaaS products. Trellis consumers embedding SurrealDB as a hidden backend are allowed. If SurrealDB is picked, it must ship behind a `[surrealdb]` optional extra with the DBaaS carve-out documented in the backend-selection ADR.

## [0.3.2] - 2026-04-17

### Fixed

- Publish workflow's `publish` job failed at `actions/checkout` with "repository not found" because the explicit `permissions: id-token: write` block implicitly set `contents: none`. Added `contents: read` alongside the OIDC token permission.

## [0.3.1] - 2026-04-17

### Fixed

- `mypy` error in [`src/trellis_sdk/async_client.py`](src/trellis_sdk/async_client.py) that blocked the `test` job in the publish workflow. The `type: ignore[arg-type]` was on the wrong line inside a multi-line `httpx.AsyncClient(...)` call. The initial `v0.3.0` tag never produced a PyPI artifact — this is the first actual release.

## [0.3.0] - 2026-04-17

### Breaking changes

- **Removed `trellis-mcp-legacy` entry point** and deleted `src/trellis/mcp_server.py`. The current MCP server lives at `src/trellis/mcp/server.py` and is exposed as `trellis-mcp`. Anyone invoking `trellis-mcp-legacy` should switch to `trellis-mcp`.
- **Removed `[langgraph]` optional extra.** The LangGraph integration is no longer shipped in the wheel — it lives in [`examples/integrations/langgraph/`](examples/integrations/langgraph/) as a copy-paste reference template. Install `langgraph` and `langchain-core` directly in your project and copy `tools.py` in.
- **Moved `integrations/` to `examples/integrations/`.** None of the integrations (LangGraph, Obsidian, OpenClaw) ship in the wheel. They are reference templates you copy into your project. Test imports updated from `integrations.obsidian.*` to `examples.integrations.obsidian.*`.

### Added

- **PyPI publishing pipeline**: trusted-publisher (OIDC) workflow, `make build`/`verify-wheel`/`publish-check` targets, `workflow_dispatch` re-run path, `twine check` step, [RELEASING.md](RELEASING.md) runbook.
- **Examples directory** ([`examples/`](examples/)): SDK local + remote demos, retrieve→act→record loop, custom extractor, custom classifier, LangGraph agent, batch ingest script, and an MCP-from-Claude-Code walkthrough.
- **Skill templates** ([`skills/`](skills/)): drop-in Claude Code skills for `retrieve-before-task`, `record-after-task`, `link-evidence`.
- **MCP setup guides** for Claude Code, Cursor, and Claude Desktop in [`docs/getting-started/`](docs/getting-started/).
- **GitHub repo hygiene**: issue templates (bug, feature, config), PR template.
- **Python 3.13 support** added to CI matrix and PyPI classifiers.
- **`py.typed` markers** for `trellis_cli`, `trellis_sdk`, `trellis_api`, `trellis_workers` so type checkers see them as typed (`trellis` already had one).

### Changed

- **MCP server documentation now lists 11 tools, not 8.** The three sectioned-context tools (`get_objective_context`, `get_task_context`, `get_sectioned_context`) were already in the server but missing from every doc surface. Updated [docs/agent-guide/operations.md](docs/agent-guide/operations.md), [examples/integrations/openclaw/SKILL.md](examples/integrations/openclaw/SKILL.md), [README.md](README.md), and the IDE setup guides.
- **README links rewritten to absolute URLs** so they render correctly on PyPI.

## [0.2.0] - 2026-04-01

### Added

- **Classification Layer**: Hybrid deterministic + LLM tagging pipeline for all ingested content
  - Four orthogonal tag facets: `domain`, `content_type`, `scope`, `signal_quality`
  - Four deterministic classifiers: `StructuralClassifier`, `KeywordDomainClassifier`, `SourceSystemClassifier`, `GraphNeighborClassifier`
  - `LLMFacetClassifier` for async enrichment of ambiguous items (fires only when confidence < threshold)
  - `ClassifierPipeline` with two modes: ingestion (deterministic-only, microseconds) and enrichment (+ LLM fallback)
  - `compute_importance()` combining tags with LLM base scores for relevance ranking
  - `apply_noise_tags()` feedback loop: effectiveness analysis flags low-value items as noise, excluding them from future packs
  - Tag-based pre-filtering in `PackBuilder` (noise items excluded by default)

- **Web UI Foundation**: Dashboard served at `/ui` when running `trellis admin serve`
  - Live store stats (traces, documents, nodes, edges, events)
  - Store health status
  - Placeholder views for Graph Explorer, Evolution, Traces, and Precedents
  - Static files bundled in the PyPI wheel — no separate install needed

- **UI Design Documents**: Comprehensive design for full interactive UI
  - Graph Explorer with force-directed layout and time-travel slider
  - Evolution View: learning curve chart, pack composition drift, item lifecycle, domain generations
  - Trace Timeline, Improvement Dashboard, Precedent Library
  - ASCII wireframes, data flow diagrams, backend schema proposals
  - Demo scenario specification (8-week improvement arc from 40% to 85% success rate)

- **Package extras**: `all` convenience extra (`pip install trellis-ai[all]`)

### Changed

- FastAPI app version bumped to 0.2.0
- Fallback version updated to 0.2.0

### Fixed

- 22 code review issues across correctness, efficiency, and quality
  - `json_each` JOIN for multi-label domain filtering in SQLite stores
  - `get_node_history` ordering (DESC by `valid_from` for newest-first)
  - `StoreRegistry.close()` safety for partially initialized registries
  - `_emit_telemetry` exception handling in PackBuilder
  - Bounded idempotency cache (10K max) in MutationExecutor
  - `Content-Type` validation in API ingest routes
  - Defensive keyword extraction in `KeywordDomainClassifier`
  - `classification_version` default set to `"1"` in ContentTags

## [0.1.0] - 2025-12-15

### Added

- Initial release
- Core library (`trellis`): schemas, stores, mutation executor, retrieval, MCP server
- CLI (`trellis`): admin, ingest, retrieve, curate, analyze commands
- REST API (`trellis-api`): FastAPI server on port 8420
- Python SDK (`trellis_sdk`): dual-mode client (local or remote via httpx)
- Background workers (`trellis_workers`): ingestion (dbt, OpenLineage), maintenance
- Six store ABCs: TraceStore, DocumentStore, GraphStore, VectorStore, EventLog, BlobStore
- SQLite default backends with PostgreSQL cloud backends
- SCD Type 2 temporal versioning on graph nodes (time-travel via `as_of`)
- 13 edge types for entity relationships
- Governed mutation pipeline: validate, policy check, idempotency, execute, emit
- Context pack builder with keyword, semantic, graph, and recency search strategies
- Token-budgeted retrieval with two-stage limits (max_items, max_tokens)
- MCP server with 8 macro tools for Claude and other MCP clients
- OpenClaw skill for Claude Code integration
- LangGraph integration
- Obsidian vault indexer
- Effectiveness analysis and feedback loop
- Token usage tracking and telemetry
