"""Shared hashing and token estimation utilities."""

from __future__ import annotations

import hashlib


def content_hash(content: str) -> str:
    """Return a truncated SHA-256 hex digest (16 chars)."""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def estimate_tokens(text: str) -> int:
    """Estimate token count (~4 chars per token)."""
    return len(text) // 4 + 1
