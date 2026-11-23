"""``TRELLIS_PACK_PROVENANCE_LINE`` resolution: env > settings > default off."""

from __future__ import annotations

import pytest

from trellis.core.settings_registry import SETTINGS_REGISTRY
from trellis.retrieve.builder_factory import (
    PACK_PROVENANCE_LINE_ENV,
    PACK_PROVENANCE_LINE_SETTING,
    resolve_pack_provenance_line,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PACK_PROVENANCE_LINE_ENV, raising=False)


def test_default_is_off() -> None:
    assert resolve_pack_provenance_line() is False
    assert resolve_pack_provenance_line({}) is False


@pytest.mark.parametrize(
    ("stored", "expected"),
    [("on", True), (" ON ", True), ("off", False), (True, False), ("yes", False)],
)
def test_settings_value(stored: object, expected: bool) -> None:
    settings = {PACK_PROVENANCE_LINE_SETTING: stored}
    assert resolve_pack_provenance_line(settings) is expected


def test_env_beats_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PACK_PROVENANCE_LINE_ENV, "off")
    assert resolve_pack_provenance_line({PACK_PROVENANCE_LINE_SETTING: "on"}) is False
    monkeypatch.setenv(PACK_PROVENANCE_LINE_ENV, "On")
    assert resolve_pack_provenance_line({PACK_PROVENANCE_LINE_SETTING: "off"}) is True


def test_unrecognised_env_reads_off_over_an_on_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PACK_PROVENANCE_LINE_ENV, "true")
    assert resolve_pack_provenance_line({PACK_PROVENANCE_LINE_SETTING: "on"}) is False


def test_registry_entry_matches_the_resolver() -> None:
    spec = SETTINGS_REGISTRY[PACK_PROVENANCE_LINE_SETTING]
    assert spec.env_var == PACK_PROVENANCE_LINE_ENV
    assert spec.default == "off"
    assert spec.enum == ("off", "on")
    assert spec.surfaces == ("mcp",)
