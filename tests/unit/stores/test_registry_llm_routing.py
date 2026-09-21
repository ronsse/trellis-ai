"""``StoreRegistry.build_llm_client`` under ``llm.tiers`` / ``llm.routes``.

The load-bearing property is the first class: an ``llm:`` block without
routes builds, for every consumer, exactly the client it built before
tiers existed. Constructor kwargs are recorded rather than a real client
built, because the provider SDKs are optional extras.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml
from structlog.testing import capture_logs

from trellis.llm.routing import LLMConsumer, LLMRoutingError
from trellis.stores.registry import StoreRegistry

_PARENT: dict[str, Any] = {
    "provider": "openai",
    "base_url": "http://localhost:11434/v1",
    "api_key_env": "TRELLIS_TEST_PARENT_KEY",
    "model": "hermes3:8b",
}

_DEEP: dict[str, Any] = {
    "base_url": "http://localhost:4000/v1",
    "api_key_env": "TRELLIS_TEST_TIER_KEY",
    "model": "deep",
}


class _RecordingClient:
    """Stands in for ``OpenAIClient``; records what it was built with."""

    calls: ClassVar[list[dict[str, Any]]]

    def __init__(self, **kwargs: Any) -> None:
        type(self).calls.append(kwargs)
        self.kwargs = kwargs


@pytest.fixture
def recorder(monkeypatch):
    class Recorder(_RecordingClient):
        calls: ClassVar[list[dict[str, Any]]] = []

    monkeypatch.setattr("trellis.llm.providers.openai.OpenAIClient", Recorder)
    return Recorder


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("TRELLIS_TEST_PARENT_KEY", "sk-parent-0001")
    monkeypatch.setenv("TRELLIS_TEST_TIER_KEY", "sk-tier-0002")


def _registry(tmp_path: Path, llm_block: dict[str, Any]) -> StoreRegistry:
    config_dir = tmp_path / ".trellis"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({"stores": {}, "llm": llm_block})
    )
    return StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )


_PARENT_KWARGS = {
    "api_key": "sk-parent-0001",
    "base_url": "http://localhost:11434/v1",
    "default_model": "hermes3:8b",
}


class TestUnchangedConfigBuildsUnchangedClients:
    @pytest.mark.parametrize(
        "llm_block",
        [_PARENT, {**_PARENT, "tiers": {"deep": _DEEP}}],
        ids=["no-tiers", "tiers-without-routes"],
    )
    def test_every_consumer_builds_the_no_argument_client(
        self, tmp_path, recorder, llm_block
    ):
        registry = _registry(tmp_path, llm_block)
        registry.build_llm_client()
        for consumer in LLMConsumer:
            registry.build_llm_client(consumer=consumer)
        assert len(recorder.calls) == 1 + len(LLMConsumer)
        assert all(call == _PARENT_KWARGS for call in recorder.calls)


class TestRoutedConsumers:
    def test_a_routed_consumer_builds_from_its_tier_and_the_rest_do_not(
        self, tmp_path, recorder
    ):
        registry = _registry(
            tmp_path,
            {**_PARENT, "tiers": {"deep": _DEEP}, "routes": {"reconcile": "deep"}},
        )
        registry.build_llm_client(consumer=LLMConsumer.RECONCILE)
        registry.build_llm_client(consumer=LLMConsumer.ENRICHMENT)
        assert recorder.calls == [
            {
                "api_key": "sk-tier-0002",
                "base_url": "http://localhost:4000/v1",
                "default_model": "deep",
            },
            _PARENT_KWARGS,
        ]

    def test_an_unbuildable_tier_never_falls_back_to_the_parent(
        self, tmp_path, recorder, monkeypatch
    ):
        """The parent key is set and would build; the routed consumer gets None."""
        monkeypatch.delenv("TRELLIS_TEST_TIER_KEY")
        registry = _registry(
            tmp_path,
            {**_PARENT, "tiers": {"deep": _DEEP}, "routes": {"reconcile": "deep"}},
        )
        assert registry.build_llm_client(consumer=LLMConsumer.RECONCILE) is None
        assert recorder.calls == []
        assert registry.build_llm_client(consumer=LLMConsumer.ENRICHMENT) is not None

    def test_the_plugin_path_receives_the_tier_values(self, tmp_path, monkeypatch):
        seen: list[dict[str, Any]] = []
        sentinel = object()

        def _plugin(**kwargs: Any) -> object:
            seen.append(kwargs)
            return sentinel

        monkeypatch.setattr("trellis.stores.registry._try_llm_provider_plugin", _plugin)
        registry = _registry(
            tmp_path,
            {
                **_PARENT,
                "tiers": {
                    "local": {
                        "provider": "vllm-native",
                        "base_url": "http://omen:8000/v1",
                        "api_key_env": "TRELLIS_TEST_TIER_KEY",
                        "model": "qwen",
                    }
                },
                "routes": {"enrichment": "local"},
            },
        )
        assert registry.build_llm_client(consumer=LLMConsumer.ENRICHMENT) is sentinel
        assert seen == [
            {
                "provider": "vllm-native",
                "api_key": "sk-tier-0002",
                "base_url": "http://omen:8000/v1",
                "model": "qwen",
            }
        ]


class TestMalformedRouting:
    def test_it_raises_at_use_not_at_registry_construction(self, tmp_path, recorder):
        """A bad ``routes`` entry must not take down every store with it."""
        registry = _registry(tmp_path, {**_PARENT, "routes": {"reconcile": "deep"}})
        with pytest.raises(LLMRoutingError) as exc_info:
            registry.build_llm_client(consumer=LLMConsumer.ENRICHMENT)
        assert exc_info.value.setting == "llm.routes.reconcile"
        with pytest.raises(LLMRoutingError):
            registry.llm_route()
        assert recorder.calls == []


class TestBuildLog:
    def test_it_names_the_consumer_and_tier_and_masks_the_key(self, tmp_path, recorder):
        registry = _registry(
            tmp_path,
            {**_PARENT, "tiers": {"deep": _DEEP}, "routes": {"reconcile": "deep"}},
        )
        with capture_logs() as logs:
            registry.build_llm_client(consumer=LLMConsumer.RECONCILE)
        built = [entry for entry in logs if entry["event"] == "llm_client_built"]
        assert len(built) == 1
        assert built[0]["consumer"] == "reconcile"
        assert built[0]["tier"] == "deep"
        assert built[0]["model"] == "deep"
        assert built[0]["masked_key"] == "***0002"
        assert "sk-tier-0002" not in repr(logs)
