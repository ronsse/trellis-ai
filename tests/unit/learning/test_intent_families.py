"""The intent-family classifier, its config loader and the Wilson helper.

The classifier is a measurement tool: it relabels history and nothing serves or
learns from it. The table below pins what the shipped rules do, including the
intents the live ``normalize_intent_family`` map gets wrong and the
word-boundary traps a bare substring match falls into.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.learning._intent_family_seed import SENTINEL
from trellis.errors import ConfigError
from trellis.learning.intent_families import (
    CONFIG_SETTING,
    DEFAULT_FALLBACK_FAMILY,
    FALLBACK_RULE_ID,
    classify_intent_family,
    default_rule_set,
    load_rule_set,
    wilson_interval,
)
from trellis.learning.scoring import normalize_intent_family

# (intent, family, rule id, keyword that matched)
TABLE: list[tuple[str | None, str, str, str | None]] = [
    ("fix flaky test", "bugfix", "bugfix.fix", "fix"),
    ("review PR #12", "review", "review.verbs", "review"),
    ("write the release notes", "docs", "docs.core", "release notes"),
    ("redeploy the service", "ops", "ops.deploy", "redeploy"),
    ("deploy to production", "ops", "ops.deploy", "deploy"),
    ("refactor the retriever", "refactor", "refactor.core", "refactor"),
    ("clean up dead code", "refactor", "refactor.core", "clean up"),
    ("investigate slow queries", "investigate", "investigate.inquiry", "investigate"),
    ("why is the pack empty", "investigate", "investigate.inquiry", "why"),
    ("how does supersession work", "investigate", "investigate.inquiry", "how does"),
    ("analyze helpful rates", "investigate", "investigate.analysis", "analyze"),
    ("add a retry to the client", "feature", "feature.build", "add"),
    ("implement the new exporter", "feature", "feature.build", "implement"),
    ("wire the endpoint", "feature", "feature.build", "wire"),
    ("migrate the schema", "feature", "feature.build", "migrate"),
    ("write unit tests for the parser", "test", "test.core", "tests"),
    ("increase coverage", "test", "test.core", "coverage"),
    ("end-to-end flow", "test", "test.core", "end-to-end"),
    ("update the README", "docs", "docs.core", "readme"),
    ("document the CLI flags", "docs", "docs.core", "document the"),
    ("add docs for the exporter", "docs", "docs.core", "docs"),
    ("set up CI for the repo", "ops", "ops.infra", "ci"),
    ("rotate the cron schedule", "ops", "ops.infra", "cron"),
    ("GitHub Actions workflow", "ops", "ops.infra", "github actions"),
    ("dbt model for orders", "data", "data.platform", "dbt"),
    ("profile the sql warehouse", "data", "data.platform", "sql"),
    ("check data quality", "data", "data.platform", "data quality"),
    ("split into modules", "refactor", "refactor.core", "split into"),
    # Case and whitespace.
    ("FIX THE CRASH", "bugfix", "bugfix.fix", "fix"),
    ("root   cause of the stall", "investigate", "investigate.inquiry", "root cause"),
    # First match wins, in rule order.
    ("review and fix the deploy", "review", "review.verbs", "review"),
    ("fix the deploy script", "bugfix", "bugfix.fix", "fix"),
    ("debug the failing build", "bugfix", "bugfix.defect", "failing"),
    ("debug the build", "investigate", "investigate.inquiry", "debug"),
    ("reproduce the crash", "bugfix", "bugfix.defect", "crash"),
    # Word-boundary traps: a substring match would land each of these somewhere.
    ("latest news", "general", FALLBACK_RULE_ID, None),
    ("show the latest", "general", FALLBACK_RULE_ID, None),
    ("contest results", "general", FALLBACK_RULE_ID, None),
    ("prefix the label", "general", FALLBACK_RULE_ID, None),
    ("ratings of the newest items", "general", FALLBACK_RULE_ID, None),
    ("prerelease checks", "general", FALLBACK_RULE_ID, None),
    # No intent at all.
    ("hello", "general", FALLBACK_RULE_ID, None),
    ("", "general", FALLBACK_RULE_ID, None),
    ("   ", "general", FALLBACK_RULE_ID, None),
    (None, "general", FALLBACK_RULE_ID, None),
]


def test_the_table_is_wide_enough() -> None:
    assert len(TABLE) >= 30


@pytest.mark.parametrize(("intent", "family", "rule_id", "keyword"), TABLE)
def test_shipped_rules_classify(
    intent: str | None, family: str, rule_id: str, keyword: str | None
) -> None:
    match = classify_intent_family(intent)

    assert (match.family, match.rule_id, match.keyword) == (family, rule_id, keyword)
    assert match.is_fallback is (rule_id == FALLBACK_RULE_ID)


def test_the_shipped_fallback_is_general() -> None:
    rules = default_rule_set()

    assert rules.fallback == DEFAULT_FALLBACK_FAMILY == "general"
    assert rules.families == [
        "review",
        "bugfix",
        "refactor",
        "data",
        "investigate",
        "docs",
        "ops",
        "test",
        "feature",
        "general",
    ]
    assert rules.source == "default"


@pytest.mark.parametrize(
    ("intent", "legacy"),
    [
        # The live map sends a flaky-test fix to a data-validation family on the
        # strength of the substring "test", and a "latest" query likewise.
        ("fix flaky test", "validation_diagnostics"),
        ("latest news", "validation_diagnostics"),
        ("review PR #12", "general_context"),
        ("write the release notes", "general_context"),
    ],
)
def test_the_live_key_is_left_as_it_was(intent: str, legacy: str) -> None:
    # The relabel report calls this function; it must still answer as before.
    assert normalize_intent_family(intent=intent) == legacy
    assert classify_intent_family(intent).family != legacy


def test_a_match_never_carries_the_intent_text() -> None:
    match = classify_intent_family(f"fix the {SENTINEL} crash")

    assert SENTINEL not in repr(match)
    assert (match.family, match.rule_id) == ("bugfix", "bugfix.fix")


# --- Configuration -----------------------------------------------------------

CUSTOM: dict[str, Any] = {
    "intent_families": {
        "fallback": "misc",
        "rules": [
            {"id": "a.ship", "family": "shipping", "keywords": ["ship it", "deploy"]},
            {"id": "b.fixes", "family": "fixes", "keywords": ["fix"]},
        ],
    }
}


@pytest.mark.parametrize("block", [None, {}, {"budgets": {"max_items": 3}}])
def test_no_override_means_the_shipped_set(block: dict[str, Any] | None) -> None:
    assert load_rule_set(block) is default_rule_set()


def test_a_configured_block_replaces_the_shipped_rules() -> None:
    rules = load_rule_set(CUSTOM)

    assert rules.source == "config"
    assert rules.families == ["shipping", "fixes", "misc"]
    assert classify_intent_family("please ship   it", rules).rule_id == "a.ship"
    assert classify_intent_family("fix the deploy", rules).family == "shipping"
    assert classify_intent_family("fix it", rules).family == "fixes"
    # The shipped keywords are gone, not merged in.
    review = classify_intent_family("review PR #12", rules)
    assert (review.family, review.rule_id) == ("misc", FALLBACK_RULE_ID)


def test_the_block_sits_beside_other_retrieval_settings() -> None:
    rules = load_rule_set({"budgets": {"max_items": 3}, **CUSTOM})

    assert rules.source == "config"


def _block(**overrides: Any) -> dict[str, Any]:
    rule = {"id": "r.one", "family": "one", "keywords": ["one"], **overrides}
    return {"intent_families": {"rules": [rule]}}


MALFORMED: dict[str, Any] = {
    "null block": {"intent_families": None},
    "empty mapping": {"intent_families": {}},
    "empty list": {"intent_families": []},
    "a string": {"intent_families": "bugfix"},
    "no rules key": {"intent_families": {"fallback": "misc"}},
    "rules empty": {"intent_families": {"rules": []}},
    "rules not a list": {"intent_families": {"rules": "fix"}},
    "unknown key": {"intent_families": {"rules": [], "mode": "x"}},
    "rule without id": {
        "intent_families": {"rules": [{"family": "a", "keywords": ["a"]}]}
    },
    "rule unknown key": _block(weight=2),
    "empty keywords": _block(keywords=[]),
    "blank keyword": _block(keywords=["   "]),
    "non-string keyword": _block(keywords=[12]),
    "keyword too long": _block(keywords=["x" * 65]),
    "bad id shape": _block(id="Not A Slug"),
    "bad family shape": _block(family="Bad Family"),
    "bad fallback shape": {
        "intent_families": {**_block()["intent_families"], "fallback": "Bad Fallback"}
    },
    "reserved rule id": _block(id=FALLBACK_RULE_ID),
    "duplicate ids": {
        "intent_families": {
            "rules": [
                {"id": "dup", "family": "a", "keywords": ["a"]},
                {"id": "dup", "family": "b", "keywords": ["b"]},
            ]
        }
    },
}


@pytest.mark.parametrize("block", MALFORMED.values(), ids=MALFORMED.keys())
def test_a_malformed_block_raises_and_never_reads_as_the_defaults(
    block: dict[str, Any],
) -> None:
    with pytest.raises(ConfigError) as caught:
        load_rule_set(block)

    assert caught.value.setting == CONFIG_SETTING == "retrieval.intent_families"


def test_a_retrieval_block_that_is_not_a_mapping_raises() -> None:
    with pytest.raises(ConfigError) as caught:
        load_rule_set(["intent_families"])  # type: ignore[arg-type]

    assert caught.value.setting == "retrieval"


def test_a_config_error_does_not_echo_what_the_block_held() -> None:
    with pytest.raises(ConfigError) as caught:
        load_rule_set(_block(family=SENTINEL.upper()))

    assert SENTINEL not in str(caught.value).lower()


# --- Wilson interval ---------------------------------------------------------


@pytest.mark.parametrize(
    ("successes", "n", "low", "high"),
    [
        # Worked independently from the closed form with z = 1.959963984540054.
        (40, 60, 0.5406, 0.7727),
        (30, 60, 0.3774, 0.6226),
        (5, 5, 0.5655, 1.0),
        (0, 5, 0.0, 0.4345),
        (1, 1, 0.2065, 1.0),
    ],
)
def test_wilson_interval_matches_the_closed_form(
    successes: int, n: int, low: float, high: float
) -> None:
    got_low, got_high = wilson_interval(successes, n)

    assert got_low == pytest.approx(low, abs=1e-4)
    assert got_high == pytest.approx(high, abs=1e-4)


def test_wilson_interval_needs_a_trial() -> None:
    with pytest.raises(ValueError, match="at least one trial"):
        wilson_interval(0, 0)
