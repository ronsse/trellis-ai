"""A software-work intent family set and an offline report that relabels history.

**Measurement only.** The learning key is ``(intent_family, item_id)`` and the
live key comes from :func:`trellis.learning.scoring.normalize_intent_family`:
six data-platform keyword rows that fall back to ``general_context``. A
software intent such as "fix flaky test" or "review PR" lands in
``general_context``, or in a data family by accident because a substring
matched. Before the key changes, the open question is whether a software-work
family set would separate helpfulness at all. This module answers it offline,
over history, and nothing that serves or learns reads it. Changing the key is a
post-readout decision.

The pin is one direction, and ``tests/unit/test_intent_families_import_rule.py``
holds it: the scoring, observation, retrieval, tagging and MCP surfaces do not
import this module, its CLI command or its REST route, and do not spell its config
key as a string. The other direction is open: this module calls
``normalize_intent_family`` and the shared pack joins.

**The rule set.** Ordered, deterministic, first match wins. A rule is an id, a
family and a list of keywords; a keyword is a word or a phrase and matches on
word boundaries, case-insensitively (``latest`` does not match ``test``, and a
phrase tolerates any run of whitespace between its words). There is no
stemming: an inflection the family should catch is listed. The shipped default
(:data:`DEFAULT_RULES`) carries ``review``, ``bugfix``, ``performance``,
``refactor``, ``investigate``, ``data``, ``docs``, ``maintenance``, ``ops``,
``test``, ``design`` and ``feature``, and an intent no rule matches falls to the
fallback family (``general``) under the reserved rule id ``fallback``.

Order is the priority, and it is chosen by which word names the *work* rather
than its object: a review verb outranks everything, a fix verb outranks the
thing being fixed (so "fix flaky test" is ``bugfix``, not ``test``), and the
broad building verbs (``add``, ``write``) come last so ``add tests`` is ``test``
and ``add a CI job`` is ``ops``. Three orderings are decisions, each pinned in
the classifier table: ``regression`` outranks ``performance`` (a "performance
regression" is a bug), ``investigate`` outranks ``data`` ("debug the SQL query"
is an investigation), and ``docs`` outranks ``design`` ("write the design doc"
is a document). A long intent that names several kinds of work lands in the
earliest rule it touches; the report's rule ids make that visible.

**Per-deployment override.** The ``retrieval.intent_families`` block of
``config.yaml`` replaces the shipped rules whole (no merge, so the effective set
is always readable from one place)::

    retrieval:
      intent_families:
        fallback: general        # optional
        rules:
          - id: bugfix.fix
            family: bugfix
            keywords: [fix, bug, "root cause"]

Unlike ``retrieval.budgets``, which degrades to its defaults with a warning, a
malformed block raises :class:`~trellis.errors.ConfigError`: a silently
substituted rule set would relabel history under names the operator did not
choose, and the report would not say so. A present-but-empty block is malformed
for the same reason; delete it to get the defaults. The error names positions
(``rules[3]``) and counts, never a rule id, a key name or a keyword, because a
config line can carry anything. A block is capped at :data:`MAX_RULES` rules of
:data:`MAX_KEYWORDS_PER_RULE` keywords, and is compiled once per distinct block.

**The report** (:func:`summarize_intent_families`) reads the served packs and
the feedback that grades them through the same join ``analyze value`` uses
(:func:`~trellis.learning.pack_observations.join_pack_feedback_with_coverage`,
which drops held-out packs and the feedback naming them) and the typed readers
in :mod:`trellis.feedback.pack_rows`. It reads no session outcome and splits
nothing by holdout arm. Citations are joined as ``analyze value`` joins them: a
cited id counts only if the pack served it, an item cited both helpful and
unhelpful is helpful, and a cited id the pack never served is counted apart as
``cited_not_served``. Per new family it states the packs, the graded packs, the
helpful and unhelpful citations, the citation-weighted helpful rate, the mean
pack rating (one vote per ``feedback_id``) and the top matched rule ids, beside
the share of packs in the fallback and a legacy-by-new cross-tab. A family below
:data:`MIN_GRADED_PACKS` graded packs has its rates withheld with the reason
stated; its counts are still shown.

**The interval's unit is the pack.** Citations cluster by pack (one grader
cites many items from one delivery), so an interval over citations is far too
narrow. The helpful rate's 95% interval is a percentile bootstrap that resamples
graded *packs* with replacement (:data:`BOOTSTRAP_RESAMPLES` resamples, a fixed
seed, so the output is deterministic) and recomputes the citation-weighted rate
on each; the payload names it ``interval_method: "pack_bootstrap_95"``. Two
non-overlapping intervals are a hint to look at the packs, not a separation.

**It never prints an intent.** Intents are private corpus text and the report is
published. Every string in it is a family name, a rule id or a fixed sentence.
"""

from __future__ import annotations

import json
import math
import random
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from pydantic import Field, ValidationError, field_validator, model_validator

