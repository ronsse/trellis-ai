"""Tests for boundary-aware excerpt truncation and the pack content floor."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trellis.core.elision import format_char_count
from trellis.retrieve import (
    DEFAULT_CONTENT_FLOOR_PENALTY,
    DEFAULT_MIN_DISTINCT_WORDS,
    EXCERPT_ELLIPSIS,
    EXCERPT_MAX_CHARS,
    ContentFloorConfig,
    apply_content_floor,
    count_substance_words,
    truncate_excerpt,
)
from trellis.retrieve.excerpts import _MIN_BOUNDARY_FRACTION, ELISION_NOTE_MIN_LIMIT
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import SearchStrategy
from trellis.schemas.pack import PackBudget, PackItem
from trellis.stores.sqlite.event_log import SQLiteEventLog

#: Prose long enough to force truncation, with sentence and word boundaries
#: at known-awkward offsets. Deliberately not lorem ipsum — the failure mode
#: this pins (mid-word cuts) is only visible against real word shapes.
_LONG_PROSE = (
    "The mutation executor validates every command before it reaches a "
    "store. Policy gates run next, then the idempotency check, and only "
    "then does the handler execute against the backend. Every accepted "
    "mutation emits an audit event carrying the command id, the actor, "
    "and the resulting entity version, which is what makes the write path "
    "replayable after an incident. Operators who skip the executor and "
    "write straight to a store lose that replayability entirely and are "
    "the single most common cause of a graph whose node history cannot be "
    "reconciled with its event log during a postmortem investigation."
)


#: Full truncation marker: the ellipsis, optionally followed by the
#: dropped-size note that limits >= ELISION_NOTE_MIN_LIMIT get (#310).
_TRAILING_MARKER_RE = re.compile(
    re.escape(EXCERPT_ELLIPSIS) + r"(?: \[\+\d+(?:\.\d+)?[kM]? chars\])?$"
)


def _split_marker(result: str) -> tuple[str, str]:
    """Split a truncated excerpt into ``(body, marker)``."""
    match = _TRAILING_MARKER_RE.search(result)
    assert match is not None, f"no truncation marker: {result!r}"
    return result[: match.start()], match.group(0)


def _assert_clean_break(
    source: str, result: str, limit: int = EXCERPT_MAX_CHARS
) -> None:
    """Assert ``result`` is a truncation of ``source`` on a clean boundary.

    Checks the *lower* bound as well as the upper one. Without it a
    boundary search that collapses a 500-character excerpt to "See.…" still
    reads as a clean break — the failure this helper exists to catch cuts
    both ways.
    """
    body, marker = _split_marker(result)
    assert source.startswith(body), "truncation must be a prefix of the source"
    remainder = source[len(body) :]
    assert remainder, "nothing was actually truncated"
    # Either the next source character starts a new whitespace-separated
    # token (so we did not cut mid-word), or we stopped on a sentence end.
    assert remainder[:1].isspace() or body[-1:] in ".!?…", (
        f"cut mid-word: ...{body[-20:]!r} | {remainder[:20]!r}"
    )
    floor = int((limit - len(marker)) * _MIN_BOUNDARY_FRACTION)
    assert len(result) >= floor, (
        f"boundary retained only {len(result)} of {limit} chars: {result!r}"
    )


class TestTruncateExcerpt:
    def test_short_text_returned_verbatim(self) -> None:
        assert truncate_excerpt("short and sweet") == "short and sweet"

    def test_text_exactly_at_limit_is_untouched(self) -> None:
        text = "a" * EXCERPT_MAX_CHARS
        assert truncate_excerpt(text) == text

    def test_empty_text(self) -> None:
        assert truncate_excerpt("") == ""

    def test_never_exceeds_the_prior_cap(self) -> None:
        """The marker is charged against the budget, not appended to it."""
        text = _LONG_PROSE * 5
        result = truncate_excerpt(text)
        assert len(result) <= EXCERPT_MAX_CHARS
        assert len(result) <= len(text[:EXCERPT_MAX_CHARS])

    def test_no_mid_word_truncation(self) -> None:
        _assert_clean_break(_LONG_PROSE, truncate_excerpt(_LONG_PROSE))

    def test_no_mid_word_truncation_across_many_limits(self) -> None:
        """Sweep the cut point across every offset inside the prose."""
        for limit in range(20, len(_LONG_PROSE)):
            result = truncate_excerpt(_LONG_PROSE, limit)
            assert len(result) <= limit
            _assert_clean_break(_LONG_PROSE, result, limit)

    def test_prefers_sentence_boundary(self) -> None:
        text = "First sentence here. " + "tail " * 60
        result = truncate_excerpt(text, 40)
        assert result == "First sentence here." + EXCERPT_ELLIPSIS

    def test_falls_back_to_word_boundary(self) -> None:
        """No sentence end in the retained region → break on whitespace."""
        text = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
        result = truncate_excerpt(text, 20)
        assert result == "alpha beta gamma" + EXCERPT_ELLIPSIS

    def test_ignores_a_too_early_sentence_boundary(self) -> None:
        """A sentence end in the first half must not gut the excerpt."""
        text = "Ok. " + "alpha beta gamma delta epsilon zeta eta theta"
        result = truncate_excerpt(text, 30)
        assert result.startswith("Ok. alpha")
        _assert_clean_break(text, result, 30)

    def test_unbroken_token_falls_back_to_a_hard_cut(self) -> None:
        """A single giant token has no clean break — cut, but still mark it."""
        text = "x" * 900
        result = truncate_excerpt(text, 100)
        assert len(result) <= 100
        body, marker = _split_marker(result)
        assert set(body) == {"x"}
        assert marker.startswith(EXCERPT_ELLIPSIS)

    @pytest.mark.parametrize(
        "tail",
        [
            "x" * 600,  # a single unbroken token
            "https://example.invalid/" + "a" * 580,  # a signed-URL shape
            ".".join(["trellis"] * 80),  # a long dotted path
            '{"a":1,"b":2,' + '"c":3,' * 90 + "}",  # minified JSON
        ],
        ids=["unbroken", "url", "dotted-path", "minified-json"],
    )
    def test_early_sentence_end_never_guts_the_excerpt(self, tail: str) -> None:
        """Both boundary kinds are subject to the retention guard.

        The regression: an early sentence terminator was kept even when the
        word-boundary fallback was *also* too early, so the documented hard
        cut never ran and a 500-char excerpt collapsed to "Ingest failed.…".
        """
        text = "Ingest failed. " + tail
        result = truncate_excerpt(text)
        assert len(result) > EXCERPT_MAX_CHARS * _MIN_BOUNDARY_FRACTION
        assert len(result) <= EXCERPT_MAX_CHARS

    def test_does_not_split_a_decimal_or_dotted_path(self) -> None:
        text = "Version 1.2.3 of trellis.retrieve.pack_builder " + "word " * 60
        result = truncate_excerpt(text, 60)
        _assert_clean_break(text, result, 60)
        assert "1.2." not in result.removeprefix("Version 1.2.3")

    def test_whitespace_exactly_at_the_cut_uses_the_full_budget(self) -> None:
        text = "aaa bbb ccc ddd"
        # budget = 8 - 1 (marker) = 7; text[7] is the space after "bbb".
        assert truncate_excerpt(text, 8) == "aaa bbb" + EXCERPT_ELLIPSIS

    def test_degenerate_limit_shorter_than_marker(self) -> None:
        assert truncate_excerpt("abcdef", 1, marker="…") == "a"

    def test_empty_marker_suppresses_the_note_too(self) -> None:
        """A caller asking for an unmarked cut must not get a size note."""
        result = truncate_excerpt(_LONG_PROSE, 200, marker="")
        assert "chars]" not in result
        assert len(result) <= 200
        assert _LONG_PROSE.startswith(result)

    def test_caller_marker_too_wide_for_a_note_falls_back_to_bare(self) -> None:
        """No room for the note under the marker → the marker alone."""
        marker = "!" * 90
        result = truncate_excerpt(_LONG_PROSE, 100, marker=marker)
        assert result.endswith(marker)
        assert "chars]" not in result
        assert len(result) <= 100


class TestElisionNote:
    """The dropped-size note on truncated excerpts (#310)."""

    def test_truncation_carries_a_dropped_size_note(self) -> None:
        result = truncate_excerpt(_LONG_PROSE * 5)
        _body, marker = _split_marker(result)
        assert re.fullmatch(
            re.escape(EXCERPT_ELLIPSIS) + r" \[\+\d+(?:\.\d+)?[kM]? chars\]", marker
        ), f"missing size note: {marker!r}"

    def test_note_counts_the_chars_actually_dropped(self) -> None:
        """The note must describe this cut, not a nominal limit-based guess."""
        text = _LONG_PROSE * 5
        result = truncate_excerpt(text)
        body, marker = _split_marker(result)
        dropped = len(text) - len(body)
        assert marker == f"{EXCERPT_ELLIPSIS} [+{format_char_count(dropped)} chars]"

    def test_multi_k_drop_uses_the_compact_rendering(self) -> None:
        result = truncate_excerpt(_LONG_PROSE * 20)
        _, marker = _split_marker(result)
        assert re.search(r"\[\+\d+\.?\d*k chars\]$", marker), marker

    def test_small_limit_omits_the_note(self) -> None:
        """At tiny budgets the note would crowd out the text it annotates."""
        result = truncate_excerpt(_LONG_PROSE, ELISION_NOTE_MIN_LIMIT - 1)
        assert result.endswith(EXCERPT_ELLIPSIS)
        assert "chars]" not in result

    def test_note_appears_from_the_threshold_limit(self) -> None:
        result = truncate_excerpt(_LONG_PROSE, ELISION_NOTE_MIN_LIMIT)
        assert result.endswith("chars]")

    def test_note_names_the_exact_dropped_count_across_limits(self) -> None:
        """The reserved-width render is exact, not approximate.

        The earlier fixpoint render could settle either side of a width
        boundary and undercount by hundreds of characters.
        """
        text = _LONG_PROSE * 3
        for limit in range(ELISION_NOTE_MIN_LIMIT, 900, 3):
            body, marker = _split_marker(truncate_excerpt(text, limit))
            expected = format_char_count(len(text) - len(body))
            assert marker == f"{EXCERPT_ELLIPSIS} [+{expected} chars]", limit

    def test_note_is_charged_against_the_budget(self) -> None:
        """Sweep the noted regime: never longer than the raw slice."""
        text = _LONG_PROSE * 3
        for limit in range(ELISION_NOTE_MIN_LIMIT, 600, 7):
            result = truncate_excerpt(text, limit)
            assert len(result) <= limit
            _assert_clean_break(text, result, limit)


class TestTruncationAtStrategySites:
    """Pin the call sites that used to slice ``content[:500]``."""

    def test_keyword_search_excerpt(self) -> None:
        from trellis.retrieve.strategies import KeywordSearch

        content = _LONG_PROSE * 2
        store = MagicMock()
        store.search.return_value = [
            {"doc_id": "d1", "content": content, "metadata": {}, "rank": -0.8}
        ]
        item = KeywordSearch(store).search("q")[0]
        assert len(item.excerpt) <= EXCERPT_MAX_CHARS
        _assert_clean_break(content, item.excerpt)

    def test_semantic_search_excerpt(self) -> None:
        from trellis.retrieve.strategies import SemanticSearch

        content = _LONG_PROSE * 2
        store = MagicMock()
        store.query.return_value = [
            {"item_id": "v1", "score": 0.9, "metadata": {"content": content}}
        ]
        item = SemanticSearch(store, lambda _q: [0.1, 0.2]).search("q")[0]
        assert len(item.excerpt) <= EXCERPT_MAX_CHARS
        _assert_clean_break(content, item.excerpt)

    def test_semantic_excerpt_end_to_end_from_the_embed_hook(self) -> None:
        """Serve the row the embed hook actually writes, not a synthetic one.

        In production ``SemanticSearch`` reads an excerpt already capped at
        500 chars by ``build_vector_row``, so retrieval-side truncation is
        a no-op over it: a cut not made at embed time is never made at all,
        and the size note never fires (#310).
        """
        from trellis.retrieve.embed_ingest_hook import build_vector_row
        from trellis.retrieve.strategies import SemanticSearch

        content = _LONG_PROSE * 3
        row = build_vector_row("d1", content, None, lambda _t: [0.1, 0.2])
        store = MagicMock()
        store.query.return_value = [
            {"item_id": row["item_id"], "score": 0.9, "metadata": row["metadata"]}
        ]
        item = SemanticSearch(store, lambda _q: [0.1, 0.2]).search("q")[0]
        assert len(item.excerpt) <= EXCERPT_MAX_CHARS
        _assert_clean_break(content, item.excerpt)
        assert item.excerpt.endswith("chars]")

    def test_graph_search_excerpt(self) -> None:
        from trellis.retrieve.strategies import GraphSearch

        description = _LONG_PROSE * 2
        store = MagicMock()
        store.query.return_value = [
            {
                "node_id": "n1",
                "node_type": "concept",
                "node_role": "semantic",
                "properties": {"name": "executor", "description": description},
            }
        ]
        item = GraphSearch(store).search("q")[0]
        assert len(item.excerpt) <= EXCERPT_MAX_CHARS
        _assert_clean_break(description, item.excerpt)


class TestCountSubstanceWords:
    def test_empty(self) -> None:
        assert count_substance_words("") == 0

    def test_name_only_stub(self) -> None:
        assert count_substance_words("Nathan Ronsse") == 2

    def test_single_identifier(self) -> None:
        assert count_substance_words("PackBuilder") == 1

    def test_a_bare_path_is_one_unit_not_one_per_segment(self) -> None:
        """A path is one identifier; scoring it per segment defeated the floor.

        ``pi/config/labwc/rc.xml`` scored 5 — exactly the default
        ``min_distinct_words`` — because ``/`` and ``.`` are word separators,
        so a bare file path cleared the check built to demote bare names.
        Three of 24 slots on a measured failing pack went to path stubs.
        """
        assert count_substance_words("pi/config/labwc/rc.xml") == 1
        assert count_substance_words("src/trellis/retrieve/excerpts.py") == 1

    def test_a_url_does_not_score_as_substance(self) -> None:
        assert count_substance_words("https://example.com/a/b/c/d/e") < 5

    def test_prose_containing_a_path_still_counts_its_words(self) -> None:
        """The path collapses; the sentence around it does not."""
        assert (
            count_substance_words("we edited src/foo.py and then ran the migration")
            == 8
        )

    def test_repeated_paths_deduplicate(self) -> None:
        assert count_substance_words("a/b a/b a/b") == 1

    def test_hyphenated_token_counts_once(self) -> None:
        assert count_substance_words("trellis-ai") == 1

    def test_distinct_not_total(self) -> None:
        assert count_substance_words("noise noise noise noise noise") == 1

    def test_case_folded(self) -> None:
        assert count_substance_words("Trellis trellis TRELLIS") == 1

    def test_punctuation_only_tokens_do_not_count(self) -> None:
        assert count_substance_words("- - -") == 0

    def test_japanese_sentence_clears_the_floor(self) -> None:
        """Scripts without inter-word spacing tokenise as one ``\\w`` run.

        Counting that run as a single word demoted every non-English memory
        to stub rank *and* recorded ``substance_words=1`` in telemetry —
        an assertion that the item is empty, which is simply wrong.
        """
        memory = "デプロイ前にキューをドレインすること。"
        assert count_substance_words(memory) >= DEFAULT_MIN_DISTINCT_WORDS

    def test_japanese_name_still_reads_as_a_stub(self) -> None:
        """Per-character counting must not let bare names through."""
        assert count_substance_words("田中太郎") < DEFAULT_MIN_DISTINCT_WORDS

    def test_mixed_script_counts_both_halves(self) -> None:
        assert count_substance_words("trellis admin init を実行") == 6

    def test_real_one_line_gotcha_clears_the_floor(self) -> None:
        gotcha = (
            "Restart the api container before re-running trellis admin init, "
            "or the sqlite WAL goes stale."
        )
        assert count_substance_words(gotcha) >= DEFAULT_MIN_DISTINCT_WORDS

    def test_elision_note_does_not_count_as_substance(self) -> None:
        """The dropped-size note is bookkeeping, not content (#310).

        Without the strip, "[+2.3k chars]" tokenises as "2" + "3k" +
        "chars" — three free distinct words that would lift a name-only
        stub over the floor whenever its excerpt happened to be truncated.
        """
        assert count_substance_words("alpha beta… [+2.3k chars]") == 2
        assert count_substance_words("alpha beta… [+734 chars]") == 2

    def test_note_on_real_truncation_output_does_not_count(self) -> None:
        """Same property, via the actual truncator rather than a literal."""
        excerpt = truncate_excerpt("x" * 900, ELISION_NOTE_MIN_LIMIT)
        assert excerpt.endswith("chars]")
        assert count_substance_words(excerpt) == 1

    def test_bracketed_prose_still_counts(self) -> None:
        """Only the exact trailing note shape is exempt, not any brackets."""
        assert count_substance_words("see [the chars] note") == 4


class TestContentFloorConfig:
    def test_defaults(self) -> None:
        config = ContentFloorConfig()
        assert config.mode == "penalize"
        assert config.min_distinct_words == DEFAULT_MIN_DISTINCT_WORDS
        assert config.penalty == DEFAULT_CONTENT_FLOOR_PENALTY

    def test_rejects_bad_mode(self) -> None:
        with pytest.raises(ValueError, match="mode must be one of"):
            ContentFloorConfig(mode="drop")  # type: ignore[arg-type]

    def test_rejects_out_of_range_penalty(self) -> None:
        with pytest.raises(ValueError, match="penalty must be"):
            ContentFloorConfig(penalty=1.5)

    def test_rejects_negative_threshold(self) -> None:
        with pytest.raises(ValueError, match="min_distinct_words"):
            ContentFloorConfig(min_distinct_words=-1)


def _floor_item(item_id: str, excerpt: str, score: float = 1.0) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="entity",
        excerpt=excerpt,
        relevance_score=score,
        strategy_source="graph",
    )


