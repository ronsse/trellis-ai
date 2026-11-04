"""The intent-family classifier, its config loader and the pack bootstrap.

The classifier is a measurement tool: it relabels history and nothing serves or
learns from it. The table below pins what the shipped rules do, including the
intents the live ``normalize_intent_family`` map gets wrong and the
word-boundary traps a bare substring match falls into.
"""

from __future__ import annotations

import copy
import math
from typing import Any

import pytest

from tests.unit.learning._intent_family_seed import SENTINEL
from trellis.errors import ConfigError
from trellis.learning import intent_families
from trellis.learning.intent_families import (
    BOOTSTRAP_RESAMPLES,
    CONFIG_SETTING,
    DEFAULT_FALLBACK_FAMILY,
    FALLBACK_RULE_ID,
    INTERVAL_METHOD,
    MAX_KEYWORDS_PER_RULE,
    MAX_RULES,
    classify_intent_family,
    default_rule_set,
    load_rule_set,
    pack_bootstrap_interval,
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
    ("implement the new exporter", "feature", "feature.implement", "implement"),
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
    # Defect vocabulary is a bug, not a miss.
    ("error in ingest", "bugfix", "bugfix.defect", "error"),
    ("errors from the exporter", "bugfix", "bugfix.defect", "errors"),
    ("traceback in the worker", "bugfix", "bugfix.defect", "traceback"),
    ("exception on startup", "bugfix", "bugfix.defect", "exception"),
    ("parser fails on unicode", "bugfix", "bugfix.defect", "fails"),
    ("the build failed", "bugfix", "bugfix.defect", "failed"),
    ("the deploy failed", "bugfix", "bugfix.defect", "failed"),
    ("bugs in the exporter", "bugfix", "bugfix.defect", "bugs"),
    ("regression in pack ranking", "bugfix", "bugfix.regression", "regression"),
    # Past tense and inflections land where the bare verb does.
    ("fixed the parser", "bugfix", "bugfix.fix", "fixed"),
    ("fixes for the writer", "bugfix", "bugfix.fix", "fixes"),
    ("refactored the store", "refactor", "refactor.core", "refactored"),
    ("renamed the module", "refactor", "refactor.core", "renamed"),
    ("added a CLI flag", "feature", "feature.build", "added"),
    ("created a migration", "feature", "feature.build", "created"),
    ("implemented the route", "feature", "feature.implement", "implemented"),
    ("analyzed the logs", "investigate", "investigate.analysis", "analyzed"),
    ("analysed the logs", "investigate", "investigate.analysis", "analysed"),
    ("investigated the stall", "investigate", "investigate.inquiry", "investigated"),
    ("documented the API", "docs", "docs.core", "documented"),
    ("reviewed the diff", "review", "review.verbs", "reviewed"),
    ("tested the parser", "test", "test.core", "tested"),
    ("testing the parser", "test", "test.core", "testing"),
    ("deployed the service", "ops", "ops.deploy", "deployed"),
    ("released v1.2", "ops", "ops.deploy", "released"),
    # False matches the first rules had: a Trellis noun, a verb of inquiry that
    # is really a noun, and a bare adjective each took work that was not theirs.
    ("add a mutation handler for retention", "feature", "feature.build", "add"),
    ("the mutation executor", "general", FALLBACK_RULE_ID, None),
    ("add audit logging to the API", "feature", "feature.build", "add"),
    ("audit trail", "general", FALLBACK_RULE_ID, None),
    ("the new exporter", "general", FALLBACK_RULE_ID, None),
    ("what's new in v2", "general", FALLBACK_RULE_ID, None),
    # "release" is ops unless the phrase is about a document.
    ("cut the release", "ops", "ops.deploy", "release"),
    ("release notes v1.2", "docs", "docs.core", "release notes"),
    ("pre-release checklist", "docs", "docs.core", "checklist"),
    # ``investigate`` outranks ``data``: the object of the work is not its kind.
    ("debug the SQL query", "investigate", "investigate.inquiry", "debug"),
    ("profiling the slow endpoint", "investigate", "investigate.inquiry", "profiling"),
    ("profile the sql warehouse", "investigate", "investigate.inquiry", "profile the"),
    (
        "investigate anomaly in pack counts",
        "investigate",
        "investigate.inquiry",
        "investigate",
    ),
    ("the sql warehouse schema", "data", "data.platform", "sql"),
    # Maintenance.
    ("update dependencies", "maintenance", "maintenance.deps", "dependencies"),
    ("bump ruff to 0.17", "maintenance", "maintenance.deps", "bump"),
    ("upgrade pydantic", "maintenance", "maintenance.deps", "upgrade"),
    ("refresh the lockfile", "maintenance", "maintenance.deps", "lockfile"),
    ("renovate config", "maintenance", "maintenance.deps", "renovate"),
    ("dependabot alerts", "maintenance", "maintenance.deps", "dependabot"),
    # Design, with a design document staying a document and an implementation
    # of a spec staying an implementation.
    ("design the schema for X", "design", "design.core", "design"),
    ("plan the migration", "design", "design.core", "plan"),
    ("architecture proposal", "design", "design.core", "architecture"),
    ("write an ADR for the store", "design", "design.core", "adr"),
    ("write the design doc", "docs", "docs.core", "doc"),
    ("implement the RFC", "feature", "feature.implement", "implement"),
    ("write a test plan", "test", "test.core", "test"),
    # Review: merging a pull request is the review's last step.
    ("merge PR #1170", "review", "review.verbs", "merge pr"),
    ("merge the pull request", "review", "review.verbs", "merge the pull request"),
    # Performance, which a regression outranks.
    ("optimize the query planner", "performance", "performance.core", "optimize"),
    ("latency of get_context", "performance", "performance.core", "latency"),
    ("speed up the test suite", "performance", "performance.core", "speed up"),
    (
        "performance regression in get_context",
        "bugfix",
        "bugfix.regression",
        "regression",
    ),
    # Word-boundary traps: a substring match would land each of these somewhere.
    ("latest news", "general", FALLBACK_RULE_ID, None),
    ("show the latest", "general", FALLBACK_RULE_ID, None),
    ("contest results", "general", FALLBACK_RULE_ID, None),
    ("prefix the label", "general", FALLBACK_RULE_ID, None),
    ("ratings of the newest items", "general", FALLBACK_RULE_ID, None),
    ("prerelease checks", "general", FALLBACK_RULE_ID, None),
    ("follow-up on the PR", "general", FALLBACK_RULE_ID, None),
    # No intent at all.
    ("hello", "general", FALLBACK_RULE_ID, None),
    ("", "general", FALLBACK_RULE_ID, None),
    ("   ", "general", FALLBACK_RULE_ID, None),
    (None, "general", FALLBACK_RULE_ID, None),
]


