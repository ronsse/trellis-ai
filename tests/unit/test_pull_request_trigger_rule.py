"""Every quality workflow must run on a pull request to *any* base branch.

All six quality workflows shipped with ``pull_request: branches: [main]``
in the initial commit, and for the repo's whole history that was correct
by accident: every pull request targeted ``main``, so the filter never
excluded one. Verified over the 60 most recently merged PRs — zero had a
base other than ``main``.

The first stacked PRs broke it silently. ``#559 → #561 → #563`` each
based on its parent's branch, and GitHub reported **no checks reported**
on all three: not a failure, not a pending run, an empty check list that
renders the same as a PR whose workflows have not started yet. Nothing
failed, so nothing said anything.

Two properties of this repo make that worse than it sounds. ``main``
carries **no required status checks** (``required_status_checks: null``),
so the merge gate in ``docs/design/swarm-handoff.md`` — *green against
current* ``main`` — is a convention an owner upholds by reading the
checks, with no platform backstop behind it; an unchecked PR is as
mergeable as a green one. And ``delete_branch_on_merge`` is **false**, so
GitHub does not auto-retarget a child PR when its parent merges: a
stacked PR stays based on a merged branch, and stays unchecked, until
someone retargets it by hand.

So the rule:

    A workflow that declares a ``pull_request`` trigger must not restrict
    it to particular base branches.

The ``push`` trigger keeps its ``branches: [main]`` filter and is out of
scope — that filter is what stops every branch push from running the
matrix a second time alongside the PR run.

**What this checks, and what it does not.** It reads the ``on:`` block of
every workflow file and asserts that those declaring ``pull_request``
declare it unfiltered. It does not check that a workflow *has* a
``pull_request`` trigger — ``claude.yml`` is comment-driven and
``publish.yml`` is release-only, and neither should acquire one — but it
does pin which files are out of scope, so a quality workflow *losing* its
trigger is a failure here rather than a silent loss of coverage.

**The ``on`` key is a YAML 1.1 boolean.** ``yaml.safe_load`` parses the
bare word ``on`` as ``True``, so ``workflow["on"]`` raises ``KeyError``
and — much worse — ``workflow.get("on", {})`` returns empty and makes
every assertion below vacuously true. That is this repo's named defect
class landing on the checker instead of the code, so the lookup is
written once in :func:`_on_block`, and
:func:`test_the_on_block_lookup_survives_the_yaml_boolean_key` fails if a
future edit reaches for the string key.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

#: Workflows that deliberately declare no ``pull_request`` trigger.
#: ``claude.yml`` runs on ``@claude`` mentions in comments and reviews;
#: ``publish.yml`` runs on a release tag. Pinned rather than inferred so a
#: quality workflow that *loses* its trigger fails here.
NO_PULL_REQUEST_TRIGGER = frozenset({"claude.yml", "publish.yml"})

#: Floor on the scan's own output. Every other assertion divides by what
#: the scan found, so a scan that silently stops finding workflows would
#: satisfy all of them.
MIN_WORKFLOWS = 6
MIN_PULL_REQUEST_WORKFLOWS = 5


def _workflow_paths() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml"))


def _on_block(workflow: dict[Any, Any]) -> dict[str, Any]:
    """Return a workflow's trigger block.

    ``on`` is a YAML 1.1 boolean, so ``safe_load`` gives the key as
    ``True``. Both spellings are accepted because a future workflow may
    quote it; an absent block raises rather than defaulting to empty.
    """
    present = [key for key in (True, "on") if key in workflow]
    assert present, "workflow declares no trigger block"
    block = workflow[present[0]]
    assert isinstance(block, dict), f"unexpected trigger block: {block!r}"
    return block


def _restricts_pull_request(workflow: dict[Any, Any]) -> bool:
    """The shipped predicate: does this workflow filter PR base branches?"""
    on = _on_block(workflow)
    if "pull_request" not in on:
        return False
    config = on["pull_request"]
    return isinstance(config, dict) and "branches" in config


def _load(path: Path) -> dict[Any, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_scan_finds_the_workflows() -> None:
    paths = _workflow_paths()

    assert len(paths) >= MIN_WORKFLOWS, f"only found {[p.name for p in paths]}"


@pytest.mark.parametrize("path", _workflow_paths(), ids=lambda p: p.name)
def test_workflow_does_not_filter_pull_request_base_branches(path: Path) -> None:
    assert not _restricts_pull_request(_load(path)), (
        f"{path.name} restricts its pull_request trigger to particular base "
        "branches, so a stacked PR gets no checks at all — and reports that "
        "as an empty check list, not a failure."
    )


def test_every_quality_workflow_declares_a_pull_request_trigger() -> None:
    """A workflow losing its trigger is a coverage loss, not a pass."""
    with_trigger = {
        path.name
        for path in _workflow_paths()
        if "pull_request" in _on_block(_load(path))
    }
    expected = {
        path.name
        for path in _workflow_paths()
        if path.name not in NO_PULL_REQUEST_TRIGGER
    }

    assert with_trigger == expected
    assert len(with_trigger) >= MIN_PULL_REQUEST_WORKFLOWS


def test_push_keeps_its_main_filter() -> None:
    """The rule is about ``pull_request`` only.

    ``push: branches: [main]`` is what stops a branch push from running
    the matrix a second time beside the PR run, so widening *that* would
    double every stacked PR's CI cost.
    """
    filtered = [
        path.name
        for path in _workflow_paths()
        if isinstance(_on_block(_load(path)).get("push"), dict)
        and _on_block(_load(path))["push"].get("branches") == ["main"]
    ]

    assert len(filtered) >= MIN_PULL_REQUEST_WORKFLOWS


def test_the_rule_catches_a_filtered_trigger() -> None:
    """Prove the check can fail, by feeding the shipped predicate the
    exact shape every workflow carried before this change."""
    offender = yaml.safe_load(
        "name: Tests\non:\n  push:\n    branches: [main]\n"
        "  pull_request:\n    branches: [main]\n"
    )

    assert _restricts_pull_request(offender)


@pytest.mark.parametrize(
    "source",
    [
        "name: Tests\non:\n  pull_request:\n",
        "name: Tests\non:\n  pull_request:\n    types: [opened]\n",
        "name: Claude\non:\n  issue_comment:\n    types: [created]\n",
    ],
    ids=["bare", "types-only", "no-pull-request"],
)
def test_the_rule_admits_an_unfiltered_trigger(source: str) -> None:
    assert not _restricts_pull_request(yaml.safe_load(source))


def test_the_on_block_lookup_survives_the_yaml_boolean_key() -> None:
    """The vacuity this checker is most exposed to.

    ``safe_load`` keys the trigger block under ``True``, so a lookup
    written as ``workflow.get("on", {})`` returns empty — and every
    assertion above passes on every possible input. Pinned against a real
    workflow file rather than a literal, so the trap has to still be real
    for the test to mean anything.
    """
    loaded = _load(WORKFLOW_DIR / "tests.yml")

    assert True in loaded, "PyYAML stopped parsing `on:` as a boolean key"
    assert "on" not in loaded
    assert "pull_request" in _on_block(loaded)
