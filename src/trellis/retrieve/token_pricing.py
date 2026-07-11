"""Input-token pricing for estimating Trellis's cost overhead.

Trellis's contribution to an agent's bill is the context it *injects* —
retrieved packs, lessons, graph slices returned by the MCP / CLI / SDK
tools. Those tokens land in the agent's next prompt, so they are billed
at the consuming model's **input** rate. This module turns a measured
token count into a dollar estimate.

Two things are deliberately separated:

* **The measurement** (how many tokens Trellis injected) is exact — it
  comes from the ``TOKEN_TRACKED`` events every context tool emits.
* **The price** is an assumption the operator owns. List prices drift and
  the consuming model varies (Claude Code on Opus, a local Hermes at
  ~zero). The table below is a rough starting point; override per run
  with ``--price-per-mtok`` or globally with ``TRELLIS_COST_PRICE_PER_MTOK``
  / ``TRELLIS_COST_MODEL``.
"""

from __future__ import annotations

import os

#: USD per 1,000,000 input tokens. Rough list prices — update as pricing
#: changes or override at the call site. Keyed by model *family*; a
#: concrete id (``claude-opus-4-8``) resolves by substring to its family.
_INPUT_PRICE_PER_MTOK: dict[str, float] = {
    "claude-opus": 15.0,
    "claude-sonnet": 3.0,
    "claude-haiku": 1.0,
    "gpt-4o-mini": 0.15,
    "gpt-4o": 2.5,
    "local": 0.0,
}

#: Default consuming model when none is configured — a mid-tier rate, so
#: the estimate is neither alarmist nor free.
DEFAULT_MODEL = "claude-sonnet"

_MODEL_ENV = "TRELLIS_COST_MODEL"
_PRICE_ENV = "TRELLIS_COST_PRICE_PER_MTOK"


def _price_for_model(model: str) -> float | None:
    """Look up a model's input price by exact key then family substring."""
    key = model.strip().lower()
    if key in _INPUT_PRICE_PER_MTOK:
        return _INPUT_PRICE_PER_MTOK[key]
    # Longest family key that appears in the id wins (so "gpt-4o-mini"
    # beats "gpt-4o" for "gpt-4o-mini-2026").
    matches = sorted(
        (fam for fam in _INPUT_PRICE_PER_MTOK if fam in key),
        key=len,
        reverse=True,
    )
    return _INPUT_PRICE_PER_MTOK[matches[0]] if matches else None


def resolve_pricing(
    model: str | None = None,
    price_per_mtok: float | None = None,
) -> tuple[str, float, str]:
    """Resolve the ``(model_label, price_per_mtok, source)`` to use.

    Precedence: an explicit ``price_per_mtok`` override → the
    ``TRELLIS_COST_PRICE_PER_MTOK`` env price → the model's table price
    (from ``model`` or ``TRELLIS_COST_MODEL`` or :data:`DEFAULT_MODEL`).
    ``source`` is a short slug naming which of these won, so the estimate
    is auditable.
    """
    resolved_model = (model or os.environ.get(_MODEL_ENV) or DEFAULT_MODEL).strip()

    if price_per_mtok is not None:
        return resolved_model, float(price_per_mtok), "explicit_override"

    env_price = os.environ.get(_PRICE_ENV)
    if env_price:
        try:
            return resolved_model, float(env_price), "env_price"
        except ValueError:
            pass  # fall through to the table

    table_price = _price_for_model(resolved_model)
    if table_price is not None:
        return resolved_model, table_price, "model_table"

    return resolved_model, _INPUT_PRICE_PER_MTOK[DEFAULT_MODEL], "default_fallback"


def estimate_dollars(tokens: int, price_per_mtok: float) -> float:
    """Dollar cost of *tokens* input tokens at *price_per_mtok* USD/Mtok."""
    return tokens / 1_000_000 * price_per_mtok
