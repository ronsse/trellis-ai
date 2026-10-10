"""Intent-family learning scoring — analyzes retrieval observations and produces
promotion candidates for the Trellis precedent store.

Provides five public entry points:

* :func:`normalize_intent_family` — maps a phase or free-text intent string to a
  canonical family label.
* :func:`analyze_learning_observations` — aggregates raw retrieval observations
  into scored promotion candidates.
* :func:`write_learning_review_artifacts` — writes the candidate report and a
  blank decisions template to disk for human review.
* :func:`prepare_learning_promotions` — joins approved decisions back to
  candidates and produces entity + edge payloads.
* :func:`build_learning_promotion_payloads` — builds the entity/edge payload for
  a single approved candidate.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trellis.learning.artifacts import LEARNING_CANDIDATES_FILENAME
from trellis.learning.evidence_gate import (
    NOT_SCREENED,
    CitationEvidence,
    screen_noise_candidates,
)
from trellis.schemas.parameters import ParameterScope

if TYPE_CHECKING:
    from trellis.mutate import MutationExecutor
    from trellis.ops.registry import ParameterRegistry
    from trellis.stores.base.document import DocumentStore

_LEARNING_ARTIFACT_VERSION = "1.0"
_DEFAULT_MIN_SUPPORT = 2

# Component id used when resolving registry overrides for promotion/noise
# thresholds. Exported so the CLI seed path and test fixtures share one
# source of truth.
LEARNING_SCORING_COMPONENT = "learning.scoring"

# Registry parameter keys. Operators tune these per ``(component, domain,
# intent_family)`` cell via ``ParameterStore``. Per the POC directive
# (plan-self-improvement-program §2) the scoring module carries no
# hard-coded defaults — callers must supply a registry whose snapshot
# resolves every key below or :func:`analyze_learning_observations` raises
# ``KeyError``. Seed defaults live in the CLI module (see
# ``trellis_cli.analyze``) so a fresh install can run without a config.
LEARNING_PROMOTE_SUCCESS_KEY = "promote_success_threshold"
LEARNING_PROMOTE_RETRY_KEY = "promote_retry_threshold"
LEARNING_NOISE_SUCCESS_KEY = "noise_success_threshold"
LEARNING_NOISE_RETRY_KEY = "noise_retry_threshold"

REQUIRED_LEARNING_PARAMETER_KEYS: tuple[str, ...] = (
    LEARNING_PROMOTE_SUCCESS_KEY,
    LEARNING_PROMOTE_RETRY_KEY,
    LEARNING_NOISE_SUCCESS_KEY,
    LEARNING_NOISE_RETRY_KEY,
)

# --- Readable fallback names (#e159) ---------------------------------------
#
# A candidate whose source item is a plain ``save_memory`` document has no
# ``title``/``capture_title``/``name`` (see ``_item_attribution`` in
# ``trellis.retrieve.pack_builder`` for that resolution order), so
# ``precedent_name`` fell back to the bare ULID ``item_id`` — unreadable in
# a review queue. When the item can be resolved via a ``DocumentStore``,
# its first non-empty line (frontmatter and a leading markdown heading
# marker stripped) stands in for the title. Kept independent of the
# dedup/ingest frontmatter regexes (same shape, different module) per this
# repo's convention of not sharing that pattern across packages.
_FALLBACK_NAME_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n.*?\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
_FALLBACK_NAME_HEADING_RE = re.compile(r"\A#{1,6}[ \t]+")
_FALLBACK_NAME_MAX_CHARS = 96

# --- Promotable digest (#e159) ----------------------------------------------
#
# A ``promote_guidance`` candidate is "promotable" for the digest only once
# at least one grader has cited it helpful and none has cited it unhelpful —
# the single threshold named here, so a reviewer (or a future tuning pass)
# finds it in one place rather than re-deriving it from the filter.
LEARNING_PROMOTABLE_MIN_HELPFUL_COUNT = 1
#: Cap on ``promotable.top`` — a digest, not a second candidates listing.
_PROMOTABLE_DIGEST_TOP_N = 5

_INTENT_FAMILY_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("analyze", "profile", "explore"), "source_analysis"),
    (("discover", "schema", "source"), "source_discovery"),
    (("plan", "design", "naming", "lineage"), "pipeline_planning"),
    (("generate", "sql", "pyspark", "code"), "asset_generation"),
    (("validate", "quality", "pii", "convention", "test"), "validation_diagnostics"),
    (("eda", "profile", "drift", "anomaly"), "eda_investigation"),
)


def normalize_intent_family(
    *,
    phase: str | None = None,
    intent: str | None = None,
    phase_family_map: Mapping[str, str] | None = None,
) -> str:
    normalized_phase = str(phase or "").strip()
    if normalized_phase and phase_family_map:
        family = phase_family_map.get(normalized_phase)
        if family:
            return str(family).strip()

    normalized_intent = str(intent or "").strip().casefold()
    for keywords, family in _INTENT_FAMILY_KEYWORDS:
        if any(keyword in normalized_intent for keyword in keywords):
            return family
    return "general_context"


def _accumulate_item(
    candidate_map: dict[tuple[str, str], dict[str, Any]],
    observation: Mapping[str, Any],
    item: Mapping[str, Any],
    intent_family: str,
) -> None:
    item_id = str(item.get("item_id", "")).strip()
    if not item_id:
        return
    key = (intent_family, item_id)
    metrics = candidate_map.setdefault(
        key,
        {
            "intent_family": intent_family,
            "item_id": item_id,
            "item_type": item.get("item_type"),
            "title": item.get("title"),
            "category": item.get("category"),
            "domain_systems": set(),
            "pack_domains": set(),
            "phases": set(),
            "target_entity_ids": set(),
            "supporting_run_ids": set(),
            "evidence_refs": [],
            "times_served": 0,
            "success_count": 0,
            "retry_count": 0,
            "retry_observed_count": 0,
            "injected_count": 0,
            "injected_observed_count": 0,
            "selection_efficiency_total": 0.0,
            "selection_efficiency_count": 0,
            "helpful_citations": 0,
            "unhelpful_citations": 0,
            "source_strategies": {},
        },
    )
    metrics["times_served"] += 1
    metrics["supporting_run_ids"].add(
        str(observation.get("run_id", "")).strip() or "unknown-run"
    )
    metrics["phases"].add(str(observation.get("phase", "")).strip())
    metrics["target_entity_ids"].update(
        str(eid).strip()
        for eid in observation.get("seed_entity_ids", [])
        if str(eid).strip()
    )
    # ``domain_systems`` mixes two vocabularies on purpose (it is a
    # reviewer-facing provenance list): the operator domain the pack was
    # served for, plus each item's ``source_system`` (dbt, snowflake, …).
    # ``pack_domains`` keeps the operator half alone because that is what
    # the promotion's ``domain`` stamp — and therefore
    # ``get_lessons(domain=...)`` — must filter on.
    pack_domain = str(observation.get("domain") or "").strip()
    item_domain_system = str(item.get("domain_system") or "").strip()
    if pack_domain:
        metrics["pack_domains"].add(pack_domain)
    metrics["domain_systems"].update(
        entry for entry in (pack_domain, item_domain_system) if entry
    )
    metrics["evidence_refs"].extend(
        str(ref).strip()
        for ref in observation.get("evidence_refs", [])
        if str(ref).strip()
    )
    if str(observation.get("outcome", "")).strip() == "success":
        metrics["success_count"] += 1
    # ``had_retry`` and ``injected`` keep a *coverage* denominator beside
    # the numerator, the way ``selection_efficiency`` always has. Without
    # one, ``observation.get(...)`` returns ``None`` for a field no
    # producer writes and ``bool()`` renders it ``False`` — so "no agent
    # reported a retry" and "nothing can report a retry" both arrive as
    # ``retry_count == 0`` and leave as ``retry_rate == 0.0``. That is the
    # conflation, not the missing field: a rate of 0.0 is a measurement,
    # and this one was manufactured. Absence is reported as absence (the
    # ``capture_coverage`` idiom) so a reviewer can tell a clean record
    # from an unwired one.
    if "had_retry" in observation:
        metrics["retry_observed_count"] += 1
        if bool(observation["had_retry"]):
            metrics["retry_count"] += 1
    if "injected" in observation:
        metrics["injected_observed_count"] += 1
        if bool(observation["injected"]):
            metrics["injected_count"] += 1

    # Per-item verdicts, stamped at the join (``pack_observations``).
    # These are counted per *serving*, not per distinct item: a memory the
    # grader called unhelpful in two separate packs has two unhelpful
    # citations, which is what makes ``unhelpful >= 2`` a statement about
    # repeated judgement rather than about one bad pack.
    if bool(item.get("cited_helpful")):
        metrics["helpful_citations"] += 1
    if bool(item.get("cited_unhelpful")):
        metrics["unhelpful_citations"] += 1

    sel_eff = observation.get("selection_efficiency")
    if isinstance(sel_eff, float | int):
        metrics["selection_efficiency_total"] += float(sel_eff)
        metrics["selection_efficiency_count"] += 1

    source_strategy = str(item.get("source_strategy", "")).strip()
    if source_strategy:
        metrics["source_strategies"][source_strategy] = (
            metrics["source_strategies"].get(source_strategy, 0) + 1
        )


def _build_promotable_digest(
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Summarize ``promote_guidance`` candidates with net-positive citations.

    Surfacing only (#e159): this counts and ranks waiting candidates so a
    reviewer knows to look — it never promotes anything itself. Eligibility
    is ``recommendation_type == "promote_guidance"`` (the narrower literal,
    not :data:`PROMOTE_RECOMMENDATIONS`, which also admits
    ``"promote_precedent"``) plus the one named threshold,
    :data:`LEARNING_PROMOTABLE_MIN_HELPFUL_COUNT`, and zero unhelpful
    citations. ``top`` is capped at :data:`_PROMOTABLE_DIGEST_TOP_N`,
    ordered by ``helpful_count`` desc, then ``success_rate`` desc, then
    ``times_served`` desc.
    """
    eligible: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.get("recommendation_type") != "promote_guidance":
            continue
        citations = candidate.get("citation_evidence")
        if not isinstance(citations, Mapping):
            continue
        helpful_count = int(citations.get("helpful_count", 0) or 0)
        unhelpful_count = int(citations.get("unhelpful_count", 0) or 0)
        if (
            helpful_count < LEARNING_PROMOTABLE_MIN_HELPFUL_COUNT
            or unhelpful_count != 0
        ):
            continue
        metrics = candidate.get("metrics")
        metrics = metrics if isinstance(metrics, Mapping) else {}
        eligible.append(
            {
                "candidate_id": candidate.get("candidate_id"),
                "precedent_name": candidate.get("precedent_name"),
                "helpful_count": helpful_count,
                "times_served": int(metrics.get("times_served", 0) or 0),
                "success_rate": float(metrics.get("success_rate", 0.0) or 0.0),
            }
        )

    eligible.sort(
        key=lambda row: (
            -row["helpful_count"],
            -row["success_rate"],
            -row["times_served"],
        )
    )
    return {
        "count": len(eligible),
        "top": eligible[:_PROMOTABLE_DIGEST_TOP_N],
    }


