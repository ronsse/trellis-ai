# Open-issue adversarial corpus — 2026-09-04

Dated snapshot of **40 open GitHub issues** reviewed against current `main` at `f9ff32c933020e267f5560493f4739538d2b95f6`.
Sources of truth remain **GitHub issues** and [`docs/design/implementation-roadmap.md`](../../design/implementation-roadmap.md).
This corpus records adversarial review verdicts; it does not replace issue bodies.

## Verdict taxonomy

| Verdict | Meaning |
|---------|---------|
| `valid-now` | Actionable on current main; evidence confirms defect or agreed work |
| `valid-slice` | Partly actionable; remainder blocked on decision, signal, or dependency |
| `blocked-signal` | Correct direction; waiting on production data, pilot, or wiring |
| `blocked-operator` | Owner/console action only |
| `blocked-decision` | Needs owner semantics or design choice before code |
| `duplicate` | Superseded by another issue in this corpus |
| `refuted` | Premise or proposed mechanism refuted by measurement |
| `stale-fixed` | (none in this snapshot) |
| `meta-board` | Tracking/infra board, not a single code PR |
| `external` | Wrong repo or deployment operator scope |

## Disposition summary

- **valid-now:** [#256](./256.md), [#257](./257.md), [#264](./264.md), [#350](./350.md), [#351](./351.md), [#356](./356.md), [#360](./360.md), [#369](./369.md), [#439](./439.md), [#494](./494.md), [#522](./522.md), [#523](./523.md), [#526](./526.md)
- **valid-slice:** [#474](./474.md), [#475](./475.md), [#477](./477.md), [#514](./514.md), [#515](./515.md)
- **blocked-signal:** [#201](./201.md), [#261](./261.md), [#306](./306.md), [#364](./364.md), [#365](./365.md), [#371](./371.md), [#503](./503.md)
- **blocked-operator:** [#250](./250.md)
- **blocked-decision:** [#194](./194.md), [#200](./200.md), [#202](./202.md), [#203](./203.md), [#463](./463.md), [#476](./476.md), [#502](./502.md)
- **duplicate:** [#525](./525.md)
- **refuted:** [#342](./342.md), [#375](./375.md)
- **meta-board:** [#275](./275.md), [#405](./405.md), [#478](./478.md)
- **external:** [#208](./208.md)

## Waves (execution order)

- **W0-external:** #208
- **W0-meta:** #275, #405, #478
- **W0-ops:** #250
- **W1-ci:** #350, #351, #356, #522, #523, #525, #526
- **W2-api:** #439
- **W2-docs:** #257, #494
- **W2-learning:** #264
- **W2-mutate:** #360, #369
- **W2-stores:** #256
- **W3-llm:** #514, #515
- **W4-mutate:** #474
- **W4-retrieval:** #463, #475, #476, #477, #502
- **W4-security:** #194
- **W5-learning:** #261, #342
- **W5-ops:** #364, #365
- **W5-retrieval:** #371, #375, #503
- **W5-workers:** #306
- **W6-query-history:** #200, #201, #202, #203

## File-territory collision map


| Territory | Issues |
|-----------|--------|
| `tests/`, CI workflows | #526, #525, #356, #351, #350, #523, #522 |
| `src/trellis_cli/` | #522, #494 |
| `src/trellis/retrieve/` | #439, #503, #502, #475, #477, #371, #463 |
| `src/trellis/mutate/` | #360, #369, #474, #194 |
| `src/trellis/llm/` | #514, #515 |
| `src/trellis/stores/` | #350, #351, #256 |
| `src/trellis/learning/` | #264, #342, #261, #502 |
| `docs/design/` | #257, #478, #200, #202, #275 |
| Ops / external | #405, #250, #208, #365 |
| Query-history (pilot) | #200, #201, #202, #203 |


## Dependency graph

```mermaid
flowchart TD
  subgraph W1_ci [W1 CI]
    I526[#526 importorskip CI]
    I356[#356 stores CI]
    I351[#351 ArcadeDB CI]
    I350[#350 pgvector init]
    I523[#523 basetemp]
    I522[#522 Rich renders]
  end
  subgraph W2_core [W2 Core]
    I360[#360 evidence.ingest]
    I369[#369 name aliases]
    I264[#264 judged op emitters]
    I439[#439 SDK withholding]
    I256[#256 bolt plugin]
    I257[#257 ingest ADR]
    I494[#494 quiet docs]
  end
  I525[#525 duplicate] --> I526
  I356 --> I351
  I256 --> I351
  I256 --> I194[#194 classification]
  I360 --> I474[#474 confirm-save]
  I360 --> I194
  I371[#371 graph recency] --> I475[#475 assumptions]
  I475 --> I477[#477 last_verified]
  I502[#502 advisory semantics] --> I503[#503 category cap]
  I478[#478 umbrella] --> I474
  I478 --> I475
  I478 --> I476[#476 spaces]
  I478 --> I477
  I201[#201 BI source] --> I203[#203 scouting]
  I514[#514 JSON fences] --> I515[#515 cache measure]
```

## Execution / merge gates

1. **Green against current `main`** — rebase before merge; no stale SHA claims.
2. **Territory collision** — issues sharing a territory in the map should not dispatch in parallel without coordination (#526 + #356 both touch CI).
3. **Split consensus stops** — blocked-decision issues (#502, #477, #463, #194, query-history family) require owner call before implementation PRs.
4. **Valid-slice discipline** — ship only the named slice (#514a fence, #515 instrumentation); defer blocked halves explicitly.
5. **Duplicate/refuted** — #525 closes to #526; #375 and #342 do not dispatch without rescope decision.
6. **JSON manifest** — [`manifest.json`](./manifest.json) is the machine index; briefs are authoritative prose.

## Files

- [`manifest.json`](./manifest.json) — machine-readable index
- `#NNN.md` — one brief per issue (40 files)
