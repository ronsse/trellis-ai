"""A relative link or in-page anchor in the docs must resolve.

The docs cross-reference each other and the source tree by relative path.
Files move, headings are renamed and modules are deleted, and a link to the
old place keeps reading as a pointer: GitHub renders it, the reader clicks
and lands on a 404.

The rule: in ``docs/`` (outside ``docs/archive/``), the root ``README*`` and
``CLAUDE.md``, every relative markdown link resolves to a path that exists,
and a ``#fragment`` on a markdown target names one of its headings. A
fragment on any other file (``path.py#L12``) is not checked. An editor-style
``path.py:12`` suffix is part of the filename, so it fails; write
``path.py#L12``.

``docs/archive/`` is exempt on purpose: its documents are history and are not
rewritten when the file they cite is later removed (see its README).

Links inside fenced code and inline code spans are text, not links. Absolute
URLs (``https:``, ``mailto:`` and the like) are not checked.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote

from trellis.core.path_presence import path_is_present

REPO = Path(__file__).resolve().parents[2]
ARCHIVE = "docs/archive"

# Hand-read floors, so a refactor that stops finding files or links cannot
# leave the rule green over an empty population. Raise them as the docs grow.
MIN_FILES = 100
MIN_LINKS = 500

_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_INLINE_LINK = re.compile(
    r"\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*(<[^>\n]*>|[^)\s]+)(?:\s+\"[^\"]*\")?\s*\)"
)
_REF_DEF = re.compile(r"^ {0,3}\[(?!\^)[^\]]+\]:[ \t]*(<[^>\n]*>|\S+)", re.MULTILINE)
_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*?)[ \t]*#*[ \t]*$")
_HTML_ANCHOR = re.compile(r"<a\s+(?:name|id)=\"([^\"]+)\"")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _blank_code(text: str) -> str:
    """Return ``text`` with code replaced by spaces, keeping line numbers."""
    out: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append("")
        elif in_fence:
            out.append("")
        else:
            out.append(_INLINE_CODE.sub(lambda m: " " * len(m.group(0)), line))
    return "\n".join(out)


def _targets(text: str) -> Iterator[tuple[int, str]]:
    prose = _blank_code(text)
    for pattern in (_INLINE_LINK, _REF_DEF):
        for match in pattern.finditer(prose):
            line_no = prose.count("\n", 0, match.start()) + 1
            yield line_no, match.group(1).strip("<>")


def _slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, drop punctuation, spaces to hyphens."""
    heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    heading = re.sub(r"<[^>]+>|[`*]", "", heading).strip().lower()
    return re.sub(r"[^\w\- ]", "", heading).replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    found: set[str] = set(_HTML_ANCHOR.findall(text))
    seen: dict[str, int] = {}
    in_fence = False
    for line in text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        match = None if in_fence else _HEADING.match(line)
        if match is None:
            continue
        slug = _slug(match.group(1))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(slug if count == 0 else f"{slug}-{count}")
    return found


def doc_files(root: Path) -> list[Path]:
    docs = root / "docs"
    files = [
        p
        for p in docs.rglob("*.md")
        if not p.relative_to(root).as_posix().startswith(ARCHIVE + "/")
    ]
    files += [*root.glob("README*"), root / "CLAUDE.md"]
    return sorted(p for p in files if p.is_file())


def count_links(root: Path) -> int:
    return sum(
        1
        for f in doc_files(root)
        for _ in _targets(f.read_text(encoding="utf-8", errors="replace"))
    )


def broken_links(root: Path) -> list[str]:
    problems: list[str] = []
    for doc in doc_files(root):
        text = doc.read_text(encoding="utf-8", errors="replace")
        for line_no, target in _targets(text):
            if _SCHEME.match(target) or target.startswith("//"):
                continue
            path_part, _, fragment = target.partition("#")
            path_part = unquote(path_part.partition("?")[0])
            fragment = unquote(fragment)
            if not path_part:
                resolved = doc
            elif path_part.startswith("/"):
                resolved = root / path_part.lstrip("/")
            else:
                resolved = Path(os.path.normpath(doc.parent / path_part))
            where = f"{doc.relative_to(root).as_posix()}:{line_no}"
            if not path_is_present(resolved):
                problems.append(f"{where}: {target} (no such path)")
            elif (
                fragment
                and resolved.is_file()
                and resolved.suffix == ".md"
                and fragment.lower() not in _anchors(resolved)
            ):
                problems.append(f"{where}: {target} (no such heading)")
    return problems


def test_docs_links_resolve() -> None:
    problems = broken_links(REPO)
    assert not problems, (
        f"{len(problems)} broken relative link(s) in docs/ (outside "
        f"{ARCHIVE}/), README* or CLAUDE.md. Point each at where the target "
        "lives now; if it was removed on purpose, reword the sentence so it "
        "does not link there. Cite a line as path.py#L12, not path.py:12.\n"
        + "\n".join(problems)
    )


def test_rule_still_sees_the_docs() -> None:
    files = doc_files(REPO)
    assert len(files) >= MIN_FILES, len(files)
    assert count_links(REPO) >= MIN_LINKS
    assert not any(f.relative_to(REPO).as_posix().startswith(ARCHIVE) for f in files)
    assert REPO / "CLAUDE.md" in files


def test_rule_flags_each_defect_and_passes_each_good_shape(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "archive").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("x = 1\n")
    (docs / "other.md").write_text("# Other\n\n## Two words\n\n## Two words\n")
    (docs / "archive" / "old.md").write_text("[gone](nowhere.md)\n")
    (tmp_path / "CLAUDE.md").write_text("[ok](docs/other.md)\n")
    (tmp_path / "README.md").write_text("[bad](docs/missing.md)\n")
    (docs / "sub" / "page.md").write_text(
        "# Page\n"
        "[file](../other.md) [dir](../sub) [src](../../src/mod.py#L1-L2)\n"
        "[heading](../other.md#two-words) [dup](../other.md#two-words-1)\n"
        "[self](#page) [root](/src/mod.py) [web](https://example.com/x)\n"
        "[multi\nline](../other.md)\n"
        "`[code](nowhere.md)`\n"
        "```\n[fenced](nowhere.md)\n```\n"
        "text[^note]\n\n[^note]: footnote body\n"
        "[ref]: ../other.md\n"
        "[missing](../gone.md)\n"
        "[anchor](../other.md#no-such-heading)\n"
        "[colon](../../src/mod.py:7)\n"
        "[selfmiss](#nope)\n"
        "[multimiss][m]\n\n[m]: ../absent.md\n"
    )
    found = sorted(broken_links(tmp_path))
    expected = [
        "README.md:1: docs/missing.md (no such path)",
        "docs/sub/page.md:15: ../gone.md (no such path)",
        "docs/sub/page.md:16: ../other.md#no-such-heading (no such heading)",
        "docs/sub/page.md:17: ../../src/mod.py:7 (no such path)",
        "docs/sub/page.md:18: #nope (no such heading)",
        "docs/sub/page.md:21: ../absent.md (no such path)",
    ]
    assert found == sorted(expected)