def test_the_table_is_wide_enough() -> None:
    assert len(TABLE) >= 100


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
        "performance",
        "refactor",
        "investigate",
        "data",
        "docs",
        "maintenance",
        "ops",
        "test",
        "feature",
        "design",
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
    "too many keywords": _block(
        keywords=[f"word{k}" for k in range(MAX_KEYWORDS_PER_RULE + 1)]
    ),
    "too many rules": {
        "intent_families": {
            "rules": [
                {"id": f"r{k}", "family": f"f{k}", "keywords": [f"w{k}"]}
                for k in range(MAX_RULES + 1)
            ]
        }
    },
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


# A config line can hold a credential by accident, so an error names a position
# and a count and never a value or a key the operator typed.
_SECRET = "SECRET-sk-live-4f9a"  # noqa: S105 - a fake credential, the thing under test
_ECHO_BLOCKS: dict[str, dict[str, Any]] = {
    "secret as a family": _block(family=_SECRET.upper()),
    "secret as a rule id": _block(id=_SECRET.upper()),
    "secret as a keyword type": _block(keywords=[{_SECRET: 1}]),
    "secret as an unknown rule key": _block(**{_SECRET: 1}),
    "secret as an unknown block key": {
        "intent_families": {**_block()["intent_families"], _SECRET: 1}
    },
    "secret as a fallback": {
        "intent_families": {**_block()["intent_families"], "fallback": _SECRET}
    },
    "secret as a repeated id": {
        "intent_families": {
            "rules": [
                {"id": _SECRET.lower(), "family": "a", "keywords": ["a"]},
                {"id": _SECRET.lower(), "family": "b", "keywords": ["b"]},
            ]
        }
    },
}


@pytest.mark.parametrize("block", _ECHO_BLOCKS.values(), ids=_ECHO_BLOCKS.keys())
def test_a_config_error_does_not_echo_what_the_block_held(
    block: dict[str, Any],
) -> None:
    with pytest.raises(ConfigError) as caught:
        load_rule_set(block)

    text = str(caught.value).lower()
    assert "secret" not in text
    assert "sk-live" not in text
    assert "4f9a" not in text


