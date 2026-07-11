"""Claude conversation-export ingestion.

Turns a claude.ai data export (Settings → Privacy → Export data, a
``conversations.json`` or the ``.zip`` it ships in) into ordinary Trellis
documents — one per conversation — so the personal context in your
everyday Claude chat (preferences, people, decisions, recurring topics)
becomes deduplicated, embedded, and semantically retrievable alongside
everything else. This is the capture half of "wire real usage into
Trellis": the corpus you actually accumulate by using Claude, which the
Claude Code / MCP path never sees.

Non-destructive, like corpus file ingestion: the export stays where it
is; re-exporting and re-syncing picks up conversations that grew new
turns (``content_hash`` changes → re-put + re-embed). Identity is the
conversation's own uuid (``conversation:<source_system>:<uuid>``), so a
re-export never duplicates — and because that id is content-independent,
move detection is disabled (two conversations with identical text are
distinct, not a rename).

Only the storage/retrieval half lives here. Mining entities out of the
prose (people, accounts, ages, preferences) into the knowledge graph is
the flag-gated ``--extract`` follow-up (ADR §5), reusing
``build_save_memory_extractor``; storing + embedding already makes the
conversations retrievable, which is the load-bearing win.
"""

from __future__ import annotations

import json
import zipfile
from typing import TYPE_CHECKING, Any

import structlog

from trellis.ingest_corpus.models import SyncRecord
from trellis.ingest_corpus.sync import sync_records

if TYPE_CHECKING:
    from pathlib import Path

    from trellis.ingest_corpus.models import CorpusSyncReport
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

#: Default corpus namespace for claude.ai exports.
DEFAULT_SOURCE_SYSTEM = "claude-ai"

#: Member read out of a ``.zip`` export.
_EXPORT_MEMBER = "conversations.json"

# claude.ai exports label the person "human"; the API uses "user" — both
# are you. "assistant"/"model" are the reply side.
_SPEAKER_LABELS = {
    "human": "You",
    "user": "You",
    "assistant": "Claude",
    "model": "Claude",
}


def conversation_doc_id(source_system: str, conversation_id: str) -> str:
    """Stable doc id for a conversation — keyed on its own uuid."""
    return f"conversation:{source_system}:{conversation_id}"


def conversation_id_prefix(source_system: str) -> str:
    """Doc-id prefix shared by every conversation of one source."""
    return f"conversation:{source_system}:"


def sync_conversations(
    registry: StoreRegistry,
    path: Path,
    *,
    source_system: str = DEFAULT_SOURCE_SYSTEM,
    extra_metadata: dict[str, Any] | None = None,
    dry_run: bool = False,
    prune: bool = False,
    extract: bool = False,
    requested_by: str = "cli:ingest-conversations",
) -> CorpusSyncReport:
    """Sync a Claude conversation export into the document store.

    Args:
        registry: Active store registry.
        path: A ``conversations.json``, the ``.zip`` export containing it,
            or a directory holding it.
        source_system: Corpus namespace; stored as
            ``metadata.source_system`` and part of every ``doc_id``.
        extra_metadata: Operator tags merged into every written document.
        dry_run: Report the plan without writing.
        prune: Delete conversations no longer present in the export.
        extract: Opt into the flag-gated entity/edge extraction pass
            (also requires ``TRELLIS_ENABLE_MEMORY_EXTRACTION``).
        requested_by: Audit identifier for events and embed logging.

    Returns:
        A :class:`CorpusSyncReport`.
    """
    from trellis.extract.memory_ingest_hook import (  # noqa: PLC0415
        build_memory_extractor,
    )

    records, warnings = read_claude_export(path, source_system=source_system)
    return sync_records(
        registry,
        records,
        source_system=source_system,
        id_prefix=conversation_id_prefix(source_system),
        root_label=str(path),
        requested_by=requested_by,
        extra_metadata=extra_metadata,
        dry_run=dry_run,
        prune=prune,
        extractor=build_memory_extractor(registry, opt_in=extract and not dry_run),
        detect_moves=False,
        initial_warnings=warnings,
    )


