"""Tests for :mod:`trellis.llm.routing` — named tiers and per-consumer routes."""

from __future__ import annotations

import json
from typing import Any

import pytest

from trellis.errors import ConfigError
from trellis.llm.routing import (
    LLM_BLOCK_KEYS,
    LLMConsumer,
    LLMRoutingError,
    resolve_llm_route,
    validate_llm_routing,
)
from trellis.stores.registry import _resolve_api_key

_PARENT: dict[str, Any] = {
    "provider": "openai",
    "base_url": "http://localhost:11434/v1",
    "api_key_env": "OLLAMA_API_KEY",
    "model": "hermes3:8b",
}

_DEEP = {
    "base_url": "http://localhost:4000/v1",
    "api_key_env": "LITELLM_API_KEY",
    "model": "deep",
}


def _config(**sections: Any) -> dict[str, Any]:
    return {**_PARENT, **sections}


def _raises(config: dict[str, Any], consumer: Any = None) -> LLMRoutingError:
    with pytest.raises(LLMRoutingError) as exc_info:
        resolve_llm_route(config, consumer)
    return exc_info.value


class TestUnroutedConsumers:
    """An unrouted consumer builds from the parent block exactly."""

    @pytest.mark.parametrize("consumer", [None, *LLMConsumer])
    def test_no_routing_block_gives_every_consumer_the_parent(self, consumer):
        route = resolve_llm_route(_PARENT, consumer)
        assert dict(route.block) == _PARENT
        assert route.tier is None
        assert route.routed is False

    @pytest.mark.parametrize("consumer", list(LLMConsumer))
    def test_tiers_without_routes_change_nothing(self, consumer):
        config = _config(tiers={"deep": _DEEP})
        assert dict(resolve_llm_route(config, consumer).block) == config

    def test_a_route_for_one_consumer_leaves_the_others_on_the_parent(self):
        config = _config(tiers={"deep": _DEEP}, routes={"reconcile": "deep"})
        route = resolve_llm_route(config, LLMConsumer.ENRICHMENT)
        assert route.tier is None
        assert route.model == "hermes3:8b"

    @pytest.mark.parametrize("config", [None, {}])
    def test_an_absent_llm_block_resolves_to_an_empty_parent(self, config):
        route = resolve_llm_route(config, LLMConsumer.RECONCILE)
        assert dict(route.block) == {}
        assert route.provider is None

    def test_commented_out_sections_parse_as_null_and_mean_none(self):
        config = _config(tiers=None, routes=None)
        route = resolve_llm_route(config, LLMConsumer.RECONCILE)
        assert route.tier is None

    def test_the_consumer_may_be_named_by_its_route_key(self):
        config = _config(tiers={"deep": _DEEP}, routes={"reconcile": "deep"})
        route = resolve_llm_route(config, "reconcile")
        assert route.consumer is LLMConsumer.RECONCILE
        assert route.tier == "deep"

    def test_an_unknown_consumer_name_is_a_code_defect_not_a_config_one(self):
        with pytest.raises(ValueError, match="not_a_consumer") as exc_info:
            resolve_llm_route(_PARENT, "not_a_consumer")
        assert not isinstance(exc_info.value, ConfigError)


