"""KeywordDomainClassifier — maps keyword dictionaries to domain tags."""

from __future__ import annotations

from trellis.classify.protocol import (
    ClassificationContext,
    ClassificationResult,
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


class KeywordDomainClassifier:
    """Classify domain by keyword dictionary lookup."""

    def __init__(
        self,
        extra_domains: dict[str, list[str]] | None = None,
        min_hits: int = _MIN_HITS,
    ) -> None:
        self._domains = dict(_DEFAULT_DOMAIN_KEYWORDS)
        if extra_domains:
            self._domains.update(extra_domains)
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
