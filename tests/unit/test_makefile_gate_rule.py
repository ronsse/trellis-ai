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

**What this text scan cannot read**, demonstrated rather than assumed —
every shape below was run through the shipped `_gate_targets` and reported
nothing. A tool reached through a variable (`RUFF := ruff`, then
`$(RUFF) check`), a quoted invocation (`"ruff" check`), and pattern rules
and `.`-prefixed targets, which a leading `%` or `.` disqualifies on
purpose so that `.PHONY` is not read as a target. Each is a spelling
nothing in this Makefile uses; the shapes it *does* use — a `##` help
comment, a `\\` continuation, `@`/`-` recipe prefixes, `cd x && ruff`,
`python -m ruff`, a path-qualified binary, an inline `; recipe` — are all
read, and each has a test below. The boundary is written down here because
the failure it would produce is silent: an unread target simply leaves the
population, and the floor test is the only thing standing under it.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.ast_rules import assert_hand_read_floor

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"

#: The gate whose absence #398/#498 are about.
GATE_TARGET = "env-check"

#: A recipe line invoking a gate tool directly. `python -m ruff` counts, and
#: so does a path-qualified `./.venv/bin/ruff` — hence `/` in the leading
#: class. `python -m pre_commit`, which runs ruff from the `rev:` pinned in
#: `.pre-commit-config.yaml` (itself verified against `pyproject.toml` by
#: `check_tool_pins.check`), does not — that is a different mechanism with
#: its own pin, not an ungated invocation.
#:
#: Two guards keep `clean`'s `rm -rf ... .mypy_cache .ruff_cache` out, and it
#: is worth knowing which does what, because adding `/` to the leading class
#: weakened one of them. The leading class does it there — `.` is not in it.
#: `\b` is what covers the spellings `/` newly admits: `bin/ruff_wrapper` and
#: `src/mypy_stubs` are now one character from a false positive, and only the
#: word boundary (`_` is a word character) stops them. Both are pinned below.
_GATE_TOOL_RE = re.compile(r"(?:^|[\s;&|(/])(?:python\s+-m\s+)?(?:ruff|mypy)\b")

#: `target: prereq prereq`, with any `## help` already stripped by
#: :func:`_parse`. Pattern rules and `.PHONY` are not targets for this
#: purpose, so a leading `.` or a `%` disqualifies. `:(?!=)` names the `:=`
#: assignment explicitly, but `[^=]*` is what actually rejects every
#: assignment spelling — the `=` is still on the line either way — so the
#: lookahead is redundant by construction and deleting it changes nothing
#: this rule can observe. It is kept as documentation, not as a guard.
_TARGET_RE = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)\s*:(?!=)(?P<prereqs>[^=]*)$"
)


def _logical_lines(text: str) -> list[str]:
    """Physical lines with `\\`-continuations joined, the way make reads them.

    Not cosmetic. A continuation whose next line is indented with **spaces**
    used to end the enclosing target: the joined-second-half line matched no
    target, so the parser dropped `current` and every recipe line after it —
    silently removing a target from the population this rule reasons over.
    Joining first makes the shape a non-event instead of an evasion.
    """
    joined: list[str] = []
    pending = ""
    for line in text.splitlines():
        raw = pending + " " + line.strip() if pending else line
        pending = ""
        if raw.endswith("\\"):
            pending = raw[:-1].rstrip()
            continue
        joined.append(raw)
    if pending:
        joined.append(pending)
    return joined


