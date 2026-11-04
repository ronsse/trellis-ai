"""A software-work intent family set and an offline report that relabels history.

**Measurement only.** The learning key is ``(intent_family, item_id)`` and the
live key comes from :func:`trellis.learning.scoring.normalize_intent_family`:
six data-platform keyword rows that fall back to ``general_context``. A
software intent such as "fix flaky test" or "review PR" lands in
``general_context``, or in a data family by accident because a substring
matched. Before the key changes, the open question is whether a software-work
family set would separate helpfulness at all. This module answers it offline,
over history, and nothing that serves or learns reads it. Changing the key is a
post-readout decision, so this module does not import the live normaliser's
callers and none of them import this module
(``tests/unit/learning/test_intent_families_isolation.py`` pins both
directions of that: the allowed importers are this module, its CLI, its REST
route and tests).

**The rule set.** Ordered, deterministic, first match wins. A rule is an id, a
family and a list of keywords; a keyword is a word or a phrase and matches on
word boundaries, case-insensitively (``latest`` does not match ``test``, and a
phrase tolerates any run of whitespace between its words). There is no
stemming: an inflection the family should catch is listed. The shipped default
(:data:`DEFAULT_RULES`) carries ``review``, ``bugfix``, ``refactor``, ``data``,
``investigate``, ``docs``, ``ops``, ``test`` and ``feature``, and an intent no
rule matches falls to the fallback family (``general``) under the reserved rule
id ``fallback``.

Order is the priority, and it is chosen by which word names the *work* rather
than its object: a review verb outranks everything, a fix verb outranks the
thing being fixed (so "fix flaky test" is ``bugfix``, not ``test``), and the
broad building verbs (``add``, ``implement``) come last so ``add tests`` is
``test`` and ``add a CI job`` is ``ops``. A long intent that names several
kinds of work lands in the earliest rule it touches; the report's rule ids
make that visible.

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
for the same reason; delete it to get the defaults.

**The report** (:func:`summarize_intent_families`) reads the served packs and
the feedback that grades them through the same join ``analyze value`` uses
(:func:`~trellis.learning.pack_observations.join_pack_feedback_with_coverage`,
which drops held-out packs and the feedback naming them) and the typed readers
in :mod:`trellis.feedback.pack_rows`. It reads no session outcome and splits
nothing by holdout arm. Per new family it states the packs, the graded packs,
the helpful and unhelpful citations, the helpful rate with a Wilson 95%
interval, the mean pack rating and the top matched rule ids, beside the share of
packs in the fallback and a legacy-by-new cross-tab. A family below
:data:`MIN_GRADED_PACKS` graded packs has its rates withheld with the reason
stated; its counts are still shown.

**It never prints an intent.** Intents are private corpus text and the report is
published. Every string in it is a family name, a rule id or a fixed sentence.
"""

from __future__ import annotations

import math
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

STATUS_OK: Final = "ok"
STATUS_TRUNCATED: Final = "truncated"