def analyze_learning_observations(
    *,
    observations: Sequence[Mapping[str, Any]],
    registry: ParameterRegistry,
    min_support: int = _DEFAULT_MIN_SUPPORT,
    artifacts_root: str | Path | None = None,
) -> dict[str, Any]:
    candidate_map: dict[tuple[str, str], dict[str, Any]] = {}
    # Window-level coverage for the evidence screen: observations whose
    # grader named any item id at all. Counted over every observation,
    # including ones contributing no candidate, because it describes the
    # grading surface rather than the candidate set.
    attributed_observations = 0

    for observation in observations:
        if bool(observation.get("citation_attributed")):
            attributed_observations += 1
        intent_family = (
            str(observation.get("intent_family", "")).strip() or "general_context"
        )
        items = observation.get("items", [])
        if not isinstance(items, Sequence) or isinstance(items, str | bytes):
            items = []
        for item in items:
            if isinstance(item, Mapping):
                _accumulate_item(candidate_map, observation, item, intent_family)

    candidates: list[dict[str, Any]] = []
    for (intent_family, item_id), metrics in sorted(candidate_map.items()):
        if metrics["times_served"] < max(1, int(min_support)):
            continue

        times_served = int(metrics["times_served"])
        success_rate = metrics["success_count"] / times_served if times_served else 0.0
        # Divided by the observations that *reported* the field, not by
        # every serving — an unreported serving is not a serving without a
        # retry. ``None`` when nothing reported it at all.
        retry_observed = int(metrics["retry_observed_count"])
        retry_rate = metrics["retry_count"] / retry_observed if retry_observed else None
        injected_observed = int(metrics["injected_observed_count"])
        injection_rate = (
            metrics["injected_count"] / injected_observed if injected_observed else None
        )
        avg_selection_efficiency = (
            metrics["selection_efficiency_total"]
            / metrics["selection_efficiency_count"]
            if metrics["selection_efficiency_count"]
            else None
        )

        recommendation_type = _recommend_learning_action(
            item_type=str(metrics.get("item_type", "")).strip(),
            success_rate=success_rate,
            retry_rate=retry_rate,
            registry=registry,
        )
        if recommendation_type is None:
            continue

        title = str(metrics.get("title") or item_id).strip()
        candidate = {
            "candidate_id": _candidate_id(intent_family=intent_family, item_id=item_id),
            "intent_family": intent_family,
            "recommendation_type": recommendation_type,
            "item_id": item_id,
            "item_type": metrics.get("item_type"),
            "title": metrics.get("title"),
            "category": metrics.get("category"),
            "domain_systems": sorted(metrics["domain_systems"]),
            # Authoritative for the promotion's ``domain`` — see
            # ``_accumulate_item``. ``None`` when every observation was
            # domain-less.
            "primary_domain": next(iter(sorted(metrics["pack_domains"])), None),
            "phases": sorted(
                phase for phase in metrics["phases"] if str(phase).strip()
            ),
            "target_entity_ids": sorted(metrics["target_entity_ids"]),
            "supporting_run_ids": sorted(metrics["supporting_run_ids"]),
            "source_strategies": dict(sorted(metrics["source_strategies"].items())),
            "metrics": {
                "times_served": times_served,
                "success_rate": round(success_rate, 4),
                "retry_rate": None if retry_rate is None else round(retry_rate, 4),
                "injection_rate": (
                    None if injection_rate is None else round(injection_rate, 4)
                ),
                "avg_selection_efficiency": (
                    None
                    if avg_selection_efficiency is None
                    else round(avg_selection_efficiency, 4)
                ),
            },
            # How much of the corpus could answer each metric at all.
            # ``times_served`` is the denominator every rate would have
            # used before; the per-metric counts say which of them were
            # actually divisible. A reviewer reading
            # ``"retry_rate": null`` needs to know whether that is a
            # sparse signal or an unwired one.
            "metrics_coverage": {
                "observations": times_served,
                "retry_observed": retry_observed,
                "injected_observed": injected_observed,
                "selection_efficiency_observed": int(
                    metrics["selection_efficiency_count"]
                ),
            },
            # What graders actually said about this item, per serving.
            # Attached to *both* arms: the noise arm is screened on it
            # (``evidence_gate``), the promote arm is not — but a reviewer
            # approving a promotion should be able to see that nobody ever
            # cited the item helpful, which on this corpus is the usual
            # case. See ``evidence_gate`` for why that is evidence for a
            # human and not a rule.
            "citation_evidence": {
                "appearances": times_served,
                "helpful_count": int(metrics["helpful_citations"]),
                "unhelpful_count": int(metrics["unhelpful_citations"]),
            },
            "evidence_refs": sorted(set(metrics["evidence_refs"]))[:10],
            "precedent_name": f"Learning: {intent_family} :: {title[:96]}".strip(),
            "precedent_properties": {
                "category": _candidate_category(recommendation_type),
                "intent_family": intent_family,
                "source_item_id": item_id,
                "source_item_type": metrics.get("item_type"),
                "success_rate": round(success_rate, 4),
                "retry_rate": None if retry_rate is None else round(retry_rate, 4),
                "support_count": times_served,
                # Carried onto the durable node, for the same reason
                # ``retry_rate`` stopped being printed when unmeasured: a
                # promoted precedent is served back as context, so it
                # should state the citation evidence behind it rather than
                # let ``success_rate`` imply one.
                "helpful_citations": int(metrics["helpful_citations"]),
                "unhelpful_citations": int(metrics["unhelpful_citations"]),
                "source_of_truth": "reviewed_promotion",
            },
        }
        candidates.append(candidate)

    # The proposal and the verdict are reported *separately*, per #336:
    # a screen that admits a fraction of the proposals is a fact about the
    # proposal rule, and collapsing the two would hide it. Every proposed
    # candidate stays in ``candidates`` carrying its own verdict; nothing
    # is dropped here.
    noise_candidates = [
        candidate
        for candidate in candidates
        if candidate["recommendation_type"] == "investigate_noise"
    ]
    screen = screen_noise_candidates(
        (candidate["candidate_id"] for candidate in noise_candidates),
        (CitationEvidence.from_candidate(c) for c in noise_candidates),
        attributed_observations=attributed_observations,
    )
    verdict_by_id = {decision.candidate_id: decision for decision in screen.decisions}
    for candidate in candidates:
        decision = verdict_by_id.get(candidate["candidate_id"])
        if decision is None:
            # The promote arm. ``NOT_SCREENED`` is distinct from every
            # refusal slug: no gate ran, rather than one running and
            # declining. The mirror-image promote gate was built, measured
            # at chance and refused — see ``evidence_gate``.
            candidate["evidence_verdict"] = NOT_SCREENED
            continue
        candidate["evidence_verdict"] = (
            "admitted" if decision.admitted else decision.reason
        )

    return {
        "artifact_version": _LEARNING_ARTIFACT_VERSION,
        "generated_at_utc": _utc_now(),
        "artifacts_root": None if artifacts_root is None else str(artifacts_root),
        "min_support": int(min_support),
        "observation_count": len(observations),
        "attributed_observation_count": attributed_observations,
        "candidate_count": len(candidates),
        "noise_screen": {
            "proposed": screen.candidates_considered,
            "admitted": len(screen.admitted),
            "admitted_candidate_ids": list(screen.admitted),
            "refused_by_reason": dict(screen.refused_by_reason),
            "attributed_observations": screen.attributed_observations,
            "min_attributed_observations": screen.min_attributed_observations,
            "min_unhelpful_citations": screen.min_unhelpful_citations,
            "suppressed": screen.suppressed,
            "suppressed_reason": screen.suppressed_reason,
        },
        "candidates": candidates,
        # ``promotable`` is deliberately NOT computed here: its
        # ``precedent_name`` entries must reflect the readable-name
        # fallback (#e159), which only runs once a ``DocumentStore`` is
        # reachable — at artifact-write time, never in this pure,
        # store-free aggregator. See ``write_learning_review_artifacts``,
        # which computes and attaches it after applying that fallback.
    }