def _parse(text: str) -> dict[str, tuple[list[str], list[str]]]:
    """`{target: (prerequisites, recipe lines)}` for one Makefile's text.

    Two make spellings are normalised before the target regex sees a line,
    because both were silent misses rather than loud ones. A trailing
    comment is cut off first — this Makefile puts a `## help` on every
    target, and an `=` anywhere in that free-form prose (`## lint with
    FOO=1`) made `[^=]*` reject the whole line, dropping the target *and*
    its recipe. And a `target: prereqs ; recipe` inline recipe is split out,
    since it is a recipe that never starts with a tab.
    """
    parsed: dict[str, tuple[list[str], list[str]]] = {}
    current: str | None = None
    for raw in _logical_lines(text):
        if raw.startswith("\t"):
            if current is not None:
                parsed[current][1].append(raw.lstrip("\t").lstrip("@-").strip())
            continue
        if not raw.strip() or raw.startswith("#"):
            continue
        head, semicolon, inline = raw.split("#")[0].partition(";")
        match = _TARGET_RE.match(head)
        if match is None:
            current = None
            continue
        name = match.group("name")
        prereqs = match.group("prereqs").split()
        body = inline.strip().lstrip("@-").strip()
        recipe = [body] if semicolon and body else []
        parsed[name] = (prereqs, recipe)
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
    shipping. Two floors, both hand-read off the Makefile: the target names,
    and the number of invocation lines. Neither is a number the scan can
    compute for itself, which is #466's mistake and the reason
    :func:`tests.ast_rules.assert_hand_read_floor` takes the count as an
    argument. This rule reads a Makefile rather than an AST, so the rest of
    that module does not apply — but the floor is the part that generalises,
    and re-deriving it per rule is exactly what #490 found four authors
    doing wrong independently.
    """
    text = MAKEFILE.read_text(encoding="utf-8")
    found = _gate_targets(text)
    assert {"lint", "format", "typecheck"} <= set(found), found
    assert_hand_read_floor(
        sum(len(lines) for lines in found.values()),
        5,
        subject="Makefile gate-tool invocation",
        hint="Counted by hand at 93e77f1: two in `lint`, two in `format`, "
        "one in `typecheck`.",
    )


def test_the_parser_survives_the_make_spellings_that_used_to_end_a_target() -> None:
    """Each shape here was a **silent** miss — a target dropped, not flagged.

    Every case below was probed against the shipped parser and reproduced.
        They matter more than the exotica the parser still cannot read (a tool
        behind a `$(RUFF)` variable, a quoted `"ruff"`) because each is a
        spelling this Makefile's own conventions invite: it puts a `##` help on
        every target, and it already continues two lines with `\\`.
    """
    # A comment containing `=` used to make `[^=]*` reject the target line,
    # taking the recipe under it with it. Both comment spellings, because
    # make treats a single `#` as a comment and only the `##` convention was
    # covered at first — a mutant narrowing the split back to `##` survived.
    with_help = "fmt: ## format with RUFF_FORMAT=1\n\truff format src/\n"
    assert set(_gate_targets(with_help)) == {"fmt"}
    one_hash = "fmt: # format with RUFF_FORMAT=1\n\truff format src/\n"
    assert set(_gate_targets(one_hash)) == {"fmt"}
    # A prerequisite list continued onto a space-indented line.
    assert set(_gate_targets("fmt: \\\n      deps\n\truff format src/\n")) == {"fmt"}
    # `target: prereqs ; recipe` — a recipe that never starts with a tab.
    assert set(_gate_targets("fmt: ; ruff format src/\n")) == {"fmt"}
    # A path-qualified invocation, which `(?:^|[\\s;&|(])` could not see.
    assert set(_gate_targets("fmt:\n\t./.venv/bin/ruff format src/\n")) == {"fmt"}


def test_a_recipe_prefix_does_not_hide_an_invocation() -> None:
    """`@` and `-` are make's own recipe prefixes, and both precede the tool.

    Deleting the `.lstrip("@-")` in `_parse` left the whole suite green
    before this existed: nothing in the real Makefile, and nothing in the
    synthetic fixtures, prefixed a gate tool. That is this repo's
    fixture-too-uniform shape, so the field is exercised rather than assumed.
    """
    assert set(_gate_targets("fmt:\n\t@ruff format src/\n")) == {"fmt"}
    assert set(_gate_targets("fmt:\n\t-mypy src/\n")) == {"fmt"}
    assert set(_gate_targets("fmt: ; @ruff format src/\n")) == {"fmt"}


def test_a_name_that_merely_starts_with_a_tool_is_not_an_invocation() -> None:
    """The negative control for the `/` added to the tool regex.

    `make clean` removes `.mypy_cache` and `.ruff_cache`, and demanding
    `env-check` of it would be absurd. There the leading class is what saves
    it — `.` is not in the class — which is *not* what the first version of
    this comment claimed, and the mutant that deleted `\\b` proved the point
    by surviving. `\\b` earns its place on the spellings `/` newly admits:
    a path segment whose name merely begins with a tool name.
    """
    cache = "rm -rf dist/ build/ *.egg-info .mypy_cache .ruff_cache .pytest_cache"
    assert not _GATE_TOOL_RE.search(cache)
    assert _gate_targets(f"clean:\n\t{cache}\n") == {}
    assert "clean" not in _gate_targets(MAKEFILE.read_text(encoding="utf-8"))
    # What `\\b` is for, now that `/` is in the class.
    assert not _GATE_TOOL_RE.search("./bin/ruff_wrapper --all")
    assert not _GATE_TOOL_RE.search("cp -r src/mypy_stubs .")
    assert _GATE_TOOL_RE.search("./bin/ruff check src/")


def test_a_help_comment_is_not_read_as_a_prerequisite() -> None:
    """`## …` is cut before the prerequisites are split, not after.

    Without the cut, a target whose help text merely *mentions* `env-check`
    would report as gated. Nothing in the Makefile does today, which is why
    dropping the split left the suite green.
    """
    text = (
        "env-check:\n\ttrue\n\nfmt: ## like env-check, but writes\n\truff format src/\n"
    )
    assert set(_gate_targets(text)) == {"fmt"}
    assert not _depends_on_gate(text, "fmt")


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


def test_the_prerequisite_walk_terminates_on_a_cycle() -> None:
    """A recursive prerequisite is a make error, not a reason to hang a suite.

    The walk marks `seen` *after* the gate comparison, so the answer is
    unaffected; what this pins is that it answers at all. A rule that hangs
    is a rule someone deletes.
    """
    text = "a: b\n\truff check src/\n\nb: a\n\tmypy src/\n"
    assert not _depends_on_gate(text, "a")
    assert not _depends_on_gate(text, "b")
    cyclic_but_gated = (
        "env-check:\n\ttrue\n\na: b\n\truff check src/\n\nb: a env-check\n"
    )
    assert _depends_on_gate(cyclic_but_gated, "a")


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
