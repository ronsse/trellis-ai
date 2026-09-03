"""Every Makefile target that runs ruff or mypy must depend on `env-check`.

#398 made `env-check` a prerequisite of `lint` and `typecheck` because a
target nobody invokes cannot catch a drifted environment. It missed
`format`, which is the one that *writes*: `ruff format src/ tests/` rewrites
every `.py` file under `src/` and `tests/` with whatever ruff is on PATH,
and a formatter is the tool where a version difference produces the largest
diff for the least reason (#498).

The fix is one word in a Makefile. This file is the durable half, and it is
a **derived rule** rather than a list of the three targets that exist today
— a roster of gate targets is exactly the kind of thing that rots while
reading as current, which is the failure `check_tool_pins.py` was written to
end one layer down. The scan is exercised against a synthetic Makefile
carrying a known evasion, so it is proved able to fail rather than merely
observed passing.

`test` is not in the population and needs no exemption: it runs `pytest`,
not a gate tool. That is deliberate — `tests.yml` runs the full
3.11/3.12/3.13 matrix, so a test run off the gate version is legitimate and
demanding the gate version there would be false.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"

#: The gate whose absence #398/#498 are about.
GATE_TARGET = "env-check"

#: A recipe line invoking a gate tool directly. `python -m ruff` counts;
#: `python -m pre_commit`, which runs ruff from the `rev:` pinned in
#: `.pre-commit-config.yaml` (itself verified against `pyproject.toml` by
#: `check_tool_pins.check`), does not — that is a different mechanism with
#: its own pin, not an ungated invocation.
_GATE_TOOL_RE = re.compile(r"(?:^|[\s;&|(])(?:python\s+-m\s+)?(?:ruff|mypy)\b")

#: `target: prereq prereq  ## help`. Pattern rules and `.PHONY` are not
#: targets for this purpose, so a leading `.` or a `%` disqualifies.
_TARGET_RE = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)\s*:(?!=)(?P<prereqs>[^=]*)$"
)


def _parse(text: str) -> dict[str, tuple[list[str], list[str]]]:
    """`{target: (prerequisites, recipe lines)}` for one Makefile's text."""
    parsed: dict[str, tuple[list[str], list[str]]] = {}
    current: str | None = None
    for raw in text.splitlines():
        if raw.startswith("\t"):
            if current is not None:
                parsed[current][1].append(raw.lstrip("\t").lstrip("@-").strip())
            continue
        match = _TARGET_RE.match(raw)
        if match is None:
            if raw.strip() and not raw.startswith("#"):
                current = None
            continue
        name = match.group("name")
        prereqs = match.group("prereqs").split("##")[0].split()
        parsed[name] = (prereqs, [])
        current = name
    return parsed


def _gate_targets(text: str) -> dict[str, list[str]]:
    """Targets whose own recipe invokes ruff or mypy, mapped to those lines."""
    found: dict[str, list[str]] = {}
    for name, (_prereqs, recipe) in _parse(text).items():
        hits = [
            line
            for line in recipe
            if _GATE_TOOL_RE.search(line) and "pre_commit" not in line
        ]
        if hits:
            found[name] = hits
    return found


def _depends_on_gate(text: str, target: str) -> bool:
    """Is `env-check` reachable from `target` through prerequisites?"""
    parsed = _parse(text)
    seen: set[str] = set()
    stack = list(parsed.get(target, ([], []))[0])
    while stack:
        name = stack.pop()
        if name == GATE_TARGET:
            return True
        if name in seen:
            continue
        seen.add(name)
        stack.extend(parsed.get(name, ([], []))[0])
    return False


def test_every_target_that_runs_a_gate_tool_declares_env_check() -> None:
    """The rule. `format` was the survivor until #498."""
    text = MAKEFILE.read_text(encoding="utf-8")
    ungated = sorted(
        name for name in _gate_targets(text) if not _depends_on_gate(text, name)
    )
    assert ungated == [], (
        f"Makefile target(s) {ungated} invoke ruff or mypy without depending on "
        f"'{GATE_TARGET}', so they run whatever tool is on PATH rather than CI's."
    )


def test_the_scan_finds_the_targets_it_is_supposed_to_be_checking() -> None:
    """A floor, because every other assertion here divides by this scan.

    A parser that quietly stopped recognising recipe lines would report zero
    ungated targets and pass — which is the shape of defect this repo keeps
    shipping. Two independent floors: the target set, and a raw count of
    invocation lines that does not depend on grouping them by target.
    """
    text = MAKEFILE.read_text(encoding="utf-8")
    found = _gate_targets(text)
    assert {"lint", "format", "typecheck"} <= set(found), found
    assert sum(len(lines) for lines in found.values()) >= 5, found


def test_the_rule_fails_on_a_synthetic_ungated_target() -> None:
    """Proved able to fail, not just observed passing.

    Same Makefile shape, one target with the prerequisite removed. If this
    passes, the rule above is decorative.
    """
    text = (
        "env-check:\n"
        "\tpython scripts/check_tool_pins.py --check-env\n"
        "\n"
        "lint: env-check ## Run linting\n"
        "\truff check src/ tests/\n"
        "\n"
        "format: ## Format code\n"
        "\truff format src/ tests/\n"
        "\n"
        "test: ## Run tests\n"
        "\tpytest tests/ -v\n"
    )
    assert set(_gate_targets(text)) == {"lint", "format"}
    assert _depends_on_gate(text, "lint")
    assert not _depends_on_gate(text, "format")


def test_a_prerequisite_reached_only_through_another_target_counts() -> None:
    """`check: lint typecheck test` inherits the gate; the walk must see it."""
    text = (
        "env-check:\n"
        "\tpython scripts/check_tool_pins.py --check-env\n"
        "\n"
        "lint: env-check\n"
        "\truff check src/\n"
        "\n"
        "check: lint\n"
        "\tmypy src/\n"
    )
    assert set(_gate_targets(text)) == {"lint", "check"}
    assert _depends_on_gate(text, "check")


def test_pre_commit_is_not_counted_as_an_ungated_invocation() -> None:
    """It runs ruff from a `rev:` that `check_tool_pins.check` verifies.

    Without this the rule would demand a prerequisite that would be wrong to
    add, and the usual resolution to that is to delete the rule.

    The recipe line has to **name** ruff for this to test anything. The first
    version of it read `pre_commit run --all-files`, which the tool regex does
    not match on its own — so deleting the very exclusion this test exists to
    pin left it green. That is this repo's fixture-too-uniform shape,
    committed inside a test written to avoid it, so the naming is now
    asserted rather than assumed.
    """
    line = "python -m pre_commit run ruff-format --all-files || true"
    assert _GATE_TOOL_RE.search(line), "fixture no longer exercises the exclusion"
    assert _gate_targets(f"fix:\n\t{line}\n") == {}
