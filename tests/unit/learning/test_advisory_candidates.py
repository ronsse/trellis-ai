"""``map_advisory``: ANTI_PATTERN and APPROACH advisories as candidate precedents."""

from __future__ import annotations

import pytest

from tests.advisory_candidates import (
    ADVISORY_SENTINEL,
    anti_pattern,
    approach,
    make_advisory,
    retired,
)
from trellis.learning.advisory_candidates import (
    ADVISORY_DISTILLER_VERSION,
    RULE_ADVISORY_ANTI_PATTERN,
    RULE_ADVISORY_APPROACH,
    SKIP_CATEGORY,
    SKIP_ENTITY_ID_OVERLONG,
    SKIP_MISSING_EVIDENCE,
    SKIP_MISSING_SUBJECT,
    SKIP_NO_EFFECT,
    SKIP_NOT_A_FAILURE_ASSOCIATION,
    SKIP_RETIRED,
    SKIP_SUPPRESSED,
    map_advisories,
    map_advisory,
)
from trellis.schemas.advisory import AdvisoryCategory, AdvisoryStatus
from trellis.schemas.precedent_record import (
    MAX_ACTION_CHARS,
    MAX_BECAUSE_CHARS,
    MAX_DOMAIN_CHARS,
    MAX_ID_CHARS,
    MAX_LIST_ITEMS,
    MAX_TRIGGER_CHARS,
    PrecedentRecord,
)


def _record(advisory) -> PrecedentRecord:
    result = map_advisory(advisory)
    assert result.skip is None, result.skip
    assert result.record is not None
    return result.record


# -- ANTI_PATTERN --


def test_an_anti_pattern_gives_an_avoid_on_its_item() -> None:
    record = _record(anti_pattern("doc-bad", scope="data"))

    assert record.rule == RULE_ADVISORY_ANTI_PATTERN
    assert record.action.kind == "avoid"
    assert "doc-bad" in record.action.text
    assert record.situation.entity_ids == ["doc-bad"]
    assert record.situation.domain == "data"
    assert record.provenance_tier == "deterministic"
    assert record.distiller_version == ADVISORY_DISTILLER_VERSION
    assert record.lifecycle == "candidate"


def test_the_evidence_names_the_advisory_and_its_packs_and_no_trace() -> None:
    record = _record(anti_pattern(advisory_id="adv-x", packs=("p1", "p2")))

    assert record.evidence.advisory_ids == ["adv-x"]
    assert record.evidence.pack_ids == ["p1", "p2"]
    assert record.evidence.trace_ids == []
    assert record.evidence.feedback_ids == []


def test_because_states_an_association_with_its_counts_not_a_cause() -> None:
    record = _record(anti_pattern(sample_size=8, rate_with=0.25, rate_without=0.8))

    assert record.because.startswith("Association, not a cause")
    assert "8 packs" in record.because
    assert "25%" in record.because
    assert "80%" in record.because
    assert "-55%" in record.because
    for causal in ("because", "caused", "leads to", "results in"):
        assert causal not in record.because.casefold()


@pytest.mark.parametrize("effect", [0.0, 0.2])
def test_an_anti_pattern_without_a_negative_effect_is_skipped(effect: float) -> None:
    result = map_advisory(anti_pattern(effect=effect))

    assert (result.record, result.skip) == (None, SKIP_NOT_A_FAILURE_ASSOCIATION)


def test_the_global_scope_is_no_domain() -> None:
    assert _record(anti_pattern(scope="global")).situation.domain is None


# -- APPROACH --


def test_an_approach_on_the_succeeding_side_gives_a_do() -> None:
    record = _record(approach("graph", rate_with=0.9, rate_without=0.4))

    assert record.rule == RULE_ADVISORY_APPROACH
    assert record.action.kind == "do"
    assert "graph" in record.action.text
    assert record.situation.entity_ids == []
    assert record.evidence.advisory_ids == ["adv-approach-graph"]
    assert record.because.startswith("Association, not a cause")
    assert "+50%" in record.because


def test_an_approach_on_the_failing_side_gives_an_avoid() -> None:
    record = _record(approach("keyword", rate_with=0.3, rate_without=0.8))

    assert record.action.kind == "avoid"
    assert "keyword" in record.action.text


def test_an_approach_with_no_effect_is_skipped() -> None:
    result = map_advisory(approach("graph", effect=0.0))

    assert (result.record, result.skip) == (None, SKIP_NO_EFFECT)


@pytest.mark.parametrize("strategy", [None, "", "   "])
def test_an_approach_without_a_strategy_is_skipped(strategy: str | None) -> None:
    advisory = make_advisory(
        AdvisoryCategory.APPROACH, advisory_id="adv-a", strategy=strategy
    )

    assert map_advisory(advisory).skip == SKIP_MISSING_SUBJECT


def test_an_approach_whose_strategy_is_not_text_is_skipped() -> None:
    advisory = make_advisory(
        AdvisoryCategory.APPROACH, advisory_id="adv-a", metadata={"strategy": 7}
    )

    assert map_advisory(advisory).skip == SKIP_MISSING_SUBJECT


# -- skips common to both --


def test_an_anti_pattern_without_an_item_is_skipped() -> None:
    advisory = make_advisory(AdvisoryCategory.ANTI_PATTERN, entity_id=None)

    assert map_advisory(advisory).skip == SKIP_MISSING_SUBJECT


