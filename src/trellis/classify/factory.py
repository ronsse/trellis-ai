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

from trellis.classify.classifiers.keyword import (
    KeywordDomainClassifier,
    build_domain_keyword_map,
)
from trellis.classify.classifiers.source_system import SourceSystemClassifier
from trellis.classify.classifiers.structural import StructuralClassifier
from trellis.classify.pipeline import ClassifierPipeline

logger = structlog.get_logger(__name__)

#: Top-level key in ``config.yaml`` carrying classification settings.
CLASSIFY_CONFIG_KEY = "classify"
#: Sub-key under ``classify`` carrying the ``domain -> [keywords]`` map.
DOMAIN_KEYWORDS_KEY = "domain_keywords"

#: ``classify`` sub-key listing domain tags that name an *aspect* of
#: engagement rather than a subject — ``planning``, ``research``,
#: ``troubleshooting``. They may never be a merge destination: collapsing
#: ``estate-planning`` and ``trip-planning`` into ``planning`` keeps the mode
#: and discards the subject, which is the half that identifies the document.
#: Whether a noun is a subject or an aspect is semantic, not structural — a
#: corpus cannot distinguish "documents about hunting" from "documents doing
#: planning", because both head compound tags identically — so this is
#: declared, never inferred.
DOMAIN_ASPECTS_KEY = "domain_aspects"

#: ``classify`` sub-key holding the ``alias -> canonical`` domain merge map.
#: Surface-only in exactly the way :data:`DOMAIN_KEYWORDS_KEY` is: proposed by
#: :mod:`trellis.learning.domain_normalization`, written by a human.
DOMAIN_ALIASES_KEY = "domain_aliases"


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


def effective_domain_aspects(
    classify_config: Mapping[str, Any] | None = None,
) -> frozenset[str]:
    """Pull and validate the ``domain_aspects`` list out of ``classify`` config.

    Empty when absent: every tag is treated as a subject until someone says
    otherwise, which merges more than it should rather than less. That is the
    right default only because merges are human-approved.
    """
    if not classify_config:
        return frozenset()
    raw = classify_config.get(DOMAIN_ASPECTS_KEY)
    if raw is None:
        return frozenset()
    if not isinstance(raw, list) or not all(isinstance(t, str) and t for t in raw):
        msg = (
            f"config.yaml {CLASSIFY_CONFIG_KEY}.{DOMAIN_ASPECTS_KEY} must be a "
            f"list of non-empty tag strings."
        )
        raise ValueError(msg)
    return frozenset(raw)


def effective_domain_aliases(
    classify_config: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Pull and validate the ``domain_aliases`` map out of ``classify`` config.

    Returns an empty map when the section is absent — the unnormalized
    vocabulary is the correct default, because an alias map that nobody wrote
    should merge nothing. Raises ``ValueError`` when the section is present but
    malformed, so a misconfigured ``config.yaml`` fails loudly rather than
    silently declining to merge.

    A self-mapping (``a -> a``) and a chain (``a -> b -> c``) are both
    rejected. The first is a no-op that reads as intent; the second implies a
    transitive rewrite that
    :func:`~trellis.learning.domain_normalization.normalize_domain_tags`
    deliberately does not perform, so accepting it would mean the map does
    something other than what it says.
    """
    if not classify_config:
        return {}
    raw = classify_config.get(DOMAIN_ALIASES_KEY)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        msg = (
            f"config.yaml {CLASSIFY_CONFIG_KEY}.{DOMAIN_ALIASES_KEY} must be a "
            f"mapping of alias -> canonical, got {type(raw).__name__}."
        )
        raise ValueError(msg)  # noqa: TRY004
    aliases: dict[str, str] = {}
    for alias, canonical in raw.items():
        if not isinstance(canonical, str) or not canonical:
            msg = (
                f"config.yaml {CLASSIFY_CONFIG_KEY}.{DOMAIN_ALIASES_KEY}: "
                f"alias {alias!r} must map to a non-empty canonical tag string."
            )
            raise ValueError(msg)
        if str(alias) == canonical:
            msg = (
                f"config.yaml {CLASSIFY_CONFIG_KEY}.{DOMAIN_ALIASES_KEY}: "
                f"alias {alias!r} maps to itself."
            )
            raise ValueError(msg)
        aliases[str(alias)] = canonical

    chained = sorted(a for a in aliases.values() if a in aliases)
    if chained:
        msg = (
            f"config.yaml {CLASSIFY_CONFIG_KEY}.{DOMAIN_ALIASES_KEY}: "
            f"{chained!r} appear as both an alias and a merge destination. "
            f"Merging is one step only — point them at the final canonical."
        )
        raise ValueError(msg)
    return aliases


def effective_domain_keywords(
    classify_config: Mapping[str, Any] | None = None,
) -> dict[str, list[str]]:
    """The ``domain -> [keywords]`` map a pipeline built here would classify with.

    Built-in defaults with ``classify_config['domain_keywords']`` merged over
    them — the same merge :func:`build_ingestion_pipeline` performs, which is
    why that function now calls this one. Two consumers must agree on it
    forever: the classifier, and the tag-keyword promotion loop
    (:mod:`trellis.learning.tag_evolution`), which needs to know what is
    *already* owned so it neither re-proposes a keyword nor reads its own prior
    promotion as fresh evidence. Deriving both from one function is what makes
    "already owned" mean the same thing on both sides.
    """
    return build_domain_keyword_map(
        config_domains=_extract_config_domains(classify_config)
    )


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
    "DOMAIN_ALIASES_KEY",
    "DOMAIN_ASPECTS_KEY",
    "DOMAIN_KEYWORDS_KEY",
    "build_ingestion_pipeline",
    "effective_domain_aliases",
    "effective_domain_aspects",
    "effective_domain_keywords",
]
