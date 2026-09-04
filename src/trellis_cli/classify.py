"""``trellis classify`` — tag backfill, shadow-mode LLM tagging, and promotion.

Four commands spanning the deterministic-to-LLM-and-back ladder:

* ``backfill`` — re-run the deterministic pipeline over stored documents.
* ``shadow`` — record what an LLM says about them, without serving it (#321
  Phase 1).
* ``shadow-report`` — compare the two, per facet and per document.
* ``tag-candidates`` — mine the shadow corpus for keyword rules the
  deterministic classifier could own, so the LLM can be switched off for what
  has been learned (#321 Phase 2).

``trellis classify backfill`` — re-tag documents already in the store.

Classify-on-write (``TRELLIS_ENABLE_CLASSIFY_ON_INGEST``, see
:mod:`trellis.classify.ingest`) only covers documents written *after* it was
enabled, and tags written at any point drift as the keyword vocabulary and the
graph around a document change. This command is the explicit,
operator-driven backfill for everything already stored: it pages the
DocumentStore, re-runs the deterministic tagging pipeline over every item whose
``content_tags.classified_at`` is missing or older than ``--max-age-days``, and
writes the fresh tags back — the same
:func:`~trellis.classify.refresh.reclassify_item` core the programmatic path
uses, so the two cannot drift.

Like ``trellis extract traces`` and ``trellis admin reindex-vectors``, this
command does **not** require the ingest-time feature flag — invoking it *is*
the opt-in. It never deletes tags: an item the pipeline produces no signal for
keeps whatever it had.

``--include-domain`` is the one dangerous switch and is off by default. See
:func:`~trellis.classify.refresh.reclassify_item` for why re-deriving the
``domain`` facet deterministically can hide a document from domain-scoped
retrieval.

**One deliberate divergence from classify-on-write.** The backfill builds its
pipeline with :meth:`StoreRegistry.build_ingestion_pipeline`, which seeds the
:class:`KeywordDomainClassifier` from ``classify.domain_keywords`` in
``config.yaml``; classify-on-write uses ``build_ingest_classifier()`` with
built-in defaults only, because it drops the ``domain`` facet at persist time
and operator vocabulary would have no effect there. It *does* have an effect
here even with ``--include-domain`` off: a keyword hit still contributes
``retrieval_affinity``, adds the classifier to ``classified_by``, and raises
that classifier's confidence (which drives per-facet merge precedence). So a
backfilled document can differ from the same document tagged at ingest. That
is the intended reading of a config block the operator wrote on purpose —
a backfill is an explicit operator action, not the silent write path — but it
is a real difference and is documented in ``operations.md`` too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
import typer

from trellis.classify.factory import (
    CLASSIFY_CONFIG_KEY,
    DOMAIN_ALIASES_KEY,
    DOMAIN_ASPECTS_KEY,
    DOMAIN_KEYWORDS_KEY,
)
from trellis.classify.refresh import DEFAULT_PAGE_SIZE, reclassify_stale
from trellis.classify.shadow import compare_shadow_to_live, shadow_classify_stale
from trellis.core.vector_metadata import resolve_vector_store
from trellis.learning import domain_normalization as dn
from trellis.learning.domain_normalization import (
    analyze_domain_alias_candidates,
    apply_normalization,
)
from trellis.learning.tag_evolution import (
    DEFAULT_SCAN_LIMIT,
    PARAM_COMPONENT_ID,
    RECOMMENDED_SEED_VALUES,
    REQUIRED_PARAM_KEYS,
    analyze_tag_keyword_candidates,
    apply_promotion,
)
from trellis.ops import ParameterRegistry
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis_cli.analyze import _InMemoryParameterStore
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_OK
from trellis_cli.output import build_console, emit_json
from trellis_cli.stores import _get_registry

if TYPE_CHECKING:
    from trellis.classify.protocol import Classifier
    from trellis.classify.refresh import BatchRefreshResult
    from trellis.classify.shadow import ShadowAgreementReport
    from trellis.learning.domain_normalization import DomainAliasCandidate
    from trellis.learning.tag_evolution import TagKeywordCandidate

logger = structlog.get_logger(__name__)

classify_app = typer.Typer(no_args_is_help=True)
console = build_console()

#: Warnings go to stderr so ``--format json`` stdout stays parseable.
err_console = build_console(stderr=True)


@classify_app.callback()
def _classify() -> None:
    """Backfill, shadow-tag, and mine promotion candidates for content tags."""


@classify_app.command("backfill")
def backfill(
    max_age_days: int = typer.Option(
        30,
        "--max-age-days",
        min=0,
        help=(
            "Re-tag items whose tags are older than this (0 = re-tag every "
            "scanned item regardless of freshness)."
        ),
    ),
    limit: int = typer.Option(
        0,
        "--limit",
        min=0,
        help="Stop after scanning this many documents (0 = all).",
    ),
    page_size: int = typer.Option(
        DEFAULT_PAGE_SIZE,
        "--page-size",
        min=1,
        help="Documents fetched per store round-trip.",
    ),
    include_domain: bool = typer.Option(
        False,
        "--include-domain",
        help=(
            "DANGEROUS: let the deterministic classifiers (re)assign the "
            "'domain' facet. 'domain' is the only facet that hard-excludes a "
            "document from a domain-scoped query on mismatch, so a wrong "
            "value hides content instead of just re-ranking it. Only use this "
            "with a pipeline you trust to compute domain."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report what would change without writing tags or emitting events.",
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Backfill / refresh content tags for stored documents."""
    registry = _get_registry()

    if include_domain:
        err_console.print(
            "[yellow]--include-domain is set: the deterministic classifiers "
            "may (re)assign the hard-excluding 'domain' facet.[/yellow]"
        )

    try:
        pipeline = registry.build_ingestion_pipeline()
    except ValueError as exc:
        # Scoped to the factory call *only*: a malformed classify: block in
        # config.yaml is an operator error, not a bug. Wrapping the scan too
        # would relabel any per-document ValueError (including every
        # pydantic.ValidationError, which subclasses it) as a config problem.
        # Per-document faults are counted by reclassify_stale instead.
        if output_format == "json":
            emit_json({"status": "error", "message": str(exc)})
        else:
            console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=EXIT_INTERNAL) from exc

    result = reclassify_stale(
        pipeline=pipeline,
        document_store=registry.knowledge.document_store,
        # Dry runs stay audit-silent: TAGS_REFRESHED claims a write that
        # did not happen.
        event_log=None if dry_run else registry.operational.event_log,
        max_age_days=max_age_days,
        limit=limit,
        page_size=page_size,
        include_domain=include_domain,
        dry_run=dry_run,
        # Dry runs sync nothing either — ``reclassify_item`` returns before
        # the write. #338: without this the backfill's tags reach the
        # document store and never the vector row the semantic axis reads.
        vector_store=None if dry_run else resolve_vector_store(registry),
    )

    summary = _summary(result, dry_run=dry_run, include_domain=include_domain)
    logger.info("classify_backfill_completed", **summary)

    if output_format == "json":
        emit_json(summary)
    else:
        _render_text(summary)
    raise typer.Exit(code=EXIT_OK)