class TestApplyContentFloor:
    def test_substantive_item_untouched(self) -> None:
        item = _floor_item("d1", "one two three four five six")
        result = apply_content_floor([item])
        assert result.items == [item]
        assert result.rejected == []
        assert result.penalized_item_ids == []

    def test_boundary_just_over_threshold_is_untouched(self) -> None:
        # Exactly DEFAULT_MIN_DISTINCT_WORDS distinct words → clears the floor.
        item = _floor_item("d1", "alpha beta gamma delta epsilon")
        assert count_substance_words(item.excerpt) == DEFAULT_MIN_DISTINCT_WORDS
        result = apply_content_floor([item])
        assert result.penalized_item_ids == []
        assert result.items[0].relevance_score == 1.0

    def test_boundary_just_under_threshold_is_penalized(self) -> None:
        item = _floor_item("d1", "alpha beta gamma delta")
        assert count_substance_words(item.excerpt) == DEFAULT_MIN_DISTINCT_WORDS - 1
        result = apply_content_floor([item])
        assert result.penalized_item_ids == ["d1"]
        assert result.items[0].relevance_score == pytest.approx(
            DEFAULT_CONTENT_FLOOR_PENALTY
        )

    def test_penalty_is_recorded_in_score_breakdown(self) -> None:
        result = apply_content_floor([_floor_item("n1", "Nathan Ronsse")])
        breakdown = result.items[0].score_breakdown
        assert breakdown["content_floor_penalty"] == DEFAULT_CONTENT_FLOOR_PENALTY
        assert breakdown["content_floor_substance_words"] == 2.0
        assert breakdown["relevance_score"] == pytest.approx(
            DEFAULT_CONTENT_FLOOR_PENALTY
        )

    def test_penalize_never_drops(self) -> None:
        items = [_floor_item("n1", ""), _floor_item("n2", "x")]
        result = apply_content_floor(items)
        assert [i.item_id for i in result.items] == ["n1", "n2"]
        assert result.rejected == []

    def test_elision_note_cannot_lift_a_thin_item_over_the_floor(self) -> None:
        """A truncated stub is still a stub — the note words don't count."""
        item = _floor_item("n1", "alpha beta… [+9.9k chars]")
        result = apply_content_floor([item])
        assert result.penalized_item_ids == ["n1"]
        breakdown = result.items[0].score_breakdown
        assert breakdown["content_floor_substance_words"] == 2.0

    def test_exclude_mode_drops_and_records_a_rejection(self) -> None:
        config = ContentFloorConfig(mode="exclude")
        items = [_floor_item("n1", "Nathan Ronsse"), _floor_item("d1", "a b c d e f")]
        result = apply_content_floor(items, config)
        assert [i.item_id for i in result.items] == ["d1"]
        assert len(result.rejected) == 1
        assert result.rejected[0].item_id == "n1"
        assert result.rejected[0].reason == "content_floor"
        assert result.rejected[0].strategy_source == "graph"

    def test_exempt_item_type_is_never_measured(self) -> None:
        """A Measurement excerpt is terse by construction, not by emptiness.

        ``MeasurementRecordHandler`` writes no ``content`` property, so the
        excerpt renders as "row_count = 41823" — two distinct words for
        *every* Measurement ever served. Without the exemption the floor
        demotes the whole item class on every build, permanently.
        """
        item = PackItem(
            item_id="m1",
            item_type="observation",
            excerpt="row_count = 41823 rows",
            relevance_score=1.0,
        )
        result = apply_content_floor([item])
        assert result.items == [item]
        assert result.penalized_item_ids == []

    def test_exemption_can_be_lifted(self) -> None:
        item = PackItem(
            item_id="m1",
            item_type="observation",
            excerpt="row_count = 41823",
            relevance_score=1.0,
        )
        config = ContentFloorConfig(exempt_item_types=frozenset())
        assert apply_content_floor([item], config).penalized_item_ids == ["m1"]

    def test_off_mode_is_a_noop(self) -> None:
        config = ContentFloorConfig(mode="off")
        items = [_floor_item("n1", "Nathan Ronsse")]
        result = apply_content_floor(items, config)
        assert result.items == items
        assert result.penalized_item_ids == []

    def test_telemetry_shape(self) -> None:
        items = [_floor_item("n1", "Nathan Ronsse"), _floor_item("d1", "a b c d e f")]
        telemetry = apply_content_floor(items).as_telemetry()
        assert telemetry["mode"] == "penalize"
        assert telemetry["min_distinct_words"] == DEFAULT_MIN_DISTINCT_WORDS
        assert telemetry["penalized_item_ids"] == ["n1"]
        assert telemetry["penalized_count"] == 1
        assert telemetry["excluded_count"] == 0

    def test_telemetry_reports_off_mode(self) -> None:
        telemetry = apply_content_floor(
            [_floor_item("n1", "Nathan Ronsse")], ContentFloorConfig(mode="off")
        ).as_telemetry()
        assert telemetry["mode"] == "off"
        assert telemetry["penalized_count"] == 0


