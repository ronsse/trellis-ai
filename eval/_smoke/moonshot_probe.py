"""Moonshot/Kimi smoke probe — Phase A precondition check.

Validates that Moonshot's API:
1. Responds on the OpenAI-compatible surface for chat completions.
2. (Critical unknown) Responds on the OpenAI-compatible surface for embeddings.

Run via::

    op run --env-file=.env -- python -m eval._smoke.moonshot_probe

Cost ceiling: under $0.01 — a single short chat round-trip plus a single
short embedding round-trip on the smallest realistic inputs.

This script is intentionally narrow: no scenario harness, no registry,
no Trellis store wiring. It calls Moonshot directly via the OpenAI SDK
to isolate the "does the OpenAI-compat surface work for both surfaces"
question from any scaffolding we might build wrong.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any

# Endpoints — international (.ai) preferred for the user's setup
MOONSHOT_BASE_URL = os.environ.get(
    "MOONSHOT_BASE_URL", "https://api.moonshot.ai/v1"
)
CHAT_MODEL = os.environ.get("MOONSHOT_CHAT_MODEL", "kimi-k2-0905-preview")
# Embedding model name is the unknown — try this first, fall through to alternates
EMBEDDING_MODEL_CANDIDATES = [
    os.environ.get("MOONSHOT_EMBEDDING_MODEL", "moonshot-v1-embedding"),
    "moonshot-embedding-v1",
    "kimi-embedding",
]


def _present(s: str | None) -> str:
    """Boolean-only presence indicator for secret env vars.

    The earlier version of this helper returned ``f"len={len(s)} prefix=
    {s[:3]!r}"`` to help diagnose mis-pasted secrets, but CodeQL's
    Python taint analysis (``py/clear-text-logging-sensitive-data``)
    can't see that the sanitizer drops the value, so it flagged the
    callers as high-severity clear-text-logging vulns. The output is
    now boolean-only; if you need to verify which secret is actually
    loaded, run ``op item get <ItemName>`` directly rather than
    inspecting it through the probe.
    """
    return "set" if s else "<empty>"


async def probe_chat() -> dict[str, Any]:
    """Single small chat call. Validates auth + endpoint + model availability."""
    from openai import AsyncOpenAI  # noqa: PLC0415,I001 — deferred so the probe stays importable without the [llm-openai] extra installed

    api_key = os.environ.get("MOONSHOT_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "MOONSHOT_API_KEY not set"}

    client = AsyncOpenAI(api_key=api_key, base_url=MOONSHOT_BASE_URL)
    started = time.monotonic()
    try:
        resp = await client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[
                {"role": "system", "content": "You answer in one short sentence."},
                {"role": "user", "content": "Reply with exactly: 'pong'."},
            ],
            max_tokens=10,
            temperature=0.0,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    elapsed_ms = int((time.monotonic() - started) * 1000)
    content = resp.choices[0].message.content or ""
    usage = resp.usage
    return {
        "ok": True,
        "model": resp.model,
        "content_first_40": content[:40],
        "input_tokens": getattr(usage, "prompt_tokens", None),
        "output_tokens": getattr(usage, "completion_tokens", None),
        "latency_ms": elapsed_ms,
    }


async def probe_embedding(model: str) -> dict[str, Any]:
    """Single small embedding call. Validates the embeddings endpoint exists."""
    from openai import AsyncOpenAI  # noqa: PLC0415,I001 — deferred so the probe stays importable without the [llm-openai] extra installed

    api_key = os.environ.get("MOONSHOT_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "MOONSHOT_API_KEY not set"}

    client = AsyncOpenAI(api_key=api_key, base_url=MOONSHOT_BASE_URL)
    started = time.monotonic()
    try:
        resp = await client.embeddings.create(
            model=model,
            input=["The quick brown fox jumps over the lazy dog."],
        )
    except Exception as exc:
        return {
            "ok": False,
            "model_tried": model,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    elapsed_ms = int((time.monotonic() - started) * 1000)
    vec = resp.data[0].embedding
    return {
        "ok": True,
        "model": resp.model,
        "model_tried": model,
        "dimension": len(vec),
        "first_3_values": [round(float(x), 4) for x in vec[:3]],
        "latency_ms": elapsed_ms,
    }


async def probe_openai_embedding_fallback() -> dict[str, Any]:
    """OpenAI text-embedding-3-small fallback — only run if Moonshot embeddings fail."""
    from openai import AsyncOpenAI  # noqa: PLC0415,I001 — deferred so the probe stays importable without the [llm-openai] extra installed

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "OPENAI_API_KEY not set"}

    client = AsyncOpenAI(api_key=api_key)  # default base_url
    started = time.monotonic()
    try:
        resp = await client.embeddings.create(
            model="text-embedding-3-small",
            input=["The quick brown fox jumps over the lazy dog."],
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    elapsed_ms = int((time.monotonic() - started) * 1000)
    vec = resp.data[0].embedding
    return {
        "ok": True,
        "model": resp.model,
        "dimension": len(vec),
        "first_3_values": [round(float(x), 4) for x in vec[:3]],
        "latency_ms": elapsed_ms,
    }


async def main() -> int:
    print(f"Endpoint: {MOONSHOT_BASE_URL}")
    print(f"Chat model: {CHAT_MODEL}")
    print(f"MOONSHOT_API_KEY: {_present(os.environ.get('MOONSHOT_API_KEY'))}")
    print(f"OPENAI_API_KEY:   {_present(os.environ.get('OPENAI_API_KEY'))}")
    print()

    print("=== Probe 1: Moonshot chat completions ===")
    chat_result = await probe_chat()
    for k, v in chat_result.items():
        print(f"  {k}: {v}")
    print()

    print("=== Probe 2: Moonshot embeddings (trying candidates) ===")
    moonshot_embed_ok = False
    moonshot_embed_dim: int | None = None
    for candidate in EMBEDDING_MODEL_CANDIDATES:
        print(f"--- trying model: {candidate} ---")
        result = await probe_embedding(candidate)
        for k, v in result.items():
            print(f"  {k}: {v}")
        if result.get("ok"):
            moonshot_embed_ok = True
            moonshot_embed_dim = result.get("dimension")
            break
        print()

    if not moonshot_embed_ok:
        print()
        print("=== Probe 3: OpenAI text-embedding-3-small (FALLBACK) ===")
        fallback_result = await probe_openai_embedding_fallback()
        for k, v in fallback_result.items():
            print(f"  {k}: {v}")

    print()
    print("=== Summary ===")
    chat_status = "PASS" if chat_result.get("ok") else "FAIL"
    print(f"  Chat (Moonshot {CHAT_MODEL}): {chat_status}")
    if moonshot_embed_ok:
        print(
            f"  Embedding (Moonshot, dim={moonshot_embed_dim}): "
            "PASS — use Moonshot for both"
        )
        print(
            "  Decision: Phase A uses Moonshot for chat AND embeddings "
            "(single provider)."
        )
        return 0 if chat_result.get("ok") else 1
    fallback_ok = "fallback_result" in dir() and fallback_result.get("ok")
    print("  Embedding (Moonshot): FAIL")
    print(f"  Embedding (OpenAI fallback): {'PASS' if fallback_ok else 'FAIL'}")
    if fallback_ok:
        print(
            "  Decision: Phase A uses Moonshot for chat, OpenAI for "
            "embeddings (split provider)."
        )
        return 0 if chat_result.get("ok") else 1
    print(
        "  Decision: BOTH embedding paths failed. "
        "Need to investigate before Phase A."
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
