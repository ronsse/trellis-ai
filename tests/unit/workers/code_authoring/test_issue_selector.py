"""Tests for the GitHub-issue proposal source.

Eligibility is fail-closed, so most of these assert that something is
*not* selected. The one path that selects is the narrow, human-blessed
case: labelled ``mechanical`` + ``ready`` with an explicit write scope.
"""

from __future__ import annotations

from trellis_workers.code_authoring.issue_selector import (
    evaluate_issue,
    parse_files_allowed,
    select_candidate,
)

SCOPE_BLOCK = """Some prose about the bug.

```files_allowed
src/trellis/retrieve/excerpts.py
tests/unit/retrieve/test_excerpts.py
```
"""


def _issue(
    number: int = 254, labels: tuple[str, ...] = ("mechanical", "ready"), **kw
) -> dict:
    issue = {
        "number": number,
        "title": f"issue {number}",
        "state": "OPEN",
        "body": SCOPE_BLOCK,
        "labels": [{"name": name} for name in labels],
    }
    issue.update(kw)
    return issue


class TestParseFilesAllowed:
    def test_extracts_entries(self) -> None:
        assert parse_files_allowed(SCOPE_BLOCK) == (
            "src/trellis/retrieve/excerpts.py",
            "tests/unit/retrieve/test_excerpts.py",
        )

    def test_absent_block_yields_empty_not_wildcard(self) -> None:
        assert parse_files_allowed("no block here") == ()
        assert parse_files_allowed("") == ()

    def test_skips_comments_and_blanks(self) -> None:
        text = "```files_allowed\n# why\n\nsrc/a.py\n```"
        assert parse_files_allowed(text) == ("src/a.py",)


class TestEvaluateIssue:
    def test_accepts_a_well_formed_mechanical_issue(self) -> None:
        candidate = evaluate_issue(_issue())
        assert candidate is not None
        assert candidate.number == 254
        assert len(candidate.files_allowed) == 2

    def test_rejects_closed(self) -> None:
        assert evaluate_issue(_issue(state="CLOSED")) is None

    def test_rejects_missing_required_label(self) -> None:
        assert evaluate_issue(_issue(labels=("mechanical",))) is None
        assert evaluate_issue(_issue(labels=("ready",))) is None

    def test_rejects_disqualifying_labels(self) -> None:
        for veto in ("keystone", "owner-only", "blocked:dep", "security"):
            assert evaluate_issue(_issue(labels=("mechanical", "ready", veto))) is None

    def test_rejects_missing_scope_block(self) -> None:
        assert evaluate_issue(_issue(body="no scope declared")) is None

    def test_rejects_scope_naming_an_excluded_path(self) -> None:
        body = "```files_allowed\nsrc/trellis/mutate/executor.py\n```"
        assert evaluate_issue(_issue(body=body)) is None

    def test_finds_scope_in_a_comment(self) -> None:
        issue = _issue(body="no scope in body", comments=[{"body": SCOPE_BLOCK}])
        candidate = evaluate_issue(issue)
        assert candidate is not None

    def test_tolerates_malformed_input(self) -> None:
        assert evaluate_issue({}) is None
        assert evaluate_issue({"number": "not-an-int", "labels": []}) is None


class TestSelectCandidate:
    def test_returns_none_when_nothing_qualifies(self) -> None:
        assert select_candidate([]) is None
        assert select_candidate([_issue(labels=("keystone", "ready"))]) is None

    def test_prefers_smallest_scope_then_lowest_number(self) -> None:
        narrow = _issue(number=300, body="```files_allowed\nsrc/a.py\n```")
        chosen = select_candidate([_issue(number=254), narrow])
        assert chosen is not None
        assert chosen.number == 300

    def test_ties_break_on_lowest_number(self) -> None:
        chosen = select_candidate([_issue(number=300), _issue(number=254)])
        assert chosen is not None
        assert chosen.number == 254

    def test_is_deterministic_across_input_order(self) -> None:
        issues = [_issue(number=300), _issue(number=254), _issue(number=280)]
        first = select_candidate(issues)
        second = select_candidate(list(reversed(issues)))
        assert first == second

    def test_honours_the_cooldown_exclusions(self) -> None:
        chosen = select_candidate(
            [_issue(number=254), _issue(number=300)],
            excluded_numbers=frozenset({254}),
        )
        assert chosen is not None
        assert chosen.number == 300
