"""Tests for the noise-demotion evidence gate (#336).

The load-bearing property is in :class:`TestGateIsNotAConstant`: this
repo's recurring defect is a measurement wired to a constant, and #336 is
itself a report of exactly that ("the gate can only return one answer").
A fix that refuses everything is the same defect pointing the other way,
so both directions are pinned.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from trellis.classify.demotion_gate import (
    MIN_ATTRIBUTED_PACKS,
    MIN_ATTRIBUTED_PACKS_ENV,
    MIN_ATTRIBUTED_PACKS_SETTING,
    MIN_UNHELPFUL_CITATIONS,
    MIN_UNHELPFUL_CITATIONS_ENV,
    MIN_UNHELPFUL_CITATIONS_SETTING,
    REFUSED_CONTESTED,
    REFUSED_INSUFFICIENT,
    REFUSED_NO_EVIDENCE,
    REFUSED_NO_UNHELPFUL,
    REFUSED_THIN_CORPUS,
    DemotionEvidence,
    NoiseDemotionScreen,
    resolve_demotion_thresholds,
    screen_noise_candidates,
)

AMPLE = MIN_ATTRIBUTED_PACKS * 4


def ev(item_id: str, *, appearances: int = 3, helpful: int = 0, unhelpful: int = 0):
    return DemotionEvidence(
        item_id=item_id,
        appearances=appearances,
        helpful_count=helpful,
        unhelpful_count=unhelpful,
    )


def screen(*evidence: DemotionEvidence, attributed_packs: int = AMPLE, **kw):
    return screen_noise_candidates(
        [e.item_id for e in evidence],
        {e.item_id: e for e in evidence},
        attributed_packs=attributed_packs,
        **kw,
    )


class TestResolveDemotionThresholds:
    """``resolve_demotion_thresholds`` is the one path every caller (the
    nightly curation worker, the REST and CLI effectiveness surfaces) uses
    to turn a settings-store snapshot into the two numbers this gate
    checks — proven here at the resolver level, independent of any one
    caller's wiring.
    """

    def test_no_env_and_no_settings_returns_the_module_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(MIN_ATTRIBUTED_PACKS_ENV, raising=False)
        monkeypatch.delenv(MIN_UNHELPFUL_CITATIONS_ENV, raising=False)

        assert resolve_demotion_thresholds(None) == (
            MIN_ATTRIBUTED_PACKS,
            MIN_UNHELPFUL_CITATIONS,
        )

    def test_a_settings_only_override_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(MIN_ATTRIBUTED_PACKS_ENV, raising=False)
        monkeypatch.delenv(MIN_UNHELPFUL_CITATIONS_ENV, raising=False)
        settings = {
            MIN_ATTRIBUTED_PACKS_SETTING: 9,
            MIN_UNHELPFUL_CITATIONS_SETTING: 4,
        }

        assert resolve_demotion_thresholds(settings) == (9, 4)

    def test_env_beats_a_settings_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MIN_ATTRIBUTED_PACKS_ENV, "11")
        monkeypatch.setenv(MIN_UNHELPFUL_CITATIONS_ENV, "7")
        settings = {
            MIN_ATTRIBUTED_PACKS_SETTING: 9,
            MIN_UNHELPFUL_CITATIONS_SETTING: 4,
        }

        assert resolve_demotion_thresholds(settings) == (11, 7)

    def test_an_unparseable_env_value_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MIN_ATTRIBUTED_PACKS_ENV, "not-a-number")
        monkeypatch.delenv(MIN_UNHELPFUL_CITATIONS_ENV, raising=False)

        packs, _citations = resolve_demotion_thresholds(None)

        assert packs == MIN_ATTRIBUTED_PACKS

    def test_an_unparseable_settings_value_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-gate #867 M6: the stored side of ``_resolve_int_threshold``
        has its own ``except`` block, independent of the env side above --
        a malformed *stored* value must fall back to the default too,
        not just a malformed env var. (The gate's M6 mutant changes this
        branch's ``return default`` to ``return 0``, which would silently
        disable the coverage floor rather than honour the module default.)
        """
        monkeypatch.delenv(MIN_ATTRIBUTED_PACKS_ENV, raising=False)
        monkeypatch.delenv(MIN_UNHELPFUL_CITATIONS_ENV, raising=False)
        settings = {MIN_ATTRIBUTED_PACKS_SETTING: "not-a-number"}

        packs, _citations = resolve_demotion_thresholds(settings)

        assert packs == MIN_ATTRIBUTED_PACKS
        assert packs != 0

    def test_a_non_finite_env_value_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``int(float("inf"))`` raises ``OverflowError``, not
        ``ValueError`` -- routing through the registry's
        ``validate_value`` (which checks ``math.isfinite`` before any
        cast) means the bare ``except (TypeError, ValueError)`` here
        never needs to see one. Proves the env side survives ``inf``.
        """
        monkeypatch.setenv(MIN_ATTRIBUTED_PACKS_ENV, "inf")
        monkeypatch.delenv(MIN_UNHELPFUL_CITATIONS_ENV, raising=False)

        packs, _citations = resolve_demotion_thresholds(None)

        assert packs == MIN_ATTRIBUTED_PACKS

    def test_a_non_finite_settings_value_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same as above, stored side: a ``nan`` stored value must not
        reach ``int()`` unguarded.
        """
        monkeypatch.delenv(MIN_ATTRIBUTED_PACKS_ENV, raising=False)
        monkeypatch.delenv(MIN_UNHELPFUL_CITATIONS_ENV, raising=False)
        settings = {MIN_ATTRIBUTED_PACKS_SETTING: float("nan")}

        packs, _citations = resolve_demotion_thresholds(settings)

        assert packs == MIN_ATTRIBUTED_PACKS

    def test_a_negative_env_value_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both thresholds are registered with ``minimum=1`` -- a
        negative env value must be rejected by that bound, not passed
        straight through to disable the floor it names.
        """
        monkeypatch.setenv(MIN_UNHELPFUL_CITATIONS_ENV, "-1")
        monkeypatch.delenv(MIN_ATTRIBUTED_PACKS_ENV, raising=False)

        _packs, citations = resolve_demotion_thresholds(None)

        assert citations == MIN_UNHELPFUL_CITATIONS

    def test_a_negative_settings_value_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(MIN_ATTRIBUTED_PACKS_ENV, raising=False)
        monkeypatch.delenv(MIN_UNHELPFUL_CITATIONS_ENV, raising=False)
        settings = {MIN_UNHELPFUL_CITATIONS_SETTING: -1}

        _packs, citations = resolve_demotion_thresholds(settings)

        assert citations == MIN_UNHELPFUL_CITATIONS

    def test_the_exact_maximum_settings_value_is_honoured_for_both_thresholds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both thresholds are registered with ``maximum=1_000_000`` -- a
        sanity cap against a typo or an overflow-class value, not an
        operational ceiling, so the cap value itself must still resolve.
        """
        monkeypatch.delenv(MIN_ATTRIBUTED_PACKS_ENV, raising=False)
        monkeypatch.delenv(MIN_UNHELPFUL_CITATIONS_ENV, raising=False)
        settings = {
            MIN_ATTRIBUTED_PACKS_SETTING: 1_000_000,
            MIN_UNHELPFUL_CITATIONS_SETTING: 1_000_000,
        }

        assert resolve_demotion_thresholds(settings) == (1_000_000, 1_000_000)

    def test_one_past_the_maximum_settings_value_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(MIN_ATTRIBUTED_PACKS_ENV, raising=False)
        monkeypatch.delenv(MIN_UNHELPFUL_CITATIONS_ENV, raising=False)
        settings = {
            MIN_ATTRIBUTED_PACKS_SETTING: 1_000_001,
            MIN_UNHELPFUL_CITATIONS_SETTING: 1_000_001,
        }

        assert resolve_demotion_thresholds(settings) == (
            MIN_ATTRIBUTED_PACKS,
            MIN_UNHELPFUL_CITATIONS,
        )

    def test_the_exact_maximum_env_value_is_honoured_for_both_thresholds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MIN_ATTRIBUTED_PACKS_ENV, "1000000")
        monkeypatch.setenv(MIN_UNHELPFUL_CITATIONS_ENV, "1000000")

        assert resolve_demotion_thresholds(None) == (1_000_000, 1_000_000)

    def test_one_past_the_maximum_env_value_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MIN_ATTRIBUTED_PACKS_ENV, "1000001")
        monkeypatch.setenv(MIN_UNHELPFUL_CITATIONS_ENV, "1000001")

        assert resolve_demotion_thresholds(None) == (
            MIN_ATTRIBUTED_PACKS,
            MIN_UNHELPFUL_CITATIONS,
        )


