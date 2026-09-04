# Shared brief — trellis-ai autonomous work (read first, every agent)

## Environment
- Repo: github.com/ronsse/trellis-ai (PUBLIC). Base every branch on a freshly fetched `origin/main` (f9ff32c when these plans were written; `git log --oneline -30` to see what moved since).
- Use a clean clone/worktree per PR. Python 3.11 matches CI. `pip install -e ".[dev]"` (NEVER `uv run` — it rewrites uv.lock).
- Tests: `python -m pytest tests/ -q -p no:cacheprovider`. Local `make test` deselects postgres/pgvector/neo/arcadedb/live/slow (~635 tests); CI's live-infra.yml runs the Postgres/Neo4j contracts on pull_request AND push.
- Do NOT run plain and FORCE_COLOR=1 pytest concurrently (shared /tmp/pytest-of-* GC collides). Use `--basetemp` if you must run two.
- Full-suite baseline at f9ff32c: ~7428 passed; establish your own baseline on your branch base before claiming a delta.
- Lint/type: `make lint`, `make typecheck` (mypy src/).
- Plans live in docs/handoff/2026-09-04/plans/<n>.md; issues via `gh issue view <n> --repo ronsse/trellis-ai --comments`.
- Never write to a live `~/.trellis/` on any machine that runs a Trellis deployment; never point TRELLIS_TEST_PG_DSN at a production database.

## Content rules (public repo)
- No personal corpus content, no production document text, no secrets in commits/PRs/issues. Counts and ratios are fine.
- Secrets only via 1Password (`op read`). Never hardcode.
- Follow CLAUDE.md at repo root: structlog not print, `extra="forbid"`, governed mutations only, exit codes independent of --format, type hints on public APIs.

## House standards the review gates enforce (the gates have returned zero clean MERGEs this session — build for them)
1. The load-bearing claim of the PR must be pinned by a test that FAILS when the claim is false (mutation-test it; report survivors honestly).
2. No assertion satisfiable by a constant. No fixture population too uniform to distinguish the field under test (size ≥2, mixed field values).
3. Docstrings must not assert behaviour the code lacks.
4. Rosters/floors derived from a scan must be pinned back to a hand-read floor (`assert_hand_read_floor` in tests/ast_rules.py pattern) and proved non-vacuous on a synthetic tree.
5. Measure before and after on real data where the change is about behaviour on data; state the window and n; never trust a figure in prose.
6. Prefer emitting an event over adding a probe; prefer one shared seam over per-site copies.
7. Smallest change that removes the defect class, not the instance. But do not widen scope beyond the issue.

## Irreversible = escalate, never do
Publishing packages, deleting/redacting production data, credential ops, spend, force-push/history rewrite, repo settings, dependency-surface changes to what gets *published*. CI workflow edits and code are reversible and in scope.