def read_claude_export(
    path: Path,
    *,
    source_system: str = DEFAULT_SOURCE_SYSTEM,
) -> tuple[list[SyncRecord], list[dict[str, Any]]]:
    """Parse a Claude export into ``(records, warnings)``.

    Tolerant of the two message shapes claude.ai has shipped (top-level
    ``text`` vs a ``content`` block list) and of the export arriving as a
    raw ``conversations.json``, the ``.zip`` it downloads as, or a
    directory containing it. Malformed conversations are skipped with a
    warning rather than aborting the run.
    """
    warnings: list[dict[str, Any]] = []
    raw = _load_export_json(path, warnings)
    if raw is None:
        return [], warnings

    conversations = _as_conversation_list(raw, warnings)
    records: list[SyncRecord] = []
    for index, conversation in enumerate(conversations):
        record = _conversation_record(conversation, index, source_system, warnings)
        if record is not None:
            records.append(record)
    return records, warnings


def _load_export_json(path: Path, warnings: list[dict[str, Any]]) -> Any | None:
    """Resolve and JSON-parse the export from a file, zip, or directory."""
    try:
        if path.is_dir():
            return json.loads((path / _EXPORT_MEMBER).read_text(encoding="utf-8"))
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                member = _find_zip_member(archive)
                if member is None:
                    warnings.append(
                        {"kind": "export_member_missing", "path": str(path)}
                    )
                    return None
                return json.loads(archive.read(member).decode("utf-8"))
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        warnings.append(
            {"kind": "unreadable_export", "path": str(path), "detail": str(exc)}
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        warnings.append(
            {"kind": "malformed_export", "path": str(path), "detail": str(exc)}
        )
    return None


def _find_zip_member(archive: zipfile.ZipFile) -> str | None:
    """Locate ``conversations.json`` anywhere in the archive."""
    for name in archive.namelist():
        if name.rsplit("/", 1)[-1] == _EXPORT_MEMBER:
            return name
    return None


def _as_conversation_list(
    raw: Any, warnings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Normalise the top-level JSON to a list of conversation dicts."""
    if isinstance(raw, list):
        return [c for c in raw if isinstance(c, dict)]
    if isinstance(raw, dict):
        if isinstance(raw.get("conversations"), list):
            return [c for c in raw["conversations"] if isinstance(c, dict)]
        # A single exported conversation.
        if "chat_messages" in raw or "messages" in raw:
            return [raw]
    warnings.append({"kind": "unrecognized_export_shape", "detail": type(raw).__name__})
    return []


def _conversation_record(
    conversation: dict[str, Any],
    index: int,
    source_system: str,
    warnings: list[dict[str, Any]],
) -> SyncRecord | None:
    """Build one :class:`SyncRecord` from a conversation dict."""
    conversation_id = str(
        conversation.get("uuid") or conversation.get("id") or ""
    ).strip()
    if not conversation_id:
        warnings.append({"kind": "conversation_missing_id", "index": index})
        return None

    title = str(conversation.get("name") or "").strip() or "Untitled conversation"
    messages = conversation.get("chat_messages")
    if not isinstance(messages, list):
        messages = conversation.get("messages")
    turns = _render_turns(messages if isinstance(messages, list) else [])
    if not turns:
        warnings.append(
            {"kind": "empty_conversation", "conversation_id": conversation_id}
        )
        return None

    content = f"# {title}\n\n" + "\n\n".join(turns)
    metadata: dict[str, Any] = {
        "conversation_id": conversation_id,
        "title": title,
        "content_type": "conversation",
        "message_count": len(turns),
    }
    for key in ("created_at", "updated_at"):
        value = conversation.get(key)
        if isinstance(value, str) and value:
            metadata[key] = value

    return SyncRecord(
        doc_id=conversation_doc_id(source_system, conversation_id),
        source_key=title,
        content=content,
        handler_metadata=metadata,
    )


def _render_turns(messages: list[Any]) -> list[str]:
    """Render non-empty messages into speaker-labelled turn strings."""
    turns: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        text = _message_text(message)
        if not text:
            continue
        sender = str(message.get("sender") or message.get("role") or "").lower()
        label = _SPEAKER_LABELS.get(sender, sender.title() or "Unknown")
        turns.append(f"**{label}:** {text}")
    return turns


def _message_text(message: dict[str, Any]) -> str:
    """Extract a message's text across both claude.ai export shapes.

    Prefers the top-level ``text`` (older exports render the full turn
    there); falls back to joining the ``text`` blocks of a ``content``
    list (newer exports), skipping tool-use / thinking blocks.
    """
    text = message.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            block["text"].strip()
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and block["text"].strip()
        ]
        return "\n\n".join(parts)
    return ""
