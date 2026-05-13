# ADR: CLI exit codes

**Status:** Accepted
**Date:** 2026-05-12
**Deciders:** Trellis core
**Related:**
- [`./plan-cleanup-silent-fallbacks.md`](./plan-cleanup-silent-fallbacks.md) — Phase 6 closeout
- [`./adr-extraction-failure-telemetry.md`](./adr-extraction-failure-telemetry.md) — sibling "no silent fallback" ADR

---

## 1. Context

`trellis_cli` exits with code `1` on every failure mode today. Operators wrap CLI calls in shell pipelines (cron jobs, CI steps, smoke probes, K8s liveness probes). They want to branch on *why* a command failed:

- A validation error means "fix your input file, retry."
- A policy denial means "ask for approval, don't retry."
- An idempotency conflict means "this already happened, treat as success."
- A store/backend error means "infrastructure is degraded, page someone."
- An internal panic means "filed bug report."

A single non-zero code collapses all five into "something broke" and makes operator scripts brittle. The Phase 6 audit (`audit/silent_fallbacks_2026-05.md`) also surfaced ~9 silent swallows in `trellis_cli/` where errors degraded to empty results without surfacing as exits at all — those need a code, too.

## 2. Decision

Adopt the following exit-code map for every `trellis` CLI command. The map is small on purpose: five codes covers every actionable branch, anything beyond falls back to `1`.

| Code | Meaning | Typical cause |
|-----:|---|---|
| `0` | Success | Command completed; output on stdout. |
| `1` | Internal / unexpected error | Bug, unhandled exception path. Re-file with traceback. |
| `2` | Validation error | Input failed schema or business-rule check. Fix input, retry. |
| `3` | Policy denied | A `PolicyGate` rejected the command. Get approval, don't retry. |
| `4` | Idempotency conflict | Command's `idempotency_key` already committed. Treat as success. |
| `5` | Store / backend error | Backend not installed, DSN unreachable, schema mismatch. Page on-call. |

`2` aligns with the POSIX convention for "user-input error" used by `sh`, `grep`, etc. The remaining four are Trellis-specific.

## 3. Rationale

- **Operators script around exit codes.** Silent success on failure (the current `except Exception: return {}` shape) masks production issues. A typed exit lets cron/CI distinguish "retry now" from "page".
- **Aligns with the typed exception hierarchy.** `trellis.errors` already separates `ValidationError`, `PolicyViolationError`, `IdempotencyError`, `StoreError`. The CLI layer maps these 1:1 to the table above.
- **JSON output is unchanged.** `--format json` callers still parse the `status` field; the exit code is the cheap branch for shell callers who skip JSON.
- **No code beyond `5`.** Resist the temptation to add codes for every event. Anything we haven't pre-classified is a `1` (bug) until we earn the code.

## 4. Implementation

CLI swallows that hid one of the above conditions get converted as:

```python
try:
    do_work()
except ValidationError as exc:
    logger.error("validation_failed", error=str(exc))
    raise typer.Exit(code=2) from exc
except PolicyViolationError as exc:
    logger.error("policy_denied", error=str(exc))
    raise typer.Exit(code=3) from exc
except IdempotencyError as exc:
    logger.info("idempotency_conflict", key=exc.idempotency_key)
    raise typer.Exit(code=4) from exc
except StoreError as exc:
    logger.error("store_error", store=exc.store, error=str(exc))
    raise typer.Exit(code=5) from exc
```

The canonical constants live in `trellis_cli.exit_codes`:

```python
EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_VALIDATION = 2
EXIT_POLICY = 3
EXIT_IDEMPOTENCY = 4
EXIT_STORE = 5
```

Anything left unmapped raises `typer.Exit(code=1)` — explicit, never silent.

## 5. Out of scope

- **HTTP exit codes.** The REST API maps status codes per the SDK ADR; CLI exit codes are a different surface.
- **Per-subcommand custom codes.** A future ADR may add `6+` for command-specific conditions if a real need emerges; today nothing justifies it.

## 6. Decision record

Accepted 2026-05-12 as part of Phase 6 of the silent-fallback cleanup ([`plan-cleanup-silent-fallbacks.md`](./plan-cleanup-silent-fallbacks.md)).
