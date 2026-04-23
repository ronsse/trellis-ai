"""Pluggable token counting for pack budget enforcement.

The default :func:`trellis.core.hashing.estimate_tokens` is a 4-chars-per-token
heuristic that drifts 5-20% from real tokenizers depending on content
(code, JSON, URL-heavy text). Closing Gap 3.1 means letting callers plug
in an accurate counter (tiktoken, anthropic tokenizer, etc.) and giving
PackBuilder two controls to tame boundary error:

* ``TokenCounter`` — the Protocol implementations conform to. A name
  identifies the counter in telemetry so downstream analysis can
  attribute drift to a specific tokenizer.
* ``safety_margin`` — fractional headroom subtracted from ``max_tokens``
  before the greedy pack, protecting against under-counting estimators
  that would otherwise overflow a real context window.

Core ships no third-party tokenizer. Adapters live in caller code and
plug in via the Protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from trellis.core.hashing import estimate_tokens as _estimate_tokens


@runtime_checkable
class TokenCounter(Protocol):
    """Callable that maps text to a token count.

    Implementations expose a stable ``name`` so telemetry can attribute
    boundary errors to the specific tokenizer in use. ``name`` is declared
    as a read-only property in the Protocol so that implementations may
    supply it as a plain attribute, a dataclass field, or a property
    without tripping Protocol variance.
    """

    @property
    def name(self) -> str:
        """Stable identifier for this counter (e.g., ``"tiktoken_cl100k"``)."""
        ...

    def count(self, text: str) -> int:
        """Return the token count for ``text``. Must never raise."""
        ...


@dataclass(frozen=True)
class HeuristicTokenCounter:
    """Default ~4-chars-per-token estimator.

    Matches the pre-existing :func:`estimate_tokens` behavior exactly so
    callers who do not configure a counter see no behavior change.
    """

    name: str = "heuristic_4cpt"

    def count(self, text: str) -> int:
        return _estimate_tokens(text)


#: Shared singleton for the default heuristic. Avoids instantiating a
#: dataclass on every budget call.
DEFAULT_TOKEN_COUNTER = HeuristicTokenCounter()
