# Changelog

All notable changes to Trellis will be documented in this file.

## [Unreleased]

### Changed

- **The steering text an MCP client reads now teaches the feedback call the
  learning loop can join.** The server `instructions`, the pack footer and the
  per-client setup snippets said "call `record_feedback`" without item ids,
  and feedback that names no item joins to nothing. Now:
  - Every empty flat pack (`get_context`, index mode and `search`) carries its
    `pack_id` header and feedback footer at every holdout rate, including the
    default `0`; before, an empty pack at rate 0 was a bare one-line answer that
    could not be graded, and its advisories could not be cited. A withheld pack
    and a genuinely empty one still take the same path and read alike.
  - The `instructions` say where the pack id and item ids sit, that a
    step's `result` is an object, and that feedback names helpful and unhelpful
    item ids (and `followed_advisory_ids`).
  - The footer of the flat, index and sectioned packs leads with `rating=` and
    names `helpful_item_ids`, `unhelpful_item_ids` and `followed_advisory_ids`.
    Advisories render above the footer, so the footer is the last thing in the
    response, and each advisory line says to cite it in `followed_advisory_ids`.
    Advisories remain outside the item budget.
  - SDK `get_context_for_task` passes the pack's `pack_id` to the formatter,
    so its markdown carries the footer, and forwards `max_tokens` to
    `assemble_pack`. Its footer reads `rating=...` rather than `success=...`.
  - The Claude Desktop, Cursor, Claude Code and OpenClaw snippets name
    `pack_id`, `rating`, `helpful_item_ids` and `unhelpful_item_ids`;
    `tests/unit/test_feedback_snippet_rule.py` fails a setup page that mentions
    `record_feedback` without `helpful_item_ids`.
  - `quickstart-query-agent.md` leads with `get_context` / `get_items`, lists
    the sectioned tools as deprecated, and documents HTTP MCP correctly
    (`TRELLIS_MCP_TRANSPORT=http trellis-mcp`, path `/mcp`, bearer API key)
    rather than `trellis admin serve`, which mounts no MCP.
  - The shipped `retrieve-before-task`, `record-after-task` and `link-evidence`
    skills drop their "preview" status and banner and move to `1.2.0`.

- **The holdout's `pre_treatment` rule now applies its commit half, and
  `post_hoc_share` is a figure.** The previous change recorded where the first
  retrieval sat against a stop hook's feedback and left two things unrecorded:
  the first commit and the last-tenth position. Each `retrieval_timing` now also
  carries `first_commit_ordinal` (the 1-based position, in the same tool-call
  numbering as `first_retrieval_ordinal`, of the first `Bash` call that
  `SessionOutcome.commits` counts, so an errored or unanswered call, a commit
  named inside `echo` or `grep`, and a `git log --grep` are not it) and
  `commit_before_first_retrieval`, an integer and a flag, never the command.
  `trellis analyze holdout` excludes a task whose first retrieval followed the
  first commit as well as hook feedback (`pre_treatment_excluded`, with
  `pre_treatment_excluded_after_commit` saying how many were commits). A join
  with neither comparison true and a missing one is still undecidable, kept and
  counted in `pre_treatment_unknown`, so every join written before this change
  reads unknown unless it shows hook feedback first. `post_hoc_share` is the
  share of tasks that reached the rule, in both arms, whose first retrieval came
  after hook feedback, after the first commit or in the last 10% of the
  session's tool calls; a join that lacks any of the positions is left out of
  both numerator and denominator and counted in the note, and with none
  measured the figure stays `None` with the reason. A session parsed again after
  this change gets one extra join for the new keys, which the holdout reads once
  as the latest; a session whose file never changes keeps its older join until a
  watermark reset parses it. `trellis analyze holdout` labels the funnel stage
  "first retrieval after a stop hook's feedback or a commit".

### Added

- **Trellis ships the loop as text for consumers that read no skill:
  `trellis admin show-prompt`, a fourth skill `trellis-loop`, and a docs page
  saying where to paste it.** The skills teach Claude Code the retrieve,
  record, grade loop, but a model called with an API key, a framework's system
  message and a dispatched subagent load no skill, and the repo held no
  system prompt for them. The text now ships in three variants under
  `trellis_cli/prompts/` (a sibling of `skills/`, so `install-skills` never
  copies it as a skill): `system` for a model's system message, `claude-md`
  for `CLAUDE.md` or a Cursor rule, and `agent-rule`, one paragraph for an
  agent definition. `trellis admin show-prompt [--variant ...] [--format
  json]` prints one byte for byte (JSON is `{"variant", "text"}`; an unknown
  variant exits 2 under both formats). `trellis-loop` carries the
  `agent-rule` paragraph, and `install-skills` and `quickstart --with-skills`
  end their text output with it plus a pointer to `show-prompt`; their
  `--format json` gains an additive `agent_rule` key.
  [`docs/getting-started/system-prompt.md`](docs/getting-started/system-prompt.md)
  gives the paste point for the Messages API, OpenAI, Cursor, Claude Desktop
  and a framework system message, and is linked from
  `integrate-your-agent.md` as a fourth path and from the Claude Desktop,
  Cursor and Claude Code setup pages. Every tool, keyword and field
  name the text uses is pinned to the server's signatures and
  `PackFeedbackRequest` in `tests/unit/mcp/test_shipped_prompt_claims.py`, and
  the page is pinned to the packaged files, so neither can drift from the
  code.

- **The holdout's `pre_treatment` exclusion is applicable: capture records
  where the first retrieval sat, and `trellis analyze holdout` drops a task
  whose first retrieval followed a stop hook's feedback.** The pre-registration
  counts a task as treated only if its first retrieval came before the work,
  but session capture recorded nothing that could decide it, so the rule was
  reported "not applied" while an operator's Stop/SubagentStop hook, which asks
  for a `get_context` at the end of a session that skipped one, produced
  afterthought retrievals that counted as treatment. Each
  `CAPTURE_SESSION_PACKS` join now carries `retrieval_timing`:
  `first_retrieval_ordinal` (the first tool call whose result served a pack),
  `tool_call_count`, `hook_feedback_messages` and
  `hook_feedback_before_first_retrieval`, counts and a flag only, never the
  message. Hook feedback is a user-record text block that opens like one,
  matched by `HOOK_FEEDBACK_PATTERN` in `transcripts.py` against the shape
  Claude Code writes (an `isMeta` user record whose string content opens
  `Stop hook feedback:`), and the funnel's
  `pre_treatment_hook_feedback_seen` makes a pattern that matches nothing
  visible instead of silent. The holdout excludes a task whose join says
  hook feedback came first (`pre_treatment_excluded`, a funnel stage before
  `eligible`), keeps one whose join predates the field (`pre_treatment_unknown`)
  so the analysis can be read both ways, and reports the exclusion as "not
  applied", "partially applied" or "applied". `no_retrieval` keeps its total
  and gains `never_called`, `errored` (the join's `retrieval_errors`, which
  nothing read before), `unparsed` and `empty_answer`, so a transport-failed
  `get_context` is no longer counted as never called. A session parsed again
  after this change gets one extra join (its payload now differs); the holdout
  reads each session's latest join only, so it counts once, and a session
  whose file never changes keeps its legacy join until a watermark reset parses
  it. The half of the rule about a first retrieval after a commit, and the
  post-hoc share, are still not recorded.