class TestTierInheritance:
    def test_a_routed_consumer_takes_the_tier_fields_it_sets(self):
        config = _config(tiers={"deep": _DEEP}, routes={"reconcile": "deep"})
        route = resolve_llm_route(config, LLMConsumer.RECONCILE)
        assert route.routed is True
        assert route.tier == "deep"
        assert dict(route.block) == {
            "provider": "openai",
            "base_url": "http://localhost:4000/v1",
            "model": "deep",
            "api_key": None,
            "api_key_env": "LITELLM_API_KEY",
        }

    def test_omitted_fields_inherit_from_the_parent_including_model(self):
        config = _config(
            tiers={"cheap": {"model": "llama3.2:3b"}, "same": {}},
            routes={"enrichment": "cheap", "reconcile": "same"},
        )
        cheap = resolve_llm_route(config, LLMConsumer.ENRICHMENT)
        assert cheap.model == "llama3.2:3b"
        assert cheap.provider == "openai"
        assert cheap.base_url == "http://localhost:11434/v1"
        same = resolve_llm_route(config, LLMConsumer.RECONCILE)
        assert same.routed is True
        assert same.model == "hermes3:8b"

    def test_an_explicit_null_is_an_override_not_an_omission(self):
        config = _config(tiers={"t": {"model": None}}, routes={"reconcile": "t"})
        assert resolve_llm_route(config, LLMConsumer.RECONCILE).model is None

    def test_the_empty_tier_aliases_the_parent_field_for_field(self):
        config = _config(tiers={"alias": {}}, routes={"reconcile": "alias"})
        route = resolve_llm_route(config, LLMConsumer.RECONCILE)
        assert {k: v for k, v in route.block.items() if v is not None} == _PARENT

    def test_credentials_inherit_as_one_unit(self):
        """A tier naming ``api_key_env`` must not pick up the parent's literal."""
        parent = {"provider": "openai", "api_key": "sk-parent-literal"}
        config = {
            **parent,
            "tiers": {"t": {"api_key_env": "TIER_KEY"}},
            "routes": {"reconcile": "t"},
        }
        route = resolve_llm_route(config, LLMConsumer.RECONCILE)
        assert route.block["api_key_env"] == "TIER_KEY"
        assert route.block["api_key"] is None

    def test_a_tier_without_credentials_inherits_both_parent_keys(self):
        parent = {"provider": "openai", "api_key_env": "K", "api_key": "sk-lit"}
        config = {
            **parent,
            "tiers": {"t": {"model": "m"}},
            "routes": {"reconcile": "t"},
        }
        route = resolve_llm_route(config, LLMConsumer.RECONCILE)
        assert route.block["api_key_env"] == "K"
        assert route.block["api_key"] == "sk-lit"

    def test_repeating_the_parent_endpoint_is_not_a_move(self):
        config = _config(
            tiers={"t": {"base_url": _PARENT["base_url"], "model": "m"}},
            routes={"reconcile": "t"},
        )
        route = resolve_llm_route(config, LLMConsumer.RECONCILE)
        assert route.block["api_key_env"] == "OLLAMA_API_KEY"

    def test_a_provider_change_may_set_base_url_null_for_the_default(self):
        config = _config(
            tiers={
                "claude": {
                    "provider": "anthropic",
                    "base_url": None,
                    "api_key_env": "ANTHROPIC_API_KEY",
                }
            },
            routes={"reconcile": "claude"},
        )
        route = resolve_llm_route(config, LLMConsumer.RECONCILE)
        assert route.provider == "anthropic"
        assert route.base_url is None

    def test_the_route_block_is_read_only(self):
        route = resolve_llm_route(_PARENT, LLMConsumer.RECONCILE)
        with pytest.raises(TypeError):
            route.block["model"] = "mutated"  # type: ignore[index]


class TestValidation:
    """Every defect raises, for every consumer, naming the YAML path to edit."""

    @pytest.mark.parametrize(
        ("sections", "setting", "fragment"),
        [
            ({"tiers": ["deep"]}, "llm.tiers", "must be a mapping"),
            ({"routes": "deep"}, "llm.routes", "must be a mapping"),
            ({"tiers": {1: {}}}, "llm.tiers", "non-empty strings"),
            ({"tiers": {"": {}}}, "llm.tiers", "non-empty strings"),
            ({"tiers": {"deep": None}}, "llm.tiers.deep", "empty (null)"),
            ({"tiers": {"deep": "gpt-5"}}, "llm.tiers.deep", "got str"),
            (
                {"tiers": {"deep": {"modle": "x"}}},
                "llm.tiers.deep",
                "unknown key(s) ['modle']",
            ),
            (
                {"tiers": {"deep": {"model": 5}}},
                "llm.tiers.deep.model",
                "string or null",
            ),
            (
                {"tiers": {"deep": {"provider": ""}}},
                "llm.tiers.deep.provider",
                "empty provider",
            ),
            (
                {"tiers": {"deep": {"api_key_env": ""}}},
                "llm.tiers.deep",
                "without a value",
            ),
            (
                {"tiers": {"deep": {"base_url": "http://localhost:4000/v1"}}},
                "llm.tiers.deep",
                "changes base_url but names no credential",
            ),
            (
                {"tiers": {"deep": {"provider": "anthropic"}}},
                "llm.tiers.deep",
                "changes provider but names no credential",
            ),
            (
                {
                    "tiers": {
                        "deep": {
                            "provider": "anthropic",
                            "api_key_env": "ANTHROPIC_API_KEY",
                        }
                    }
                },
                "llm.tiers.deep.base_url",
                "would inherit the parent block's base_url",
            ),
            ({"routes": {"bogus": "deep"}}, "llm.routes.bogus", "unknown consumer"),
            (
                {"tiers": {"deep": _DEEP}, "routes": {"reconcile": None}},
                "llm.routes.reconcile",
                "got null",
            ),
            (
                {"tiers": {"deep": _DEEP}, "routes": {"reconcile": ""}},
                "llm.routes.reconcile",
                "must name a tier",
            ),
            (
                {"routes": {"reconcile": "deep"}},
                "llm.routes.reconcile",
                "(defined: none)",
            ),
            (
                {"tiers": {"deep": _DEEP}, "routes": {"reconcile": "dep"}},
                "llm.routes.reconcile",
                "(defined: ['deep'])",
            ),
        ],
    )
    def test_defect_names_its_setting(self, sections, setting, fragment):
        error = _raises(_config(**sections), LLMConsumer.RECONCILE)
        assert error.setting == setting
        assert fragment in error.message
        assert error.message.endswith(".")

    @pytest.mark.parametrize("consumer", [None, *LLMConsumer])
    def test_a_defect_anywhere_fails_every_consumer(self, consumer):
        """No consumer quietly degrades to the parent while another is broken."""
        config = _config(tiers={"deep": _DEEP}, routes={"enrichment": "missing"})
        assert _raises(config, consumer).setting == "llm.routes.enrichment"

    def test_it_is_a_config_error_so_the_cli_boundary_renders_it(self):
        error = _raises(_config(routes={"reconcile": "missing"}))
        assert isinstance(error, ConfigError)
        assert error.message == str(error)

    def test_a_moved_endpoint_with_a_literal_key_is_accepted(self):
        config = _config(tiers={"t": {"base_url": "http://x/v1", "api_key": "sk-t"}})
        validate_llm_routing(config)

    def test_a_provider_change_needs_no_base_url_when_the_parent_has_none(self):
        parent = {"provider": "openai", "api_key_env": "OPENAI_API_KEY"}
        validate_llm_routing(
            {
                **parent,
                "tiers": {"c": {"provider": "anthropic", "api_key_env": "A"}},
            }
        )

    def test_a_tier_may_set_every_block_key(self):
        body = dict.fromkeys(LLM_BLOCK_KEYS, "x")
        validate_llm_routing(_config(tiers={"t": body}))