def _summary(
    result: BatchRefreshResult,
    *,
    dry_run: bool,
    include_domain: bool,
) -> dict[str, object]:
    """Flat JSON-friendly view of a :class:`BatchRefreshResult`.

    ``status`` is ``"partial"`` when any document failed, so a machine
    consumer keying off it does not read a half-completed backfill as a
    clean run. ``errors`` carries the count either way.
    """
    return {
        "status": "partial" if result.errors else "ok",
        "scanned": result.scanned,
        "refreshed": result.refreshed,
        "skipped_fresh": result.skipped_fresh,
        "skipped_unchanged": result.skipped_unchanged,
        "skipped_no_signal": result.skipped_no_signal,
        "skipped_missing_content": result.skipped_missing_content,
        "errors": result.errors,
        "dry_run": dry_run,
        "include_domain": include_domain,
        "item_ids_refreshed": list(result.item_ids_refreshed),
    }


def _render_text(summary: dict[str, object]) -> None:
    """Human-readable rendering of the backfill summary."""
    verb = "would re-tag" if summary["dry_run"] else "re-tagged"
    console.print(
        f"[green]Classify backfill:[/green] {verb} {summary['refreshed']} of "
        f"{summary['scanned']} scanned "
        f"({summary['skipped_fresh']} still fresh, "
        f"{summary['skipped_unchanged']} unchanged, "
        f"{summary['skipped_no_signal']} no signal, "
        f"{summary['skipped_missing_content']} empty)"
    )
    if summary["errors"]:
        console.print(
            f"[red]  {summary['errors']} document(s) failed and were skipped — "
            f"see the log for the item IDs.[/red]"
        )
    if summary["dry_run"]:
        console.print("  [yellow]dry-run — nothing written, no events[/yellow]")


