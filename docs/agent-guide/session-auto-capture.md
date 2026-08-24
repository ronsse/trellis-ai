# Claude Code session auto-capture — install runbook

Auto-capture reads local Claude Code transcripts, distils durable operator
memories with a local model, hard-gates them against secret leakage, and
writes survivors into Trellis through the sanctioned
[`sync_records`](../../src/trellis/ingest_corpus/sync.py) seam.

**Client-side by design (ADR #257).** No transcript parser lives in `trellis`
core: transcript formats churn per harness (Claude Code, and later others),
so the reader + distiller live in `trellis_workers.session_capture` and the
core surface ends at the existing document-ingest APIs. Trellis ingests
normalized documents; format conversion is the client's pre-step.

This runbook is the **machine-side install** — it is executed by the operator
(or the orchestrator under supervision), not by the capture code itself. The
repo ships the code and its tests; the timer unit, config, and live Trellis
instance are set up here.

---

## How it works (one sweep)

1. **Discover** `~/.claude/projects/**/*.jsonl`; a per-file `(mtime, size)`
   watermark skips unchanged sessions before they are opened.
2. **Parse** each new/changed file into a *secret-free* digest — natural-language
   turns (in transcript order, user and assistant interleaved) and tool
   *names* only. Raw `tool_result` / `toolUseResult` content
   (where `op`-style secret reads and env dumps live) never enters the digest.
   Malformed lines are skipped and counted; unknown record types, sidechains,
   and compaction summaries are tolerated.
3. **Trigger** deterministically: sessions with errors or user corrections are
   capture-mandatory (failure-bias); clean sessions are sampled ~1-in-N.
4. **Distil** triggered sessions with the local model. The judge prompt carries
   skip discipline: routine operational steps (a clean install, a bare
   listing, a status check that found nothing) are refused at the source, a
   skip is the empty array rather than prose, and the judge records what the
   session did — never what the capture process itself is doing.
   **Fail-closed**: if the model is unavailable the sweep captures *nothing*
   for that session and leaves it un-watermarked so a later run retries it.
5. **Gate** each candidate: the deterministic secret-scan gate (hard drop on a
   hit; the content is never logged), the capture-instruction injection guard
   (drops candidates whose text addresses the memory system — "remember
   this…" shapes or worthiness-rubric stuffing), then the worthiness gate
   (durable / actionable / attributed, plus a minimum memory length).
   `non_derivable` is **recorded but not gated on** — see below.
6. **Reconcile** (optional, flag-gated) survivors against already-stored
   captures, reusing the #263 reconcile-on-write machinery.
7. **Write** through `sync_records` — content-hash idempotent, per-source
   id-prefix scoped (`capture:claude-code:`), embed-on-ingest, emits
   `MEMORY_STORED`. Each written memory emits a leak-safe
   `MEMORY_OP_JUDGED` (`distillation`) training-pair event.

Two idempotency layers make re-runs safe: the watermark skips unchanged files,
and the content-derived `doc_id` makes `sync_records` skip an identical memory
even if a file is re-parsed after a watermark reset.

**Residual risk (be honest with yourself as operator):** unattended capture
of adversarial text is inherently gameable at the v1 deterministic tier — a
model can launder an injected instruction into clean-looking prose the guard
patterns won't match. The mitigations are layered, not absolute: every
capture is provenance-marked (`capture:claude-code:` doc-id prefix,
`distilled: true` metadata) so evidence-driven retention (#261) can prune
captures that never prove useful, and the secret-scan gate bounds a
successful injection's damage to junk, not leakage.

---

## Prerequisites

- Trellis installed on the operator host and initialized
  (`trellis admin init`); `~/.trellis/config.yaml` present and pointed at the
  live stores.
- A local, OpenAI-compatible model endpoint configured in that `config.yaml`
  under the `llm:` block (the sweep builds the distillation client via
  `StoreRegistry.build_llm_client()`). **The judge is not optional.**
  Distillation fail-closes, so a sweep without a client captures nothing — it
  therefore refuses to run and exits non-zero rather than reporting a clean
  no-op. A sweep whose judge disappears part-way finishes, reports
  `sessions_judge_unavailable` (those sessions stay un-watermarked for the
  next run), and also exits non-zero.

## Three ways to run it

All three are the same code path (`run_sweep` in
`trellis_workers/session_capture/sweep.py`) and read the same environment:

```bash
trellis worker capture-sessions [--dry-run] [--format text|json]
trellis-session-capture [--dry-run]            # console script
python -m trellis_workers.session_capture [--dry-run]
```

Only `trellis worker capture-sessions` takes `--format`; the other two
always emit the JSON `CaptureReport` on stdout.

## Configuration (environment)

| Variable | Default | Purpose |
|---|---|---|
| `TRELLIS_CONFIG_DIR` | `~/.trellis` | Trellis config/stores directory. |
| `TRELLIS_CAPTURE_TRANSCRIPTS_ROOT` | `~/.claude/projects` | Transcript root to sweep. |
| `TRELLIS_CAPTURE_WATERMARK` | `<config_dir>/capture-watermark.json` | Per-file cursor store. |
| `TRELLIS_CAPTURE_SAMPLE_DENOMINATOR` | `5` | Clean-session sampling (`1` = capture all clean sessions). |
| `TRELLIS_CAPTURE_SOURCE_SYSTEM` | `claude-code` | Corpus namespace / doc-id prefix. |
| `TRELLIS_DISTILL_MODEL` | `hermes3:8b` | Model id label recorded in training events. |
| `TRELLIS_CAPTURE_STRICT` | `1` | When truthy (the default), a sweep that left any session unjudged exits non-zero. Set `0`/`false`/`no`/`off` to report the count and exit `0` instead — those sessions stay un-watermarked and are retried next sweep. A sweep with *no* judge at all always fails, strict or not. |
| `TRELLIS_CAPTURE_MAX_SALIENT_CHARS` | `8000` | Cap on conversation text sent to the judge. **Coupled to the judge endpoint's context window — read the warning below before raising it.** |
| `TRELLIS_ENABLE_RECONCILE_ON_WRITE` | *(unset)* | When truthy, near-duplicate captures are adjudicated (ADD/UPDATE/SUPERSEDE/NOOP) instead of piling up. Off by default. |

The reconcile step also honours the #263 knobs (`TRELLIS_RECONCILE_MODEL`,
`TRELLIS_RECONCILE_TIMEOUT_S`).

### Why `non_derivable` is recorded but not enforced

The judge self-reports four booleans; the gate enforces three of them. The
fourth was dropped after being measured against a real corpus: hermes3:8b
returned `non_derivable=False` on **every** candidate distilled from real
sessions (9 of 9 across three transcripts), so the gate rejected 100% of them
and the sweep could not write a memory at all. The candidates it discarded
were good ones — a roadmap-drift finding, a stack pivot, a build-vs-buy call.

The tempting explanation is that two of the tests contradict: "attributed"
asks the judge to cite a path, and "non_derivable" asks whether the memory is
reconstructable from the repo, so a cited path arguably makes it derivable by
construction. That was **tested and refuted** — rewording the prompt to judge
the insight rather than its evidence produced *zero* candidates instead of
more passing ones.

What the evidence supports is narrower: a small local judge does not reliably
self-assess this particular abstraction and defaults it to False. The field
still rides the `MEMORY_OP_JUDGED` training pair (#264), because a
self-report is worth collecting even when it is not worth trusting. Restoring
it as a gate requires a judge demonstrated to *vary* on it — a larger model,
or a prompt carrying the corpus's actual domain vocabulary — not a re-tuned
threshold. Re-measure before re-enabling.

### The salient-text cap is coupled to the judge's context window

Raising `TRELLIS_CAPTURE_MAX_SALIENT_CHARS` **without also raising the judge
endpoint's context window makes the judge fabricate.** The failure is silent
in every layer that would normally catch it:

* Ollama's OpenAI-compatible endpoint **ignores `num_ctx`** in `extra_body`
  (verified — the request is accepted and the window is unchanged), so the
  client cannot raise the window. Only the server can.
* A prompt over the window is truncated **server-side**. The request still
  returns 200 and the JSON still parses.
* hermes3:8b does not decline a truncated prompt — it answers from the
  remnant. Measured on a real session at a 24k cap against a 4096 window, it
  returned memories that appear nowhere in the transcript ("Learning Python",
  "First Git Repository", "Claude's Birthday Party").
* The worthiness gate cannot catch this: a fabricated memory carries
  confident booleans and plausible-looking evidence.

The default `8000` fits under Ollama's default window; that is why it works,
and nothing else was protecting it. To capture more per session:

1. Raise the **server** window first — `OLLAMA_CONTEXT_LENGTH` on the Ollama
   service, or a Modelfile `PARAMETER num_ctx`.
2. Then raise `TRELLIS_CAPTURE_MAX_SALIENT_CHARS`.

If step 1 is skipped or insufficient, `distill_session` detects the shortfall
from the response's own `usage.prompt_tokens`, logs `distill_prompt_truncated`
with both counts and the remedy, and treats the session as a judge outage —
it is left un-watermarked and retried rather than written. Under the default
strict mode that also makes the sweep exit non-zero, so the misconfiguration
surfaces instead of quietly filling memory with fiction.

---

## Dry run first (writes nothing)

```bash
trellis-session-capture --dry-run
```

Emits the JSON `CaptureReport` to stdout: how many sessions would be parsed,
triggered, distilled, blocked by the secret gate, and written. No documents
are stored and the watermark is not advanced. Confirm `candidates_blocked_scan`
(drops by the secret-scan gate) behaves as expected and `memories_written` (the
*plan* count on a dry run) is sane before enabling the timer.

> **Precise scope of "writes nothing":** a dry run stores no documents, emits
> no training-pair events, and never advances the watermark — but it does
> emit the seam's `CORPUS_SYNCED` telemetry event (flagged `dry_run: true`,
> run **counts** only, no content), the same convention as
> `ingest corpus --dry-run`.

## Nightly sweep — systemd user timer

A nightly sweep beats a `SessionEnd`/`Stop` hook: hooks run under tight
time budgets and distillation is a model call. The off-peak hour matters for
a second reason: **the sweep writes in-process against the same stores the
live MCP server uses** (same `~/.trellis` config → same SQLite files by
default). Schedule it when live sessions are idle and avoid manual runs
during heavy interactive use — the worst case is a transient
`SQLITE_BUSY`-class error on a contended write, which fails that sweep's
write and is retried next sweep (the session stays un-watermarked; nothing
is lost). Install as a **user** timer (replace `<user>` and paths to match
the host):

`~/.config/systemd/user/trellis-session-capture.service`

```ini
[Unit]
Description=Trellis Claude Code session auto-capture sweep
After=network-online.target

[Service]
Type=oneshot
# Point at the venv that has trellis + trellis_workers installed.
ExecStart=/home/<user>/path/to/.venv/bin/trellis-session-capture
Environment=TRELLIS_CONFIG_DIR=/home/<user>/.trellis
# Opt into near-duplicate adjudication once a memory corpus exists:
# Environment=TRELLIS_ENABLE_RECONCILE_ON_WRITE=1
```

`~/.config/systemd/user/trellis-session-capture.timer`

```ini
[Unit]
Description=Nightly Trellis session auto-capture

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable and start:

```bash
systemctl --user daemon-reload
systemctl --user enable --now trellis-session-capture.timer
# (optional) allow user timers to run without an active login session:
#   loginctl enable-linger <user>
```

---

## Verification probes

```bash
# Trigger one sweep now and read its report.
systemctl --user start trellis-session-capture.service
journalctl --user -u trellis-session-capture.service -n 50 --no-pager

# Confirm captured memories exist under the source prefix.
trellis documents list --format json | \
  python -c 'import json,sys; docs=json.load(sys.stdin); \
print(sum(1 for d in docs if d["doc_id"].startswith("capture:claude-code:")))'

# 30-day success check (from the issue): advisories start flowing and the
# nightly curate log stops being all-zeros.
trellis analyze context-effectiveness --format json
```

Health signals in the JSON `CaptureReport`:

- `scan_hits_by_class` (secret-scan gate hits, class label → count) climbing
  while `memories_written` stays reasonable → the gate is doing its job;
  investigate only if *every* candidate is blocked.
- `candidates_rejected_injection` > 0 → a session tried to address the memory
  system directly ("remember this…" / rubric-stuffing). Worth eyeballing the
  session; the candidate was dropped, never stored.
- `sessions_judge_unavailable` > 0 (equivalently, repeated
  `warnings[].kind == "distill_unavailable"`) → the local model endpoint is
  down; those sessions are retried next sweep (not lost), but nothing is
  captured until it recovers. The run exits non-zero, so the systemd unit is
  marked failed instead of logging a clean success. **This is a behaviour
  change** for timers installed before this landed: a single transient model
  timeout in an otherwise-good sweep now fails the unit. Set
  `TRELLIS_CAPTURE_STRICT=0` in the unit to keep the count but restore the
  zero exit.
- `sessions_skipped_watermark` should dominate on steady-state runs (only new
  work is processed).

## Rollback

Stop and disable the timer; captured documents remain (they are ordinary
Trellis memories under the `capture:claude-code:` prefix and can be pruned with
the standard document tooling if desired).

```bash
systemctl --user disable --now trellis-session-capture.timer
```
