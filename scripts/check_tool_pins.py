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

1. ``pyproject.toml`` ``[dev]`` pins ruff and mypy with ``==`` (a range
   would make "the pinned version" meaningless and is rejected).
2. ``.pre-commit-config.yaml``'s ``rev:`` for ``ruff-pre-commit`` and
   ``mirrors-mypy`` equals those versions. Pre-commit's ``rev`` cannot
   reference ``pyproject.toml``, so this mirror is unavoidable — which is
   exactly why it is verified rather than asserted.
3. No workflow under ``.github/workflows/`` hardcodes a ``ruff==`` or
   ``mypy==`` version. Workflows must install ``.[dev]`` or derive the pin
   from ``pyproject.toml``; restating it is how this started.

Usage
-----

::

    python scripts/check_tool_pins.py

Exits 0 when every pin agrees, 1 with a report naming each divergence and
the edit that resolves it. Read-only — it never modifies a file.
"""

from __future__ import annotations

import re
import sys
import tomllib
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
    paths = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))
    return [
        (path, lineno, match.group(1), match.group(2))
        for path in paths
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for match in _HARDCODED_RE.finditer(line)
    ]


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

    return problems


def main() -> int:
    """Report divergences on stderr; exit 1 if any."""
    problems = check()
    if not problems:
        pins = pyproject_pins(REPO_ROOT / "pyproject.toml")
        summary = ", ".join(f"{tool}=={version}" for tool, version in pins.items())
        print(f"tool pins consistent: {summary}")
        return 0
    print(
        f"tool pin drift ({len(problems)} problem(s)) — "
        f"pyproject.toml [dev] is the single source of truth:",
        file=sys.stderr,
    )
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