def _strategy(name: str, items: list[PackItem]) -> SearchStrategy:
    strategy = MagicMock(spec=SearchStrategy)
    strategy.name = name
    strategy.search.return_value = items
    return strategy


_STUB_EXCERPT = "Nathan Ronsse"
_SUBSTANTIVE_EXCERPT = (
    "The advisory generator reads FEEDBACK_RECORDED events and suppresses "
    "items whose outcomes trend negative."
)
#: A legitimately terse memory — a one-line gotcha under the floor.
_TERSE_BUT_REAL = "Never reformat unrelated files."


class TestPackBuilderContentFloor:
    def test_stub_is_demoted_below_substantive_content(self) -> None:
        """A name-only node loses its head start over a real memory."""
        strategy = _strategy(
            "kw",
            [
                _floor_item("stub", _STUB_EXCERPT, score=1.0),
                _floor_item("real", _SUBSTANTIVE_EXCERPT, score=0.6),
            ],
        )
        pack = PackBuilder(strategies=[strategy]).build("q")
        assert [item.item_id for item in pack.items] == ["real", "stub"]

    def test_terse_but_real_item_is_still_served(self) -> None:
        """The floor demotes; it must never silently delete short content."""
        strategy = _strategy("kw", [_floor_item("gotcha", _TERSE_BUT_REAL, score=0.5)])
        pack = PackBuilder(strategies=[strategy]).build("q")
        assert [item.item_id for item in pack.items] == ["gotcha"]
        assert pack.items[0].score_breakdown["content_floor_penalty"] == (
            DEFAULT_CONTENT_FLOOR_PENALTY
        )

    def test_terse_item_survives_a_budget_squeeze_when_nothing_competes(self) -> None:
        """The default mode is the difference between served and deleted.

        Asserting only that the item survives is not discriminating — it
        survives with the floor switched off too. Running the same pack
        under ``mode="exclude"`` and asserting it comes back *empty* is what
        pins "penalize never starves a thin pool" as a real property.
        """
        budget = PackBudget(max_items=1, max_tokens=100)

        def build(floor: ContentFloorConfig | None) -> list[str]:
            strategy = _strategy(
                "kw", [_floor_item("gotcha", _TERSE_BUT_REAL, score=0.5)]
            )
            pack = PackBuilder(strategies=[strategy], content_floor=floor).build(
                "q", budget=budget
            )
            return [item.item_id for item in pack.items]

        assert build(None) == ["gotcha"]
        assert build(ContentFloorConfig(mode="exclude")) == []

    def test_exclude_mode_records_a_rejected_item(self) -> None:
        strategy = _strategy(
            "kw",
            [
                _floor_item("stub", _STUB_EXCERPT, score=1.0),
                _floor_item("real", _SUBSTANTIVE_EXCERPT, score=0.6),
            ],
        )
        builder = PackBuilder(
            strategies=[strategy],
            content_floor=ContentFloorConfig(mode="exclude"),
        )
        pack = builder.build("q")
        assert [item.item_id for item in pack.items] == ["real"]
        reasons = {r.item_id: r.reason for r in pack.retrieval_report.rejected_items}
        assert reasons == {"stub": "content_floor"}

    def test_off_mode_preserves_prior_ranking(self) -> None:
        strategy = _strategy(
            "kw",
            [
                _floor_item("stub", _STUB_EXCERPT, score=1.0),
                _floor_item("real", _SUBSTANTIVE_EXCERPT, score=0.6),
            ],
        )
        builder = PackBuilder(
            strategies=[strategy], content_floor=ContentFloorConfig(mode="off")
        )
        pack = builder.build("q")
        assert [item.item_id for item in pack.items] == ["stub", "real"]
        assert pack.items[0].relevance_score == 1.0


