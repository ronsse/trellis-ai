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

#: USD per 1,000,000 **input** tokens. List prices verified against
#: ``platform.claude.com/docs/en/about-claude/pricing`` on **2026-09-04** —
#: re-derive rather than trusting this comment, pricing moves.
#:
#: Keyed by model *family*, with a concrete id resolving to the longest key
#: that appears in it (see :func:`_price_for_model`).  **A family is no longer
#: one price**, which is the trap this table fell into: ``claude-opus`` spans
#: $5 (Opus 4.5 through Opus 5) and $15 (the retired-but-still-served Opus 4.1
#: and Opus 4), and ``claude-sonnet`` spans $2 (Sonnet 5) and $3 (Sonnet 4.6
#: and earlier).  So the bare family key carries the **current generation's**
#: price — the one an unqualified "claude-opus" most likely means today — and
#: every still-served member that differs gets its own longer key.
#:
#: Keys are chosen so no key is a prefix of a *differently-priced* id: adding
#: a bare ``"claude-opus-4"`` would silently re-price ``claude-opus-4-5``
#: through ``4-8`` at the retired tier, because longest-key-wins only helps
#: for ids that have a key of their own.
_INPUT_PRICE_PER_MTOK: dict[str, float] = {
    # Anthropic — current generation prices on the family key.
    "claude-fable": 10.0,
    "claude-mythos": 10.0,
    "claude-opus": 5.0,
    "claude-sonnet": 2.0,
    "claude-haiku": 1.0,
    # Still-served members priced off their family.
    "claude-opus-4-1": 15.0,
    "claude-sonnet-4-6": 3.0,
    "claude-sonnet-4-5": 3.0,
    "claude-haiku-3-5": 0.80,
    # OpenAI.
    "gpt-4o-mini": 0.15,
    "gpt-4o": 2.5,
    # Self-hosted.
    "local": 0.0,
}

#: Default consuming model when none is configured — a mid-tier rate, so
#: the estimate is neither alarmist nor free.  This is also what an
#: *unrecognised* model id falls back to, which is a real assumption about a
#: deployment that may never have configured a Claude model at all.  It is
#: kept rather than replaced because the fallback is not silent:
#: :func:`resolve_pricing` returns ``source="default_fallback"``, and both
#: arms of ``trellis analyze cost`` render it, so the estimate says out loud
#: that nothing matched.  A local model prices correctly at ``--model local``.
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
