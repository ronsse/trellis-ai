# Overnight work corpus — 2026-09-09

> **What this is.** A dependency-ordered, lane-partitioned body of work sized for
> unattended execution. Every item is self-contained: it names its own scope, its
> acceptance criteria, the command that verifies it, and — where it exists — the
> evidence that the premise is still true. Companion to
> [`swarm-handoff.md`](../design/swarm-handoff.md) (the operating manual) and
> [`autonomous-backlog.md`](../design/autonomous-backlog.md) (the full specs).
>
> **Read §0 before executing anything.** Nothing in this corpus picks itself up.

---

## 0. Who runs this, and what does not exist

`src/trellis_workers/code_authoring/issue_selector.py` decides *eligibility* — open,
carrying both `mechanical` and `ready`, carrying none of `keystone` / `owner-only` /
`blocked:*`, and carrying a fenced ```` ```files_allowed ```` block that survives
`validate_allowlist`. It is conjunctive and fail-closed; an absent or invalid allowlist
collapses to `()` = *not a candidate*, never *allow everything*.

**Eligibility is not execution.** `select_candidate` and `evaluate_issue` are exported and
called by nothing outside their own module and tests. skynet-hub #10/#11 shipped Cohort-2
as **guardrails only**; the spawn harness (gate (a)) is still open. `roadmap-nightly.sh`
(cron `0 5 * * *`) is REPORT_ONLY — it posts a fixed DoD block to board #275 and does not
enumerate open issues.

So this corpus has exactly three ways to be performed, and they need different things:

| Mode | What it needs | What it gets |
|---|---|---|
| **A. A long attended session** | someone leaves a session running | everything in Lanes 0–5, in the order below |
| **B. The selector, once a spawn harness exists** | skynet-hub gate (a) — the harness is the work, not the corpus | only the items marked **`selector-ready`** |
| **C. A person** | nothing | everything, including Lane 6 |

**Lane 6 is owner-only under every mode.** Pushing, PRs, container rebuilds, cron edits,
credential scope changes and production data cleanup are outside the autonomy contract
(`swarm-handoff.md` §2: *never yours, at any confidence*).

### Autonomy classes used below

- **`selector-ready`** — mechanical, reversible, allowlist-scoped. Safe for mode B today.
- **`agent`** — reversible in git, but needs judgement the selector cannot encode
  (a design call, a measurement, a scope split). Mode A or C.
- **`decision`** — the deliverable is a ledger entry with a recommendation, not a diff.
  Ship the safe default so the operator's answer costs a config change, not a PR.
- **`owner-only`** — outside the contract. Queued here so it is visible, never executed.
- **`do-not-do`** — recorded so it is not rediscovered as work.

### The gate every code item passes

Green against **current** `main`, in `/mnt/ssd/trellis-worktrees/.ci-venv311` (Python
3.11.15, ruff 0.16.5, mypy 2.3.1 — the CI pins). Not `.ci-venv`: it is 3.12, where bare
`mypy src/` aborts having checked **zero files** and prints as clean.

```bash
V=/mnt/ssd/trellis-worktrees/.ci-venv311/bin
$V/ruff check src tests && $V/ruff format --check src tests
$V/mypy src | tail -1          # must name a file count > 0
$V/pytest tests/ -q            # deselects 683 cloud-backend tests; that is expected
```

---

## Lane 0 — the integrity of the record

**Do this lane first.** Every measurement in every other lane is read out of these
documents, and four of them are currently wrong. These are cheap, reversible, and they
are what stops the next agent inheriting a false premise.

---

### N1 · `swarm-handoff.md` prescribes a typecheck that is worse than the defect it works around · `selector-ready`

`docs/design/swarm-handoff.md:272` tells a review-gate agent to run
`mypy --python-version 3.12 src/` when bare `mypy src/` aborts at zero files. That
workaround **runs a different check from CI** — `pyproject.toml` pins
`python_version = "3.11"` — so it masks exactly the 3.11-vs-3.12 divergence the pin
exists to catch. The abort is an artefact of `.ci-venv` being 3.12 (it resolves numpy
2.5.2, whose stub carries an unguarded PEP 695 `type` statement); a 3.11 venv resolves
numpy 2.4.6 and bare `mypy src/` succeeds on every file.

The row's own last sentence is right — *"treat a suspiciously fast clean mypy as a red
flag"* — and its prescription contradicts it.

**Do:** replace the workaround with `/mnt/ssd/trellis-worktrees/.ci-venv311`. Keep the
diagnosis and the red-flag sentence; they are the durable half. Sweep for the same advice
elsewhere (#480 is reported to have merged it into `autonomous-backlog.md`; verify).

```files_allowed
docs/design/swarm-handoff.md
docs/design/autonomous-backlog.md
```

**Acceptance:** no file under `docs/` recommends `--python-version`; the named venv exists
and its `mypy src/` reports a non-zero file count.
**Verify:** `grep -rn -- "--python-version" docs/` returns nothing.

---

### N2 · `swarm-handoff.md` §1 is 40+ commits stale, and §6 already says why that is a defect · `selector-ready`

§1 opens `main = 9ed98f7` and then lists what landed on three named dates. `origin/main`
is `1ef5c9c`. §6 of the same document states the rule: **"mechanism does not rot; status
does. Write down why something is true, and link to the live source for whether it
currently holds."** §1 is status, written as fact, with no live source beside it.

This is not cosmetic. §1.2 — *"read this before trusting any production measurement"* —
hangs off a `main` pointer that has been wrong for eleven days, so an agent computing
deployment lag from it computes it against the wrong base.

**Do:** apply §6's rule to §1. Keep §1.0 (*what the 2026-08-28 wave found*) and §1.2's
*mechanism*; replace the commit pins and the landed-PR lists with the commands that
answer them (`git log origin/main`, `gh pr list --state merged --limit 20`). Update the
"Last updated" line to the commit the edit lands on.

```files_allowed
docs/design/swarm-handoff.md
```

**Acceptance:** §1 contains no hard-coded commit sha or PR roster; each replaced claim
names the command that re-derives it.

---

### N3 · #351 is closed as COMPLETED and the gap it tracks is still open · `agent`

**Verified 2026-09-09.** `#351` — *"ArcadeDB graph contract never runs in CI — the
blessed substrate has no service container in any workflow"* — was closed
2026-09-08T16:54:52Z with `stateReason: COMPLETED`. It was not completed:

