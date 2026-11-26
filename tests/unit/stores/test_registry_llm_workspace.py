"""The registry builds tier effort and the Anthropic workspace header into clients."""

from __future__ import annotations

from typing import Any

import pytest

from trellis.errors import ConfigError
from trellis.stores.registry import StoreRegistry

_HEADER = "anthropic-workspace-id"

_LOCAL_PARENT: dict[str, Any] = {
    "provider": "openai",
    "base_url": "http://localhost:11434/v1",
    "api_key": "sk-literal-LOCAL",
    "model": "small-local",
}


def _capture_tier(**extra: Any) -> dict[str, Any]:
    tier = {
        "provider": "anthropic",
        "base_url": None,
        "api_key_env": "FAKE_ORG_KEY",
        "model": "fake-model",
        **extra,
    }
    return {**_LOCAL_PARENT, "tiers": {"cj": tier}, "routes": {"session_capture": "cj"}}


def _build(config: dict[str, Any], consumer: str | None = "session_capture") -> Any:
    return StoreRegistry(llm_config=config).build_llm_client(consumer=consumer)


def test_default_anthropic_client_sends_no_workspace_header() -> None:
    pytest.importorskip("anthropic")
    client = _build({"provider": "anthropic", "api_key": "sk-literal-XYZ9"}, None)
    assert client is not None
    assert _HEADER not in client._client.default_headers
    assert client._reasoning_effort is None


def test_workspace_header_is_sent_when_the_env_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("anthropic")
    monkeypatch.setenv("FAKE_ORG_KEY", "sk-fake-org")
    monkeypatch.setenv("FAKE_WORKSPACE", "wrkspc_fake_0001")
    client = _build(
        _capture_tier(workspace_id_env="FAKE_WORKSPACE", reasoning_effort="high")
    )
    assert client is not None
    assert client._client.default_headers[_HEADER] == "wrkspc_fake_0001"
    assert client._reasoning_effort == "high"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_named_but_unset_workspace_env_fails_closed(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    pytest.importorskip("anthropic")
    monkeypatch.setenv("FAKE_ORG_KEY", "sk-fake-org")
    if value is None:
        monkeypatch.delenv("FAKE_WORKSPACE", raising=False)
    else:
        monkeypatch.setenv("FAKE_WORKSPACE", value)
    with pytest.raises(ConfigError) as exc_info:
        _build(_capture_tier(workspace_id_env="FAKE_WORKSPACE"))
    assert exc_info.value.setting == "llm.tiers.cj.workspace_id_env"
    assert "FAKE_WORKSPACE" in exc_info.value.message


def test_other_consumers_keep_the_parent_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the routed consumer sees the tier; the rest build the parent block."""
    pytest.importorskip("openai")
    monkeypatch.delenv("FAKE_WORKSPACE", raising=False)
    client = _build(_capture_tier(workspace_id_env="FAKE_WORKSPACE"), "reconcile")
    from trellis.llm.providers.openai import OpenAIClient

    assert isinstance(client, OpenAIClient)
    assert client._reasoning_effort is None  # type: ignore[attr-defined]


def test_tier_effort_reaches_a_routed_client_that_keeps_the_endpoint() -> None:
    pytest.importorskip("openai")
    config = {
        **_LOCAL_PARENT,
        "reasoning_effort": "medium",
        "tiers": {"same": {"model": "bigger-local"}},
        "routes": {"session_capture": "same"},
    }
    client = _build(config)
    assert client is not None
    assert client._reasoning_effort == "medium"  # type: ignore[attr-defined]


def test_tier_effort_is_not_sent_to_a_moved_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("anthropic")
    monkeypatch.setenv("FAKE_ORG_KEY", "sk-fake-org")
    config = {**_capture_tier(), "reasoning_effort": "medium"}
    client = _build(config)
    assert client is not None
    assert client._reasoning_effort is None
    assert _HEADER not in client._client.default_headers