_Z_95: Final = 1.959963984540054
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
    keywords: list[str] = Field(min_length=1)

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
    rules: list[IntentFamilyRule] = Field(min_length=1)

    @field_validator("fallback")
    @classmethod
    def _fallback_shape(cls, value: str) -> str:
        if not _NAME.match(value):
            msg = _NAME_RULE
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _ids_unique(self) -> IntentFamilyConfig:
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                msg = f"rule id {rule.id!r} appears more than once"
                raise ValueError(msg)
            seen.add(rule.id)
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
        "workaround",
    ),
    _rule(
        "bugfix.defect",
        "bugfix",
        "bug",
        "bugs",
        "crash",
        "crashes",
        "crashing",
        "broken",
        "flaky",
        "failing",
        "fails",
    ),
    _rule(
        "refactor.core",
        "refactor",
        "refactor",
        "refactors",
        "refactoring",
        "restructure",
        "reorganize",
        "reorganise",
        "rename",
        "cleanup",
        "clean up",
        "simplify",
        "dedupe",
        "deduplicate",
        "consolidate",
        "decompose",
        "split into",
    ),
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
        "profiling",
        "warehouse",
        "lakehouse",
    ),
    _rule(
        "investigate.inquiry",
        "investigate",
        "investigate",
        "investigating",
        "investigation",
        "debug",
        "debugging",
        "diagnose",
        "diagnosing",
        "triage",
        "root cause",
        "why",
        "how does",
        "how do",
        "figure out",
        "find out",
        "look into",
    ),
    _rule(
        "investigate.analysis",
        "investigate",
        "analyze",
        "analyse",
        "analyzing",
        "analysis",
        "research",
        "audit",
        "survey",
        "explore",
        "measure",
        "measuring",
        "compare",
        "evaluate",
        "reproduce",
    ),
    _rule(
        "docs.core",
        "docs",
        "docs",
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
        "document the",
        "documenting",
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
        "redeployment",
        "rollout",
        "rollback",
        "release",
        "releases",
        "publish",
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
        "mutation",
        "e2e",
        "end-to-end",
        "fixture",
        "fixtures",
    ),
    _rule(
        "feature.build",
        "feature",
        "implement",
        "implements",
        "implementing",
        "implementation",
        "add",
        "adds",
        "adding",
        "build",
        "building",
        "create",
        "creating",
        "introduce",
        "feature",
        "features",
        "support",
        "wire",
        "wiring",
        "extend",
        "extending",
        "enable",
        "expose",
        "integrate",
        "migrate",
        "write",
        "writing",
        "new",
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


def _describe(exc: ValidationError) -> str:
    # ``include_input=False``: a config line can carry anything, and the error
    # names the key path and the problem, which is what an operator edits.
    parts = []
    for err in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in err["loc"]) or "(block)"
        parts.append(f"{location}: {err['msg']}")
    return "; ".join(parts)


def load_rule_set(retrieval_config: Mapping[str, Any] | None) -> RuleSet:
    """The effective rule set for a deployment's ``retrieval:`` config block.

    Absent block: the shipped defaults. Present block: it replaces them, or the
    call raises :class:`~trellis.errors.ConfigError`. There is no path on which
    a malformed block yields the defaults.
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
    try:
        config = IntentFamilyConfig.model_validate(dict(raw))
    except ValidationError as exc:
        msg = f"{CONFIG_SETTING} is malformed: {_describe(exc)}"
        raise ConfigError(msg, setting=CONFIG_SETTING) from exc
    return RuleSet.from_config(config, source="config")


def classify_intent_family(
    intent: str | None, rule_set: RuleSet | None = None
) -> FamilyMatch:
    """The family of ``intent`` under ``rule_set`` (default: the shipped set).

    First matching rule wins; no match is the fallback family under the rule id
    :data:`FALLBACK_RULE_ID`.
    """
    return (rule_set or default_rule_set()).classify(intent)


# --- Report ----------------------------------------------------------------


def wilson_interval(successes: int, n: int, z: float = _Z_95) -> tuple[float, float]:
    """The Wilson score interval for ``successes`` of ``n`` (``n`` must be positive)."""
    if n <= 0:
        msg = "a Wilson interval needs at least one trial"
        raise ValueError(msg)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


class RuleCount(TrellisModel):
    rule_id: str
    packs: int


class FamilyRow(TrellisModel):
    """One new family's counts, and its rates unless they are withheld."""

    family: str
    packs: int = 0
    graded_packs: int = 0
    #: Distinct helpful / unhelpful item citations, unioned per pack.
    helpful_citations: int = 0
    unhelpful_citations: int = 0
    helpful_rate: float | None = None
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
    rule_set: RuleSetInfo
    #: Served packs in the window, after the holdout drop.
    packs_served: int = 0
    #: Served packs with item rows: the population every figure below uses.
    packs_analyzed: int = 0
    packs_without_item_rows: int = 0
    packs_without_intent: int = 0
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
    graded: int = 0
    helpful: int = 0
    unhelpful: int = 0
    ratings: list[float] = field(default_factory=list)
    rules: Counter[str] = field(default_factory=Counter)


