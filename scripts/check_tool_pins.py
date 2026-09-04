"""Fail when the lint/typecheck tool pins disagree across the repo (#378).

``pyproject.toml``'s ``[project.optional-dependencies] dev`` is the **single
source of truth** for the ruff and mypy versions. Everything else either
derives its version from that list at runtime or is a mirror this script
verifies.

Why a script and not a comment
------------------------------

Three files carried a comment asserting the pins were in sync, and all
three were wrong::

    pyproject.toml   ruff==0.16.4    "matches .pre-commit-config.yaml"   -> it did not
    pyproject.toml   mypy==2.3.1     "matches .pre-commit-config.yaml"   -> it did not
    lint.yml         ruff==0.15.22   "Keep in sync with ... pyproject"   -> it did not

The mechanism is worth naming, because it defeats any comment-based
convention: **Dependabot rewrites the version in ``pyproject.toml`` and
carries the trailing comment across verbatim.** The comment describing the
invariant survives the edit that breaks it. So the guarantee has to be
executable, and it has to run in CI.

The consequence of the drift was not cosmetic. ``lint.yml`` pinned its own
older ruff and stayed green, while ``publish.yml`` — the release gate —
installs ``.[dev]`` and runs the same ``ruff check`` under the newer pin.
Two ruff versions ran in CI at once, and the one nobody watched was the one
gating releases.

What is checked
---------------

**Repo consistency** (always; this is what CI's lint job runs):

1. ``pyproject.toml`` ``[dev]`` pins ruff and mypy with ``==`` (a range
   would make "the pinned version" meaningless and is rejected), and pins
   *nothing else* with ``==`` that :data:`TOOLS` does not name — so the
   roster below is a verified mirror rather than a declared one.
2. ``.pre-commit-config.yaml``'s ``rev:`` for ``ruff-pre-commit`` and
   ``mirrors-mypy`` equals those versions. Pre-commit's ``rev`` cannot
   reference ``pyproject.toml``, so this mirror is unavoidable — which is
   exactly why it is verified rather than asserted.
3. No workflow under ``.github/workflows/`` hardcodes a ``ruff==`` or
   ``mypy==`` version. Workflows must install ``.[dev]`` or derive the pin
   from ``pyproject.toml``; restating it is how this started.
4. The three places ``pyproject.toml`` declares the *gate Python version*
   agree — ``requires-python``'s floor, ``[tool.mypy] python_version`` and
   ``[tool.ruff] target-version`` — and every workflow that runs ``ruff``
   or ``mypy`` sets up that version. (#398)

**Environment parity**, under ``--check-env`` (#398):

5. The running interpreter's ``major.minor`` is the gate version derived in
   (4). This is the check whose absence cost the most: a 3.12 venv resolves
   a numpy whose stub carries an unguarded PEP 695 ``type`` statement, which
   mypy rejects under the pinned ``python_version = "3.11"`` — and the
   rejection aborts the run **having checked zero files in** ``src/``. The
   workaround that then circulated in agent instructions,
   ``mypy --python-version 3.12 src/``, makes the run pass by checking a
   target CI never checks.
6. The ``ruff`` and ``mypy`` **on PATH** are the pinned versions — the
   binaries the Makefile actually invokes, not whatever the metadata of an
   importable distribution claims. ``.ci-venv`` sat on ruff 0.16.4 against a
   0.16.5 pin for days and nothing noticed; that is (1) reproduced one layer
   out, and #378's finding was precisely that the older ruff stays green on
   code the newer one rejects.

Severity, and why absence is not drift
--------------------------------------

Everything in (5) and (6) is an **error**, not a warning: each one makes a
local gate return a different verdict from CI *silently*, and a warning
printed before a long run is the failure this check exists to remove. A
tool that is simply **not installed** is reported as a note and never fails
— the invocation is already loud (``make lint`` on a non-activated shell
dies with ``ruff: No such file or directory``, exit 127), and CI's lint job
legitimately installs ruff alone. The line is: *silent divergence fails;
loud divergence is left to the thing that is already loud.* The all-clear
line names the absent tool rather than reading as an unqualified match,
because ``make env-check`` is run on its own to certify an environment and
"not compared" is not "compared and equal".

``TRELLIS_ALLOW_ENV_DRIFT=1`` downgrades the errors in (5)/(6) to warnings
and exits 0. It exists so a knowingly-drifted environment does not brick
unrelated work, and it is an environment variable rather than a flag so
that opting out is an explicit, greppable act rather than a default.
**Only the spellings in** :data:`ENV_DRIFT_TRUTHY` **turn it on** —
``1``, ``true``, ``yes``, ``on``, case-insensitively and whitespace-
stripped. Everything else, ``0`` and ``false`` and ``no`` included,
leaves the gate enforcing; see :func:`env_drift_override` for why that
is spelled as an allow-list rather than as a truthiness test (#498).

Two further parity axes, measured and deliberately not checked here
-------------------------------------------------------------------

**Rich colour.** CI colorizes Typer/Rich CLI output and a local run does
not, and Rich's highlighter styles *parts* of a token — so ``--include-
chunks`` arrives as three separately-wrapped SGR runs and
``"--include-chunks" in output`` is ``False`` against output that plainly
displays it. That is a stronger divergence than anything above, because it
changes an **assertion outcome** rather than a tool's verdict: it broke
PR #488 on all three Python versions after a fully green local suite.
Measured on ``origin/main``: 6814 passed plain, 22 failed under
``FORCE_COLOR=1``, of which 21 are pre-existing across ten CLI test modules
(#495). It is not a check here for two reasons. A parity *warning* would
fire on **100% of local runs forever** — local can never be GitHub Actions
— and this repo has already established that a caveat which always prints
is one that always gets skipped. And the remedy is not the shape this
script issues: you cannot "install" your way to it, because the fix is to
**pin** Rich's colour in the test harness (``FORCE_COLOR=1`` in the root
``conftest.py``, which makes local runs predictive by default — an extra CI
matrix leg only tells you after you push, which is the failure #398 is
about). That pin turns the suite red until #495 lands, so it belongs to
#495. Note for whoever takes it: only ``FORCE_COLOR`` reproduces it —
neither ``CI=true``, ``GITHUB_ACTIONS=true`` nor ``CliRunner(color=True)``
does.

**Installed extras.** ``.ci-venv`` collected 255 tests CI never installs
the extras for (31 skipped / 310 deselected against CI's 40 / 50). Not
checked because a *superset* of CI's extras is a legitimate local
configuration — it changes which tests run, not what the lint and typecheck
gates conclude about the code CI does run — and enforcing an exact extras
set would need a hand-written roster of "which extras matter", the shape
:func:`unrostered_exact_pins` exists to avoid.

Usage
-----

::

    python scripts/check_tool_pins.py               # repo consistency
    python scripts/check_tool_pins.py --check-env   # ... and CI parity

Exits 0 when every pin agrees, 1 with a report naming each divergence and
the edit that resolves it. Read-only — it never modifies a file.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Tools whose versions must agree, mapped to the pre-commit repo whose
#: ``rev:`` mirrors the ``pyproject.toml`` pin.
TOOLS: dict[str, str] = {
    "ruff": "https://github.com/astral-sh/ruff-pre-commit",
    "mypy": "https://github.com/pre-commit/mirrors-mypy",
}

#: A ``rev:`` line's version, e.g. ``rev: v0.16.4`` -> ``0.16.4``.
_REV_RE = re.compile(r"^\s*rev:\s*v?([0-9][0-9A-Za-z.\-]*)\s*(?:#.*)?$")

#: A hardcoded pin anywhere in a workflow, e.g. ``ruff==0.15.22``.
_HARDCODED_RE = re.compile(r"\b(ruff|mypy)==([0-9][0-9A-Za-z.\-]*)")

#: A workflow ``run:`` line, in either spelling GitHub Actions accepts: the
#: bare step ``      - run: mypy src/`` and the keyed form ``        run: mypy
#: src/`` under a ``- name:``. **Both** are matched deliberately. The leading
#: dash was originally required, which meant respelling a gate step as a named
#: step silently removed its workflow from :func:`workflows_running_gates` —
#: and with it the python-version cross-check on that file. lint.yml already
#: uses named steps for other work, so that is an ordinary edit, not an exotic
#: one, and a rule that a rename can switch off is the shape this repo keeps
#: shipping.
_RUN_STEP_RE = re.compile(r"^\s*(?:-\s*)?run:\s*(?P<cmd>\S.*?)\s*$")

#: The opening line of a ``run: |`` (or ``run: >``) block, whose body is on
#: the following, more-indented lines.
_RUN_BLOCK_RE = re.compile(r"^(?P<indent>\s*)(?:-\s*)?run:\s*[|>][+-]?\s*$")

#: A ``python-version:`` value. ``${{ matrix.python-version }}`` is an
#: indirection, not a literal, and is skipped — the matrix list it points at
#: is itself a literal on another line of the same file.
_PY_VERSION_RE = re.compile(r"^\s*python-version:\s*(?P<value>\S.*?)\s*$")

#: ``requires-python`` must be a bare ``>=X.Y`` floor. Anything else (a
#: range, a ``~=``, an upper bound) would make "the gate version" ambiguous,
#: and guessing is how a checker becomes a no-op.
_REQUIRES_PYTHON_RE = re.compile(r"^>=\s*(\d+)\.(\d+)$")

#: ``ruff --version`` -> ``ruff 0.16.4``; ``mypy --version`` ->
#: ``mypy 2.3.1 (compiled: yes)``. Both put the version in the first
#: number-looking token.
_TOOL_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?[0-9A-Za-z.\-]*)")

#: Set to opt out of the ``--check-env`` errors. See the module docstring.
ENV_DRIFT_OVERRIDE = "TRELLIS_ALLOW_ENV_DRIFT"

#: Values of :data:`ENV_DRIFT_OVERRIDE` that read as "on". This is the
#: repo's existing convention, restated rather than imported:
#: ``trellis.core.write_config.TRUTHY`` holds the identical four spellings
#: and every boolean knob in ``src/`` parses through it. Restated because
#: this script is stdlib-only on purpose — it runs in CI's lint job, which
#: installs ruff and nothing else, so it cannot import the package. The
#: duplication is a mirror, and like every other mirror in this file it is
#: *verified* rather than declared: ``tests/scripts/test_check_tool_pins.py``
#: imports both and asserts they are equal.
ENV_DRIFT_TRUTHY = frozenset({"1", "true", "yes", "on"})


def env_drift_override(env: Mapping[str, str] | None = None) -> bool:
    """``True`` iff :data:`ENV_DRIFT_OVERRIDE` is set to a truthy spelling.

    An **allow-list** (:data:`ENV_DRIFT_TRUTHY`), matched after stripping
    surrounding whitespace and lowercasing, so ``1``, ``true``, ``yes`` and
    ``on`` enable the override and *everything else* — ``0``, ``false``,
    ``no``, ``off``, unset, empty, or a typo — leaves the gate enforcing.

    This was ``bool(os.environ.get(ENV_DRIFT_OVERRIDE))``, which is true for
    **any non-empty value**, so ``TRELLIS_ALLOW_ENV_DRIFT=0`` *enabled* the
    override (#498). That inverts the intent of the one person the escape
    hatch is dangerous for: an operator who writes ``=0`` is trying to turn
    it off, and got a gate that reported drift and exited 0 instead. The
    failure is silent by construction — the run still prints its findings,
    just under the ``(ignored)`` label and with a zero exit — which is the
    same shape as the divergences this whole script exists to catch.

    An allow-list is the right shape rather than a deny-list of falsey
    spellings for the same reason: a value nobody anticipated must fall on
    the *enforcing* side, because the cost of wrongly enforcing is a legible
    error message and the cost of wrongly overriding is a gate that is off
    and says so only in a line the caller has already decided to ignore.

    ``env`` is a parameter so the tests can supply a mapping instead of
    mutating the process environment.
    """
    source = os.environ if env is None else env
    return source.get(ENV_DRIFT_OVERRIDE, "").strip().lower() in ENV_DRIFT_TRUTHY


@dataclass(frozen=True)
class EnvFinding:
    """One environment-parity observation.

    ``severity`` is ``"error"`` (a silent divergence from CI) or ``"note"``
    (a tool that is absent, which cannot diverge and announces itself).
    ``subject`` names what the finding is about (a tool name, or ``"python"``)
    so the report can summarise findings without parsing its own prose.
    """

    severity: str
    message: str
    subject: str = ""


def pyproject_pins(pyproject: Path) -> dict[str, str]:
    """The ``==`` pins for :data:`TOOLS` in ``[dev]``.

    Raises ``ValueError`` when a tool is absent or not pinned with ``==``.
    A range is rejected rather than resolved: the whole point is that one
    file names one version, and ``ruff>=0.16`` names a set.
    """
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    dev = data["project"]["optional-dependencies"]["dev"]
    pins: dict[str, str] = {}
    for tool in TOOLS:
        matches = [
            spec.split("==", 1)[1].strip()
            for spec in dev
            if spec.split("==")[0].strip() == tool and "==" in spec
        ]
        if not matches:
            msg = (
                f"{pyproject.name}: [dev] must pin {tool} with '=='. "
                f"Found: {[s for s in dev if s.startswith(tool)] or 'nothing'}"
            )
            raise ValueError(msg)
        pins[tool] = matches[0]
    return pins


def precommit_revs(config: Path) -> dict[str, str]:
    """The ``rev:`` version for each repo in :data:`TOOLS`, by tool name.

    Parsed line-wise rather than with a YAML loader so the script has no
    dependency beyond the stdlib — it runs in the lint job, which installs
    ruff and nothing else.
    """
    repo_by_url = {url: tool for tool, url in TOOLS.items()}
    revs: dict[str, str] = {}
    pending: str | None = None
    for line in config.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- repo:"):
            url = stripped.split("- repo:", 1)[1].strip()
            pending = repo_by_url.get(url)
        elif pending is not None and (match := _REV_RE.match(line)):
            revs[pending] = match.group(1)
            pending = None
    return revs


def hardcoded_workflow_pins(workflows_dir: Path) -> list[tuple[Path, int, str, str]]:
    """Every ``tool==version`` literal in a workflow file.

    Returns ``(path, line number, tool, version)`` tuples. Any hit is a
    failure: a workflow that restates a pin is a second source of truth,
    and the second one is the one that goes stale.
    """
    return [
        (path, lineno, match.group(1), match.group(2))
        for path in _workflow_paths(workflows_dir)
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for match in _HARDCODED_RE.finditer(line)
    ]


def unrostered_exact_pins(pyproject: Path) -> list[str]:
    """``[dev]`` entries pinned with ``==`` that :data:`TOOLS` does not name.

    :data:`TOOLS` is hand-written — it has to be, because the pre-commit URL
    it maps to is not derivable from anything. A hand-written roster is the
    shape this repo keeps watching rot (#443 declared three control keys
    against six real sites), so it is *verified* here rather than trusted:
    add a third ``==``-pinned dev tool and this fails until the roster names
    it, which is what keeps every check keyed on ``TOOLS`` from quietly
    narrowing.
    """
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    dev = data["project"]["optional-dependencies"]["dev"]
    return [
        spec
        for spec in dev
        if "==" in spec and spec.split("==")[0].strip() not in TOOLS
    ]


def declared_python_targets(pyproject: Path) -> dict[str, tuple[int, int]]:
    """Every place ``pyproject.toml`` states the Python version to target.

    Returns ``{human-readable source: (major, minor)}``. All of them must
    agree — they are three spellings of one fact, and the whole point of
    #398 is that the environment silently stopped being that version.

    ``requires-python`` must be a bare ``>=X.Y``; a range or an upper bound
    makes "the gate version" ambiguous and raises rather than being guessed
    at, on the same reasoning that rejects ``ruff>=0.16`` above.
    ``[tool.mypy] python_version`` is **required**: without it mypy targets
    whatever interpreter is running, which is exactly the non-determinism
    this check exists to remove. ``[tool.ruff] target-version`` is optional
    because ruff derives it from ``requires-python`` when absent — checked
    when present, not invented when not.
    """
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    targets: dict[str, tuple[int, int]] = {}

    requires = str(data["project"]["requires-python"]).strip()
    floor = _REQUIRES_PYTHON_RE.match(requires)
    if floor is None:
        msg = (
            f"{pyproject.name}: requires-python must be a bare '>=X.Y' floor "
            f"so the gate version is unambiguous. Found: {requires!r}"
        )
        raise ValueError(msg)
    targets["[project] requires-python"] = (int(floor.group(1)), int(floor.group(2)))

    tool = data.get("tool", {})
    mypy_target = tool.get("mypy", {}).get("python_version")
    if mypy_target is None:
        msg = (
            f"{pyproject.name}: [tool.mypy] must set python_version. Without "
            f"it mypy targets the running interpreter, which is the drift "
            f"#398 is about."
        )
        raise ValueError(msg)
    mypy_match = re.fullmatch(r"(\d+)\.(\d+)", str(mypy_target).strip())
    if mypy_match is None:
        msg = f"{pyproject.name}: [tool.mypy] python_version is {mypy_target!r}"
        raise ValueError(msg)
    targets["[tool.mypy] python_version"] = (
        int(mypy_match.group(1)),
        int(mypy_match.group(2)),
    )

    ruff_target = tool.get("ruff", {}).get("target-version")
    if ruff_target is not None:
        ruff_match = re.fullmatch(r"py(\d)(\d+)", str(ruff_target).strip())
        if ruff_match is None:
            msg = f"{pyproject.name}: [tool.ruff] target-version is {ruff_target!r}"
            raise ValueError(msg)
        targets["[tool.ruff] target-version"] = (
            int(ruff_match.group(1)),
            int(ruff_match.group(2)),
        )

    return targets


def gate_python_version(pyproject: Path) -> tuple[int, int]:
    """The ``major.minor`` the lint and typecheck gates run under.

    This is the ``requires-python`` floor, which :func:`check` separately
    proves equal to every other declaration of the same fact and to the
    ``python-version`` of every workflow that runs a gate.
    """
    return declared_python_targets(pyproject)["[project] requires-python"]


def workflow_python_setups(workflows_dir: Path) -> dict[Path, set[tuple[int, int]]]:
    """Literal ``python-version`` values per workflow file.

    Both ``python-version: "3.11"`` and the matrix list form are read;
    ``${{ matrix.python-version }}`` is skipped as an indirection to a list
    that is itself literal elsewhere in the same file.

    Granularity is the *file*, not the job, and the tidier justification
    once given here — that every workflow sets up at most one version set
    per gate — is **false**: ``publish.yml``'s ``test`` job runs
    ``ruff check`` and ``mypy src/`` under all three of
    ``["3.11", "3.12", "3.13"]``, which is legitimate for a release gate
    (#498). So what the cross-check actually asserts is the weaker
    property that the gate Python *appears* in every workflow that runs a
    gate — enough to catch a workflow that has moved off it entirely, not
    enough to catch one that splits its gate across a matrix in which the
    gate version is merely one leg. Left weak deliberately: a job-level
    parser would need a YAML dependency this script does not have (it runs
    in the lint job, which installs ruff and nothing else), and the
    approximation is documented here rather than rediscovered.
    """
    setups: dict[Path, set[tuple[int, int]]] = {}
    for path in _workflow_paths(workflows_dir):
        versions: set[tuple[int, int]] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            match = _PY_VERSION_RE.match(line)
            if match is None:
                continue
            value = match.group("value")
            if "${{" in value:
                continue
            versions.update(
                (int(major), int(minor))
                for major, minor in re.findall(r"(\d+)\.(\d+)", value)
            )
        if versions:
            setups[path] = versions
    return setups


def workflows_running_gates(workflows_dir: Path) -> dict[Path, list[str]]:
    """Workflow files with a ``run:`` step invoking a tool.

    All three spellings a gate step can take are read — the bare ``- run:``
    step, the keyed ``run:`` under a ``- name:``, and the body of a
    ``run: |`` block — because this roster is what the python-version
    cross-check iterates, so anything it fails to see is a workflow that
    check silently stops covering.

    The tool names come from :data:`TOOLS` — verified against
    ``pyproject.toml`` by :func:`unrostered_exact_pins` — so this does not
    introduce a second roster. Selection is on the command's **first token**,
    which is what keeps block bodies from producing false positives:
    lint.yml's inline Python mentions ``'ruff'`` inside a ``print(...)`` call
    and inside comments, and neither line begins with a tool name.
    """
    running: dict[Path, list[str]] = {}
    for path in _workflow_paths(workflows_dir):
        commands = [
            command
            for command in _run_commands(path.read_text(encoding="utf-8"))
            if command.split()[0] in TOOLS
        ]
        if commands:
            running[path] = commands
    return running


def _run_commands(text: str) -> list[str]:
    """Every shell command a workflow's ``run:`` steps issue.

    Comment lines are *not* special-cased. They cannot reach the gate roster
    anyway — selection in :func:`workflows_running_gates` is on the command's
    first token, and a comment's first token is ``#`` — and a guard no test
    can kill is the thing this script exists to argue against.
    """
    commands: list[str] = []
    block_indent: int | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if block_indent is not None:
            indent = len(line) - len(line.lstrip())
            if not stripped:
                continue
            if indent > block_indent:
                commands.append(stripped)
                continue
            block_indent = None
        if block := _RUN_BLOCK_RE.match(line):
            block_indent = len(block.group("indent"))
        elif match := _RUN_STEP_RE.match(line):
            commands.append(match.group("cmd"))
    return commands


def _workflow_paths(workflows_dir: Path) -> list[Path]:
    return sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))


def check(repo_root: Path = REPO_ROOT) -> list[str]:
    """Return one problem string per divergence; empty means consistent."""
    pyproject = repo_root / "pyproject.toml"
    precommit = repo_root / ".pre-commit-config.yaml"
    workflows = repo_root / ".github" / "workflows"

    problems: list[str] = []
    pins = pyproject_pins(pyproject)
    revs = precommit_revs(precommit)

    for tool, pinned in pins.items():
        rev = revs.get(tool)
        if rev is None:
            problems.append(
                f".pre-commit-config.yaml: no 'rev:' found for {tool} "
                f"({TOOLS[tool]}). Expected 'rev: v{pinned}'."
            )
        elif rev != pinned:
            problems.append(
                f".pre-commit-config.yaml: {tool} rev is v{rev}, but "
                f"pyproject.toml [dev] pins {tool}=={pinned}. "
                f"pyproject.toml is authoritative — set 'rev: v{pinned}'."
            )

    for path, lineno, tool, version in hardcoded_workflow_pins(workflows):
        problems.append(
            f"{path.relative_to(repo_root)}:{lineno}: hardcodes {tool}=={version}. "
            f"Workflows must not restate a pin — install '.[dev]', or derive "
            f"the version from pyproject.toml at run time."
        )

    problems.extend(
        f"pyproject.toml: [dev] pins {spec!r}, which check_tool_pins.py's TOOLS "
        f"roster does not name. Add it to TOOLS (with its pre-commit repo URL) "
        f"so the pin is actually checked, or drop the '=='."
        for spec in unrostered_exact_pins(pyproject)
    )

    problems.extend(_python_target_problems(pyproject, workflows, repo_root))

    return problems


def _python_target_problems(
    pyproject: Path, workflows: Path, repo_root: Path
) -> list[str]:
    """Divergences among the declared gate Python version and the workflows."""
    problems: list[str] = []
    targets = declared_python_targets(pyproject)
    gate = targets["[project] requires-python"]

    problems.extend(
        f"pyproject.toml {source} declares Python "
        f"{declared[0]}.{declared[1]}, but [project] requires-python's floor "
        f"is {gate[0]}.{gate[1]}. These are one fact spelled three ways; "
        f"requires-python is authoritative."
        for source, declared in targets.items()
        if declared != gate and source != "[project] requires-python"
    )

    setups = workflow_python_setups(workflows)
    for path, commands in sorted(workflows_running_gates(workflows).items()):
        versions = setups.get(path, set())
        rel = path.relative_to(repo_root)
        joined = ", ".join(sorted(commands))
        if not versions:
            problems.append(
                f"{rel}: runs a gate ({joined}) but declares no literal "
                f"python-version, so nothing pins which interpreter the gate "
                f"ran under. Add 'python-version: \"{gate[0]}.{gate[1]}\"'."
            )
        elif gate not in versions:
            shown = ", ".join(f"{major}.{minor}" for major, minor in sorted(versions))
            problems.append(
                f"{rel}: runs a gate ({joined}) on Python {shown}, which does "
                f"not include the declared target {gate[0]}.{gate[1]}. CI would "
                f"be checking a version the repo does not claim to target."
            )
    return problems


# ---------------------------------------------------------------------------
# Environment parity (#398)
# ---------------------------------------------------------------------------

#: Returned by :func:`installed_tool_versions` when the binary is on PATH but
#: will not say what it is. Distinct from ``None`` (absent) on purpose:
#: absence is loud and harmless, an unidentifiable tool is neither.
UNKNOWN_VERSION = "?"


def installed_tool_versions(
    tools: tuple[str, ...] | None = None,
) -> dict[str, str | None]:
    """The version of each tool **as resolved on PATH**.

    ``None`` means the executable is not on PATH. The binary is asked rather
    than distribution metadata queried, because ``make lint`` invokes the
    bare name: a venv can carry ``ruff`` metadata at the pinned version while
    an older binary earlier on PATH is the one that actually runs, and it is
    the one that runs whose verdict CI is being compared against.
    """
    found: dict[str, str | None] = {}
    for tool in tools if tools is not None else tuple(TOOLS):
        executable = shutil.which(tool)
        if executable is None:
            found[tool] = None
            continue
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, path from which()
                [executable, "--version"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            found[tool] = UNKNOWN_VERSION
            continue
        match = _TOOL_VERSION_RE.search(completed.stdout or completed.stderr or "")
        found[tool] = match.group(1) if match else UNKNOWN_VERSION
    return found


def check_environment(
    repo_root: Path = REPO_ROOT,
    *,
    python_version: tuple[int, int] | None = None,
    installed: dict[str, str | None] | None = None,
) -> list[EnvFinding]:
    """Compare *this* environment against the gates CI runs.

    Both inputs are injectable so the drift cases can be tested without
    provisioning a drifted interpreter — and so this function never asserts
    anything about the ambient environment of the suite that tests it, which
    legitimately runs on 3.11, 3.12 and 3.13.
    """
    pyproject = repo_root / "pyproject.toml"
    workflows = repo_root / ".github" / "workflows"
    findings: list[EnvFinding] = []

    gate = gate_python_version(pyproject)
    actual = python_version if python_version is not None else sys.version_info[:2]
    if actual != gate:
        gate_files = ", ".join(
            sorted(path.name for path in workflows_running_gates(workflows))
        )
        findings.append(
            EnvFinding(
                "error",
                f"Python {actual[0]}.{actual[1]} is running, but the gates run "
                f"on {gate[0]}.{gate[1]} ({gate_files or 'the CI workflows'}). "
                f"A different interpreter resolves a different dependency set: "
                f"this is how 'mypy src/' came to abort having checked zero "
                f"files while reading as clean. Rebuild the virtualenv on "
                f"Python {gate[0]}.{gate[1]}.",
                "python",
            )
        )

    pins = pyproject_pins(pyproject)
    resolved = installed if installed is not None else installed_tool_versions()
    for tool, pinned in sorted(pins.items()):
        version = resolved.get(tool)
        if version is None:
            findings.append(
                EnvFinding(
                    "note",
                    f"{tool} is not on PATH — nothing to diverge. Invoking it "
                    f"fails loudly (exit 127); only a *silent* difference is "
                    f"this check's business.",
                    tool,
                )
            )
        elif version == UNKNOWN_VERSION:
            findings.append(
                EnvFinding(
                    "error",
                    f"{tool} is on PATH but would not report a version, so "
                    f"whether it matches the {tool}=={pinned} pin is unknown.",
                    tool,
                )
            )
        elif version != pinned:
            findings.append(
                EnvFinding(
                    "error",
                    f"{tool} {version} is on PATH, but pyproject.toml [dev] "
                    f"pins {tool}=={pinned}. The local gate is not the gate CI "
                    f"runs. Fix: pip install '{tool}=={pinned}'.",
                    tool,
                )
            )

    return findings


def _report_environment(findings: list[EnvFinding], *, override: bool) -> int:
    """Print the parity report; return the exit code it implies."""
    for note in (f for f in findings if f.severity == "note"):
        print(f"  note: {note.message}")
    errors = [finding for finding in findings if finding.severity == "error"]
    if not errors:
        # Name what was NOT compared. An all-clear that reads identically
        # whether both tools matched or neither was present is this repo's
        # signature defect in miniature: `make env-check` is run precisely to
        # certify "my environment is CI's", and absence certifies nothing.
        unchecked = sorted(f.subject for f in findings if f.severity == "note")
        if unchecked:
            print(
                f"environment matches the CI gates, except "
                f"{', '.join(unchecked)}: not installed, so not compared"
            )
        else:
            print("environment matches the CI gates")
        return 0
    label = "environment drift (ignored)" if override else "environment drift"
    print(f"{label} ({len(errors)} problem(s)):", file=sys.stderr)
    for error in errors:
        print(f"  - {error.message}", file=sys.stderr)
    if override:
        print(
            f"{ENV_DRIFT_OVERRIDE} is set — continuing with a gate that is not "
            f"CI's. Unset it, or set it to 0, to make these fail.",
            file=sys.stderr,
        )
        return 0
    print(
        f"Set {ENV_DRIFT_OVERRIDE}=1 to proceed anyway, knowing the local "
        f"result does not predict CI.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    """Report divergences on stderr; exit 1 if any.

    ``argv`` is a parameter rather than read straight from ``sys.argv`` so
    the tests can call this without pytest's own arguments arriving here.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    check_env = "--check-env" in args
    unknown = [arg for arg in args if arg != "--check-env"]
    if unknown:
        print(f"unknown argument(s): {' '.join(unknown)}", file=sys.stderr)
        print("usage: check_tool_pins.py [--check-env]", file=sys.stderr)
        return 2

    problems = check()
    if problems:
        print(
            f"tool pin drift ({len(problems)} problem(s)) — "
            f"pyproject.toml is the single source of truth:",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    pins = pyproject_pins(REPO_ROOT / "pyproject.toml")
    gate = gate_python_version(REPO_ROOT / "pyproject.toml")
    summary = ", ".join(f"{tool}=={version}" for tool, version in pins.items())
    print(f"tool pins consistent: {summary}, python {gate[0]}.{gate[1]}")
    if not check_env:
        return 0
    return _report_environment(
        check_environment(),
        override=env_drift_override(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
