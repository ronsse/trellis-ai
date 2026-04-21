"""Advisory store — JSON file-based persistence for advisories."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from trellis.schemas.advisory import Advisory, AdvisoryStatus

logger = structlog.get_logger(__name__)


class AdvisoryStore:
    """Load and save advisories from a JSON file.

    Follows the same lightweight pattern as :class:`PolicyStore`.
    Advisories are small, infrequently updated, and loaded in full —
    a JSON file is the right weight class.

    File format::

        {"advisories": [<Advisory.model_dump()>, ...]}
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._advisories: dict[str, Advisory] = {}
        if self._path.exists():
            self._load()

    # -- Public API --

    def list(
        self,
        *,
        scope: str | None = None,
        min_confidence: float = 0.0,
        include_suppressed: bool = False,
    ) -> list[Advisory]:
        """Return advisories, optionally filtered by scope and confidence.

        Suppressed advisories are excluded by default — they stay in the
        store so the fitness loop can restore them when evidence warrants
        (see :meth:`restore`), but retrieval callers only see active ones.
        Pass ``include_suppressed=True`` to inspect the full set (tools,
        audits, fitness-loop scoring).

        Results are ordered by confidence descending.
        """
        result = list(self._advisories.values())
        if not include_suppressed:
            result = [a for a in result if a.status == AdvisoryStatus.ACTIVE]
        if scope is not None:
            result = [a for a in result if a.scope == scope]
        if min_confidence > 0.0:
            result = [a for a in result if a.confidence >= min_confidence]
        result.sort(key=lambda a: a.confidence, reverse=True)
        return result

    def get(self, advisory_id: str) -> Advisory | None:
        """Get an advisory by ID.

        Returns the advisory regardless of status — suppressed advisories
        remain retrievable by ID so the fitness loop can evaluate them
        and the UI can surface suppression history.
        """
        return self._advisories.get(advisory_id)

    def put(self, advisory: Advisory) -> Advisory:
        """Add or replace an advisory.  Persists immediately."""
        self._advisories[advisory.advisory_id] = advisory
        self._save()
        logger.info("advisory_stored", advisory_id=advisory.advisory_id)
        return advisory

    def put_many(self, advisories: Sequence[Advisory]) -> int:
        """Add or replace multiple advisories.  Single write."""
        for advisory in advisories:
            self._advisories[advisory.advisory_id] = advisory
        self._save()
        logger.info("advisories_stored", count=len(advisories))
        return len(advisories)

    def suppress(
        self,
        advisory_id: str,
        *,
        reason: str | None = None,
    ) -> Advisory | None:
        """Soft-suppress an advisory: flip status, stamp metadata, persist.

        Returns the updated advisory, or ``None`` if the id is unknown.
        Idempotent — suppressing an already-suppressed advisory is a
        no-op and returns the existing record without updating
        ``suppressed_at``.

        Unlike :meth:`remove`, the advisory is preserved so it can be
        restored via :meth:`restore` if later evidence warrants.
        """
        advisory = self._advisories.get(advisory_id)
        if advisory is None:
            return None
        if advisory.status == AdvisoryStatus.SUPPRESSED:
            return advisory
        updated = advisory.model_copy(
            update={
                "status": AdvisoryStatus.SUPPRESSED,
                "suppressed_at": datetime.now(UTC),
                "suppression_reason": reason,
                "updated_at": datetime.now(UTC),
            }
        )
        self._advisories[advisory_id] = updated
        self._save()
        logger.info(
            "advisory_suppressed",
            advisory_id=advisory_id,
            reason=reason,
        )
        return updated

    def restore(self, advisory_id: str) -> Advisory | None:
        """Restore a suppressed advisory to active status.

        Returns the updated advisory, or ``None`` if the id is unknown.
        Idempotent — restoring an already-active advisory is a no-op.
        Clears ``suppressed_at`` and ``suppression_reason``.
        """
        advisory = self._advisories.get(advisory_id)
        if advisory is None:
            return None
        if advisory.status == AdvisoryStatus.ACTIVE:
            return advisory
        updated = advisory.model_copy(
            update={
                "status": AdvisoryStatus.ACTIVE,
                "suppressed_at": None,
                "suppression_reason": None,
                "updated_at": datetime.now(UTC),
            }
        )
        self._advisories[advisory_id] = updated
        self._save()
        logger.info("advisory_restored", advisory_id=advisory_id)
        return updated

    def remove(self, advisory_id: str) -> bool:
        """Hard-delete an advisory by ID.  Returns ``True`` if found.

        This is the irreversible path and is intended for manual
        cleanup (admin commands, broken-state recovery). The fitness
        loop should use :meth:`suppress` instead so the record remains
        available for later restoration.
        """
        if advisory_id not in self._advisories:
            return False
        del self._advisories[advisory_id]
        self._save()
        logger.info("advisory_removed", advisory_id=advisory_id)
        return True

    def clear(self) -> int:
        """Remove all advisories.  Returns count removed."""
        count = len(self._advisories)
        self._advisories.clear()
        self._save()
        logger.info("advisories_cleared", count=count)
        return count

    # -- Persistence --

    def _load(self) -> None:
        """Load advisories from the JSON file."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for entry in raw.get("advisories", []):
                advisory = Advisory.model_validate(entry)
                self._advisories[advisory.advisory_id] = advisory
            logger.info("advisories_loaded", count=len(self._advisories))
        except Exception:
            logger.exception("advisory_load_failed", path=str(self._path))

    def _save(self) -> None:
        """Persist current advisories to the JSON file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "advisories": [a.model_dump(mode="json") for a in self._advisories.values()]
        }
        self._path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
