"""Tests for ULID generation utilities."""

from trellis.core.ids import generate_prefixed_id, generate_ulid, ulid_to_timestamp


def test_generate_ulid_returns_26_char_string():
    result = generate_ulid()
    assert isinstance(result, str)
    assert len(result) == 26


def test_generate_ulid_produces_unique_values():
    ids = {generate_ulid() for _ in range(100)}
    assert len(ids) == 100


def test_generate_prefixed_id_starts_with_prefix():
    result = generate_prefixed_id("node")
    assert result.startswith("node_")
    # The part after prefix_ should be a 26-char ULID
    ulid_part = result[len("node_") :]
    assert len(ulid_part) == 26


def test_ulid_to_timestamp_returns_positive_float():
    ulid_str = generate_ulid()
    ts = ulid_to_timestamp(ulid_str)
    assert isinstance(ts, float)
    assert ts > 0


def test_ulid_to_timestamp_handles_prefixed_ids():
    prefixed = generate_prefixed_id("evt")
    ts = ulid_to_timestamp(prefixed)
    assert isinstance(ts, float)
    assert ts > 0
