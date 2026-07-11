"""Tests for :mod:`trellis.retrieve.token_pricing`."""

from __future__ import annotations

import pytest

from trellis.retrieve.token_pricing import (
    DEFAULT_MODEL,
    estimate_dollars,
    resolve_pricing,
)


class TestEstimateDollars:
    def test_basic_math(self):
        assert estimate_dollars(1_000_000, 3.0) == pytest.approx(3.0)
        assert estimate_dollars(30_000, 15.0) == pytest.approx(0.45)

    def test_zero_tokens(self):
        assert estimate_dollars(0, 15.0) == 0.0


class TestResolvePricing:
    def test_default_model_when_unset(self, monkeypatch):
        monkeypatch.delenv("TRELLIS_COST_MODEL", raising=False)
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        model, price, source = resolve_pricing()
        assert model == DEFAULT_MODEL
        assert price == 3.0
        assert source == "model_table"

    def test_family_substring_match(self, monkeypatch):
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        _, price, source = resolve_pricing("claude-opus-4-8")
        assert price == 15.0
        assert source == "model_table"

    def test_longest_family_key_wins(self, monkeypatch):
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        # "gpt-4o-mini-2026" contains both "gpt-4o" and "gpt-4o-mini";
        # the longer key must win.
        _, price, _ = resolve_pricing("gpt-4o-mini-2026")
        assert price == 0.15

    def test_explicit_price_override_wins(self, monkeypatch):
        monkeypatch.setenv("TRELLIS_COST_PRICE_PER_MTOK", "9.0")
        model, price, source = resolve_pricing("claude-opus", price_per_mtok=7.5)
        assert price == 7.5
        assert source == "explicit_override"
        assert model == "claude-opus"

    def test_env_price_used_when_no_override(self, monkeypatch):
        monkeypatch.setenv("TRELLIS_COST_PRICE_PER_MTOK", "9.0")
        _, price, source = resolve_pricing("claude-opus")
        assert price == 9.0
        assert source == "env_price"

    def test_env_model_used_when_no_arg(self, monkeypatch):
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        monkeypatch.setenv("TRELLIS_COST_MODEL", "claude-haiku")
        model, price, _ = resolve_pricing()
        assert model == "claude-haiku"
        assert price == 1.0

    def test_unknown_model_falls_back(self, monkeypatch):
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        model, price, source = resolve_pricing("some-unlisted-model")
        assert model == "some-unlisted-model"
        assert source == "default_fallback"
        assert price == 3.0

    def test_malformed_env_price_falls_through_to_table(self, monkeypatch):
        monkeypatch.setenv("TRELLIS_COST_PRICE_PER_MTOK", "not-a-number")
        _, price, source = resolve_pricing("claude-opus")
        assert price == 15.0
        assert source == "model_table"

    def test_local_model_is_free(self, monkeypatch):
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        _, price, _ = resolve_pricing("local")
        assert price == 0.0