def _pack_ratings(feedback_events: Sequence[Any], served: set[str]) -> dict[str, float]:
    """The mean rating per served pack that has one, from the typed feedback reader."""
    by_pack: dict[str, list[float]] = {}
    for row in iter_feedback_rows(feedback_events):
        if row.pack_id in served and row.rating is not None:
            by_pack.setdefault(row.pack_id, []).append(row.rating)
    return {pack_id: sum(vals) / len(vals) for pack_id, vals in by_pack.items()}


def _family_row(family: str, cell: _Cell) -> FamilyRow:
    row = FamilyRow(
        family=family,
        packs=cell.packs,
        graded_packs=cell.graded,
        helpful_citations=cell.helpful,
        unhelpful_citations=cell.unhelpful,
        rated_packs=len(cell.ratings),
        top_rules=[
            RuleCount(rule_id=rule_id, packs=count)
            for rule_id, count in sorted(
                cell.rules.items(), key=lambda item: (-item[1], item[0])
            )[:TOP_RULES]
        ],
    )
    if cell.graded < MIN_GRADED_PACKS:
        row.withheld_reason = (
            f"{cell.graded} graded packs, below the floor of {MIN_GRADED_PACKS}"
        )
        return row
    citations = cell.helpful + cell.unhelpful
    if citations:
        low, high = wilson_interval(cell.helpful, citations)
        row.helpful_rate = round(cell.helpful / citations, 4)
        row.helpful_rate_low = round(low, 4)
        row.helpful_rate_high = round(high, 4)
    else:
        row.withheld_reason = "no helpful or unhelpful citations among the graded packs"
    if cell.ratings:
        row.mean_rating = round(sum(cell.ratings) / len(cell.ratings), 4)
    return row


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
    verdicts = collect_pack_verdicts(feedback_events, flat)
    helpful_by_pack = verdicts["helpful"]
    unhelpful_by_pack = verdicts["unhelpful"]
    ratings = _pack_ratings(feedback_events, set(flat))

    cells: dict[str, _Cell] = {family: _Cell() for family in rules.families}
    cross: Counter[tuple[str, str]] = Counter()
    without_intent = fallback = stamp_differs = 0
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
        helpful = helpful_by_pack.get(pack_id, set())
        unhelpful = unhelpful_by_pack.get(pack_id, set())
        if helpful or unhelpful or pack_id in ratings:
            cell.graded += 1
            cell.helpful += len(helpful)
            cell.unhelpful += len(unhelpful)
            if pack_id in ratings:
                cell.ratings.append(ratings[pack_id])

    analyzed = len(flat)
    if not pack_payloads:
        empty_reason = "no served packs in the window"
    elif not flat:
        empty_reason = "no served pack in the window carries item rows"
    else:
        empty_reason = ""
    notes = [
        (
            "Measurement only: serving and learning still key on "
            "normalize_intent_family, and nothing reads this report."
        ),
        (
            "A pack counts as graded when feedback naming it carries a rating or "
            "cites items; rates are over cited items, so an uncited item is not "
            "unhelpful."
        ),
        "Held-out packs and the feedback naming them are dropped before every count.",
    ]
    if coverage.truncated:
        notes.append(coverage.note)
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
        packs_without_intent=without_intent,
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
            "| family | packs | graded | helpful | unhelpful | helpful rate | "
            "95% CI | mean rating | top rules |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
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
            f"{row.helpful_citations} | {row.unhelpful_citations} | {rate} | "
            f"{interval} | "
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
    "CONFIG_KEY",
    "CONFIG_SETTING",
    "DEFAULT_RULES",
    "FALLBACK_RULE_ID",
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
    "summarize_intent_families",
    "wilson_interval",
]
