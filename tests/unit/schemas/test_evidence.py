"""Tests for Evidence schema."""

from __future__ import annotations

import hashlib

from trellis.schemas import AttachmentRef, Evidence, EvidenceType


class TestEvidence:
    """Tests for Evidence model."""

    def test_inline_content_computes_hash(self) -> None:
        ev = Evidence(
            evidence_type=EvidenceType.SNIPPET,
            content="print('hello world')",
            source_origin="trace",
        )
        expected = hashlib.sha256(b"print('hello world')").hexdigest()[:16]
        assert ev.content_hash == expected
        assert len(ev.evidence_id) == 26

    def test_uri_pointer_no_content(self) -> None:
        ev = Evidence(
            evidence_type=EvidenceType.LINK,
            uri="https://example.com/doc.pdf",
            source_origin="manual",
        )
        assert ev.content is None
        assert ev.content_hash == ""
        assert ev.uri == "https://example.com/doc.pdf"

    def test_evidence_with_attachments(self) -> None:
        refs = [
            AttachmentRef(target_id="trace_01", target_type="trace"),
            AttachmentRef(target_id="ent_02", target_type="entity"),
        ]
        ev = Evidence(
            evidence_type=EvidenceType.DOCUMENT,
            content="Some document text",
            source_origin="ingestion",
            source_trace_id="tr_abc",
            attached_to=refs,
        )
        assert len(ev.attached_to) == 2
        assert ev.attached_to[0].target_type == "trace"
        assert ev.source_trace_id == "tr_abc"

    def test_precomputed_hash_preserved(self) -> None:
        ev = Evidence(
            evidence_type=EvidenceType.SNIPPET,
            content="some content",
            content_hash="custom_hash_value",
            source_origin="trace",
        )
        assert ev.content_hash == "custom_hash_value"
