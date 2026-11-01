"""Report-only scan that points a human at documents they may want to label.

Nothing in the store writes a sensitivity label for you: the label is set by a
person (``trellis curate classify-sensitivity``), and in a large store a person
cannot be expected to find every sensitive document by reading. This module
narrows the search. It reads documents, counts how often a handful of plain
patterns match, and reports **which document, which rule, how many times**.

**It never reports the value it matched.** A finding carries a document id, a
rule name and a count, and nothing else: no excerpt, no text around the match,
not the matched string. A report that quoted the value would itself be a copy of
what the label is meant to protect, one that lands in a terminal, a CI log or an
agent's context with no label on it. The scan also logs counts only.

**It writes nothing.** No label, no tag, no event, no queue row. Applying a
label is the owner's decision, made through the existing route. This is the same
stance ``trellis classify tag-candidates`` takes for a mined ``domain`` keyword
(a wrong one hides content) and the reason there is no auto-apply here: an
access label from a heuristic is a guess about who may read a document.

**A miss proves nothing, and this is not a PII guarantee.** The rules below are
four shapes, written to be cheap to read and deterministic to run. They do not
read medical, financial or any other content that has no fixed shape, they do
not read names, and they over-report: an asset called ``icon@2x.png`` reads as
an email address, and a 13-digit millisecond timestamp passes a Luhn check one
time in ten. Precision on a real store has not been measured. A document with no
finding may hold exactly what an owner would want to label.

The rules live in :data:`LABEL_RULES`, the one place to read or change them.
Documents that already carry a ``data_classification`` record are skipped and
counted: a person has decided about them, and a proposal would second-guess it.
Chunk rows are not read; a chunk takes its parent's label, and the parent holds
the same text.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from trellis.classify.refresh import DEFAULT_PAGE_SIZE
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY

if TYPE_CHECKING:
    from trellis.stores.base.document import DocumentStore

logger = structlog.get_logger(__name__)

#: Cap on documents read per run when the caller names none.
DEFAULT_SCAN_LIMIT = 5_000

#: Said by every surface that prints the report, in this wording.
NOTICE = (
    "A miss proves nothing: a document with no finding may still hold personal "
    "or health information, and these rules do not read medical content. This "
    "is a prompt for human review, not a PII guarantee."
)

# ASCII flag on every pattern: ``\d`` and ``\w`` otherwise match other
# scripts' digits and letters, and ``int()`` accepts them.
_FLAGS = re.ASCII

# Bounded quantifiers on the email shape (RFC 5321 limits: 64 local, 63 per
# label), so a long run of word characters with no ``@`` costs a constant per
# start position rather than the length of the run.
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*"
    r"\.[A-Za-z]{2,24}(?![\w-])",
    _FLAGS,
)

# NANP shape with separators: area code and exchange start 2-9, optional
# country code, then 3-3-4 digits. Bare 10-digit runs are not matched: every
# epoch-second timestamp and numeric id would be.
_PHONE_RE = re.compile(
    r"(?<![\w-])(?:\+?1[ .-])?(?:\([2-9]\d{2}\)[ .-]?|[2-9]\d{2}[ .-])"
    r"[2-9]\d{2}[ .-]\d{4}(?![\w-])",
    _FLAGS,
)

# 3-2-4 digits, minus the groups no SSN or ITIN carries (area 000 or 666,
# group 00, serial 0000). 9xx areas stay in: ITINs start with 9.
_SSN_RE = re.compile(
    r"(?<![\w-])(?!000|666)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\w-])",
    _FLAGS,
)

# 13-19 digits, each pair optionally split by one space or hyphen. A candidate
# only; the Luhn check below decides.
_CARD_CANDIDATE_RE = re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])", _FLAGS)
_NON_DIGIT_RE = re.compile(r"\D", _FLAGS)


def _count(pattern: re.Pattern[str], text: str) -> int:
    return sum(1 for _ in pattern.finditer(text))


def _luhn_valid(digits: str) -> bool:
    """The Luhn checksum over an all-ASCII-digit string."""
    total = 0
    for position, char in enumerate(reversed(digits)):
        digit = int(char)
        if position % 2 == 1:
            # Doubled, then its digits summed: 7 -> 14 -> 1 + 4.
            digit = sum(divmod(digit * 2, 10))
        total += digit
    return total % 10 == 0


def _count_cards(text: str) -> int:
    count = 0
    for match in _CARD_CANDIDATE_RE.finditer(text):
        digits = _NON_DIGIT_RE.sub("", match.group())
        # One repeated digit ("0000 0000 0000 0000") passes Luhn and is a
        # placeholder, not a card.
        if len(set(digits)) > 1 and _luhn_valid(digits):
            count += 1
    return count


@dataclass(frozen=True)
class LabelRule:
    """One named pattern: ``count`` returns how many times it matches a text.

    ``count`` returns a number and never the matches, so a rule cannot hand a
    caller the value it found.
    """

    name: str
    description: str
    count: Callable[[str], int]


#: Every rule the scan runs, in report order. Add a rule here and nowhere else;
#: each needs a match and a near-miss in ``tests/unit/classify/test_label_proposer.py``.
LABEL_RULES: tuple[LabelRule, ...] = (
    LabelRule(
        "email_address",
        "an email-address shape (local@domain.tld)",
        lambda text: _count(_EMAIL_RE, text),
    ),
    LabelRule(
        "phone_number",
        "a US/Canada phone number with separators, such as (212) 555-0123",
        lambda text: _count(_PHONE_RE, text),
    ),
    LabelRule(
        "us_ssn_shape",
        "a US SSN or ITIN shape (3-2-4 digits), shape only, not a validity check",
        lambda text: _count(_SSN_RE, text),
    ),
    LabelRule(
        "card_number_luhn",
        "a 13-19 digit run that passes the Luhn check",
        _count_cards,
    ),
)


@dataclass(frozen=True)
class LabelFinding:
    """A document, a rule that fired on it, and how many times. No matched text."""

    doc_id: str
    rule: str
    matches: int


@dataclass
class LabelScanReport:
    """What one scan read, skipped and found.

    ``stopped_at_limit`` is true only when the store held more documents than
    ``limit`` allowed the scan to read, so ``False`` means the whole store was
    read and ``True`` means it was not.
    """

    limit: int
    documents_read: int = 0
    skipped_labelled: int = 0
    skipped_unscannable: int = 0
    stopped_at_limit: bool = False
    findings: list[LabelFinding] = field(default_factory=list)

    @property
    def documents_scanned(self) -> int:
        return self.documents_read - self.skipped_labelled - self.skipped_unscannable

    @property
    def documents_flagged(self) -> int:
        return len({finding.doc_id for finding in self.findings})

    def to_payload(self) -> dict[str, Any]:
        """The ``--format json`` view. ``status`` is ``partial`` when truncated."""
        return {
            "status": "partial" if self.stopped_at_limit else "ok",
            "limit": self.limit,
            "stopped_at_limit": self.stopped_at_limit,
            "documents_read": self.documents_read,
            "documents_scanned": self.documents_scanned,
            "skipped_labelled": self.skipped_labelled,
            "skipped_unscannable": self.skipped_unscannable,
            "documents_flagged": self.documents_flagged,
            "rules": [rule.name for rule in LABEL_RULES],
            "findings": [
                {"doc_id": f.doc_id, "rule": f.rule, "matches": f.matches}
                for f in self.findings
            ],
            "notice": NOTICE,
        }


def _is_labelled(metadata: object) -> bool:
    """Whether a person already decided: any ``data_classification`` record.

    Presence, not value. A record that is malformed reads as ``restricted``
    everywhere else (:func:`trellis.schemas.classification.sensitivity_of`), so
    it is not an unlabelled document either.
    """
    return isinstance(metadata, Mapping) and DATA_CLASSIFICATION_KEY in metadata


def scan_for_label_candidates(
    document_store: DocumentStore,
    *,
    limit: int = DEFAULT_SCAN_LIMIT,
    page_size: int = DEFAULT_PAGE_SIZE,
    rules: tuple[LabelRule, ...] = LABEL_RULES,
) -> LabelScanReport:
    """Read up to ``limit`` documents and count rule matches in each.

    Read-only: it calls ``list_documents`` and nothing else on the store. Only
    counts leave this function; document text is dropped as each row is done.
    """
    report = LabelScanReport(limit=limit)
    offset = 0
    exhausted = False

    while report.documents_read < limit:
        fetch = min(page_size, limit - report.documents_read)
        page = document_store.list_documents(
            limit=fetch, offset=offset, include_chunks=False
        )
        if not page:
            exhausted = True
            break
        offset += len(page)

        for doc in page:
            report.documents_read += 1
            if _is_labelled(doc.get("metadata")):
                report.skipped_labelled += 1
                continue
            doc_id = doc.get("doc_id")
            content = doc.get("content")
            if not (
                isinstance(doc_id, str)
                and doc_id
                and isinstance(content, str)
                and content.strip()
            ):
                report.skipped_unscannable += 1
                continue
            for rule in rules:
                matches = rule.count(content)
                if matches:
                    report.findings.append(LabelFinding(doc_id, rule.name, matches))

    if not exhausted:
        # Read exactly ``limit`` rows: the scan is whole only if nothing follows.
        report.stopped_at_limit = bool(
            document_store.list_documents(limit=1, offset=offset, include_chunks=False)
        )

    # Counts only: a finding's value is never logged, and neither is a row.
    logger.info(
        "label_candidates_scan_completed",
        documents_read=report.documents_read,
        documents_scanned=report.documents_scanned,
        skipped_labelled=report.skipped_labelled,
        skipped_unscannable=report.skipped_unscannable,
        findings=len(report.findings),
        stopped_at_limit=report.stopped_at_limit,
    )
    return report


__all__ = [
    "DEFAULT_SCAN_LIMIT",
    "LABEL_RULES",
    "NOTICE",
    "LabelFinding",
    "LabelRule",
    "LabelScanReport",
    "scan_for_label_candidates",
]
