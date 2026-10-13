"""Tests for the fail-closed REST bind check (#189).

Mirrors ``tests/unit/mcp/test_auth.py::TestResolveHttpSettings``: same
loopback detection, same truthy-override parsing, same fail-closed
shape, a different surface. ``check_bind_is_safe`` is exercised as a
pure decision function here — no uvicorn, no socket — the CLI-level
"does a refusal actually stop the server" behaviour lives in
``tests/unit/cli/test_serve_bind_check.py``.
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from trellis.errors import ConfigError
from trellis_api.auth import (
    ALLOW_INSECURE_BIND_ENV,
    AUTH_MODE_ENV,
    check_bind_is_safe,
)


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no auth or bind-override configuration."""
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.delenv(AUTH_MODE_ENV, raising=False)
    monkeypatch.delenv(ALLOW_INSECURE_BIND_ENV, raising=False)


class TestCheckBindIsSafe:
    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
    @pytest.mark.parametrize("mode", ["off", "optional", "required"])
    def test_loopback_host_is_always_allowed(
        self, monkeypatch: pytest.MonkeyPatch, mode: str, host: str
    ) -> None:
        """Loopback never needs the override, whatever the auth mode."""
        monkeypatch.setenv(AUTH_MODE_ENV, mode)
        check_bind_is_safe(host, 8420)  # must not raise

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.12", "skynet"])  # noqa: S104
    def test_required_mode_may_bind_anywhere(
        self, monkeypatch: pytest.MonkeyPatch, host: str
    ) -> None:
        """A container binding the pod IP is fine once auth is required."""
        monkeypatch.setenv(AUTH_MODE_ENV, "required")
        check_bind_is_safe(host, 8420)  # must not raise

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.12", "skynet"])  # noqa: S104
    def test_off_mode_refuses_non_loopback_bind(
        self, monkeypatch: pytest.MonkeyPatch, host: str
    ) -> None:
        monkeypatch.setenv(AUTH_MODE_ENV, "off")
        with pytest.raises(ConfigError, match="Refusing to bind") as exc:
            check_bind_is_safe(host, 8420)
        assert exc.value.setting == AUTH_MODE_ENV

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.12", "skynet"])  # noqa: S104
    def test_optional_mode_refuses_non_loopback_bind(
        self, monkeypatch: pytest.MonkeyPatch, host: str
    ) -> None:
        """optional grants every scope without a credential, same as off."""
        monkeypatch.setenv(AUTH_MODE_ENV, "optional")
        with pytest.raises(ConfigError, match="Refusing to bind"):
            check_bind_is_safe(host, 8420)

    def test_refusal_names_both_escape_routes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(AUTH_MODE_ENV, "off")
        with pytest.raises(ConfigError) as exc:
            check_bind_is_safe("0.0.0.0", 8420)  # noqa: S104
        message = str(exc.value)
        assert AUTH_MODE_ENV in message
        assert "required" in message
        assert ALLOW_INSECURE_BIND_ENV in message

    def test_insecure_bind_override_permits_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(AUTH_MODE_ENV, "off")
        monkeypatch.setenv(ALLOW_INSECURE_BIND_ENV, "1")
        check_bind_is_safe("0.0.0.0", 8420)  # noqa: S104 — must not raise

    @pytest.mark.parametrize("raw", ["1", "true", "YES", "On"])
    def test_override_truthy_values(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(AUTH_MODE_ENV, "off")
        monkeypatch.setenv(ALLOW_INSECURE_BIND_ENV, raw)
        check_bind_is_safe("0.0.0.0", 8420)  # noqa: S104 — must not raise

    def test_override_fails_closed_on_unrecognised_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(AUTH_MODE_ENV, "off")
        monkeypatch.setenv(ALLOW_INSECURE_BIND_ENV, "maybe")
        with pytest.raises(ConfigError, match="Refusing to bind"):
            check_bind_is_safe("0.0.0.0", 8420)  # noqa: S104

    def test_override_logs_a_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(AUTH_MODE_ENV, "off")
        monkeypatch.setenv(ALLOW_INSECURE_BIND_ENV, "1")
        with capture_logs() as logs:
            check_bind_is_safe("0.0.0.0", 8420)  # noqa: S104
        assert any(log["event"] == "api_insecure_bind_allowed" for log in logs)

    def test_legacy_api_key_implies_required_and_allows_non_loopback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset TRELLIS_AUTH_MODE + legacy TRELLIS_API_KEY infers required."""
        monkeypatch.setenv("TRELLIS_API_KEY", "legacy-shared-secret")
        check_bind_is_safe("0.0.0.0", 8420)  # noqa: S104 — must not raise

    def test_invalid_mode_raises_before_the_bind_decision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unparseable mode is its own ConfigError, same as request handling."""
        monkeypatch.setenv(AUTH_MODE_ENV, "sometimes")
        with pytest.raises(ConfigError, match=AUTH_MODE_ENV):
            check_bind_is_safe("0.0.0.0", 8420)  # noqa: S104