def test_a_config_error_names_a_position_and_a_count() -> None:
    repeated = {
        "intent_families": {
            "rules": [
                {"id": "a", "family": "x", "keywords": ["k"]},
                {"id": "b", "family": "y", "keywords": ["j"]},
                {"id": "a", "family": "z", "keywords": ["i"]},
            ]
        }
    }
    with pytest.raises(ConfigError) as repeated_caught:
        load_rule_set(repeated)
    assert "rules[2] repeats the id of rules[0]" in str(repeated_caught.value)

    with pytest.raises(ConfigError) as unknown_caught:
        load_rule_set(_block(**{_SECRET: 1}))
    assert "rules[0]: has a key that is not recognised" in str(unknown_caught.value)

    with pytest.raises(ConfigError) as count_caught:
        load_rule_set(MALFORMED["too many rules"])
    assert str(MAX_RULES + 1) in str(count_caught.value)


# --- Caps and compilation ----------------------------------------------------


def test_a_block_at_the_caps_loads() -> None:
    block = {
        "intent_families": {
            "rules": [
                {
                    "id": f"r{k}",
                    "family": f"f{k}",
                    "keywords": [f"word{k}x{j}" for j in range(MAX_KEYWORDS_PER_RULE)],
                }
                for k in range(MAX_RULES)
            ]
        }
    }

    rules = load_rule_set(block)

    assert len(rules.rules) == MAX_RULES
    assert classify_intent_family("word499x49", rules).rule_id == "r499"


def test_a_loaded_block_is_compiled_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = intent_families._keyword_pattern

    def counting(keyword: str):  # type: ignore[no-untyped-def]
        calls.append(keyword)
        return real(keyword)

    monkeypatch.setattr(intent_families, "_keyword_pattern", counting)
    monkeypatch.setattr(intent_families, "_COMPILED_CACHE", {})
    block = copy.deepcopy(CUSTOM)

    first = load_rule_set(block)
    after_first = len(calls)
    second = load_rule_set(copy.deepcopy(block))

    assert after_first == 3  # "ship it", "deploy", "fix"
    assert len(calls) == after_first
    assert second is first
    # A different block compiles on its own and does not return the cached set.
    other = load_rule_set(_block(keywords=["uniquely-other"]))
    assert other is not first
    assert len(calls) == after_first + 1


# --- Pack bootstrap interval -------------------------------------------------


def _wilson_width(successes: int, n: int) -> float:
    """The width of the Wilson 95% interval over ``n`` independent trials."""
    z = 1.959963984540054
    p = successes / n
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return 2 * half


def test_the_interval_method_is_named() -> None:
    assert INTERVAL_METHOD == "pack_bootstrap_95"
    assert BOOTSTRAP_RESAMPLES == 2000


def test_an_all_helpful_family_has_a_degenerate_interval() -> None:
    packs = [(3, 3)] * 25

    assert pack_bootstrap_interval(packs) == (1.0, 1.0)


def test_the_same_packs_give_the_same_interval() -> None:
    packs = [(1, 2), (3, 3), (0, 4), (2, 2), (1, 5), (4, 4)] * 4

    first = pack_bootstrap_interval(packs)
    second = pack_bootstrap_interval(list(packs))

    assert first is not None
    assert first == second
    low, high = first
    assert 0.0 <= low < high <= 1.0
    # The point estimate sits inside its own interval.
    point = sum(h for h, _ in packs) / sum(j for _, j in packs)
    assert low <= point <= high


def test_clustered_citations_widen_the_interval_past_wilson() -> None:
    # 20 packs citing 10 items each: ten packs all helpful, ten all unhelpful.
    # Wilson over the 200 citations treats them as 200 independent draws.
    packs = [(10, 10)] * 10 + [(0, 10)] * 10

    interval = pack_bootstrap_interval(packs)

    assert interval is not None
    width = interval[1] - interval[0]
    assert width > 2 * _wilson_width(100, 200)
    assert interval[0] < 0.5 < interval[1]


def test_no_packs_or_no_judged_citation_has_no_interval() -> None:
    assert pack_bootstrap_interval([]) is None
    assert pack_bootstrap_interval([(0, 0)] * 20) is None