- **`MEMORY_EXTRACTION_STATE`: a dead memory extractor is a health event, and
  a failed LLM-client resolution is retried (#987).** With
  `TRELLIS_ENABLE_MEMORY_EXTRACTION` on, `_get_memory_extractor` cached `None`
  for the life of the process when no LLM client could be built on first use,
  so one transient outage at startup turned extraction off until a restart and
  left only an `info` log line, which the default CLI never prints. A
  "no client" outcome is now retried at most once per 60 s (one float
  comparison per save in between), and stops when the flag goes off. A
  construction failure (the client or the extractor raising, an `McpError`
  included) no longer raises at a save: it is logged with its traceback,
  reported as `unavailable` / `build_failed`, and retried on the same
  interval, so a save that has stored its document still embeds and never
  settles extraction off. The new event
  (`source="mcp:memory_extraction"`, payload `state` / `reason` /
  `error_type`) is written when the `(state, reason)` pair changes, never per
  save: `unavailable` (`no_llm_client` or `build_failed`, refreshed daily by
  the first save after the interval, with no background timer, so it
  outlives the 7-day window while saves keep arriving) and `available`
  (first successful build per process, and on recovery). `trellis analyze
  health` prints `Memory extraction: ...` with the onset and warns while the
  most recent event says `unavailable`; a window with no event reads
  `unobserved`, not healthy. The emit is wrapped whole, so it can never fail
  a save. The stale `capture_health.py` comment claiming
  `save_memory`'s only `MUTATION_EXECUTED` comes from extraction is corrected.
  The CLI bulk-ingest hook (`extract/memory_ingest_hook.py`) does not cache
  and is unchanged. It still logs the missing client at `info` and emits no
  event.
- **`RejectedItem.estimated_tokens`: a `max_items` rejection is priced, so
  the graph-selection replay can refill it (#970).** The builder records the
  budget walk's charge on the two budget cuts, `max_items` (which never
  reaches `budget_trace`) and `token_budget`; every other reason, and every
  event written before the field, stays `null` and reads as unpriced. A
  `max_items` row is priced at what the walk would have charged, the
  excerpt or the index line in index mode, not at a disclosure pointer,
  because disclosure runs after the cut. Before this every real
  `max_items`-bound pack reported `max_items_unpriced` and the refill figure
  covered only token-bound packs. `docs/api/v1.yaml` does not expose
  `RejectedItem`, so the spec is unchanged. Two replay fixes ride with it:
  the policy replay (`analyze replay --body-items` and its siblings, the
  older `_price` arm) budgets against `token_budget_effective` with the
  refill's fallback, so both arms agree under a safety margin; and a
  fractional, `nan` or `inf` budget or cost is refused rather than
  truncated (an integral float such as `8000.0` still reads), with the
  unmodelled-pack note no longer saying the token walk failed when it was
  the item cut.
- **`BudgetStep.walk_tokens`: the replay refill walks the prices the builder's
  walk charged, not the ones disclosure re-priced to (#970).** Graduated
  disclosure (on by default, `DEFAULT_BODY_ITEMS=12` against `max_items=50`)
  rewrites tail excerpts after the walk, and `_recharge_budget_trace` then
  re-prices the served `budget_trace` steps to pointers, while a
  `max_items`-cut row stays priced at the walk's full charge. The refill
  summed the two, so removing a withheld item freed more budget than it had
  and it admitted a row the builder's own counterfactual build turns away.
  Whenever disclosure demotes anything the builder now keeps the walk's
  charge on every included step as `walk_tokens` (additive; absent when
  nothing was demoted, where `item_tokens` is the walk's charge). Both replay
  arms walk `walk_tokens` when present, so the policy replay (`--body-items`
  and siblings) also prices a demoted row at its body, where it had read the
  pointer's price as the body. Its as-served baseline still reads
  `item_tokens`, what was paid. An event written before the field, whose
  `disclosure.pointer_item_ids` names an included step with no `walk_tokens`,
  cannot have its walk rebuilt: the graph-selection replay counts that pack
  unmodelled as `disclosure_repriced` rather than guessing. The policy replay
  keeps pricing such events as before, a known approximation. Two smaller
  gate fixes: a pack whose `token_budget_effective` is present but
  unreadable is excluded from the policy replay (counted under
  `packs_without_budget_trace`) when the policy refills, instead of walking a
  budget of 0; and a `budget_trace` step with a missing, fractional, `nan` or
  `inf` `item_tokens`/`walk_tokens` refuses its pack (`trace_unreadable`)
  instead of aborting the replay on `int()` or reading a missing charge as 0.
  `docs/api/v1.yaml` does not expose `BudgetStep`, so the spec is unchanged.
- **`analyze value` splits citations by `graph_selection`; `analyze replay
  --graph-selection` prices withholding one value (#961, part).** `value`
  gains a `by_graph_selection` block (JSON and a text table) counting
  per-`(pack_id, item_id)` servings in `recency_window`, `seeded` and
  `caller_seeded` buckets, plus `(graph, no stamp)` and `(not graph
  axis)` so the buckets sum to the new `attributed_servings`; counts
  print for every bucket and only the rates of a bucket under five
  attributed packs are refused. `replay --graph-selection VALUE`
  reports the matched servings with their recorded helpful, unhelpful
  and no-verdict counts, and, which `--class-prior` cannot, what the
  freed budget would have admitted, by re-running each pack's token
  walk only after it reproduces the recorded one (otherwise the pack is
  counted `unmodelled`). Holdout packs are dropped; both are read-only,
  and the replay records a meta-Activity unless `--no-meta-trace` is
  given, like its siblings. The decision the issue's third box asks for
  stays open.
- **Packs record `retrieval_phase` and `refresh`.** `get_context`,
  `search`, `get_objective_context`, `get_task_context`, and
  `get_sectioned_context` take an optional `retrieval_phase` —
  `"pre_work"`, `"mid_task"`, or `"post_hoc"` — refused with
  `INVALID_PARAMS` (library callers get `ValueError`) when the value
  is unrecognised, never silently stored. Both it (`null` when
  unstated) and `refresh` (always `true`/`false`) now land in the
  `PACK_ASSEMBLED` event payload, flat and sectioned alike, outside
  the telemetry the pack-effect holdout branch replaces — the same
  posture `index_mode` already had — so a `post_hoc` pack (most often
  a stop hook grading after the work is done) can be told apart from
  one that could have changed the outcome, and an unchanged re-serve
  (`refresh=false`) from a forced re-injection (`refresh=true`).
  `refresh` was already threaded through `PackBuilder.build()` /
  `build_sectioned()` for session-dedup bypass; this change is what
  makes it observable in the event.

- **Precedent auto-demotion, and a real `commit` mode for
  learning-candidate auto-promotion — both ship `enabled=False`.**
  `trellis.learning.precedent_auto_demote.PrecedentAutoDemotePolicy`
  evaluates current precedents against the same unhelpful-citation
  evidence gate `demotion_gate.py` uses for noise, and reports what it
  would demote; once an operator sets
  `learning.precedent_auto_demote.enabled` (and, to actually commit,
  its `mode` to `commit`), `run_precedent_auto_demotion` demotes each
  would-demote precedent through the same governed path a human
  `trellis curate demote-precedent` call uses, capped by
  `max_demotions_per_run` and rate-limited per run. New CLI `trellis
  curate evaluate-precedent-demotion`, catalog job
  `evaluate-precedent-demotion` (daily cadence, deterministic, no LLM
  spend), and `SETTINGS_REGISTRY` family `learning.precedent_auto_demote.*`
  (group `learning_loops`). Every run emits exactly one
  `LEARNING_AUTO_DEMOTE_EVALUATED` event, whatever it did, with the same
  `outcome`/`configured_mode`/`policy_snapshot` shape the sibling
  promotion event already used — a run that exits before evaluation
  still emits one event rather than reading as "never ran".
  - This is this project's safety rollback for auto-promotion's own
    `commit` mode: `LearningCandidateAutoPromotePolicy.__post_init__`
    now accepts `mode="commit"` (previously refused unconditionally
    with a `ConfigError`) once constructed with
    `demotion_enabled=True`, i.e. once
    `learning.precedent_auto_demote.enabled` is also on — running one
    loop's commit mode without the other's would leave a wrong
    unattended promotion with no automatic undo. `run_candidate_auto_promotion`'s
    commit-execution branch is now reachable: it promotes each
    qualifying candidate through the same governed path a human
    review-queue approval uses, capped by `max_promotions_per_run`.
    The Settings UI still never offers `commit` as a selectable value
    for `learning.candidate_auto_promote.mode` (only the environment
    variable can set it, and construction still refuses it unless
    auto-demotion is enabled too) — that permanent Settings-layer block
    was previously described as lasting only "until auto-demotion
    ships"; it ships in this change and the block stays.
  - `trellis.ops.loop_health` gains a ninth row,
    `learning_precedent_auto_demote`, placed directly after
    `learning_candidate_auto_promote` since it is that loop's rollback;
    both rows now derive `actuates` from the latest run's own recorded
    effective mode rather than a hardcoded `False`, since both can now
    genuinely commit. The `auto-promote-candidates` catalog job
    description and the Loops tab's labels and Precedents-tab empty
    state no longer claim auto-promotion can never write — they say
    what it does in each mode instead.
  - No default behavior changes: both policies ship `enabled=False`,
    so every existing deployment stays exactly as report-only as
    before until an operator opts in to one or both.
  - **A human `precedent.reinstate` resets the demotion evidence
    window.** `discover_current_precedents_with_coverage` now starts
    each precedent's window at the later of its last successful
    `precedent.promote` and its last successful `precedent.reinstate`,
    in both report-only and commit mode, so citations from before a
    reinstatement no longer count toward demoting it again on the
    next run. A reinstated precedent with no new citations reports
    `ineligible:no_evidence_supplied` (report-only:
    `refused:no_evidence_supplied`) instead of being silently
    re-demoted.
  - **Commit mode never demotes on a truncated evidence scan.** When
    the citation join is truncated, a precedent is demoted only if its
    evidence window start is at or after the scan's `covered_since`;
    every other precedent is reported under a new disposition,
    `skipped_incomplete_evidence`, and counted via the report's new
    `skipped_incomplete_evidence_count`, never silently dropped.
    Report-only shows the same classification.
  - **Human-promoted precedents are pre-filtered, not refused per
    run.** Both report-only and commit mode now drop a human-promoted
    precedent before judging it (reusing the existing
    `_was_auto_promoted` derivation), reporting it
    `ineligible:not_auto_promoted` (report-only:
    `refused:not_auto_promoted`) instead of listing it as
    `would_demote` or submitting a command the handler's own
    provenance check refuses every run. The handler's provenance check
    stays as defence in depth.
  - **A hand-edited `settings.json` can no longer set `mode: commit`.**
    Resolving `learning.precedent_auto_demote.mode` (and the
    promotion twin) now checks a stored settings value against the
    setting's `refused_values` the same way the environment-variable
    path already does, raising `ConfigError` and naming the setting
    when one is found; the CLI exits `5` and the rejection is
    recorded. Only the environment variable can still select `commit`.
    `GET /api/v1/settings` degrades only that setting's family to its
    defaults, flagged with a parse error, rather than failing the page.
    `--dry-run` writes nothing, so it still runs report-only over such a
    stored value.
  - **The demote command carries an `idempotency_key`** derived from
    the precedent id and its evidence window start, so two overlapping
    runs cannot both count a demotion or spend rate-limit cap on the
    same no-op; the loser reports a new disposition, `deduplicated`
    (`PrecedentAutoDemoteReport.deduplicated_count`), and is never
    counted as `demoted`.
  - Both the demotion and promotion report JSON gain a top-level
    `status` key, derived from the same flag that drives the exit
    code.
- **Operator UI: paged search results, a reachable event lookup, typed
  report parameters, and a help link on every tab.** `GET /precedents/domains`
  mirrors `GET /traces/domains` (same shape, same demoted/archived
  filtering as `GET /precedents`), so the Precedents domain filter lists
  every domain rather than only the ones on the current page; the Memories
  search results are paged the same way the unfiltered document list
  already is. `GET /events/{event_id}` gets one event by id regardless of
  the recency window a list caller has loaded, 404 when absent, under the
  same pack-holdout rule as `GET /events`; the Events detail view uses it
  so an older event opens instead of reading as not found. The Graph tab's
  first-visit sample load now renders a failed `/graph/search` call into a
  banner instead of discarding it, and the Reports tab no longer runs a
  report on open — the operator picks one. `GET /api/v1/analyze` gains a
  `params_schema` field per report (`params_model.model_json_schema()`),
  and the Reports tab renders a typed control per parameter from it —
  a bounded number input for an integer/float, a checkbox for a boolean, a
  select for an enum (the holdout report's `outcome` is now one) — with
  client-side bounds checking before submit and a legible rendering of any
  422 that still reaches the server. Every sidebar tab carries a small "?"
  link to the matching section of the operator docs on GitHub; a test
  reimplements GitHub's heading-to-anchor rule and checks every linked
  anchor against the real headings in that file on this branch.

- **Precedent demotion verb (manual-trigger only): `precedent.demote` through
  the governed mutation pipeline, with CLI `trellis curate
  demote-precedent`, REST `POST /precedents/{target_id}/demote`, and MCP
  `execute_mutation`.** A new `Lifecycle.state` value, `"demoted"`, is an
  SCD-2 rewrite of a precedent's graph node — not a purge like `redact` —
  so the pre-demotion version stays readable via `get_node_history`/`as_of`.
  The reversal verb, `precedent.reinstate`, and the other follow-up fixes
  below shipped in the next PR. Only a precedent promoted from a
  learning candidate has a graph node and can be demoted; a trace- or
  miner-sourced precedent id is refused with a message naming that gap
  rather than a generic "Node not found". `PrecedentDemoteHandler` refuses
  a non-precedent target and requires a non-empty, length-bounded reason
  written to the audit trail. Provenance is re-derived from the event log,
  never trusted from a caller-supplied flag: a caller demoting under the
  (not yet wired) automatic requester identity may demote only a precedent
  whose own `precedent.promote` event was itself automatic, while every
  other caller may demote any graph-backed precedent regardless of lifecycle
  state. Re-demoting an already-demoted precedent is an idempotent no-op
  (no new SCD-2 version, no new event, the stored reason unchanged) that
  says so; demoting a precedent whose lifecycle state is not `"current"`
  (superseded, archived, or anything else) is refused, naming the actual
  state. Once demoted, a precedent is excluded from `GET /precedents`,
  `trellis retrieve precedents` and `get_lessons`, and is withheld from
  packs at the collect seam alongside the existing archived/noise gates,
  recorded in `rejected_items` with its own reason so the pack's
  withholding statement names it. `"demoted"` is deliberately left out of
  `RetentionPruneHandler`'s `UNSELECTABLE_STATES`, so it stays reachable by
  `retention.prune --lifecycle-state demoted`.

- **Precedent demotion's reversal verb and four ways a demotion could come
  back or keep being served, closed: `precedent.reinstate`, through CLI
  `trellis curate reinstate-precedent`, REST
  `POST /precedents/{target_id}/reinstate`, and MCP `execute_mutation`.**
  Human-only, like `precedent.demote`'s manual-trigger posture: a caller
  under the automatic demotion policy's own requester identity is refused
  outright. Only a precedent whose current state is `"demoted"` may be
  reinstated — `"current"`, `"archived"`, not found, or not graph-backed is
  refused legibly, naming the actual state rather than no-opping or
  re-demoting. `PrecedentReinstateHandler` re-syncs the precedent's vector
  row in the same call `PrecedentDemoteHandler` now also does on demote, so
  a semantic hit stops carrying the pre-change snapshot and the pack
  collect seam's `partition_demoted` sees the update. After a reinstate,
  every reader serves the precedent again: `get_lessons`, `GET /precedents`,
  `GET /precedents/domains`, `trellis retrieve precedents`, and a flat pack.
  Four more gaps close alongside the new verb:
  - **Re-approval no longer silently un-demotes.** Re-approving a precedent's
    learning candidate (`submit_learning_promotion`, reachable from the
    Review tab and the CLI) is refused for **every non-current state** —
    demoted, archived, superseded, draft, and deprecated alike, each naming
    its own remedy (`reinstate-precedent` for demoted, `retention.restore`
    for archived, the successor entity for superseded) — instead of quietly
    rewriting the node to `"current"` with no reason recorded. A precedent
    that is already `"current"` (including one just reinstated) still
    re-approves normally, adding a version and a `PRECEDENT_PROMOTED` event
    exactly as a first-time promotion does: `entity.create`'s own existing-
    precedent guard refuses only a non-current precedent or a mismatched
    `entity_type`, not a current one, so legitimate re-approval and this
    refusal coexist.
  - **`entity.update` refuses the lifecycle bypass it used to be.** A write
    to `properties.lifecycle` where the node's current type is `"precedent"`
    is refused, naming `precedent.demote`/`precedent.reinstate` instead —
    the only way `entity.update` could previously undo (or redo) a
    demotion, unaudited and without the vector re-sync. Every other node
    type's `lifecycle` key is unaffected (the save-surfaces' supersession
    stamp still writes it this way). The guard reads the merged properties
    whether `properties` arrives as a dict or as an iterable of `(key,
    value)` pairs — both shapes are normalised before the check runs, so a
    pairs-form payload can no longer reinstate a demoted precedent through
    MCP `execute_mutation` or REST `/commands/batch` the way a dict-form
    payload was already refused from doing.
  - **An MCP `execute_mutation` demote (and now reinstate) carries its
    target as `entity_id` on the `MUTATION_EXECUTED` event**, on every
    surface — CLI, REST, and MCP alike — instead of `entity_id=None` on the
    MCP path.
  - **Prune-then-restore no longer launders a demotion.** `retention.restore`
    returns a precedent to the lifecycle state it held immediately *before*
    its archival — read from SCD-2 history — rather than unconditionally to
    `"current"`, so a precedent that was demoted and then pruned comes back
    demoted. A precedent whose pre-archival state cannot be read from
    history is refused individually rather than aborting the whole batch:
    the rest of a mixed batch still restores or skips, and the command is
    rejected only when every requested id was refused and none was
    restored.
  - **`save_knowledge(supersedes=<precedent>)` is refused before any write.**
    `plan_entity_supersession` previously let the evidence document get
    written and only then hit `entity.update`'s lifecycle guard, so the
    tool raised "no entity was created; re-send the same call to finish" —
    advice that could never succeed, after a write had already landed. It
    now refuses a precedent target immediately after the node lookup,
    naming `precedent.demote` as the way to withdraw a precedent instead,
    so nothing is written. (`save_memory`'s `supersedes` only ever accepts
    a document id, so a precedent — a graph entity — was never reachable
    through that tool.)
  - **A demote/reinstate vector-sync failure now surfaces as a warning**
    instead of a silent log line. `precedent.demote` and
    `precedent.reinstate` still return `SUCCESS` when the paired vector-row
    re-stamp fails, but the `CommandResult`/REST/MCP response now carries a
    `..._vector_lifecycle_sync_failed` warning naming the risk — the
    semantic path may keep serving a stale snapshot until the next resync —
    on every surface that passes `result.warnings` through.

- **Admin UI for the job schedule registry: `GET`/`PATCH /api/v1/admin/jobs`,
  `POST /api/v1/admin/jobs/{name}/run`, CLI parity, a Schedules tab (e175,
  plan p1 PR 6).** Completes the schedule registry below (#e166): an admin
  can now edit a catalog job's `enabled`/`cadence`/`timeout_seconds` and
  request an out-of-band run, without the write surface ever being able to
  name a command, argv, path or environment variable —
  `AdminJobPatchRequest` has no such field, and `extra="forbid"` refuses any
  other field name with 422 rather than silently ignoring it. `GET
  /api/v1/admin/jobs` joins every `JOB_CATALOG` entry with its
  `ScheduleStore` row and its last-run status, derived from the EventLog's
  own `JOB_RUN_STARTED`/`JOB_RUN_COMPLETED` pair — never a hardcoded
  literal. "Run now" writes only `run_requested_at`; the response's
  `dispatcher_hint` says a host dispatcher polling `due-jobs` picks it up,
  typically within ~5 minutes, and never claims the run itself completed.
  Both writes refuse with 409 and the schedule store's real error text while
  it is degraded or stale, and both emit an audit event (`SCHEDULE_UPDATED`
  / `JOB_RUN_REQUESTED`) carrying only the changed old/new fields plus the
  caller's identity. `trellis admin schedule set <name>` / `schedule run
  <name>` give the same two writes from a terminal, `--format json` and an
  exit code independent of format. The web UI's new **Schedules** tab
  renders cadence as both raw cron and hand-formatted text (labelled UTC —
  `trellis.core.cron` evaluates every production caller's `utc_now()`, not
  the dispatcher host's local clock), an enable toggle, inline edit with
  bounds validation, a two-step "Run now" confirm with an extra warning for
  `spends_llm`/`heavy` jobs, and a dispatcher-activity banner that compares
  against the longest enabled cadence's own tick interval rather than a
  fixed window.
  **Gate #860 fixes, same change:** a degraded schedule store is now
  surfaced on `GET /api/v1/admin/jobs` (`store_degradation`, each row's
  `stored: bool`) instead of showing catalog defaults as if they were the
  operator's own stored, enabled choice; "Run now" refuses a disabled or
  never-stored job (`job_disabled` / `not_scheduled`) instead of silently
  enrolling it into the catalog's default cadence; `run_requested_at` in
  the run response is exactly what the handler wrote, or `null`, never a
  timestamp fabricated by the route or the CLI; and the Edit form reads the
  current cadence from a data attribute rather than scraping rendered
  table text, so saving with no changes round-trips instead of clobbering
  the cadence with whatever the display text happened to contain.
  **Bug found and fixed in the same change:** `admin schedule set`'s
  tri-state option detection compared `ctx.get_parameter_source(name)`
  against `click.ParameterSource.COMMANDLINE` by identity — Typer invokes
  that callback through its own vendored click fork, whose `ParameterSource`
  is a distinct enum class with identically-named members, so the
  comparison was always `False` and every `--enabled`/`--cadence`/`--timeout`
  flag was silently a no-op regardless of what was passed. Fixed by
  comparing `source.name == "COMMANDLINE"` instead.
- **Schedule writes route through `MutationExecutor`, not a direct store
  call (e175 rework, PR #860).** The admin route and CLI above now submit
  `Operation.SCHEDULE_UPDATE` / `Operation.JOB_RUN_REQUEST` `Command`s to a
  new `trellis.mutate.build_schedule_executor`, whose `ScheduleUpdateHandler`
  / `JobRunRequestHandler` (`trellis/mutate/schedule_handlers.py`) are the
  only code left that calls `ScheduleStore.put`. Both handlers re-enforce
  the same boundary the wire layer already had — a catalog job name only;
  only `enabled`/`cadence`/`timeout_seconds` on update, no fields at all on
  a run request; every check runs before the store write, so a rejected
  command never reaches `ScheduleStore.put`. The two operations are
  deliberately **not** in `create_curate_handlers`: that dict backs `POST
  /api/v1/commands/batch` (`mutate` scope) and MCP `execute_mutation`, and
  a mutate-scoped credential must not be able to edit a job's schedule or
  request a run — those stay admin-only through a separate authorization
  axis. Submitting either operation to the curate-wired executor fails
  with "no handler registered for: \<operation\>", the same path an
  unknown operation gets anywhere else. `changed_by`/audit identity comes
  from the authenticated principal, never a client-supplied field, and a
  policy scoped to `entity_type=schedule` now refuses the route and the
  CLI the same way it refuses any other governed mutation.
- **Governed tag/domain candidate review — Apply and
  Revoke.** `trellis classify tag-candidates` / `domain-candidates` have
  always *mined* keyword and alias rules (`TAG_KEYWORD_CANDIDATE` /
  `DOMAIN_ALIAS_CANDIDATE` events) but never written anywhere — approving
  one meant hand-pasting YAML into `config.yaml`, with no audit trail.
  This adds the missing approve step:
  - `classify_overrides.json` (`<data_dir>/stores/`): a third
    `DegradableJsonStore` subclass (CLAUDE.md: "a third such file gets a
    subclass, never a copy") alongside `PolicyStore` / `AdvisoryStore` —
    reads degrade row-by-row, writes refuse whole. Holds applied
    `tag_keyword` / `domain_alias` rows, keyed by `(kind, key)`.
  - **Precedence**: built-in defaults < `config.yaml` < this overlay.
    `effective_domain_keywords()` / `effective_domain_aliases()`
    (`trellis.classify.factory`) are the only readers of the merged view;
    every call site that used to read `config.yaml` alone now reads
    through them.
  - New governed operations `Operation.CLASSIFY_RULE_APPLY` /
    `CLASSIFY_RULE_REVOKE`, routed through a dedicated admin-only
    executor (`build_classify_rules_executor`) rather than the general
    curate executor — applying a mined rule changes what the
    classifier does on every future document, not a piece of content.
    Apply carries the candidate's original `source_event_id` forward so
    the applied row traces back to the event that proposed it.
  - REST: `GET /classify/candidates`, `POST /classify/candidates/mine`,
    `POST /classify/candidates/{id}/dismiss`, `POST /classify/rules`,
    `DELETE /classify/rules/{kind}/{key}`, `GET /classify/rules` — admin
    scope on every write, read scope on the two `GET`s, mirroring
    `policies.py`'s mixed-scope mounting.
  - CLI: `trellis classify apply-rule`, `revoke-rule`, `effective-rules`
    — same executor, text/JSON output, exit code derived from the
    `--format`-independent refusal mapping (CLAUDE.md format/exit
    parity rule).
  - UI: a new "Tag rules" tab lists open candidates with Apply/Dismiss
    and the effective-rule table with Revoke gated to `source ==
    "overlay"` rows (a `default`/`config` row was never written by the
    overlay store and the `DELETE` route refuses it).
- **Analyze reports over HTTP + a Reports UI tab (#e182).** "Are our
  learning loops performing?" was answerable only from the CLI
  (`trellis analyze <report>`); it now has a read-scoped HTTP surface and a
  UI tab, both calling the exact library functions the CLI calls — never a
  copy of their logic.
  - `GET /api/v1/analyze` lists the allowlisted reports with an operator
    description and default params each.
  - `GET /api/v1/analyze/{report}` runs one: `holdout`, `judged-outcomes`,
    `advisory-effectiveness`, `value`, `dimension-predictiveness`,
    `learning-candidates`, `domains`, `extractor-fallbacks`,
    `pack-telemetry`, `token-usage`, `cost`. Each report has its own
    `extra="forbid"` param DTO; an unknown report 404s listing the allowed
    names, a bad or unknown param 422s. `pack-quality` (needs a scenarios
    file, no bounded default shape), `replay`, `generate-advisories` and
    `apply-noise-tags` (writes) are deliberately excluded.
  - Payloads are finite JSON always — Infinity/NaN are sanitised to `null`
    before the response leaves the process, never written inline. A report
    that scans the whole EventLog is cached per-process for 60s, surfaced
    via `computed_at` / `cached`.
  - `advisory-effectiveness` refuses to score a degraded advisory store
    (same check the CLI makes before its own dry-run), rather than silently
    scoring an incomplete set.
  - The UI's new "Reports" tab picks a report, fills params from its
    defaults, and renders the result as cards with a raw-JSON toggle,
    honest "insufficient data" / degraded states, and `computed_at`.
  - A small readiness pill in the header (fed by the `/readyz` probe the
    dashboard already fetches — no extra request) shows ready / degraded /
    unknown, with failing backend names on hover when the caller is
    authenticated.
  - G9 (`parameter_promotions` truthfulness, plus outcome/version signal)
    was re-measured rather than rebuilt: the metric and its annotated
    promote/reject/degrade/auto-rollback event strip already ship on the
    Metrics view (`src/trellis/retrieve/metrics_timeseries.py`) — no gap
    found, no new work needed there.
- **Schedule registry + job-run records (#e166).** The pieces a host-side
  dispatcher cron needs so job schedules can later be edited from a UI
  (plan p1 §(b)) without the container ever touching crontab — and the
  command boundary redesigned so the shared, read-write-mounted
  `schedule.json` can never choose *what* runs:
  - `trellis.schedule.catalog.JOB_CATALOG`: the **only** source of a job's
    command. A Trellis-native job (`tune`, `worker-enrich`, …) carries its
    argv fixed in source; a host-only job (`capture-nightly`,
    `curate-nightly`, `backup-nightly`, `roadmap-nightly` — they need the
    docker socket / `gh` auth the container doesn't have) names only an
    executable the operator places at `$TRELLIS_HOST_JOBS_DIR/<name>`,
    outside the shared data mount. No absolute personal path in the repo.
  - `schedule.json` (`<data_dir>/stores/schedule.json`) holds only
    operator-tunable state per `ScheduledJob` row: `name` (must be a
    catalog key — an unknown name degrades the row, never runs it),
    `cadence`, `enabled`, a bounded `timeout_seconds` override (1–86400),
    `run_requested_at`. `command`, `description` and `host_only` are gone
    from the persisted row — `extra="forbid"` means a legacy or tampered
    row still carrying `command` degrades instead of executing it. Backed
    by `ScheduleStore`, the third `DegradableJsonStore` subclass alongside
    `PolicyStore` and `AdvisoryStore` (reads degrade, writes refuse).
  - `trellis admin init-schedule` seeds the registry from the catalog's
    defaults — the nightly capture/curate/backup/roadmap sweeps, the tuner
    dry-run, and the manual-only commands from the UI gap inventory —
    without overwriting anything an operator already edited.
  - `trellis admin due-jobs --format json` joins each due row with its
    catalog entry and reports which jobs are due now, combining `enabled`,
    `cadence` (`trellis.core.cron`), the most recent `JOB_RUN_COMPLETED` in
    the EventLog, and `run_requested_at`. For a host-only job it reports
    `command: null` and `host_only: true` — a marker, not a path — so the
    host dispatcher resolves `$TRELLIS_HOST_JOBS_DIR/<name>` itself, after
    checking the name matches `^[a-z0-9-]+$` (no path traversal).
  - `trellis admin record-job-run` records one execution as a
    `JOB_RUN_STARTED` / `JOB_RUN_COMPLETED` event pair, modeled on
    `CAPTURE_SWEEP_COMPLETED`.
  - `docs/ops/job-dispatcher.sh.example`: a reference host-side dispatcher
    script (documentation only, not installed by this repo) showing the
    `due-jobs` → exec argv (or resolve + exec a host-only path) →
    `record-job-run` loop under `flock`.

  The REST route and UI that edit this registry shipped separately, above
  (plan p1 PR 6). **The actual
  boundary:** commands are fixed in source or confined to one host-only
  directory outside the shared mount; editing `schedule.json` — by hand, a
  compromised container, or the PR 6 API above — can change only
  *whether* and *when* a catalog job runs, never *what* runs. (An earlier
  draft of this entry claimed "no arbitrary command execution anywhere" on
  the strength of argv-not-shell alone; that claim did not hold once
  `schedule.json`'s own `command` field was the thing deciding what an
  argv-exec'd — this redesign is the fix.) **Deviation from the original
  PR:** `capture-nightly` and `curate-nightly` are now catalogued
  `host_only=True` (they were `False`) — both need the host's Claude Code
  project directories / `flock`-serialized worker invocation that a
  container does not have, matching `backup-nightly` and
  `roadmap-nightly`.
- **Report-only evaluation of learning candidates.** Owner's
  stated intent is "eventually we want automatic promotion"; this PR
  ships only the evaluation and reporting half of that — a candidate is
  never promoted or demoted by this code. Auto-demotion and a working
  commit mode are deferred to a follow-up PR.
  - `trellis.learning.candidate_auto_promote.LearningCandidateAutoPromotePolicy`:
    `enabled` (default `False`), `mode` (`report_only` default | `commit`),
    `min_helpful_count` (≥1, default 5), `min_distinct_sessions` (≥2,
    default 3 — an n=1 single-session candidate can never qualify),
    `require_zero_unhelpful` (default `True`), `max_promotions_per_run`
    (≥1, default 3). All six are `SETTINGS_REGISTRY` entries
    (`learning.candidate_auto_promote.*`, group `learning_loops`)
    resolved env > settings > default, each with a plain-English UI
    description. **`mode="commit"` raises `ConfigError` at construction**
    — "commit mode is not available until auto-demotion ships" — on
    every resolution path (CLI, settings, env); `report_only` is the
    only mode that can actually run. `--dry-run` forces this run's
    *effective* mode to `report_only` before the policy is constructed,
    so a stored or env-configured `commit` never reaches the refusal —
    the run still truthfully reports what was configured: the CLI
    output, the emitted event, and the Loop Health row all carry a
    separate `configured_mode` (the pre-`--dry-run` value) alongside the
    `mode`/`effective_mode` that actually ran, so "commit is configured"
    and "commit ran" stay distinguishable. The Settings UI does not
    offer `commit` as a selectable value for this setting at all, and the
    settings route reports it under `refused_values`/`refused_reason`
    rather than letting a client learn the refusal only by submitting it.
  - The pre-existing tuner-side `trellis.learning.tuners.auto_promote.AutoPromotePolicy`
    (`enabled`, `min_sample_size`, `min_effect_size`) is now also a
    `SETTINGS_REGISTRY` family (`learning.auto_promote.*`). `resolve_auto_promote_overrides`
    is new in this PR — it is the first thing to resolve env > settings >
    config.yaml > default for this family — and reads three new env vars:
    `TRELLIS_ENABLE_AUTO_PROMOTE`, `TRELLIS_AUTO_PROMOTE_MIN_SAMPLE_SIZE`,
    `TRELLIS_AUTO_PROMOTE_MIN_EFFECT_SIZE`. Registering the family adds a
    UI-tunable surface, not a second resolution path. Both families' effective values now appear in
    `GET /api/v1/settings` (#857), computed by calling each resolver
    directly rather than reimplementing env/settings/default precedence a
    third time.
  - `run_candidate_auto_promotion` evaluates every candidate and reports
    a disposition — never writes a precedent, since the commit-execution
    branch is unreachable while `mode="commit"` is refused at
    construction. Evaluation order: already-promoted (any existing
    `PRECEDENT_PROMOTED` for that candidate's entity id, human or
    automatic, reports `already_promoted` and is excluded from
    `would_promote` and from the `max_promotions_per_run` cap), then
    not-promotable recommendation type, then unhelpful citations (if
    `require_zero_unhelpful`), then the helpful-count and
    distinct-session floors.
  - Distinct-session counting now reads the real `PACK_ASSEMBLED.session_id`
    captured on each citation, never a pack or run id. A citation with
    no captured session id cannot count toward `min_distinct_sessions`
    and is reported separately as `sessionless_citations`.
  - Every invocation of this command emits exactly one
    `LEARNING_AUTO_PROMOTE_EVALUATED` event, whatever it did. A completed
    run carries a per-candidate disposition, the
    would_promote/already_promoted/promoted/ineligible counts, the
    policy snapshot, and an `outcome` of `"ok"` (or `"degraded"` if a
    candidate was skipped for having no usable id). A run that exits
    *before* reaching evaluation — a missing/malformed `--candidates`
    file or a top-level shape error (exit 2), a non-finite env override
    (exit 1), or a refused `mode=commit` without `--dry-run` (exit 5,
    `ConfigError`) — still emits one event, with `outcome: "refused"`
    (the `ConfigError` case) or `outcome: "failed"` (everything else)
    and an `error: {type, message}`, never zero events: a silent exit
    would otherwise be indistinguishable from "this job never ran" in
    `loop_health.py`, whose `last_status` now derives from this
    `outcome` field (falling back to the old skipped-based rule for an
    event recorded before the field existed) instead of always reading
    "ok" off the mere presence of an event. `report_to_dict` adds
    `already_promoted_count` alongside `would_promote_count`.
    `compute_agreement_metric` compares this policy's `would_promote`
    dispositions against later human Review-queue decisions
    (agree/disagree/undecided). Two data gaps reported honestly, not
    assumed away: there is no per-intent-family holdout marker anywhere
    in the pipeline — `_holdout_arm_status` always reports `"unknown"`
    and nothing gates on it — and no event records a human *rejecting*
    a candidate for this surface (only `action="promote"` exists), so
    `disagree` is structurally always `0` and `rate` can only be `None`
    or `1.0` today; `AgreementMetric.note` says so in the payload rather
    than leaving a reader to infer it from an un-explained constant.
    The `loop_health` panel gains an eighth row,
    `learning_candidate_auto_promote` (`actuates=False`: nothing it does
    writes without a human in the loop today), whose `last_status` also
    reads `"stale"` once the job's known daily cadence
    (`auto-promote-candidates` in the job catalog) has had a tick with
    no new run since.
  - `trellis curate auto-promote-candidates` (job catalog:
    `auto-promote-candidates`, nightly). A missing `--candidates` file,
    one that isn't valid JSON, or one whose top level isn't a JSON
    object (e.g. a bare list) all fail with a clean `BadParameter`
    message (exit 2) instead of a raw traceback.
  - The Precedents tab's empty state names the real promotion path
    (`trellis curate promote-learning`) and, conditionally on whether
    `auto-promote-candidates` is scheduled, that the job reports but
    never commits — true both before and after the job is scheduled.
  - **Removed `Operation.PRECEDENT_UPDATE` and `EventType.PRECEDENT_UPDATED`.**
    The verb had a registry schema but no handler since the initial
    commit, so every request for it was rejected at dispatch, and
    `PRECEDENT_UPDATED` was never emitted; both are removed.
  - The Settings UI never offers `commit` as a selectable value for
    `learning.candidate_auto_promote.mode`: `GET /api/v1/settings` adds
    `refused_values`/`refused_reason` to this setting's row (so a client
    learns the refusal from the schema, not by submitting it and reading
    a validation refusal — `400` over REST, exit `2` from the CLI), and
    `renderValueInput` disables the refused option in the dropdown
    instead of letting it be picked and then rejected. The save path's
    "must be one of" validation error also no longer lists `commit`
    among the values it suggests trying instead.
  - `AutoPromotePolicy.__post_init__`'s effect-size floor check rejects
    a non-finite `min_effect_size` (`NaN`/`inf`) regardless of whether it
    arrived via env, the settings store, or `config.yaml` (a YAML
    `.nan`/`.inf` literal parses straight to a non-finite `float`) — a
    non-finite value compares `False` against every bound the "looser
    than the manual floor" check uses, so without the dedicated
    `isfinite` check it would construct successfully and let every
    proposal clear the gate.
- **Loop health panel (e167): `GET /api/v1/loops`.** Answers "is each
  curation / learning loop actually doing anything?" for all seven loops
  (noise demotion, advisory generation, advisory fitness,
  learning-candidate scoring, precedent promotion, tuner, feedback
  intake) from the EventLog alone — no new probe. `trellis worker curate`
  and `trellis worker tune` now emit `CURATE_CYCLE_COMPLETED` /
  `TUNE_CYCLE_COMPLETED` at the end of every **live** run (never on
  `--dry-run`, which still writes nothing), carrying the same counters
  `--format json` already reports; the emit is wrapped whole, so a broken
  EventLog write cannot fail the cycle it is reporting on. Each row
  carries `actuates: bool` and a one-line `what_it_changes`, and — the
  one load-bearing distinction — a loop with no event yet reports
  `last_run_at`/`last_status`/`counters` all `null` ("never run"), never
  a bare `0` indistinguishable from "ran and found nothing to do". New
  module `trellis.ops.loop_health`; route is admin-scoped, alongside the
  other Review-queue surfaces in `trellis_api.routes.admin`. A new Loops
  tab in the operator UI (`src/trellis_api/static/index.html`) renders
  the report: timestamps labelled UTC with relative age, a loop whose
  last run is over 36h old is flagged `stale` independently of its own
  `last_status`, a never-run loop reads "Not measured" rather than a
  bare zero, and a fetch failure renders a visible error instead of a
  silent empty state.
- **Pack feedback from the Packs detail view.** `POST /packs/{pack_id}/feedback`
  was the only learning-loop input the dashboard could add and the UI never
  called it — the "Feedback" count on a pack's detail page only ever grew by
  an agent calling the route directly. The injected-items table now carries a
  Helpful/Unhelpful checkbox per row (checking one clears the other), plus an
  optional comment and two overall-verdict buttons ("Helpful overall" /
  "Not helpful") that set `success` and leave `rating` for the server to
  derive, matching the existing REST/MCP contract. A successful submit shows
  whether the event reached the EventLog yet (`event_log_in_sync`) and
  reloads the pack so the new row and count reflect it; a failed submit shows
  the error and re-enables the buttons instead of resetting the form. UI
  only — the route and `PackFeedbackRequest`/`PackFeedbackResponse` shapes
  are unchanged.
- **Advisories and Policies tabs in the web UI (#850).** Both surfaces
  existed only as CLI/REST before this: `GET /advisories` (+
  `POST /advisories/generate`, an admin action gated behind an inline
  two-step confirm, never `window.confirm`) and `/policies*` (list is
  read-scope, create/delete escalate to admin — a 403 now renders as "needs
  an admin-scoped key" rather than a blank panel). A degraded
  `AdvisoryStore`/`PolicyStore` (`DegradableJsonStore`) renders a dedicated
  banner from `store_degradation` so a damaged file never reads the same as
  an empty one, and `GET /advisories`' `stores_dir not configured` sentinel
  (a 200 with `{"status": "error"}`, not an HTTP failure) is checked
  explicitly rather than falling through to a silently empty table. Every
  interpolated value goes through `escHtml`; dates are rendered through a new
  UTC-labelled `fmtDateUtc` rather than the existing unlabelled `fmtDate`. The
  policy create form submits exactly one rule per policy, matching
  `trellis policy add`'s own shape; each field carries a one-line
  description, including the deny-wins resolution order.
- **Settings store and env > settings > default precedence for write-behaviour
  knobs.** `SettingsStore` (`<data_dir>/stores/settings.json`, built on the
  shared `DegradableJsonStore`) and a 13-entry catalog
  (`src/trellis/core/settings_registry.py`'s `SETTINGS_REGISTRY`: the 12
  `WriteBehaviourConfig` fields plus `graph_seeding`) let an operator override
  a write-behaviour knob without an env var — once a write surface exists; this
  change ships the store and the read path only, no CLI/REST write route and no
  `MutationExecutor` op yet (deferred to a follow-up). Every knob now resolves
  `env > settings > default`: `WriteBehaviourConfig.from_env_and_settings()`
  overlays a settings value only where the real environment is blank, and
  `resolve_overridden_by()` reports which of the three won, per field, as a new
  `overridden_by` column on `trellis admin write-config --format json`'s
  `knobs[]` and on `GET /api/version`'s `write_behaviour_settings[]`.
  `describe()` stays backward compatible: a caller that omits
  `overridden_by=` keeps the old `"env"`/`"default"` two-valued read. A damaged
  `settings.json` degrades per row (never raises) and falls back through the
  same precedence chain, logging `settings_load_degraded` at `error` so the
  reversion is visible; every write is refused while degraded or stale, same
  posture as `PolicyStore`/`AdvisoryStore`. Only `pack_holdout_rate`
  (`SettingSpec.settings_live`) has a settings-aware enforcement site today
  (`builder_factory.py`); the other twelve registry entries report truthfully
  but have no runtime effect from a settings override yet. See
  [`docs/agent-guide/operations.md` § Settings
  Store](docs/agent-guide/operations.md#settings-store).
- **Governed settings write path (#857), the write surface the entry above
  deferred.** `Operation.SETTINGS_UPDATE` (`SettingsUpdateHandler`) backs
  `trellis admin settings set|unset` and admin-scoped
  `PUT`/`POST /api/v1/settings/{name}` — unknown name, wrong type and
  out-of-range are refused, never clamped, through the real `MutationExecutor`
  Stage 2 policy gate on both surfaces.
  - The handler is registered only on a new, narrow
    `create_settings_handlers` / `build_settings_executor`, built by the admin
    route and the CLI alone — `create_curate_handlers` (and so the generic
    `POST /commands/batch` route and MCP's `execute_mutation`, both built on
    `build_curate_executor`) does not register it at all, so a
    `settings.update` submitted there answers `FAILED`, with a message
    naming where it *is* reachable, rather than being reachable itself. The
    handler also refuses any `Command` whose `target_type` isn't `"setting"`
    or whose `target_id` doesn't match the setting name, as defence in
    depth. An `entity_type=setting` deny policy refuses the write on every
    path that can reach it.
  - `changed_by` on the audit event comes from the authenticated principal,
    never the client-supplied `requested_by` (kept only as a separate
    `requested_by_claim`); `stored_value` is the normalized, parsed value,
    never the raw request body — surrounding whitespace included, so a
    string setting written as `" x "` stores `"x"`, matching the effective
    value `WriteBehaviourConfig.from_env_and_settings` resolves. A write
    behind a currently-set env var still stores the override and says so,
    identically on the CLI and REST and on a plain set and a reset alike (a
    reset leaves nothing stored, and the note says exactly that rather than
    claiming a stored value "takes effect" once the variable is unset). The
    response and event both carry `settings_live`/`restart_required` from
    the registry, with an explicit note that a `restart_required` change
    applies after a restart; `GET /settings` carries a `read_by:
    "trellis-api"` label so a multi-container deployment can tell a
    per-process read from a fleet-wide one. A degraded or stale
    `SettingsStore` answers REST `409` / CLI exit `5` (`EXIT_STORE`); the
    409 body carries `recovery` and the real `store_degradation` as their
    own fields (mirroring `routes/policies.py`), so the recovery command
    survives `sanitize_error_message`'s 512-character cap on `message`
    instead of being truncated away on a long `stores_dir` path.
  - `validate_value` refuses every value `WriteBehaviourConfig.from_env`
    would otherwise silently replace with the shipped default instead of
    applying — `reconcile_timeout_s <= 0` (including `-0.0`) and a
    blank/whitespace `reconcile_model` are rejected, not stored as a no-op
    override. Both sides of each affected knob share one parser
    (`parse_finite_float`), so an `int` too large for `float()` (e.g.
    `10**400`) raises `ValueError` — a governed `invalid_value` rejection —
    on both the env read and the settings write, never an uncaught
    `OverflowError` that would otherwise surface as the executor's generic
    `FAILED`.

  Each claim above is pinned by a regression test, not asserted on faith:
  `tests/unit/api/test_settings_routes.py`,
  `tests/unit/api/test_settings_update_not_reachable_via_batch.py`,
  `tests/unit/cli/test_admin_settings.py`,
  `tests/unit/core/test_settings_value_validation.py`,
  `tests/unit/mcp/test_execute_mutation.py` and
  `tests/unit/mutate/test_settings_update_handler.py` — rerun those (or
  `make test`) to reverify rather than trusting this entry.
- **Promotion-ready-candidate digest and readable fallback names (#845).**
  Prod had scored 796 learning candidates with 0 ever promoted, and 13 of the
  top 15 `promote_guidance` candidates carried an ugly bare-item-id
  `precedent_name` because `title` was missing at scoring time. Two fixes,
  both surfacing-only — nothing here promotes anything:
  - A title-less candidate now gets a readable fallback name — the first
    non-empty line of its document (frontmatter and a leading markdown
    heading marker stripped, truncated like the existing `precedent_name`
    convention) — resolved once a `DocumentStore` is reachable, at the point
    `write_learning_review_artifacts` writes the artifact. A candidate that
    already has a `title`, or whose item can't be resolved, is unchanged; the
    `promotion_name` override still wins over whichever `precedent_name`
    results.
  - `intent_learning_candidates.json` and `trellis worker curate --format
    json` now carry a `promotion_ready` / `learning_promotion_ready` digest:
    `count` of `promote_guidance` candidates with `helpful_count >=
    LEARNING_PROMOTION_READY_MIN_HELPFUL_COUNT` (`1`) and
    `unhelpful_count == 0`, plus `top` (up to 5, ranked by `helpful_count`
    desc, then `success_rate` desc, then `times_served` desc). Text output
    prints `"N candidate(s) promotion-ready — review with trellis curate
    promote-learning or the Review tab"` when `N > 0`, singular at `N == 1`.
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
- **`TrellisClient`/`AsyncTrellisClient` take `api_key=`.** Against a
  server running `TRELLIS_AUTH_MODE=required`, SDK calls failed with a 401
  `TrellisClientError` unless the caller injected
  `http=httpx.Client(headers=...)`. Both clients now take a keyword-only
  `api_key=`, sent as `Authorization: Bearer` on every request including
  the version handshake (the server also accepts `X-API-Key`, on the same
  scopes; `httpx` masks only `Authorization` in a header repr). Nothing is
  read from the environment. An empty `api_key=`, or one beside an
  injected `http=`, raises `ValueError`.
  (follow-up from the [#804](https://github.com/ronsse/trellis-ai/pull/804) gate)

### Fixed

- **Class-prior chunk scope and replay: a caller-named `#chunk-` id is no
  chunk, a chunk whose row is gone still resolves, and a pointer replays at
  its recorded charge (#1001).** `is_chunk_doc_id` was a substring test, so a
  document a caller named `notes#chunk-intro` read as a chunk of `notes` and
  the `parents_and_chunks` scope withheld it by `notes`'s form. It now matches
  the shape ingest mints (`chunk_doc_id`: `<parent>#chunk-<digits>`, split at
  the last separator), and a new `chunk_parent_id` gives the parent for the
  same shape, so `parent_id_of`'s id-prefix fallback agrees with the
  predicate. The store-level `LIKE '%#chunk-%'` exclusion is unchanged (SQL
  cannot test digits), so a caller-named `notes#chunk-intro` is still hidden
  by the SQL surfaces (API search and explore, `count(include_chunks=False)`,
  the MCP fuzzy-dedup seed) while file-context `docs_by_path` now lists it and
  `sync --prune` now considers it. The judged-outcomes join, which split on
  the first separator, now uses `chunk_parent_id`, so it and serving read
  `notes#chunk-intro` and `x#chunk-5#chunk-0` alike, and redacting a
  caller-named target cascades to its own minted chunks. `analyze replay --class-prior` resolved a chunk whose own row had
  been deleted as `unresolved` without trying its parent; serving never needed
  that row, only the parent's, so replay now falls back to the id prefix the
  same way and counts it resolved when the parent carries a form (a chunk
  whose parent is also gone stays unresolved). `analyze replay` also priced a
  row the pack served as a disclosure pointer from the character estimate,
  which runs about two tokens over the charge the pack paid, so a policy
  matching the served disclosure read as a small saving (`token_delta`
  0.007 to 0.035 on a disclosed pack); a row served as a pointer is now
  replayed at its recorded tokens, and only a row the policy newly demotes is
  estimated. A null `ReplayPolicy` still prices every row as a body at the
  walk charge, as before. #1001's scope-stamp box was already satisfied by
  #990: the resolved scope, whether from config or `TRELLIS_CLASS_PRIOR_SCOPE`,
  is appended to the replay's `applied_config` whenever it is not the default.

- **A promoted precedent's document row is no longer rewritten by
  `worker enrich`, and `retention.restore` will not un-archive a redaction's
  blank row (#957).** `worker enrich` selected a precedent's row like any
  other document, so its LLM pass could merge facets into it or overwrite the
  `decision` `content_type` the promotion stamped; the candidate selection now
  skips every id in `precedent://learning/` (the same id predicate the noise
  pass and classify refresh use), with or without `--reenrich`. Separately,
  `redaction.apply` blanks a document row and then purges its node, and
  `retention.restore` read the document store first, so it re-stamped that
  empty archived row `current`, either beside a deleted node (a redacted
  precedent) or beside a live one (a redaction whose purge failed, which is
  re-runnable). Two refusals now ride the existing `refused` path, so each is
  named in `refused_items`, the `RETENTION_RESTORED` event and
  `CommandResult.warnings`, and the rest of the batch still restores:
  `retention_restore_precedent_node_missing` (a namespace row archived with no
  node) and `retention_restore_redaction_tombstone` (an archived row whose
  `deprecation_reason` is `redacted` *and* whose content is empty, in or out of
  the namespace; the reason alone is caller-chosen text and does not qualify).
  A batch restoring nothing rejects with the one refusal code present, or
  `retention_restore_nothing_restored` when the codes differ; the summary line
  now names the codes in place of "refused (history unavailable)". The
  `classify.shadow`, `domain_normalization` and `tag_evolution` loops were
  audited and left alone: shadow overlays only `content_tags_shadow`, which is
  never served, and the other two write events, not documents.

- **A refused precedent supersede is `target_protected`, not a `stale_recheck`
  or a missing target (#994).** `mark_document_superseded` returns the reason
  it did not apply (`None` when it did) instead of a bool, and a protected
  `precedent://learning/...` target now reports `target_protected` where it
  used to read as a vanished one. The successor's `reconciliation` marker and
  the `RECONCILE_DEGRADED` event both say so, and the marker is not
  `stale_recheck` because a re-judge would be refused again. The session
  capture sweep maps it to `supersede_target_protected` rather than
  `supersede_target_missing`. The reason is not a judge failure
  (`judge_failure=false`), and a protected target still emits no
  `MEMORY_OP_JUDGED`: that stream pairs a verdict with its outcome, and a
  verdict that never took effect has none. `precedent_sync_health` also
  breaks a same-microsecond tie toward the failure, lists a failed reinstate
  older than a capped `precedent.demoted` read as `uncertain` rather than
  unresolved (the dropped demote may have superseded it), and its vector
  warning now says semantic retrieval withholds the demoted snapshot, which is
  what the collect seam does, not that it serves it. A test id now carries
  markup so the escaped-id CLI test discriminates.

- **`promote-learning`: re-running a file after a refused run no longer
  re-promotes the rows that landed (#983).** A mixed run exits `2` /
  `refused` and the next move is to fix the bad row and run the same file
  again, but `submit_learning_promotion` refused re-approval only for a
  precedent that was not current, so the second run wrote each landed row a
  second time: a new SCD-2 version at the same id and a second
  `PRECEDENT_PROMOTED` event. A row whose precedent is current, complete and
  unchanged is now reported `already_promoted` and writes nothing, in
  `promote-learning`, `POST /learning/promotions` and `auto-promote-candidates`.
  It counts as satisfied for the verdict, so a corrected file exits `0`, and
  is carried in a new `already_promoted_count` (CLI JSON, REST response,
  review-decision event detail; `docs/api/v1.yaml` regenerated) apart from
  `promoted_count`. A `promoted_incomplete` row and a row with changed
  content still write. A re-run skips a row whose payload keys still match,
  so a key since dropped from `precedent_properties` is not removed by a
  re-run, on purpose: a key a later writer added, a lifecycle stamp say, reads
  the same as a dropped one, and that is not content the promotion owns
  ([#996](https://github.com/ronsse/trellis-ai/issues/996)).
  `review-ready --approve` keeps refusing an existing
  precedent as `refused_already_promoted`. Two smaller fixes ride with it:
  `review-ready --approve ""` could raise `IndexError` when a malformed
  artifact held a ready row with a blank `candidate_id`, and is now refused as
  `refused_not_ready`; and the `--dry-run` note no longer says a row "was
  skipped" or that a skipped row "makes the run refused" about a run that
  did not happen, while the text header divides by `named_count` (the Review
  toast's denominator) and not `approved_count`.

- **`analyze replay`: the as-served baseline counts a disclosure pointer as a
  pointer (#978).** `replay_pack_value` marked every included row of the
  baseline arm a body, so a pack that graduated disclosure had demoted
  reported `pointer_items_served=0` with all of its rows under
  `body_items_served`, and a cited-helpful row served only as a pointer added
  its pointer tokens to `helpful_tokens`, against the arm's own rule that a
  pointer delivers an id, not substance. The baseline now reads
  `disclosure.pointer_item_ids`, which every flat build has written since
  #359, so the two arms agree on what a pointer is. On a window that cites
  pointers as helpful the baseline's `useful_token_fraction` is lower than it
  read, so `fraction_delta` reads higher against it. An absent, empty or
  non-list block reads as no pointers, as before. Not changed: a window
  spanning #976's deploy still mixes the two disclosed-pack pricings (see the
  `walk_tokens` entry).

- **`analyze replay`: "What the policy cost" is a delta against the baseline
  it prints above it, and index-mode packs read as pointers (#993).**
  `helpful_bodies_withheld` counted every cited-helpful serving the
  counterfactual carried as a pointer, so replaying a disclosed pack under the
  policy it was served with (`--body-items 12` on a 14-row pack whose last two
  rows were pointers, one cited helpful) printed `body withheld 1/2` beneath two
  arms that read the same. It now counts a helpful serving only when the
  as-served pack delivered it as a body and the policy turns it into a pointer,
  as its docstring always said, so that replay reads `0`. The new
  `helpful_bodies_served` (additive, in `--format json`) is the denominator the
  CLI line now prints, the helpful servings the baseline delivered as a body;
  `helpful_items_total` keeps counting every cited-helpful serving, body or
  pointer, because `helpful_items_dropped` is measured against it and a dropped
  pointer is an id that is no longer addressable. The `notes` text for a
  withheld body now says the same. An index-mode pack records `disclosure`
  `mode='off'` with no `pointer_item_ids`, so the baseline read every row as a
  body while `feedback.attribution` reads the pack as all pointers; the replay
  now takes attribution's rule (`index_mode` is `true` on the event), and the
  counterfactual leaves such a pack all pointers and exempt from graduation, as
  the builder does, so a null policy still changes nothing. On a window with
  index packs the baseline's `useful_token_fraction` drops by their helpful
  tokens, which were counted as bodies. The replay baseline therefore no longer
  matches `analyze value` on the same window: both judge the same servings, but
  `analyze value` still credits a cited pointer at its pointer cost.
  `--excerpt-max-chars` no longer re-prices an index pack's rows, because an
  index line carries no excerpt to cut; before, a cap below the line width
  reported a saving the builder would not make. Pointer exclusion from
  `unhelpful_tokens`, introduced with #978 and unchanged here, is now pinned by
  a test: a pointer cited unhelpful moves from `unhelpful_tokens` to
  `unjudged_tokens`, because the caller never saw its excerpt. A window
  fixture with a non-default `DisclosureConfig(body_items=...)` and one whose
  pointers are not the trailing ranks now pin that the baseline reads pointer
  ids from the event rather than assuming the default cut at rank 12.

- **`axes.graph` / `PACK_ASSEMBLED.graph_axis_state` read `absent` when the
  builder has no graph strategy (#985).** `resolve_graph_axis_state` returned
  `ran` for a builder that never had the axis, so a reader could not tell
  "ran and found nothing" from "never configured". The vocabulary is now
  `ran`, `failed`, `unseeded_fallback_off` and `absent`; an absent axis is in
  neither `axes.ran` nor `axes.failed`. No shipped surface is affected: every
  one builds through `builder_factory`, which always includes the graph
  strategy, so no stored row reads differently. `AxisReportResponse.graph`
  stays a free string (default `ran` for an older server's block), the
  REST and CLI JSON forward the value unchanged, and no reader switches on
  the old three-value set, so `docs/api/v1.yaml` is unchanged.

- **`POST /api/v1/learning/promotions` states the verdict `promote-learning`
  states.** `LearningPromotionResponse.status` was always the default `"ok"`,
  even when every ready row was refused or failed to land, and the response
  had no `named_count`, so the Review tab divided by `approved_count` and read
  an approved row with a blank `candidate_id` as a promotion that fell short.
  `status` is now `"ok"`, `"refused"` or `"failed"` from
  `trellis.learning.scoring.promotion_verdict`, the function the CLI now maps
  to its exit code (rules and exit codes unchanged), so the two surfaces
  cannot disagree about one run. `named_count` (additive, default `0`) is the
  approved rows that name a candidate, and is in the
  `REVIEW_DECISION_RECORDED` detail. The HTTP status stays 200 for every
  verdict. Behaviour change: `promoted_count` and each row's `status` now
  apply the CLI's landed check, so a promotion whose edges or precedent event
  did not land reads `promoted_incomplete` and is not counted, where the route
  used to count it as `promoted`. The Review tab divides by `named_count` and
  shows the toast as an error when `status` is not `"ok"`.

- **Stored metadata can no longer relabel the strategy that collected an item.**
  The keyword, semantic and graph strategies built item metadata as
  `{"source_strategy": <name>, **stored}`, so a stored `source_strategy` key
  overrode the collecting strategy, and with it the class-prior axis that
  judges the item and `strategy_source` in `PACK_ASSEMBLED.injected_items[]`
  (telemetry, RRF and feedback attribution). The stamp now follows the splat,
  as `recency_clock` does. No writer stores the key, so served packs are
  unchanged; the key now sits after the stored keys in the item's metadata
  rather than before them. The observation strategy does not splat stored
  properties and was not exposed. (#943)

- **A mined precedent candidate no longer reads as a promoted precedent
  ([#925](https://github.com/ronsse/trellis-ai/issues/925)).** `PrecedentMiner`
  wrote each candidate it mined as `PRECEDENT_PROMOTED`, so `get_lessons`,
  `GET /precedents` and `trellis retrieve precedents` would have served an
  ungoverned, unapproved row as a promoted lesson. It now emits a new
  `PRECEDENT_CANDIDATE_MINED` event (`precedent.candidate_mined`) on both its
  paths. The readers that mean "a promoted precedent" — `list_precedents`,
  `GET /precedents/domains`, the demote handler's "is this a real precedent"
  check and the loop-health promotion count — also skip the rows an earlier
  miner already wrote, through `is_miner_candidate_event` (`source ==
  "precedent_miner"` or `action == "precedent_candidate_generated"`). The
  governed `precedent.promote` event is unchanged. Candidates still bypass
  `MutationExecutor` and nothing routes them into the Review queue yet.

- **A pack records its graph seeds once, not once per graph item.** A seeded
  `GraphSearch` stamps the whole seed list on every item it returns, and the
  stamp stayed there: `POST /api/v1/packs`, `POST /api/v1/packs/sectioned` and
  the SDK repeated it on each graph item. `PackBuilder` now reads the seeds
  into `PACK_ASSEMBLED.seed_entity_ids` (same values and order as before, `[]`
  when unseeded, flat packs only) and then serves each graph item without
  `metadata["graph_seed_ids"]`, on the flat and the sectioned path alike. The
  items a strategy returned are copied, not edited. Seeds are still read
  before dedup and the budget cut, so a pack whose graph items were all cut
  there records them, as it did; a document's own metadata of the same name is
  left alone.

- **`--dry-run` on `admin migrate-graph` and `analyze advisory-effectiveness`
  no longer writes a meta-trace Activity.** `wrap_cli_meta_analysis` writes
  an Agent node, an Activity node and an edge to the CLI's default graph the
  moment it is entered, so a dry run that promised to write nothing still grew
  the live graph. The wrapper takes a keyword-only `dry_run` and yields the
  same no-op record as `disabled`, logging `cli_meta_trace.skipped_for_dry_run`;
  `migrate-graph`, `migrate-provenance` and `advisory-effectiveness` pass it.
  A real run records exactly what it did before. `worker curate --dry-run` is
  unchanged on purpose: each stage that runs still records its Activity, as
  its help text says (`--no-meta-trace` skips it). A new rule test fails any
  function with a `dry_run` parameter that calls the wrapper without stating
  the flag.

- **`trellis admin migrate-graph` carries an edge's five typed provenance
  columns.** `source_trace_id`, `agent_id`, `confidence`, `evidence_ref` and
  `extractor_tier` are columns, not `properties` keys, and the migrator
  copied only `properties`: a backend copy reported every edge written and
  left all five NULL on the destination, with no error. That loss is silent
  once `migrate-provenance` has lifted the values out of `properties`. The
  migrator now passes the columns the source row holds through the
  destination's own `upsert_edge`, which validates and normalises them. An
  edge with no provenance copies as before, and one whose provenance is
  still inside `properties` copies unchanged (lifting it stays
  `migrate-provenance`'s job). A destination backend whose `upsert_edge`
  cannot take the keywords now fails the step loudly for an edge that
  carries provenance instead of dropping it. Not carried, by design:
  `edge_id`, `alias_id`, `created_at` and `valid_from` are minted by the
  destination, and history is current-versions only. An edge already on
  the destination is skipped whole, so a re-run does not backfill
  provenance onto an edge an earlier copy wrote without it.

- **`migrate-graph` copies an alias's `match_confidence` of `0.0`, and
  `GraphStore.upsert_edge` declares its provenance keywords.** The alias
  copy read the stored confidence with `or 1.0`, so a stored `0.0` arrived
  as `1.0`; only a missing value now falls back to `1.0`. The abstract
  `GraphStore.upsert_edge` and the `upsert_edges_bulk` docstring omitted
  the five provenance keywords (`source_trace_id`, `agent_id`,
  `confidence`, `evidence_ref`, `extractor_tier`) that the SQLite, Postgres
  and Bolt-family backends all accept; the abstract signature now lists them as
  keyword-only, default `None`. No backend's behaviour changes. A
  third-party `GraphStore` plugin whose `upsert_edge` still lacks them now
  fails a type check against the ABC (the contract change `adr-plugin-contract`
  routes through plugin CI); the migrator still copies provenance-free edges
  through such a plugin, since it passes only the keywords an edge holds.

- **A destructive mutation now requires the `admin` scope at both generic
  mutation surfaces, not just `mutate`.** `POST /api/v1/commands/batch` and
  the MCP tool `execute_mutation` previously accepted any `Operation` —
  including entity merge, redaction, retention prune, alias rebind,
  link/label removal, and schedule/settings/classify-rule writes — from a
  caller holding only `mutate`, the scope an ordinary agent key carries.
  Both surfaces now refuse any operation outside
  `trellis.mutate.NON_DESTRUCTIVE_OPERATIONS` unless the caller holds
  `admin`: gating on membership in that explicit allow-list (rather than on
  the `DESTRUCTIVE_OPERATIONS` deny-list) means a future `Operation` member
  nobody has classified yet is refused by default instead of silently
  allowed. Over REST, a batch containing any such operation is refused
  whole with HTTP 403 before any command executes, naming every refused
  operation. Over MCP, the refusal reads the FastMCP access token's scopes
  when the server is running over HTTP, and is allowed unconditionally on
  stdio, the trusted local transport with no token to read; the refusal is
  the tool's ordinary error reply, never a fabricated success. A command's
  `requested_by`/`actor` field is unchanged by this fix — it stays a
  caller-chosen audit label, not a verified identity, whether the caller
  holds `mutate` or `admin`.
- **The pack-telemetry REST routes now require the `admin` scope, not
  `read`.** `GET /api/v1/events`, `GET /api/v1/events/{event_id}`,
  `GET /api/v1/packs`, `GET /api/v1/packs/{pack_id}`, `GET
  /api/v1/analyze` and `GET /api/v1/analyze/{report}` replay other
  callers' pack telemetry — intents, served item ids and titles,
  attribution — which a `read` key (what an agent or connector holds)
  should not be able to read back wholesale. `/documents`,
  `/documents/{doc_id}` and `/graph/history` are unaffected and stay
  `read`. The `explore` router's pack-telemetry routes carry their own
  route-level requirement (it is a mixed-scope router); the `analyze`
  router, which is pack-telemetry end to end, flips at the
  `include_router` call. With every reachable caller of these routes
  now already holding `admin`, the pack-holdout blinding they used to
  apply to non-admin callers is dead code and has been removed; no
  route that stays `read` had its holdout blinding touched. The static
  UI badges the affected tabs (Events, Packs, Reports) as needing an
  admin-scoped key, the same way it already does for Settings.

- **A non-loopback REST bind without required auth now refuses to start
  (#189).** `trellis serve` and `trellis admin serve` both bound any host
  with no check beyond a startup log line — `TRELLIS_AUTH_MODE=off` (or
  unset with no legacy `TRELLIS_API_KEY`), and `optional` with no
  credential presented, both grant every scope to anyone who can reach the
  port, and the shipped `Dockerfile` binds `0.0.0.0` by default. Both entry
  points now call `trellis_api.auth.check_bind_is_safe` — the same
  loopback-detection and fail-closed shape `trellis.mcp.auth.resolve_http_settings`
  already uses for the MCP HTTP transport — before `uvicorn.run`; a refused
  bind is a `ConfigError` (`EXIT_STORE`) naming both ways out: set
  `TRELLIS_AUTH_MODE=required` and mint a key with `trellis admin
  api-keys create`, or set `TRELLIS_API_ALLOW_INSECURE_BIND=1` when the
  listener is genuinely on a trusted interface. `admin serve` now delegates
  to the same `trellis_api.app.main` entry point `serve` uses, so there is
  exactly one call to `uvicorn.run` for the REST API
  (`tests/unit/test_rest_bind_rule.py`). The local `docker-compose.yml`
  rehearsal stack sets the override and now publishes both the API and
  Postgres ports on `127.0.0.1` only; before, its bare `"8420:8420"` and
  `"5432:5432"` mappings published an unauthenticated API and a
  fixed-password database on every host interface.

- **`retention.restore` re-syncs a restored graph node's vector row, and a
  REJECTED batch now names the whole batch.** A graph-node restore (used
  for, among others, a demoted-then-pruned precedent) rewrote the node
  but never re-stamped its vector row, unlike `precedent.demote` /
  `precedent.reinstate`, which always do — a restored precedent's row
  could keep reading `archived` and stay withheld on the semantic
  retrieval path even though the node itself was current again: wrong,
  though it failed closed rather than open. The graph branch now calls
  the same vector-resync helper demote/reinstate use, and a sync failure
  surfaces as a result warning the same way theirs does; a document
  restore already had this for free via `put_document`'s metadata
  mirror, so it is unchanged. `RETENTION_RESTORED` is now documented as
  unconditional, including over a batch that restored nothing at all —
  nothing in the codebase reads the event's `restored` count, so there
  is no consumer to protect by suppressing it, and this keeps the same
  audit-completeness posture as `RETENTION_PRUNED`. The REJECTED message
  for a batch that restored nothing now names every skipped id alongside
  every refused one, not only the refused half.

- **UI P2 sweep (e174): tables, values, time, naming, accessibility,
  build footer.** Nine findings from a UI-quality review's P2 section
  ("consistency, clarity, enterprise hygiene"), re-located against
  current `main` since several had already moved or been fixed by
  #849-#852/#855:
  - **Tables.** Traces, Events and Packs share one cursor-pager family
    (`makeCursorPager`/`renderCursorPagerFooter`) with real "N of total
    &middot; newer/older" footers; Memories' existing offset-based
    pagination already satisfied this and was left alone. A Trace,
    Document or Pack detail miss names the id instead of rendering blank;
    an Event miss (paged by cursor window, not id lookup) also names the
    loaded window. The Traces domain filter is now populated from a
    server-side `GET /traces/domains` count over the whole store, not
    just the currently-loaded page.
  - **Values.** Swept the remaining `|| '-'`/`|| 0` fallbacks that were
    conflating a legitimate zero with "missing"; the sole surviving
    `|| '-'` is a string-join fallback, not a numeric one. `/packs`
    summaries gain a `sectioned` field so a sectioned pack's attribution
    badge reads "unattributable (sectioned)" instead of implying a zero
    outcome, and `get_pack` gains `holdout_blinded` so the UI can say
    *why* a blinded holdout payload reads short.
  - **Time.** `fmtDate`/`fmtUtc`/`fmtDateUtc` collapsed into one `fmtTime`
    that always renders in UTC and labels it; `fmtTimeCell` pairs a
    relative "3h ago" with the raw ISO instant as its `title`.
  - **Build footer.** The sidebar now reads "API build" (not "Backend")
    and surfaces `stamp_stale` / `fallback-version` / `commit: null` from
    `GET /api/version` as an honest warning instead of a normal version.
  - **Toasts.** A dedicated `#toast-region[aria-live="polite"]` replaces
    the previous reuse of the static `#error` banner; a terminal-action
    toast pauses on hover/focus and carries an explicit dismiss button.
  - **Naming.** The "Evolution" tab is now "Effectiveness"; "self-learning"
    is never used.
  - **Help text.** Added one-line `.field-hint` descriptions for the
    Metrics `days`/`group_by` controls and the Graph Explorer's
    depth/sort controls. The Dashboard stat cards already had per-card
    descriptions via `renderCard`'s `sub` param, and the Review tab has
    no filter controls to describe — both sub-items were already
    satisfied or do not apply to current code, not re-implemented.
  - **Accessibility.** 18 previously-unlabeled `<select>`/`<input>`
    elements got `aria-label`s; the four clickable `<tr>` row templates
    (Traces/Memories/Events/Packs) are now `tabindex="0" role="button"`
    with a keyboard handler for Enter/Space; the "Why no approve?"
    explanations are now visible text, not only a hover `title`; every
    icon-only button (API-key toggle, value-copy, row-expand, the five
    graph-toolbar buttons) carries an `aria-label`; the graph canvas gets
    `role="img"` plus a `aria-describedby` text alternative; the
    search-results-panel's close control is a real `<button>`, not a
    `<span>` with an `onclick`; a `/stats` or `/health` fetch failure on
    the Dashboard now renders inside the card that failed instead of the
    shared `#error` line. The key-banner close control was already a
    `<button>` from a prior merged PR — no change needed there.
  - **Welcome banner.** Unchanged this sweep — already gated on a green
    `/readyz` and zero counts together, from a prior fix.
  New failing-before tests in `tests/unit/api/test_ui_p2_sweep.py` pin
  every item above that wasn't already covered.

### Changed

- **`save_experience`'s tool description says a step's `result` is an object.**
  `trellis analyze health` reported the same boundary rejection recurring
  (`type` at `steps[].result`, 14 times in 7 days): agents wrote a bare string
  where `TraceStep.result` takes a dict. The skill that teaches the trace shape
  already said so; the MCP tool description, the only guide an agent without
  that skill sees, did not. The schema is unchanged.

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

- **The Review tab and its empty states stop hiding why.** Five defects in
  `src/trellis_api/static/index.html` where the admin console read a failure
  or an absence as "nothing pending," per the UI-quality review's P1 section:
  - A tuner proposal card now shows `status`, `tool_name` and the
    reachability verdict `/proposals` already computes; an unreachable
    proposal's Approve control is disabled with the reason in its `title`
    instead of inviting a click `promote_proposal()` would refuse. Reject —
    terminal, unlike a recoverable bootstrap refusal — now routes through an
    in-page two-step confirm instead of calling it directly.
  - A fetch failure on any of the four Review-queue count badges
    (proposals, learning, schema, code) used to write a literal `0` via
    `setCount`, reading identically to an honest empty result. A new
    `setCountError` renders a distinct "!" marker with the failure in
    `title`, and `setCount` clears that state on a subsequent clean load.
  - `GET /learning/candidates` answering 200 with `status: "error"` (the
    artifact is missing or unreadable) never reached the page's `catch`
    block and rendered as "no candidates found." It now renders as an error
    with the server's own `hint`. The artifact's `generated_at_utc` is shown
    through a new `fmtUtc()`, labelled UTC, instead of `fmtDate()`'s silent
    `toLocaleString()` conversion to the browser's zone.
  - The precedents empty state said "Promote traces to create precedents,"
    implying any trace promotion sufficed. It now names the actual path (a
    learning candidate approved via Review → Learning, or `trellis curate
    promote-learning`) and says plainly that nothing schedules that
    promotion automatically today.
  - `/effectiveness` with zero feedback rendered a "0.0%" success rate,
    reading as "every pack failed" rather than "nothing was measured"; it
    now renders "not measured". `noise_candidates` is the usage-rule's
    *proposal* (see `EffectivenessReport`'s docstring), not a demotion, so
    the section is relabelled "Usage-Rule Proposals" and reports how many of
    those proposals `demotion_screen.admitted` — the evidence gate's
    verdict — actually cleared, without claiming any of them were tagged
    (this view never calls the write path).
- **`GET /api/v1/health` reports real store status; the Memories table
  reads the tags a tagging pipeline actually writes.** Two defects found
  in a UI quality review (P0):
  - `health()` returned a literal `{"api": True, "stores": True}`
    regardless of backend state — the dashboard's Store Health card read
    green even while `/readyz` (which really probes) read 503. The probe
    logic in `readyz` is now factored into `probe_backends` /
    `overall_backend_status` (`src/trellis_api/routes/health.py`), and
    `health()` calls the same functions, so the two surfaces can no
    longer disagree. `HealthResponse` gains an additive, optional
    `backends` field (per-backend status/latency/error) alongside the
    existing `checks` bool map, which keeps the response shape backward
    compatible for its two prior readers
    (`tests/unit/api/test_routes.py`,
    `tests/integration/api/test_live_smoke.py`). The dashboard's store
    health rows now render each backend's latency or error string.
  - The Memories table's `tagChips(metadata)` read `metadata.tags`, a key
    no writer sets; the tagging pipeline writes the 4 retrieval-shaping
    facets to `metadata["content_tags"]` (`ContentTags` in
    `trellis.schemas.classification`), so the column was empty on every
    row regardless of whether tagging ran. `tagChips` now reads
    `content_tags` and renders only the facets a row actually has;
    `content_tags_shadow` (LLM shadow-mode proposals never applied — see
    `trellis.retrieve.servable`) is never rendered as if it were an
    applied tag. The column header is renamed "Content tags" per
    [`adr-terminology.md`](docs/design/adr-terminology.md).

- **`trellis analyze learning-candidates` text output no longer crashes on
  an unmeasured retry rate.** The "Candidates by Recommendation" table
  formatted `metrics["retry_rate"]` with `:.1%` unconditionally;
  `scoring.py` legitimately returns `retry_rate=None` whenever no feedback
  event in the window reported `had_retry` at all (a sparse-signal fact,
  not a measured zero — see the `metrics_coverage` comments in
  `src/trellis/learning/scoring.py`), so any non-empty, text-mode run
  without `had_retry` coverage raised `TypeError: unsupported format
  string passed to NoneType.__format__`. The cell now renders `"n/a"`
  instead. A swept check of every other `{x:.Nf}`-style format call in
  `analyze.py` found no other unguarded nullable metric; `injection_rate`
  and `avg_selection_efficiency` (also nullable in `scoring.py`) are not
  currently rendered by any text surface, so there was nothing else to fix
  there.

- **The flat `get_context` / `search` paths render advisories.**
  `_flat_context` built `pack.advisories` via `PackBuilder._select_advisories`
  on every call but never rendered them — only `_sectioned_context` called
  `format_advisories_as_markdown`. Flat is the path agents actually use (37
  flat packs served in production vs. 0 sectioned, per the decision-ledger
  measurement), so advisories were mined nightly and attached to packs an
  agent could never see, while the advisory-fitness loop
  (`analyze_advisory_effectiveness`) still read `PACK_ASSEMBLED.advisory_ids`
  off those same flat packs as "presented". `_flat_context` now calls
  `format_advisories_as_markdown` in the same position `_sectioned_context`
  does — after the items and the cite footer — on both the
  non-empty and empty-pack branches (an empty-item pack can still carry
  advisories; only the pack-effect holdout zeroes both together), and in
  both full and `index=True` rendering. An empty advisory list renders
  nothing, so output is unchanged for every pack that has none. Decided and
  approved as decision-ledger D-4, 2026-10-10. While the pack holdout is on,
  an item-less pack now carries no advisories on either shape too
  (`PackBuilder._blind_advisories_for_empty_pack`), so a naturally empty
  pack and a withheld one still look alike instead of the advisory block
  becoming the one tell that gave a holdout draw away.
  ([#392](https://github.com/ronsse/trellis-ai/issues/392),
  [#844](https://github.com/ronsse/trellis-ai/pull/844))

- **Stop serving advisories minted before the #394 generator repair.**
  #844 rendered advisories on the flat path but shipped decision-ledger D-4's
  option A (render everything matching), not the option B the owner
  approved: the code made no attempt to distinguish pre-/post-#394 rows, so
  the 51 known-degenerate legacy rows measured live on 2026-10-10 (vs. 361
  post-repair, zero mismatches against the alternative id-prefix
  discriminator) were eligible to ride every pack. `_select_advisories` now
  drops any advisory whose `evidence.evidence_confidence is None` — the
  field only the repaired generator populates — before ranking and the
  delivery cap run, on both pack shapes, so a legacy row can never occupy a
  rank or cap slot a post-repair row would have won. Reversible via
  `TRELLIS_FILTER_LEGACY_ADVISORIES` (default on, same accepted-value
  vocabulary as `TRELLIS_GRAPH_SEEDING`). `PACK_ASSEMBLED` carries the
  withheld count as `advisories_filtered_legacy` (`0` when the knob is
  off), counted before `advisories_matched`, so the advisory-fitness loop
  never counts a withheld row as a presentation. Clearing the 51 legacy
  rows from the live store is a separate, operator-only live-store
  mutation, unchanged by this fix.
  ([decision-ledger D-4](docs/design/decision-ledger.md#d-4--should-the-flat-pack-path-render-advisories-at-all--panel-split))

- **Noise demotion counts what was written, not what the evidence gate
  admitted.** `apply_noise_tags` writes `signal_quality="noise"` only to
  ids that resolve in the document store; the demotion gate admits
  candidates on citation evidence alone, with no notion of which store an
  id belongs to, so an admitted trace id (or other non-document id)
  reached the writer and nothing was written for it, silently (one
  `logger.debug` per id). Curate's nightly `noise_tagged`, REST
  `POST /effectiveness/apply-noise-tags`'s `noise_candidates_tagged`, and
  CLI `trellis analyze apply-noise-tags`'s text output each independently
  reported the admission count as the demotion count, overstating it by
  the non-document remainder. `apply_noise_tags` now returns a
  `NoiseTagResult` (`updated`, `refused_not_document`) instead of a bare
  `int`; `EffectivenessReport` carries `noise_tags_written` /
  `noise_refused_not_document`. REST and the CLI report the real write
  count beside the refused ids by name (`noise_refused_not_document` /
  `noise_candidates_refused_not_document`); curate reports the write
  count beside only a refused *count* (`noise_refused_non_document`), not
  names. A curate dry run, which writes nothing either way, previews the
  same split with a read-only `document_store.get` check per admitted
  id, and curate's `NoiseTagsApplied` finding now fires on the write
  count rather than the admission count.
  ([#833](https://github.com/ronsse/trellis-ai/pull/833))
- **Every remaining `json.dumps` write path in the graph, document,
  vector, outcome, tuner-state, parameter and blob stores now refuses
  NaN/Infinity too, closing the gap #831 (above) left open.** The
  advisory and policy JSON files (`DegradableJsonStore`) are unchanged
  and still write a bare `NaN`/`Infinity` token. That PR fixed
  the event logs and the SQLite/Postgres graph stores; the ArcadeDB/Neo4j
  graph store (`bolt_opencypher/graph.py`, 11 call sites across
  `upsert_node`, `update_node_if_current`, `upsert_nodes_bulk`,
  `upsert_edge` and `upsert_edges_bulk`), the SQLite and Postgres document
  stores, every vector store's metadata write (SQLite, pgvector, Neo4j —
  `upsert` and the separately-implemented `upsert_bulk` are two distinct
  call sites there — and ArcadeDB), the outcome/tuner-state/parameter
  stores, and the local blob store's metadata sidecar all still wrote a
  non-finite float as a silent `NaN`/`Infinity` JSON token. The fix is the
  same `allow_nan=False` on each `json.dumps` call, and the same
  `ValueError` raised before the write lands. Two gaps this also closes:
  (1) MCP `save_memory` previously committed a document row with NaN
  metadata durably *before* the later `MEMORY_STORED` event emit failed,
  leaving an orphan a caller's error made look recoverable — fixing
  `sqlite/document.py`'s `put()` means the write itself refuses first, so
  nothing durable lands; (2) the SQLite graph store's
  `upsert_nodes_bulk`/`upsert_edges_bulk` already rolled back a
  non-finite row atomically before this PR (both build every `json.dumps`
  call before any write statement runs), but had no test pinning a
  two-row batch where an earlier row has a prior version and a later row
  is non-finite — added, and confirmed non-vacuous by mutating the store
  to commit each row as it's built (which the test catches: the prior
  row's value leaks through).
  ([#835](https://github.com/ronsse/trellis-ai/pull/835))
- **A failed idempotency read, and a store failure a Postgres or Bolt
  backend wraps as `StoreError ... from exc`, audited only the
  exception's type name; both now audit the actual error, one level of
  wrapping included.** Before this change, most failure paths already
  stored the real error: the untyped panic catch stored a driver's raw
  `str(exc)` (sometimes multi-line, with query text and row values on
  later lines), and a Trellis-composed message (`StoreError("backend
  down")`, an orphan-edge refusal) stored that text as-is. Only two
  paths stored the type alone, which is what the owner's "not just the
  error type" asked to fix: the failed idempotency read
  (`"Idempotency check failed: OperationalError"`, naming neither
  "database is locked" nor a Postgres wrapper's own type), and a
  `StoreError`/`TrellisError` wrapping a driver error via `from exc`
  (`"Purge of node n1 failed: DeadlockDetected"` plus a constant
  `error_code="STORE_ERROR"`, discarding the driver's SQLSTATE,
  message and constraint that sat on `__cause__`).
  `error_sanitize.py` gains `summarize_exception(exc) -> dict`:
  `error_type` (`type(exc).__name__`), `error_code`
  (`_driver_error_code` — a psycopg `sqlstate`, then a sqlite3
  `sqlite_errorname`, then a `.code` attribute, then `errno`, first one
  present), `message` (a `TrellisError`'s own `.message`, run through
  `sanitize_error_message`; a generic exception's `str()` first line,
  masked with `_mask_quoted` and then also run through
  `sanitize_error_message`), and `constraint`
  (`exc.diag.constraint_name` when present). When `exc.__cause__` is
  set, the summary also nests one level of the same four fields for
  the cause, under `cause` — read from `__cause__` only, never the
  implicit `__context__` a bare `raise` inside an `except` block also
  sets. All four in-scope catches in `MutationExecutor.execute` — the
  idempotency-read failure, the `ValidationError` rejection, the typed
  `(StoreError, TrellisError)` failure, and the untyped panic — now
  build the stored `message` and the `error_type` / `error_code` /
  `constraint` / `cause` keys from this summary, added only when not
  `None`. The panic path's stored `message` itself changes: it was the
  driver's raw (possibly multi-line) text and is now the masked,
  sanitized first line only — a behavior change for any reader of
  existing `mutation.rejected` events, not an additive one.
  `CommandResult.message` — what callers see — is unchanged on every
  path; it stays type-only on the idempotency and panic paths.
  Mutation testing against `error_sanitize.py` and `executor.py`
  (36 hand-written mutants) now kills 31 of 36; the 5 remaining
  survivors were assessed as near-equivalent or, in one case
  (`CommandResult` built from the summary message), unpinnable without
  leaking raw credential text into the caller-facing message, and are
  left as a follow-up rather than pinned with a vacuous assertion.
  ([#837](https://github.com/ronsse/trellis-ai/pull/837))
- **The SQLite event log and both the SQLite and Postgres graph stores
  refuse a NaN/Infinity float at write time, instead of silently storing
  JSON text a stricter reader can't parse.** Python's `json.dumps` writes
  the non-standard `NaN` / `Infinity` / `-Infinity` tokens by default, and
  Python's own `json.loads` parses them back into the same float when a
  row is read back out, so a non-finite float in an event payload, event
  metadata, or a graph node/edge `properties` (or a curated node's
  `generation_spec`) round-tripped silently through SQLite's own read
  path — though not through SQLite's `json_extract`/`json_each`, used for
  filtering, which read a stored `NaN` back as `NULL` and `Infinity` as
  `9e999` — while Postgres's `jsonb` column already rejected it outright
  at write time: an inconsistency between backends, and invalid JSON by
  spec either way (the REST API's `json.dumps` on the way out, or any
  non-Python consumer, breaks on it). Every `json.dumps` call on a write
  path in `sqlite/event_log.py`, `postgres/event_log.py`,
  `sqlite/graph.py` and `postgres/graph.py` now passes `allow_nan=False`,
  matching the idiom already used in
  `tests/unit/learning/tuners/test_promotion_effect_size.py` to pin the
  Postgres behavior without a live server. The error is a `ValueError`
  raised before the INSERT executes; a version close already run in the
  same transaction — SQLite's and Postgres's `update_node_if_current`,
  and SQLite's `upsert_edges_bulk` — is rolled back along with it, so
  nothing is written either way. For Postgres this surfaces unwrapped
  (not a `StoreError`) via the same contract already pinned by
  `test_an_error_that_is_not_the_driver_s_keeps_its_type`, because a
  non-serializable value is the caller's bug, not the store's. One live
  producer did reach this path: `PrecedentMiner`
  (`trellis_workers/learning/miner.py`) read an LLM reply's `confidence`
  with a bare `float()`, which accepts both a JSON `NaN` token and the
  string `"nan"`, and the clamp that followed preserved it rather than
  rejecting it — unlike `compute_importance`'s clamp, which does. It now
  reads with `coerce_finite_float` and falls back to `0.5`, the same
  pattern `session_capture/distill.py` already uses for the same kind of
  field. Feedback `rating` (#741/#751), the tuner's `effect_size` (#620)
  and `Measurement.metric_value` (#827, below) were already closed at
  their producers before this PR; the miner was the one still open, and
  is now closed here too.
  ([#831](https://github.com/ronsse/trellis-ai/pull/831))

- **`Measurement.metric_value` refuses `Infinity` and `-Infinity`, not only
  `NaN`.** The field accepted any float that passed `math.isnan`, so a
  caller could record `Infinity`. `metric_value` now refuses `NaN`,
  `Infinity` and `-Infinity` alike with "metric_value must be a finite
  number" (422 at the REST boundary, `REJECTED` through
  `MutationExecutor`, before any node is written), because a non-finite
  value poisons downstream arithmetic — `inf - inf` or `0 * inf` is NaN,
  and `max()` over a series containing one always picks it — and cannot
  be stored by the Postgres event log, whose JSONB has no token for
  `Infinity`; the field docstring no longer calls Infinity an open
  question.
  ([#827](https://github.com/ronsse/trellis-ai/pull/827))
- **`trellis admin smoke-test` sends its resolved API key to `/readyz` and
  `/metrics`, not just `/api/v1/advisories`.** On an auth-required
  deployment, the readyz check previously went out with no credential, so
  `trellis_api.routes.health.readyz` withheld its per-backend breakdown
  (`backends` came back `None` even though the deployment was healthy), and
  a gated `/metrics` (`TRELLIS_METRICS_PUBLIC=false`) 401'd and read as a
  smoke-test bug rather than the deploy choice it was. Both checks now take
  the key resolved for `_check_auth_accepts_valid` and send `X-API-Key` when
  one resolves. If either `/readyz` or `/metrics` rejects that key (401) — a
  verdict `_check_auth_accepts_valid` already owns — the check re-probes
  once without the header so it's still answered (readiness, or a public
  `/metrics` under the default `TRELLIS_METRICS_PUBLIC` posture), and notes
  that the key was rejected rather than failing on a deploy choice that
  isn't actually broken. Whenever `/readyz`'s response body still carries no
  `backends` (no key sent, or the key was rejected), both the text and JSON
  output note "per-backend breakdown withheld (no API key)"; if a valid key
  was sent and `backends` is still absent, the note reads "per-backend
  breakdown absent" instead, since no API key isn't the cause.
  ([#826](https://github.com/ronsse/trellis-ai/pull/826))
- **A proposal with no comparable baseline is refused by default, and the
  refusal is recoverable.** `PromotionPolicy.allow_no_baseline` now
  defaults to `False`, so a bare `trellis metrics promote --commit` no
  longer promotes a scope's first, unbaselined proposal — the gap #823
  left open on the CLI after closing it on the Review queue. Pass the new
  `--allow-no-baseline` flag to opt in per call; `--force` keeps its
  existing, broader meaning and still skips the whole policy gate,
  baseline rule included. A no-baseline refusal no longer marks the
  proposal terminally `"rejected"`, so a later `--allow-no-baseline` call
  on the same proposal can still promote it, and `TUNER_PROPOSAL_REJECTED`
  now carries a `terminal` key reflecting that. The Review queue's
  "Confirm approve" button is disabled when the preview predicts a
  rejection and names the CLI bootstrap command.
  ([#823](https://github.com/ronsse/trellis-ai/pull/823) follow-up)
- **`TrellisError` raise sites describe the caught exception instead of
  quoting it.** Nine sites built their message as `f"...: {exc}"` inside an
  `except ... as exc:` handler — a pydantic `ValidationError`'s own text
  embeds `input_value=<the caller's field value>` (Measurement and
  Observation recording; a policy file's own entry), and an `OSError` /
  `json.JSONDecodeError` / `ImportError`'s text can carry more of a path,
  document or module than the raiser intended (`policy_source.py`,
  `registry.py`). The sharpest case: a rejected Measurement's message
  becomes the immutable `mutation.rejected` audit event verbatim, so the
  leak was permanent once written. `trellis.core.error_sanitize` gains four
  helpers — `describe_os_error`, `describe_json_error`,
  `describe_validation_error` (pydantic's `errors(include_input=False)`,
  never `str(exc)`), `describe_import_error` (a `ModuleNotFoundError` names
  the module that was not found; any other `ImportError` says the import
  from that module failed, since the module itself exists) — each naming
  only the exception's own structured fields, and the nine sites now build
  their message from one of these instead of the caught exception's
  `str()`. The eight REST routes that construct an `HTTPException` detail
  or response field directly from a `CommandResult.message` or a caught
  exception's `.message` — bypassing the `trellis_error_handler` middleware
  that already sanitizes an uncaught `TrellisError` — now wrap that value in
  `sanitize_error_message` too, which masks secret-shaped, SQL-shaped and
  long-token text; it is a deny-list, not a guarantee, so a handler that
  has not yet adopted a describe helper can still leak text that matches
  none of those shapes. The secret-pattern deny-list in
  `sanitize_error_message` now also matches a credential key wrapped in
  quotes (`"api_key": "..."`), the shape a JSON body or a repr'd mapping
  takes, which the unquoted `key\s*[=:]` pattern missed entirely. A new AST
  rule (`tests/unit/test_error_describe_not_quote_rule.py`, built on
  `tests/ast_rules.py`) flags any `TrellisError`-family raise inside an
  `except ... as name:` handler that interpolates `name` — or a local
  variable transitively bound from it — into its message by f-string,
  `str()`/`repr()`, `.format()` or `%`-formatting, and resolves the
  `TrellisError` family dynamically across modules rather than from a
  hardcoded list — `LLMRoutingError` subclasses `ConfigError` from
  `trellis.llm.routing`, outside `trellis/errors.py`, and a fixed list
  would have missed it silently.
  ([#829](https://github.com/ronsse/trellis-ai/pull/829))
- **A pack build's latency is now recorded, not hard-coded to zero.**
  `PackBuilder.build()` and `build_sectioned()` both constructed their
  `RetrievalReport` with `duration_ms=0` — one path as a literal, the other
  by never passing the field at all — so `PACK_ASSEMBLED` carried no timing
  key and health reporting had no latency signal: every row read zero,
  whatever the build actually cost. Both now time with
  `time.perf_counter()` from entry through strategy collection and
  budgeting (per-section budgets included on the sectioned path), snapshotted
  once just before section assembly — the cross-section dedup, annotation
  and per-`PackSection` report construction that follows — so every section
  reports the same window rather than its own loop iteration. Neither
  window covers advisory selection, the optional quality evaluator, or the
  event write, on either path; nothing in `src` reads the new key yet, so a
  p50/p95 health-report reader stays a follow-up, not shipped here.
  `_withhold_sectioned`, the pack-effect holdout's (#701) sectioned
  reconstruction path, had the same asymmetry `_withhold_flat` already
  avoided — a withheld sectioned pack's timing silently read back as `0`
  even though a real build happened — and now carries the real value
  through too. `duration_ms` is an additive key on both `PACK_ASSEMBLED`
  payloads; no schema or contract pins the payload key set other than
  `test_pack_holdout_seam.py`'s hand-read base snapshot, which is updated
  to expect it alongside the two holdout keys. Three CLI, API and retrieve
  tests that compared two separately-built packs' full payloads for
  equality (`test_pack_holdout_cli.py`, `test_pack_holdout_routes.py`,
  `test_pack_holdout_seam.py` itself) masked `duration_ms` before
  comparing, since it is now real elapsed time and two builds are not
  expected to cost the same.
  ([#832](https://github.com/ronsse/trellis-ai/pull/832))
- **`trellis admin migrate-provenance` exits `5` when any edge fails to
  migrate, and sanitizes the errors it reports on stdout.** A per-edge
  upsert failure was recorded in `report.errors`, but the command still
  exited `0`, and the raw exception text — including anything
  secret-shaped a store driver's exception carries — reached stdout and
  `--format json` unsanitized. The exit is now decided once, below the
  `--format` branch, from the same `report.errors` flag: `0` when empty,
  `5` otherwise. `--format json` gains a `status` field derived from that
  flag (`"ok"`, `"partial"` when at least one edge still migrated,
  `"error"` when none did); a dry run never writes, so it reports `"ok"`
  and exits `0`. The exception text in the per-edge failure line and in
  both store-error outputs now runs through `sanitize_error_message`,
  which passes an ordinary message through and replaces a leak-shaped one
  with a marker; the edge id and exception type stay, and the full
  exception still goes to the stderr log. This is
  a deliberate departure from `trellis_cli.exit_codes.batch_outcome`
  (#687, #730), which treats a batch as successful unless every command
  in it failed: here, one failed edge in an otherwise-clean
  10,000-edge scan still exits non-zero, because a corpus with even one
  row this command could not write is a state an operator needs to see,
  not one that nets out as a quiet partial success.
  ([#824](https://github.com/ronsse/trellis-ai/pull/824))
- **A broken embedder config is now loud once per cause, not once per
  document.** `run_embed_on_ingest` caught a failed `registry.embedding_fn`
  resolve (a bad `TRELLIS_EMBEDDING_FN`/`embeddings.provider` path, a
  missing provider extra, a missing API key — all config errors that fail
  every subsequent ingest identically) with `logger.exception`, so a
  misconfiguration logged a full traceback per ingested document. It now
  logs `embed_on_ingest_embedder_resolve_failed` at WARNING once per
  distinct `(error_type, setting)` per process, and the hook's returned
  `reason` carries only the exception's type name and, when the exception
  names one, the broken setting (e.g. `"ConfigError: embeddings.provider"`)
  — never the exception's message text, which can echo a credential (this
  resolve path never sees document content). The MCP http prewarm's
  `mcp_prewarm_optional_unavailable` warning now names `error_type` too,
  and its comment states each prewarmed component's own runtime posture
  instead of one blanket claim: an unresolvable `embedding_fn` raises at
  every retrieval call site until the setting is fixed; a broken
  `vector_store` degrades retrieval to keyword and graph (`semantic:
  misconfigured`); embed-on-ingest is fail-soft for an embedder resolve
  failure. Added a recovery runbook,
  [Playbook 15](docs/agent-guide/playbooks.md#playbook-15-recovering-from-a-broken-embedder-config):
  fix the setting, restart (the `embeddings:` block is read once, at
  `StoreRegistry` construction, so a running process can't see an edited
  config or environment), then run `trellis admin reindex-vectors` for
  documents that arrived while it was broken.
  ([#830](https://github.com/ronsse/trellis-ai/pull/830))
- **A broken `vector_store` resolve is now fail-soft too, like the embedder
  resolve #830 fixed above.** `run_embed_on_ingest` resolved
  `registry.knowledge.vector_store` with a bare
  `getattr(registry.knowledge, "vector_store", None)`: the default only
  absorbs `AttributeError`, so a vector backend that raises while
  instantiating (`ConfigError`, `BackendNotInstalledError`, a connection
  error) propagated straight out of the hook — called, unwrapped, by every
  caller (MCP `save_memory`, the mutate "soft" handler, corpus-sync ingest,
  CLI dbt-manifest ingest) *after* the document was already durably stored,
  so the write's own response failed for content that had, in fact, been
  saved, inviting a client retry of an already-stored write. The resolve is
  now wrapped the same way #830 wrapped the embedder's, returning
  `{"embedded": False, "reason": ...}` instead of raising. The dedup helper
  generalized to cover both: the WARNING event is renamed
  `embed_on_ingest_resolve_failed` (from
  `embed_on_ingest_embedder_resolve_failed`) and now carries a `component`
  field (`embedding_fn` or `vector_store`); the once-per-cause cache key is
  `(component, error_type, setting)`, not just `(error_type, setting)`, so
  an embedder and a vector store failing with the same shape (e.g. both a
  bare `RuntimeError` with no `setting`) log independently instead of one
  suppressing the other. Playbook 15 and the MCP prewarm comment updated to
  match.
  ([#834](https://github.com/ronsse/trellis-ai/pull/834))
- **#829 follow-up: the REST pre-validation 422s, `admin.py`'s learning-candidates
  read failure, and `CommandResult.message` on two more routes still quoted
  raw exception/message text.** Five sites built a 422 `detail` as
  `f"...: {exc}"` directly from a `model_validate`/`SectionRequest` failure,
  before the governed-mutation pipeline and its sanitized rejection path ever
  ran: `ingest.py`'s `ingest_trace` / `ingest_evidence`, `observations.py`'s
  `record_observation` / `record_measurement`, and `retrieve.py`'s
  `assemble_sectioned_pack`. All five now build the detail with
  `describe_validation_error`, same as the nine sites above.
  `admin.py`'s `_load_learning_candidates` built
  `_LearningCandidatesUnavailableError`'s message as `f"...: {exc}"` for an
  unreadable or malformed candidates file; it now uses `describe_os_error` /
  `describe_json_error`, falling back to the exception's type name.
  Separately, `CommandResult.message` reached a REST caller unsanitized
  through two paths #829 did not cover: `/commands/batch` (every result,
  including `FAILED`/`REJECTED`, via `_results.py`'s `command_response` —
  the one projection the `curate`, `extract` and `mutations` routers share)
  and `/ingest/bulk`'s three per-item `BulkItemResult` constructions (entity,
  edge, alias), neither of which went through `command_response`. Both now
  sanitize `message` through `sanitize_error_message`, but only when
  `status` is `FAILED` or `REJECTED`: a `SUCCESS`/`DUPLICATE` message only
  restates the caller's own request (a name, id, title or idempotency key),
  and wrapping it unconditionally would replace an ordinary long name, an
  email-named entity, or a digest idempotency key with the suppression
  marker, for no protective value. Fixing the shared
  `command_response` projection closes `/commands/batch` and, incidentally,
  `extract.py`'s identical unfiltered construction, in one change. The two
  `trellis_sdk` sites with the same `f"...: {exc}"` shape
  (`client.py`/`async_client.py`'s `record_feedback`) are left as-is:
  `trellis_sdk` has no dependency edge to `trellis` core to reach
  `error_sanitize` from (by the dual-mode local/remote design), and the
  exception there describes the SDK's own parse failure on a response its
  own trusted server just returned, not caller-supplied or server-internal
  text.
  ([#829](https://github.com/ronsse/trellis-ai/pull/829) follow-up)
- **Retrieval degrades, instead of failing, when the embedder fails to
  resolve.** `build_strategies` used to read `registry.embedding_fn` outside
  its own `try`, so the same raise #830 made quiet on ingest (a bad
  `TRELLIS_EMBEDDING_FN` / `embeddings.provider` path, a missing provider
  extra, a missing API key) still propagated uncaught out of every
  retrieval surface — REST `POST /packs` (and sectioned), MCP
  `get_context` / `get_sectioned_context`, and `trellis retrieve pack` all
  re-read that property directly and turned a healthy keyword+graph pack
  into a 409 / `INTERNAL_ERROR` / exit `5`. The embedder now resolves
  inside `build_strategies`'s own `try`, and the outcome
  (`BuildStrategiesResult.embedder_resolve_failure`) flows into
  `describe_axes` once, so none of the five surfaces re-read
  `registry.embedding_fn` to render it. **`build_strategies` now returns
  `BuildStrategiesResult`, a `NamedTuple`, instead of a bare
  `list[SearchStrategy]`** — existing callers take `.strategies` for the
  list they used to get back directly. A new axes state,
  `semantic: "embedder_failed"`, carries `embedder_error_type` and
  `embedder_setting` (the exception's type name and, when it names one,
  the broken setting — never its message) and a note pointing at
  `trellis admin reindex-vectors`, the same recovery step
  [Playbook 15](docs/agent-guide/playbooks.md#playbook-15-recovering-from-a-broken-embedder-config)
  documents. The failure is recorded as one semantic `StrategyFailure` in
  `PACK_ASSEMBLED` (tallied by `FailedStrategyReport`) but deliberately
  excluded from both the required-strategy and all-axes-failed checks
  below it, so a pack still raises `PackAssemblyError` when every
  remaining axis has also failed, and still degrades — not raises — when
  keyword or graph are healthy.
  ([#838](https://github.com/ronsse/trellis-ai/pull/838))

- **The packaged agent skills no longer teach calls the server rejects or
  ignores.** `record-after-task` showed `"source": "claude-code"` in both
  example traces; `source` is an enum (`agent | human | workflow | system`)
  and trace validation is all-or-nothing, so an agent that copied the example
  lost the whole trace. The skill now documents the trace schema as the server
  enforces it — the keys a step accepts, `result` as an object, artifacts in
  top-level `artifacts_produced`, `metadata` and `outcome.metrics` as the home
  for anything else — and adds grading packs by item id and saving environment
  facts with `save_knowledge`. `link-evidence` told the reader to pass a
  `save_memory` document id as `relates_to`, which creates no edge (documents
  are not graph nodes), and claimed `save_knowledge` updates an existing
  entity, which it does not: every call creates a node. It now stores the fact
  and its anchor in one `save_knowledge(content=..., relates_to=...)` call,
  uses `evidence_ref` for a document that already exists, and says to read the
  reply for `Edge created` or the `edge not created` warning.
  `retrieve-before-task` now covers retrieval after a pivot, after compaction
  (`refresh=True`), in dispatched subagents, and survey-then-fetch with
  `index=True` and `get_items`, and explains the `Withheld` line. All three
  skills move to version 1.1.0; an existing install picks them up with
  `trellis admin install-skills <scope> --force`.

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

- **The CLI finds the stores `trellis admin init --data-dir PATH` made, and
  `admin init` repairs a missing stores directory.** Every CLI store command
  checked `$TRELLIS_DATA_DIR`, else the config dir's `data/`, for the stores
  directory, while the store registry reads `config.yaml`'s `data_dir`
  first. So after `init --data-dir PATH` each store command exited `1` with
  `Stores not initialized`, and the `trellis admin init` it advised found
  `config.yaml` and did nothing. The stores guard now checks the registry's
  directory and names it. `policy add` and the advisory commands write where
  the MCP server and REST API read, and `demo reset` wipes the directory the
  stores are in. For each, the directory differs from before only when
  `config.yaml`'s `data_dir` differs from `$TRELLIS_DATA_DIR` (else the
  config dir's `data/`). With `config.yaml` present, `admin init` recreates
  a missing `stores/` directory without touching the file, and its JSON
  adds `data_dir` and `stores_created`. A `config.yaml` the registry
  refuses now makes `admin init` without `--force`, `policy`, `demo reset`
  and `admin quickstart` exit `5` (was `0`).
  ([#666](https://github.com/ronsse/trellis-ai/pull/666))

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
- **The agent guide names every path behind the `rejected` and `failed`
  command statuses.** `schemas.md` credited `rejected` to the policy gate
  alone. `operations.md` now says the trace-extraction `failed` count
  covers both statuses. The `API_MINOR` comment and `surfaces.md` state the
  rule the repo follows: the minor moves with `SDK_API_MINOR` when the SDK
  comes to rely on an addition, not on every new optional field.
  ([#722](https://github.com/ronsse/trellis-ai/pull/722))
- **On Neo4j and ArcadeDB, a write heals a node with two current rows.**
  `upsert_node` and `upsert_nodes_bulk` over such a node failed the
  `version_id` unique constraint and left both rows current, so every
  later upsert of the node failed too. `update_node_if_current` closed only
  the row its token named, so the node kept two current rows, and it
  raised when both rows had the same `valid_from`. All three now close
  every current row and create one version, which carries `created_at`
  over from the row `get_node` shows, and `update_node_if_current`
  compares its token with that row, so a token from the hidden row is
  refused. The race that leaves the two rows is unchanged.
  ([#723](https://github.com/ronsse/trellis-ai/pull/723))
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
  ([#724](https://github.com/ronsse/trellis-ai/pull/724))
- **A SQLite error inside a governed write fails the command instead of
  escaping it.** A `sqlite3.Error` from a SQLite store, such as for a locked
  or read-only database file, answers `failed` with one `mutation.rejected`
  event: `trellis curate entity --format json` prints its JSON payload and
  exits `5` instead of a traceback and `1`, and `POST /api/v1/entities`
  answers `400` instead of `500`. A SQLite event log that cannot write the
  audit event leaves the `audit_event_not_recorded` warning on the result,
  as a `StoreError` from it already did, instead of raising.
  ([#725](https://github.com/ronsse/trellis-ai/pull/725))
- **A Bolt purge that loses its connection during the commit reads back
  whether it committed.** On Neo4j and ArcadeDB, `delete_node` reported a
  connection lost while its commit was outstanding (`IncompleteCommit`) as
  an unknown outcome, so a purge that had committed still failed the
  redaction: the audit held `MUTATION_REJECTED` and no
  `REDACTION_APPLIED`, and running the redaction again answered
  `target_not_found`. The purge now reads the node's `Node` rows back in a
  new session. With none left it returns as it would have without the
  error, so the redaction is applied and audited; with a row left the
  redaction fails as any other failed purge does. When that read fails
  too, the outcome is still reported as unknown.
  ([#727](https://github.com/ronsse/trellis-ai/pull/727))
- **On Neo4j and ArcadeDB, the graph store's bulk, subgraph and listing
  reads show one version of a node with two current rows.**
  `get_nodes_bulk`, and so `get_subgraph`, `query` and
  `execute_node_query` returned both rows of such a node, so a listing's
  `limit` counted the node twice and a type, doc-link or property filter
  could match the row `get_node` hides. They now return the row
  `get_node` shows, once, now or `as_of` an instant both rows are valid;
  a `limit` counts nodes and a filter judges the row shown. A listing
  filtered on a node field now reads every current row of each node
  passing it: on Neo4j at 5,000 nodes that costs 2.5x to 3x for a filter
  most nodes pass. The race that leaves the two rows is unchanged, as are
  edges written to such a node.
  ([#726](https://github.com/ronsse/trellis-ai/pull/726))
- **A SQLite graph or vector filter key is bound as a parameter instead of
  running as SQL, and names one flat key, as written.** The SQLite graph
  store's `query(properties=...)` and `properties.<key>` query filters, on
  nodes and edges, and the SQLite vector store's `query(filters=...)` spliced
  the key into SQL text as a JSON path, so a `'` in it closed the literal: one
  such key matched every row and another raised `sqlite3.OperationalError`.
  They now bind the key's JSON path, `$."<key>"`, as a statement parameter, so
  no part of a key is SQL text. A key the other backends filter on now filters
  on SQLite too, as one flat object key: `a.b` names the key `a.b`, which
  SQLite alone read as `b` inside `a`, and a space, `'`, `"`, `\`, `[` or
  non-ASCII letter is part of the key. A key holding a NUL character raises
  `ValueError`, since no SQLite JSON path names it, and before SQLite 3.45 a
  key holding `"` matches no row. `GraphStore.query` and `VectorStore.query`
  state the rule, and both contract suites pin it on every backend but the
  Neo4j vector store, whose query the CI image cannot parse. No REST, MCP,
  SDK or CLI route passes a caller-chosen key to these filters.
  ([#729](https://github.com/ronsse/trellis-ai/pull/729),
  [#735](https://github.com/ronsse/trellis-ai/pull/735))
- **A keyed command whose idempotency check cannot read the event log
  fails instead of escaping.** A `TrellisError` or `sqlite3.Error` from
  the executor's read of the event log for a command's idempotency key,
  such as for a malformed event row, answers `failed` with the message
  `Idempotency check failed: <exception type>` and one `mutation.rejected`
  event with `reason: idempotency_check_failed`, and the handler does not
  run. `trellis admin backfill-name-aliases --format json` prints its JSON
  payload and exits `5` instead of a traceback and `1`, and
  `POST /api/v1/commands/batch` answers `200` with the command `failed`
  instead of `500`. The key is not recorded, so a retry runs once the log
  can be read.
  ([#728](https://github.com/ronsse/trellis-ai/pull/728))
- **`trellis extract traces` and `extract refresh` report what their batch
  answered.** Both discarded the results of the governed batch they ran: a
  backfill whose every write was refused printed `"status": "backfilled"`
  and "Extracted N entities" and exited `0`, and a refused refresh read as
  unchanged and exited `0`. Both now count the results by status
  (`succeeded`, `failed`, `rejected`, `duplicates`) in JSON and text, the
  text names the failure count and the first failure's message, and
  `extract traces` labels its totals as drafts. A run whose every command
  is refused or fails exits by the first one (`3` for a policy, `2` for
  another refusal, `5` for a failure) with `"status": "error"` and that
  failure's `message`, as `trellis ingest dbt-manifest` does; a run that
  wrote anything still exits `0`. A dry run's JSON and exit are unchanged.
  The text output prints a trace's domain and a refresh diff's entity type,
  keys and values verbatim, instead of deleting bracketed text or exiting
  `1` on a closing tag such as `[/x]`.
  ([#730](https://github.com/ronsse/trellis-ai/pull/730))
- **A Postgres graph filter on a property key holding `%` filters on that
  key.** The Postgres graph store's `contains` and range (`lt`, `lte`,
  `gt`, `gte`) `properties.<key>` query filters, on nodes and edges,
  spliced the key into the statement as a quoted literal, and psycopg read
  a `%` in it as placeholder syntax: a key such as `a%b` raised
  `psycopg.ProgrammingError`, and a key such as `a%%b` read the property
  `a%b` and returned the wrong rows. Both filters now bind the key
  as a parameter, as the store's other property filters already did. Plain
  keys and keys holding `'` return the same rows as before.
  ([#731](https://github.com/ronsse/trellis-ai/pull/731))
- **On Neo4j and ArcadeDB, an edge written from or to a node with two
  current rows gets one version.** `upsert_edge` and `upsert_edges_bulk`
  created the edge once per current row of each endpoint, so such a node
  left two current versions of the edge, `get_edges` returned it twice, and
  `upsert_edge` raised the driver's "found multiple" warning. Both now
  attach the new version to the row `get_node` shows for each endpoint. An
  edge version already current on the hidden row, such as one written
  before this fix, stays current, and the race that leaves the two rows is
  unchanged. Writes between nodes with one current row are unchanged.
  ([#732](https://github.com/ronsse/trellis-ai/pull/732))
- **A Postgres event log's driver errors are raised as `StoreError`, and a
  failed SQLite event-log append no longer holds the write lock.**
  `PostgresEventLog.append`, `has_idempotency_key`, `get_events` and
  `count` let a psycopg error escape, such as a `PoolTimeout` taking a
  connection or an `OperationalError` from a statement, so a keyed command
  raised out of `MutationExecutor.execute` instead of failing closed and an
  unkeyed one raised after its handler had written. They now raise
  `StoreError` with the message `Event log <method> failed: <exception
  type>`, leaving out the server's text: a keyed command answers `failed`
  with `Idempotency check failed: StoreError`, and an unkeyed one reports
  its write with a warning that its audit event is missing. A handler's
  own event write fails the same way, so a trace ingest whose event
  cannot be written answers `failed` with `Execution failed: Event log
  append failed: <exception type>`, and `POST /api/v1/traces` answers `409`
  instead of `500`, with the trace written either way.
  `GET /api/v1/events` answers `500` with `code: store_error` instead of
  `internal_error`, and `trellis analyze health --format json` prints a
  JSON error payload and exits `5` instead of a traceback and `1`.
  `SQLiteEventLog.append` now rolls back an INSERT that fails, such as on a
  duplicate `event_id`. The transaction had stayed open, so every other
  connection's write to that database waited out its 10-second busy
  timeout and failed with `database is locked` until the connection's next
  commit.
  ([#733](https://github.com/ronsse/trellis-ai/pull/733))
- **Two error log lines no longer print a driver's text.**
  `api_trellis_error`, the REST API's line for a typed Trellis failure, and
  `audit_emit_failed`, the executor's line for an audit event it could not
  write, carry the exception's type and, when Trellis wrote it, its message
  under `error`, instead of a traceback. A traceback prints the exception's
  chain: the driver exception a type-only `StoreError` is chained from, whose
  text can carry query text and values (#702, #713), and for an emit inside
  an `except` block, the failure it was auditing. An untyped failure that
  reaches the API's catch-all still logs its traceback; response bodies and
  command results are unchanged.
  ([#734](https://github.com/ronsse/trellis-ai/pull/734))
- **A graph search for text holding `%`, `_` or `\` matches that text
  literally on SQLite and Postgres.** `GET /graph/search`'s `q`, and the
  facet counts under it, reach the graph store's `search_nodes` and
  `count_nodes_by_type`, which the SQLite and Postgres stores matched as a
  `LIKE` / `ILIKE` pattern: `%` and `_` were wildcards, so `q=a_b` also
  listed a node named `axb` and `q=%` listed every node, and on Postgres a
  backslash escaped the next character, so `back\slash` found `backslash`
  and not itself. Both stores now escape the three characters and name `\`
  as the `ESCAPE` character, so they match as the Neo4j and ArcadeDB stores
  already did. ([#737](https://github.com/ronsse/trellis-ai/pull/737))
- **A partial `trellis extract traces` or `extract refresh` run names its
  first failure in JSON, as its text does.** The JSON of a run with some
  writes refused or failed carries the first one's sanitized `message` beside
  `"status": "backfilled"` or `"refreshed"`; it named a failure only when
  every write was refused or failed. Exit codes are unchanged. The batch rule
  these two share with `trellis ingest dbt-manifest` and `openlineage` is now
  one function, `trellis_cli.exit_codes.batch_outcome`, rather than a copy in
  each module, and those commands' output is otherwise unchanged.
  ([#736](https://github.com/ronsse/trellis-ai/pull/736))
- **`upsert_nodes_bulk` refuses a `node_id` given twice in one call.** On
  Neo4j and ArcadeDB every occurrence that did not match the node's stored
  version wrote a version of its own, so two such occurrences left the node
  with two current rows. On SQLite and Postgres it failed the
  one-current-row unique index with `sqlite3.IntegrityError` or psycopg's
  `UniqueViolation`, and SQLite kept the batch's writes before the failing
  row pending on its connection, so the store's next commit saved them.
  Every backend now raises `ValueError` naming the second occurrence's
  index before it writes anything, as `upsert_edges_bulk` does for a
  repeated edge. A call that names each `node_id` once is unchanged.
  ([#738](https://github.com/ronsse/trellis-ai/pull/738))
- **A failed SQLite trace, API key, parameter or outcome write no longer
  holds the write lock, and a failed outcome batch writes none of its
  rows.** `SQLiteTraceStore.append`, `SQLiteApiKeyStore.create`,
  `SQLiteParameterStore.put`, `SQLiteOutcomeStore.append` and
  `SQLiteOutcomeStore.append_many` now roll back an INSERT that fails, such
  as on a duplicate id, as `SQLiteEventLog.append` does. The transaction
  had stayed open, so every other connection's write to that database
  waited out its 10-second busy timeout and failed with `database is
  locked` until the connection's next commit, and that commit wrote the
  rows a failed `append_many` had inserted before the duplicate. The
  errors raised are unchanged.
  ([#739](https://github.com/ronsse/trellis-ai/pull/739))
- **The warning for a missing audit event names a driver's exception by its
  type alone.** When a command's audit event cannot be written, its result
  carries an `audit_event_not_recorded` warning, which REST, MCP and the CLI
  return to the caller. A raw driver error, such as the `sqlite3.Error` the
  SQLite event log raises, is named by its type, `(IntegrityError)` instead
  of `(IntegrityError: <driver text>)`, as the `audit_emit_failed` log line
  names it, because a driver's text can carry query text and values. A
  Trellis error, such as the type-only `StoreError` the Postgres event log
  raises, keeps its message, and the warning's prefix, the rest of its text
  and every status are unchanged.
  ([#740](https://github.com/ronsse/trellis-ai/pull/740))
- **A failure inside `trellis extract traces`' per-trace loop is reported,
  not left as a traceback.** An untyped exception from extracting a trace,
  reconciling its node roles or executing its batch, such as a database
  driver error the executor does not turn into a result, left the CLI as a
  Python traceback with nothing on stdout, so a `--format json` caller had
  no JSON to parse. The loop now reports it as `extract refresh` reports
  its run: the sanitized error payload in JSON or `Trace backfill failed:`
  and the message in text, then exit `1`, as before. A `TrellisError`
  still reaches the root boundary and exits by its type, and batches
  already written for earlier traces stay written.
  ([#742](https://github.com/ronsse/trellis-ai/pull/742))
- **A validation error that echoes `NaN` or `Infinity` from the request
  body answers 422, not 500; `POST /api/v1/feedback` and `trellis curate
  feedback` hold `rating` to 0.0–1.0, and `Measurement` refuses NaN.**
  Python's `json` module parses both bare tokens, and FastAPI's default 422
  echoes the rejected value back, which Starlette cannot render, so a
  validation error echoing one answered `500 internal_error` and logged a
  traceback. The API's 422 handler now writes a non-finite number as its
  token and is otherwise FastAPI's own, byte for byte. `POST
  /api/v1/feedback` holds `rating` to 0.0–1.0 inclusive, as `POST
  /api/v1/packs/{pack_id}/feedback` and the MCP tool already did, and
  `trellis curate feedback` refuses anything else, NaN included, with exit
  2. `Measurement.metric_value` refuses NaN, so `measurement.record`
  refuses it from every surface; `Infinity` is still accepted.
  ([#741](https://github.com/ronsse/trellis-ai/pull/741))
- **A command missing a required arg is refused, not failed.**
  `MutationExecutor`'s Stage 1 arg check audited its refusal as
  `mutation.rejected` with reason `validate` but returned `FAILED` with no
  `rejection_reason`, so `POST /api/v1/commands/batch` counted a caller's
  malformed command under `failed` (HTTP 200 either way), MCP
  `execute_mutation` answered `"status": "failed"`, and `refusal_exit_code`
  mapped the result to `5`. It now returns `REJECTED` with
  `metadata["rejection_reason"] = "validate"`, as Stage 1's `immutable_core`
  refusal already does, so the batch counts it under `rejected`, MCP answers
  `"rejected"` and the exit code is `2`. Its message, audit event and
  warnings are unchanged. No CLI command builds such a command today.
  ([#744](https://github.com/ronsse/trellis-ai/pull/744))
- **A failed SQLite graph, document, vector, tuner-state or API-key revoke
  write no longer holds the write lock or saves part of itself.**
  `SQLiteGraphStore.upsert_node`, `upsert_nodes_bulk`, `upsert_alias`,
  `upsert_edge`, `upsert_edges_bulk`, `delete_node` and `delete_edge`,
  `SQLiteDocumentStore.put` and `delete`, `SQLiteVectorStore.upsert` and
  `delete`, `SQLiteTunerStateStore.put_proposal`, `update_status` and
  `set_cursor`, and `SQLiteApiKeyStore.revoke` now roll back a write that
  fails, as the writes fixed in #739 do. Until the store's next commit,
  other connections' writes failed with `database is locked`, and that
  commit saved the failed call's earlier statements, such as a version's
  close without its replacement or a document without its full-text row.
  `commit=False` writes and the errors raised are unchanged.
  ([#745](https://github.com/ronsse/trellis-ai/pull/745))
- **The error sanitizer suppresses PostgreSQL row values.** psycopg's text
  for a constraint violation ends in a `DETAIL` line that quotes the row:
  `Key (name)=(value) already exists.` for a unique, foreign key or
  exclusion violation, and `Failing row contains (...).` for a NOT NULL or
  CHECK violation. No leak heuristic matched either shape, so a psycopg
  error that reached `sanitize_error_message`, such as through a CLI
  command's `--format json` error payload, carried the values verbatim.
  Such text is now replaced with the sanitizer's static marker. The
  marker, the truncation, the payload shape and every caller are
  unchanged.
  ([#747](https://github.com/ronsse/trellis-ai/pull/747))
- **Neo4j and ArcadeDB `upsert_edges_bulk` refuses a row whose endpoint
  stops being current during the call.** The method checks that every
  source and target is current, then writes the batch in a second round
  trip. A row whose endpoint stopped being current between the two, as
  when another writer deletes it, wrote nothing and came back as `""` in
  the returned ids, while the batch's other rows were written. The write
  now checks that it wrote every row it was sent and raises `ValueError`
  naming the first missing row's index and endpoint, as the endpoint check
  does, and the raise rolls the write's transaction back, so no row of the
  batch is written. A call whose endpoints stay current is unchanged.
  ([#746](https://github.com/ronsse/trellis-ai/pull/746))
- **`trellis.testing.in_memory_client` answers a `TrellisError` and a
  `NaN`-echoing validation error the way `create_app()` does.** The
  testing shim behind it and `in_memory_async_client` registered none of
  `create_app`'s exception handlers, so a `TrellisError` raised in a route
  reached the test as that exception, and a validation error echoing `NaN`
  raised `ValueError`. Both now answer production's status and body (the
  shim's `request_id` is null), so the SDK raises `TrellisClientError` for
  a `ConfigError`'s 409 and `TrellisServerError` for any other
  `TrellisError`'s 500. An untyped exception still raises into the test.
  Both apps register the handlers from
  `trellis_api.app.register_exception_handlers`.
  ([#749](https://github.com/ronsse/trellis-ai/pull/749))
- **A `feedback.record` command refuses a rating outside `[0.0, 1.0]`.**
  `OperationRegistry.validate` checked only that `rating` was present, so
  `POST /api/v1/commands/batch` and MCP `execute_mutation` — the two
  surfaces that build a `Command` straight from caller args — could pass
  NaN, +/-Infinity, a negative value, a value above `1.0`, a bool, `null`
  or a string straight through to `FeedbackRecordHandler`, which recorded it
  verbatim. `POST /api/v1/feedback` and `trellis curate feedback` already
  bound `rating` before building the `Command` and are unaffected. A bad
  rating on `feedback.record` now fails Stage 1 the same way a missing arg
  does: `REJECTED` with `metadata["rejection_reason"] = "validate"`, one
  `mutation.rejected` event, nothing recorded. No other operation changes.
  ([#751](https://github.com/ronsse/trellis-ai/pull/751))
- **SQLite and Postgres `upsert_edges_bulk` closes only the prior versions
  of the triplets a batch names.** It closed every current edge from a
  batch entry's source, so upserting one `(source, target, edge_type)` edge
  left that source's other current edges with no open version. Neo4j and
  ArcadeDB already closed by exact triplet and are unchanged.
  ([#752](https://github.com/ronsse/trellis-ai/pull/752))
- **A CLI failure line is no longer hard-wrapped at the console width.**
  Sixteen `except Exception` arms in `trellis extract refresh`,
  `extract traces`, `ingest` (trace, evidence, dbt-manifest, openlineage,
  conversations, corpus), `admin migrate-provenance` and the admin proposal
  commands print `<what failed>: <message>` in text mode. Rich hard-wrapped
  that line at the console width, 80 columns when no standard stream is a
  terminal, so a caller reading one line got part of the message. They now
  print it unwrapped, as the root error boundary does, and the terminal
  still wraps it on screen. Text, colour, JSON output and exit codes are
  unchanged, and a message with its own newlines keeps them.
  ([#750](https://github.com/ronsse/trellis-ai/pull/750))
- **The error sanitizer suppresses Neo4j and ArcadeDB duplicate-constraint
  values over Bolt.** Neo4j's uniqueness-violation text quotes the value:
  `` Node(<n>) already exists with label `<Label>` and property
  `<prop>` = '<value>' `` (gql_status `22N79`). So does ArcadeDB's, raised
  inside a managed transaction even with the value passed as a bound
  parameter: `` Duplicated key [<value>] found on index '<Label>[<prop>]'
  already assigned to record #<rid> `` (gql_status `50N42`). Both passed
  through `sanitize_error_message` verbatim and now get its static marker.
  The marker and every other pattern, caller and payload shape are
  unchanged.
  ([#753](https://github.com/ronsse/trellis-ai/pull/753))
- **ArcadeDB `upsert_edges_bulk` raises its documented `ValueError`, not a
  raw driver error, when a dropped row's endpoint is re-created mid-call.**
  #746 re-read a dropped row's endpoints inside the write's still-open
  transaction. On ArcadeDB, when another writer re-created one of them
  after the write, beside a row the write had written, that re-read raised
  `neo4j.exceptions.DatabaseError: Record #... not found`. The endpoints
  are now re-read after the transaction rolls back: the error names the
  endpoint that is still missing, or both when both are current again, and
  no edge of the batch is written. Neo4j already raised the `ValueError`;
  SQLite and Postgres are unchanged.
  ([#754](https://github.com/ronsse/trellis-ai/pull/754))
- **`SQLiteDocumentStore.search` refuses a scalar metadata filter on a
  NUL-holding key with the `ValueError` the SQLite graph and vector stores
  raise**, before any SQL runs, where it raised a raw
  `sqlite3.OperationalError`. A key that also holds `"` or `\`, and a
  `None`, list or dict value, are still compared in Python and do not raise.
  ([#743](https://github.com/ronsse/trellis-ai/issues/743) follow-up 3)
- **Concurrent `upsert_node` writes of one node on Postgres no longer
  raise a raw `UniqueViolation`.** The `FOR UPDATE` read serialises
  neither two creates of a new `node_id`, which have no row to lock, nor
  two updates of an existing one, where the writer that waited finds no
  current row. The later `INSERT` hit the partial unique index
  `idx_nodes_current` and its write was lost. `upsert_node` now retries
  once in a fresh transaction, which writes a new version over the
  winner's row, and matches the index by `exc.diag.constraint_name`
  because the message text follows the server's locale. A second
  conflict, or any conflict in `upsert_nodes_bulk`, which does not retry,
  raises a `StoreError` naming only the exception type. Other unique
  violations are unchanged.
  ([#755](https://github.com/ronsse/trellis-ai/pull/755))
- **A failed command names a non-Trellis exception by its type alone.** The
  FAILED message that REST, MCP and the CLI return reads, for example,
  `Execution failed: IntegrityError` instead of the exception's text, which a
  driver can fill with query text and values. MCP `execute_mutation` names an
  exception that escapes the executor the same way. A Trellis error keeps its
  text, and the operator log, the audit event and every status are unchanged.
  A `ValueError` or pydantic error raised on a caller's input loses its detail
  too: an invalid or changed `node_role`, or duplicate `document_ids`, on
  `entity.create` reads `Execution failed: ValueError`, and an invalid trace
  sent to `trace.ingest` reads `Execution failed: ValidationError`.
  ([#748](https://github.com/ronsse/trellis-ai/pull/748))
- **`trellis admin migrate-graph` sanitizes a destination store's failure
  text before printing it.** The `Migration aborted:` line,
  `--continue-on-error`'s `Errors:` list, and the `--format json`
  `errors[].message` and `step_failures[].message` printed the store's
  exception text verbatim, which can quote the row value behind a
  duplicate-key or constraint violation. All four now go through
  `sanitize_error_message`, and the payload drops
  `step_failures[].traceback`, which repeats every chained exception's
  text. `MigrationReport` keeps the raw text, as does the
  `--continue-on-error` error log on stderr.
  ([#757](https://github.com/ronsse/trellis-ai/pull/757))
- **Four more CLI failure lines are no longer hard-wrapped at the console
  width.** The same exposure #750 fixed for 16 sites also applied to
  `admin migrate-graph`'s "Invalid YAML in ..." line and to the
  "File not found" / "Path not found" lines of `ingest trace`, `evidence`,
  `dbt-manifest`, `openlineage`, `conversations` and `corpus`. They now
  print unwrapped the same way. Text, colour, JSON output and exit codes
  are unchanged. ([#758](https://github.com/ronsse/trellis-ai/pull/758))
- **The error sanitizer also suppresses Neo4j's constraint-*creation* text,
  not just the write-time violation #753 covers.** When the stores' startup
  schema DDL, `CREATE CONSTRAINT ... IS UNIQUE`, runs over nodes that
  already duplicate a value, Neo4j's error quotes it: `` Both Node(<n>) and
  Node(<n>) have the label `<Label>` and property `<prop>` = '<value>' ``
  (gql_status `50N11`). `sanitize_error_message` kept that value and now
  returns the static marker. The stores already report this failure by its
  error type alone, so the entry covers a caller handed the driver's error.
  ArcadeDB's client-visible text for the same failure quotes no value.
  ([#753](https://github.com/ronsse/trellis-ai/pull/753) follow-up 1)
- **`SQLiteDocumentStore.search`'s `content_tags` facet filter quotes the
  facet name before building its JSON path.** A `.` or `[` in a facet name
  changed which path `$.content_tags.{facet}` read instead of naming the
  literal facet; a NUL silently truncated it; a facet starting with `"`,
  or an empty one, raised an uncaught `sqlite3.OperationalError: bad JSON
  path`. Reachable from `POST /api/v1/packs` `tag_filters`, which answers
  200 for every shape while another axis serves the pack: `PackBuilder`
  drops a failing keyword axis and records it in the `PACK_ASSEMBLED`
  event's `strategy_failures`, not in the response. The facet is now
  quoted by `json_key_path`, as the SQLite graph and vector stores quote a
  filter key, so it names the literal facet; a NUL now raises `ValueError`
  before any SQL runs.
  ([#756](https://github.com/ronsse/trellis-ai/pull/756) follow-up 1)
- **An MCP tool that catches a store or driver exception sanitizes its text
  before replying.** 14 sites in `src/trellis/mcp/server.py`, among them
  `save_memory`'s governed write and `record_observation`, quoted the
  exception verbatim, so a Postgres `DETAIL` line could return a row value
  to the agent. They now render it through `_exception_detail`: a
  `TrellisError` keeps its text, and anything else goes through
  `sanitize_error_message`. The sites left quote caller-input errors or the
  executor's own message, and `tests/unit/mcp/test_exception_text_roster.py`
  fails the build on a new one. An exception a tool does not catch, such as
  one from `save_knowledge`'s write, still reaches the caller through
  FastMCP's generic error. (trellis-ai#748)
- **49 more red CLI lines are no longer hard-wrapped at the console width.**
  Each now passes `soft_wrap=True`, and `extract refresh`'s
  undeclared-source line also escapes the source name. A new rule,
  `tests/unit/test_cli_failure_soft_wrap_rule.py`, fails on an unwrapped red
  `console.print` with an interpolation that a non-zero exit follows in its
  own or an enclosing block; an exit under a later `if`, an `Exit` a helper
  returns and a conditional-expression message are not policed yet.
  ([#766](https://github.com/ronsse/trellis-ai/pull/766))
- **`sanitize_error_message` scans a bounded window, not the whole
  exception text.** The email and inline-credential-URL patterns backtrack
  quadratically over a long run of word characters, so a 100k-character
  input took about 26 s. The leak heuristics now scan at most
  `max_len + 500` characters, a few milliseconds at worst. A secret that
  starts in the visible prefix still trips its pattern if it completes
  within 500 characters past the cut; a leak lying wholly past the window
  now yields the truncated prefix instead of the marker.
  ([#763](https://github.com/ronsse/trellis-ai/pull/763) follow-up 2)
- **`PostgresGraphStore.upsert_edge` and `upsert_edges_bulk` leave one
  current row per logical edge under concurrency.** `idx_edges_current` is
  unique on the random `edge_id`, not on `(source_id, target_id,
  edge_type)`, and `FOR UPDATE` cannot serialize writers that find no
  current row, so two concurrent writers of one edge could each commit a
  current row. Both paths now take a `pg_advisory_xact_lock` on that key
  before reading, as `upsert_alias` does; the bulk path takes its keys in
  sorted order so overlapping batches cannot deadlock, and each version is
  stamped after its lock is granted. Duplicate rows already stored stay.
  (trellis-ai#768)
- **An exception a tool does not catch is sanitized too.** `save_knowledge`
  and `save_experience` call `executor.execute` with no `try/except`, so a
  driver exception escaping either reached the caller raw inside FastMCP's
  generic `Error calling tool '<name>': {e}`. A `_SanitizeUncaughtToolErrors`
  middleware now rebuilds that message for every tool through
  `_exception_detail`, as #765's sites do. Their replies, a tool's own
  `ToolError` and FastMCP's rate-limit and timeout messages pass unchanged.
  A pydantic `ValidationError` raised inside a tool body is not wrapped that
  way, so it still reaches the caller raw.
  ([#765](https://github.com/ronsse/trellis-ai/pull/765) follow-up 1)
- **10 more red CLI lines, and one yellow one, no longer hard-wrap at the
  console width**, which could split an id or path mid-token. The soft-wrap
  rule now also sees an exit raised inside a later `if` (`admin.py` x2,
  `analyze.py`, `curate.py`, `extract_refresh.py`) and the red arm of a
  conditional-expression message (`extract_refresh.py`, `ingest.py` x2). A
  line whose exit is raised in a different function from the print is still
  outside the rule; two such lines are wrapped here by hand.
  ([#766](https://github.com/ronsse/trellis-ai/pull/766) follow-ups 1-2)
- **`sanitize_error_message` rejects a negative `max_len`.** A negative
  value made `text[:max_len]` keep everything but the last few
  characters instead of a short prefix, so the returned text could run
  past the `max_len + 500`-character window the leak heuristics scan —
  a secret starting beyond that window printed unscanned. No caller
  passes `max_len` today, so this was unreachable; it now raises
  `ValueError` for any `max_len < 0`, and `max_len=0` is unchanged.
  ([#767](https://github.com/ronsse/trellis-ai/pull/767) follow-up 2)
- **`POST /api/v1/packs` says which retrieval axes ran.** When one strategy
  raises, `PackBuilder` serves the surviving axes and records the failure in
  `PACK_ASSEMBLED.strategy_failures` and the log, so a REST caller got a 200
  and a degraded pack it could not tell from a full one. `PackResponse` now
  carries the optional `axes` block (`available`, `ran`, `failed`,
  `semantic`) that `trellis retrieve pack --format json` already prints,
  from the same `describe_axes` call: axis names and states, never exception
  text. The sectioned route and MCP `get_context` are unchanged.
  ([#761](https://github.com/ronsse/trellis-ai/pull/761) follow-up A)
- **`SQLiteGraphStore.upsert_edge` and `upsert_edges_bulk` leave one current
  row per logical edge under concurrency.** `idx_edges_upsert` on
  `(source_id, target_id, edge_type)` is not unique, and both methods read
  the current row before taking the write lock, so two connections (two
  processes, or two threads of one store) creating the same edge could each
  insert a current row. Both now run `BEGIN IMMEDIATE` before that read, so
  a second writer waits (up to the busy timeout) and then reads the first
  one's row; a write on a connection already in a transaction joins it.
  Duplicate rows already stored stay.
  ([#762](https://github.com/ronsse/trellis-ai/pull/762) follow-up 2)
- **6 more red CLI failure lines no longer hard-wrap.** Each prints an id,
  path or error text from a helper whose caller then exits non-zero, so the
  soft-wrap rule cannot see it: `admin install-skills`, `admin
  check-extractors`, `admin smoke-test` (x2), `worker embed-traces` and
  `ingest corpus --prune`. A hand-listed roster test pins these and two
  earlier hand-wrapped lines of the same shape.
  ([#771](https://github.com/ronsse/trellis-ai/pull/771) follow-ups 1 and 3)
- **A Postgres or Bolt driver error no longer escapes `MutationExecutor`.**
  A Postgres graph store's `psycopg.Error`, or a Neo4j or ArcadeDB store's
  `DriverError`/`Neo4jError`, raised unmapped from a handler now yields a
  FAILED `CommandResult` and a `MUTATION_REJECTED` audit event, as
  `sqlite3.Error` already did, so a `CONTINUE_ON_ERROR` batch carries on
  past it. The result names the error's type alone; the audit event keeps
  the error's text, which can quote the values being written.
  ([#773](https://github.com/ronsse/trellis-ai/pull/773))
- **A failed embedder resolution is retried, not cached as "not
  configured".** One raising resolution of `TRELLIS_EMBEDDING_FN` or
  `embeddings.provider` (an unimportable dotted path, the `llm-openai`
  extra missing) left `StoreRegistry.embedding_fn` returning `None` for
  the life of the process: `POST /api/v1/packs` answered `409` once and
  then `200` with `axes.semantic: "not_configured"`, and an MCP http
  server, whose boot prewarm absorbed the raise, served every pack
  without the semantic axis and recorded no failure. Each call now
  resolves again until one succeeds, so `/packs` keeps answering `409`
  and MCP `get_context` keeps erroring while the configuration is broken.
  ([#779](https://github.com/ronsse/trellis-ai/pull/779))
- **`trellis analyze health` surfaces a failed retrieval strategy.** When one
  `PackBuilder` strategy raised, the surviving axes kept serving and the
  failure reached only the `PACK_ASSEMBLED` event's `strategy_failures` and
  an ERROR log line. `trellis analyze health` now counts the window's packs
  with a failed strategy, per strategy name with the latest occurrence, in
  text and `--format json`, and any such pack adds a `warn` reason. Counts
  and strategy names only, never exception text.
  ([#781](https://github.com/ronsse/trellis-ai/pull/781))
- **`admin smoke-test` and `admin install-skills`/`quickstart` keep a
  bracketed backend or OS error intact.** A check's or readyz backend's
  `error`, and a failed skill copy's, went into Rich markup raw, so a
  `[...]` in it (a bracketed host or path) was read as a style tag and
  deleted: the operator saw a different error from the one raised. Each
  is now escaped; `--format json` is unchanged.
  ([#777](https://github.com/ronsse/trellis-ai/pull/777) follow-up 2)
- **`POST /api/v1/packs/sectioned`, MCP `get_context` and `search` report a
  failed retrieval axis.** The sectioned response gains the optional `axes`
  block `POST /api/v1/packs` has (`null` for `sections=[]`). `get_context`
  without `sections` and `search` add one line,
  `**Retrieval axis failed:** <names>.`, naming only the axes that raised,
  so a degraded empty pack no longer reads like an empty corpus; a clean
  reply is unchanged. `get_context(sections=...)`, `get_objective_context`,
  `get_task_context` and `get_sectioned_context` do not report it yet.
  ([#783](https://github.com/ronsse/trellis-ai/pull/783))
- **`BoltOpenCypherGraphStore.upsert_edge` (Neo4j and ArcadeDB) leaves one
  current row per logical edge under concurrency.** Two concurrent writers
  on the same `(source_id, target_id, edge_type)` could both read "no
  current edge" and both create one. `upsert_edge` now write-locks the
  source endpoint's current row before that read, so the second writer
  reads the first one's row. `upsert_edges_bulk` takes no such lock, so a
  bulk write racing another writer of the same edge can still duplicate it.
  ([#762](https://github.com/ronsse/trellis-ai/pull/762) follow-up 2, also
  the Bolt item from the [#774](https://github.com/ronsse/trellis-ai/pull/774) gate)
- **A missing OpenAI API key answers like a config error, not a 500.**
  `embeddings: provider: openai` with the `llm-openai` extra installed and
  no key anywhere let the SDK client constructor's untyped
  `openai.OpenAIError` escape, so every `POST /api/v1/packs` answered
  `500` and the CLI exited `1` uncaught. It is now a `ConfigError` naming
  `embeddings.api_key_env`, `embeddings.api_key` and `OPENAI_API_KEY`,
  never the SDK's wording or a key value: REST answers `409`
  `config_error`, the CLI exits `5`, and MCP `get_context` keeps
  `INTERNAL_ERROR` with that message. A failed embeddings call still
  raises `openai.OpenAIError`.
  (follow-up F-a from [#779](https://github.com/ronsse/trellis-ai/pull/779))
- **`admin smoke-test`'s header and readyz backend rows keep a bracketed URL
  or backend name intact.** The header's URL and a readyz backend's name and
  status/latency detail went into Rich markup raw, so an IPv6 host led by a
  lowercase letter (`http://[fd00::1]:8420`) or a backend key or status
  carrying `[...]` was read as a style tag and deleted. All three are now
  escaped; `--format json` is unchanged.
  ([#784](https://github.com/ronsse/trellis-ai/pull/784) follow-up 1)
- **`admin smoke-test`'s text mode no longer crashes on a non-dict readyz
  `backends`.** A `/readyz` body whose `backends` is a list or a string (a
  server or proxy that is not Trellis) raised `AttributeError` in text mode
  while `--format json` printed it. Text mode now skips the backend rows for
  such a value, as it already did for a non-dict backend entry; json still
  shows it raw, and both formats exit with the same code.
  (follow-up F1 from [#788](https://github.com/ronsse/trellis-ai/pull/788))
- **`trellis admin migrate-provenance` prints each per-edge error
  verbatim.** An error line carries the store's exception text, which went
  into Rich markup raw, so a `[...]` in it was read as a style tag and
  deleted, and a line wider than the console was hard-wrapped, folding a
  long token mid-way. The text is now escaped and printed with
  `soft_wrap=True`; `--format json` is unchanged.
  (follow-up F2 from [#777](https://github.com/ronsse/trellis-ai/pull/777))
- **A broken driver install no longer replaces a handler's own failure in
  `MutationExecutor`.** The handler-panic catch imported `psycopg` and
  `neo4j.exceptions` to find their base errors, so a driver whose import
  raised anything but `ImportError` (a native library that fails to load)
  escaped `execute()` in place of the handler's FAILED result, on every
  call. The classes are now read from `sys.modules` and nothing is
  imported: a driver's exception can only exist once its module is.
  ([#773](https://github.com/ronsse/trellis-ai/pull/773) follow-up 2)
- **`get_context(sections=...)`, `get_objective_context`, `get_task_context`
  and `get_sectioned_context` report a failed retrieval axis too.** These
  four share `_sectioned_context`, the one helper #783 left silent: a
  failed axis reached neither the markdown reply nor any JSON block, so the
  gap #783 closed for the flat path and the sectioned REST route stayed
  open on these MCP tools. Same one-line note, same `describe_axes` /
  `format_failed_axes_note` helpers, same header placement (after
  `pack_id`, before the withholding note, outside the token budget) as the
  flat path; a clean reply is unchanged.
  ([#783](https://github.com/ronsse/trellis-ai/pull/783) follow-up)
- **`BoltOpenCypherGraphStore.upsert_edge` and `upsert_edges_bulk` (Neo4j and
  ArcadeDB) find an edge left on a re-versioned endpoint.** `upsert_node`
  re-versions a node without moving its relationships, and the existing-edge
  lookup matched only between the two current endpoint rows, so the next
  upsert of a triplet with a re-versioned endpoint minted a second current
  edge. The lookup now matches each endpoint's `node_id` on any row and
  closes every current match, carrying the `edge_id` forward, so a triplet
  already doubled heals on its next upsert. SQLite and Postgres were not
  affected.
  ([#782](https://github.com/ronsse/trellis-ai/pull/782) follow-up 1)
- **`admin smoke-test`'s text mode no longer crashes on a non-string readyz
  backend `error`.** A dict backend entry whose own `error` was a truthy
  non-string (an int, an object, a list, `true`) raised `TypeError` at
  `rich.markup.escape`, which requires `str`, while `--format json` printed
  the value fine. The backend-error line now renders `str(error)`; a string
  `error` is unchanged.
  (follow-up F1 from [#791](https://github.com/ronsse/trellis-ai/pull/791))
- **A `supersedes=` stamp failure no longer leaks driver text to an MCP
  caller.** `supersede_document` and `supersede_entity` returned the caught
  exception's raw text, which `save_knowledge` and `save_memory` put into an
  `McpError` message or into a saved memory's warning. They now render it
  through `trellis.core.error_sanitize.render_exception_detail`, the rule
  `trellis.mcp.server`'s other caught-exception sites already used: a
  `TrellisError`'s text and a clean message (a timeout) read through
  unchanged, and a leak-shaped one comes back as the sanitizer's marker
  after the exception's type name.
  (follow-up 5 from the [#793](https://github.com/ronsse/trellis-ai/pull/793) gate)
- **`trellis ingest corpus --prune` and `ingest conversations --prune` keep
  a long withheld path or title on one line.** The yellow `withheld` line
  each prints before its exit-`5` carries a relpath, title or doc id plus
  error text; Rich hard-wrapped a long one at the console width, splitting
  it mid-token so it could not be copied whole. Both lines now pass
  `soft_wrap=True`.
  ([#777](https://github.com/ronsse/trellis-ai/pull/777) follow-up 1)
- **A missing OpenAI API key on the provider classes is a config error, not
  a raw SDK exception.** Constructing `OpenAIClient` or `OpenAIEmbedder`
  (`trellis.llm.providers.openai`) with no key anywhere raised the SDK's
  untyped `openai.OpenAIError`; it now raises `ConfigError` naming
  `llm.api_key_env` or `llm.embedding.api_key_env`, with the SDK error
  chained as `__cause__`. No in-repo caller reaches it today:
  `StoreRegistry`'s builders and `mcp.server`'s env fallback treat a missing
  key as "not configured" before constructing a client.
  (follow-up 2 from [#786](https://github.com/ronsse/trellis-ai/pull/786))
- **A bad `TRELLIS_EMBEDDING_FN` path's `ConfigError` names the env var, not
  `embeddings.provider`.** `_import_callable` hardcoded `setting=` to the
  config key, so an operator chasing a typo'd env var was pointed at the
  wrong YAML key; the env-var call site now passes
  `setting="TRELLIS_EMBEDDING_FN"`. The docstrings now state which failures
  are a `ConfigError` (a malformed path, an `ImportError`, a missing or
  non-callable attribute) and that any other import-time exception
  propagates unchanged.
  ([#794](https://github.com/ronsse/trellis-ai/pull/794) follow-ups F1-F3)
- **The yellow `warning` line of `trellis ingest corpus` and `ingest
  conversations`, and the "not found" line of `retrieve trace` and
  `retrieve entity`, keep a long path or id on one line.** The warning
  line carries a directory path plus error text on an exit-`5` `--prune`
  run; each not-found line carries the id the caller passed, before exit
  `1`. Rich hard-wrapped a long one at the console width, splitting it
  mid-token. All four now pass `soft_wrap=True`.
  (follow-up 1 from the [#799](https://github.com/ronsse/trellis-ai/pull/799) gate)
- **A leading-dot embedding-callable path, or a non-string
  `embeddings.provider`, answered 500 instead of `ConfigError`.** `.pkg.fn`
  reached `importlib.import_module` as a relative import (`TypeError`) and an
  int, list or mapping had no `.rpartition` (`AttributeError`). The path is
  now checked whole before any import (a `str`, every dot-separated part
  non-empty) and raises the `ConfigError` (REST 409) a no-dot path does; a
  non-string is named by its type, never echoed, since a mapping can hold a
  credential.
  (follow-ups B and C from the [#800](https://github.com/ronsse/trellis-ai/pull/800) gate)
- **`trellis curate`'s `Warning:` and `Message:` lines, and `extract
  refresh`'s `first:` failure line, keep a long value on one line.** Each
  can carry a policy condition, audit error or command message before a
  refused write exits non-zero, and none is a red line in the function that
  exits, so the red-only soft-wrap scan could not see them. All three now
  pass `soft_wrap=True` and are listed by hand beside that scan. `extract
  refresh`'s `~ key: before -> after` diff line passes it too.
  (follow-ups 1 and 2 from the [#807](https://github.com/ronsse/trellis-ai/pull/807) gate)
- **`upsert_edge` on Neo4j locks both endpoints before resolving them.** It
  resolved the source and target rows unlocked and then locked only the
  source row, so an `upsert_node` re-versioning either endpoint at the same
  moment could make it raise `ValueError: ... has no current version` for a
  node that had one, or attach the edge to the source row that re-version
  had just closed. It now locks the current rows of both endpoints, in
  sorted `node_id` order so writers of `x->y` and `y->x` cannot deadlock,
  and resolves them after. ArcadeDB, which takes no locks, and
  `upsert_edges_bulk` behave as before. (follow-up 1 from
  [#790](https://github.com/ronsse/trellis-ai/pull/790))
- **`get_context`, `search`, `get_context(sections=...)`, `get_objective_context`,
  `get_task_context` and `get_sectioned_context` now report a misconfigured
  semantic axis.** These MCP tools already added a `**Retrieval axis failed:**`
  line for an axis named in `axes.failed`, but a `misconfigured` semantic axis
  (an embedder resolved and the vector backend never initialised) never lands
  in that list — it's absent from `axes.available` entirely — so an agent on
  MCP heard nothing of it while REST's `axes.semantic`
  and the CLI's text sentence both reported the gap. The same tools now add a
  second, independent `**Semantic retrieval misconfigured:**` line for that
  state, reusing the one `describe_axes` report both lines are built from; a
  clean pack, or one with only a failed axis, is unchanged.
  (follow-up F2 from the [#783](https://github.com/ronsse/trellis-ai/pull/783) gate)
- **`POST /api/v1/packs/sectioned` refuses `sections=[]` instead of answering
  `200` with `axes` null.** MCP's `get_context`/`get_sectioned_context` already
  reject an empty `sections` list with `"sections must not be empty"` before
  any build runs; REST let it through, ran `build_sectioned()`'s pool-level
  strategy pass anyway (so `PACK_ASSEMBLED.strategy_failures` genuinely
  recorded a raised axis), then answered `200` with `axes: null` because an
  empty `sections` list produces zero `PackSection`s for the route to read
  `strategies_used` off. The route now raises the same `422` REST already
  uses for a malformed section dict, with MCP's own wording, so the two
  surfaces give the same reason for the same input and a `200` sectioned
  response always has at least one section to read `axes` from.
  (follow-up F4 from the [#783](https://github.com/ronsse/trellis-ai/pull/783) gate)
- **`upsert_edges_bulk` on Neo4j locks every distinct endpoint in the batch in
  one constant-text query before resolving any row.** Two bulk writers of the
  same `(source_id, target_id, edge_type)` triplet, or a bulk writer racing a
  single-row `upsert_edge`, could each see "no current edge" and both create
  one. A single `UNWIND $lock_ids AS lock_nid` clause over the sorted endpoint
  list replaces one clause per endpoint, so the query text and cached plan stay
  constant at any batch size, and the sort order keeps two such writers from
  deadlocking on shared endpoints. ArcadeDB takes no lock from this touch; a
  same-triplet duplicate there is still caught only by the existing OCC retry.
  (follow-up 2 from the [#782](https://github.com/ronsse/trellis-ai/pull/782)
  gate, repeated as follow-up 2 by the
  [#803](https://github.com/ronsse/trellis-ai/pull/803) gate)
- **The Python SDK's `get_objective_context` and `get_task_context` now name
  a failed or misconfigured retrieval axis, sync and async.** They read the
  response's `sections` and `withholding` but not its `axes` block, so an SDK
  caller whose keyword or semantic axis failed saw what looked like a clean
  pack. They now render MCP's two axis lines from that block, with MCP's
  formatters, which moved to the new `trellis_wire.axes` (still re-exported
  from `trellis.retrieve.builder_factory`); a response without `axes` renders
  no note.
  (follow-up F3 from the [#783](https://github.com/ronsse/trellis-ai/pull/783) gate)
- **`trellis extract traces`'s per-trace backfill row keeps a long domain on
  one line.** `_print_backfill` prints `- {trace_id} ({domain}): N
  entities, M edges` for every trace with drafts, and those counts come
  from extraction, before the governed batch runs — so the row still
  prints on a refused (deny-all) backfill. It is uncoloured and printed in
  a different function than the one that exits non-zero after it, past the
  red-only soft-wrap scan. It now passes `soft_wrap=True` and is listed by
  hand beside that scan (`CROSS_FUNCTION_FAILURE_LINES`, 18 -> 19).
  (follow-up from the [#811](https://github.com/ronsse/trellis-ai/pull/811) gate)
- **Single-row `upsert_edge` carries `created_at` forward on SQLite and
  Postgres.** Re-upserting the same `(source_id, target_id, edge_type)`
  triplet through the single-row path stamped a fresh `created_at` on the
  new version instead of keeping the logical edge's original mint time —
  `upsert_edges_bulk` and the Bolt store (Neo4j, ArcadeDB) already carried it
  forward, so the single-row path disagreed with every other path to the
  same table. Both stores now read `created_at` alongside `edge_id` under
  the same lock that reads the current row and write it back on the new
  version; `valid_from` still advances on every write.
- **Three follow-ups to #834's embed-on-ingest fix apply its "describe,
  don't quote; log once" discipline to the paths it didn't cover.**
  `trellis admin reindex-vectors` read the vector store with an unwrapped
  `getattr(..., None)`, which only ever absorbed an `AttributeError` the
  real property never raises, so an untyped backend-construction error
  escaped as a bare traceback with exit 1; a `ConfigError` already exited
  5 through the root CLI boundary (`_BoundaryGroup`) on its own, before
  this change. A new `_resolve_vector_store` helper re-raises a
  `TrellisError` (a `ConfigError` included) unchanged, keeping its own
  `error_code` and `setting`, and wraps only an untyped cause in a
  `StoreError` naming its type alone; both now reach the boundary and
  exit `EXIT_STORE` (5), regardless of `--format`, with the standard
  sanitized JSON envelope. `resolve_vector_store` (`trellis.core.vector_metadata`)
  already logged at `WARNING` with `exc_info=True` on every call while a
  vector-store backend stayed broken — a full traceback carrying the
  exception's message on every tag write, demotion and session capture;
  it now describes the cause (type name, plus the setting for a
  `ConfigError`) and logs once per distinct cause per process instead.
  The embed-on-ingest hook had a single `except` block shared by its
  embed and upsert calls, logged the `embed_on_ingest_failed` event at
  `ERROR` (`logger.exception`) on every document, and returned the
  exception's message as the hook's own `reason`; it now splits into two
  `except` blocks keyed by step (`embed`, `upsert`), describes rather
  than quotes, and logs the renamed `embed_on_ingest_upsert_failed` event
  at `WARNING` once per distinct `(step, cause)` per process — so an
  embedder failure and a vector-store failure of the same error type can
  no longer hide each other — dropping `doc_id`/`source` from the log
  line. In both helpers, a `setting` that is not a string degrades to
  `None` instead of breaking the dedup cache, so every path still
  degrades rather than raising.
- **The last two NaN/Infinity gaps #831 and #835 left open are closed.**
  `DegradableJsonStore._save` — the shared base of `PolicyStore` and
  `AdvisoryStore`, called out by #835 as unchanged — dumped with a lenient
  `json.dumps` and still wrote a bare `NaN`/`Infinity` token; it now
  refuses with the same `allow_nan=False`, before the file is touched, so
  an existing file survives byte-identical on a refused write. A legacy
  file some already-shipped build wrote with the non-standard token still
  reads back clean (the read path, `json.loads`, stays lenient on
  purpose). In practice only `AdvisoryStore` can drive this refusal
  through a real write today: `Policy`'s float-capable fields, `metadata`
  and `PolicyRule.params`, are both `dict[str, Any]`, and Pydantic's own
  `model_dump(mode="json")` nulls a non-finite float nested under an
  `Any`-typed value before
  `_save` ever runs, so a live NaN cannot reach the guard through
  `Policy`'s public schema — the guard is still wired on the shared base
  regardless, and a new test pins that directly. Separately,
  `trellis admin backfill-outcomes --apply` replaying a legacy
  feedback event with a non-finite relevance score (predating the
  EventLog's own #831 write guard, so a raw row written before that
  fix can still hold one) wrote through `OutcomeStore.append_many`,
  which raises exactly that bare `ValueError` — not a `TrellisError`,
  so the CLI's global boundary did not catch it, and it surfaced as an
  untyped traceback. The command now catches it at its own boundary and
  reports a described failure — a sanitized JSON envelope or an escaped
  one-line message — exiting `EXIT_INTERNAL` in both `--format text` and
  `json`. The replay appends in 500-row transactions, so chunks before
  the refused one may already be committed.
- **A non-string `embeddings.provider` silently meant "not configured."**
  `registry.embedding_fn` tested the config value with a bare
  `if provider:`, so any falsy non-string — notably YAML `provider: off`,
  which parses to the bool `False` — fell through to the "no embedder"
  branch with no error, turning semantic search off with nothing to read
  (`axes.semantic` just read `not_configured`, indistinguishable from an
  operator who never set the key). `None` and `""` still mean "not
  configured"; every other non-string (bool, int, list, mapping) now
  raises `ConfigError(setting="embeddings.provider")`, naming only the
  value's type, never the value itself — a misplaced mapping can hold a
  credential. A bool gets an extra hint ("delete the key or set it to
  null") since `off`/`on`/`yes`/`no` are the likely source. Retrieval
  still degrades rather than crashing, as for every other
  embedder-resolve failure (#838): the pack falls back to keyword + graph
  and reports `axes.semantic == "embedder_failed"` with
  `embedder_setting == "embeddings.provider"`, so a `provider: off`
  deployment now reads `embedder_failed` where it used to read
  `not_configured`. Embed-on-ingest stays fail-soft, and
  `trellis admin reindex-vectors` now refuses with the `ConfigError`
  (exit 5) instead of its generic missing-embedder message (exit 1).
- **Three more "describe, don't quote" (#206) surfaces follow up #829/#836/#837 — MCP, admin-proposals CLI and the SDK.**
  MCP `server.py`'s three pydantic-validation sites (`save_experience`,
  `record_observation`, `execute_mutation`) quoted `str(exc)` — including a
  rejected field's own value — back to the calling agent; they now render
  through `describe_validation_error`, which never serializes the rejected
  value at all. A mutation's `CommandResult.message` is now sanitized when,
  and only when, its status is `FAILED` or `REJECTED` — the same gate #836
  applied on the REST boundary (`_results.py`), not one already applied
  elsewhere in this file. The gate covers `save_experience`, `save_knowledge`'s
  entity create, `save_memory`, `record_observation` and `execute_mutation`
  in `server.py`, `supersession.py`'s `_execute`, and both link writers in
  the new `knowledge_links.py` module this PR adds — the code it replaces
  interpolated `result.message` directly at the two link call sites, not
  through any helper; `_describe_unsuccessful` did not exist before this PR.
  `save_knowledge`'s evidence path wrapped a non-SUCCESS `evidence.ingest`
  result's message in a `MutationError` and quoted it raw in both the McpError
  message and its `data["message"]` echo; it now sanitizes that message too,
  and its exemption leaves the MCP exception-text roster, which falls from
  three hand-read sites to one. SUCCESS and DUPLICATE messages only restate
  the caller's own request, so they pass through unchanged — this gate is a
  status-gated deny-list, not a type/location renderer, so clean FAILED/REJECTED
  text still passes through verbatim too. CLI `admin_proposals.py`'s three
  broad `except Exception` handlers (`generate-proposals`, `list-proposals`,
  `show-proposal`) built their JSON `message` as `f"{type(exc).__name__}: {exc}"`,
  interpolating a raw store/driver exception; they now render through
  `render_exception_detail`, which passes a `TrellisError`'s own text, or other
  clean foreign text, through verbatim — only an operator sees this surface's
  output, on their own terminal. SDK `TrellisClient.record_feedback` /
  `AsyncTrellisClient.record_feedback` built a `TrellisProtocolError` as
  `f"...: {exc}"` on a malformed response body; a pydantic `ValidationError`'s
  own `str()` composes `input_value=<the body's own field value>` into every
  line by design, so whatever a misbehaving proxy or wire-schema-skewed server
  sent back was quoted back whole. `trellis_sdk` must not import `trellis.*`
  (`tests/unit/sdk/test_isolation.py` AST-walks the package), so its fix is a
  new, dependency-free `trellis_sdk._http.describe_body_parse_error` rather
  than a call to `trellis.core.error_sanitize.describe_validation_error` — a
  local port that keeps a JSON decode error's message/line/column and a
  validation error's `loc`/`type`, dropping pydantic's `msg` outright, since
  this copy carries no sanitizer to defend a future custom validator's own
  message. These pydantic and SDK sites are the only ones that now name the
  exception's type and field location instead of its rendered text; the
  `CommandResult` sites above are a sanitizer, not a renderer. REST `admin.py`'s
  `_LearningCandidatesUnavailableError` path was checked and found already
  routing every exception through
  `describe_os_error`/`describe_json_error`/`sanitize_error_message`.
- **`build_strategies()`'s vector-backend init failure is now loud once per
  cause, not once per pack build.** Degrading to keyword + graph search when
  `SemanticSearch(registry.knowledge.vector_store, ...)` raised logged
  `semantic_search_init_failed` with `exc_info=True` on every single build —
  `StoreRegistry._get` caches only success (#830), so a persistent
  misconfiguration (a bad `vector_store.provider`, a missing extra, a down
  backend) re-raised identically on every retrieval call, turning one broken
  setting into a full traceback per pack. A new
  `_warn_semantic_search_init_failed_once` (`functools.cache`-backed,
  mirroring the embedder-resolve helper #838 added to this same module)
  now logs at WARNING once per distinct `(error_type, setting)` per
  process, describing the cause — the exception's type name and, for a
  `ConfigError`, its `setting` — never the exception's message or a
  traceback, which can echo a DSN or credential. A non-string `setting`
  (only reachable from a plugin or future caller today) is narrowed to
  `None` before it reaches the cache key, the same guard #839 added for
  `embed_ingest_hook.py` and `vector_metadata.py`, so an unhashable
  `ConfigError.setting` still degrades retrieval instead of raising; the
  sibling embedder-resolve call site (#838) gets the same guard.
  ([#843](https://github.com/ronsse/trellis-ai/pull/843))
- **The stored `PACK_ASSEMBLED.strategy_failures[].message` is now a
  sanitized summary, not a raw `str(exc)`.** A strategy's own exception text
  (a DSN fragment, a credential, a row value) reached this durable audit
  event verbatim. `StrategyFailure` keeps a raw `.message` — `PackAssemblyError`'s
  own interpolated text and `trellis_cli.main`'s plain-text render arm both
  depend on seeing it unsanitized, by design (#493,
  `test_the_machine_arm_suppresses_a_leaky_axis_message`) — alongside a new
  `.audit_message`, computed by a module-level helper that calls
  `summarize_exception(exc)["message"]` and falls back to the suppression
  marker if summarizing itself raises, so an exotic exception from the open
  `SearchStrategy` protocol can't turn a degraded build into a crash. When a
  constructor passes no `audit_message`, the default is now
  `sanitize_error_message(message)` rather than the raw message, so the
  stored form is sanitized by construction rather than by caller discipline
  alone. `build()` and `build_sectioned()` write the event's
  `strategy_failures[]` through a new `to_audit_event_payload()` serializer
  that reads `.audit_message`, instead of the raw-message `to_event_payload()`
  CLI rendering already uses. The axes block an agent-facing pack response
  carries (`format_failed_axes_note`) never reads either field.
  `PACK_ASSEMBLED` rows written before this fix keep their raw text — events
  are immutable — and remain servable verbatim by
  `GET /api/v1/packs/{pack_id}`.
  ([#843](https://github.com/ronsse/trellis-ai/pull/843))

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