from trellis.core.base import TrellisModel
from trellis.errors import ConfigError
from trellis.feedback.attribution import is_sectioned_payload, served_item_ids
from trellis.feedback.pack_rows import has_served_rows, iter_feedback_rows
from trellis.learning.pack_observations import join_pack_feedback_with_coverage
from trellis.learning.scoring import normalize_intent_family
from trellis.retrieve.pack_value import collect_pack_verdicts
from trellis.stores.base.event_log import (
    DEFAULT_SCAN_LIMIT,
    EventLog,
    ScanCoverage,
    resolve_scan_window,
)

logger = structlog.get_logger(__name__)

#: ``config.yaml`` path of the override, for the ``ConfigError`` hint.
CONFIG_SETTING: Final = "retrieval.intent_families"
#: The key inside the ``retrieval:`` block.
CONFIG_KEY: Final = "intent_families"

#: The rule id every fallback match carries. A configured rule may not use it.
FALLBACK_RULE_ID: Final = "fallback"
DEFAULT_FALLBACK_FAMILY: Final = "general"

#: Graded packs a family needs before its rates are stated.
MIN_GRADED_PACKS: Final = 20
#: Rule ids named per family in the report.
TOP_RULES: Final = 5

#: A configured block above either cap is a ``ConfigError``.
MAX_RULES: Final = 500
MAX_KEYWORDS_PER_RULE: Final = 50

#: The helpful rate's interval: a percentile bootstrap over graded packs.
INTERVAL_METHOD: Final = "pack_bootstrap_95"
BOOTSTRAP_RESAMPLES: Final = 2000
#: Fixed so the same log gives the same interval on every run and every host.
BOOTSTRAP_SEED: Final = 1179
_BOOTSTRAP_ALPHA: Final = 0.05

STATUS_OK: Final = "ok"
STATUS_TRUNCATED: Final = "truncated"

_NAME = re.compile(r"^[a-z][a-z0-9_.-]*$")
_MAX_KEYWORD_LENGTH: Final = 64
_NAME_RULE: Final = (
    "must be lowercase letters, digits, '_', '.' or '-', starting with a letter"
)


class IntentFamilyError(ValueError):
    """A request the report refuses: an unparseable ``--since`` or ``--until``."""


# --- Rules -----------------------------------------------------------------


