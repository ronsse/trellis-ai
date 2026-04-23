"""Tests for the pluggable :mod:`trellis.retrieve.token_counting` layer."""

from __future__ import annotations

from dataclasses import dataclass

from trellis.retrieve.token_counting import (
    DEFAULT_TOKEN_COUNTER,
    HeuristicTokenCounter,
    TokenCounter,
)


class TestHeuristicTokenCounter:
    def test_matches_four_chars_per_token_plus_one(self) -> None:
        counter = HeuristicTokenCounter()
        assert counter.count("x" * 100) == 26
        assert counter.count("") == 1

    def test_default_name_is_stable(self) -> None:
        assert HeuristicTokenCounter().name == "heuristic_4cpt"

    def test_default_singleton_is_heuristic(self) -> None:
        assert DEFAULT_TOKEN_COUNTER.name == "heuristic_4cpt"
        assert isinstance(DEFAULT_TOKEN_COUNTER, HeuristicTokenCounter)


class TestTokenCounterProtocol:
    def test_custom_counter_conforms(self) -> None:
        @dataclass(frozen=True)
        class WordCounter:
            name: str = "word_count"

            def count(self, text: str) -> int:
                return len(text.split()) or 1

        counter: TokenCounter = WordCounter()
        assert counter.count("two word") == 2
        # runtime_checkable Protocol: isinstance works
        assert isinstance(counter, TokenCounter)
