"""Build a redaction tombstone without running a redaction (#1034).

A surface test needs a document id that reads as redacted, not the redaction
machinery. This writes the same row ``_tombstone_document`` leaves: no text, an
archived lifecycle, and the ``redacted`` deprecation reason.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from trellis.core.document_write import (
    REDACTION_TOMBSTONE_REASON,
    is_redaction_tombstone,
    put_document,
)
from trellis.schemas.classification import LIFECYCLE_KEY, Lifecycle

ORIGINAL_TEXT = "Cobalt lantern original text that was redacted"


def tombstone_metadata() -> dict[str, Any]:
    return {
        LIFECYCLE_KEY: Lifecycle(
            state="archived",
            valid_until=datetime.now(UTC),
            deprecation_reason=REDACTION_TOMBSTONE_REASON,
        ).model_dump(mode="json")
    }


def make_tombstone(document_store: Any, vector_store: Any, doc_id: str) -> None:
    """Write ``doc_id`` as live text, then blank it the way a redaction does."""
    put_document(
        document_store,
        vector_store,
        doc_id,
        ORIGINAL_TEXT,
        {},
        preserve_updated_at=False,
    )
    put_document(
        document_store,
        vector_store,
        doc_id,
        "",
        tombstone_metadata(),
        preserve_updated_at=False,
    )
    assert is_redaction_tombstone(document_store.get(doc_id))
