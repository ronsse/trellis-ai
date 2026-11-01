"""The label proposer's rules and its scan.

Every rule has a match and a near-miss, because a rule whose only test is a hit
cannot tell a narrow pattern from a pattern that matches everything. Values are
obviously fake: ``example.com``, the ``555-01xx`` fictional range, and the card
numbers payment networks publish for testing.
"""

from __future__ import annotations

from typing import Any

import pytest

from trellis.classify.label_proposer import (
    LABEL_RULES,
    LabelFinding,
    LabelScanReport,
    scan_for_label_candidates,
)
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY
from trellis.stores.sqlite.document import SQLiteDocumentStore

RULES = {rule.name: rule for rule in LABEL_RULES}


def _count(rule: str, text: str) -> int:
    return RULES[rule].count(text)


class TestRuleRoster:
    def test_the_first_set_is_the_four_named_rules(self) -> None:
        assert [rule.name for rule in LABEL_RULES] == [
            "email_address",
            "phone_number",
            "us_ssn_shape",
            "card_number_luhn",
        ]

    def test_every_rule_has_a_description(self) -> None:
        assert all(rule.description for rule in LABEL_RULES)

    def test_every_rule_is_exercised_by_a_match_below(self) -> None:
        """A rule added to the roster must come with a match in this file."""
        covered = {name for name, _text, _n in MATCHES}
        assert covered == set(RULES)


#: ``(rule, text, expected match count)``.
MATCHES = [
    ("email_address", "write to alice@example.com today", 1),
    ("email_address", "alice.b+tag@mail.example.co.uk and bob@example.org", 2),
    ("phone_number", "call (212) 555-0123 after noon", 1),
    ("phone_number", "212-555-0123", 1),
    ("phone_number", "+1 212.555.0198", 1),
    ("phone_number", "1-212-555-0100 or 212 555 0101", 2),
    ("us_ssn_shape", "ssn 123-45-6789 on file", 1),
    ("us_ssn_shape", "123-45-6789, 223-45-6789", 2),
    # ITINs start with 9 and are as sensitive as an SSN.
    ("us_ssn_shape", "tax id 923-70-1234", 1),
    ("card_number_luhn", "card 4111 1111 1111 1111 exp 12/30", 1),
    ("card_number_luhn", "4111-1111-1111-1111", 1),
    ("card_number_luhn", "4111111111111111", 1),
    ("card_number_luhn", "amex 378282246310005", 1),
    ("card_number_luhn", "amex 3782 822463 10005", 1),
    ("card_number_luhn", "mc 5555 5555 5555 4444, visa 4111111111111111", 2),
]

#: ``(rule, text)`` that the rule must NOT match.
NEAR_MISSES = [
    # email: no top-level domain, no local part, a one-letter TLD, prose.
    ("email_address", "alice@localhost"),
    ("email_address", "alice@example"),
    ("email_address", "ping @alice about it"),
    ("email_address", "a@b.c"),
    ("email_address", "alice at example dot com"),
    ("email_address", "actions/checkout@v4 and pkg@1.2.3"),
    # phone: no separators, too short, impossible area code or exchange, a
    # longer digit chain, and a date.
    ("phone_number", "2125550123"),
    ("phone_number", "212-555-012"),
    ("phone_number", "012-555-0123"),
    ("phone_number", "212-055-0123"),
    ("phone_number", "212-555-0123-4567"),
    ("phone_number", "2024-05-12"),
    ("phone_number", "ref212-555-0123"),
    # ssn: groups no SSN carries, wrong digit grouping, and no hyphens.
    ("us_ssn_shape", "000-12-3456"),
    ("us_ssn_shape", "666-12-3456"),
    ("us_ssn_shape", "123-00-6789"),
    ("us_ssn_shape", "123-45-0000"),
    ("us_ssn_shape", "1234-56-7890"),
    ("us_ssn_shape", "123-456-789"),
    ("us_ssn_shape", "123456789"),
    ("us_ssn_shape", "id-123-45-6789"),
    # card: fails Luhn, one repeated digit, too short, too long, and a longer
    # run that merely begins with a valid number.
    ("card_number_luhn", "4111 1111 1111 1112"),
    ("card_number_luhn", "0000 0000 0000 0000"),
    ("card_number_luhn", "4111 1111 1111"),
    ("card_number_luhn", "41111111111111110000"),
    ("card_number_luhn", "order-4111111111111111"),
]

