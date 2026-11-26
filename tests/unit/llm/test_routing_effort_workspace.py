"""Per-tier ``reasoning_effort`` and the credential-bound ``workspace_id_env``."""

from __future__ import annotations

from typing import Any

import pytest

from trellis.llm.routing import (
    LLMConsumer,
    LLMRoutingError,
    resolve_llm_route,
)

_PARENT: dict[str, Any] = {
    "provider": "anthropic",
    "api_key_env": "PARENT_KEY",
    "model": "parent-model",
    "reasoning_effort": "high",
}


def _route(parent: dict[str, Any], tier: dict[str, Any]) -> Any:
    config = {**parent, "tiers": {"t": tier}, "routes": {"session_capture": "t"}}
    return resolve_llm_route(config, LLMConsumer.SESSION_CAPTURE)


def _raises(parent: dict[str, Any], tier: dict[str, Any]) -> LLMRoutingError:
    with pytest.raises(LLMRoutingError) as exc_info:
        _route(parent, tier)
    return exc_info.value


class TestTierReasoningEffort:
    """Effort is bound to the model, so a tier never inherits the parent's."""

    def test_a_model_only_tier_gets_no_effort_from_the_parent(self):
        route = _route(_PARENT, {"model": "other"})
        assert route.reasoning_effort is None
        assert "reasoning_effort" not in route.block

    def test_a_tier_that_keeps_the_whole_block_still_gets_none(self):
        route = _route(_PARENT, {})
        assert route.reasoning_effort is None

    def test_a_tier_that_moves_the_endpoint_gets_none(self):
        route = _route(
            _PARENT,
            {"provider": "openai", "base_url": None, "api_key_env": "OTHER_KEY"},
        )
        assert route.reasoning_effort is None
        assert "reasoning_effort" not in route.block

    def test_a_model_only_tier_with_the_key_gets_its_own(self):
        route = _route(_PARENT, {"model": "other", "reasoning_effort": "low"})
        assert route.reasoning_effort == "low"

    def test_a_tier_sets_its_own_effort(self):
        route = _route(
            _PARENT,
            {"provider": "openai", "base_url": None, "api_key_env": "K",
             "reasoning_effort": "low"},
        )  # fmt: skip
        assert route.reasoning_effort == "low"

    def test_explicit_null_is_an_override(self):
        route = _route(_PARENT, {"reasoning_effort": None})
        assert route.block["reasoning_effort"] is None
        assert route.reasoning_effort is None

    def test_a_non_string_effort_on_a_tier_is_refused(self):
        error = _raises(_PARENT, {"reasoning_effort": 3})
        assert error.setting == "llm.tiers.t.reasoning_effort"

    def test_unrouted_consumer_keeps_the_parent_effort(self):
        route = resolve_llm_route(_PARENT, LLMConsumer.SESSION_CAPTURE)
        assert route.reasoning_effort == "high"

    def test_describe_shows_effort(self):
        route = resolve_llm_route(_PARENT, LLMConsumer.SESSION_CAPTURE)
        assert route.describe(environ={})["reasoning_effort"] == "high"
        tier = _route(_PARENT, {"reasoning_effort": "low"})
        assert tier.describe(environ={})["reasoning_effort"] == "low"


class TestWorkspaceIdEnv:
    def test_a_tier_with_its_own_credential_sets_its_own_workspace(self):
        route = _route(
            {"provider": "openai", "api_key_env": "LOCAL_KEY", "base_url": "http://x/v1"},
            {"provider": "anthropic", "base_url": None, "api_key_env": "ORG_KEY",
             "workspace_id_env": "ORG_WORKSPACE"},
        )  # fmt: skip
        assert route.workspace_id_env == "ORG_WORKSPACE"
        described = route.describe(environ={"ORG_WORKSPACE": "wrkspc_fake_value"})
        assert described["workspace_id_env"] == "ORG_WORKSPACE"
        assert "wrkspc_fake_value" not in repr(described)

    def test_inherited_with_the_parents_credential(self):
        parent = {**_PARENT, "workspace_id_env": "PARENT_WORKSPACE"}
        assert _route(parent, {"model": "x"}).workspace_id_env == "PARENT_WORKSPACE"

    def test_a_tier_naming_its_own_credential_inherits_no_workspace(self):
        parent = {**_PARENT, "workspace_id_env": "PARENT_WORKSPACE"}
        route = _route(parent, {"api_key_env": "OTHER_KEY"})
        assert route.workspace_id_env is None

    def test_a_tier_workspace_without_a_credential_is_refused(self):
        error = _raises(_PARENT, {"workspace_id_env": "W"})
        assert error.setting == "llm.tiers.t.workspace_id_env"
        assert "credential" in error.message

    def test_refused_on_a_non_anthropic_tier(self):
        error = _raises(
            _PARENT,
            {"provider": "openai", "base_url": None, "api_key_env": "K",
             "workspace_id_env": "W"},
        )  # fmt: skip
        assert error.setting == "llm.tiers.t.workspace_id_env"

    def test_refused_on_a_non_anthropic_parent(self):
        config = {"provider": "openai", "api_key_env": "K", "workspace_id_env": "W"}
        with pytest.raises(LLMRoutingError) as exc_info:
            resolve_llm_route(config, None)
        assert exc_info.value.setting == "llm.workspace_id_env"

    def test_an_empty_name_is_refused(self):
        config = {**_PARENT, "workspace_id_env": "  "}
        with pytest.raises(LLMRoutingError):
            resolve_llm_route(config, None)

    def test_absent_by_default(self):
        route = resolve_llm_route(_PARENT, None)
        assert route.workspace_id_env is None
        assert route.describe(environ={})["workspace_id_env"] is None
