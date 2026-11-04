"""Pin the seven pack-size / excerpt / disclosure knob resolvers.

Moving the resolvers into :mod:`trellis.core.knobs` was meant to change no
resolved value, env var name, settings key, default or warning event name.
Every number and string below is therefore a *literal*, not an import of the
constant under test: a test that reads ``knobs.REST_PACK_MAX_ITEMS_DEFAULT``
to say what the default is would stay green if someone changed it.

The resolver-behaviour tests (``TestResolverBehaviour``) go through
``_resolver_for``, the only place the import path appears. They were run
against ``origin/main``'s resolvers at their old homes
(``trellis.mcp.server._resolve_*``, ``trellis_api.routes.retrieve._resolve_*``,
``trellis.retrieve.builder_factory._resolve_*``) before those were deleted,
and against this module's resolvers after.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs

from tests.structlog_isolation import clear_cached_logger_proxies
from trellis.core import knobs


@dataclass(frozen=True)
class Knob:
    """One knob's pinned contract, spelled out as literals."""

    setting: str
    env: str
    default: int
    warning_prefix: str


KNOBS = (
    Knob(
        "get_context_max_tokens", "TRELLIS_GET_CONTEXT_MAX_TOKENS", 2000, "mcp_server"
    ),
    Knob("get_context_max_items", "TRELLIS_GET_CONTEXT_MAX_ITEMS", 50, "mcp_server"),
    Knob("search_max_tokens", "TRELLIS_SEARCH_MAX_TOKENS", 2000, "mcp_server"),
    Knob("rest_pack_max_items", "TRELLIS_REST_PACK_MAX_ITEMS", 50, "retrieve_route"),
    Knob(
        "rest_pack_max_tokens", "TRELLIS_REST_PACK_MAX_TOKENS", 8000, "retrieve_route"
    ),
    Knob(
        "disclosure_body_items",
        "TRELLIS_DISCLOSURE_BODY_ITEMS",
        12,
        "builder_factory",
    ),
    Knob("excerpt_max_chars", "TRELLIS_EXCERPT_MAX_CHARS", 500, "builder_factory"),
)

#: A value that is in bounds for every knob and equals no default above.
ENV_VALUE = 123
SETTINGS_VALUE = 77


# --- resolver-lookup-begin (the one place the import path appears) ---
def _resolver_for(setting: str) -> Callable[[Mapping[str, Any] | None], int]:
    return {
        "get_context_max_tokens": knobs.resolve_get_context_max_tokens,
        "get_context_max_items": knobs.resolve_get_context_max_items,
        "search_max_tokens": knobs.resolve_search_max_tokens,
        "rest_pack_max_items": knobs.resolve_rest_pack_max_items,
        "rest_pack_max_tokens": knobs.resolve_rest_pack_max_tokens,
        "disclosure_body_items": knobs.resolve_body_items,
        "excerpt_max_chars": knobs.resolve_excerpt_max_chars,
    }[setting]


# --- resolver-lookup-end ---


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for knob in KNOBS:
        monkeypatch.delenv(knob.env, raising=False)


@pytest.fixture
def logs() -> Iterator[list[dict[str, Any]]]:
    """Capture structlog events; see ``tests/structlog_isolation.py`` for why
    both the wrapper swap and the proxy-cache eviction are required."""
    prior = structlog.get_config()
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.NOTSET)
    )
    try:
        with capture_logs() as captured:
            clear_cached_logger_proxies()
            yield captured
    finally:
        structlog.configure(**prior)
        clear_cached_logger_proxies()


def _ids() -> list[str]:
    return [k.setting for k in KNOBS]


