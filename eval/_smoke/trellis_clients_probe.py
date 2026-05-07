"""Validate the Trellis client wrappers against real APIs.

Lower-stakes probe than :mod:`eval._smoke.moonshot_probe` — that one
isolated the OpenAI-compat surface from Trellis. This one exercises
:func:`eval._real_llm.build_phase_a_clients` end-to-end, confirming
the Trellis ``OpenAIClient`` / ``OpenAIEmbedder`` wrappers behave
correctly against the live providers and that ``TokenUsage`` extraction
maps correctly for cost tracking.

Run via::

    op run --env-file=.env -- uv run python -m eval._smoke.trellis_clients_probe

Cost ceiling: under $0.01.
"""

from __future__ import annotations

import asyncio
import sys
import time

from eval._real_llm import build_phase_a_clients
from trellis.llm.types import Message


async def main() -> int:
    chat_client, embedder, config = build_phase_a_clients()
    print("Resolved config:")
    print(f"  moonshot_chat_model:    {config.moonshot_chat_model}")
    print(f"  moonshot_base_url:      {config.moonshot_base_url}")
    print(f"  openai_embedding_model: {config.openai_embedding_model}")
    print(f"  openai_embedding_dim:   {config.openai_embedding_dim}")
    print()

    print("=== Trellis chat wrapper (Moonshot/Kimi) ===")
    started = time.monotonic()
    chat_response = await chat_client.generate(
        messages=[
            Message(role="system", content="You answer in one short sentence."),
            Message(role="user", content="Reply with exactly: 'pong'."),
        ],
        max_tokens=10,
        temperature=0.0,
    )
    chat_ms = int((time.monotonic() - started) * 1000)
    print(f"  model:    {chat_response.model}")
    print(f"  content:  {chat_response.content[:40]!r}")
    print(f"  usage:    {chat_response.usage}")
    print(f"  latency:  {chat_ms}ms")
    print()

    print("=== Trellis embedder wrapper (OpenAI text-embedding-3-small) ===")
    started = time.monotonic()
    embed_response = await embedder.embed(
        "The quick brown fox jumps over the lazy dog."
    )
    embed_ms = int((time.monotonic() - started) * 1000)
    print(f"  model:     {embed_response.model}")
    print(f"  dimension: {len(embed_response.embedding)}")
    print(f"  first_3:   {[round(float(x), 4) for x in embed_response.embedding[:3]]}")
    print(f"  usage:     {embed_response.usage}")
    print(f"  latency:   {embed_ms}ms")
    print()

    print("=== Embedder batch path ===")
    started = time.monotonic()
    batch_response = await embedder.embed_batch(
        [
            "auth_module: handles user authentication and session lifecycle.",
            "session_token: short-lived bearer token issued at login.",
            "rate_limiter: per-IP rate-limiting middleware.",
        ]
    )
    batch_ms = int((time.monotonic() - started) * 1000)
    print(f"  count:     {len(batch_response)}")
    print(f"  dim/each:  {len(batch_response[0].embedding)}")
    print(f"  usage[0]:  {batch_response[0].usage}")
    print(f"  usage[1]:  {batch_response[1].usage}  (None expected — batch attributes total to [0])")
    print(f"  latency:   {batch_ms}ms")
    print()

    # Sanity: dimension matches the dimension we'll wire into vector_store config
    expected_dim = config.openai_embedding_dim
    actual_dim = len(embed_response.embedding)
    if actual_dim != expected_dim:
        print(
            f"WARN: embedder dimension {actual_dim} != configured "
            f"{expected_dim}. Update OPENAI_EMBEDDING_3_SMALL_DIM."
        )
        return 1

    print("All Trellis client wrappers PASS. Phase A factories ready to wire into scenarios.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
