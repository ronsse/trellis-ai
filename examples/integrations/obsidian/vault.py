"""Obsidian vault interface for Trellis."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import Field

from trellis.core.base import TrellisModel

logger = structlog.get_logger(__name__)


class ObsidianNote(TrellisModel):
    """Represents an Obsidian vault note."""

    path: str
    title: str
    content: str
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    created: datetime | None = None
    modified: datetime | None = None
    tags: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)


class ObsidianVault:
    """Interface to an Obsidian vault (folder of markdown files)."""

    def __init__(self, vault_path: str | Path) -> None:
        self.vault_path = Path(vault_path)
        if not self.vault_path.exists():
            logger.warning("vault_not_found", path=str(vault_path))

    def read_note(self, path: str) -> ObsidianNote | None:
        """Read a note. Auto-appends .md if missing."""
        full_path = self.vault_path / path
        if not path.endswith(".md"):
            full_path = self.vault_path / f"{path}.md"
        if not full_path.exists():
            return None

        content = full_path.read_text(encoding="utf-8")
        frontmatter, body = _parse_frontmatter(content)

        stat = full_path.stat()
        created = datetime.fromtimestamp(stat.st_ctime, tz=UTC)
        modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC)

        title_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        title = title_match.group(1) if title_match else full_path.stem

        tags = _extract_tags(content)
        if "tags" in frontmatter:
            fm_tags = frontmatter["tags"]
            if isinstance(fm_tags, list):
                tags.extend(fm_tags)
            elif isinstance(fm_tags, str):
                tags.extend(fm_tags.split())

        links = _extract_links(content)

        return ObsidianNote(
            path=full_path.relative_to(self.vault_path).as_posix(),
            title=title,
            content=body,
            frontmatter=frontmatter,
            created=created,
            modified=modified,
            tags=list(set(tags)),
            links=links,
        )

    def create_note(
        self,
        path: str,
        content: str,
        frontmatter: dict[str, Any] | None = None,
    ) -> ObsidianNote:
        """Create a new note with optional frontmatter."""
        if not path.endswith(".md"):
            path = f"{path}.md"
        full_path = self.vault_path / path
        full_path.parent.mkdir(parents=True, exist_ok=True)

        if frontmatter:
            fm_text = yaml.dump(frontmatter, default_flow_style=False)
            full_content = f"---\n{fm_text}---\n\n{content}"
        else:
            full_content = content

        full_path.write_text(full_content, encoding="utf-8")
        note = self.read_note(path)
        assert note is not None
        return note

    def update_note(
        self,
        path: str,
        content: str | None = None,
        frontmatter: dict[str, Any] | None = None,
        append: bool = False,
    ) -> ObsidianNote | None:
        """Update an existing note."""
        note = self.read_note(path)
        if note is None:
            return None

        if not path.endswith(".md"):
            path = f"{path}.md"
        full_path = self.vault_path / path

        new_frontmatter = {**note.frontmatter}
        if frontmatter:
            new_frontmatter.update(frontmatter)

        if content is not None:
            new_content = note.content + "\n" + content if append else content
        else:
            new_content = note.content

        if new_frontmatter:
            fm_text = yaml.dump(new_frontmatter, default_flow_style=False)
            full_content = f"---\n{fm_text}---\n\n{new_content}"
        else:
            full_content = new_content

        full_path.write_text(full_content, encoding="utf-8")
        return self.read_note(path)

    def delete_note(self, path: str) -> bool:
        """Delete a note. Returns True if deleted."""
        if not path.endswith(".md"):
            path = f"{path}.md"
        full_path = self.vault_path / path
        if not full_path.exists():
            return False
        full_path.unlink()
        return True

    def list_notes(
        self,
        folder: str | None = None,
        recursive: bool = True,
    ) -> list[str]:
        """List note paths in the vault."""
        base = self.vault_path / folder if folder else self.vault_path
        if not base.exists():
            return []
        paths = base.rglob("*.md") if recursive else base.glob("*.md")
        return [
            p.relative_to(self.vault_path).as_posix()
            for p in paths
            if not p.name.startswith(".")
        ]

    def search(
        self,
        query: str,
        folder: str | None = None,
        limit: int = 20,
    ) -> list[tuple[ObsidianNote, float]]:
        """Search notes by content. Returns (note, score) tuples."""
        results: list[tuple[ObsidianNote, float]] = []
        query_lower = query.lower()
        query_terms = query_lower.split()

        for path in self.list_notes(folder=folder):
            note = self.read_note(path)
            if note is None:
                continue
            content_lower = (note.title + " " + note.content).lower()
            matches = sum(1 for t in query_terms if t in content_lower)
            if matches > 0:
                score = matches / len(query_terms) if query_terms else 0
                results.append((note, score))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]


# -- helpers -----------------------------------------------------------------


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from note content."""
    if not content.startswith("---"):
        return {}, content
    end_match = re.search(r"\n---\n", content[3:])
    if not end_match:
        return {}, content
    fm_text = content[4 : end_match.start() + 3]
    body = content[end_match.end() + 3 :]
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, body


def _extract_tags(content: str) -> list[str]:
    """Extract #tag patterns from content."""
    pattern = r"(?<!\w)#([a-zA-Z][a-zA-Z0-9_/-]*)"
    return list(set(re.findall(pattern, content)))


def _extract_links(content: str) -> list[str]:
    """Extract [[wiki-links]] from content."""
    pattern = r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]"
    return list(set(re.findall(pattern, content)))