def _derive_readable_name(content: str) -> str | None:
    """First non-empty line of ``content``, ready to stand in for a title.

    Strips a leading YAML frontmatter block (the same shape as the
    ingest/dedup frontmatter regexes elsewhere in this codebase, kept as
    an independent copy per this repo's convention for that pattern), then
    returns the first non-empty line with any leading markdown heading
    marker (``#`` through ``######``) removed and the result truncated
    with :func:`trellis.retrieve.excerpts.truncate_excerpt`.

    Returns ``None`` when the content has no non-empty line at all (empty
    or whitespace-only body) — the caller keeps the bare item id in that
    case, the same as when the item can't be resolved at all.
    """
    # Deferred: ``trellis.retrieve`` imports ``pack_builder``, which imports
    # ``normalize_intent_family`` back from this module at module level, so
    # a top-level import here would be circular. By the time this function
    # runs, this module has already finished importing.
    from trellis.retrieve.excerpts import truncate_excerpt  # noqa: PLC0415

    stripped = _FALLBACK_NAME_FRONTMATTER_RE.sub("", content, count=1)
    for raw_line in stripped.splitlines():
        candidate_line = raw_line.strip()
        if not candidate_line:
            continue
        candidate_line = _FALLBACK_NAME_HEADING_RE.sub(
            "", candidate_line, count=1
        ).strip()
        if not candidate_line:
            continue
        return truncate_excerpt(candidate_line, limit=_FALLBACK_NAME_MAX_CHARS)
    return None