class IntentFamilyRule(TrellisModel):
    """One ordered rule: any of its keywords matching puts the intent in ``family``."""

    id: str
    family: str
    keywords: list[str] = Field(min_length=1, max_length=MAX_KEYWORDS_PER_RULE)

    @field_validator("id", "family")
    @classmethod
    def _name_shape(cls, value: str) -> str:
        if not _NAME.match(value):
            msg = _NAME_RULE
            raise ValueError(msg)
        return value

    @field_validator("keywords")
    @classmethod
    def _keywords_usable(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for keyword in value:
            text = keyword.strip()
            if not text or len(text) > _MAX_KEYWORD_LENGTH:
                msg = f"a keyword must be 1 to {_MAX_KEYWORD_LENGTH} characters"
                raise ValueError(msg)
            cleaned.append(text)
        return cleaned

    @model_validator(mode="after")
    def _id_not_reserved(self) -> IntentFamilyRule:
        if self.id == FALLBACK_RULE_ID:
            msg = f"the rule id {FALLBACK_RULE_ID!r} is reserved for the fallback"
            raise ValueError(msg)
        return self


class IntentFamilyConfig(TrellisModel):
    """The ``retrieval.intent_families`` block."""

    fallback: str = DEFAULT_FALLBACK_FAMILY
    rules: list[IntentFamilyRule] = Field(min_length=1, max_length=MAX_RULES)

    @field_validator("fallback")
    @classmethod
    def _fallback_shape(cls, value: str) -> str:
        if not _NAME.match(value):
            msg = _NAME_RULE
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _ids_unique(self) -> IntentFamilyConfig:
        first_seen: dict[str, int] = {}
        for position, rule in enumerate(self.rules):
            if rule.id in first_seen:
                # Positions, not the id: a config line can carry anything.
                msg = (
                    f"rules[{position}] repeats the id of rules[{first_seen[rule.id]}]"
                )
                raise ValueError(msg)
            first_seen[rule.id] = position
        return self


def _rule(rule_id: str, family: str, *keywords: str) -> IntentFamilyRule:
    return IntentFamilyRule(id=rule_id, family=family, keywords=list(keywords))


#: The shipped set, in priority order. See the module docstring for the order.
DEFAULT_RULES: Final[tuple[IntentFamilyRule, ...]] = (
    _rule(
        "review.verbs",
        "review",
        "review",
        "reviews",
        "reviewed",
        "reviewing",
        "reviewer",
        "re-review",
        "code review",
        "critique",
        "lgtm",
        "merge pr",
        "merge prs",
        "merge the pr",
        "merge the prs",
        "merge pull request",
        "merge the pull request",
    ),
    # Before ``performance`` on purpose: "performance regression" is a bug.
    _rule(
        "bugfix.regression",
        "bugfix",
        "regression",
        "regressions",
        "regressed",
    ),
    _rule(
        "performance.core",
        "performance",
        "optimize",
        "optimise",
        "optimized",
        "optimised",
        "optimizing",
        "optimising",
        "optimization",
        "optimisation",
        "performance",
        "perf",
        "latency",
        "throughput",
        "speed up",
        "speed-up",
        "benchmark",
        "benchmarks",
        "profile memory",
        "memory profiling",
    ),
    _rule(
        "bugfix.fix",
        "bugfix",
        "fix",
        "fixes",
        "fixed",
        "fixing",
        "bugfix",
        "hotfix",
        "repair",
        "repairs",
        "repaired",
        "repairing",
        "workaround",
    ),
    _rule(
        "bugfix.defect",
        "bugfix",
        "bug",
        "bugs",
        "crash",
        "crashes",
        "crashed",
        "crashing",
        "broken",
        "flaky",
        "failing",
        "fails",
        "fail",
        "failed",
        "exception",
        "exceptions",
        "traceback",
        "tracebacks",
        "stack trace",
    ),
    # A bare ``error`` or ``failure`` is also a noun of design work ("add error
    # handling", "retry on failure", "error budget"), so only the phrases that
    # report a defect count. A rule of its own keeps both under the keyword cap.
    _rule(
        "bugfix.error_phrase",
        "bugfix",
        "error in",
        "errors in",
        "error from",
        "errors from",
        "error on",
        "errors on",
        "error when",
        "errors when",
        "error during",
        "errors during",
        "error while",
        "errors while",
        "error with",
        "errors with",
        "runtime error",
        "syntax error",
        "type error",
        "import error",
        "unexpected error",
        "unexpected errors",
        "test failure",
        "test failures",
        "build failure",
        "build failures",
        "ci failure",
        "ci failures",
        "failure in",
        "failures in",
        "failure of",
        "failures of",
        "failure when",
        "failures when",
        "failure during",
        "failures during",
        "failure to",
    ),
    _rule(
        "refactor.core",
        "refactor",
        "refactor",
        "refactors",
        "refactored",
        "refactoring",
        "restructure",
        "restructured",
        "reorganize",
        "reorganized",
        "reorganise",
        "reorganised",
        "rename",
        "renames",
        "renamed",
        "renaming",
        "cleanup",
        "clean up",
        "cleaned up",
        "tidy up",
        "tidied up",
        "tidying up",
        "simplify",
        "simplified",
        "dedupe",
        "deduped",
        "deduplicate",
        "deduplicated",
        "consolidate",
        "consolidated",
        "decompose",
        "decomposed",
        "split into",
    ),
    _rule(
        "investigate.inquiry",
        "investigate",
        "investigate",
        "investigated",
        "investigating",
        "investigation",
        "debug",
        "debugged",
        "debugging",
        "diagnose",
        "diagnosed",
        "diagnosing",
        "triage",
        "triaged",
        "root cause",
        "why",
        "how does",
        "how do",
        "figure out",
        "find out",
        "look into",
        "profile the",
        "profiled",
        "profiling",
        "profiler",
    ),
    _rule(
        "investigate.analysis",
        "investigate",
        "analyze",
        "analyzed",
        "analyse",
        "analysed",
        "analyzing",
        "analysing",
        "analysis",
        "research",
        "researched",
        "survey",
        "explore",
        "explored",
        "measure",
        "measured",
        "measuring",
        "compare",
        "compared",
        "evaluate",
        "evaluated",
        "reproduce",
        "reproduced",
    ),
    # After ``investigate`` on purpose: "debug the SQL query" is an investigation
    # whose object happens to be data.
    _rule(
        "data.platform",
        "data",
        "data pipeline",
        "data quality",
        "dataset",
        "datasets",
        "dbt",
        "etl",
        "sql",
        "pyspark",
        "lineage",
        "pii",
        "eda",
        "anomaly",
        "anomalies",
        "data drift",
        "schema drift",
        "warehouse",
        "lakehouse",
    ),
    # Before ``design`` on purpose: "write the design doc" is a document.
    _rule(
        "docs.core",
        "docs",
        "docs",
        "doc",
        "documentation",
        "readme",
        "changelog",
        "release notes",
        "docstring",
        "docstrings",
        "write-up",
        "writeup",
        "write up",
        "tutorial",
        "how-to",
        "playbook",
        "playbooks",
        "runbook",
        "blog post",
        "checklist",
        "document the",
        "documented",
        "documenting",
    ),
    _rule(
        "maintenance.deps",
        "maintenance",
        # ``dependency`` and ``upgrade`` stand alone as nouns of other work
        # ("add dependency injection", "deploying the new upgrade"), so only the
        # phrases that name the upkeep itself count.
        "update dependencies",
        "update the dependencies",
        "updated dependencies",
        "updating dependencies",
        "upgrade dependencies",
        "upgrade the dependencies",
        "upgraded dependencies",
        "upgrading dependencies",
        "upgrading the dependencies",
        "dependency update",
        "dependency updates",
        "dependency upgrade",
        "dependency upgrades",
        "deps",
        "bump",
        "bumps",
        "bumped",
        "bumping",
        "lockfile",
        "lock file",
        "renovate",
        "dependabot",
    ),
    _rule(
        "ops.deploy",
        "ops",
        "deploy",
        "deploys",
        "deploying",
        "deployed",
        "deployment",
        "redeploy",
        "redeploying",
        "redeployed",
        "redeployment",
        "rollout",
        "rolled out",
        "rollback",
        "rolled back",
        "release",
        "releases",
        "released",
        "releasing",
        "publish",
        "published",
        "publishing",
        "pypi",
    ),
    _rule(
        "ops.infra",
        "ops",
        "ci",
        "ci/cd",
        "github actions",
        "infra",
        "infrastructure",
        "docker",
        "dockerfile",
        "container",
        "containers",
        "kubernetes",
        "k8s",
        "terraform",
        "helm",
        "systemd",
        "cron",
        "backup",
        "backups",
        "monitoring",
        "alerting",
        "incident",
        "outage",
        "provisioning",
    ),
    _rule(
        "test.core",
        "test",
        "test",
        "tests",
        "tested",
        "testing",
        "retest",
        "pytest",
        "unittest",
        "coverage",
        "mutant",
        "mutants",
        "e2e",
        "end-to-end",
        "fixture",
        "fixtures",
    ),
    # "Implement the RFC" is the work of implementing, so it outranks ``design``,
    # whose nouns ("rfc", "spec") would otherwise take it.
    _rule(
        "feature.implement",
        "feature",
        "implement",
        "implements",
        "implemented",
        "implementing",
        "implementation",
    ),
    _rule(
        "design.core",
        "design",
        "design",
        "designs",
        "designed",
        "designing",
        # ``plan`` alone is a noun of other work ("add a plan command"), so only
        # the phrases that name the planning count.
        "plan the",
        "planning",
        "design plan",
        "architecture",
        "architect",
        "proposal",
        "proposals",
        "adr",
        "rfc",
        "spec",
        "specs",
        "specification",
    ),
    _rule(
        "feature.build",
        "feature",
        "add",
        "adds",
        "added",
        "adding",
        "build",
        "building",
        "create",
        "creates",
        "created",
        "creating",
        "introduce",
        "introduced",
        "feature",
        "features",
        "support",
        "wire",
        "wired",
        "wiring",
        "extend",
        "extending",
        "enable",
        "expose",
        "integrate",
        "migrate",
        "migrated",
        "migrating",
        "write",
        "wrote",
        "writing",
    ),
)


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    """A word-boundary pattern; a phrase tolerates any whitespace between its words."""
    body = r"\s+".join(re.escape(part) for part in keyword.split())
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FamilyMatch:
    """Where an intent landed and why.

    ``keyword`` is the configured keyword that matched (never the intent's
    text), ``None`` for the fallback.
    """

    family: str
    rule_id: str
    keyword: str | None

    @property
    def is_fallback(self) -> bool:
        return self.rule_id == FALLBACK_RULE_ID


@dataclass(frozen=True, slots=True)
class _CompiledRule:
    rule_id: str
    family: str
    patterns: tuple[tuple[str, re.Pattern[str]], ...]


@dataclass(frozen=True, slots=True)
class RuleSet:
    """A compiled, ordered rule set and where it came from."""

    rules: tuple[_CompiledRule, ...]
    fallback: str
    #: ``"default"`` (the shipped set) or ``"config"`` (the override block).
    source: str

    @classmethod
    def from_config(cls, config: IntentFamilyConfig, *, source: str) -> RuleSet:
        return cls(
            rules=tuple(
                _CompiledRule(
                    rule_id=rule.id,
                    family=rule.family,
                    patterns=tuple((kw, _keyword_pattern(kw)) for kw in rule.keywords),
                )
                for rule in config.rules
            ),
            fallback=config.fallback,
            source=source,
        )

    @property
    def families(self) -> list[str]:
        """Every family the set can return, in first-rule order, fallback last."""
        names: list[str] = []
        for rule in self.rules:
            if rule.family not in names:
                names.append(rule.family)
        if self.fallback not in names:
            names.append(self.fallback)
        return names

    def classify(self, intent: str | None) -> FamilyMatch:
        text = str(intent or "").strip()
        if text:
            for rule in self.rules:
                for keyword, pattern in rule.patterns:
                    if pattern.search(text):
                        return FamilyMatch(rule.family, rule.rule_id, keyword)
        return FamilyMatch(self.fallback, FALLBACK_RULE_ID, None)


_DEFAULT_RULE_SET: Final = RuleSet.from_config(
    IntentFamilyConfig(rules=list(DEFAULT_RULES)), source="default"
)


def default_rule_set() -> RuleSet:
    """The shipped rule set, compiled."""
    return _DEFAULT_RULE_SET


def _position(loc: tuple[int | str, ...]) -> str:
    """``rules[3].keywords``: a key path where list positions read as indexes."""
    text = ""
    for part in loc:
        if isinstance(part, int):
            text += f"[{part}]"
        else:
            text += f".{part}" if text else str(part)
    return text or "(block)"


def _describe(exc: ValidationError) -> str:
    # ``include_input=False`` keeps the offending value out of the message. An
    # unknown key is the other place a config line leaks: pydantic puts the key
    # itself in the error location, so the location stops at the mapping that
    # holds it, and the message gives the position and says a key is unrecognised.
    parts = []
    for err in exc.errors(include_input=False, include_url=False):
        loc = tuple(err["loc"])
        if err["type"] == "extra_forbidden":
            parts.append(f"{_position(loc[:-1])}: has a key that is not recognised")
        else:
            parts.append(f"{_position(loc)}: {err['msg']}")
    return "; ".join(parts)


#: Compiled blocks by their canonical JSON, so a REST route that loads the block
#: per request compiles it once. Bounded: a deployment has one block.
_COMPILED_CACHE: dict[str, RuleSet] = {}
_COMPILED_CACHE_SIZE: Final = 4


def _cache_key(raw: Mapping[str, Any]) -> str:
    return json.dumps(raw, sort_keys=True, default=repr)


def load_rule_set(retrieval_config: Mapping[str, Any] | None) -> RuleSet:
    """The effective rule set for a deployment's ``retrieval:`` config block.

    Absent block: the shipped defaults. Present block: it replaces them, or the
    call raises :class:`~trellis.errors.ConfigError`. There is no path on which
    a malformed block yields the defaults. A valid block is compiled once and
    the compiled set is reused for an identical block.
    """
    if not retrieval_config:
        return default_rule_set()
    if not isinstance(retrieval_config, Mapping):
        msg = "retrieval must be a mapping"
        raise ConfigError(msg, setting="retrieval")
    if CONFIG_KEY not in retrieval_config:
        return default_rule_set()
    raw = retrieval_config[CONFIG_KEY]
    if not isinstance(raw, Mapping) or not raw:
        msg = (
            f"{CONFIG_SETTING} is present but not a non-empty mapping with a 'rules' "
            "list; remove the block to use the shipped rules"
        )
        raise ConfigError(msg, setting=CONFIG_SETTING)
    key = _cache_key(raw)
    cached = _COMPILED_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        config = IntentFamilyConfig.model_validate(dict(raw))
    except ValidationError as exc:
        msg = f"{CONFIG_SETTING} is malformed: {_describe(exc)}"
        raise ConfigError(msg, setting=CONFIG_SETTING) from exc
    compiled = RuleSet.from_config(config, source="config")
    if len(_COMPILED_CACHE) >= _COMPILED_CACHE_SIZE:
        _COMPILED_CACHE.clear()
    _COMPILED_CACHE[key] = compiled
    return compiled


def classify_intent_family(
    intent: str | None, rule_set: RuleSet | None = None
) -> FamilyMatch:
    """The family of ``intent`` under ``rule_set`` (default: the shipped set).

    First matching rule wins; no match is the fallback family under the rule id
    :data:`FALLBACK_RULE_ID`.
    """
    return (rule_set or default_rule_set()).classify(intent)


# --- Report ----------------------------------------------------------------


def pack_bootstrap_interval(
    packs: Sequence[tuple[int, int]],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float] | None:
    """A 95% percentile bootstrap interval of the citation-weighted helpful rate.

    ``packs`` holds one ``(helpful, judged)`` pair per graded pack, where
    ``judged`` is the pack's helpful plus unhelpful citations. The unit that is
    resampled is the pack, with replacement, because the citations of one pack
    are not independent draws: the rate is recomputed as total helpful over
    total judged on each resample. A resample with no judged citation has no
    rate and is skipped. The generator is seeded here, never shared, so the
    interval depends on ``packs`` alone. ``None`` when no resample has a rate.
    """
    count = len(packs)
    if count == 0 or resamples <= 0:
        return None
    helpful = [pair[0] for pair in packs]
    judged = [pair[1] for pair in packs]
    rng = random.Random(seed)  # noqa: S311 - a reproducible resample, not security
    positions = range(count)
    rates: list[float] = []
    for _ in range(resamples):
        drawn = rng.choices(positions, k=count)
        total = sum(judged[i] for i in drawn)
        if total:
            rates.append(sum(helpful[i] for i in drawn) / total)
    if not rates:
        return None
    rates.sort()
    tail = _BOOTSTRAP_ALPHA / 2
    low = rates[int(tail * len(rates))]
    high = rates[min(len(rates) - 1, math.ceil((1 - tail) * len(rates)) - 1)]
    return low, high


class RuleCount(TrellisModel):
    rule_id: str
    packs: int


class FamilyRow(TrellisModel):
    """One new family's counts, and its rates unless they are withheld."""

    family: str
    packs: int = 0
    graded_packs: int = 0
    #: Distinct helpful / unhelpful citations of items the pack served, unioned
    #: per pack; an item cited both ways is helpful, as in ``analyze value``.
    helpful_citations: int = 0
    unhelpful_citations: int = 0
    #: Distinct cited ids the pack never served, which no rate counts.
    cited_not_served: int = 0
    #: Helpful over helpful plus unhelpful citations, weighted by citation.
    helpful_rate: float | None = None
    #: The bounds of :attr:`IntentFamilyReport.interval_method`, over packs.
    helpful_rate_low: float | None = None
    helpful_rate_high: float | None = None
    rated_packs: int = 0
    mean_rating: float | None = None
    #: Why the rates are ``None`` (empty when they are stated).
    withheld_reason: str = ""
    top_rules: list[RuleCount] = Field(default_factory=list)


class CrossTabCell(TrellisModel):
    legacy: str
    new: str
    packs: int


class RuleSetInfo(TrellisModel):
    source: str
    rules: int
    families: list[str]
    fallback: str


class IntentFamilyReport(TrellisModel):
    """The offline relabel report. Counts, families and rule ids only."""

    status: str = STATUS_OK
    #: Why the report holds no packs (empty when it does).
    empty_reason: str = ""
    window_days: int
    since: datetime
    until: datetime | None = None
    min_graded_packs: int = MIN_GRADED_PACKS
    #: How every family's rate interval was made (the resampled unit is the pack).
    interval_method: str = INTERVAL_METHOD
    bootstrap_resamples: int = BOOTSTRAP_RESAMPLES
    rule_set: RuleSetInfo
    #: Served packs in the window, after the holdout drop.
    packs_served: int = 0
    #: Served packs with item rows: the population every figure below uses.
    packs_analyzed: int = 0
    packs_without_item_rows: int = 0
    #: Of those, sectioned packs from before they emitted rows (#958), as
    #: ``analyze value`` counts them.
    sectioned_packs_excluded: int = 0
    packs_without_intent: int = 0
    #: Cited ids the cited pack never served, across every analysed pack.
    cited_not_served: int = 0
    fallback_packs: int = 0
    fallback_share: float | None = None
    #: Packs whose stored ``intent_family`` stamp differs from the legacy
    #: family recomputed here (a caller named one at serve time, or the
    #: normaliser changed since).
    legacy_stamp_differs: int = 0
    families: list[FamilyRow] = Field(default_factory=list)
    crosstab: list[CrossTabCell] = Field(default_factory=list)
    scan: ScanCoverage = Field(default_factory=ScanCoverage)
    notes: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class _Cell:
    packs: int = 0
    #: ``(helpful, helpful + unhelpful)`` of each graded pack: the bootstrap's unit.
    graded: list[tuple[int, int]] = field(default_factory=list)
    stray: int = 0
    ratings: list[float] = field(default_factory=list)
    rules: Counter[str] = field(default_factory=Counter)


def _pack_ratings(feedback_events: Sequence[Any], served: set[str]) -> dict[str, float]:
    """The mean rating per served pack that has one, from the typed feedback reader.

    A ``feedback_id`` counts once: the same feedback recorded twice (a replay) is
    one vote. Rows without an id (the CLI family) are each their own vote.
    """
    by_pack: dict[str, list[float]] = {}
    seen: set[str] = set()
    for row in iter_feedback_rows(feedback_events):
        if row.pack_id not in served or row.rating is None:
            continue
        if row.feedback_id:
            if row.feedback_id in seen:
                continue
            seen.add(row.feedback_id)
        by_pack.setdefault(row.pack_id, []).append(row.rating)
    return {pack_id: sum(vals) / len(vals) for pack_id, vals in by_pack.items()}


def _judged_citations(
    payload: Mapping[str, Any], helpful: set[str], unhelpful: set[str]
) -> tuple[int, int, int]:
    """``(helpful, unhelpful, cited_not_served)`` as ``analyze value`` joins them.

    A cited id counts only if the pack served it (the canonical reader,
    :func:`~trellis.feedback.attribution.served_item_ids`); an item cited both
    ways is helpful; an id the pack never served is counted apart.
    """
    served = served_item_ids(payload)
    helpful_served = helpful & served
    unhelpful_served = (unhelpful & served) - helpful_served
    stray = (helpful | unhelpful) - served
    return len(helpful_served), len(unhelpful_served), len(stray)


def _family_row(family: str, cell: _Cell) -> FamilyRow:
    helpful = sum(pair[0] for pair in cell.graded)
    judged = sum(pair[1] for pair in cell.graded)
    row = FamilyRow(
        family=family,
        packs=cell.packs,
        graded_packs=len(cell.graded),
        helpful_citations=helpful,
        unhelpful_citations=judged - helpful,
        cited_not_served=cell.stray,
        rated_packs=len(cell.ratings),
        top_rules=[
            RuleCount(rule_id=rule_id, packs=count)
            for rule_id, count in sorted(
                cell.rules.items(), key=lambda item: (-item[1], item[0])
            )[:TOP_RULES]
        ],
    )
    if len(cell.graded) < MIN_GRADED_PACKS:
        row.withheld_reason = (
            f"{len(cell.graded)} graded packs, below the floor of {MIN_GRADED_PACKS}"
        )
        return row
    interval = pack_bootstrap_interval(cell.graded) if judged else None
    if interval is not None:
        row.helpful_rate = round(helpful / judged, 4)
        row.helpful_rate_low = round(interval[0], 4)
        row.helpful_rate_high = round(interval[1], 4)
    else:
        row.withheld_reason = (
            "no helpful or unhelpful citation of a served item among the graded packs"
        )
    if cell.ratings:
        row.mean_rating = round(sum(cell.ratings) / len(cell.ratings), 4)
    return row


def _report_notes(
    *, cited_not_served: int, sectioned: int, coverage: ScanCoverage
) -> list[str]:
    notes = [
        (
            "Measurement only: serving and learning still key on "
            "normalize_intent_family, and nothing reads this report."
        ),
        (
            "A pack counts as graded when feedback naming it carries a rating or "
            "cites items; rates are over cited items the pack served, so an uncited "
            "item is not unhelpful."
        ),
        (
            "The rate interval resamples graded packs, not citations "
            f"({INTERVAL_METHOD}, {BOOTSTRAP_RESAMPLES} resamples, fixed seed); "
            "overlap or not, it is a hint to read the packs, not a separation."
        ),
        "Held-out packs and the feedback naming them are dropped before every count.",
    ]
    if cited_not_served:
        notes.append(
            f"{cited_not_served} cited id(s) were not served by the pack they were "
            "cited against and are counted in no rate."
        )
    if sectioned:
        notes.append(
            f"{sectioned} sectioned pack(s) carry no per-item rows and are not "
            "analysed."
        )
    if coverage.truncated:
        notes.append(coverage.note)
    return notes


def summarize_intent_families(
    event_log: EventLog,
    *,
    rule_set: RuleSet | None = None,
    days: int = 30,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = DEFAULT_SCAN_LIMIT,
) -> IntentFamilyReport:
    """Relabel the window's served packs under ``rule_set`` and report helpfulness.

    Read-only. The join drops held-out packs and the feedback naming them; the
    population is the served packs that carry item rows, as in ``analyze value``.
    The legacy family is :func:`~trellis.learning.scoring.normalize_intent_family`
    called on the pack's intent, not read from the pack's stamp.
    """
    rules = rule_set or default_rule_set()
    start, end = resolve_scan_window(days, since=since, until=until)
    feedback_events, pack_payloads, _raw_count, coverage = (
        join_pack_feedback_with_coverage(event_log, since=start, limit=limit, until=end)
    )
    flat = {
        pack_id: payload
        for pack_id, payload in pack_payloads.items()
        if has_served_rows(payload)
    }
    sectioned = sum(
        1
        for payload in pack_payloads.values()
        if is_sectioned_payload(payload) and not has_served_rows(payload)
    )
    verdicts = collect_pack_verdicts(feedback_events, flat)
    helpful_by_pack = verdicts["helpful"]
    unhelpful_by_pack = verdicts["unhelpful"]
    ratings = _pack_ratings(feedback_events, set(flat))

    cells: dict[str, _Cell] = {family: _Cell() for family in rules.families}
    cross: Counter[tuple[str, str]] = Counter()
    without_intent = fallback = stamp_differs = stray_total = 0
    for pack_id in sorted(flat):
        payload = flat[pack_id]
        raw_intent = payload.get("intent")
        intent = raw_intent if isinstance(raw_intent, str) else ""
        if not intent.strip():
            without_intent += 1
        match = rules.classify(intent)
        legacy = normalize_intent_family(intent=intent)
        stamped = payload.get("intent_family")
        if isinstance(stamped, str) and stamped != legacy:
            stamp_differs += 1
        fallback += match.is_fallback
        cross[(legacy, match.family)] += 1
        cell = cells[match.family]
        cell.packs += 1
        cell.rules[match.rule_id] += 1
        cited_helpful = helpful_by_pack.get(pack_id, set())
        cited_unhelpful = unhelpful_by_pack.get(pack_id, set())
        if cited_helpful or cited_unhelpful or pack_id in ratings:
            helpful, unhelpful, stray = _judged_citations(
                payload, cited_helpful, cited_unhelpful
            )
            cell.graded.append((helpful, helpful + unhelpful))
            cell.stray += stray
            stray_total += stray
            if pack_id in ratings:
                cell.ratings.append(ratings[pack_id])

    analyzed = len(flat)
    if not pack_payloads:
        empty_reason = "no served packs in the window"
    elif not flat:
        empty_reason = "no served pack in the window carries item rows"
    else:
        empty_reason = ""
    notes = _report_notes(
        cited_not_served=stray_total, sectioned=sectioned, coverage=coverage
    )
    return IntentFamilyReport(
        status=STATUS_TRUNCATED if coverage.truncated else STATUS_OK,
        empty_reason=empty_reason,
        window_days=days,
        since=start,
        until=end,
        rule_set=RuleSetInfo(
            source=rules.source,
            rules=len(rules.rules),
            families=rules.families,
            fallback=rules.fallback,
        ),
        packs_served=len(pack_payloads),
        packs_analyzed=analyzed,
        packs_without_item_rows=len(pack_payloads) - analyzed,
        sectioned_packs_excluded=sectioned,
        packs_without_intent=without_intent,
        cited_not_served=stray_total,
        fallback_packs=fallback,
        fallback_share=round(fallback / analyzed, 4) if analyzed else None,
        legacy_stamp_differs=stamp_differs,
        families=[_family_row(family, cell) for family, cell in cells.items()],
        crosstab=[
            CrossTabCell(legacy=legacy, new=new, packs=count)
            for (legacy, new), count in sorted(cross.items())
        ],
        scan=coverage,
        notes=notes,
    )


def parse_instant(value: str | None, *, option: str) -> datetime | None:
    """An ISO-8601 date or time as an aware UTC datetime; naive is read as UTC."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        message = f"{option} must be an ISO-8601 date or time, got {value!r}."
        raise IntentFamilyError(message) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def render_markdown(report: IntentFamilyReport) -> str:
    """The report as plain GFM. Families, rule ids and counts only; no intent text."""
    lines = [
        "---",
        "title: Intent family relabel report",
        "---",
        "",
        "# Intent family relabel report",
        "",
        (
            f"Status: {report.status}. Rule set: {report.rule_set.source} "
            f"({report.rule_set.rules} rules). Window: {report.window_days} days."
        ),
        "",
        (
            f"{report.packs_analyzed} of {report.packs_served} served packs carry "
            f"item rows; {report.fallback_packs} ({_pct(report.fallback_share)}) "
            f"fall to `{report.rule_set.fallback}`."
        ),
    ]
    if report.empty_reason:
        lines += ["", f"Empty: {report.empty_reason}."]
    lines += [
        "",
        "## Families",
        "",
        (
            "| family | packs | graded | helpful | unhelpful | not served | "
            "helpful rate | 95% CI (pack bootstrap) | mean rating | top rules |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report.families:
        if row.helpful_rate is None:
            rate, interval = "withheld", "-"
        else:
            rate = f"{row.helpful_rate:.1%}"
            interval = f"{row.helpful_rate_low:.1%} to {row.helpful_rate_high:.1%}"
        rules = ", ".join(f"{r.rule_id} ({r.packs})" for r in row.top_rules) or "-"
        lines.append(
            f"| {row.family} | {row.packs} | {row.graded_packs} | "
            f"{row.helpful_citations} | {row.unhelpful_citations} | "
            f"{row.cited_not_served} | {rate} | {interval} | "
            f"{'-' if row.mean_rating is None else f'{row.mean_rating:.2f}'} | "
            f"{rules} |"
        )
    withheld = [row for row in report.families if row.withheld_reason and row.packs]
    if withheld:
        lines += ["", "Rates withheld:", ""]
        lines += [f"- {row.family}: {row.withheld_reason}" for row in withheld]
    lines += [
        "",
        "## Legacy by new",
        "",
        "| legacy | new | packs |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {c.legacy} | {c.new} | {c.packs} |" for c in report.crosstab]
    lines += [""]
    lines += [f"- {note}" for note in report.notes]
    return "\n".join(lines) + "\n"


__all__ = [
    "BOOTSTRAP_RESAMPLES",
    "CONFIG_KEY",
    "CONFIG_SETTING",
    "DEFAULT_RULES",
    "FALLBACK_RULE_ID",
    "INTERVAL_METHOD",
    "MAX_KEYWORDS_PER_RULE",
    "MAX_RULES",
    "MIN_GRADED_PACKS",
    "STATUS_OK",
    "STATUS_TRUNCATED",
    "CrossTabCell",
    "FamilyMatch",
    "FamilyRow",
    "IntentFamilyConfig",
    "IntentFamilyError",
    "IntentFamilyReport",
    "IntentFamilyRule",
    "RuleCount",
    "RuleSet",
    "RuleSetInfo",
    "classify_intent_family",
    "default_rule_set",
    "load_rule_set",
    "parse_instant",
    "render_markdown",
    "pack_bootstrap_interval",
    "summarize_intent_families",
]