def _assert_no_env_value_leak(logs: list[dict], event: str, secret: str) -> dict:
    """SF3 (env half): see the matching helper in
    ``tests/unit/retrieve/test_advisory_generator.py``. The log line for
    a malformed env value must carry ``var=<ENV_NAME>`` and
    ``error_type=...``, never the raw value -- checked both as the full
    secret and as every 8-character substring of it, so a truncating
    formatter could not leak a damaging fragment either.
    """
    matches = [entry for entry in logs if entry["event"] == event]
    assert len(matches) == 1, logs
    entry = matches[0]
    rendered = repr(entry)
    assert secret not in rendered, rendered
    for start in range(len(secret) - 7):
        chunk = secret[start : start + 8]
        assert chunk not in rendered, (chunk, rendered)
    return entry


class TestEnvLogLineNeverLeaksTheRawValue:
    """SF3 (env half). The settings-store half of ``_resolve_int_knob``
    was fixed in #867; the env half -- the same function's other
    branch, shared by both thresholds -- was not.
    """

    def test_bad_env_int_logs_the_var_name_not_the_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        needle = "sk-proj-FAKEDemotionEnvNeedle000111"
        monkeypatch.setenv(MIN_ATTRIBUTED_PACKS_ENV, needle)
        monkeypatch.delenv(MIN_UNHELPFUL_CITATIONS_ENV, raising=False)

        with capture_logs() as logs:
            packs, _citations = resolve_demotion_thresholds(None)

        assert packs == MIN_ATTRIBUTED_PACKS
        entry = _assert_no_env_value_leak(logs, "demotion_gate.bad_env_int", needle)
        assert entry["var"] == MIN_ATTRIBUTED_PACKS_ENV
        assert "error_type" in entry
        assert "value" not in entry

    def test_bad_env_int_on_the_other_threshold_logs_the_var_name_not_the_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        needle = "sk-proj-FAKEDemotionEnvCitationsNeedle000111"
        monkeypatch.delenv(MIN_ATTRIBUTED_PACKS_ENV, raising=False)
        monkeypatch.setenv(MIN_UNHELPFUL_CITATIONS_ENV, needle)

        with capture_logs() as logs:
            _packs, citations = resolve_demotion_thresholds(None)

        assert citations == MIN_UNHELPFUL_CITATIONS
        entry = _assert_no_env_value_leak(logs, "demotion_gate.bad_env_int", needle)
        assert entry["var"] == MIN_UNHELPFUL_CITATIONS_ENV
        assert "error_type" in entry
        assert "value" not in entry