#: Real-world shapes the rules over-report on. Pinned so the module docstring
#: stays true; a change that fixes one should update the docstring with it.
KNOWN_OVER_REPORTS = [
    ("email_address", "see icon@2x.png"),
    # 13 digits that pass Luhn: a millisecond timestamp.
    ("card_number_luhn", "ts=1700000000004"),
]


#: Shapes a person would call a hit that the rules do not catch. Pinned so the
#: operations guide, which names the first, stays true.
KNOWN_MISSES = [
    # A card followed by a space and a three-digit code reads as one 19-digit run
    # that fails the Luhn check.
    ("card_number_luhn", "4111 1111 1111 1111 123"),
    # An address with a non-ASCII local part.
    ("email_address", "jos\u00e9@example.com x"),
]


@pytest.mark.parametrize(("rule", "text", "expected"), MATCHES)
def test_a_rule_counts_its_matches(rule: str, text: str, expected: int) -> None:
    assert _count(rule, text) == expected


@pytest.mark.parametrize(("rule", "text"), NEAR_MISSES)
def test_a_near_miss_does_not_match(rule: str, text: str) -> None:
    assert _count(rule, text) == 0


@pytest.mark.parametrize(("rule", "text"), KNOWN_OVER_REPORTS)
def test_known_over_reports_still_match(rule: str, text: str) -> None:
    assert _count(rule, text) == 1


@pytest.mark.parametrize(("rule", "text"), KNOWN_MISSES)
def test_known_misses_still_miss(rule: str, text: str) -> None:
    assert _count(rule, text) == 0


def test_the_near_misses_cover_every_rule() -> None:
    assert {rule for rule, _text in NEAR_MISSES} == set(RULES)


def test_a_rule_returns_a_count_and_never_the_text() -> None:
    for rule in LABEL_RULES:
        assert isinstance(rule.count("alice@example.com 123-45-6789"), int)


def test_a_long_run_without_an_at_sign_is_not_quadratic() -> None:
    """The email shape is bounded, so 200 KB of dotted words scans quickly."""
    text = "a." * 100_000
    assert _count("email_address", text) == 0


@pytest.fixture
def store(tmp_path: Any) -> SQLiteDocumentStore:
    return SQLiteDocumentStore(tmp_path / "docs.db")


def _labelled(level: str = "internal") -> dict[str, Any]:
    return {DATA_CLASSIFICATION_KEY: {"sensitivity": level}}


