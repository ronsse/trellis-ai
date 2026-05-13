"""Shared meta-trace wiring helper for CLI commands.

Wraps :func:`trellis.meta.record_meta_analysis` with the bits every CLI
caller needs to repeat:

* fetch the cached :class:`~trellis.stores.registry.StoreRegistry` (the
  recorder needs the knowledge plane to write the Activity node);
* short-circuit to a no-op record when the operator passed
  ``--no-meta-trace``;
* prefix the synthetic ``agent_id`` with
  :data:`trellis.meta.agents.META_AGENT_PREFIX` so the PackBuilder
  default filter (Item 6 Phase 2) recognises it as Trellis-internal.

Use :func:`wrap_cli_meta_analysis` as a context manager around each
analyzer invocation::

    with wrap_cli_meta_analysis(
        agent_suffix="analyze",
        analyzer_name="cli.analyze.context-effectiveness",
        disabled=no_meta_trace,
    ) as record:
        report = analyze_effectiveness(event_log, ...)
        # Record the primary output as a finding; per-row instrumentation
        # is intentionally avoided to keep the graph proportional to
        # *change*, not to row count.
        if record.enabled and report.advisories_generated:
            record.produced_finding(
                f"effectiveness-report-{report.report_id}",
                finding_type="EffectivenessReport",
            )

Note: ``record.enabled`` is ``False`` when either the env var
``TRELLIS_META_TRACES=off`` is set or the operator passed
``--no-meta-trace``. The recorder's own methods are silent no-ops in
that state — the ``if record.enabled`` guard above is only relevant for
``finding_id``s the analyzer wouldn't otherwise compute. See
:func:`trellis.meta.record_meta_analysis` for the underlying primitive.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import structlog

from trellis.meta import (
    DEFAULT_MERGE_WINDOW_SECONDS,
    META_AGENT_PREFIX,
    MetaAnalysisRecord,
    record_meta_analysis,
)
from trellis_cli.stores import _get_registry

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = structlog.get_logger(__name__)

#: Standard agent_id prefix for CLI-originated meta-Activities. Every
#: wrapped command resolves to ``trellis_meta_cli_<suffix>`` so the
#: PackBuilder default filter (``META_AGENT_PREFIX``) catches them and
#: operators can grep ``analyzer_name`` for the subcommand.
_CLI_META_AGENT_PREFIX: str = f"{META_AGENT_PREFIX}cli"


def cli_meta_agent_id(suffix: str | None = None) -> str:
    """Return the synthetic-agent ID for a CLI meta-trace.

    ``suffix`` is appended (separated by ``_``) when provided. ``None``
    or empty string yields the bare ``trellis_meta_cli`` agent.

    Examples::

        cli_meta_agent_id()             -> "trellis_meta_cli"
        cli_meta_agent_id("analyze")    -> "trellis_meta_cli_analyze"
        cli_meta_agent_id("admin")      -> "trellis_meta_cli_admin"
    """
    if not suffix:
        return _CLI_META_AGENT_PREFIX
    return f"{_CLI_META_AGENT_PREFIX}_{suffix}"


class _NoopMetaRecord:
    """No-op stand-in returned when ``--no-meta-trace`` is set.

    Mirrors :class:`trellis.meta.MetaAnalysisRecord`'s public surface
    so call sites don't need ``if record.enabled`` guards around every
    method call. The methods discard their arguments.
    """

    enabled: bool = False
    activity_id: None = None

    def __init__(self, analyzer_name: str, agent_id: str) -> None:
        self.analyzer_name = analyzer_name
        self.agent_id = agent_id

    def consumed_event(self, event_id: str) -> None:
        del event_id

    def consumed_observation(self, observation_id: str) -> None:
        del observation_id

    def produced_finding(self, finding_id: str, finding_type: str) -> None:
        del finding_id, finding_type


@contextmanager
def wrap_cli_meta_analysis(
    *,
    agent_suffix: str,
    analyzer_name: str,
    disabled: bool = False,
    merge_window_seconds: int = DEFAULT_MERGE_WINDOW_SECONDS,
) -> Iterator[MetaAnalysisRecord | _NoopMetaRecord]:
    """Wrap a CLI analyzer invocation in a meta-trace context manager.

    Args:
        agent_suffix: Sub-suffix to append to the standard CLI agent
            namespace. ``"analyze"`` -> ``trellis_meta_cli_analyze``.
            Pass an empty string or a more specific suffix when a
            subsystem wants its own agent.
        analyzer_name: Stable analyzer name. Per the ADR, this is the
            ``analyzer_name`` property stamped on the Activity node and
            used as half of the merge-window dedup key.
        disabled: When ``True`` (typically because the operator passed
            ``--no-meta-trace``), skips the recorder entirely and
            yields a no-op record. The wrapped command's primary
            output is unaffected.
        merge_window_seconds: Forwarded to
            :func:`trellis.meta.record_meta_analysis`. Override only
            in tests that need a tight window.

    Yields:
        Either a real :class:`MetaAnalysisRecord` or a no-op stand-in
        when ``disabled=True``. Both expose the same minimal surface
        (``consumed_event`` / ``consumed_observation`` /
        ``produced_finding`` / ``activity_id`` / ``enabled``).
    """
    agent_id = cli_meta_agent_id(agent_suffix)
    if disabled:
        logger.debug(
            "cli_meta_trace.disabled_by_flag",
            agent_id=agent_id,
            analyzer_name=analyzer_name,
        )
        yield _NoopMetaRecord(analyzer_name=analyzer_name, agent_id=agent_id)
        return

    # ``_get_registry`` raises :class:`typer.Exit` when the operator
    # hasn't run ``trellis admin init``. The wrapped commands themselves
    # already validate that — falling through here would mask the real
    # error. Swallow the Exit ONLY for the meta-trace path so the
    # underlying command gets a chance to surface its own diagnostic.
    import typer  # noqa: PLC0415 — keep typer out of module-level import path

    try:
        registry = _get_registry()
    except typer.Exit:
        logger.info(
            "cli_meta_trace.skipped_no_registry",
            agent_id=agent_id,
            analyzer_name=analyzer_name,
        )
        yield _NoopMetaRecord(analyzer_name=analyzer_name, agent_id=agent_id)
        return

    with record_meta_analysis(
        analyzer_name=analyzer_name,
        agent_id=agent_id,
        registry=registry,
        merge_window_seconds=merge_window_seconds,
    ) as record:
        yield record


__all__ = [
    "cli_meta_agent_id",
    "wrap_cli_meta_analysis",
]
