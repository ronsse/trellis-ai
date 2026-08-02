"""Diff-level guardrails for the Cohort-2 autonomous authoring path.

Pure functions — no stores, no event log, no subprocess. The harness owns
the I/O (computing the diff, emitting ``SECRET_SCRUB_TRIGGERED``,
preserving the worktree); this module owns the *decisions* so they can be
unit-tested exhaustively without a git tree.

Two enforcement layers, both mandated by
``docs/design/adr-coding-agent-loop-cohort2-amendment.md``:

* **Allowlist** (§2.5) — :func:`validate_allowlist` runs at *parse time*
  (before Claude Code is invoked) and rejects a malformed or
  hard-excluded ``files_allowed``; :func:`verify_diff_allowlist` runs
  *after* the spawn against the real diff. Deliberately redundant with
  the spawn-time env scoping: that layer is enforced by the SDK, this one
  by code we own.
* **Secret scrub** (§2.6) — :func:`scan_secrets` runs over the raw diff
  text before a PR is opened. Deliberately conservative: a false positive
  is operator-recoverable, a committed secret is not.

Both pattern sets are **frozen constants**, not config. Widening them is
a deliberate ADR amendment, not a deploy-time knob — that is the whole
point of the control.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Globs that may never appear in a proposal's ``files_allowed``, no
#: matter what the proposal says. Sourced verbatim from the Cohort-2
#: amendment §2.5 (which folds in the original ADR §4.1 exclusions).
#: These are the paths where an autonomous edit would either escalate the
#: system's own privileges or disable the controls that bound it:
#: auth, the policy gates, the mutation executor, the store registry,
#: CI configuration, and anything secret-shaped.
HARD_EXCLUDED_GLOBS: tuple[str, ...] = (
    "src/trellis_api/auth.py",
    "src/trellis_api/auth/**",
    "src/trellis/mutate/policies/**",
    "src/trellis/mutate/executor.py",
    "src/trellis/stores/registry.py",
    "**/*_security_*.py",
    "**/*_secret_*.py",
    ".github/workflows/**",
    ".github/actions/**",
    "**/secrets.*",
    "**/credentials.*",
    "**/.env*",
)

#: Secret-shaped patterns scanned against the raw diff before any PR is
#: opened (amendment §2.6). Ordered as in the ADR table. ``dotenv_filename``
#: is intentionally filename-shaped rather than value-shaped: a diff that
#: adds a ``.env`` file is never legitimate, whatever its contents.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    (
        "aws_secret_access_key",
        re.compile(
            r"(?i)aws[_\-]?secret[_\-]?(access[_\-]?)?key\s*[:=]\s*[\"']?[A-Za-z0-9/+=]{40}"
        ),
    ),
    ("openai_api_key", re.compile(r"sk-[A-Za-z0-9]{32,}")),
    ("anthropic_api_key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{32,}")),
    (
        "generic_api_key_assignment",
        re.compile(
            r"(?i)\b(api[_\-]?key|secret[_\-]?key)\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{16,}"
        ),
    ),
    ("password_assignment", re.compile(r"(?i)\bpassword\s*[:=]\s*[\"']?\S{6,}")),
    ("dotenv_filename", re.compile(r"\.env(?:\.\w+)?\b")),
    ("bearer_token_literal", re.compile(r"Bearer\s+[A-Za-z0-9_\-\.=]{20,}")),
    (
        "private_key_block",
        re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    ),
)


class AllowlistError(ValueError):
    """A ``files_allowed`` entry is malformed or hard-excluded.

    Raised at *parse time* by :func:`validate_allowlist`, before Claude
    Code is invoked — a bad allowlist fails the spawn at the harness
    layer rather than being discovered after the fact.
    """


@dataclass(frozen=True, slots=True)
class AllowlistViolation:
    """One diff path that no allowlist glob permitted.

    Attributes:
        path: The offending repo-relative path from the diff.
        allowed_globs: The glob set it was checked against, carried so the
            operator can see *why* it failed without re-deriving state.
    """

    path: str
    allowed_globs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SecretMatch:
    """One secret-shaped hit in a diff.

    Attributes:
        pattern_name: Which :data:`SECRET_PATTERNS` entry matched.
        line_number: 1-indexed line within the scanned text.
        line_preview: The matched line, redacted — the matched span is
            replaced with ``***`` so the finding is reportable (and
            loggable) without re-leaking the secret it found.
    """

    pattern_name: str
    line_number: int
    line_preview: str


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    """Translate a path glob to an anchored regex.

    ``**`` crosses directory separators, ``*`` and ``?`` do not. This is
    stricter than :mod:`fnmatch` (whose ``*`` also matches ``/``), which
    matters for the *allow* direction: a permissive matcher would widen
    the blast radius of every proposal.
    """
    out: list[str] = []
    i = 0
    while i < len(glob):
        char = glob[i]
        if glob.startswith("/**", i):
            # The separator is part of the wildcard, so "a/**" matches the
            # directory "a" itself as well as everything beneath it.
            # Without this, an exclusion like "src/trellis_api/auth/**"
            # leaves the bare directory path unguarded.
            out.append("(?:/.*)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
            if glob.startswith("/", i):
                i += 1
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile(f"^{''.join(out)}$")


def _matches(path: str, glob: str) -> bool:
    return _glob_to_regex(glob).match(path) is not None


def is_hard_excluded(path_or_glob: str) -> bool:
    """Whether a path (or a proposal's glob) collides with an exclusion.

    Checked both ways: a literal path matching an exclusion glob, and an
    exclusion glob matching a *broader* proposal glob. The second
    direction is what stops ``src/trellis/**`` from quietly buying write
    access to ``executor.py``.
    """
    for excluded in HARD_EXCLUDED_GLOBS:
        if _matches(path_or_glob, excluded) or _matches(excluded, path_or_glob):
            return True
    return False


def validate_allowlist(globs: tuple[str, ...]) -> None:
    """Parse-time validation of a proposal's ``files_allowed``.

    Rejects an empty allowlist (deny-by-default means an empty list can
    author nothing — surfacing that as an error beats a confusing
    empty-diff run), path traversal, absolute or user-relative paths, and
    anything colliding with :data:`HARD_EXCLUDED_GLOBS`.

    Raises:
        AllowlistError: On the first offending entry, naming it.
    """
    if not globs:
        msg = "files_allowed is empty - deny-by-default permits no edits"
        raise AllowlistError(msg)
    for glob in globs:
        if not glob or not glob.strip():
            msg = "files_allowed contains a blank entry"
            raise AllowlistError(msg)
        if ".." in glob.split("/"):
            msg = f"path traversal in files_allowed: {glob!r}"
            raise AllowlistError(msg)
        if glob.startswith(("/", "~")):
            msg = f"non-relative path in files_allowed: {glob!r}"
            raise AllowlistError(msg)
        if is_hard_excluded(glob):
            msg = f"files_allowed entry hits a hard exclusion: {glob!r}"
            raise AllowlistError(msg)


def verify_diff_allowlist(
    changed_paths: tuple[str, ...],
    allowed_globs: tuple[str, ...],
) -> tuple[AllowlistViolation, ...]:
    """Post-spawn check of the real diff against the allowlist.

    Deny by default: a path is permitted only if at least one glob
    matches it *and* it hits no hard exclusion. Returns every violation
    rather than the first, so one failed run reports the full picture.
    """
    violations: list[AllowlistViolation] = []
    for path in changed_paths:
        permitted = any(_matches(path, glob) for glob in allowed_globs)
        if not permitted or is_hard_excluded(path):
            violations.append(
                AllowlistViolation(path=path, allowed_globs=allowed_globs)
            )
    return tuple(violations)


def scan_secrets(diff_text: str) -> tuple[SecretMatch, ...]:
    """Scan raw diff text for secret-shaped tokens.

    Scans the *diff* rather than the worktree: the diff is what becomes a
    public artifact, and scanning it avoids false positives on
    pre-existing fixtures that legitimately carry placeholder secrets.

    Every match is redacted in :attr:`SecretMatch.line_preview` before it
    is returned — a scanner that leaked findings into logs would defeat
    its own purpose.
    """
    matches: list[SecretMatch] = []
    for line_number, line in enumerate(diff_text.splitlines(), start=1):
        # Only added lines can introduce a secret; '+++' is a file header.
        if line.startswith("+++") or not line.startswith("+"):
            continue
        for pattern_name, pattern in SECRET_PATTERNS:
            found = pattern.search(line)
            if found is None:
                continue
            redacted = f"{line[: found.start()]}***{line[found.end() :]}"
            matches.append(
                SecretMatch(
                    pattern_name=pattern_name,
                    line_number=line_number,
                    line_preview=redacted[:200],
                )
            )
    return tuple(matches)
