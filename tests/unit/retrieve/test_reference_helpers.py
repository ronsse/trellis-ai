"""The reference-document predicate and the link helpers (#1250).

Text here is invented.
"""

from __future__ import annotations

from typing import Any

import pytest

from trellis.retrieve.embed_ingest_hook import EMBED_INPUT_CHAR_CAP
from trellis.retrieve.embed_limits import (
    EMBED_INPUT_CHAR_CAP as CAP_IN_LIMITS,
)
from trellis.retrieve.embed_limits import (
    VECTOR_METADATA_EXCERPT_CHARS,
)
from trellis.retrieve.excerpts import truncate_excerpt
from trellis.retrieve.reference import (
    EXCERPT_SOURCE_KEY,
    clean_uri,
    fetched_body,
    served_text,
    shape_vector_row,
    summary_link_line,
    summary_of,
    summary_refusal,
)

URI = "https://docs.example.test/kiln/handbook"


class TestPredicate:
    def test_the_stripped_summary_of_a_reference(self) -> None:
        assert summary_of({"document_form": "reference", "summary": "  s  "}) == "s"

    @pytest.mark.parametrize(
        "metadata",
        [
            None,
            {},
            {"summary": "s"},
            {"document_form": "note", "summary": "s"},
            {"document_form": "reference"},
            {"document_form": "reference", "summary": ""},
            {"document_form": "reference", "summary": 7},
            {"document_form": "reference", "summary": "s", "parent_doc_id": "p"},
        ],
    )
    def test_anything_else_is_not_summary_served(
        self, metadata: dict[str, Any] | None
    ) -> None:
        assert summary_of(metadata) is None


class TestSummaryOverTheEmbedCap:
    """A summary the embedder would cut is not summary-served (#1250).

    Corpus sync does not run the write boundary's cap, so the predicate holds it:
    a summary the embedder would see only in part falls back to the body, whole.
    """

    def test_the_cap_is_one_number(self) -> None:
        assert CAP_IN_LIMITS == EMBED_INPUT_CHAR_CAP

    def test_a_summary_at_the_cap_is_served(self) -> None:
        summary = "s" * EMBED_INPUT_CHAR_CAP
        assert summary_of({"document_form": "reference", "summary": summary}) == summary

    def test_the_cap_is_on_the_stripped_summary(self) -> None:
        padded = "  " + "s" * EMBED_INPUT_CHAR_CAP + "\n"
        assert summary_of({"document_form": "reference", "summary": padded}) == (
            "s" * EMBED_INPUT_CHAR_CAP
        )

    def test_a_summary_over_the_cap_is_not_served(self) -> None:
        over = "s" * (EMBED_INPUT_CHAR_CAP + 1)
        meta = {"document_form": "reference", "summary": over}
        assert summary_of(meta) is None
        assert served_text("the body", meta) == "the body"


class TestShapeVectorRow:
    BODY = "the body of a document " * 60

    def test_a_summary_served_row_gets_the_summary_excerpt_and_the_marker(self) -> None:
        meta = {"document_form": "reference", "summary": "a short summary", "uri": URI}
        row = shape_vector_row(
            {"summary": "a short summary", "content": "stale", "uri": URI},
            self.BODY,
            meta,
        )
        assert row["content"] == "a short summary"
        assert row[EXCERPT_SOURCE_KEY] == "summary"
        assert "summary" not in row
        assert row["uri"] == URI

    def test_any_other_row_gets_the_body_excerpt_and_no_marker(self) -> None:
        row = shape_vector_row(
            {EXCERPT_SOURCE_KEY: "summary", "summary": "kept", "content": "stale"},
            self.BODY,
            {"summary": "kept"},
        )
        assert row["content"] == truncate_excerpt(
            self.BODY, VECTOR_METADATA_EXCERPT_CHARS
        )
        assert EXCERPT_SOURCE_KEY not in row
        assert row["summary"] == "kept"

    def test_the_row_is_returned_and_shaped_in_place(self) -> None:
        row: dict[str, Any] = {}
        assert shape_vector_row(row, "body", None) is row
        assert row == {"content": "body"}


class TestUriIsUntrusted:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (URI, URI),
            ("https://a.test/x`y", "https://a.test/x%60y"),
            ("https://a.test/x\n\n# heading", "https://a.test/x # heading"),
            ("a\x00b\x1b[31mc", "ab[31mc"),
            ("", None),
            ("   ", None),
            (None, None),
            (12, None),
            ("u" * 501, None),
        ],
    )
    def test_clean_uri(self, raw: object, expected: str | None) -> None:
        assert clean_uri(raw) == expected

    def test_a_hostile_uri_cannot_end_the_code_span_or_the_line(self) -> None:
        line = summary_link_line(
            {EXCERPT_SOURCE_KEY: "summary", "uri": "x`\n\n## injected\n`y"}
        )
        assert line is not None
        assert "\n" not in line
        assert line.count("`") == 2


class TestSelectors:
    def test_served_text_is_the_summary_of_a_reference_else_the_content(self) -> None:
        ref = {"document_form": "reference", "summary": "short"}
        assert served_text("the body", ref) == "short"
        assert served_text("the body", {"summary": "short"}) == "the body"

    def test_fetched_body_leads_with_the_link_only_for_a_reference(self) -> None:
        ref = {"document_form": "reference", "summary": "short", "uri": URI}
        assert fetched_body("the body", ref) == f"Source: `{URI}`\n\nthe body"
        assert fetched_body("the body", {"uri": URI}) == "the body"
        no_uri = {"document_form": "reference", "summary": "short"}
        assert fetched_body("the body", no_uri) == "the body"

    def test_the_link_line_keys_off_the_marker_not_the_uri(self) -> None:
        assert summary_link_line({"uri": URI}) is None
        assert summary_link_line({"document_form": "reference", "uri": URI}) is None
        assert summary_link_line(None) is None


class TestRefusal:
    @pytest.mark.parametrize(
        ("metadata", "expected"),
        [
            ({"document_form": "reference", "summary": 3}, "not int"),
            ({"document_form": "reference", "summary": " "}, "must not be empty"),
            ({"document_form": "reference", "summary": "abcdef"}, "6 characters"),
            ({"document_form": "reference", "summary": "abc"}, None),
            ({"document_form": "reference"}, None),
            ({"summary": 3}, None),
            (None, None),
        ],
    )
    def test_summary_refusal(
        self, metadata: dict[str, Any] | None, expected: str | None
    ) -> None:
        message = summary_refusal(metadata, limit=5)
        if expected is None:
            assert message is None
        else:
            assert message is not None
            assert expected in message
            if "characters" in expected:
                assert "limit is 5" in message