# ---------------------------------------------------------------------------
# Shadow-mode tagging (#321 Phase 1)
# ---------------------------------------------------------------------------


def _require_llm_facet_classifier() -> Classifier:
    """Build the enrichment-mode LLM classifier, or exit loudly.

    Shadow mode is opt-in but must be loud on misuse: an operator running the
    pass without an LLM configured gets an actionable error naming the missing
    config block, not a run that silently shadows nothing.
    """
    from trellis.classify.classifiers.llm import (  # noqa: PLC0415
        build_llm_facet_classifier,
    )
    from trellis.stores.registry import (  # noqa: PLC0415
        BackendNotInstalledError,
    )
    from trellis_workers.enrichment.service import (  # noqa: PLC0415
        EnrichmentService,
    )

    registry = _get_registry()
    try:
        llm = registry.build_llm_client()
    except BackendNotInstalledError as exc:
        console.print(
            f"[red]classify shadow requires an LLM SDK that is not installed: "
            f"{exc}[/red]\n"
            "[dim]Install it, e.g. 'uv pip install trellis-ai[llm-openai]', "
            "and configure an 'llm:' block in config.yaml.[/dim]"
        )
        raise typer.Exit(code=EXIT_INTERNAL) from exc
    if llm is None:
        console.print(
            "[red]classify shadow requires an LLM client but none is "
            "configured.[/red]\n"
            "[dim]Add an 'llm:' block to ~/.trellis/config.yaml (provider, "
            "api_key_env, model). A local model is the intended default here — "
            "the pass is ~1.6s per document and runs over the whole corpus.[/dim]"
        )
        raise typer.Exit(code=EXIT_INTERNAL)
    return build_llm_facet_classifier(
        registry,
        enrichment_service=EnrichmentService(
            llm, event_log=registry.operational.event_log
        ),
    )


