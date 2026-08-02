"""Select a GitHub issue for autonomous authoring.

Cohort 1's :class:`~trellis_workers.code_authoring.generator.ProposalGenerator`
sources proposals from *operational telemetry* — ``EXTRACTION_FAILED``
clusters and ``WELL_KNOWN_CANDIDATE`` events. That signal is
deployment-dependent and, on a young deployment, empty.

The roadmap driver adds a second source: the curated **GitHub issue
queue**, where a human has already done the judgment work of deciding
what is small, mechanical, and safely automatable. This module is that
seam. It is deliberately **pure** — it takes issue dicts (as produced by
``gh issue list --json number,title,labels,body``) and returns a
candidate. Fetching is the harness's job; deciding is this module's.

Everything downstream is unchanged Cohort-2 machinery: the selected
candidate becomes a ``proposal.md`` with a ``files_allowed`` frontmatter,
and every control in
``docs/design/adr-coding-agent-loop-cohort2-amendment.md`` §2.1-§2.8
applies exactly as it does to a telemetry-sourced proposal.

**The label is not the authority.** ``ready`` is maintained by an
automated routine and can lag; the harness re-verifies dependencies from
live issue state before spawning. What this module *does* treat as
authoritative is the human-owned side: an issue is eligible only if a
human marked it ``mechanical`` and wrote an explicit ``files_allowed``
block. Absent either, it is not a candidate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from trellis_workers.code_authoring.safety import AllowlistError, validate_allowlist

#: Labels an issue must carry to be eligible. ``mechanical`` is the
#: human's assertion that the change is small and allowlist-scoped;
#: ``ready`` is the computed assertion that its dependencies are closed.
REQUIRED_LABELS: frozenset[str] = frozenset({"mechanical", "ready"})

#: Labels that veto autonomous authoring outright, whatever else the
#: issue carries. ``keystone`` and ``owner-only`` are human-authorship
#: markers; the ``blocked:*`` family means a gate has not cleared.
DISQUALIFYING_LABELS: frozenset[str] = frozenset(
    {
        "keystone",
        "owner-only",
        "blocked:dep",
        "blocked:owner-decision",
        "blocked:signal",
        "security",
    }
)

#: Fenced block, in an issue body or comment, carrying the human-authored
#: write scope. One repo-relative path or glob per line::
#:
#:     ```files_allowed
#:     src/trellis/retrieve/excerpts.py
#:     tests/unit/retrieve/test_excerpts.py
#:     ```
#:
#: A fenced block (rather than prose parsing) keeps the contract
#: greppable, reviewable in the GitHub UI, and unambiguous.
FILES_ALLOWED_BLOCK: re.Pattern[str] = re.compile(
    r"```files_allowed\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class IssueCandidate:
    """A GitHub issue judged eligible for autonomous authoring.

    Attributes:
        number: The issue number. Also the branch suffix
            (``agent-issue/<number>``) and the proposal ID stem.
        title: Issue title, used for the ``[auto]``-prefixed PR title.
        files_allowed: The human-authored write scope, already validated
            against the hard-exclusion set.
        label_names: The issue's labels at selection time, retained so
            the harness can record what state it acted on.
    """

    number: int
    title: str
    files_allowed: tuple[str, ...]
    label_names: tuple[str, ...]


def parse_files_allowed(text: str) -> tuple[str, ...]:
    """Extract the ``files_allowed`` fenced block from issue text.

    Returns an empty tuple when no block is present — the caller treats
    that as "not a candidate", never as "allow everything". Comment lines
    (``#``) and blank lines are ignored so operators can annotate the
    block.
    """
    found = FILES_ALLOWED_BLOCK.search(text or "")
    if found is None:
        return ()
    entries = [
        line.strip()
        for line in found.group(1).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return tuple(entries)


def _label_names(issue: dict) -> tuple[str, ...]:
    """Normalize the label shape ``gh --json labels`` produces."""
    labels = issue.get("labels") or []
    names: list[str] = []
    for label in labels:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            names.append(name)
    return tuple(names)


def _labels_qualify(label_set: set[str]) -> bool:
    """Both label conditions: every required one, and no disqualifying one."""
    return REQUIRED_LABELS.issubset(label_set) and not (
        label_set & DISQUALIFYING_LABELS
    )


def _extract_scope(issue: dict) -> tuple[str, ...]:
    """The issue's validated write scope, or ``()`` if it has none.

    The scope may live in the body or in any comment. An allowlist that
    fails validation collapses to ``()`` — an unsafe scope and an absent
    scope are the same answer here (not a candidate), and the harness
    should never see a scope that :mod:`.safety` would later reject.
    """
    sources = [issue.get("body") or ""]
    sources.extend(
        comment.get("body") or ""
        for comment in (issue.get("comments") or [])
        if isinstance(comment, dict)
    )
    for source in sources:
        files_allowed = parse_files_allowed(source)
        if not files_allowed:
            continue
        try:
            validate_allowlist(files_allowed)
        except AllowlistError:
            return ()
        return files_allowed
    return ()


def evaluate_issue(issue: dict) -> IssueCandidate | None:
    """Judge a single issue. ``None`` means "not eligible", never an error.

    Eligibility is conjunctive and fail-closed: open, carries every label
    in :data:`REQUIRED_LABELS`, carries none in
    :data:`DISQUALIFYING_LABELS`, and has a ``files_allowed`` block that
    survives :func:`~trellis_workers.code_authoring.safety.validate_allowlist`.
    An allowlist that names an excluded path disqualifies the issue here,
    rather than failing later inside the spawn.
    """
    number = issue.get("number")
    if not isinstance(number, int):
        return None
    if str(issue.get("state", "OPEN")).upper() != "OPEN":
        return None

    names = _label_names(issue)
    if not _labels_qualify(set(names)):
        return None

    files_allowed = _extract_scope(issue)
    if not files_allowed:
        return None

    return IssueCandidate(
        number=number,
        title=str(issue.get("title") or f"issue {number}"),
        files_allowed=files_allowed,
        label_names=names,
    )


def select_candidate(
    issues: list[dict],
    *,
    excluded_numbers: frozenset[int] = frozenset(),
) -> IssueCandidate | None:
    """Pick the single issue to author this cycle, deterministically.

    One issue per cycle — the per-PR LOC ceiling and weekly budget both
    assume a bounded run, and a single reviewable PR is the artifact this
    loop exists to produce.

    Ordering is smallest scope first (fewest ``files_allowed`` entries),
    then lowest issue number. Deterministic ordering matters: two runs
    over unchanged state must pick the same issue, or the idempotency
    lock cannot do its job.

    Args:
        issues: Issue dicts from ``gh issue list --json ...``.
        excluded_numbers: Issues to skip — those with an open agent PR or
            a ``PROPOSAL_AUTHORSHIP_*`` event inside the 30-day cooldown.
            The harness supplies these; this module does no I/O.
    """
    candidates = [
        candidate
        for candidate in (evaluate_issue(issue) for issue in issues)
        if candidate is not None and candidate.number not in excluded_numbers
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: (len(c.files_allowed), c.number))
