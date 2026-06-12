"""KeywordDomainClassifier — maps keyword dictionaries to domain tags."""

from __future__ import annotations

from collections.abc import Mapping

from trellis.classify.protocol import (
    ClassificationContext,
    ClassificationResult,
)
from trellis.schemas.classification import (
    _format_reservation_error,
    _reserved_name_for,
)

_DEFAULT_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "data-pipeline": [
        "dbt",
        "spark",
        "airflow",
        "dag",
        "etl",
        "transform",
        "lineage",
        "warehouse",
        "databricks",
        "redshift",
        "snowflake",
    ],
    "infrastructure": [
        "kubernetes",
        "k8s",
        "docker",
        "terraform",
        "deploy",
        "cluster",
        "ec2",
        "vpc",
        "ecs",
        "lambda",
        "cloudformation",
    ],
    "api": [
        "endpoint",
        "route",
        "rest",
        "graphql",
        "request",
        "response",
        "middleware",
        "openapi",
        "swagger",
    ],
    "frontend": [
        "react",
        "component",
        "css",
        "dom",
        "render",
        "ui",
        "typescript",
        "jsx",
        "tsx",
        "tailwind",
    ],
    "backend": [
        "service",
        "handler",
        "orm",
        "migration",
        "celery",
        "queue",
        "worker",
        "sqlalchemy",
    ],
    "ml-ops": [
        "model",
        "training",
        "inference",
        "feature",
        "embedding",
        "pytorch",
        "tensorflow",
        "mlflow",
        "sagemaker",
    ],
    "security": [
        "auth",
        "token",
        "rbac",
        "permission",
        "cve",
        "vulnerability",
        "encrypt",
        "tls",
        "oauth",
        "jwt",
    ],
    "testing": [
        "pytest",
        "unittest",
        "assert",
        "fixture",
        "mock",
        "coverage",
        "test_",
        "conftest",
    ],
    "observability": [
        "logging",
        "metric",
        "tracing",
        "alert",
        "dashboard",
        "prometheus",
        "grafana",
        "datadog",
        "honeycomb",
    ],
}

_AFFINITY_KEYWORDS: dict[str, list[str]] = {
    "domain_knowledge": [
        "ownership",
        "governance",
        "compliance",
        "regulatory",
        "policy",
        "convention",
        "boundary",
        "precedent",
    ],
    "technical_pattern": [
        "pattern",
        "template",
        "example",
        "how-to",
        "implementation",
        "snippet",
        "boilerplate",
    ],
    "operational": [
        "error",
        "failure",
        "incident",
        "debug",
        "retry",
        "timeout",
        "exception",
        "traceback",
    ],
}

_MIN_HITS = 2
_MAX_DOMAINS = 3


def _validate_domain_names(domains: Mapping[str, list[str]], *, source: str) -> None:
    """Reject domain names that collide with a reserved policy namespace.

    Domains stay free strings (no enum, no registry — see
    ``docs/design/adr-tag-vocabulary-split.md``), but the policy-relevant
    namespaces enumerated in
    :data:`trellis.schemas.classification.RESERVED_NAMESPACES` are off-limits
    as domain values. A reserved name is rejected loudly at load time rather
    than silently dropped at classify time, so a typo in ``config.yaml`` never
    yields a silently-unassigned domain. ``source`` names the origin (e.g.
    ``"config.yaml classify.domain_keywords"``) for an actionable error.
    """
    for name in domains:
        reserved = _reserved_name_for(name)
        if reserved is not None:
            detail = _format_reservation_error(name, reserved, "domain value")
            msg = f"{source}: {detail}"
            raise ValueError(msg)


def build_domain_keyword_map(
    config_domains: Mapping[str, list[str]] | None = None,
    extra_domains: Mapping[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """Merge built-in defaults, config-supplied, and constructor-supplied maps.

    Precedence (later wins on key collision): built-in
    :data:`_DEFAULT_DOMAIN_KEYWORDS` < ``config_domains`` (from
    ``config.yaml`` ``classify.domain_keywords``) < ``extra_domains``
    (constructor param). Reserved-namespace names in either supplied map are
    rejected loudly (see :func:`_validate_domain_names`).
    """
    merged: dict[str, list[str]] = dict(_DEFAULT_DOMAIN_KEYWORDS)
    if config_domains:
        _validate_domain_names(
            config_domains, source="config.yaml classify.domain_keywords"
        )
        merged.update({k: list(v) for k, v in config_domains.items()})
    if extra_domains:
        _validate_domain_names(
            extra_domains, source="KeywordDomainClassifier(extra_domains=...)"
        )
        merged.update({k: list(v) for k, v in extra_domains.items()})
    return merged


class KeywordDomainClassifier:
    """Classify domain by keyword dictionary lookup.

    The keyword map merges three sources, later winning on key collision:
    built-in :data:`_DEFAULT_DOMAIN_KEYWORDS`, ``config_domains`` (seeded from
    ``config.yaml`` ``classify.domain_keywords`` — see
    :meth:`trellis.stores.registry.StoreRegistry.build_ingestion_pipeline`),
    and ``extra_domains`` (the legacy constructor param). Domains stay free
    strings; only reserved policy namespaces are rejected.
    """

    def __init__(
        self,
        extra_domains: Mapping[str, list[str]] | None = None,
        min_hits: int = _MIN_HITS,
        *,
        config_domains: Mapping[str, list[str]] | None = None,
    ) -> None:
        self._domains = build_domain_keyword_map(
            config_domains=config_domains, extra_domains=extra_domains
        )
        self._min_hits = min_hits

    @property
    def name(self) -> str:
        return "keyword_domain"

    @property
    def allowed_modes(self) -> frozenset[str]:
        from trellis.classify.protocol import BOTH_MODES  # noqa: PLC0415

        return BOTH_MODES

    def classify(
        self,
        content: str,
        *,
        context: ClassificationContext | None = None,  # noqa: ARG002
    ) -> ClassificationResult:
        content_lower = content.lower()
        matches: dict[str, int] = {}

        for domain, keywords in self._domains.items():
            hits = sum(1 for kw in keywords if kw.lower() in content_lower)
            if hits >= self._min_hits:
                matches[domain] = hits

        if not matches:
            return ClassificationResult(
                tags={},
                confidence=0.5,
                classifier_name=self.name,
                needs_llm_review=True,
            )

        domains = sorted(matches, key=lambda d: matches[d], reverse=True)[:_MAX_DOMAINS]
        max_hits = max(matches.values())
        confidence = min(0.95, 0.6 + 0.05 * max_hits)

        # Compute retrieval affinity tags (min_hits=1 for more specific terms)
        affinity_matches: list[str] = []
        for affinity, keywords in _AFFINITY_KEYWORDS.items():
            hits = sum(1 for kw in keywords if kw.lower() in content_lower)
            if hits >= 1:
                affinity_matches.append(affinity)

        tags: dict[str, list[str]] = {"domain": domains}
        if affinity_matches:
            tags["retrieval_affinity"] = affinity_matches

        return ClassificationResult(
            tags=tags,
            confidence=confidence,
            classifier_name=self.name,
        )
