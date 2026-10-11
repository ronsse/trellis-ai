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
from trellis.schemas.classification import _format_reservation_error, _reserved_name_for

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

#: ``classify`` sub-key declaring tags that ARE subjects despite appearing in
#: :data:`BUILTIN_DOMAIN_ASPECTS`. The inverse of :data:`DOMAIN_ASPECTS_KEY`,
#: and what makes a shipped default safe: whether a noun names a subject or an
#: activity depends on the corpus. ``documentation`` is an activity in a
#: personal vault and a subject to a docs-tooling team, and only they can say.
DOMAIN_SUBJECTS_KEY = "domain_subjects"

#: Activity nouns that name a *mode of engagement* rather than a subject, and
#: so may never be a merge destination.
#:
#: Generic English rather than corpus-specific, which is what makes a shipped
#: default defensible where an inferred one was not (see
#: :mod:`trellis.learning.domain_normalization`): "planning" describes what you
#: are doing *to* a subject in any vocabulary. Deliberately conservative —
#: ``automation``, ``migration``, ``testing``, ``deployment`` and
#: ``negotiation`` are all plausibly real subjects in a technical or
#: professional corpus, so they are left out, and an operator who wants them
#: adds them under :data:`DOMAIN_ASPECTS_KEY`.
BUILTIN_DOMAIN_ASPECTS: frozenset[str] = frozenset(
    {
        "analysis",
        "brainstorming",
        "comparison",
        "decision-making",
        "documentation",
        "evaluation",
        "exploration",
        "learning",
        "management",
        "monitoring",
        "optimization",
        "organization",
        "planning",
        "research",
        "review",
        "strategy",
        "tracking",
        "troubleshooting",
    }
)

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
    """The domain tags that may never be a merge destination.

    :data:`BUILTIN_DOMAIN_ASPECTS` plus ``classify.domain_aspects``, minus
    ``classify.domain_subjects``. Both directions are overridable because the
    call is genuinely corpus-dependent, and an operator must be able to say
    "this default is wrong for my vocabulary" without restating the list.

    Raises:
        ValueError: When either list is malformed, or a tag is in both.
    """
    declared = _tag_list(classify_config, DOMAIN_ASPECTS_KEY)
    exempted = _tag_list(classify_config, DOMAIN_SUBJECTS_KEY)

    both = sorted(declared & exempted)
    if both:
        msg = (
            f"config.yaml {CLASSIFY_CONFIG_KEY}: {both!r} appear in both "
            f"{DOMAIN_ASPECTS_KEY} and {DOMAIN_SUBJECTS_KEY}. A tag is one or "
            f"the other."
        )
        raise ValueError(msg)

    return (BUILTIN_DOMAIN_ASPECTS | declared) - exempted


def _tag_list(classify_config: Mapping[str, Any] | None, key: str) -> frozenset[str]:
    """Read one ``classify.<key>`` list of tag strings, or an empty set."""
    if not classify_config:
        return frozenset()
    raw = classify_config.get(key)
    if raw is None:
        return frozenset()
    if not isinstance(raw, list) or not all(isinstance(t, str) and t for t in raw):
        msg = (
            f"config.yaml {CLASSIFY_CONFIG_KEY}.{key} must be a list of "
            f"non-empty tag strings."
        )
        raise ValueError(msg)
    return frozenset(raw)


