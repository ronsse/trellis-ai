"""Tests for :mod:`trellis.retrieve.token_pricing`."""

from __future__ import annotations

import pytest

from trellis.retrieve.token_pricing import (
    _INPUT_PRICE_PER_MTOK,
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
        assert price == 2.0
        assert source == "model_table"

    def test_family_substring_match(self, monkeypatch):
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        _, price, source = resolve_pricing("claude-opus-4-8")
        assert price == 5.0
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
        assert price == 2.0

    def test_malformed_env_price_falls_through_to_table(self, monkeypatch):
        monkeypatch.setenv("TRELLIS_COST_PRICE_PER_MTOK", "not-a-number")
        _, price, source = resolve_pricing("claude-opus")
        assert price == 5.0
        assert source == "model_table"

    def test_local_model_is_free(self, monkeypatch):
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        _, price, _ = resolve_pricing("local")
        assert price == 0.0


class TestPublishedPricesAsOf20260904:
    """Verified against Anthropic's published list prices on 2026-09-04.

    These are the numbers, not a paraphrase of them: the table was 3x over on
    Opus and 1.5x over on Sonnet because a *family* is no longer one price.
    Every case below is a still-served model an operator could plausibly name.
    """

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            # Current generation, priced off the bare family key.
            ("claude-opus-5", 5.0),
            ("claude-opus-4-8", 5.0),
            ("claude-opus-4-7", 5.0),
            ("claude-opus-4-6", 5.0),
            ("claude-opus-4-5", 5.0),
            ("claude-sonnet-5", 2.0),
            ("claude-haiku-4-5", 1.0),
            ("claude-haiku-4-5-20251001", 1.0),
            ("claude-fable-5-1", 10.0),
            ("claude-mythos-5", 10.0),
            # Still served, priced off their family — each needs its own key.
            ("claude-sonnet-4-6", 3.0),
            ("claude-sonnet-4-5", 3.0),
            ("claude-opus-4-1", 15.0),
            ("claude-haiku-3-5", 0.80),
        ],
    )
    def test_model_prices_at_its_published_input_rate(
        self, monkeypatch, model, expected
    ):
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        _, price, source = resolve_pricing(model)
        assert source == "model_table"
        assert price == pytest.approx(expected)

    def test_no_key_mispriced_by_a_prefix_of_a_differently_priced_key(
        self, monkeypatch
    ):
        """The trap that makes this table easy to get wrong.

        Longest-key-wins only rescues an id that has a key of its own, so a
        key must never be a prefix of a differently-priced id. Adding a bare
        ``claude-opus-4`` would silently re-price 4.5 through 4.8 at the
        retired $15 tier while every test above that names an explicit key
        stayed green.
        """
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        for key, key_price in _INPUT_PRICE_PER_MTOK.items():
            for other, other_price in _INPUT_PRICE_PER_MTOK.items():
                if other == key or key_price == other_price:
                    continue
                assert not other.startswith(key) or len(other) > len(key), (
                    f"{key!r} shadows differently-priced {other!r}"
                )
        # And the concrete regression: the family key must not swallow the
        # still-served members that price differently.
        assert resolve_pricing("claude-opus-4-1")[1] == 15.0
        assert resolve_pricing("claude-opus-4-5")[1] == 5.0
        assert resolve_pricing("claude-sonnet-4-6")[1] == 3.0
        assert resolve_pricing("claude-sonnet-5")[1] == 2.0


class TestUnrecognisedModelIsLabelledNotSilent:
    """The fallback prices at ``DEFAULT_MODEL``'s rate — which is an
    assumption about a deployment that may never have configured a Claude
    model. It is kept because it is *stated*: ``source`` says
    ``default_fallback``, and ``trellis analyze cost`` renders ``price_source``
    in both its text and JSON arms. This pins the label, so the fallback
    cannot become silent."""

    @pytest.mark.parametrize(
        "model",
        ["hermes3:8b", "llama3.2:3b", "qwen2.5-coder:7b", "some-unlisted-model"],
    )
    def test_unmatched_model_reports_default_fallback(self, monkeypatch, model):
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        monkeypatch.delenv("TRELLIS_COST_MODEL", raising=False)
        resolved, price, source = resolve_pricing(model)
        assert resolved == model
        assert source == "default_fallback"
        assert price == _INPUT_PRICE_PER_MTOK[DEFAULT_MODEL]

    def test_a_matched_model_is_not_labelled_as_a_fallback(self, monkeypatch):
        """Guards the pair: if every lookup reported ``default_fallback`` the
        test above would pass while the label carried no information."""
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        assert resolve_pricing("claude-opus-5")[2] == "model_table"