class TestConstantsArePinnedByValue:
    """The two thresholds, asserted as the numbers they actually are.

    Every other test in this file expresses itself *relative to* the
    constants, and the loop fixtures in
    ``tests/integration/loops/conftest.py`` derive their round count from
    ``MIN_ATTRIBUTED_PACKS`` for the same reason — so both are
    value-invariant by construction. Measured: lower the coverage floor
    from 5 to 3 and ``tests/unit/classify/`` plus
    ``tests/integration/loops/`` is **368 passed**. The whole suite
    retunes itself to the new policy, and the direction it cannot see is
    *loosening a safety floor*.

    So these two lines are the pin, and they are the place a policy
    change has to be argued. Both numbers carry a measured justification
    in :mod:`trellis.classify.demotion_gate`'s docstring — the coverage
    floor because below it a rate is not a measurement, and
    ``MIN_UNHELPFUL_CITATIONS = 2`` because one citation is produced by
    chance roughly two times in five at the observed 0.41 unhelpful base
    rate, while two puts that near 0.17 and still admits 24 of 79 live
    items. Changing either means updating that argument, not just the
    number, and this test failing is the prompt to do it.
    """

    def test_coverage_floor_value(self):
        assert MIN_ATTRIBUTED_PACKS == 5

    def test_unhelpful_citation_floor_value(self):
        assert MIN_UNHELPFUL_CITATIONS == 2

    def test_coverage_floor_agrees_with_the_retrieve_surface(self):
        """The gate's docstring claims it shares ``pack_value``'s value.

        It is imported by value, not by reference, to keep ``classify``
        free of a dependency on ``retrieve`` — which means nothing stops
        the two drifting apart and quietly applying two different
        standards to the same question. Pinned here because the claim is
        made in prose one module away from the number.
        """
        from trellis.retrieve.pack_value import (
            MIN_ATTRIBUTED_PACKS as RETRIEVE_MIN_ATTRIBUTED_PACKS,
        )

        assert MIN_ATTRIBUTED_PACKS == RETRIEVE_MIN_ATTRIBUTED_PACKS