class TestContentFloorTelemetry:
    """Floor decisions must be visible in PACK_ASSEMBLED, not silent."""

    def test_penalized_item_appears_in_payload(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        try:
            strategy = _strategy(
                "kw",
                [
                    _floor_item("stub", _STUB_EXCERPT, score=1.0),
                    _floor_item("real", _SUBSTANTIVE_EXCERPT, score=0.6),
                ],
            )
            PackBuilder(strategies=[strategy], event_log=event_log).build("q")
            payload = event_log.get_events(limit=10)[0].payload
            floor = payload["content_floor"]
            assert floor["mode"] == "penalize"
            assert floor["penalized_item_ids"] == ["stub"]
            assert floor["excluded_count"] == 0
            # Per-item detail rides the existing injected_items block.
            by_id = {row["item_id"]: row for row in payload["injected_items"]}
            assert by_id["stub"]["score_breakdown"]["content_floor_penalty"] == (
                DEFAULT_CONTENT_FLOOR_PENALTY
            )
            assert "content_floor_penalty" not in by_id["real"]["score_breakdown"]
        finally:
            event_log.close()

    def test_excluded_item_appears_in_payload(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        try:
            strategy = _strategy(
                "kw",
                [
                    _floor_item("stub", _STUB_EXCERPT, score=1.0),
                    _floor_item("real", _SUBSTANTIVE_EXCERPT, score=0.6),
                ],
            )
            builder = PackBuilder(
                strategies=[strategy],
                event_log=event_log,
                content_floor=ContentFloorConfig(mode="exclude"),
            )
            builder.build("q")
            payload = event_log.get_events(limit=10)[0].payload
            assert payload["content_floor"]["excluded_item_ids"] == ["stub"]
            rejected = payload["rejected_items"]
            assert [r["reason"] for r in rejected] == ["content_floor"]
        finally:
            event_log.close()

    def test_sectioned_exclusion_is_recorded_on_the_section_report(self) -> None:
        """The returned object must be self-describing, not only the event.

        The floor runs over the shared pool before section assignment, so a
        dropped item has no single "best" section; it is recorded on every
        section whose filter it matched — every report where its absence
        would otherwise be unexplained.
        """
        from trellis.schemas.pack import SectionRequest

        strategy = _strategy(
            "kw",
            [
                _floor_item("stub", _STUB_EXCERPT, score=1.0),
                _floor_item("real", _SUBSTANTIVE_EXCERPT, score=0.6),
            ],
        )
        builder = PackBuilder(
            strategies=[strategy],
            content_floor=ContentFloorConfig(mode="exclude"),
        )
        sectioned = builder.build_sectioned(
            "q", sections=[SectionRequest(name="all", max_items=5, max_tokens=5000)]
        )
        section = sectioned.sections[0]
        assert [item.item_id for item in section.items] == ["real"]
        rejected = section.retrieval_report.rejected_items
        assert [(r.item_id, r.reason) for r in rejected] == [("stub", "content_floor")]

    def test_sectioned_pack_emits_the_floor_summary(self, tmp_path: Path) -> None:
        from trellis.schemas.pack import SectionRequest

        event_log = SQLiteEventLog(tmp_path / "events.db")
        try:
            strategy = _strategy(
                "kw",
                [
                    _floor_item("stub", _STUB_EXCERPT, score=1.0),
                    _floor_item("real", _SUBSTANTIVE_EXCERPT, score=0.6),
                ],
            )
            builder = PackBuilder(strategies=[strategy], event_log=event_log)
            builder.build_sectioned(
                "q",
                sections=[SectionRequest(name="all", max_items=5, max_tokens=5000)],
            )
            payload = event_log.get_events(limit=10)[0].payload
            assert payload["content_floor"]["penalized_item_ids"] == ["stub"]
        finally:
            event_log.close()