- the only occurrence of the string `arcade` anywhere under `.github/workflows/` is
  `live-infra.yml:203`, a comment reading *"Still not covered: the ArcadeDB graph contract
  … Tracked as issue #351"*;
- `live-infra.yml` declares two services, `neo4j:2025.12` and `pgvector/pgvector:pg16`.
  There is no ArcadeDB container;
- `CLAUDE.md:252` still states the gap and still points at #351.

So the *blessed* graph + vector substrate's contract suite has never executed in CI, and
the one artefact that would bring anyone back to it now reads closed. This is the
[[closed-issues-can-hide-open-gates]] shape (#312) reproduced exactly, one day old.

**Do, in this order:** (1) reopen #351 or file its successor — the tracker state is the
urgent half, and it is one API call; (2) then either add an ArcadeDB service container to
`live-infra.yml` and sweep the contract in, or, if that is refused on cost, replace both
prose claims with an explicit *"deliberately unwired, because X"* so the gap is a decision
rather than an oversight. Do **not** close the successor until
`pytest tests/unit/stores/contracts/test_arcadedb_graph_contract.py` has a green run in a
workflow.

```files_allowed
.github/workflows/live-infra.yml
CLAUDE.md
```

**Acceptance:** the claim in `CLAUDE.md` and the claim in `live-infra.yml`'s comment agree
with each other **and** with the workflow's `services:` block.

---

### N4 · #365 shipped its recommended option and is still open · `selector-ready`

#365's own "cheapest and probably first" option — *have `analyze health` state the
assumption explicitly* — is implemented: `ServeAttributionReport.retrieval_availability_note`
at `src/trellis/ops/write_health.py:378`, populated at `:572`, rendered at
`src/trellis_cli/analyze.py:679`, and gated on `untargeted_feedback > 0` so a caveat that
always prints does not become one that always gets skipped. The two richer shapes are
recorded as **deferred** in `decision-ledger.md` F-4.

**Do:** close #365 naming the implementation site and the F-4 pointer. Per
`swarm-handoff.md` §2, grep the linked ledger entry for its own status line before
closing — F-4 is *deferred*, not *pending*, so nothing is being hidden.

**Acceptance:** #365 closed with a comment naming `write_health.py:378` and ledger F-4.

---

### N5 · #369's close condition is N3, and nothing records that · `selector-ready`

PR #530 (merged as `1ef5c9c`, current `origin/main`) states in its own body:
*"Refs #369. This PR does not close the issue while the ArcadeDB contract remains
unverified in live CI."* That condition **is** N3. With #351 closed and the contract still
unwired, #369's gate is now invisible from both ends.

**Do:** comment on #369 naming the exact blocking condition and linking N3's successor
issue, so the dependency lives on the open issue rather than in a merged PR body.

**Acceptance:** #369 carries a comment naming its close condition; a reader of #369 alone
can find the blocker.

---

### N6 · The CI-coverage claim is prose, and prose about what CI runs is the thing that rots · `agent`

`CLAUDE.md` carries ~20 lines describing which suites run in which workflow. It has been
wrong twice in three weeks — the `on: push` trigger (corrected by #401, the paragraph left
stale, then repeated into agent briefs where it made an agent *under-state its own
evidence*), and the ArcadeDB claim now pointing at a closed issue (N3). The paragraph even
tells the reader not to trust it: *"Re-read `live-infra.yml`'s `on:` block rather than
trusting this sentence."*

This repo already has the right answer to this shape, six times over:
`tests/unit/test_policy_gate_rule.py`, `test_format_exit_parity_rule.py`,
`test_chunk_visibility_rule.py`, `test_machine_output_rule.py`,
`test_subprocess_pythonpath_rule.py`, `tests/unit/mcp/test_capture_surface_roster.py`.
Each **derives** an invariant instead of asserting a roster, and each is guarded against
vacuity.

**Do:** add `tests/unit/test_ci_coverage_rule.py`. Parse the `pytest` invocations out of
every workflow, compute the set of test paths CI actually executes, and assert that every
file under `tests/unit/stores/contracts/` is either covered **or** named in an explicit
`DELIBERATELY_UNWIRED` map whose values are reasons. Fail when a contract file is in
neither.

**The three vacuity guards are the load-bearing part**, and this repo has burned itself on
each of them: a **floor** on paths discovered (a parser that silently finds nothing
satisfies every other assertion — #457's scanner dropped 148 branches to 123 with all
three of its guards green); a **synthetic workflow tree** run through the shipped parser
proving it fails when a contract goes unwired; and a **cross-check by a second method**
(a regex sweep of the same files, compared over line numbers, so a divergence names the
site it missed).

Do **not** assert today's roster. `#443` declared 3 control keys against 6 real sites;
that is the failure this replaces, not the one it should reproduce.

```files_allowed
tests/unit/test_ci_coverage_rule.py
CLAUDE.md
```

**Acceptance:** the rule fails on a synthetic tree with an unwired, undeclared contract
file; it passes on `main` only after N3 has made the two claims agree.

---

## Lane 1 — retrieval

Territory: `src/trellis/retrieve/`, `src/trellis/stores/*/vector*`, contract suites.
Does not collide with Lanes 2–4.

---

### R1 · #549 Part A is implemented and unlanded · `owner-only` (the push), `agent` (everything before it)

`get_file_context` returns empty for every file because **0 of 1,875 documents carry a
code path**. Part A gives it a producer: `build_trace_metadata` stamps
`parse_trace_evidence(trace).files_touched` onto the trace-summary document, and
`file_context` gains a second match key with `matched_via` on every returned row so a
consumer can tell *"this document is about that file"* from *"this document is a record of
changing it"*.

**State:** committed as `c2cde9e` on `feat/549-file-context-files-touched`, based on
`1ef5c9c` (current `origin/main`), 1 ahead / 0 behind. 9 files, +611 / −13.

Verification already run against current `main`: full default selection **7737 passed, 4
skipped, 683 deselected**; ruff check + format clean on 807 files; mypy clean on **340
source files** (count checked deliberately — a zero-file run also prints Success); the four
affected test files green under `FORCE_COLOR=1` on 3.11, guarding the #488 Rich/ANSI trap;
**17/17 mutants killed** across all four touched source files.

**Remaining:** push the branch, open the PR, run the simplify + review passes
(`swarm-handoff.md` §4.1), merge on a green run against whatever `main` is then.
**Outward-facing — owner go-ahead required.**

`files_touched` is **evidence-only** by #308's rule: a model's claim about what it
modified lives under `files_touched_unverified` and can never displace an evidence value.
Preserve that in review.

---

### R2 · #463 — chunk rows decay off the import clock · `agent`

The 148 imported conversations landed in one 72-second batch on 2026-08-07 against source
stamps spanning 28 months, so the row's `created_at` carries **zero** recency information
for them. #417/#462 fixed the parents on both axes. **Chunks inherit only
`CLASSIFY_METADATA_KEYS`, which does not carry the source stamp** — so a chunk of a 2024
conversation still scores as five weeks old. The chunk population is nearly as large a
slice of real retrieval as the parents: 147 further servings against the parents' 152, of
917 injected.

**Settle the question before touching the key set.** A chunk *is* the 2024 conversation,
sliced — but `CLASSIFY_METADATA_KEYS` is deliberately narrow, and widening a metadata bag
because one consumer wants a key is how #325/#326's two vocabularies drifted apart.

**Then sweep the roster, do not fix the one case.** Three successive `updated_at` reader
lists in this repo were each wrong, and #443 declared 3 control keys against 6 sites. Find
**every** derived-row path, not just chunking. A second producer is already known and
currently unexercised: the markdown ingest handler passes YAML frontmatter through flat and
reserves neither `updated_at` nor `created_at`, so a note carrying either becomes a source
stamp — **zero production rows today**, so any fix must not assume conversation-ingest is
the only provenance.

**Counter-evidence that must be weighed, not skipped:** the stamped parents are the
*worse*-cited half — P(cited helpful | served) **0.015** for parents against **0.081** for
their chunks and **0.123** for the rest of the corpus. Propagating the stamp promotes the
half that grades worse. #417 left this alone for exactly that reason. If the fix ships
anyway, say why in the PR body.

```files_allowed
src/trellis/retrieve/strategies.py
src/trellis/core/vector_metadata.py
src/trellis/ingest/
tests/unit/retrieve/
```

**Acceptance:** a roster test enumerating derived-row producers, not a patch to the chunk
path alone; the decision recorded either way.

---

### R3 · #494 — `retrieve pack --quiet` changed what a shell pipeline receives · `decision`

#488 routed `--quiet` through `PackBuilder`, so it now emits chunk fragment ids
(`<parent>#chunk-N`) and entity ids alongside document ids. `--quiet` is the arm built for
`xargs` and `while read`; its whole value is that the id it prints can be fed to another
command, and a fragment id is a worse handle than its parent (#402 recorded exactly this
when excluding chunks from MinHash seeding).

Three options are on the issue. **The first is probably right** — `--quiet` reports what
the pack served, which is now true and consistent with every other pack surface — but it
was arrived at silently, and it should be a decision.

**Do:** record it in the ledger with a recommendation, and state the population in
`docs/agent-guide/operations.md` regardless of which way it goes. The doc gap is real
under all three options.

```files_allowed
docs/agent-guide/operations.md
docs/design/decision-ledger.md
```

---

### R4 · #375 — the graph axis has no query-relevant candidate path · `agent`

The unseeded branch is `ORDER BY created_at DESC LIMIT n` on every shipped backend, and
**production always takes it**: `build_strategies` injects no extractor and nothing in the
repo supplies `seed_ids`. The reachable set is a fixed row count, so coverage decays as
1/N — measured at a median **8.6%** of servable nodes over a median 58-hour window, falling
monotonically 0.150 → 0.072 as the graph grew 286 → 665 nodes. It gets worse every day the
system remembers anything.

Ledger **T-4** already decided the shape: option 2 is *a one-kwarg stamp, not a write-path
redesign*. #530 supplies the alias substrate. This item is the **retrieval** side.

**Two traps, both already paid for.** Counterfactual replay **cannot** evaluate this —
`pack_replay` re-walks `budget_trace[]`, which records the candidates the walk *saw*, and
a seeding change alters which candidates exist; both arms have to actually run against the
real stores. And the graph axis currently measures *best* (`useful_token_fraction` 0.1744
vs semantic 0.1069, keyword 0.0241), so a fix can regress the densest axis in a pack — but
that number is **confounded by construction** and is not evidence that recency is a good
relevance proxy: within the window, age does not separate outcomes at all (cited-helpful
median 13.1h n=16, uncited 12.7h n=53).

**Refused, do not re-propose:** scanning the whole node table per pack and matching names
client-side (O(N) per pack, hides the same silent-truncation failure one layer up); and
auto-wiring `SemanticSeedExtractor` when an embedder resolves (**measured**: 0 seeds on
37/37 real production intents, 0/37 packs changed — a change that reports success and does
nothing).

```files_allowed
src/trellis/retrieve/strategies.py
src/trellis/retrieve/builder_factory.py
tests/unit/retrieve/
```

---

### R5 · #364 — 42% of injected tokens get no verdict · `decision` then `agent`

`useful_token_fraction` is computed over 58% of the tokens it describes: on the 30 days to
2026-08-27, cited helpful 8.8%, cited unhelpful 49.2%, **no verdict at all 42.0%**. The
unjudged bucket is not a random 42% — graders cite what helped and what got in the way, and
never enumerate what they ignored, so it is systematically *the population a trimming policy
most wants to characterise*. Two readings stay live and this data cannot separate them: the
items were read and were neutral, or they were never read at all.

`pack_value` already refuses to fold it into the "not helpful" residue, correctly.

**Do:** decide between the two shapes on the issue — an explicit third verdict
(`ignored_item_ids`), or shrinking the denominator by having MCP `record_feedback` hand
back the served ids and ask for a verdict on each (`TRELLIS_REQUIRE_PACK_ATTRIBUTION`
already does the adjacent thing). Ship the capability **off**, so the operator's answer is
a config change.

**Blocks:** any cut deeper than #359's. Do not let a trimming change land against this
number until it is resolved.

```files_allowed
src/trellis/schemas/feedback.py
src/trellis/feedback/
src/trellis/mcp/server.py
src/trellis/core/write_config.py
tests/unit/feedback/
```

---

## Lane 2 — feedback and learning

Territory: `src/trellis/feedback/`, `src/trellis/learning/`, `src/trellis/ops/write_health.py`.

---

### F1 · #502 — an advisory outside the delivery cap can never be scored, so it can never be suppressed · `decision`

`analyze_advisory_effectiveness` builds presentations from
`PACK_ASSEMBLED.payload["advisory_ids"]` — from what was **served**. #499 caps that set. So
a matching advisory outside the cap accumulates no presentations, is never scored, and is
structurally unable to be suppressed, while the nightly generator keeps minting rows. 44 of
56 stored rows matched an undomained pack at the time of #499; the cap serves a fraction.

**Not a regression** — before 2026-08-31 zero packs carried advisories, so nothing was
scored either. What changed is the shape: from *nothing is scored* to *a bounded subset is
scored and the rest accumulates monotonically in exactly the population the loop cannot
reach*. A slow failure with no signal attached is the class this repo keeps paying for.

Option 2 (score on match for the suppression half, keep served for the value half) is
probably right, but it is a **measurement-semantics change**, and the two denominators
getting confused later is precisely the #336 / `pack_attribution_rate` failure. Write the
ledger entry with that risk named.

**Decide together with** the audience question still open from #499: the highest-confidence
active advisory tells an agent that packs using the `keyword` strategy succeed less often,
and *an agent cannot select strategies*. If a large fraction of the corpus is addressed to
the wrong reader, "cannot be scored" matters less than "should not have been generated".

```files_allowed
docs/design/decision-ledger.md
```

---

### F2 · #503 — the advisory cap ranks category-blind · `do-not-do`

`ENTITY` advisories are the only category stamping per-item provenance, and they compete
in one global confidence ranking against `APPROACH` rows. Real, and **inert**: all 44
matching rows are `approach`, the 12 `anti_pattern` rows are suppressed, so there is no
category competition to lose.

The issue says it plainly: *"Do not do this until it has a subject. A per-category
reservation tuned against a corpus with zero entity rows would be a constant wearing a
policy's clothes."* **The trigger is the first `entity` advisory reaching a pack.** Until
then this is a recorded hazard, not work. It is in this corpus so that nobody lifts it out
of the issue list as available.

---

### F3 · 22 of 56 advisory rows are pre-#383 duplicates carrying a refuted claim · `owner-only`

Minted one fresh ULID per night from 2026-08-08 to 08-30, each still asserting the refuted
`vs 0% without`. Production data cleanup — outside the contract at any confidence. It is a
large part of what would otherwise sit unscored forever under F1, so the two are worth
deciding together even though only one is executable here.

---

## Lane 3 — mutation and policy

Territory: `src/trellis/mutate/`, `src/trellis/core/`.

---

### M1 · #360 — govern the document and vector planes · `agent` · **the top unblocked feature item**

The hard rule *"all mutations go through the governed pipeline"* does not hold for the
document and vector planes. Panel-decided **unanimous, option B**, recorded as ledger
**T-3**; implementation not started. #357's worker-local handler is the natural seed.

Its one-time caveat is satisfied: C1 merged `1e6c66e`, so Stage 2 runs on every surface and
a governed document/vector write would actually be policy-checked. **Nothing else in the
queue blocks it.**

Read T-3 in full before starting — it is the decision, and the panel's reasoning is the
spec.

```files_allowed
src/trellis/mutate/
src/trellis/core/
tests/unit/mutate/
```

---

## Lane 4 — extraction and workers

Territory: `src/trellis/extract/`, `src/trellis_workers/`.

---

### E1 · #264 — two of five judged stages emit no training example · `selector-ready`

Already carries `mechanical` + `ready`. Every judged memory op has the shape of a labeled
example — *(input context, decision, downstream outcome via feedback attribution)* — and
**the dataset accrues now or never**: retrofitting labels onto un-logged history is
impossible.

**Verified 2026-09-09.** `JudgedOpType` declares five members. Three have emitters:

| stage | emitter |
|---|---|
| `DISTILLATION` | `src/trellis_workers/session_capture/distill.py:354` |
| `CLASSIFICATION` | `src/trellis/classify/shadow.py:779` |
| `RECONCILIATION` | `src/trellis_workers/session_capture/reconcile_pass.py`, `src/trellis/mcp/reconcile.py` |
| `EXTRACTION` | **none** |
| `CURATION` | **none** |

**Do:** add the two missing emitters, one fixture per stage. Payload carries op type, model
id, leak-safe input digest (content refs, never raw prompt dumps of restricted items),
decision, confidence. Follow `shadow.py:770`'s emitter exactly — it is the reference
implementation and it already solves the leak-safety question: it records digest, verdict
label and a subject pointer, and **never** the open-vocabulary `domain` tags, because those
reveal subject matter and stay behind the same access path as the content.

Export tooling lives in **trellis-evals**, not core. Core only emits.

```files_allowed
src/trellis/extract/
src/trellis_workers/
tests/unit/extract/
tests/unit/workers/
```

**Acceptance:** a fixture per stage proving the emit; `DataClassification`-restricted
content never appears raw in a payload.

---

### E2 · #306 — observer-agent auto-capture at tool-use granularity · `agent`

Capture density is the dogfood deficit the claude-mem audit named as the real source of
that system's felt performance. Sized larger than one sitting; take it only after Lane 0.

---

## Lane 5 — CI and test integrity

---

### C1 · #525 — `pytest.importorskip` is a second, unmarked way to run in no workflow · `agent`

`CLAUDE.md`'s test-coverage caveat covers exactly one mechanism, marker deselection. A
marker is visible: it is on the test, it is in `addopts`, and a reader has something to
grep. `importorskip` has none of that — no marker, so the test *looks* like default
coverage; it skips rather than fails, so nothing is red; and the gate is written down only
inside the test file.

**~246 tests are `importorskip`-gated and execute in no workflow, ~103 of them carrying no
marker at all.** It caused two findings in two days (#512/PR #524, and #517's three
strongest tests silently skipping because `dev` does not include `llm-anthropic`).

**Do:** make the mechanism visible. The durable form is a rule test — every
`importorskip`-gated test is either reachable in some workflow's install set, or carries a
marker naming why not. Same three vacuity guards as N6.

```files_allowed
tests/unit/test_importorskip_rule.py
CLAUDE.md
pyproject.toml
```

---

### C2 · #356 — sweep `tests/unit/stores/` into CI, behind the capability probe · `agent`

`live-infra.yml` names paths, not markers, so 59 Postgres-marked tests under
`tests/unit/stores/` outside `contracts/` have never been run by CI. They pass; nothing has
ever run them.

The obvious fix does not work: `test_neo4j_vector.py::TestQuery` issues
`SEARCH ... IN (VECTOR INDEX ...)`, AuraDB-grade Cypher that self-hosted `neo4j:2025.12`
cannot parse — 4 failures, verified locally against the same image. The e2e suite already
solved this with the session-scoped `neo4j_vector_search_supported` fixture in
`tests/integration/conftest.py`, which runs the production query verbatim against a
throwaway index name and reads an index-resolution error as *supported* and a parse error
as *not supported*. This unit file has no such probe.

**Do:** give the unit file the same probe, then widen the workflow path.

```files_allowed
tests/unit/stores/test_neo4j_vector.py
tests/unit/stores/conftest.py
.github/workflows/live-infra.yml
```

**Interacts with N3** — both edit `live-infra.yml`. Sequence them; do not run in parallel.

---

### C3 · Find the next uniform-pool blind spot before it ships · `agent`

#447 and #456 are the same defect twice: **eleven of twelve `item_type` / `relevance_score`
mutants survived the full 6,468-test default selection**, across six sites each hand-copying
four fields off a `PackItem`. Two fixture shapes hid them, and both generalise:

- **a population sized 1** — the sectioned meta-Activity count test used one dropped and one
  kept item, so `len(dropped) == len(deduped)` and no assertion could separate them. Its
  flat twin used two, which is why the same mutant died there;
- **a pool too uniform to distinguish the field under test** — every fixture item was
  `item_type="document"`, so hard-coding `item_type` survived everything.

**Do:** sweep `tests/unit/` for both shapes mechanically — fixtures with a single-element
pool where a count assertion is the only check, and fixtures where every element shares the
value of a field the code under test reads. Report the candidates ranked by how load-bearing
the field is. **Do not** mass-edit fixtures; the deliverable is the ranked list plus mutants
proving the top three.

`/tmp/…/scratchpad/mutants.py` (17 mutants, all killed) is the working harness pattern.
**Trap:** `make openapi-check` is `git diff --exit-code`, so an uncommitted spec
regeneration makes every mutant report as killed. Commit the spec first.

---

## Lane 6 — owner-only

Queued for visibility. **Not executable under the autonomy contract at any confidence.**

| # | Item | Why it is owner-only |
|---|---|---|
| O1 | **#546 steps 0–4** — production runs a commit (`2e62ffe`) that exists on no remote branch; rebase + PR, `git pull --ff-only`, container rebuild, redeploy-log entry | publishing + prod mutation. Step 5 (the drift-detector fix) is already committed as skynet-hub `7e15430` |
| O2 | **#549 Part B** — run `trellis-skynet worker embed-traces` once against prod, add it to nightly cron, re-probe `get_file_context` and report the hit rate against the 199 existing traces | prod mutation + cron |
| O3 | **#547** — the Stop hook nudge rewards non-retrieval: never retrieving is the zero-ask path | `~/.claude/hooks/`, outside the repo |
| O4 | **#548** — `retrieve-before-task` documents one of eight retrieval mechanisms and its trigger fires only at session start | `~/projects/my-claude-skills`; **waits on R1** for its `get_file_context` section |
| O5 | Board #275 membership for #546–#549 | needs `gh auth refresh -s project`, interactive |
| O6 | **#250** — AuraDB live-test credential hygiene | credential operation |
| O7 | **#208** — direct-write BI metadata smoke | blocked on a missing ArcadeDB secret and expired AWS SSO |

**O1 is the one with a compounding cost.** Three builds run against one database — host
editable install (working tree = production for the host CLI *and* the stdio MCP), plus two
containers. Every day the gap widens, every production measurement read in this corpus gets
harder to attribute. The `TRELLIS_BUILD_VERSION` export is the step that gets skipped,
because the build succeeds without it and reports `version_source: fallback-version`,
`commit: null` — an honestly unidentifiable deploy. Verify the stamp equals repo HEAD after
every deploy; a healthy container is not evidence of an identifiable one.

---

## Blocked or deferred — not in this corpus, and why

| Item | Status |
|---|---|
| #194 classification enforcement | `keystone` + `blocked:dep` + `blocked:owner-decision` |
| #200 / #202 / #203 query-history | `blocked:owner-decision` |
| #201 BI metadata | `blocked:signal` |
| #256 Bolt plugin extraction | `keystone` — halves #194's enforcement surface, so it precedes it, but it is not autonomous work |
| #257 ingest normalization ADR | `owner-only` |
| #261 promote-to-standing advisory | `blocked:signal` |
| #474–#478 | `blocked:owner-decision` — the OpenAI-contract set; two of the original six proposals were already stale when filed |
| #514 structured outputs / #515 prompt caching | deferred from #500; #515 is explicitly *measure first, then decide whether to build* |
| #405 | a meta-issue enumerating environment debt, not an item |
| #275 | the board, not work |

---

## Suggested execution order

Lane 0 entirely, in order N1 → N2 → N3 → N4 → N5 → N6. It is a few hours, it is all
reversible, and **N3 is a live finding a day old**.

Then, by lane, in parallel where territories do not collide:

- **retrieval** — R3 (cheap decision + doc) → R2 → R5 → R4
- **feedback** — F1
- **mutation** — M1
- **workers** — E1
- **CI** — C1 → C2 (after N3) → C3

R1 and every Lane 6 item wait on an operator.

## Execution log

Appended as items are executed. **Read this before picking up an item** — three of the
first six turned out to be stale against the repo, which is the corpus's own warning
applied to itself.

| Item | Outcome |
|---|---|
| **N3** · #351 ArcadeDB gap | **Refuted.** The premise was `grep -rl arcade .github/workflows/` returning one comment-only hit. It is wrong: `live-infra.yml` has had an `arcadedb` service container since #543 (2026-09-08), and the contract runs through `contracts/`. #351 is correctly closed. This is the refutation the corpus itself said to check for first. |
| **N4** · #365 | Tracker-only — the recommended option shipped; nothing to build. |
| **N5** · #369 close condition | **Refuted** with N3, which it depended on. |
| **N6** · CI-coverage prose | **Done — [#583](https://github.com/ronsse/trellis-ai/pull/583).** Scope grew on measurement: the prose was wrong in *both* directions. The ArcadeDB claim was stale (above), and **99 tests across eight files really did run in no workflow leg** — both ArcadeDB store suites, the Neo4j graph and connectivity suites, `test_migrate_graph_live.py`, and the three `live_api_server` suites. All eight are now wired, each measured against this workflow's own images first. The durable half is `tests/unit/test_ci_coverage_rule.py`, which *derives* the join (a leg's `pytest` selects the path **and** its `TRELLIS_TEST_*` env covers every gating marker) instead of declaring a roster. CI on #583: all 99 pass. |
| **C2** · #356 capability probe | **Already built — [#552](https://github.com/ronsse/trellis-ai/pull/552), open and unmerged.** Not open work. The corpus item was written against `main` and is stale; found in minutes by re-measuring the premise before executing it. #552 is the better answer than N6's exclusion of the same file: it removes the private `index_name=` override so the suite takes the production default, which kills the vector-index collision at source rather than by exclusion. **It conflicts with #583** in three files; the verified resolution is on `merge/n6-plus-356` (`cebc404`) and is written out on #583's body. |

**A standing hazard this surfaced.** `main` is red on `live-infra` — #570's
`test_bind_alias_if_absent_is_atomic_for_concurrent_contenders`, reproduced **four times
with the identical record id `#9:0`** (#552's CI, #582's CI, #583's CI, and a local run),
on top of `main`'s own last `live-infra` run failing at `1ef5c9c4` (#530, 2026-09-08) and
every run since being a PR. On this evidence it is a hard red, not a flake, and it dates
to #530 — the change that introduced the alias lifecycle the race is on. **Every open PR
inherits it.**

**There are two independent fixes for it, and only one should merge.**
[#555](https://github.com/ronsse/trellis-ai/pull/555) (2026-09-11) and
[#571](https://github.com/ronsse/trellis-ai/pull/571) (2026-09-12) were built a day apart
without either author seeing the other; both are green on `live-infra`, based on `main`,
MERGEABLE, and land on the same file (`src/trellis/stores/bolt_opencypher/graph.py`) with
the same design — a module-level attempt bound of 3, a message-matched contention
predicate, a retry around the alias write. **Merging both is worse than merging either**,
and #571's body argues it "should go first" without mentioning that #555 existed and was
already green. This corpus's own rule caught it: `gh pr list --state open` is part of an
item's premise, because unmerged work leaves no trace in `git log`.

**Recommendation: merge #555.** It covers a second ArcadeDB failure shape the race also
produces (`Record #\d+:\d+ not found`, via `_STALE_RECORD_RE`), and it *measures* and then
deliberately excludes a third — the deadlock shape, where the driver's own managed retry
already re-runs the transaction, shown identical across 120 live races with and without the
new retry. It also measures Neo4j 20 times to document its own branch as defensive rather
than load-bearing (Neo4j's `MERGE` locks the index entry, so the loser blocks and reads the
committed claim; 0 raises with and without). Its predicate matches the constraint's **own
identity** — the `AliasClaim` label and `claim_key` property as whole tokens, checked
against the shipped DDL — with five parametrized exclusions including the
`AliasClaimArchive` prefix trap.

**What #571 has that #555 does not**, and the follow-up worth taking: #571 additionally
requires a unique-violation marker (`"duplicated key"` / `"already exists"`) in the message,
so an unrelated driver error that happens to name both tokens is not retried. That is a
latency and log-noise difference rather than a correctness one — #555 re-raises `last_error`
after exhausting `_ALIAS_CLAIM_RETRY_ATTEMPTS = 3`, so no error is ever swallowed — but the
conjunct is cheap and strictly better. **After merging #555, add #571's marker conjunct to
`_is_alias_claim_contention` and close #571 with a pointer to both.**

---

## What would refute this corpus

Every claim above with a number or a `path:line` was checked on 2026-09-09 against
`origin/main` = `1ef5c9c`. Three carry more risk than the rest:

- **N3** rests on `grep -rl arcade .github/workflows/` returning one file whose only hit is
  a comment. Re-run it; a service container added after this was written refutes the item
  outright.
- **R2 and R4** quote production measurements taken over rolling windows. `swarm-handoff.md`
  §1.3 is right that these must be re-derived and never transcribed — including from here.
- **E1's** emitter table is a `grep` over `src/`. A stage emitting through an indirection
  the grep missed would make it a two-item list, not four.

**Before implementing against any number in this document, verify the number can move.**
Query it, and check it can return more than one answer. That is what caught every
substantive finding this repo has made; the panel caught none of them.
