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
from typing import Any

_LEARNING_ARTIFACT_VERSION = "1.0"
_DEFAULT_MIN_SUPPORT = 2

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
            "phases": set(),
            "target_entity_ids": set(),
            "supporting_run_ids": set(),
            "evidence_refs": [],
            "times_served": 0,
            "success_count": 0,
            "retry_count": 0,
            "injected_count": 0,
            "selection_efficiency_total": 0.0,
            "selection_efficiency_count": 0,
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
    metrics["domain_systems"].update(
        entry
        for entry in (
            observation.get("domain"),
            item.get("domain_system"),
        )
        if str(entry or "").strip()
    )
    metrics["evidence_refs"].extend(
        str(ref).strip()
        for ref in observation.get("evidence_refs", [])
        if str(ref).strip()
    )
    if str(observation.get("outcome", "")).strip() == "success":
        metrics["success_count"] += 1
    if bool(observation.get("had_retry")):
        metrics["retry_count"] += 1
    if bool(observation.get("injected")):
        metrics["injected_count"] += 1

    sel_eff = observation.get("selection_efficiency")
    if isinstance(sel_eff, float | int):
        metrics["selection_efficiency_total"] += float(sel_eff)
        metrics["selection_efficiency_count"] += 1

    source_strategy = str(item.get("source_strategy", "")).strip()
    if source_strategy:
        metrics["source_strategies"][source_strategy] = (
            metrics["source_strategies"].get(source_strategy, 0) + 1
        )


def analyze_learning_observations(
    *,
    observations: Sequence[Mapping[str, Any]],
    min_support: int = _DEFAULT_MIN_SUPPORT,
    artifacts_root: str | Path | None = None,
) -> dict[str, Any]:
    candidate_map: dict[tuple[str, str], dict[str, Any]] = {}

    for observation in observations:
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
        retry_rate = metrics["retry_count"] / times_served if times_served else 0.0
        injection_rate = (
            metrics["injected_count"] / times_served if times_served else 0.0
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
            "phases": sorted(
                phase for phase in metrics["phases"] if str(phase).strip()
            ),
            "target_entity_ids": sorted(metrics["target_entity_ids"]),
            "supporting_run_ids": sorted(metrics["supporting_run_ids"]),
            "source_strategies": dict(sorted(metrics["source_strategies"].items())),
            "metrics": {
                "times_served": times_served,
                "success_rate": round(success_rate, 4),
                "retry_rate": round(retry_rate, 4),
                "injection_rate": round(injection_rate, 4),
                "avg_selection_efficiency": (
                    None
                    if avg_selection_efficiency is None
                    else round(avg_selection_efficiency, 4)
                ),
            },
            "evidence_refs": sorted(set(metrics["evidence_refs"]))[:10],
            "precedent_name": f"Learning: {intent_family} :: {title[:96]}".strip(),
            "precedent_properties": {
                "category": _candidate_category(recommendation_type),
                "intent_family": intent_family,
                "source_item_id": item_id,
                "source_item_type": metrics.get("item_type"),
                "success_rate": round(success_rate, 4),
                "retry_rate": round(retry_rate, 4),
                "support_count": times_served,
                "source_of_truth": "reviewed_promotion",
            },
        }
        candidates.append(candidate)

    return {
        "artifact_version": _LEARNING_ARTIFACT_VERSION,
        "generated_at_utc": _utc_now(),
        "artifacts_root": None if artifacts_root is None else str(Path(artifacts_root)),
        "min_support": int(min_support),
        "observation_count": len(observations),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def write_learning_review_artifacts(
    *,
    report: Mapping[str, Any],
    output_dir: str | Path,
) -> dict[str, str]:
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    candidates_path = target_dir / "intent_learning_candidates.json"
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
        if recommendation_type not in {"promote_precedent", "promote_guidance"}:
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


_PROMOTE_SUCCESS_THRESHOLD = 0.75
_PROMOTE_RETRY_THRESHOLD = 0.25
_NOISE_SUCCESS_THRESHOLD = 0.4
_NOISE_RETRY_THRESHOLD = 0.5


def _recommend_learning_action(
    *,
    item_type: str,
    success_rate: float,
    retry_rate: float,
) -> str | None:
    if (
        success_rate >= _PROMOTE_SUCCESS_THRESHOLD
        and retry_rate <= _PROMOTE_RETRY_THRESHOLD
    ):
        if item_type == "precedent":
            return "promote_precedent"
        return "promote_guidance"
    if success_rate <= _NOISE_SUCCESS_THRESHOLD or retry_rate >= _NOISE_RETRY_THRESHOLD:
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
    parts = [
        f"Reviewed learning for intent family '{family}'.",
        f"Source item '{item}' showed success_rate={sr} and retry_rate={rr}.",
    ]
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
    "analyze_learning_observations",
    "build_learning_promotion_payloads",
    "normalize_intent_family",
    "prepare_learning_promotions",
    "write_learning_review_artifacts",
]