# --------------------------------------------------------------------
# #336's own acceptance criterion, stated verbatim in the issue:
#
#   "A test proving that an item served in packs, with feedback recorded
#    but zero item attribution, is not returned as a noise candidate —
#    and that an item with an explicit unhelpful_item_ids citation still
#    is."
# --------------------------------------------------------------------
class TestIssue336Acceptance:
    def test_served_with_feedback_but_zero_attribution_is_not_demoted(self):
        """Absence of a helpful citation is not evidence of unhelpfulness."""
        result = screen(ev("served-never-cited", appearances=9))

        assert result.admitted == []
        assert result.decisions[0].reason == REFUSED_NO_UNHELPFUL

    def test_explicit_unhelpful_citation_is_demoted(self):
        result = screen(ev("cited-unhelpful", appearances=3, unhelpful=3))

        assert result.admitted == ["cited-unhelpful"]
        assert result.decisions[0].admitted is True
        assert result.decisions[0].reason == ""

    def test_serving_more_does_not_manufacture_evidence(self):
        """The old rule got *more* confident with appearances; this does not.

        ``usage_rate = 0/N`` falls below the 0.3 threshold at every N, so
        the proposal rule's certainty grew with exposure alone. Evidence
        does not accumulate by being shown to nobody.
        """
        for appearances in (2, 5, 50, 500):
            result = screen(ev("popular-but-ungraded", appearances=appearances))
            assert result.admitted == [], f"demoted at n={appearances}"


class TestGateIsNotAConstant:
    """The gate must be able to return both answers, on the same batch."""

    def test_one_batch_yields_both_verdicts(self):
        result = screen(
            ev("demote-me", appearances=4, unhelpful=3),
            ev("spare-me", appearances=4, helpful=0, unhelpful=0),
        )

        assert result.admitted == ["demote-me"]
        assert result.refused_count == 1
        # Both branches exercised — neither list may be empty.
        assert len(result.admitted) > 0
        assert result.refused_count > 0

    def test_verdict_flips_across_an_unhelpful_sweep(self):
        """Sweeping the only input that should matter must flip the verdict.

        A gate hard-wired to either answer produces a constant column
        here and fails. The sweep also pins *monotonicity*: more negative
        evidence may never turn an admission back into a refusal.
        """
        verdicts = [
            screen(ev("x", appearances=8, unhelpful=u)).admitted == ["x"]
            for u in range(6)
        ]

        assert True in verdicts, "gate never admits — constant refuse"
        assert False in verdicts, "gate never refuses — constant admit"
        # Monotone: once it flips to admit it stays admitted.
        first_admit = verdicts.index(True)
        assert all(verdicts[first_admit:]), f"non-monotone: {verdicts}"
        assert not any(verdicts[:first_admit]), f"non-monotone: {verdicts}"

    def test_threshold_is_actually_read(self):
        """A gate ignoring its own threshold is a constant with a knob."""
        candidate = ev("x", appearances=8, unhelpful=2)

        assert screen(candidate, min_unhelpful_citations=2).admitted == ["x"]
        assert screen(candidate, min_unhelpful_citations=3).admitted == []

    def test_coverage_floor_is_actually_read(self):
        candidate = ev("x", appearances=8, unhelpful=5)

        at_floor = screen(candidate, attributed_packs=MIN_ATTRIBUTED_PACKS)
        below = screen(candidate, attributed_packs=MIN_ATTRIBUTED_PACKS - 1)
        assert at_floor.admitted == ["x"]
        assert below.admitted == []