@pytest.mark.parametrize("knob", KNOBS, ids=_ids())
class TestResolverBehaviour:
    def test_unset_resolves_the_default(self, knob: Knob) -> None:
        resolve = _resolver_for(knob.setting)

        assert resolve(None) == knob.default
        assert resolve({}) == knob.default
        assert resolve({"some_other_setting": 5}) == knob.default

    def test_env_only(self, knob: Knob, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(knob.env, str(ENV_VALUE))

        assert _resolver_for(knob.setting)(None) == ENV_VALUE

    def test_settings_only(self, knob: Knob) -> None:
        assert _resolver_for(knob.setting)({knob.setting: SETTINGS_VALUE}) == (
            SETTINGS_VALUE
        )

    def test_env_beats_settings(
        self, knob: Knob, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(knob.env, str(ENV_VALUE))

        resolved = _resolver_for(knob.setting)({knob.setting: SETTINGS_VALUE})

        assert resolved == ENV_VALUE

    def test_blank_env_falls_through_to_settings(
        self, knob: Knob, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(knob.env, "   ")

        resolved = _resolver_for(knob.setting)({knob.setting: SETTINGS_VALUE})

        assert resolved == SETTINGS_VALUE

    def test_bad_env_logs_the_callers_event_and_falls_back(
        self,
        knob: Knob,
        monkeypatch: pytest.MonkeyPatch,
        logs: list[dict[str, Any]],
    ) -> None:
        monkeypatch.setenv(knob.env, "not-an-int")

        # A bad env value falls back to the default, not to the settings
        # value: the env var was set, so it is the one consulted.
        resolved = _resolver_for(knob.setting)({knob.setting: SETTINGS_VALUE})

        assert resolved == knob.default
        lines = [e for e in logs if e["event"] == f"{knob.warning_prefix}.bad_env_int"]
        assert len(lines) == 1
        assert lines[0]["var"] == knob.env
        assert "value" not in lines[0]

    def test_out_of_bounds_env_is_rejected_not_honoured(
        self,
        knob: Knob,
        monkeypatch: pytest.MonkeyPatch,
        logs: list[dict[str, Any]],
    ) -> None:
        monkeypatch.setenv(knob.env, "0")  # every knob's minimum is 1

        assert _resolver_for(knob.setting)(None) == knob.default
        assert [e["event"] for e in logs] == [f"{knob.warning_prefix}.bad_env_int"]

    def test_bad_settings_value_logs_the_callers_event_and_falls_back(
        self, knob: Knob, logs: list[dict[str, Any]]
    ) -> None:
        resolved = _resolver_for(knob.setting)({knob.setting: "not-an-int"})

        assert resolved == knob.default
        lines = [
            e for e in logs if e["event"] == f"{knob.warning_prefix}.bad_settings_int"
        ]
        assert len(lines) == 1
        assert lines[0]["setting"] == knob.setting
        assert "value" not in lines[0]


# --- scratch-cut-begin (needs the new module's constants; not run on the old tree) ---
@pytest.mark.parametrize("knob", KNOBS, ids=_ids())
class TestRegistryAgreement:
    """The settings registry restates each knob's env var and default; it
    must say what the resolver does, or the operator-facing settings page
    reports a default no request ever gets."""

    def test_registry_spec_matches_the_pinned_contract(self, knob: Knob) -> None:
        from trellis.core.settings_registry import get_setting

        spec = get_setting(knob.setting)

        assert spec is not None
        assert spec.env_var == knob.env
        assert spec.default == knob.default

    def test_knobs_module_constants_match_the_pinned_contract(self, knob: Knob) -> None:
        upper = {
            "get_context_max_tokens": "GET_CONTEXT_MAX_TOKENS",
            "get_context_max_items": "GET_CONTEXT_MAX_ITEMS",
            "search_max_tokens": "SEARCH_MAX_TOKENS",
            "rest_pack_max_items": "REST_PACK_MAX_ITEMS",
            "rest_pack_max_tokens": "REST_PACK_MAX_TOKENS",
            "disclosure_body_items": "BODY_ITEMS",
            "excerpt_max_chars": "EXCERPT_MAX_CHARS",
        }[knob.setting]

        assert getattr(knobs, f"{upper}_ENV") == knob.env
        assert getattr(knobs, f"{upper}_SETTING") == knob.setting
        default = getattr(knobs, f"{upper}_DEFAULT", None)
        if default is not None:  # body/excerpt defaults are owned elsewhere
            assert default == knob.default


def test_body_and_excerpt_defaults_are_the_modules_that_own_them() -> None:
    """``resolve_body_items`` / ``resolve_excerpt_max_chars`` read their
    default from the module that owns it; pin that those still say 12 / 500."""
    from trellis.retrieve.disclosure import DEFAULT_BODY_ITEMS
    from trellis.retrieve.excerpts import EXCERPT_MAX_CHARS

    assert DEFAULT_BODY_ITEMS == 12
    assert EXCERPT_MAX_CHARS == 500


def test_flat_max_items_is_the_get_context_default() -> None:
    from trellis.mcp import server

    assert server._FLAT_MAX_ITEMS == 50
    assert server._FLAT_MAX_ITEMS == knobs.GET_CONTEXT_MAX_ITEMS_DEFAULT


# --- scratch-cut-end ---
