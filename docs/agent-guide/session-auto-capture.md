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
   turns and tool *names* only. Raw `tool_result` / `toolUseResult` content
   (where `op`-style secret reads and env dumps live) never enters the digest.
   Malformed lines are skipped and counted; unknown record types, sidechains,
   and compaction summaries are tolerated.
3. **Trigger** deterministically: sessions with errors or user corrections are
   capture-mandatory (failure-bias); clean sessions are sampled ~1-in-N.
4. **Distil** triggered sessions with the local model. **Fail-closed**: if the
   model is unavailable the sweep captures *nothing* for that session and
   leaves it un-watermarked so a later run retries it.
5. **Gate** each candidate: the deterministic secret-scan gate (hard drop on a
   hit; the content is never logged), the capture-instruction injection guard
   (drops candidates whose text addresses the memory system — "remember
   this…" shapes or worthiness-rubric stuffing), then the four-test
   worthiness gate (non-derivable / durable / actionable / attributed).
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
  `StoreRegistry.build_llm_client()`). If no client can be built, the sweep is
  a safe no-op (captures nothing).

## Configuration (environment)

| Variable | Default | Purpose |
|---|---|---|
| `TRELLIS_CONFIG_DIR` | `~/.trellis` | Trellis config/stores directory. |
| `TRELLIS_CAPTURE_TRANSCRIPTS_ROOT` | `~/.claude/projects` | Transcript root to sweep. |
| `TRELLIS_CAPTURE_WATERMARK` | `<config_dir>/capture-watermark.json` | Per-file cursor store. |
| `TRELLIS_CAPTURE_SAMPLE_DENOMINATOR` | `5` | Clean-session sampling (`1` = capture all clean sessions). |
| `TRELLIS_CAPTURE_SOURCE_SYSTEM` | `claude-code` | Corpus namespace / doc-id prefix. |
| `TRELLIS_DISTILL_MODEL` | `hermes3:8b` | Model id label recorded in training events. |
| `TRELLIS_ENABLE_RECONCILE_ON_WRITE` | *(unset)* | When truthy, near-duplicate captures are adjudicated (ADD/UPDATE/SUPERSEDE/NOOP) instead of piling up. Off by default. |

The reconcile step also honours the #263 knobs (`TRELLIS_RECONCILE_MODEL`,
`TRELLIS_RECONCILE_TIMEOUT_S`).

---

## Dry run first (writes nothing)

```bash
python -m trellis_workers.session_capture --dry-run
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
# Point at the venv/interpreter that has trellis + trellis_workers installed.
ExecStart=/home/<user>/path/to/.venv/bin/python -m trellis_workers.session_capture
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
- Repeated `warnings[].kind == "distill_unavailable"` → the local model
  endpoint is down; sessions are being retried (not lost), but nothing is
  captured until it recovers.
- `sessions_skipped_watermark` should dominate on steady-state runs (only new
  work is processed).

## Rollback

Stop and disable the timer; captured documents remain (they are ordinary
Trellis memories under the `capture:claude-code:` prefix and can be pruned with
the standard document tooling if desired).

```bash
systemctl --user disable --now trellis-session-capture.timer
```