def _resolve_fallback_candidate_name(
    item_id: str, document_store: DocumentStore | None
) -> str | None:
    """Readable fallback name for ``item_id``, or ``None`` if unresolvable.

    ``None`` covers every case the caller treats identically: no store was
    given, the store has no document for ``item_id``, the document has no
    content, or the content has no derivable non-empty line.
    ``DocumentStore.get`` returns ``None`` cleanly on no match (no
    exception handling needed around it — see the sqlite backend).
    """
    if document_store is None:
        return None
    document = document_store.get(item_id)
    if not document:
        return None
    content = str(document.get("content") or "")
    if not content:
        return None
    return _derive_readable_name(content)


def _apply_readable_name_fallback(
    candidate: dict[str, Any], document_store: DocumentStore | None
) -> None:
    """Rebuild a title-less candidate's ``precedent_name`` from its item, in place.

    No-op when the candidate already has a non-empty ``title`` (it already
    produced a readable ``precedent_name`` in
    :func:`analyze_learning_observations`) or when no readable fallback name
    could be resolved — the bare-id name survives untouched in both cases,
    per the brief: "don't change names of candidates that already have a
    title" and "keep the bare id only when the item can't be resolved."
    """
    if str(candidate.get("title") or "").strip():
        return
    item_id = str(candidate.get("item_id", "")).strip()
    if not item_id:
        return
    fallback_name = _resolve_fallback_candidate_name(item_id, document_store)
    if not fallback_name:
        return
    intent_family = str(candidate.get("intent_family", "")).strip()
    candidate["precedent_name"] = (
        f"Learning: {intent_family} :: {fallback_name}".strip()
    )