class TestScan:
    def test_a_finding_is_a_document_a_rule_and_a_count(
        self, store: SQLiteDocumentStore
    ) -> None:
        store.put("doc-a", "mail alice@example.com and bob@example.org, 123-45-6789")
        store.put("doc-b", "nothing sensitive here")

        report = scan_for_label_candidates(store)

        assert report.findings == [
            LabelFinding("doc-a", "email_address", 2),
            LabelFinding("doc-a", "us_ssn_shape", 1),
        ]
        assert report.documents_read == 2
        assert report.documents_scanned == 2
        assert report.documents_flagged == 1
        assert not report.stopped_at_limit

    def test_a_finding_carries_no_field_that_could_hold_text(self) -> None:
        assert set(LabelFinding.__dataclass_fields__) == {"doc_id", "rule", "matches"}

    def test_a_labelled_document_is_skipped_and_counted(
        self, store: SQLiteDocumentStore
    ) -> None:
        store.put("doc-open", "alice@example.com")
        store.put("doc-internal", "alice@example.com", _labelled("internal"))
        store.put("doc-restricted", "alice@example.com", _labelled("restricted"))
        # A malformed record is a label someone meant to write.
        store.put("doc-malformed", "alice@example.com", {DATA_CLASSIFICATION_KEY: 7})

        report = scan_for_label_candidates(store)

        assert [f.doc_id for f in report.findings] == ["doc-open"]
        assert report.documents_read == 4
        assert report.skipped_labelled == 3
        assert report.documents_scanned == 1

    def test_a_document_with_no_text_is_counted_not_scanned(
        self, store: SQLiteDocumentStore
    ) -> None:
        store.put("doc-blank", "   \n")
        store.put("doc-real", "alice@example.com")

        report = scan_for_label_candidates(store)

        assert report.skipped_unscannable == 1
        assert report.documents_scanned == 1
        assert report.documents_read == 2

    def test_chunk_rows_are_not_read(self, store: SQLiteDocumentStore) -> None:
        store.put("doc-parent", "alice@example.com")
        store.put("doc-parent#chunk-0", "alice@example.com")

        report = scan_for_label_candidates(store)

        assert report.documents_read == 1
        assert [f.doc_id for f in report.findings] == ["doc-parent"]

    def test_a_limit_below_the_store_size_says_it_stopped(
        self, store: SQLiteDocumentStore
    ) -> None:
        for i in range(5):
            store.put(f"doc-{i}", f"alice{i}@example.com")

        report = scan_for_label_candidates(store, limit=3)

        assert report.documents_read == 3
        assert report.stopped_at_limit is True
        assert report.to_payload()["status"] == "partial"

    def test_a_limit_equal_to_the_store_size_is_a_complete_scan(
        self, store: SQLiteDocumentStore
    ) -> None:
        for i in range(3):
            store.put(f"doc-{i}", f"alice{i}@example.com")

        report = scan_for_label_candidates(store, limit=3)

        assert report.documents_read == 3
        assert report.stopped_at_limit is False
        assert report.to_payload()["status"] == "ok"

    def test_a_small_page_size_still_reads_every_document(
        self, store: SQLiteDocumentStore
    ) -> None:
        for i in range(7):
            store.put(f"doc-{i}", f"alice{i}@example.com")

        report = scan_for_label_candidates(store, limit=100, page_size=2)

        assert report.documents_read == 7
        assert len(report.findings) == 7
        assert report.stopped_at_limit is False

    def test_an_empty_store_is_a_complete_empty_scan(
        self, store: SQLiteDocumentStore
    ) -> None:
        report = scan_for_label_candidates(store)

        assert report == LabelScanReport(limit=report.limit)

    def test_the_scan_only_lists_documents(self) -> None:
        """Read-only at the store seam: ``list_documents`` is the one method called.

        The recorder serves one document with a finding, so the loop body that
        would write a label back actually runs.
        """
        calls: list[str] = []
        doc = {"doc_id": "doc-a", "content": "alice@example.com", "metadata": {}}

        class _Recorder:
            def __getattr__(self, name: str) -> Any:
                def _record(*_args: Any, **kwargs: Any) -> list[dict[str, Any]]:
                    calls.append(name)
                    first_page = name == "list_documents" and kwargs.get("offset") == 0
                    return [doc] if first_page else []

                return _record

        report = scan_for_label_candidates(_Recorder())  # type: ignore[arg-type]

        assert [f.doc_id for f in report.findings] == ["doc-a"]
        assert set(calls) == {"list_documents"}

    def test_the_payload_names_the_rules_and_says_a_miss_proves_nothing(
        self, store: SQLiteDocumentStore
    ) -> None:
        payload = scan_for_label_candidates(store).to_payload()

        assert payload["rules"] == [rule.name for rule in LABEL_RULES]
        assert "A miss proves nothing" in payload["notice"]
        assert "not a PII guarantee" in payload["notice"]
