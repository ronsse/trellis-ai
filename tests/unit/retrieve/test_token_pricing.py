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


#: Every Claude model id an operator could plausibly name, with the input
#: price Anthropic published on 2026-09-04.  These are the *real* API ids
#: from the models overview and the model-deprecations page — not paraphrases
#: of a family name.  The pricing table is checked against this roster
#: because the property that matters ("no key silently mis-serves an id")
#: cannot be expressed over the key set alone.
PUBLISHED_INPUT_PRICES: dict[str, float] = {
    # Current generation.
    "claude-fable-5-1": 10.0,
    "claude-fable-5": 10.0,
    "claude-mythos-5-1": 10.0,
    "claude-mythos-5": 10.0,
    "claude-opus-5": 5.0,
    "claude-opus-4-8": 5.0,
    "claude-opus-4-7": 5.0,
    "claude-opus-4-6": 5.0,
    "claude-opus-4-5-20251101": 5.0,
    "claude-opus-4-5": 5.0,
    "claude-sonnet-5": 2.0,
    "claude-sonnet-4-6": 3.0,
    "claude-sonnet-4-5-20250929": 3.0,
    "claude-sonnet-4-5": 3.0,
    "claude-haiku-4-5-20251001": 1.0,
    "claude-haiku-4-5": 1.0,
    # Retired on the Claude API, still served (and still priced) on the
    # partner clouds — the population the longer keys exist for.
    "claude-opus-4-1-20250805": 15.0,
    "claude-opus-4-1": 15.0,
    "claude-opus-4-20250514": 15.0,
    "claude-sonnet-4-20250514": 3.0,
    "claude-3-5-haiku-20241022": 0.80,
}


def _winning_key(model_id: str, table: dict[str, float]) -> str | None:
    """The key ``_price_for_model`` would resolve *model_id* through."""
    matches = [key for key in table if key in model_id]
    return max(matches, key=len) if matches else None


def _shadowing_failures(table: dict[str, float], roster: dict[str, float]) -> list[str]:
    """Ids in *roster* that *table* resolves to the wrong published price.

    This is the derived form of the prefix trap: a key is only safe if, for
    every real id it wins, its price is that id's published price.
    """
    failures: list[str] = []
    for model_id, published in roster.items():
        winner = _winning_key(model_id, table)
        if winner is None:
            failures.append(f"{model_id!r}: no key matches (would fall back)")
        elif table[winner] != pytest.approx(published):
            failures.append(
                f"{model_id!r}: key {winner!r} prices it at "
                f"${table[winner]}, published ${published}"
            )
    return failures


class TestPublishedPricesAsOf20260904:
    """Verified against Anthropic's published list prices on 2026-09-04.

    These are the numbers, not a paraphrase of them: the table was 3x over on
    Opus and 1.5x over on Sonnet because a *family* is no longer one price.
    Every case below is a still-served model an operator could plausibly name.
    """

    @pytest.mark.parametrize(
        ("model", "expected"), sorted(PUBLISHED_INPUT_PRICES.items())
    )
    def test_model_prices_at_its_published_input_rate(
        self, monkeypatch, model, expected
    ):
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        _, price, source = resolve_pricing(model)
        assert source == "model_table"
        assert price == pytest.approx(expected)

    def test_no_key_mispriced_by_a_prefix_of_a_differently_priced_key(self):
        """The trap that makes this table easy to get wrong.

        Longest-key-wins only rescues an id that has a key of its own, so a
        key must never be a prefix of a differently-priced id. Adding a bare
        ``claude-opus-4`` would silently re-price 4.5 through 4.8 at the
        retired $15 tier.

        The derivation has to run over the **published ids**, not over the
        key set: for two distinct keys ``k`` and ``o``, ``o.startswith(k)``
        already implies ``len(o) > len(k)``, so a key-set-only rule of that
        shape is a tautology that cannot fail. (It was one, and it passed
        against the exact bare-``claude-opus-4`` mutant it names.)
        """
        assert _shadowing_failures(_INPUT_PRICE_PER_MTOK, PUBLISHED_INPUT_PRICES) == []

    def test_the_shadowing_check_can_actually_fail(self):
        """Guard the guard: the predicate must reject a table it should.

        Run the *shipped* helper against synthetic tables carrying each way
        this has gone wrong, so a future simplification back into a tautology
        fails here rather than silently.
        """
        roster = {"claude-opus-4-5": 5.0, "claude-opus-4-20250514": 15.0}
        # A bare family stem shadowing the differently-priced newer ids.
        assert _shadowing_failures({"claude-opus": 5.0, "claude-opus-4": 15.0}, roster)
        # A key that matches no real id, leaving one to fall back.
        assert _shadowing_failures({"claude-opus-4-5": 5.0}, roster)
        # And the honest table passes, so the check is not failing on everything.
        assert (
            _shadowing_failures(
                {
                    "claude-opus": 5.0,
                    "claude-opus-4-20250514": 15.0,
                },
                roster,
            )
            == []
        )


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
