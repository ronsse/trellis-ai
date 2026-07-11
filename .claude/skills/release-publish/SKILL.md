---
name: release-publish
description: >
  Cut and publish a trellis-ai release to PyPI via the tag-triggered GitHub
  workflow. Use when asked to "cut a release", "publish to PyPI", "tag a
  version", or "ship vX.Y.Z". Do not use for refreshing the skynet
  deployment — that is the skynet-hub stack rebuild (see step 6), which this
  skill only reminds about.
---

# Release trellis-ai to PyPI

Versioning is hatch-vcs: the version derives from the git tag — there is no
version field to bump. The `v*` tag push IS the release trigger.

1. **Preflight** — clean tree on `main`, up to date with origin, CI green:
   ```sh
   git status --porcelain          # must be empty
   git pull --ff-only
   gh run list --branch main -L 4  # Tests/Lint/Type Check green on HEAD
   ```

2. **Local artifact sanity** (optional but cheap):
   ```sh
   make publish-check              # python -m build + twine check dist/*
   ```

3. **Pick the version** — semver against the last tag:
   ```sh
   git describe --tags --abbrev=0  # e.g. v0.9.0
   ```
   Scan `git log <last-tag>..HEAD --oneline` for breaking changes (major),
   features (minor), fixes only (patch). Confirm the number with the owner
   if it is a major bump.

4. **Tag and push** — this triggers `.github/workflows/publish.yml`
   (test matrix py3.11–3.13, then OIDC trusted-publish, environment `pypi`):
   ```sh
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

5. **Watch and verify**:
   ```sh
   gh run watch $(gh run list --workflow publish.yml -L 1 --json databaseId -q '.[0].databaseId')
   pip index versions trellis-ai   # new version listed
   ```

6. **Remind about the skynet deployment** — publishing does NOT update the
   running instance. The `trellis-api` container builds from the local
   checkout and drifts (see
   `~/projects/skynet-hub/stacks/trellis/drift-and-redeploy.md`); offer the
   rebuild as a follow-up. The host-side CLI/MCP track the working tree
   automatically (editable venv) — nothing to do there.

## Gotchas

- The tag must point at a commit already on `origin/main` — tagging an
  unpushed commit publishes something CI never saw.
- Re-running a failed publish: use the workflow's `workflow_dispatch` with
  `ref` set to the tag (a bare re-run of a tag push can lose the ref).
- hatch-vcs needs full history (`fetch-depth: 0` — already set in the
  workflow); a shallow local build shows a `.devN` version, which is normal
  locally and wrong only if it appears in CI.
