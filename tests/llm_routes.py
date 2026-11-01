"""Per-consumer ``llm:`` configs for tests that must tell the consumers apart.

A test that sends every consumer through one block cannot notice a gate that
reads the wrong consumer's route. ``per_consumer_config`` routes the consumer
under test to one tier and every other consumer to the opposite one, so a gate
that reads any other consumer's route answers the opposite way.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from trellis.llm.routing import LLMConsumer, LLMRoute, resolve_llm_route


def per_consumer_config(consumer: LLMConsumer, *, local: bool) -> dict[str, Any]:
    """An ``llm:`` block where only *consumer*'s route is (not) declared local."""
    return {
        "provider": "openai",
        "base_url": "http://hosted.example/v1",
        "api_key_env": "HOSTED_API_KEY",
        "model": "m",
        "tiers": {
            "onprem": {
                "base_url": "http://onprem.example/v1",
                "api_key_env": "ONPREM_API_KEY",
                "local": True,
            },
            "hosted": {},
        },
        "routes": {
            member.value: "onprem" if (member is consumer) == local else "hosted"
            for member in LLMConsumer
        },
    }


def route_from(config: Mapping[str, Any]) -> Callable[..., LLMRoute]:
    """A ``registry.llm_route`` that resolves from *config*."""

    def _route(consumer: LLMConsumer | None = None) -> LLMRoute:
        return resolve_llm_route(config, consumer)

    return _route


class TierRoutedRegistry:
    """A real registry whose ``llm_route`` resolves from *config* instead."""

    def __init__(self, inner: Any, config: Mapping[str, Any]) -> None:
        self._inner = inner
        self._config = config

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def llm_route(self, consumer: LLMConsumer | None = None) -> LLMRoute:
        return resolve_llm_route(self._config, consumer)