class TestDescribe:
    """What ``trellis admin llm-routes`` prints: sources, never secrets."""

    def _routed(self, **tier: Any):
        config = _config(tiers={"t": tier}, routes={"reconcile": "t"})
        return resolve_llm_route(config, LLMConsumer.RECONCILE)

    def test_it_names_the_consumer_tier_and_model(self):
        described = self._routed(**_DEEP).describe(environ={})
        assert described == {
            "consumer": "reconcile",
            "tier": "t",
            "provider": "openai",
            "model": "deep",
            "base_url": "http://localhost:4000/v1",
            "api_key_env": "LITELLM_API_KEY",
            "credential_source": None,
        }

    def test_a_literal_key_is_reported_as_a_source_and_never_read_out(self):
        route = self._routed(base_url="http://x/v1", api_key="sk-literal-SECRET")
        described = route.describe(environ={})
        assert described["credential_source"] == "literal"
        assert "sk-literal-SECRET" not in json.dumps(described)
        assert "sk-literal-SECRET" not in repr(route)

    def test_userinfo_in_the_base_url_is_masked(self):
        route = self._routed(
            base_url="https://user:hunter2@proxy.example:8443/v1", api_key_env="K"
        )
        described = route.describe(environ={})
        assert described["base_url"] == "https://***@proxy.example:8443/v1"
        assert "hunter2" not in json.dumps(described)

    def test_an_unparseable_base_url_is_not_echoed(self):
        route = self._routed(base_url="http://[::1/v1", api_key_env="K")
        assert route.describe(environ={})["base_url"] == "<unparseable>"


class TestCredentialSource:
    """``credential_source`` must agree with the key the registry would read."""

    @pytest.mark.parametrize(
        ("block", "environ", "expected"),
        [
            ({"api_key_env": "K"}, {"K": "sk-env"}, "env"),
            ({"api_key_env": "K", "api_key": "sk-lit"}, {"K": "sk-env"}, "env"),
            ({"api_key_env": "K", "api_key": "sk-lit"}, {}, "literal"),
            ({"api_key_env": "K", "api_key": "sk-lit"}, {"K": ""}, "literal"),
            ({"api_key": "sk-lit"}, {}, "literal"),
            ({"api_key_env": "K"}, {}, None),
            ({"api_key_env": "K"}, {"K": ""}, None),
            ({}, {"K": "sk-env"}, None),
        ],
    )
    def test_it_mirrors_the_registry_key_precedence(
        self, monkeypatch, block, environ, expected
    ):
        route = resolve_llm_route({"provider": "openai", **block}, None)
        assert route.credential_source(environ) == expected

        monkeypatch.delenv("K", raising=False)
        for name, value in environ.items():
            monkeypatch.setenv(name, value)
        resolved = _resolve_api_key(route.block)
        assert (resolved is None) == (expected is None)
        if expected == "env":
            assert resolved == environ["K"]
        elif expected == "literal":
            assert resolved == block["api_key"]

    def test_it_reads_the_process_environment_by_default(self, monkeypatch):
        monkeypatch.setenv("TRELLIS_TEST_ROUTING_KEY", "sk-env")
        route = resolve_llm_route(
            {"provider": "openai", "api_key_env": "TRELLIS_TEST_ROUTING_KEY"}, None
        )
        assert route.credential_source() == "env"
