"""Trellis cost overhead — what memory adds on top of an agent's bill.

Answers "how much did Trellis cost me?" in dollars, by pricing the
context Trellis injected into agent turns. Builds on
:func:`~trellis.retrieve.token_usage.analyze_token_usage` (which sums the
``TOKEN_TRACKED`` events every context tool emits) and applies an
input-token price (:mod:`trellis.retrieve.token_pricing`).

What this measures precisely: the tokens Trellis *added* to agent
prompts via ``get_context`` / ``get_lessons`` / the other macro tools —
the marginal overhead of having memory in the loop. What it deliberately
does **not** claim: the agent's *total* spend (Trellis never sees the
model's own generation) or a ratio against it. To get the overhead
*fraction*, compare ``overhead_dollars`` against the provider's
input-token bill for the same window. The token count is exact (from the
events); the dollar figure is an estimate — the per-token counter is the
~4-chars/token heuristic and the price is an operator-owned assumption.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trellis.core.base import TrellisModel
from trellis.retrieve.token_pricing import estimate_dollars, resolve_pricing
from trellis.retrieve.token_usage import analyze_token_usage

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog


class TrellisCostReport(TrellisModel):
    """Priced summary of Trellis's context-injection overhead."""

    period_days: int
    #: Number of context-injection responses in the window.
    overhead_events: int
    #: Total tokens Trellis injected into agent prompts (the overhead).
    overhead_tokens: int
    #: Consuming model the price is drawn for.
    model: str
    #: Input price applied, USD per million tokens.
    price_per_mtok: float
    #: Which input won the price — ``explicit_override`` / ``env_price``
    #: / ``model_table`` / ``default_fallback``.
    price_source: str
    #: Estimated dollar cost of the injected overhead.
    overhead_dollars: float
    #: Per-operation breakdown, each with a ``dollars`` field, dollars-desc.
    by_operation: list[dict[str, Any]]
    #: Token-estimator identity behind the count, for auditability.
    estimator: str = "estimate_4_chars_per_token"


def summarize_trellis_cost(
    event_log: EventLog,
    *,
    days: int = 7,
    model: str | None = None,
    price_per_mtok: float | None = None,
) -> TrellisCostReport:
    """Price Trellis's injected-context overhead over the last *days*.

    Args:
        event_log: Event log to read ``TOKEN_TRACKED`` events from.
        days: History window.
        model: Consuming model for pricing (else ``TRELLIS_COST_MODEL`` /
            the default).
        price_per_mtok: Explicit input price override, USD/Mtok.

    Returns:
        A :class:`TrellisCostReport`.
    """
    usage = analyze_token_usage(event_log, days=days)
    resolved_model, price, source = resolve_pricing(model, price_per_mtok)

    by_operation = [
        {
            "operation": op["operation"],
            "layer": op["layer"],
            "calls": op["count"],
            "tokens": op["total_tokens"],
            "dollars": round(estimate_dollars(op["total_tokens"], price), 6),
        }
        for op in usage.by_operation
    ]

    return TrellisCostReport(
        period_days=days,
        overhead_events=usage.total_responses,
        overhead_tokens=usage.total_tokens,
        model=resolved_model,
        price_per_mtok=price,
        price_source=source,
        overhead_dollars=round(estimate_dollars(usage.total_tokens, price), 6),
        by_operation=by_operation,
    )
