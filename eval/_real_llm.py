"""Phase A real-LLM provider factories.

Builds split-provider clients per [`docs/design/plan-real-corpus-eval.md`](
../docs/design/plan-real-corpus-eval.md) §5.1 — Moonshot/Kimi for chat,
OpenAI for embeddings. The split was forced by the
[`eval/_smoke/moonshot_probe.py`](_smoke/moonshot_probe.py) verdict:
Moonshot's international (.ai) endpoint returns 403
``permission_denied_error`` on its embeddings surface for all observed
candidate model names, while OpenAI's ``text-embedding-3-small`` works
unmodified.

Reused without modification:
- :class:`trellis.llm.providers.openai.OpenAIClient` for chat (Moonshot
  is OpenAI-compatible — we just override ``base_url``).
- :class:`trellis.llm.providers.openai.OpenAIEmbedder` for embeddings
  (default ``base_url`` — OpenAI proper).

This module is intentionally thin. It does not construct a
``StoreRegistry``; callers compose the clients with whatever registry
they build elsewhere. Keeping it independent of the registry config
shape avoids coupling Phase A wiring to the YAML / env-var resolution
machinery in :func:`StoreRegistry.build_llm_client` /
:func:`StoreRegistry.build_embedder_client` — those still work, but
the eval scenarios construct registries programmatically and prefer
direct factory calls.

Environment variables expected (typically injected via
``op run --env-file=.env -- ...``):

- ``MOONSHOT_API_KEY`` — required for chat.
- ``OPENAI_API_KEY`` — required for embeddings.
- ``MOONSHOT_BASE_URL`` — optional override (default
  ``https://api.moonshot.ai/v1``).
- ``MOONSHOT_CHAT_MODEL`` — optional override (default
  ``kimi-k2-0905-preview``).
- ``OPENAI_EMBEDDING_MODEL`` — optional override (default
  ``text-embedding-3-small``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from trellis.llm.protocol import EmbedderClient, LLMClient

# Defaults aligned with the probe verdict (§5.1 of plan-real-corpus-eval.md)
DEFAULT_MOONSHOT_BASE_URL = "https://api.moonshot.ai/v1"
DEFAULT_MOONSHOT_CHAT_MODEL = "kimi-k2-0905-preview"
DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
# OpenAI text-embedding-3-small native dimension. Registries that wire
# this embedder must align ``embedding_dim`` accordingly.
OPENAI_EMBEDDING_3_SMALL_DIM = 1536


class RealLLMConfigError(RuntimeError):
    """Raised when a real-LLM factory can't construct a client.

    Distinct from :class:`ModuleNotFoundError` (SDK missing) so callers
    can surface "set the API key" guidance separately from "install
    the extras".
    """


@dataclass(frozen=True)
class RealLLMConfig:
    """Resolved configuration for a Phase A run.

    Carries the resolved model names + endpoint info so scenarios can
    log them in their reports without re-reading env vars. Immutable so
    a single config flows through scenario setup, telemetry, and
    report-writing without any path mutating it mid-run.
    """

    moonshot_chat_model: str
    moonshot_base_url: str
    openai_embedding_model: str
    openai_embedding_dim: int


def resolve_config() -> RealLLMConfig:
    """Resolve the Phase A config from environment variables.

    Pure read — no API calls. Defaults track the probe verdict.
    """
    return RealLLMConfig(
        moonshot_chat_model=os.environ.get(
            "MOONSHOT_CHAT_MODEL", DEFAULT_MOONSHOT_CHAT_MODEL
        ),
        moonshot_base_url=os.environ.get(
            "MOONSHOT_BASE_URL", DEFAULT_MOONSHOT_BASE_URL
        ),
        openai_embedding_model=os.environ.get(
            "OPENAI_EMBEDDING_MODEL", DEFAULT_OPENAI_EMBEDDING_MODEL
        ),
        openai_embedding_dim=OPENAI_EMBEDDING_3_SMALL_DIM,
    )


def build_moonshot_chat_client(config: RealLLMConfig | None = None) -> LLMClient:
    """Build an :class:`LLMClient` pointed at Moonshot's OpenAI-compat surface.

    Wraps :class:`OpenAIClient` with ``base_url`` overridden to Moonshot's
    endpoint and ``api_key`` sourced from ``MOONSHOT_API_KEY``.
    """
    cfg = config or resolve_config()
    api_key = os.environ.get("MOONSHOT_API_KEY")
    if not api_key:
        msg = (
            "MOONSHOT_API_KEY not set. Run via "
            "`op run --env-file=.env -- ...` so the secret reference resolves."
        )
        raise RealLLMConfigError(msg)

    try:
        from trellis.llm.providers.openai import OpenAIClient  # noqa: PLC0415
    except ModuleNotFoundError as exc:  # pragma: no cover — install guard
        msg = (
            "openai SDK not installed. "
            "Run: uv pip install -e '.[llm-openai]'"
        )
        raise ModuleNotFoundError(msg) from exc

    return OpenAIClient(
        api_key=api_key,
        base_url=cfg.moonshot_base_url,
        default_model=cfg.moonshot_chat_model,
    )


def build_openai_embedder(config: RealLLMConfig | None = None) -> EmbedderClient:
    """Build an :class:`EmbedderClient` pointed at OpenAI's embedding endpoint.

    Wraps :class:`OpenAIEmbedder` with default ``base_url`` (i.e.,
    ``https://api.openai.com/v1``) and ``api_key`` sourced from
    ``OPENAI_API_KEY``. Default model: ``text-embedding-3-small``
    (1536-dim).
    """
    cfg = config or resolve_config()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        msg = (
            "OPENAI_API_KEY not set. Run via "
            "`op run --env-file=.env -- ...` so the secret reference resolves."
        )
        raise RealLLMConfigError(msg)

    try:
        from trellis.llm.providers.openai import OpenAIEmbedder  # noqa: PLC0415
    except ModuleNotFoundError as exc:  # pragma: no cover — install guard
        msg = (
            "openai SDK not installed. "
            "Run: uv pip install -e '.[llm-openai]'"
        )
        raise ModuleNotFoundError(msg) from exc

    return OpenAIEmbedder(
        api_key=api_key,
        base_url=None,  # default OpenAI endpoint
        default_model=cfg.openai_embedding_model,
    )


def build_phase_a_clients() -> tuple[LLMClient, EmbedderClient, RealLLMConfig]:
    """Convenience: build both clients + return the resolved config.

    Returns ``(chat_client, embedder, config)``. Callers attach the
    clients to their registry / scenario directly.
    """
    config = resolve_config()
    chat = build_moonshot_chat_client(config)
    embed = build_openai_embedder(config)
    return chat, embed, config
