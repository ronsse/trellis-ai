"""Every ``put_document`` caller has to say whose clock the row carries.

``preserve_updated_at`` decides whether a write re-stamps the row's
``updated_at`` — the clock ``KeywordSearch``'s recency decay reads (#406,
#417). ``True`` is right for a write that does not change what the document
says (a tag, a lifecycle stamp, derived metadata); ``False`` is right for a
write whose content is new. Only the caller knows which it is.

While the keyword defaulted to ``False``, a caller that never thought about
it got the content-write answer silently. #463's per-site roster used to
re-assert the literal ``True`` at the in-place re-puts it listed; #569
collapsed those sites into this seam and the roster was deleted as
unreachable, which left the choice unguarded. The seam-level answer is the
signature itself: keyword-only with **no default**, so an undecided caller is
a ``TypeError`` at runtime and a missing-argument error under mypy.

What this does **not** check is that a declaration is *correct* — no static
rule can tell a content write from a metadata-only one. That half is left to
each caller's own behavioural test where one exists: the #397/#406 recency
tests built on ``tests/document_recency.py`` re-put a row and compare its
``updated_at``.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from trellis.core.document_write import put_document
from trellis.stores.sqlite.document import SQLiteDocumentStore


def test_preserve_updated_at_is_keyword_only_with_no_default() -> None:
    param = inspect.signature(put_document).parameters["preserve_updated_at"]

    assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
        "preserve_updated_at must be keyword-only: a positional bool at a call "
        "site reads as noise and cannot be grepped for"
    )
    assert param.default is inspect.Parameter.empty, (
        "preserve_updated_at must have no default. A default answers the "
        "clock question for every caller that did not think about it — "
        "which is how a metadata-only write re-stamps a row's age (#406)."
    )


def test_a_caller_that_does_not_declare_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    document_store = SQLiteDocumentStore(tmp_path / "docs.db")
    try:
        with pytest.raises(TypeError, match="preserve_updated_at"):
            put_document(document_store, None, "d1", "body", {})  # type: ignore[call-arg]

        # Refused at the call boundary, not after a partial write: the
        # document plane is the authority, so a row landing here would be a
        # write carrying a clock nobody chose.
        assert document_store.get("d1") is None
    finally:
        document_store.close()
