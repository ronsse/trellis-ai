"""The reference-document predicate and the link helpers (#1250).

Text here is invented.
"""

from __future__ import annotations

from typing import Any

import pytest

from trellis.retrieve.reference import (
    EXCERPT_SOURCE_KEY,
    clean_uri,
    fetched_body,
    served_text,
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
