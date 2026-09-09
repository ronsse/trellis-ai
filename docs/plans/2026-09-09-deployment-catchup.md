# Plan 1 — Bring the deployment back onto `origin/main`

> **Nightly TODO.** Tracked as a trellis-ai issue on board #275. **Not** eligible for the
> autonomous code-authoring loop: it mutates production containers and rewrites branch
> state, neither of which `git revert` undoes, so it sits outside the autonomy bound in
> `~/.claude/CLAUDE.md` ("authority is bounded by reversibility"). The nightly *reports*
> it; a human runs it.

**Repo:** `ronsse/trellis-ai` (+ `~/projects/skynet-hub/stacks/trellis` for the rebuild).

## Verified premise (measured 2026-09-09)

| Surface | Runs | Evidence |
|---|---|---|
| `origin/main` | `1ef5c9c4f` — *fix(graph): govern atomic name alias lifecycle (#369) (#530)* | `git rev-parse origin/main` after fetch |
| local `main` | `4349dbb2c` — **1 ahead, 42 behind** | `git rev-list --left-right --count main...origin/main` → `1  42` |
| working branch `ui/sidebar-and-table-interaction` | `2e62ffe52` — **2 ahead, 42 behind** | same, against `HEAD` |
| `trellis-api` + `trellis-mcp` containers | image created **2026-09-03T03:28:26Z**, stamped `commit: 2e62ffe52`, `dirty: false`, `version_source: dist-metadata` | `GET /api/version` with the LAN key; `docker image inspect` |
| host CLI + stdio MCP | the **working tree**, whatever branch it is on | `~/.local/bin/trellis-skynet` → `exec ~/projects/trellis-ai/.venv/bin/trellis` (editable install) |

Three facts follow, and two of them are new:

1. **Production runs code that exists on no remote branch.** `2e62ffe52` is the tip of a
   local-only UI branch. The compose build context is the working tree
   (`context: /home/nronsse/projects/trellis-ai`), so the 2026-09-03 build baked in
   whatever was checked out — exactly the exposure `stacks/trellis/drift-and-redeploy.md`
   warns about under *"Check `git -C ~/projects/trellis-ai status` before every build."*
2. **That build was never logged.** `drift-and-redeploy.md`'s redeploy log ends at
   *2026-09-02 — both containers rebuilt from `main` @ `4349dbb`*. The 2026-09-03 rebuild
   from `2e62ffe` has no entry, so the runbook's own record of what is deployed is wrong.
3. **The two orphan commits are trivially rebasable.** They touch exactly one file —
   `src/trellis_api/static/index.html`, +921/−47 — and **zero** of the 42 upstream commits
   touch it (`git log --oneline main..origin/main -- src/trellis_api/static/index.html`
   is empty). `git merge-tree` reports **0 conflicts**.

What the 42-commit gap costs is bug fixes, not capability: the MCP tool surface is
unchanged across them (`mcp_tools_version: 1` on both sides). 12 of the 42 touch
`src/trellis_api` or `src/trellis/mcp`, so a rebuild is required by the runbook's own
rule.

**Fourth finding, and it is the one that will bite a script:** `op read` returns an
**empty string** in a non-interactive shell that has not exported
`OP_SERVICE_ACCOUNT_TOKEN` (the `.bashrc` auto-load does not reach the agent Bash tool or
cron's shell). `curl -H "X-API-Key: "` then answers **200** with `write_provenance: null`,
which is the anonymous response — so the runbook's verification command degrades to
"prints nothing / tracebacks on `None`" rather than failing loudly. This is the same shape
already recorded once in the 2026-09-02 log entry (*"turning auth on silently breaks every
unauthenticated probe"*), reached by a different route. Reproduced 2026-09-09: token
unset → key length 0; token exported from `~/.config/op-service-token` → key length 67 and
the stamp reads back.

**Fifth finding — the drift detector has never been able to fire.** Both the runbook's
rebuild rule and the nightly DoD-9 metric ask the same question with the same two paths:

```
git log --oneline --since="$IMG_CREATED" -- src/trellis/api src/trellis/ui
```

`drift-and-redeploy.md:28` and `skynet-hub/stacks/trellis/roadmap-nightly.sh:121`. **Neither
path has ever existed in this repository** — `git log --all -- src/trellis/api` and
`-- src/trellis/ui` each return **0 commits over all history**. The real packages are
`src/trellis_api` and `src/trellis/mcp`. So `container_dod9.api_ui_commits_since` has
reported `0` in every nightly status block since the reporter was written
(skynet-hub `e67bfe2`, 2026-08-02) — not because the containers were current, but because
the question addressed nothing. Measured today with the corrected paths the same query
returns **12**. This is the repository's recurring failure mode (*a measurement path wired
to a constant*) in the one instrument that was supposed to catch the drift this plan is
about: the metric read `0` on 2026-09-03 while a build from a local-only branch went out,
and read `0` every night since.

## The change

### Step 0 — assert the probe can actually fail (2 min)

```bash
export OP_SERVICE_ACCOUNT_TOKEN="$(cat ~/.config/op-service-token)"
K="$(op read 'op://Agent Secrets/Trellis-LAN-Browser/credential')"
[ ${#K} -gt 0 ] || { echo "FATAL: op read returned empty — fix before deploying"; exit 1; }
```

Add exactly that guard to `stacks/trellis/drift-and-redeploy.md` above the verification
block. A probe that cannot distinguish "wrong deploy" from "no credential" is not a probe.

### Step 1 — land the UI work upstream (no conflicts expected)

```bash
cd ~/projects/trellis-ai
git switch ui/sidebar-and-table-interaction
git rebase origin/main            # 0 conflicts predicted by git merge-tree
make lint && make test            # local suite deselects 635 cloud-backend tests — see CLAUDE.md
gh pr create --base main --title "feat(api/ui): collapsible sidebar, resizable columns, labelled row disclosure"
```

Both commits are UI-only, so the PR is reviewable on one file. Merge through CI rather than
fast-forwarding locally — `live-infra.yml` runs on `pull_request` since #401, and this is
the cheapest way to keep the branch honest.

### Step 2 — reset local `main` to the remote

After the PR merges, `git switch main && git pull --ff-only`. Local `main`'s single ahead
commit (`4349dbb2c`) is an ancestor of the branch in step 1, so it arrives with the merge
and nothing is orphaned. **Do not** `git reset --soft origin/main` — see the trap recorded
in `7a3739b docs(design): trap — git reset --soft origin/main can revert merged work`.

### Step 3 — rebuild both containers from a clean `main`

Follow `stacks/trellis/drift-and-redeploy.md` verbatim; the two steps that get skipped are
the `TRELLIS_BUILD_VERSION` export and the post-deploy stamp comparison.

```bash
cd ~/projects/trellis-ai && git status --porcelain    # MUST be empty, on main
export TRELLIS_BUILD_VERSION=$(git describe --tags --abbrev=9 --dirty=.dirty --always \
  | sed -E 's/^v//; s/-([0-9]+)-g/.dev\1+g/')
cd ~/projects/skynet-hub/stacks/trellis
op run --env-file=op.env -- docker compose build api mcp
op run --env-file=op.env -- docker compose up -d api mcp
```

Verify — the stamp must equal repo `HEAD`, not merely be non-null:

```bash
export OP_SERVICE_ACCOUNT_TOKEN="$(cat ~/.config/op-service-token)"
K="$(op read 'op://Agent Secrets/Trellis-LAN-Browser/credential')"; [ ${#K} -gt 0 ] || exit 1
curl -s -H "X-API-Key: $K" http://localhost:8420/api/version \
  | python3 -c 'import json,sys; wp=json.load(sys.stdin)["write_provenance"]; print(wp["commit"], wp["dirty"], wp["version_source"])'
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8420/api/v1/stats          # 401 anonymous
curl -s -o /dev/null -w '%{http_code}\n' -X GET http://127.0.0.1:8421/mcp             # non-5xx (405 expected)
```

Rebuild **both** containers even though only `api` strictly needs it — the runbook's own
lesson from 2026-08-02 is that three code versions against one database is the drift mode
that silently invalidates other fixes.

### Step 4 — write the log entry that is missing

Append two entries to `drift-and-redeploy.md`: a retrospective one for **2026-09-03 —
`2e62ffe`, built from a local-only branch, unlogged**, and one for this deploy. The
retrospective entry is the point: the log is only useful if it records the deploys nobody
meant to make.

### Step 5 — fix the drift query in both places (the detector, not the deploy)

Two files, same edit — `src/trellis/api src/trellis/ui` → `src/trellis_api src/trellis/mcp`:

- `~/projects/skynet-hub/stacks/trellis/roadmap-nightly.sh:121` (the DoD-9 metric)
- `~/projects/skynet-hub/stacks/trellis/drift-and-redeploy.md:28` (the rebuild rule)

Then prove the corrected metric can be non-zero *and* can be zero, since a constant is
what is being replaced: run it against the pre-rebuild image (expect **12**) and again
after Step 3 (expect **0**). A metric that only ever reads `0` is indistinguishable from
this bug; both readings are the acceptance test.

## Measurement

- `GET /api/version` `write_provenance.commit` == `git rev-parse --short=9 HEAD` on `main`.
- `git rev-list --left-right --count main...origin/main` == `0  0`.
- No branch other than `main` checked out in `~/projects/trellis-ai` at rest (the host CLI
  and stdio MCP run it).
- The nightly `container_dod9.api_ui_commits_since` reads **12** before the rebuild and
  **0** after — the first time that field has carried information.

## Non-goals

- No change to the `:8420` bind posture (skynet-hub #5, tracked separately).
- No pinning of the host venv to a tag. The live-venv design is a known, accepted risk
  documented in the runbook; changing it is a separate decision.
- No dependency upgrade. `uv sync` only if the 42 commits changed `pyproject.toml`.

## Risks

- **Building from a dirty or non-`main` tree.** Step 3's `git status --porcelain` guard is
  the whole mitigation; it has already been missed twice.
- **Forgetting `TRELLIS_BUILD_VERSION`.** The build succeeds without it and reports
  `version_source: fallback-version`, `commit: null` — an honestly unidentifiable deploy.
- **Switching branches mid-session breaks live agents.** The editable install means any
  Claude Code session running on this box picks up whatever `src/` says. Do steps 1–3 in
  one sitting.
- **Postgres.** Never rebuilt with app changes; leave `trellis-postgres` alone (9 days
  uptime at time of writing).