class TestCoverageSuppression:
    def test_thin_corpus_refuses_the_whole_batch(self):
        result = screen(
            ev("a", appearances=9, unhelpful=9),
            ev("b", appearances=9, unhelpful=9),
            attributed_packs=1,
        )

        assert result.suppressed is True
        assert result.suppressed_reason == REFUSED_THIN_CORPUS
        assert result.admitted == []
        assert result.refused_by_reason == {REFUSED_THIN_CORPUS: 2}
        assert all(d.reason == REFUSED_THIN_CORPUS for d in result.decisions)

    def test_suppressed_decisions_still_report_the_counts(self):
        """A coverage refusal must not fabricate zero citations.

        The suppressed branch used to build its decisions from
        ``item_id`` / ``admitted`` / ``reason`` alone, so all three
        counts fell back to their field defaults. That made a refusal on
        *window coverage* byte-identical to :data:`REFUSED_NO_EVIDENCE`'s
        honest zeros, and put the screen in direct contradiction with the
        ``item_scores`` row for the same id in the same report — which is
        exactly how it read in the CI failure that surfaced it.

        The verdict is unchanged either way; only the accounting was
        wrong. It is pinned because the counts are what a reader uses to
        decide whether the floor is the *only* thing standing between a
        candidate and demotion.
        """
        result = screen(
            ev("has-evidence", appearances=9, helpful=1, unhelpful=4),
            attributed_packs=1,
        )

        assert result.suppressed is True
        decision = result.decisions[0]
        assert decision.reason == REFUSED_THIN_CORPUS
        assert (decision.appearances, decision.helpful_count) == (9, 1)
        assert decision.unhelpful_count == 4

    def test_suppression_keeps_absent_evidence_at_zero(self):
        """The other half: a candidate with no evidence still reads zero.

        Without this the fix could have been "stamp counts from anywhere"
        — the zeros have to stay zero exactly where they are true, or
        ``REFUSED_NO_EVIDENCE`` and ``REFUSED_THIN_CORPUS`` swap which
        one is lying.
        """
        result = screen_noise_candidates(
            ["never-scored"],
            {"other": ev("other", appearances=9, unhelpful=9)},
            attributed_packs=1,
        )

        decision = result.decisions[0]
        assert decision.reason == REFUSED_THIN_CORPUS
        assert (decision.appearances, decision.unhelpful_count) == (0, 0)

    def test_ample_corpus_is_not_suppressed(self):
        result = screen(ev("a", appearances=9, unhelpful=9))

        assert result.suppressed is False
        assert result.suppressed_reason == ""

    def test_coverage_floor_alone_would_not_have_fixed_336(self):
        """Named because it is the trap the brief warned about.

        Production sits at 17 attributed packs against a floor of 5, so a
        coverage-only gate passes and demotes exactly what it demoted
        before. The per-item evidence rule is what does the work; this
        pins that the two conditions are independent.
        """
        production_like_coverage = 17
        assert production_like_coverage >= MIN_ATTRIBUTED_PACKS

        result = screen(
            ev("uncited-durable-memory", appearances=2),
            attributed_packs=production_like_coverage,
        )

        assert result.suppressed is False, "coverage gate did not fire, as expected"
        assert result.admitted == [], "…and the item is still spared, by evidence"
        assert result.decisions[0].reason == REFUSED_NO_UNHELPFUL