@classify_app.command("shadow")
def shadow(
    limit: int = typer.Option(
        0, "--limit", min=0, help="Stop after scanning this many documents (0 = all)."
    ),
    max_age_days: int = typer.Option(
        -1,
        "--max-age-days",
        help=(
            "Re-judge shadow records older than this many days. The default "
            "(-1) judges only documents with no shadow record at all — each "
            "one costs a model call, so re-judging an unchanged document with "
            "an unchanged model buys nothing."
        ),
    ),
    page_size: int = typer.Option(
        DEFAULT_PAGE_SIZE, "--page-size", min=1, help="Documents per store round-trip."
    ),
    model_id: str = typer.Option(
        "",
        "--model-id",
        help="Label recorded on each record and event (e.g. 'hermes3:8b').",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be written without writing."
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Record LLM tags alongside — never in place of — the live tags.

    The precondition for teaching the deterministic classifier a vocabulary it
    has never observed (#321 Phase 1). Every verdict is written under a
    separate metadata key that no retrieval path reads and no tag filter can
    address, so this is safe to run against a production store: no pack
    ranking moves while the corpus accrues. Each judged document also emits a
    leak-safe ``MEMORY_OP_JUDGED`` training pair.

    Compare the result against the live tags with ``trellis classify
    shadow-report``, and mine it for promotable keyword rules with ``trellis
    classify tag-candidates``.
    """
    registry = _get_registry()
    classifier = _require_llm_facet_classifier()

    result = shadow_classify_stale(
        classifier=classifier,
        document_store=registry.knowledge.document_store,
        # Dry runs stay audit-silent: a MEMORY_OP_JUDGED event claims a
        # judgement that was not persisted.
        event_log=None if dry_run else registry.operational.event_log,
        max_age_days=None if max_age_days < 0 else max_age_days,
        limit=limit,
        page_size=page_size,
        model_id=model_id,
        dry_run=dry_run,
    )

    summary = {
        "status": "partial" if result.errors else "ok",
        "scanned": result.scanned,
        "written": result.written,
        "skipped_fresh": result.skipped_fresh,
        "skipped_no_signal": result.skipped_no_signal,
        "skipped_missing_content": result.skipped_missing_content,
        "errors": result.errors,
        # #421: documents whose content changed while the model was judging
        # them. A subset of ``written`` — the record landed on the *current*
        # content — so it is a concurrency signal, not a failure count.
        "stale_snapshot": result.stale_snapshot,
        "dry_run": dry_run,
        "item_ids_written": list(result.item_ids_written),
    }
    logger.info("classify_shadow_completed", **summary)

    if output_format == "json":
        emit_json(summary)
    else:
        verb = "would record" if dry_run else "recorded"
        console.print(
            f"[green]Shadow pass:[/green] {verb} {result.written} of "
            f"{result.scanned} scanned "
            f"({result.skipped_fresh} already shadowed, "
            f"{result.skipped_no_signal} no signal, "
            f"{result.skipped_missing_content} empty)"
        )
        if result.errors:
            console.print(
                f"[red]  {result.errors} document(s) failed and were skipped — "
                f"see the log for the item IDs.[/red]"
            )
        if result.stale_snapshot:
            console.print(
                f"[yellow]  {result.stale_snapshot} document(s) were written "
                f"concurrently while being judged — the shadow record was "
                f"merged onto the current content.[/yellow]"
            )
        if dry_run:
            console.print("  [yellow]dry-run — nothing written, no events[/yellow]")
    raise typer.Exit(code=EXIT_OK)


@classify_app.command("shadow-report")
def shadow_report(
    limit: int = typer.Option(
        0, "--limit", min=0, help="Stop after scanning this many documents (0 = all)."
    ),
    page_size: int = typer.Option(
        DEFAULT_PAGE_SIZE, "--page-size", min=1, help="Documents per store round-trip."
    ),
    per_document: bool = typer.Option(
        False,
        "--per-document",
        help="Include a row per shadowed document (json format only).",
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Compare shadow tags against live tags, per facet and per document.

    Read-only. Answers the two questions that decide whether a shadow corpus is
    worth promoting from: where does the LLM produce a facet the deterministic
    pipeline leaves empty (``live_missing`` — the coverage gain), and where do
    the two disagree outright.

    ``agreement_rate`` is ``null`` when nothing is comparable. That is
    deliberate: a rate over an empty denominator is not a measurement, and
    reporting one as a number is how a metric ends up wired to a constant.
    """
    registry = _get_registry()
    report = compare_shadow_to_live(
        document_store=registry.knowledge.document_store,
        limit=limit,
        page_size=page_size,
        collect_comparisons=per_document,
    )

    summary: dict[str, object] = {
        "status": "ok",
        "scanned": report.scanned,
        "with_shadow": report.with_shadow,
        "facets": {
            facet: {
                "agreed": agreement.agreed,
                "disagreed": agreement.disagreed,
                "live_missing": agreement.live_missing,
                "shadow_missing": agreement.shadow_missing,
                "both_missing": agreement.both_missing,
                "agreement_rate": agreement.agreement_rate,
            }
            for facet, agreement in report.per_facet.items()
        },
        "out_of_vocabulary_content_types": dict(report.out_of_vocabulary_content_types),
    }
    if per_document:
        summary["comparisons"] = [
            {
                "item_id": row.item_id,
                "live": row.live,
                "shadow": row.shadow,
                "agreements": row.agreements,
            }
            for row in report.comparisons
        ]

    if output_format == "json":
        emit_json(summary)
    else:
        _render_shadow_report(report)
    raise typer.Exit(code=EXIT_OK)


def _render_shadow_report(report: ShadowAgreementReport) -> None:
    """Human-readable rendering of a shadow-vs-live comparison."""
    console.print(
        f"[green]Shadow report:[/green] {report.with_shadow} shadowed of "
        f"{report.scanned} scanned"
    )
    if not report.with_shadow:
        console.print(
            "  [yellow]no shadow records — run 'trellis classify shadow' first[/yellow]"
        )
        return
    for facet, agreement in report.per_facet.items():
        rate = agreement.agreement_rate
        rate_text = "n/a" if rate is None else f"{rate:.0%}"
        console.print(
            f"  {facet}: agreed {agreement.agreed}, disagreed "
            f"{agreement.disagreed}, LLM-only {agreement.live_missing}, "
            f"live-only {agreement.shadow_missing} (agreement {rate_text})"
        )
    if report.out_of_vocabulary_content_types:
        console.print(
            "  [yellow]content_type values outside the live vocabulary:[/yellow] "
            + ", ".join(
                f"{value} ({count})"
                for value, count in sorted(
                    report.out_of_vocabulary_content_types.items(),
                    key=lambda kv: -kv[1],
                )
            )
        )


# ---------------------------------------------------------------------------
# Tag-keyword promotion candidates (#321 Phase 2)
# ---------------------------------------------------------------------------


def _tag_evolution_registry(registry: object) -> ParameterRegistry:
    """Resolve the analyzer's thresholds, seeding recommended defaults if absent.

    Mirrors ``trellis analyze schema-evolution``'s
    ``_build_schema_evolution_registry``: the persistent store wins when its
    snapshot carries every required key, otherwise an in-memory snapshot seeded
    from :data:`RECOMMENDED_SEED_VALUES` plus one WARN line. Without this the
    command hard-fails on any deployment nobody has hand-seeded — which is
    every deployment, since ``trellis admin init-learning-params`` does not know
    about this component. The analyzer keeps its no-silent-defaults rule; the
    default is chosen *here*, loudly, by the CLI.
    """
    persistent = ParameterRegistry(registry.operational.parameter_store)  # type: ignore[attr-defined]
    scope = ParameterScope(component_id=PARAM_COMPONENT_ID)
    if all(k in persistent.get_values(scope) for k in REQUIRED_PARAM_KEYS):
        return persistent

    err_console.print(
        "[yellow]learning.tag_evolution thresholds are not seeded — using "
        "recommended defaults for this run. Seed them to make the run "
        "reproducible.[/yellow]"
    )
    logger.warning(
        "tag_evolution.parameter_registry.seeded_defaults",
        component=PARAM_COMPONENT_ID,
        defaults=dict(RECOMMENDED_SEED_VALUES),
    )
    store = _InMemoryParameterStore()
    store.put(
        ParameterSet(
            scope=scope,
            values=dict(RECOMMENDED_SEED_VALUES),
            source="cli:classify",
            notes="seeded by trellis_cli.classify._tag_evolution_registry",
        )
    )
    return ParameterRegistry(store=store)


@classify_app.command("tag-candidates")
def tag_candidates(
    facet: str = typer.Option(
        "domain",
        "--facet",
        help=(
            "Shadow facet to mine. 'domain' is the only facet with a config "
            "write target; others are surfaced for review only."
        ),
    ),
    limit: int = typer.Option(
        DEFAULT_SCAN_LIMIT, "--limit", min=1, help="Cap on documents scanned."
    ),
    emit: bool = typer.Option(
        True,
        "--emit/--no-emit",
        help=(
            "Emit a TAG_KEYWORD_CANDIDATE event per surfaced candidate. "
            "--no-emit is a dry run (and does not advance the cooldown)."
        ),
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Surface keyword rules the deterministic classifier could own.

    Mines the shadow corpus for keywords that predict an LLM-assigned tag, and
    proposes them — it never writes ``config.yaml``. For ``domain`` that is not
    a limitation but the safety property: ``domain`` is the only facet that
    *hard-excludes* a document from a domain-scoped query, so a wrong promoted
    keyword hides content rather than merely re-ranking it. A human approves
    every domain promotion.

    Candidates are gated on support, precision, and lift over the tag's base
    rate. Lift matters: in a corpus where one tag dominates, every keyword has
    high precision for it, so precision alone would surface the whole
    vocabulary as predictive.

    The text output ends with a ready-to-paste ``classify.domain_keywords``
    block. Paste it into ``config.yaml`` to promote; delete those lines to
    revoke.
    """
    registry = _get_registry()

    try:
        candidates = analyze_tag_keyword_candidates(
            document_store=registry.knowledge.document_store,
            event_log=registry.operational.event_log,
            registry=_tag_evolution_registry(registry),
            facet=facet,
            # Filters its own writes: a keyword the live classifier already
            # owns must not be re-proposed, nor accrue support the next run
            # could read as fresh evidence.
            known_keywords=[
                kw
                for keywords in registry.domain_keyword_map().values()
                for kw in keywords
            ],
            # Mine the normalized vocabulary: without this a keyword's support
            # is split across every spelling the model invented, so a rule that
            # predicts the subject perfectly can miss the support floor on
            # every fragment.
            domain_aliases=registry.domain_alias_map(),
            emit_events=emit,
            scan_limit=limit,
        )
    except (KeyError, ValueError) as exc:
        # Operator misconfiguration, not a bug, in three flavours: a missing
        # threshold (KeyError — the analyzer deliberately refuses to substitute
        # a default it was not given), an out-of-range threshold, and a
        # malformed `classify.domain_keywords` block reached through
        # `domain_keyword_map()` (both ValueError). Surface the message — it
        # names every missing key and its recommended seed — rather than a
        # traceback. `backfill` scopes its ValueError catch the same way.
        message = str(exc).strip("'\"")
        if output_format == "json":
            emit_json({"status": "error", "message": message})
        else:
            console.print(f"[red]{message}[/red]")
        raise typer.Exit(code=EXIT_INTERNAL) from exc

    if output_format == "json":
        emit_json(
            {
                "status": "ok",
                "facet": facet,
                "emitted": emit,
                "candidates": [c.to_event_payload() for c in candidates],
                "domain_keywords_fragment": apply_promotion(
                    {}, candidates
                ).domain_keywords,
            }
        )
    else:
        _render_tag_candidates(candidates, facet=facet, emitted=emit)
    raise typer.Exit(code=EXIT_OK)


def _render_tag_candidates(
    candidates: list[TagKeywordCandidate], *, facet: str, emitted: bool
) -> None:
    """Human-readable rendering, ending in a paste-ready config block."""
    if not candidates:
        console.print(
            f"[green]Tag candidates ({facet}):[/green] none surfaced.\n"
            "[dim]Either no keyword cleared the support / precision / lift "
            "gates, the shadow corpus is below the configured floor, or every "
            "candidate is inside its cooldown. Run 'trellis classify "
            "shadow-report' to check the corpus size.[/dim]"
        )
        return

    console.print(
        f"[green]Tag candidates ({facet}):[/green] {len(candidates)} surfaced"
        + ("" if emitted else " [yellow](dry run — not emitted)[/yellow]")
    )
    for candidate in candidates:
        console.print(
            f"  [bold]{candidate.keyword}[/bold] -> {candidate.tag}: "
            f"support {candidate.support}/{candidate.keyword_documents}, "
            f"precision {candidate.precision:.0%}, lift {candidate.lift:.1f}x"
            + ("" if candidate.has_write_target else "  [dim](no write target)[/dim]")
        )

    fragment = apply_promotion({}, candidates).domain_keywords
    if not fragment:
        return
    console.print(
        "\n[dim]To promote, merge into ~/.trellis/config.yaml (delete the "
        "lines to revoke):[/dim]"
    )
    console.print(f"{CLASSIFY_CONFIG_KEY}:")
    console.print(f"  {DOMAIN_KEYWORDS_KEY}:")
    for tag, keywords in fragment.items():
        console.print(f"    {tag}:")
        for keyword in keywords:
            console.print(f"      - {keyword}")
    console.print(
        "\n[yellow]Review before promoting: 'domain' hard-excludes on "
        "mismatch, so a wrong keyword hides documents from domain-scoped "
        "queries rather than re-ranking them.[/yellow]"
    )


def _domain_normalization_registry(registry: object) -> ParameterRegistry:
    """Resolve the normalizer's thresholds, seeding recommended defaults if absent.

    Same shape and same reasoning as :func:`_tag_evolution_registry`: the
    analyzer keeps its no-silent-defaults rule, and the default is chosen
    *here*, loudly, by the CLI — because otherwise the command hard-fails on
    every deployment nobody has hand-seeded, which is every deployment.
    """
    persistent = ParameterRegistry(registry.operational.parameter_store)  # type: ignore[attr-defined]
    scope = ParameterScope(component_id=dn.PARAM_COMPONENT_ID)
    if all(k in persistent.get_values(scope) for k in dn.REQUIRED_PARAM_KEYS):
        return persistent

    err_console.print(
        "[yellow]learning.domain_normalization thresholds are not seeded — "
        "using recommended defaults for this run. Seed them to make the run "
        "reproducible.[/yellow]"
    )
    logger.warning(
        "domain_normalization.parameter_registry.seeded_defaults",
        component=dn.PARAM_COMPONENT_ID,
        defaults=dict(dn.RECOMMENDED_SEED_VALUES),
    )
    store = _InMemoryParameterStore()
    store.put(
        ParameterSet(
            scope=scope,
            values=dict(dn.RECOMMENDED_SEED_VALUES),
            source="cli:classify",
            notes="seeded by trellis_cli.classify._domain_normalization_registry",
        )
    )
    return ParameterRegistry(store=store)


@classify_app.command("domain-candidates")
def domain_candidates(
    limit: int = typer.Option(
        dn.DEFAULT_SCAN_LIMIT, "--limit", min=1, help="Cap on documents scanned."
    ),
    min_gain: int = typer.Option(
        0,
        "--min-gain",
        min=0,
        help=(
            "Only show merges that would make at least this many documents "
            "newly reachable under the canonical tag. 0 shows all, including "
            "the redundant ones that only tidy the vocabulary."
        ),
    ),
    emit: bool = typer.Option(
        True,
        "--emit/--no-emit",
        help=(
            "Emit a DOMAIN_ALIAS_CANDIDATE event per surfaced candidate. "
            "--no-emit is a dry run (and does not advance the cooldown)."
        ),
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Propose ``alias -> canonical`` merges for the ``domain`` vocabulary.

    An open-vocabulary model invents a fresh near-synonym per document. Left
    alone that produces a facet whose values are mostly singletons, which
    cannot function as a filter — and ``domain`` is the one facet that
    *hard-excludes*, so a query scoped to ``hunting`` cannot see a document
    tagged only ``budget-hunting``.

    Proposes; never writes ``config.yaml``. The gate is deliberately narrow:
    a merge is generated only when the alias contains the canonical as a whole
    token, because co-occurrence — measured against a real corpus — proposes
    ``playwright -> hunting`` in any corpus where one subject dominates. It
    measures topical association, not synonymy. Here it corroborates, and the
    output labels every merge that spelling alone supports.

    Review the rows marked cross-cutting hardest: those aliases name two
    canonical subjects, so merging into either hides them from the other.
    """
    registry = _get_registry()

    try:
        candidates = analyze_domain_alias_candidates(
            document_store=registry.knowledge.document_store,
            event_log=registry.operational.event_log,
            registry=_domain_normalization_registry(registry),
            known_aliases=registry.domain_alias_map(),
            aspect_tags=registry.domain_aspect_tags(),
            emit_events=emit,
            scan_limit=limit,
        )
    except (KeyError, ValueError) as exc:
        message = str(exc).strip("'\"")
        if output_format == "json":
            emit_json({"status": "error", "message": message})
        else:
            console.print(f"[red]{message}[/red]")
        raise typer.Exit(code=EXIT_INTERNAL) from exc

    shown = [c for c in candidates if c.documents_gained >= min_gain]

    if output_format == "json":
        emit_json(
            {
                "status": "ok",
                "emitted": emit,
                "scanned_candidates": len(candidates),
                "candidates": [c.to_event_payload() for c in shown],
                "domain_aliases_fragment": apply_normalization(
                    {}, shown
                ).domain_aliases,
            }
        )
    else:
        _render_domain_candidates(shown, total=len(candidates), emitted=emit)
    raise typer.Exit(code=EXIT_OK)


def _render_domain_candidates(
    candidates: list[DomainAliasCandidate], *, total: int, emitted: bool
) -> None:
    """Human-readable rendering, ending in a paste-ready config block."""
    if not candidates:
        console.print(
            "[green]Domain alias candidates:[/green] none surfaced.\n"
            "[dim]Either no low-support tag contains a canonical tag as a "
            "whole token, every candidate is inside its cooldown, or the "
            "shadow corpus is empty. Run 'trellis classify shadow' first.[/dim]"
        )
        return

    suppressed = total - len(candidates)
    console.print(
        f"[green]Domain alias candidates:[/green] {len(candidates)} shown"
        + (f" ([dim]{suppressed} below --min-gain[/dim])" if suppressed else "")
        + ("" if emitted else " [yellow](dry run — not emitted)[/yellow]")
    )
    by_canonical: dict[str, list[DomainAliasCandidate]] = {}
    for candidate in candidates:
        by_canonical.setdefault(candidate.canonical, []).append(candidate)

    # Grouped by destination on purpose. A tag that names an *aspect* rather
    # than a subject gives itself away here and nowhere else: it collects a
    # pile of unrelated modifiers (`estate`, `trip`, `venture`), where a real
    # subject collects qualifiers of itself. No structural test found that —
    # one was built and measured, and it rated `hunting` more modifier-like
    # than `planning` — so the reviewer's eye is the detector and this is the
    # view that arms it.
    for canonical, group in by_canonical.items():
        console.print(f"  [bold]-> {canonical}[/bold] ({len(group)}):")
        for candidate in group:
            # Literal brackets are escaped: rich reads an unescaped `[...]` as
            # a markup tag and swallows it, which silently dropped exactly the
            # two warnings a reviewer most needs to see.
            marks = ""
            if candidate.competing_canonicals:
                marks += (
                    "  [yellow]\\[cross-cutting: also "
                    f"{', '.join(candidate.competing_canonicals)}][/yellow]"
                )
            if candidate.is_lexical_only:
                marks += "  [dim]\\[spelling only][/dim]"
            console.print(
                f"    [bold]{candidate.alias}[/bold]: "
                f"{candidate.alias_documents} docs, "
                f"{candidate.documents_gained} newly reachable" + marks
            )

    fragment = apply_normalization({}, candidates).domain_aliases
    console.print(
        "\n[dim]To merge, add to ~/.trellis/config.yaml (delete the lines to "
        "revoke):[/dim]"
    )
    console.print(f"{CLASSIFY_CONFIG_KEY}:")
    console.print(f"  {DOMAIN_ALIASES_KEY}:")
    for alias, canonical in fragment.items():
        console.print(
            f"    {alias}: {canonical}",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
    console.print(
        "\n[yellow]Review before merging: 'domain' hard-excludes on mismatch, "
        "so a wrong merge hides every document carrying the alias — in bulk, "
        "which is worse than one wrong keyword.[/yellow]"
    )
    console.print(
        "[dim]If a destination above collects unrelated subjects it names an "
        f"aspect, not a subject: list it under {CLASSIFY_CONFIG_KEY}."
        f"{DOMAIN_ASPECTS_KEY} and it stops attracting merges.[/dim]"
    )