def write_learning_review_artifacts(
    *,
    report: dict[str, Any],
    output_dir: str | Path,
    document_store: DocumentStore | None = None,
) -> dict[str, str]:
    """Write the candidates report and a blank decisions template to disk.

    Mutates ``report`` in place on two counts before serializing it, so a
    caller that goes on to print or re-read the same ``report`` object
    (both CLI surfaces do, for ``--format json``) sees exactly what landed
    on disk rather than a stale pre-write copy:

    * **Readable names (#e159).** Any candidate with no ``title`` keeps a
      bare-``item_id`` ``precedent_name`` from :func:`analyze_learning_observations`
      (which is store-free by design). When ``document_store`` is given,
      each such candidate's item is looked up and, if resolved, its
      ``precedent_name`` is rebuilt from a readable fallback name — see
      :func:`_apply_readable_name_fallback`. A candidate that already has
      a ``title``, or whose item can't be resolved, is untouched; the
      promoted-name override (``promotion_name``, built below and in
      :func:`build_learning_promotion_payloads`) still wins over whatever
      ``precedent_name`` ends up being.
    * **Promotable digest (#e159).** ``report["promotable"]`` is (re)computed
      from the candidates *after* the fallback above, so its ``top`` entries
      carry the same readable names — a digest built from the pure
      aggregator's output would otherwise echo the bare ids this fallback
      exists to fix. See :func:`_build_promotable_digest`.
    """
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    candidates = report.get("candidates", [])
    if isinstance(candidates, Sequence) and not isinstance(candidates, str | bytes):
        for candidate in candidates:
            if isinstance(candidate, dict):
                _apply_readable_name_fallback(candidate, document_store)
    else:
        candidates = []
    report["promotable"] = _build_promotable_digest(candidates)

    candidates_path = target_dir / LEARNING_CANDIDATES_FILENAME
    candidates_path.write_text(
        json.dumps(dict(report), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    decisions_template = {
        "artifact_version": _LEARNING_ARTIFACT_VERSION,
        "generated_from": str(candidates_path),
        "decisions": [
            {
                "candidate_id": candidate["candidate_id"],
                "approved": False,
                "promotion_name": candidate.get("precedent_name", ""),
                "rationale": "",
            }
            for candidate in report.get("candidates", [])
            if isinstance(candidate, Mapping)
        ],
    }
    decisions_path = target_dir / "promotion_decisions.template.json"
    decisions_path.write_text(
        json.dumps(decisions_template, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "candidates_path": str(candidates_path),
        "decisions_template_path": str(decisions_path),
    }


def prepare_learning_promotions(
    *,
    candidates_payload: Mapping[str, Any],
    decisions_payload: Mapping[str, Any],
) -> dict[str, Any]:
    candidate_lookup = {
        str(candidate.get("candidate_id", "")).strip(): candidate
        for candidate in candidates_payload.get("candidates", [])
        if isinstance(candidate, Mapping)
        and str(candidate.get("candidate_id", "")).strip()
    }
    decisions = decisions_payload.get("decisions", [])
    if not isinstance(decisions, Sequence) or isinstance(decisions, str | bytes):
        decisions = []

    results: list[dict[str, Any]] = []
    for decision in decisions:
        if not isinstance(decision, Mapping):
            continue
        candidate_id = str(decision.get("candidate_id", "")).strip()
        if not candidate_id or not bool(decision.get("approved")):
            continue

        candidate = candidate_lookup.get(candidate_id)
        if candidate is None:
            results.append(
                {"candidate_id": candidate_id, "status": "missing_candidate"}
            )
            continue

        recommendation_type = str(candidate.get("recommendation_type", "")).strip()
        if recommendation_type not in PROMOTE_RECOMMENDATIONS:
            results.append(
                {
                    "candidate_id": candidate_id,
                    "status": "skipped_non_promotable",
                    "recommendation_type": recommendation_type,
                }
            )
            continue

        promotion = build_learning_promotion_payloads(
            candidate=candidate,
            promotion_name=str(decision.get("promotion_name", "")).strip(),
            rationale=str(decision.get("rationale", "")).strip(),
        )
        results.append(
            {
                "candidate_id": candidate_id,
                "status": "ready",
                **promotion,
            }
        )

    return {
        "approved_count": sum(
            1
            for decision in decisions
            if isinstance(decision, Mapping) and bool(decision.get("approved"))
        ),
        "results": results,
    }


def build_learning_promotion_payloads(
    *,
    candidate: Mapping[str, Any],
    promotion_name: str,
    rationale: str,
) -> dict[str, Any]:
    candidate_id = str(candidate.get("candidate_id", "")).strip()
    entity_id = f"precedent://learning/{_slugify(candidate_id)}"
    entity_name = (
        promotion_name or str(candidate.get("precedent_name", "")).strip() or entity_id
    )
    target_entity_ids = [
        str(target_id).strip()
        for target_id in candidate.get("target_entity_ids", [])
        if str(target_id).strip()
    ]
    domain_systems = [
        str(domain).strip()
        for domain in candidate.get("domain_systems", [])
        if str(domain).strip()
    ]
    primary_domain = str(candidate.get("primary_domain") or "").strip()
    entity_payload = {
        "entity_type": "precedent",
        "entity_id": entity_id,
        "name": entity_name,
        "properties": {
            **dict(candidate.get("precedent_properties", {})),
            "description": _build_precedent_description(candidate, rationale=rationale),
            "approved_rationale": rationale or None,
            "approved_at": _utc_now(),
            "supporting_run_ids": list(candidate.get("supporting_run_ids", [])),
            "source_phases": list(candidate.get("phases", [])),
            "target_entity_ids": target_entity_ids,
            # Provenance list (pack domain + per-item source systems),
            # kept for reviewers.
            "domain_systems": domain_systems,
            # The single domain submit_learning_promotion stamps on the
            # promotion event, which is what get_lessons(domain=...) and
            # list_precedents(domain=...) filter on.
            "primary_domain": primary_domain or None,
        },
    }
    edge_payloads = [
        {
            "source_id": entity_id,
            "target_id": target_id,
            "edge_kind": "precedent_applies_to",
            "properties": {
                "source_of_truth": "reviewed_promotion",
                "intent_family": candidate.get("intent_family"),
                "candidate_id": candidate_id,
            },
        }
        for target_id in target_entity_ids
    ]
    return {
        "entity_id": entity_id,
        "entity_payload": entity_payload,
        "edge_payloads": edge_payloads,
        "linked_entity_ids": target_entity_ids,
    }


def submit_learning_promotion(
    executor: MutationExecutor,
    entity_payload: Mapping[str, Any],
    edge_payloads: Sequence[Mapping[str, Any]],
    *,
    requested_by: str,
) -> dict[str, Any]:
    """Submit one approved promotion through the governed pipeline.

    Single write path shared by every surface that executes a reviewed
    learning promotion (CLI ``curate promote-learning``, API review
    queue) — an ``ENTITY_CREATE`` followed by per-target
    ``LINK_CREATE``; a failed entity short-circuits the edges.
    ``requested_by`` is the only thing that varies per surface.

    ``entity_payload`` / ``edge_payloads`` come from
    :func:`build_learning_promotion_payloads`, which always sets
    ``entity_id`` and a non-empty ``properties`` dict on both — this
    function trusts that contract rather than re-guarding.

    After the entity + edges land, a governed ``PRECEDENT_PROMOTE`` is
    submitted so the promotion emits a ``PRECEDENT_PROMOTED`` event and
    becomes visible to ``get_lessons`` / ``list_precedents``. Without it
    the precedent entity exists in the graph but the lessons read-path —
    which only queries ``PRECEDENT_PROMOTED`` events — never surfaces it.
    """
    from trellis.mutate import Command, CommandStatus, Operation  # noqa: PLC0415

    entity_cmd = Command(
        operation=Operation.ENTITY_CREATE,
        args={
            "entity_type": entity_payload["entity_type"],
            "entity_id": entity_payload["entity_id"],
            "name": entity_payload["name"],
            "properties": dict(entity_payload["properties"]),
        },
        target_type="entity",
        requested_by=requested_by,
    )
    entity_result = executor.execute(entity_cmd)
    if entity_result.status != CommandStatus.SUCCESS:
        return {
            "status": "entity_failed",
            "entity_status": entity_result.status.value,
            "message": entity_result.message,
        }

    edge_outcomes = []
    for edge in edge_payloads:
        edge_cmd = Command(
            operation=Operation.LINK_CREATE,
            args={
                "source_id": edge["source_id"],
                "target_id": edge["target_id"],
                "edge_kind": edge["edge_kind"],
                "properties": dict(edge["properties"]),
            },
            target_id=edge["source_id"],
            target_type="entity",
            requested_by=requested_by,
        )
        edge_result = executor.execute(edge_cmd)
        edge_outcomes.append(
            {
                "edge_kind": edge["edge_kind"],
                "target_id": edge["target_id"],
                "status": edge_result.status.value,
            }
        )

    entity_props = dict(entity_payload["properties"])
    # The domain the pack was served for wins. ``domain_systems`` also
    # collects per-item ``source_system`` values, and those sort ahead of
    # real domains often enough to file a lesson under "dbt" — invisible to
    # the ``get_lessons(domain="platform")`` an operator would actually run.
    # Falling back to it keeps candidates produced before ``primary_domain``
    # existed (on-disk review artifacts) stamped as they were.
    domain_systems = entity_props.get("domain_systems") or []
    primary_domain = str(entity_props.get("primary_domain") or "").strip()
    if not primary_domain and domain_systems:
        primary_domain = str(domain_systems[0]).strip()
    promote_cmd = Command(
        operation=Operation.PRECEDENT_PROMOTE,
        args={
            "title": entity_payload["name"],
            "description": str(entity_props.get("description", "")),
            "domain": primary_domain or None,
            "entity_type": "precedent",
            "source_item_id": entity_props.get("source_item_id"),
        },
        target_id=entity_payload["entity_id"],
        target_type="entity",
        requested_by=requested_by,
    )
    promote_result = executor.execute(promote_cmd)
    return {
        "status": "promoted",
        "node_id": entity_result.created_id,
        "edges": edge_outcomes,
        # Surfaces whether the precedent actually reached the lessons
        # read-path. A non-SUCCESS here means the entity exists but
        # get_lessons won't show it — an operator-visible half-state.
        "precedent_event_status": promote_result.status.value,
        "precedent_event_id": promote_result.created_id,
    }


#: Recommendation values that flow through ``prepare_learning_promotions``
#: into ENTITY_CREATE payloads. The CLI surface checks the candidate's
#: ``recommendation_type`` against this set to decide whether to surface
#: the candidate as promotable in displays / filters.
PROMOTE_RECOMMENDATIONS: frozenset[str] = frozenset(
    {"promote_precedent", "promote_guidance"}
)


def _resolve_required_threshold(
    registry: ParameterRegistry, scope: ParameterScope, key: str
) -> float:
    """Resolve a required threshold from the registry.

    Per the POC directive (plan-self-improvement-program §2), no hard-coded
    fallback exists at the scoring layer. A missing key raises ``KeyError``
    naming both the key and the scope so operators can seed it.
    """
    # Sentinel object distinguishes "key resolved to None" from "key absent".
    sentinel: object = object()
    value = registry.get(scope, key, sentinel)
    if value is sentinel:
        msg = (
            f"ParameterRegistry is missing required key {key!r} for scope "
            f"{scope.key()!r}. Seed it via 'trellis admin init-learning-params' "
            f"or call ParameterStore.put() with a ParameterSet containing keys: "
            f"{list(REQUIRED_LEARNING_PARAMETER_KEYS)}."
        )
        raise KeyError(msg)
    return float(value)


def _recommend_learning_action(
    *,
    item_type: str,
    success_rate: float,
    retry_rate: float | None,
    registry: ParameterRegistry,
) -> str | None:
    """Classify one candidate, skipping any conjunct nothing measured.

    ``retry_rate`` is ``None`` when no observation reported ``had_retry``.
    An unmeasured conjunct **drops out of the rule** rather than
    evaluating: it must not silently satisfy the promote conjunct (which
    a fabricated ``0.0`` did, since ``0.0 <= promote_retry`` for every
    threshold an operator would set) nor silently fail the noise disjunct
    (which the same ``0.0`` did, since ``0.0 >= noise_retry`` never
    holds). Both arms therefore behave exactly as they did while the rate
    was manufactured — the rule was *already* single-variable and this
    only makes it say so. ``metrics_coverage`` on the candidate records
    which conjuncts were evaluated, so a reviewer reads a rule that ran on
    one variable as a rule that ran on one variable.
    """
    scope = ParameterScope(component_id=LEARNING_SCORING_COMPONENT)
    promote_success = _resolve_required_threshold(
        registry, scope, LEARNING_PROMOTE_SUCCESS_KEY
    )
    promote_retry = _resolve_required_threshold(
        registry, scope, LEARNING_PROMOTE_RETRY_KEY
    )
    noise_success = _resolve_required_threshold(
        registry, scope, LEARNING_NOISE_SUCCESS_KEY
    )
    noise_retry = _resolve_required_threshold(registry, scope, LEARNING_NOISE_RETRY_KEY)

    retry_low = retry_rate is None or retry_rate <= promote_retry
    retry_high = retry_rate is not None and retry_rate >= noise_retry

    if success_rate >= promote_success and retry_low:
        if item_type == "precedent":
            return "promote_precedent"
        return "promote_guidance"
    if success_rate <= noise_success or retry_high:
        return "investigate_noise"
    return None


def _candidate_category(recommendation_type: str) -> str:
    if recommendation_type == "promote_precedent":
        return "retrieval_precedent"
    if recommendation_type == "promote_guidance":
        return "retrieval_guidance"
    return "retrieval_noise"


def _build_precedent_description(
    candidate: Mapping[str, Any],
    *,
    rationale: str,
) -> str:
    metrics = candidate.get("metrics", {})
    if not isinstance(metrics, Mapping):
        metrics = {}
    family = candidate.get("intent_family", "unknown")
    item = candidate.get("item_id", "unknown")
    sr = metrics.get("success_rate")
    rr = metrics.get("retry_rate")
    # A promotion mints a durable node whose description is read by humans
    # and served back as context, so it states only what was measured.
    # ``retry_rate`` is ``None`` on every deployment that has no producer
    # for ``had_retry``; printing "retry_rate=0.0" there would persist a
    # fabricated measurement into the graph permanently.
    measured = (
        f"success_rate={sr} and retry_rate={rr}"
        if rr is not None
        else f"success_rate={sr} (retry_rate not measured)"
    )
    parts = [
        f"Reviewed learning for intent family '{family}'.",
        f"Source item '{item}' showed {measured}.",
    ]
    # Citation evidence, stated rather than implied. ``success_rate`` is a
    # pack-level outcome that says nothing about whether a grader ever
    # found *this item* useful, and on the live corpus most promote
    # candidates were never cited helpful by anyone.
    citations = candidate.get("citation_evidence")
    if isinstance(citations, Mapping):
        helpful = int(citations.get("helpful_count", 0) or 0)
        unhelpful = int(citations.get("unhelpful_count", 0) or 0)
        if helpful or unhelpful:
            parts.append(
                f"Graders cited it helpful {helpful} time(s) and unhelpful "
                f"{unhelpful} time(s)."
            )
        else:
            parts.append("No grader cited this item either way.")
    if rationale:
        parts.append(f"Review rationale: {rationale}")
    return " ".join(parts)


def _candidate_id(*, intent_family: str, item_id: str) -> str:
    digest = hashlib.sha256(f"{intent_family}|{item_id}".encode()).hexdigest()[:12]
    return f"{intent_family}:{digest}"


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return normalized or "learning"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = [
    "LEARNING_NOISE_RETRY_KEY",
    "LEARNING_NOISE_SUCCESS_KEY",
    "LEARNING_PROMOTABLE_MIN_HELPFUL_COUNT",
    "LEARNING_PROMOTE_RETRY_KEY",
    "LEARNING_PROMOTE_SUCCESS_KEY",
    "LEARNING_SCORING_COMPONENT",
    "PROMOTE_RECOMMENDATIONS",
    "REQUIRED_LEARNING_PARAMETER_KEYS",
    "analyze_learning_observations",
    "build_learning_promotion_payloads",
    "normalize_intent_family",
    "prepare_learning_promotions",
    "submit_learning_promotion",
    "write_learning_review_artifacts",
]