class TestPerItemRules:
    def test_one_citation_is_below_the_floor(self):
        result = screen(ev("x", appearances=4, unhelpful=1))

        assert result.admitted == []
        assert result.decisions[0].reason == REFUSED_INSUFFICIENT

    def test_contested_items_are_spared(self):
        """Graders disagreed. Disagreement is not a tiebreak for removal."""
        result = screen(ev("x", appearances=6, helpful=3, unhelpful=3))

        assert result.admitted == []
        assert result.decisions[0].reason == REFUSED_CONTESTED

    def test_more_unhelpful_than_helpful_still_demotes(self):
        result = screen(ev("x", appearances=9, helpful=2, unhelpful=5))

        assert result.admitted == ["x"]

    def test_missing_evidence_refuses_rather_than_admits(self):
        """Absent evidence must never be the permissive branch."""
        result = screen_noise_candidates(["ghost"], {}, attributed_packs=AMPLE)

        assert result.admitted == []
        assert result.decisions[0].reason == REFUSED_NO_EVIDENCE

    def test_evidence_none_refuses_everything(self):
        result = screen_noise_candidates(["a", "b"], None, attributed_packs=AMPLE)

        assert result.admitted == []
        assert result.refused_by_reason == {REFUSED_NO_EVIDENCE: 2}


class TestScoreRowAdaptation:
    def test_reads_an_item_scores_row(self):
        result = screen_noise_candidates(
            ["x"],
            [
                {
                    "item_id": "x",
                    "appearances": 4,
                    "referenced_count": 0,
                    "usage_rate": 0.0,
                    "unhelpful_count": 3,
                }
            ],
            attributed_packs=AMPLE,
        )

        assert result.admitted == ["x"]
        assert result.decisions[0].unhelpful_count == 3

    def test_row_without_unhelpful_count_refuses(self):
        """An older report that never counted negatives must not demote."""
        result = screen_noise_candidates(
            ["x"],
            [{"item_id": "x", "appearances": 40, "referenced_count": 0}],
            attributed_packs=AMPLE,
        )

        assert result.admitted == []
        assert result.decisions[0].reason == REFUSED_NO_UNHELPFUL

    def test_from_score_row_tolerates_none_values(self):
        record = DemotionEvidence.from_score_row(
            {"item_id": "x", "appearances": None, "unhelpful_count": None}
        )

        assert record.appearances == 0
        assert record.unhelpful_count == 0


class TestScreenShape:
    def test_empty_batch(self):
        result = screen_noise_candidates([], {}, attributed_packs=AMPLE)

        assert result.candidates_considered == 0
        assert result.admitted == []
        assert result.refused_by_reason == {}
        assert result.refused_count == 0

    def test_decisions_cover_every_candidate_in_order(self):
        result = screen(
            ev("a", appearances=3, unhelpful=3),
            ev("b", appearances=3),
            ev("c", appearances=3, unhelpful=2),
        )

        assert [d.item_id for d in result.decisions] == ["a", "b", "c"]
        assert result.candidates_considered == 3
        assert len(result.admitted) + result.refused_count == 3

    def test_reason_is_empty_exactly_when_admitted(self):
        result = screen(
            ev("a", appearances=3, unhelpful=3),
            ev("b", appearances=3),
        )

        for d in result.decisions:
            assert (d.reason == "") is d.admitted

    def test_defaults_are_reported_not_implied(self):
        result = screen(ev("a", appearances=3))

        assert result.min_attributed_packs == MIN_ATTRIBUTED_PACKS
        assert result.min_unhelpful_citations == MIN_UNHELPFUL_CITATIONS
        assert result.attributed_packs == AMPLE

    def test_screen_is_serialisable(self):
        result = screen(ev("a", appearances=3, unhelpful=3), ev("b", appearances=3))
        payload = result.model_dump(mode="json")

        assert payload["admitted"] == ["a"]
        assert payload["refused_by_reason"] == {REFUSED_NO_UNHELPFUL: 1}
        assert len(payload["decisions"]) == 2

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            NoiseDemotionScreen(candidates_considered=1, bogus=True)
