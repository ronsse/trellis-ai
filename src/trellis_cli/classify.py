"""``trellis classify`` — tag backfill, shadow-mode LLM tagging, and promotion.

Four commands spanning the deterministic-to-LLM-and-back ladder (``label-candidates``,
a read-only scan that proposes documents for a human to label, sits beside them):

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

from typing import TYPE_CHECKING, Any

import structlog
import typer
from rich.markup import escape

from trellis.classify.factory import (
    CLASSIFY_CONFIG_KEY,
    DOMAIN_ASPECTS_KEY,
    effective_classify_rule_rows,
)
from trellis.classify.label_proposer import (
    DEFAULT_SCAN_LIMIT as DEFAULT_LABEL_SCAN_LIMIT,
)
from trellis.classify.label_proposer import NOTICE as LABEL_SCAN_NOTICE
from trellis.classify.label_proposer import scan_for_label_candidates
from trellis.classify.refresh import DEFAULT_PAGE_SIZE, reclassify_stale
from trellis.classify.shadow import compare_shadow_to_live, shadow_classify_stale
from trellis.core.text_safety import contains_nul_or_lone_surrogate
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
from trellis.llm.egress import EGRESS_WITHHELD_REASON, EgressGate
from trellis.llm.routing import LLMConsumer
from trellis.mutate import (
    Command,
    CommandStatus,
    Operation,
    build_classify_rules_executor,
)
from trellis.ops import ParameterRegistry
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.classify_overrides_store import (
    ClassifyOverridesStore,
    resolve_classify_overrides_path,
)
from trellis_cli.analyze import _InMemoryParameterStore
from trellis_cli.config import get_config_dir
from trellis_cli.exit_codes import (
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_STORE,
    EXIT_VALIDATION,
    refusal_exit_code,
)
from trellis_cli.output import active_output_format, build_console, emit_json
from trellis_cli.stores import _get_registry

if TYPE_CHECKING:
    from trellis.classify.label_proposer import LabelScanReport
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
            console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
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
        "skipped_precedent": result.skipped_precedent,
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
        f"{summary['skipped_missing_content']} empty"
        + (
            f", {summary['skipped_precedent']} promoted precedent"
            if summary["skipped_precedent"]
            else ""
        )
        + ")"
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
    config block, not a run that silently shadows nothing. The client is built
    for :attr:`~trellis.llm.routing.LLMConsumer.CLASSIFY_SHADOW`, so
    ``llm.routes.classify_shadow`` picks its tier; a malformed routing block
    propagates to the CLI boundary as the ``ConfigError`` it is.
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
        llm = registry.build_llm_client(consumer=LLMConsumer.CLASSIFY_SHADOW)
    except BackendNotInstalledError as exc:
        if active_output_format() == "json":
            emit_json(
                {
                    "status": "error",
                    "message": (
                        "classify shadow requires an LLM SDK that is not "
                        f"installed: {exc} Install it, e.g. 'uv pip install "
                        "trellis-ai[llm-openai]', and configure an 'llm:' block "
                        "in config.yaml."
                    ),
                }
            )
        else:
            console.print(
                f"[red]classify shadow requires an LLM SDK that is not installed: "
                f"{escape(str(exc))}[/red]\n"
                "[dim]Install it, e.g. 'uv pip install trellis-ai\\[llm-openai]', "
                "and configure an 'llm:' block in config.yaml.[/dim]",
                soft_wrap=True,
            )
        raise typer.Exit(code=EXIT_INTERNAL) from exc
    if llm is None:
        config_path = str(get_config_dir() / "config.yaml")
        if active_output_format() == "json":
            emit_json(
                {
                    "status": "error",
                    "message": (
                        "classify shadow requires an LLM client but none is "
                        f"configured. Add an 'llm:' block to {config_path} "
                        "(provider, api_key_env, model). A local model is the "
                        "intended default here — the pass is ~1.6s per document "
                        "and runs over the whole corpus."
                    ),
                }
            )
        else:
            console.print(
                "[red]classify shadow requires an LLM client but none is "
                "configured.[/red]\n"
                f"[dim]Add an 'llm:' block to {escape(config_path)} (provider, "
                "api_key_env, model). A local model is the intended default here — "
                "the pass is ~1.6s per document and runs over the whole corpus.[/dim]",
                soft_wrap=True,
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
        vector_store=resolve_vector_store(registry),
        # Dry runs stay audit-silent: a MEMORY_OP_JUDGED event claims a
        # judgement that was not persisted.
        event_log=None if dry_run else registry.operational.event_log,
        max_age_days=None if max_age_days < 0 else max_age_days,
        limit=limit,
        page_size=page_size,
        model_id=model_id,
        dry_run=dry_run,
        egress=EgressGate.for_route(
            registry, LLMConsumer.CLASSIFY_SHADOW, registry.knowledge.document_store
        ),
    )

    summary = {
        "status": "partial" if result.errors else "ok",
        "scanned": result.scanned,
        "written": result.written,
        "skipped_fresh": result.skipped_fresh,
        "skipped_no_signal": result.skipped_no_signal,
        "skipped_missing_content": result.skipped_missing_content,
        # Documents redacted while the model was judging them: the verdict is
        # dropped, not written onto the blank row, and is not an error.
        "skipped_redacted": result.skipped_redacted,
        # Documents labelled above internal, not sent to a classifier whose
        # route is not declared local (``trellis.llm.egress``).
        "skipped_egress_withheld": result.skipped_egress_withheld,
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
        if result.skipped_redacted:
            console.print(
                f"[yellow]  {result.skipped_redacted} document(s) were redacted "
                f"while being judged — no shadow record was written.[/yellow]"
            )
        if result.skipped_egress_withheld:
            console.print(
                f"[yellow]  {EGRESS_WITHHELD_REASON}: "
                f"{result.skipped_egress_withheld} document(s) labelled above "
                f"internal were not sent; the classify_shadow route is not "
                f"declared local.[/yellow]"
            )
        if result.errors:
            console.print(
                f"[red]  {result.errors} document(s) failed and were skipped — "
                f"see the log for the item IDs.[/red]",
                soft_wrap=True,
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

    The text output ends with a ready ``trellis classify apply-rule
    tag_keyword <keyword> <tag> --source-event-id <id>`` line per candidate
    -- never a ``config.yaml`` fragment to hand-paste. Promoting (or
    undoing) goes through that governed Apply/Revoke command, which is
    audited; nothing here writes ``config.yaml`` on your behalf.
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
            console.print(f"[red]{escape(message)}[/red]", soft_wrap=True)
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
    """Human-readable rendering, ending in a ready apply-rule command per row."""
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
        "\n[dim]Not written to config.yaml. To promote one, find its "
        "source event id via 'GET /api/v1/classify/candidates' (or the Tag "
        "Rules UI tab) and run the governed command, e.g.:[/dim]"
    )
    for tag, keywords in fragment.items():
        for keyword in keywords:
            console.print(
                f"  trellis classify apply-rule tag_keyword {keyword} {tag} "
                "--source-event-id <event-id>",
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
    console.print(
        "\n[yellow]Review before promoting: 'domain' hard-excludes on "
        "mismatch, so a wrong keyword hides documents from domain-scoped "
        "queries rather than re-ranking them. To undo an applied rule, run "
        "'trellis classify revoke-rule tag_keyword <keyword>'.[/yellow]"
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
            console.print(f"[red]{escape(message)}[/red]", soft_wrap=True)
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
    """Human-readable rendering, ending in a ready apply-rule command per row."""
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
        console.print(f"  [bold]-> {escape(canonical)}[/bold] ({len(group)}):")
        for candidate in group:
            # Literal brackets are escaped: rich reads an unescaped `[...]` as
            # a markup tag and swallows it, which silently dropped exactly the
            # two warnings a reviewer most needs to see. Identifier-shaped
            # values from the LLM vocabulary get the same treatment — they
            # sit inside `[bold]` / `[yellow]`, so `markup=False` would drop
            # the styling the comments already protect with `\[`.
            marks = ""
            if candidate.competing_canonicals:
                competing = ", ".join(
                    escape(name) for name in candidate.competing_canonicals
                )
                marks += f"  [yellow]\\[cross-cutting: also {competing}][/yellow]"
            if candidate.is_lexical_only:
                marks += "  [dim]\\[spelling only][/dim]"
            console.print(
                f"    [bold]{escape(candidate.alias)}[/bold]: "
                f"{candidate.alias_documents} docs, "
                f"{candidate.documents_gained} newly reachable" + marks
            )

    fragment = apply_normalization({}, candidates).domain_aliases
    console.print(
        "\n[dim]Not written to config.yaml. To promote one, find its "
        "source event id via 'GET /api/v1/classify/candidates' (or the Tag "
        "Rules UI tab) and run the governed command, e.g.:[/dim]"
    )
    for alias, canonical in fragment.items():
        console.print(
            f"  trellis classify apply-rule domain_alias {alias} {canonical} "
            "--source-event-id <event-id>",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
    console.print(
        "\n[yellow]Review before merging: this only changes how future "
        "keyword/alias candidates are mined and how rules are labelled -- "
        "it does not change the domain on any document already stored. "
        "To undo, run "
        "'trellis classify revoke-rule domain_alias <alias>'.[/yellow]"
    )
    console.print(
        "[dim]If a destination above collects unrelated subjects it names an "
        f"aspect, not a subject: list it under {CLASSIFY_CONFIG_KEY}."
        f"{DOMAIN_ASPECTS_KEY} and it stops attracting merges.[/dim]"
    )


# ---------------------------------------------------------------------------
# apply-rule / revoke-rule / effective-rules — CLI parity for the governed
# Apply/Revoke review flow (REST: trellis_api/routes/classify_rules.py).
# ---------------------------------------------------------------------------


def _render_classify_overrides_degradation(degraded: dict[str, Any] | None) -> None:
    """Print the classify-overrides store's degraded state, or nothing at all.

    Mirrors ``trellis_cli.policy._render_degradation`` /
    ``trellis_cli.admin._render_schedule_degradation`` -- same markup and
    wrapping hazards (an exception ``detail`` can contain ``[...]``; a long
    ``mv`` recovery command must survive as one line), same fix.
    """
    if not degraded:
        return
    console.print(
        f"  [bold red]CLASSIFY OVERLAY DEGRADED[/bold red] — "
        f"{escape(str(degraded['reason']))}: {escape(str(degraded['detail']))}",
        soft_wrap=True,
    )
    console.print(
        f"    file: [cyan]{escape(str(degraded['path']))}[/cyan] "
        f"({degraded['rows_loaded']} applied rule(s) readable, "
        f"{escape(str(degraded['rows_skipped_display']))} not)",
        soft_wrap=True,
    )
    console.print(
        "    Writes are refused so the file is intact. This listing is a "
        "partial view, not every applied rule.",
        soft_wrap=True,
    )
    console.print(f"    recovery: {escape(degraded['recovery'])}", soft_wrap=True)


def _reject_unsafe_cli_string(value: str, *, field: str, output_format: str) -> None:
    """Refuse a NUL byte or unpaired UTF-16 surrogate in a CLI argument.

    ``Command(...)`` raises *pydantic's own* ``ValidationError`` -- not
    ``trellis.errors.ValidationError``, the type
    ``_execute_classify_command``/``refusal_exit_code`` know how to map
    to an exit code -- for either hazard in a plain ``str`` field (issue
    #869 F6), uncaught, before a ``CommandResult`` exists to render.
    This check runs first so the same input instead exits
    :data:`EXIT_VALIDATION` -- the code this CLI already uses for bad
    input -- with a ``--format json`` payload shaped like
    ``_execute_classify_command``'s own, rather than a traceback and
    exit 1.

    Reuses :func:`trellis.core.text_safety.contains_nul_or_lone_surrogate`,
    the same predicate the REST dismiss route (``classify_rules.py``) and
    the Apply/Revoke audit probe (``handlers.py``) use (the latter via
    ``_find_unsafe_jsonb_string``), rather than re-deriving the check --
    and never echoes the raw value, for the same reason neither of
    those does.
    """
    if not contains_nul_or_lone_surrogate(value):
        return
    message = (
        f"{field} contains a NUL byte or an unpaired UTF-16 surrogate "
        "and cannot be processed"
    )
    if output_format == "json":
        emit_json(
            {
                "status": CommandStatus.REJECTED.value,
                "command_id": None,
                "operation": None,
                "message": message,
                "created_id": None,
                "warnings": [],
            }
        )
    else:
        console.print(f"[red]✗ {escape(message)}[/red]", soft_wrap=True)
    raise typer.Exit(code=EXIT_VALIDATION)


def _execute_classify_command(cmd: Command, output_format: str) -> None:
    """Submit *cmd*, render the result, and exit non-zero on refusal/failure.

    Mirrors ``trellis_cli.admin_settings._execute`` exactly: both formats
    are rendered first, the exit decided below that branch
    (``tests/unit/test_format_exit_parity_rule.py``).
    """
    registry = _get_registry()
    result = build_classify_rules_executor(registry).execute(cmd)

    if output_format == "json":
        emit_json(
            {
                "status": result.status.value,
                "command_id": result.command_id,
                "operation": result.operation,
                "message": result.message,
                "created_id": result.created_id,
                "warnings": list(result.warnings),
            }
        )
    else:
        if result.status == CommandStatus.SUCCESS:
            console.print(f"[green]✓ Command executed[/green]: {result.operation}")
        else:
            console.print(
                f"[red]✗ Command {result.status}[/red]: {result.operation}",
                soft_wrap=True,
            )
        console.print(f"  ID: {escape(result.command_id)}")
        console.print(
            f"  Message: {result.message}",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )

    if result.status in (CommandStatus.FAILED, CommandStatus.REJECTED):
        raise typer.Exit(code=refusal_exit_code(result))


def _print_apply_rule_risk_warning(kind: str, key: str, target: str) -> None:
    """Print the same truthful risk copy the UI shows before a confirm click.

    The UI only ever surfaces this on ``confirmApplyTagCandidate`` in
    ``index.html`` -- the CLI route to the identical governed write
    (``Operation.CLASSIFY_RULE_APPLY``) said nothing. Printed to stderr
    unconditionally, before the command even executes, so ``--format
    json`` stdout stays parseable and the exit code is unaffected either
    way (format-exit parity: this runs above that branch entirely).
    """
    if kind == "domain_alias":
        risk = (
            f"Merge {key!r} into {target!r}? This only changes how future "
            "keyword/alias candidates are mined and how rules are labelled "
            "-- it does not change the domain on any document already "
            "stored."
        )
    else:
        risk = (
            f"Apply {key!r} -> {target!r}? A single keyword never assigns "
            "a domain by itself: this classifier requires at least two "
            "matching keywords for the same domain in one document, and a "
            "wrong domain hides content from domain-scoped queries rather "
            "than just re-ranking it. Even once applied, this has no "
            "effect by itself -- new and existing content is never "
            "auto-tagged with domain at write time; it only reaches "
            "already-stored content if an administrator later runs a "
            "separate, explicitly opted-in reprocessing pass."
        )
    err_console.print(f"[yellow]{escape(risk)}[/yellow]", soft_wrap=True)


def _print_revoke_rule_risk_warning(kind: str, key: str) -> None:
    """Print the same truthful risk copy the UI shows before a confirm click.

    Mirrors ``_print_apply_rule_risk_warning`` for the revoke side
    (``confirmRevokeTagRule`` in ``index.html``): same stderr-only,
    format/exit-neutral posture.
    """
    if kind == "domain_alias":
        risk = (
            f"Revoke {key!r}? This only stops it affecting future "
            "candidate mining and this listing's badges -- it does not "
            "change the domain on any document already stored."
        )
    else:
        risk = (
            "Revoke this rule? Removing the override does not retag any "
            "content a reprocessing pass already assigned under it -- "
            "already-stored domain tags stay exactly as they are."
        )
    err_console.print(f"[yellow]{escape(risk)}[/yellow]", soft_wrap=True)


def _audit_identity() -> dict[str, str | None]:
    """CLI-actor identity stamped on a classify-rule audit event.

    A local invocation carries no authenticated credential, so this
    stamps the OS user in place of the REST route's ``key_id``/
    ``key_name`` (:func:`_audit_identity` in
    ``trellis_api.routes.classify_rules``) -- no other CLI module
    stamps identity this way. Apply's handler prefers this identity
    over ``requested_by`` for the audit event's ``applied_by`` field,
    so ``--by`` no longer reaches ``applied_by`` at all on
    ``apply-rule``; it still sets ``requested_by`` itself, a free-text
    label for the entry point, which ``revoke-rule`` copies directly as
    ``revoked_by`` (Revoke never reads this identity back the way Apply
    does).
    """
    import getpass  # noqa: PLC0415

    try:
        user: str | None = getpass.getuser()
    except Exception:  # pragma: no cover - exotic/no-passwd environments
        user = None
    return {"key_id": None, "key_name": f"cli:{user}" if user else "cli"}


@classify_app.command("apply-rule")
def apply_rule(
    kind: str = typer.Argument(..., help="'tag_keyword' or 'domain_alias'."),
    key: str = typer.Argument(
        ..., help="Keyword (tag_keyword) or alias (domain_alias) to apply."
    ),
    target: str = typer.Argument(
        ..., help="Domain (tag_keyword) or canonical tag (domain_alias)."
    ),
    source_event_id: str = typer.Option(
        ...,
        "--source-event-id",
        help=(
            "The TAG_KEYWORD_CANDIDATE/DOMAIN_ALIAS_CANDIDATE event this "
            "rule was mined from. 'trellis classify tag-candidates' / "
            "'domain-candidates' print a candidate_id, not this id -- read "
            "it from GET /api/v1/classify/candidates's 'source_event_id' "
            "field for the same candidate."
        ),
    ),
    support: int | None = typer.Option(
        None, "--support", help="Carried onto the audit event."
    ),
    precision: float | None = typer.Option(
        None, "--precision", help="Carried onto the audit event."
    ),
    lift: float | None = typer.Option(
        None, "--lift", help="Carried onto the audit event."
    ),
    requested_by: str = typer.Option(
        "cli:classify-apply-rule",
        "--by",
        help=(
            "Sets Command.requested_by, the audit event's entry-point "
            "label -- it does not reach applied_by, which always comes "
            "from the stamped OS-user identity instead."
        ),
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json."
    ),
) -> None:
    """Apply one mined candidate into the operator overlay (governed write).

    Routes through ``Operation.CLASSIFY_RULE_APPLY`` -- the same governed
    path the admin-scoped REST route (``POST /api/v1/classify/rules``)
    uses -- so a denying policy, a degraded store, or a malformed
    candidate is refused identically on both surfaces.
    """
    # Unlike the float-NaN case below, this one *does* need a pre-check:
    # a NUL byte or lone surrogate in ``key``/``requested_by`` makes
    # ``Command(...)`` itself raise pydantic's own ``ValidationError`` --
    # there is no ``CommandResult`` yet for ``_execute_classify_command``
    # to render, so skipping the check here means an uncaught traceback
    # and exit 1, not a parseable refusal (issue #869 F6).
    _reject_unsafe_cli_string(key, field="key", output_format=output_format)
    _reject_unsafe_cli_string(requested_by, field="--by", output_format=output_format)
    # Click's FLOAT type parses via Python's ``float()``, which happily
    # accepts "nan"/"inf"/"-inf" from the command line -- unlike the REST
    # DTO's ``allow_inf_nan=False``, nothing upstream of this command stops
    # it. No pre-check here: ``ClassifyRuleApplyHandler``'s own
    # ``_validate_classify_rule_metrics`` refuses the same value with the
    # same exit code, and it does so through ``_execute_classify_command``
    # below, which still renders a parseable ``--format json`` payload --
    # a pre-check that exits before that point left ``--format json``
    # callers with an empty stdout for an otherwise-ordinary rejection.
    _print_apply_rule_risk_warning(kind, key, target)
    cmd = Command(
        operation=Operation.CLASSIFY_RULE_APPLY,
        args={
            "kind": kind,
            "key": key,
            "target": target,
            "source_event_id": source_event_id,
            "support": support,
            "precision": precision,
            "lift": lift,
        },
        target_id=f"{kind}:{key}",
        target_type="classify_rule",
        requested_by=requested_by,
        metadata=_audit_identity(),
    )
    _execute_classify_command(cmd, output_format)


@classify_app.command("revoke-rule")
def revoke_rule(
    kind: str = typer.Argument(..., help="'tag_keyword' or 'domain_alias'."),
    key: str = typer.Argument(
        ..., help="Keyword (tag_keyword) or alias (domain_alias)."
    ),
    requested_by: str = typer.Option(
        "cli:classify-revoke-rule",
        "--by",
        help=(
            "Sets Command.requested_by, which the audit event copies "
            "directly as revoked_by -- unlike apply-rule, where the "
            "stamped OS-user identity wins instead."
        ),
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json."
    ),
) -> None:
    """Revoke one applied rule from the operator overlay.

    Refused -- not a no-op -- when ``(kind, key)`` has no overlay row,
    including when the rule instead comes from ``config.yaml``: that one
    was never written by this store and cannot be un-applied through it
    (see ``ClassifyRuleRevokeHandler``'s docstring for the two distinct
    refusal messages).
    """
    # Same pre-check as ``apply_rule``, same reason (issue #869 F6):
    # ``Command(...)`` raises an uncaught pydantic ``ValidationError``
    # on a NUL byte or lone surrogate before any ``CommandResult``
    # exists.
    _reject_unsafe_cli_string(key, field="key", output_format=output_format)
    _reject_unsafe_cli_string(requested_by, field="--by", output_format=output_format)
    _print_revoke_rule_risk_warning(kind, key)
    cmd = Command(
        operation=Operation.CLASSIFY_RULE_REVOKE,
        args={"kind": kind, "key": key},
        target_id=f"{kind}:{key}",
        target_type="classify_rule",
        requested_by=requested_by,
        metadata=_audit_identity(),
    )
    _execute_classify_command(cmd, output_format)


@classify_app.command("effective-rules")
def effective_rules(
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json."
    ),
) -> None:
    """Every effective tag-keyword / domain-alias rule, badged by source.

    Read-only parity for ``GET /api/v1/classify/rules``: both call
    :func:`~trellis.classify.factory.effective_classify_rule_rows`, so the
    precedence badging (``default`` / ``config`` / ``overlay``) can never
    disagree between the two surfaces.

    Works on a degraded overlay file -- the applied rules that parsed are
    still listed -- but says so and exits
    :data:`~trellis_cli.exit_codes.EXIT_STORE` rather than 0, mirroring
    ``trellis policy list`` / ``trellis admin due-jobs``: an applied rule
    that failed to parse is simply missing from this listing with no other
    signal, so a caller reading the exit code alone must not see a partial
    answer as a complete one.
    """
    registry = _get_registry()
    path = resolve_classify_overrides_path(registry.stores_dir)
    overlay_rows: dict[tuple[str, str], object] = {}
    degraded: dict[str, Any] | None = None
    if path is not None:
        store = ClassifyOverridesStore(path)
        overlay_rows = {(row.kind, row.key): row for row in store.list()}
        degradation = store.degradation
        degraded = degradation.to_dict() if degradation is not None else None

    try:
        domain_keyword_map = registry.domain_keyword_map()
        domain_alias_map = registry.domain_alias_map()
    except ValueError as exc:
        # Same precedent as ``backfill`` above: registry.domain_alias_map()
        # raises a bare ValueError when config.yaml and/or the overlay
        # merge into a self-map or chain (effective_domain_aliases) --
        # Revoke now refuses the write that would create one, but a
        # config.yaml edit alone can still produce this with no overlay
        # write to have caught it. Degrade visibly with the composed
        # reason instead of a traceback.
        if output_format == "json":
            emit_json({"status": "error", "message": str(exc)})
        else:
            console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(code=EXIT_INTERNAL) from exc

    rows = effective_classify_rule_rows(
        classify_config=registry.classify_config,
        domain_keyword_map=domain_keyword_map,
        domain_alias_map=domain_alias_map,
        overlay_rows=overlay_rows,
    )

    if output_format == "json":
        emit_json(
            {
                "status": "degraded" if degraded else "ok",
                "count": len(rows),
                "rules": [
                    {
                        **row,
                        "applied_at": row["applied_at"].isoformat()
                        if row["applied_at"] is not None
                        else None,
                    }
                    for row in rows
                ],
                "store_degradation": degraded,
            }
        )
        raise typer.Exit(code=EXIT_STORE if degraded else EXIT_OK)

    _render_classify_overrides_degradation(degraded)

    if not rows:
        console.print("[green]Effective classify rules:[/green] none.")
        raise typer.Exit(code=EXIT_STORE if degraded else EXIT_OK)

    console.print(f"[green]Effective classify rules:[/green] {len(rows)}")
    badges = {"default": "dim", "config": "cyan", "overlay": "bold green"}
    for row in rows:
        badge = badges[row["source"]]
        console.print(
            f"  [{badge}]{row['source']:>8}[/{badge}] "
            f"{escape(row['kind'])}: {escape(row['key'])} -> {escape(row['target'])}"
        )
    raise typer.Exit(code=EXIT_STORE if degraded else EXIT_OK)


@classify_app.command("label-candidates")
def label_candidates(
    limit: int = typer.Option(
        DEFAULT_LABEL_SCAN_LIMIT,
        "--limit",
        min=1,
        help=(
            "Cap on documents read. The report says whether the scan stopped "
            "here, and a stopped scan is not the whole store."
        ),
    ),
    page_size: int = typer.Option(
        DEFAULT_PAGE_SIZE,
        "--page-size",
        min=1,
        help="Documents fetched per store round-trip.",
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """List documents a human may want to label, and which rule fired.

    A report-only scan. It writes nothing -- no label, no tag, no event -- and
    a finding names a document id, a rule and a match count, never the value
    it matched or any text around it. Apply a label yourself, with ``trellis
    curate classify-sensitivity <doc_id> --sensitivity <level>``.

    Four plain rules run: email address, phone number, US SSN shape, and a
    card number that passes a Luhn check. Documents that already carry a label
    are skipped and counted. A miss proves nothing, and this is not a PII
    guarantee: the rules do not read medical content, and they over-report.
    """
    registry = _get_registry()
    report = scan_for_label_candidates(
        registry.knowledge.document_store, limit=limit, page_size=page_size
    )

    if output_format == "json":
        emit_json(report.to_payload())
    else:
        _render_label_candidates(report)
    raise typer.Exit(code=EXIT_OK)


def _render_label_candidates(report: LabelScanReport) -> None:
    """Human-readable rendering: one line per flagged document, rules and counts."""
    by_document: dict[str, list[str]] = {}
    for finding in report.findings:
        by_document.setdefault(finding.doc_id, []).append(
            f"{finding.rule} x{finding.matches}"
        )

    console.print(
        f"[green]Label candidates:[/green] {len(report.findings)} findings in "
        f"{report.documents_flagged} of {report.documents_scanned} documents scanned "
        f"({report.documents_read} read, {report.skipped_labelled} already "
        f"labelled, {report.skipped_unscannable} with no text to scan)"
    )
    for doc_id, rules in by_document.items():
        console.print(f"  {escape(doc_id)}: {', '.join(rules)}", soft_wrap=True)

    if report.stopped_at_limit:
        console.print(
            f"\n[yellow]Stopped at --limit {report.limit}: documents past it were "
            "not read, so this is not the whole store. Raise --limit to read "
            "more.[/yellow]"
        )
    if by_document:
        console.print(
            "\n[dim]Nothing was written. To label one, run: trellis curate "
            "classify-sensitivity <doc_id> --sensitivity <level>[/dim]"
        )
    console.print(f"\n[dim]{LABEL_SCAN_NOTICE}[/dim]")
