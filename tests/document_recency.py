"""Shared helpers for the ``preserve_updated_at`` recency tests (#397, #406).

Explicitly imported rather than a ``conftest`` fixture: these are used by five
test modules and have nothing to do with the other ~5,900 tests. Same
convention as :mod:`tests.structlog_isolation`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from trellis.retrieve.strategies import KeywordSearch

if TYPE_CHECKING:
    import pytest

    from trellis.stores.base.document import DocumentStore


def fake_document_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Bind the SQLite document store's clock to a mutable ``{"now": ...}`` holder.

    ``SQLiteDocumentStore.put`` stamps both ``created_at`` and ``updated_at``
    from this one call, so a holder gives a test exact stamps instead of two
    wall-clock reads that merely *tend* to differ.

    **The vacuity caveat.** The patch is by module path, so against a store
    that is not SQLite-backed it still succeeds and has no effect — and any
    stamp assertion would then pass silently, a preserved stamp being equal to
    itself either way. Every caller therefore either asserts the seeded stamp
    directly (the cheap canary) or pairs the stamp assertion with a consequence
    test that fails loudly; each class names which.

    Shared because the caveat drifted into four paraphrases across two commits
    while it lived in four copies. It carries no call-site knowledge — it
    patches one module path and returns a holder.
    """
    holder: dict[str, Any] = {"now": datetime.now(UTC)}
    monkeypatch.setattr("trellis.stores.sqlite.document.utc_now", lambda: holder["now"])
    return holder


def keyword_recency_ratio(
    document_store: DocumentStore, query: str, *, older: str, fresher: str
) -> float:
    """Relevance of ``older`` over ``fresher`` on the keyword axis.

    Callers seed two **byte-identical** documents so their FTS base ranks are
    equal by construction and recency is the only variable left. A
    correctly-dated year-old row lands at about ``strategies.RECENCY_FLOOR``
    (0.3); a re-stamped one comes back at ~1.0.

    **Assert a margin, never a bare ``older < fresher``.**
    ``_apply_recency_decay`` resolves its reference time with its own
    ``datetime.now(UTC)`` call *per item*, so two rows carrying identical
    stamps still separate by a few hundred nanoseconds in whatever order the
    store returned them — and in this configuration the fresh row is scored
    first, consistently. A review pass ran the un-fixed code 200 times and a
    plain ordering assertion held 200/200 (ratios 0.999999999983-…997), so it
    would be **always** vacuous, not merely flaky. #397's draft had one; it was
    caught pre-merge, before #411 shipped.

    The half-life is pinned here rather than inherited from
    ``DEFAULT_RECENCY_HALF_LIFE_DAYS``: retuning that global is an ordinary
    retrieval change, but at 365.0 the ratio rises to 0.65 and every caller's
    regression assertion would read as "the bug is back". Pinning costs no
    fix-sensitivity — under the bug both stamps are identical, so the ratio is
    1.0 at every half-life. Note this does **not** immunise callers against
    ``RECENCY_FLOOR``: the coupling runs both ways, and a floor above 0.5
    breaks the same assertion.
    """
    scores = {
        item.item_id: item.relevance_score
        for item in KeywordSearch(document_store, recency_half_life_days=30.0).search(
            query, limit=10
        )
    }
    assert set(scores) == {older, fresher}, scores
    return scores[older] / scores[fresher]
