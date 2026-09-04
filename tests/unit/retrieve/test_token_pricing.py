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


def _dead_claude_keys(table: dict[str, float], roster: dict[str, float]) -> list[str]:
    """``claude-`` keys in *table* that no id in *roster* resolves through.

    The other half of the prefix trap, and the one that actually shipped: a
    key that matches nothing is not merely inert, it is a *claim* that some
    model prices that way, and it hides the absence of the id it was written
    for.  ``"claude-haiku-3-5"`` sat in this table doing exactly that — the
    3.x line names its ids ``claude-3-5-haiku-…``, tier *after* version — so
    Haiku 3.5 was silently priced at the ``default_fallback`` rate while a
    test pinned the dead key's string as though it were a model.

    Scoped to ``claude-`` because the roster is a Claude roster; the OpenAI
    and ``local`` keys have no published-id list here to be checked against.
    """
    return [
        key
        for key in table
        if key.startswith("claude-")
        and not any(_winning_key(model_id, table) == key for model_id in roster)
    ]


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

    def test_every_claude_key_is_earned_by_a_published_id(self):
        """The module docstring's other claim, which nothing was checking.

        ``_shadowing_failures`` runs roster → key and so can only see a key
        that mis-serves an id it *wins*; a key that wins nothing is invisible
        to it.  That is the direction the shipped defect ran in: re-adding
        ``"claude-haiku-3-5"`` to the table leaves the whole suite green
        without this.
        """
        assert _dead_claude_keys(_INPUT_PRICE_PER_MTOK, PUBLISHED_INPUT_PRICES) == []

    def test_the_dead_key_check_can_actually_fail(self):
        """Guard the guard, same shape as the shadowing one above."""
        roster = {"claude-opus-4-5-20251101": 5.0}
        # The exact key that shipped dead, against a roster naming no 3.x id.
        assert _dead_claude_keys(
            {"claude-opus": 5.0, "claude-haiku-3-5": 0.80}, roster
        ) == ["claude-haiku-3-5"]
        # A shadowed key is dead too — it wins nothing because a longer key
        # takes every id it would have matched.
        assert _dead_claude_keys(
            {"claude-opus": 5.0, "claude-opus-4-5": 5.0}, roster
        ) == ["claude-opus"]
        # Non-Claude keys are out of scope: the roster cannot speak for them.
        assert _dead_claude_keys({"claude-opus": 5.0, "gpt-4o": 2.5}, roster) == []
        # And an honest table passes, so the check is not failing on everything.
        assert _dead_claude_keys({"claude-opus": 5.0}, roster) == []

    def test_the_roster_cannot_be_emptied_without_failing(self):
        """Both derived checks divide by the roster, so a roster that merely
        *shrinks* satisfies them vacuously — measured: emptying
        ``PUBLISHED_INPUT_PRICES`` leaves the suite green, and so does cutting
        it to one entry.  The dead-key check closes that, because every
        ``claude-`` key needs an id to earn it; this pins that it does, rather
        than leaving it as a side effect somebody later optimises away.
        """
        claude_keys = {k for k in _INPUT_PRICE_PER_MTOK if k.startswith("claude-")}
        assert claude_keys, "the table no longer prices any Claude model"
        for shrunk in ({}, dict(sorted(PUBLISHED_INPUT_PRICES.items())[:1])):
            assert _dead_claude_keys(_INPUT_PRICE_PER_MTOK, shrunk), (
                f"a roster of {len(shrunk)} entries passed the derived checks"
            )
        # The floor is derived, not a pinned count: a roster smaller than the
        # key set it has to earn cannot possibly pass.
        assert len(PUBLISHED_INPUT_PRICES) >= len(claude_keys)


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