def _extract_config_aliases(
    classify_config: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Pull and validate the ``domain_aliases`` map out of ``classify`` config.

    Returns an empty map when the section is absent — the unnormalized
    vocabulary is the correct default, because an alias map that nobody wrote
    should merge nothing. Raises ``ValueError`` when the section is present but
    malformed, so a misconfigured ``config.yaml`` fails loudly rather than
    silently declining to merge.

    Only the per-entry shape (non-empty canonical, no self-mapping) is
    checked here. The chain check (``a -> b -> c``) runs once, in
    :func:`effective_domain_aliases`, over the *merged* config+overlay map —
    an overlay entry can introduce a chain a config-only read would never
    see.
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
    return aliases


def effective_domain_aliases(
    classify_config: Mapping[str, Any] | None = None,
    *,
    overlay_aliases: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The ``alias -> canonical`` map a reader should merge with, right now.

    ``config.yaml``'s ``classify.domain_aliases`` with ``overlay_aliases``
    merged on top (overlay wins on key collision) — the precedence
    ``config.yaml`` < operator overlay documented in
    :mod:`trellis.stores.classify_overrides_store`. ``overlay_aliases`` is
    normally
    :meth:`trellis.stores.classify_overrides_store.ClassifyOverridesStore.domain_aliases_map`,
    read fresh by the caller (:class:`~trellis.stores.registry.StoreRegistry`)
    so an applied rule is visible without a restart.

    A self-mapping (``a -> a``) and a chain (``a -> b -> c``) are both
    rejected, across the *merged* map — an overlay entry can introduce
    either even when ``config.yaml`` alone was clean. The first is a no-op
    that reads as intent; the second implies a transitive rewrite that
    :func:`~trellis.learning.domain_normalization.normalize_domain_tags`
    deliberately does not perform, so accepting it would mean the map does
    something other than what it says.
    """
    aliases = _extract_config_aliases(classify_config)
    for raw_alias, canonical in (overlay_aliases or {}).items():
        alias = str(raw_alias)
        if not isinstance(canonical, str) or not canonical:
            msg = (
                f"classify overlay: alias {alias!r} must map to a non-empty "
                "canonical tag string."
            )
            raise ValueError(msg)
        if alias == canonical:
            msg = f"classify overlay: alias {alias!r} maps to itself."
            raise ValueError(msg)
        aliases[alias] = canonical

    chained = sorted(a for a in aliases.values() if a in aliases)
    if chained:
        msg = (
            f"{CLASSIFY_CONFIG_KEY}.{DOMAIN_ALIASES_KEY} (config.yaml and/or "
            f"the operator overlay): {chained!r} appear as both an alias and "
            f"a merge destination. Merging is one step only — point them at "
            f"the final canonical."
        )
        raise ValueError(msg)
    return aliases


def _expand_overlay_domains(
    base: Mapping[str, list[str]],
    overlay_domains: Mapping[str, list[str]] | None,
) -> dict[str, list[str]] | None:
    """Overlay keywords, expanded to a full per-domain list against ``base``.

    :func:`~trellis.classify.classifiers.keyword.build_domain_keyword_map`
    merges its ``extra_domains`` layer by **replacing** the whole keyword
    list for a colliding domain key, not unioning it (confirmed by its own
    ``merged.update(...)``) — correct for a caller that names a domain's
    *complete* keyword list, wrong for this overlay, which stores one
    *additional* keyword per applied rule. Passed through unexpanded, an
    operator approving one new keyword for an existing domain would silently
    delete every other keyword ``config.yaml`` (or the built-in defaults)
    already gave that domain. So each overlay domain's keywords are unioned
    onto ``base``'s existing list for that domain *before* the caller hands
    the result to ``build_domain_keyword_map`` as ``extra_domains`` — by the
    time it does its own replacing update, there is nothing left to lose.

    Order-preserving and de-duplicating (first occurrence wins), so the
    result is deterministic across calls. Returns ``None`` for an empty or
    absent ``overlay_domains``, the sentinel ``build_domain_keyword_map``
    treats as "no third layer".
    """
    if not overlay_domains:
        return None
    expanded: dict[str, list[str]] = {}
    for domain, keywords in overlay_domains.items():
        combined = list(base.get(domain, []))
        for keyword in keywords:
            if keyword not in combined:
                combined.append(keyword)
        expanded[domain] = combined
    return expanded


def effective_domain_keywords(
    classify_config: Mapping[str, Any] | None = None,
    *,
    overlay_domains: Mapping[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """The ``domain -> [keywords]`` map a pipeline built here would classify with.

    Built-in defaults, ``classify_config['domain_keywords']`` merged over
    them, and ``overlay_domains`` merged over *that* — the same three-layer
    merge :func:`build_ingestion_pipeline` performs (default < ``config.yaml``
    < operator overlay), which is why that function now calls this one. The
    overlay layer is additive per domain (see :func:`_expand_overlay_domains`):
    applying one keyword to an existing domain keeps that domain's other
    keywords, it does not replace the list. ``overlay_domains`` is normally
    :meth:`trellis.stores.classify_overrides_store.ClassifyOverridesStore.domain_keywords_map`,
    read fresh by the caller (:class:`~trellis.stores.registry.StoreRegistry`)
    so an applied rule is visible without a restart. Three consumers must
    agree on the result forever: the classifier, the tag-keyword promotion
    loop (:mod:`trellis.learning.tag_evolution`, which needs to know what is
    *already* owned so it neither re-proposes a keyword nor reads its own
    prior promotion as fresh evidence) and the domain-alias miner. Deriving
    all three from one function is what makes "already owned" mean the same
    thing everywhere.
    """
    config_domains = _extract_config_domains(classify_config)
    base = build_domain_keyword_map(config_domains=config_domains)
    extra = _expand_overlay_domains(base, overlay_domains)
    if extra is None:
        return base
    return build_domain_keyword_map(
        config_domains=config_domains,
        extra_domains=extra,
        extra_domains_source="the operator overlay (classify_overrides.json)",
    )


def build_ingestion_pipeline(
    classify_config: Mapping[str, Any] | None = None,
    *,
    overlay_domains: Mapping[str, list[str]] | None = None,
) -> ClassifierPipeline:
    """Construct the default deterministic ingestion pipeline.

    Wires the deterministic classifiers Trellis ships (structural, keyword
    domain, source system) into a :class:`ClassifierPipeline` in ingestion
    mode. The keyword classifier's domain map comes from
    :func:`effective_domain_keywords` — the same three-layer merge (built-in
    defaults < ``config.yaml`` < operator overlay, overlay additive per
    domain) that the tag-keyword promotion loop and the domain-alias miner
    use, so this function cannot drift from theirs by re-deriving the merge
    independently. Reserved-namespace domain names are rejected loudly at
    load time (inside :func:`effective_domain_keywords`).

    Args:
        classify_config: The ``classify`` section of ``config.yaml`` (or any
            mapping with the same shape). ``None`` or empty yields the
            built-in defaults.
        overlay_domains: Applied ``tag_keyword`` rules from
            :class:`~trellis.stores.classify_overrides_store.ClassifyOverridesStore`,
            as ``domain -> [keyword, ...]``. ``None`` or empty applies none.

    Returns:
        A deterministic-only :class:`ClassifierPipeline` ready for ingestion.
    """
    config_domains = _extract_config_domains(classify_config)
    if config_domains:
        logger.debug(
            "ingestion_pipeline.config_domains_loaded",
            domains=sorted(config_domains),
        )
    domain_map = effective_domain_keywords(
        classify_config, overlay_domains=overlay_domains
    )
    keyword = KeywordDomainClassifier(extra_domains=domain_map)
    return ClassifierPipeline(
        classifiers=[
            StructuralClassifier(),
            keyword,
            SourceSystemClassifier(),
        ]
    )


def config_sourced_tag_keyword_domains(
    classify_config: Mapping[str, Any] | None, keyword: str
) -> list[str]:
    """Domains ``config.yaml``'s ``domain_keywords`` already maps ``keyword`` into.

    Empty when ``keyword`` is not in ``config.yaml`` at all. Used by
    ``ClassifyRuleRevokeHandler`` (:mod:`trellis.mutate.handlers`) to tell
    a config.yaml-sourced rule apart from one in the operator overlay
    (:class:`~trellis.stores.classify_overrides_store.ClassifyOverridesStore`):
    revoking the former is refused with a message pointing at
    ``config.yaml``, never silently treated as "no such rule". Also used
    by the ``GET /api/v1/classify/rules`` effective-rules listing to badge
    a row's source as ``config.yaml`` vs the overlay vs a built-in default.
    """
    config_domains = _extract_config_domains(classify_config)
    return sorted(
        domain for domain, keywords in config_domains.items() if keyword in keywords
    )


def is_config_sourced_alias(
    classify_config: Mapping[str, Any] | None, alias: str
) -> bool:
    """Whether ``alias`` comes from ``config.yaml``'s ``domain_aliases``."""
    return alias in _extract_config_aliases(classify_config)


def config_sourced_alias_target(
    classify_config: Mapping[str, Any] | None, alias: str
) -> str | None:
    """``config.yaml``'s canonical for ``alias``, or ``None`` if unset there.

    The value an applied overlay row for the same ``alias`` key makes
    non-effective going forward -- operator overlay wins over
    ``config.yaml`` on a colliding alias key (see
    :func:`effective_domain_aliases`), so this is "what reading ``alias``
    used to resolve to before the overlay row took precedence." Companion
    to :func:`is_config_sourced_alias`, which answers only whether one
    exists. Used by ``ClassifyRuleApplyHandler`` / ``ClassifyRuleRevokeHandler``
    (:mod:`trellis.mutate.handlers`) to record the shadowed ``config.yaml``
    value on the audit event, when there is one.

    There is no ``tag_keyword`` analogue: the overlay's keyword layer is
    additive per domain (:func:`_expand_overlay_domains`), so an applied
    keyword never makes a ``config.yaml``-sourced keyword/domain pairing
    stop applying -- nothing is shadowed for that kind.
    """
    return _extract_config_aliases(classify_config).get(alias)


def reject_reserved_rule_target(target: str) -> None:
    """Refuse a classify-rule ``target`` that names a reserved policy namespace.

    Both overlay kinds feed ``target`` into the deployment's domain
    vocabulary -- a ``tag_keyword`` row's target is the domain a keyword
    promotes into, a ``domain_alias`` row's target is the canonical domain
    an alias normalises to -- so both are checked against the same
    :data:`~trellis.schemas.classification.RESERVED_NAMESPACES` guard
    :func:`~trellis.classify.classifiers.keyword.build_domain_keyword_map`
    already enforces for a ``config.yaml``-sourced domain. Called by
    ``ClassifyRuleApplyHandler`` (:mod:`trellis.mutate.handlers`) *before*
    the row reaches the operator overlay file, so a reserved target is
    refused at apply time -- not discovered only once some later reader
    tries to build a merged domain map from the file and fails.

    Raises:
        ValueError: ``target`` collides with a reserved namespace, exact
            or ``namespace:...``-prefixed.
    """
    reserved = _reserved_name_for(target)
    if reserved is not None:
        detail = _format_reservation_error(target, reserved, "domain value")
        msg = f"classify rule target: {detail}"
        raise ValueError(msg)


def effective_classify_rule_rows(
    *,
    classify_config: Mapping[str, Any] | None,
    domain_keyword_map: Mapping[str, list[str]],
    domain_alias_map: Mapping[str, str],
    overlay_rows: Mapping[tuple[str, str], Any],
) -> list[dict[str, Any]]:
    """Every effective tag-keyword / domain-alias rule, badged by source.

    Shared by ``GET /api/v1/classify/rules``
    (:mod:`trellis_api.routes.classify_rules`) and ``trellis classify
    effective-rules`` so the precedence badging (``"default"`` built-in,
    ``"config"`` from ``config.yaml``, or ``"overlay"`` applied through
    the governed write path) lives in exactly one place rather than
    being re-derived twice.

    ``domain_keyword_map``/``domain_alias_map`` are each caller's own
    already-resolved :meth:`~trellis.stores.registry.StoreRegistry.domain_keyword_map`
    / ``domain_alias_map`` (full precedence already applied); this
    function only re-derives *which* layer won, never a second merge.
    ``overlay_rows`` is keyed ``(kind, key)`` -> a row exposing
    ``target``/``applied_at``/``applied_by``/``source_event_id`` (see
    :class:`~trellis.schemas.classify_overrides.ClassifyOverrideRow`).
    Callers resolve the registry and the overlay store themselves, so
    this module stays free of a ``trellis.stores`` import.
    """
    rows: list[dict[str, Any]] = []
    for domain, keywords in domain_keyword_map.items():
        for keyword in keywords:
            overlay = overlay_rows.get(("tag_keyword", keyword))
            if overlay is not None and overlay.target == domain:
                source = "overlay"
            elif domain in config_sourced_tag_keyword_domains(classify_config, keyword):
                source = "config"
            else:
                source = "default"
            rows.append(
                {
                    "kind": "tag_keyword",
                    "key": keyword,
                    "target": domain,
                    "source": source,
                    "applied_at": overlay.applied_at
                    if source == "overlay" and overlay is not None
                    else None,
                    "applied_by": overlay.applied_by
                    if source == "overlay" and overlay is not None
                    else None,
                    "source_event_id": overlay.source_event_id
                    if source == "overlay" and overlay is not None
                    else None,
                }
            )
    for alias, canonical in domain_alias_map.items():
        overlay = overlay_rows.get(("domain_alias", alias))
        if overlay is not None and overlay.target == canonical:
            source = "overlay"
        elif is_config_sourced_alias(classify_config, alias):
            source = "config"
        else:
            source = "default"
        rows.append(
            {
                "kind": "domain_alias",
                "key": alias,
                "target": canonical,
                "source": source,
                "applied_at": overlay.applied_at
                if source == "overlay" and overlay is not None
                else None,
                "applied_by": overlay.applied_by
                if source == "overlay" and overlay is not None
                else None,
                "source_event_id": overlay.source_event_id
                if source == "overlay" and overlay is not None
                else None,
            }
        )
    return rows


__all__ = [
    "CLASSIFY_CONFIG_KEY",
    "DOMAIN_ALIASES_KEY",
    "BUILTIN_DOMAIN_ASPECTS",
    "DOMAIN_ASPECTS_KEY",
    "DOMAIN_SUBJECTS_KEY",
    "DOMAIN_KEYWORDS_KEY",
    "build_ingestion_pipeline",
    "effective_domain_aliases",
    "effective_domain_aspects",
    "effective_domain_keywords",
    "effective_classify_rule_rows",
    "config_sourced_tag_keyword_domains",
    "config_sourced_alias_target",
    "is_config_sourced_alias",
    "reject_reserved_rule_target",
]