@pytest.mark.parametrize(
    "build", [anti_pattern, approach], ids=["anti_pattern", "approach"]
)
def test_an_advisory_citing_no_pack_or_no_sample_is_skipped(build) -> None:
    assert map_advisory(build(packs=())).skip == SKIP_MISSING_EVIDENCE
    assert map_advisory(build(packs=("",))).skip == SKIP_MISSING_EVIDENCE
    assert map_advisory(build(sample_size=0)).skip == SKIP_MISSING_EVIDENCE


def test_an_overlong_item_id_is_skipped_not_truncated() -> None:
    result = map_advisory(
        anti_pattern("x" * (MAX_ID_CHARS + 1), advisory_id="adv-long")
    )

    assert (result.record, result.skip) == (None, SKIP_ENTITY_ID_OVERLONG)
    at_the_bound = anti_pattern("x" * MAX_ID_CHARS, advisory_id="adv-bound")
    assert _record(at_the_bound).situation.entity_ids == ["x" * MAX_ID_CHARS]


def test_a_suppressed_advisory_is_skipped_as_suppressed() -> None:
    advisory = anti_pattern(status=AdvisoryStatus.SUPPRESSED)

    assert map_advisory(advisory).skip == SKIP_SUPPRESSED


def test_a_rule_retired_advisory_is_skipped_as_retired() -> None:
    assert map_advisory(retired(anti_pattern())).skip == SKIP_RETIRED
    assert map_advisory(retired(approach())).skip == SKIP_RETIRED


@pytest.mark.parametrize(
    "category",
    [
        AdvisoryCategory.ENTITY,
        AdvisoryCategory.QUERY,
        AdvisoryCategory.SCOPE,
    ],
)
def test_the_other_categories_are_skipped(category: AdvisoryCategory) -> None:
    advisory = make_advisory(category, entity_id="doc-1", strategy="graph")

    assert map_advisory(advisory).skip == SKIP_CATEGORY


# -- determinism, idempotency, bounds --


def test_the_same_advisory_gives_the_same_record_and_id() -> None:
    advisory = anti_pattern()

    first, second = _record(advisory), _record(advisory)

    assert first == second
    assert first.candidate_id == first.expected_candidate_id()
    assert first.candidate_id.startswith("pcand_")


def test_a_rerun_over_a_shuffled_batch_gives_the_same_records() -> None:
    batch = [anti_pattern(f"doc-{i}") for i in range(4)] + [approach("graph")]
    shuffled = [*batch[2:], *reversed(batch[:2])]

    one, two = map_advisories(batch), map_advisories(shuffled)

    assert one.records == two.records
    assert one.by_rule == {
        RULE_ADVISORY_ANTI_PATTERN: 4,
        RULE_ADVISORY_APPROACH: 1,
    }


def test_two_findings_never_share_an_id() -> None:
    ids = {
        _record(anti_pattern("doc-a")).candidate_id,
        _record(anti_pattern("doc-b")).candidate_id,
        _record(approach("graph")).candidate_id,
        _record(approach("keyword")).candidate_id,
    }

    assert len(ids) == 4


def test_the_mapper_does_not_change_the_advisory() -> None:
    advisory = anti_pattern(scope="data")
    before = advisory.model_dump()

    map_advisory(advisory)

    assert advisory.model_dump() == before


def test_every_text_field_is_cut_to_the_schemas_bound() -> None:
    long = "s" * 500
    record = _record(
        approach(
            long,
            advisory_id="adv-long",
            scope="d" * 500,
            packs=tuple(f"p{i}" for i in range(50)),
        )
    )

    assert len(record.action.text) <= MAX_ACTION_CHARS
    assert len(record.situation.trigger) <= MAX_TRIGGER_CHARS
    assert len(record.because) <= MAX_BECAUSE_CHARS
    assert len(record.situation.domain or "") <= MAX_DOMAIN_CHARS
    assert len(record.evidence.pack_ids) == MAX_LIST_ITEMS
    # The cut record still derives its own id and validates as a whole.
    assert PrecedentRecord.model_validate(record.model_dump()) == record


def test_an_overlong_pack_id_is_left_out_of_the_evidence() -> None:
    record = _record(anti_pattern(packs=("p1", "q" * (MAX_ID_CHARS + 1))))

    assert record.evidence.pack_ids == ["p1"]


def test_a_record_that_fails_the_schema_is_skipped_not_raised() -> None:
    result = map_advisory(anti_pattern(advisory_id="a" * (MAX_ID_CHARS + 1)))

    assert (result.record, result.skip) == (None, "invalid_record")


def test_confidence_is_the_evidences_own_and_capped() -> None:
    assert _record(anti_pattern(evidence_confidence=0.99)).confidence == 0.5
    assert _record(anti_pattern(evidence_confidence=0.2)).confidence == 0.2
    assert (
        _record(anti_pattern(evidence_confidence=None, confidence=0.3)).confidence
        == 0.3
    )


def test_the_advisory_message_never_reaches_the_record() -> None:
    advisory = anti_pattern(message=f"{ADVISORY_SENTINEL} says avoid it")

    assert ADVISORY_SENTINEL not in _record(advisory).model_dump_json()


def test_the_tally_counts_every_advisory_once() -> None:
    batch = [
        anti_pattern("doc-a"),
        anti_pattern("doc-b", effect=0.1),
        retired(approach("graph")),
        make_advisory(AdvisoryCategory.QUERY, advisory_id="adv-q"),
    ]

    tally = map_advisories(batch)

    assert tally.advisories_read == 4
    assert len(tally.records) == 1
    assert tally.skips_by_reason == {
        SKIP_NOT_A_FAILURE_ASSOCIATION: 1,
        SKIP_RETIRED: 1,
        SKIP_CATEGORY: 1,
    }
