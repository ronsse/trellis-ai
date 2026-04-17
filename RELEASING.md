# Releasing Trellis

This is the runbook for cutting a new `trellis-ai` release to PyPI. Follow it top to bottom.

## One-time setup (do this once, then forget)

1. **Create a PyPI project + register the trusted publisher.**
   - Go to https://pypi.org/manage/account/publishing/.
   - Add a "pending publisher":
     - PyPI project name: `trellis-ai`
     - Owner: `ronsse`
     - Repository: `trellis-ai`
     - Workflow filename: `publish.yml`
     - Environment: `pypi`
   - The first publish will reserve the project name and finalize the trust relationship.
2. **Create the GitHub `pypi` environment.**
   - Repo → Settings → Environments → New environment → name `pypi`.
   - (Optional but recommended) require a manual approval reviewer so prod publishes are gated.
3. **Confirm `id-token: write` works.** The publish workflow already sets this — no per-release action needed.

## Per-release steps

### 1. Pre-flight (local)

```bash
make check          # lint + typecheck + tests
make verify-wheel   # builds + lists what's in dist/*.whl and dist/*.tar.gz
make publish-check  # twine check on the artifacts
```

Eyeball `make verify-wheel` output. Confirm:
- `src/trellis*` packages are present.
- No stray `examples/`, `docs/`, `tests/` under the wheel (those go in the sdist only).
- `py.typed` marker files appear under each `trellis*` package.

### 2. Update CHANGELOG.md

- Move items from `## [Unreleased]` into a new `## [X.Y.Z] - YYYY-MM-DD` section.
- Group by **Added / Changed / Fixed / Removed / Breaking**.
- For breaking changes, name the user-visible thing that broke (entry point, API, config key) so users grep the changelog and find the migration path.

### 3. Commit and tag

```bash
git add CHANGELOG.md
git commit -m "Release vX.Y.Z"
git tag -s vX.Y.Z -m "vX.Y.Z"
git push origin main --follow-tags
```

The tag triggers `.github/workflows/publish.yml`. Watch it on the Actions tab.

### 4. Watch the publish workflow

- The `test` job runs lint + mypy + pytest on Python 3.11/3.12/3.13.
- The `publish` job builds, runs `twine check`, and uploads via OIDC trusted publishing — no API token needed.
- If the `pypi` environment requires an approval, click **Approve and deploy**.

### 5. Post-release

1. Verify on PyPI: https://pypi.org/project/trellis-ai/X.Y.Z/.
2. Confirm `pip install trellis-ai==X.Y.Z` works in a clean venv:
   ```bash
   python -m venv /tmp/trellis-test && /tmp/trellis-test/bin/pip install "trellis-ai==X.Y.Z"
   /tmp/trellis-test/bin/trellis --version
   /tmp/trellis-test/bin/trellis-mcp --help
   ```
3. Create the GitHub release: Releases → Draft a new release → choose tag `vX.Y.Z` → paste the matching CHANGELOG section → publish.
4. Reset `## [Unreleased]` at the top of CHANGELOG.md for the next cycle.

## Hotfixing a failed publish

The publish workflow accepts `workflow_dispatch` with a `ref` input. If a tag's run failed transiently (rate limit, flaky CI, network), re-run it from the Actions tab without re-tagging:

- Actions → Publish to PyPI → Run workflow → enter the tag (e.g. `v0.2.0`) → Run.

If the build itself was broken, fix on `main`, tag a new patch version (`vX.Y.Z+1`), and push. **Do not re-tag** — PyPI rejects re-uploads of the same version, and yanking is for emergencies, not retries.

## Versioning

- We use [SemVer](https://semver.org/). Pre-1.0 means we may break minor versions, but we'll call it out in CHANGELOG and bump `0.X` accordingly.
- The version is **derived from git tags** by `hatch-vcs`. Don't write it into a Python file. Tagging is the source of truth.
- For RCs, tag `vX.Y.Z-rc1` etc. Hatch derives PEP 440-compliant pre-release versions (`X.Y.Z.dev0+gSHA` for untagged builds, `X.Y.Zrc1` for RC tags).

## Test PyPI dry-runs (optional)

If you want to test the release pipeline without affecting real PyPI:

1. Register a separate trusted publisher on https://test.pypi.org/ (same project name).
2. Add a parallel workflow file or a job step pointing at `repository-url: https://test.pypi.org/legacy/` on the `pypa/gh-action-pypi-publish` action.
3. Tag `vX.Y.Z-rc1` and push. Verify on https://test.pypi.org/project/trellis-ai/.

This adds setup cost — skip it until you've shipped at least one real release and want a buffered rollout for future major versions.

## Yanking a bad release

If a release is actively harmful (data loss, install-time crash):

```bash
# From the PyPI project page → Manage → Releases → ⋯ → Yank release
```

Yank, don't delete. Yanked versions stay installable by exact pin (`==X.Y.Z`) but are excluded from solver resolution. Then ship a fixed `X.Y.Z+1` immediately and call out the yank in the CHANGELOG.
