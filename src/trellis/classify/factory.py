"""Factory for the default deterministic ingestion pipeline.

Builds a :class:`~trellis.classify.pipeline.ClassifierPipeline` wired with the
deterministic classifiers Trellis ships, seeding the
:class:`~trellis.classify.classifiers.keyword.KeywordDomainClassifier` from the
operator's ``config.yaml`` ``classify.domain_keywords`` section. Keeping the
config-to-classifier wiring here (rather than re-deriving it at each call site)
means an operator can add a custom domain by editing ``config.yaml`` alone — no
code change.

Ingestion mode only: no LLM classifier is attached, so the returned pipeline is
deterministic and inline. The enrichment path (LLM fallback) is wired
separately by the enrichment worker.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog

from trellis.classify.classifiers.keyword import KeywordDomainClassifier
from trellis.classify.classifiers.source_system import SourceSystemClassifier
from trellis.classify.classifiers.structural import StructuralClassifier
from trellis.classify.pipeline import ClassifierPipeline

logger = structlog.get_logger(__name__)

#: Top-level key in ``config.yaml`` carrying classification settings.
CLASSIFY_CONFIG_KEY = "classify"
#: Sub-key under ``classify`` carrying the ``domain -> [keywords]`` map.
DOMAIN_KEYWORDS_KEY = "domain_keywords"


def _extract_config_domains(
    classify_config: Mapping[str, Any] | None,
) -> dict[str, list[str]]:
    """Pull and validate the ``domain_keywords`` map out of ``classify`` config.

    Returns an empty map when the section is absent. Raises ``ValueError`` when
    the section is present but malformed (not a mapping, or a value that is not
    a list of strings) so a misconfigured ``config.yaml`` fails loudly at load
    time rather than silently dropping the operator's custom domains.
    """
    if not classify_config:
        return {}
    raw = classify_config.get(DOMAIN_KEYWORDS_KEY)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        msg = (
            f"config.yaml {CLASSIFY_CONFIG_KEY}.{DOMAIN_KEYWORDS_KEY} must be a "
            f"mapping of domain -> [keywords], got {type(raw).__name__}."
        )
        # ValueError (not TypeError): this is a config-shape validation error,
        # consistent with how the rest of config loading reports bad values.
        raise ValueError(msg)  # noqa: TRY004
    domains: dict[str, list[str]] = {}
    for name, keywords in raw.items():
        if not isinstance(keywords, list) or not all(
            isinstance(kw, str) for kw in keywords
        ):
            msg = (
                f"config.yaml {CLASSIFY_CONFIG_KEY}.{DOMAIN_KEYWORDS_KEY}: "
                f"domain {name!r} must map to a list of keyword strings."
            )
            raise ValueError(msg)
        domains[str(name)] = list(keywords)
    return domains


def build_ingestion_pipeline(
    classify_config: Mapping[str, Any] | None = None,
) -> ClassifierPipeline:
    """Construct the default deterministic ingestion pipeline.

    Wires the deterministic classifiers Trellis ships (structural, keyword
    domain, source system) into a :class:`ClassifierPipeline` in ingestion
    mode. The keyword classifier's domain map is seeded from
    ``classify_config['domain_keywords']`` merged over the built-in defaults
    (config wins on key collision); reserved-namespace domain names are
    rejected loudly here, at load time.

    Args:
        classify_config: The ``classify`` section of ``config.yaml`` (or any
            mapping with the same shape). ``None`` or empty yields the
            built-in defaults.

    Returns:
        A deterministic-only :class:`ClassifierPipeline` ready for ingestion.
    """
    config_domains = _extract_config_domains(classify_config)
    if config_domains:
        logger.debug(
            "ingestion_pipeline.config_domains_loaded",
            domains=sorted(config_domains),
        )
    keyword = KeywordDomainClassifier(config_domains=config_domains)
    return ClassifierPipeline(
        classifiers=[
            StructuralClassifier(),
            keyword,
            SourceSystemClassifier(),
        ]
    )


__all__ = [
    "CLASSIFY_CONFIG_KEY",
    "DOMAIN_KEYWORDS_KEY",
    "build_ingestion_pipeline",
]
