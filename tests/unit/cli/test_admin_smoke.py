"""Tests for the ``trellis admin smoke-test`` CLI command."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx
from typer.testing import CliRunner

from trellis_cli import admin as admin_module
from trellis_cli.main import app

if TYPE_CHECKING:
    import pytest

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_READYZ_OK_BODY = {
    "status": "ready",
    "backends": {
        "event_log": {"status": "ok", "latency_ms": 4.21},
        "graph_store": {"status": "ok", "latency_ms": 12.30},
        "vector_store": {"status": "ok", "latency_ms": 3.05},
        "document_store": {"status": "ok", "latency_ms": 2.81},
    },
}

_METRICS_PROMETHEUS_BODY = (
    "# HELP http_requests_total Total HTTP requests\n"
    "http_requests_total 1\n"
)


def _advisories_response(
    request: httpx.Request, api_key: str | None
) -> httpx.Response:
    """Mirror ``require_api_key`` in ``trellis_api.auth``."""
    if api_key is None:
        return httpx.Response(200, json={"advisories": []})
    if request.headers.get("X-API-Key") == api_key:
        return httpx.Response(200, json={"advisories": []})
    return httpx.Response(
        401,
        json={"detail": "missing or invalid X-API-Key"},
        headers={"WWW-Authenticate": "ApiKey"},
    )


def _healthy_handler(
    api_key: str | None = "secret",
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a MockTransport handler simulating a healthy deployment.

    Mirrors the actual route shapes: ``/readyz`` returns the same per-backend
    body that ``trellis_api.routes.health.readyz`` produces; ``/api/v1/advisories``
    rejects requests without ``X-API-Key`` (when ``api_key`` is set) the way
    ``require_api_key`` in ``trellis_api.auth`` does.
    """
    routes = {
        "/healthz": lambda _r: httpx.Response(200, json={"status": "ok"}),
        "/readyz": lambda _r: httpx.Response(200, json=_READYZ_OK_BODY),
        "/api/v1/advisories": lambda r: _advisories_response(r, api_key),
        "/metrics": lambda _r: httpx.Response(
            200,
            text=_METRICS_PROMETHEUS_BODY,
            headers={"content-type": "text/plain; version=0.0.4"},
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(request.url.path, lambda _r: httpx.Response(404))(request)

    return handler


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler: Callable) -> None:
    """Replace ``httpx.Client`` in the admin module with one that uses MockTransport."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(admin_module.httpx, "Client", factory)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestSmokeTestHappyPath:
    def test_healthy_api_with_valid_key_passes_all_checks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_client(monkeypatch, _healthy_handler("secret"))

        result = runner.invoke(
            app, ["admin", "smoke-test", "--api-key", "secret", "--format", "json"]
        )

        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["summary"]["fail"] == 0
        assert payload["summary"]["pass"] == 5
        names = [c["name"] for c in payload["checks"]]
        assert names == [
            "healthz",
            "readyz",
            "auth_rejects_missing",
            "auth_accepts_valid",
            "metrics",
        ]

    def test_healthy_api_text_output_renders_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_client(monkeypatch, _healthy_handler("secret"))

        result = runner.invoke(app, ["admin", "smoke-test", "--api-key", "secret"])

        assert result.exit_code == 0, result.stdout
        assert "Trellis API smoke test" in result.stdout
        assert "PASS" in result.stdout
        assert "5 checks" in result.stdout
        assert "0 fail" in result.stdout

    def test_no_api_key_skips_auth_checks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Server enforces no auth either, so auth_accepts_valid would
        # also be checkable — but we want to verify the *client-side*
        # behavior: when no key is configured, both auth checks SKIP.
        _patch_client(monkeypatch, _healthy_handler(None))
        monkeypatch.delenv("TRELLIS_API_KEY", raising=False)

        result = runner.invoke(app, ["admin", "smoke-test", "--format", "json"])

        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        assert payload["summary"]["skip"] == 2
        assert payload["summary"]["fail"] == 0
        skipped = [c for c in payload["checks"] if c["status"] == "skip"]
        assert {c["name"] for c in skipped} == {
            "auth_rejects_missing",
            "auth_accepts_valid",
        }

    def test_api_key_from_env_var_is_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_client(monkeypatch, _healthy_handler("env-secret"))
        monkeypatch.setenv("TRELLIS_API_KEY", "env-secret")

        result = runner.invoke(app, ["admin", "smoke-test", "--format", "json"])

        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        assert payload["summary"]["pass"] == 5


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


class TestSmokeTestFailures:
    def test_healthz_down_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/healthz":
                return httpx.Response(503)
            return _healthy_handler("secret")(request)

        _patch_client(monkeypatch, handler)

        result = runner.invoke(
            app, ["admin", "smoke-test", "--api-key", "secret", "--format", "json"]
        )

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        healthz = next(c for c in payload["checks"] if c["name"] == "healthz")
        assert healthz["status"] == "fail"
        assert "503" in healthz["error"]

    def test_readyz_degraded_fails_with_backend_detail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/readyz":
                return httpx.Response(
                    503,
                    json={
                        "status": "degraded",
                        "backends": {
                            "event_log": {"status": "ok", "latency_ms": 4.0},
                            "graph_store": {
                                "status": "degraded",
                                "latency_ms": 30000.0,
                                "error": "connection timed out",
                            },
                            "vector_store": {"status": "ok", "latency_ms": 3.0},
                            "document_store": {"status": "ok", "latency_ms": 2.8},
                        },
                    },
                )
            return _healthy_handler("secret")(request)

        _patch_client(monkeypatch, handler)

        result = runner.invoke(
            app, ["admin", "smoke-test", "--api-key", "secret", "--format", "json"]
        )

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        readyz = next(c for c in payload["checks"] if c["name"] == "readyz")
        assert readyz["status"] == "fail"
        # Per-backend detail flows through to the report so operators can
        # tell which backend is down without re-running curl manually.
        assert readyz["backends"]["graph_store"]["status"] == "degraded"
        assert "timed out" in readyz["backends"]["graph_store"]["error"]

    def test_auth_misconfigured_when_protected_route_returns_200(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulates the dangerous case: TRELLIS_API_KEY is set in the
        # operator's env (so the smoke test sends auth checks), but the
        # server hasn't been configured with the key — it accepts every
        # request. The smoke test must catch this.
        _patch_client(monkeypatch, _healthy_handler(api_key=None))

        result = runner.invoke(
            app, ["admin", "smoke-test", "--api-key", "secret", "--format", "json"]
        )

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        rejects = next(
            c for c in payload["checks"] if c["name"] == "auth_rejects_missing"
        )
        assert rejects["status"] == "fail"
        assert "200" in rejects["error"]

    def test_network_error_is_surfaced_as_fail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            message = f"connection refused: {request.url}"
            raise httpx.ConnectError(message)

        _patch_client(monkeypatch, handler)

        result = runner.invoke(
            app, ["admin", "smoke-test", "--api-key", "secret", "--format", "json"]
        )

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        # Every check that hits the network should fail; metrics + healthz
        # + readyz + both auth checks = 5 fails.
        assert payload["summary"]["fail"] == 5


# ---------------------------------------------------------------------------
# Tolerable non-pass — observability not wired
# ---------------------------------------------------------------------------


class TestSmokeTestMetricsOptional:
    def test_metrics_404_treated_as_info_not_fail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/metrics":
                return httpx.Response(404)
            return _healthy_handler("secret")(request)

        _patch_client(monkeypatch, handler)

        result = runner.invoke(
            app, ["admin", "smoke-test", "--api-key", "secret", "--format", "json"]
        )

        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        metrics = next(c for c in payload["checks"] if c["name"] == "metrics")
        assert metrics["status"] == "info"
        assert "observability" in metrics["note"]
        assert payload["summary"]["fail"] == 0
        assert payload["summary"]["info"] == 1

    def test_metrics_200_but_not_prometheus_format_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/metrics":
                return httpx.Response(200, text="not a prometheus payload")
            return _healthy_handler("secret")(request)

        _patch_client(monkeypatch, handler)

        result = runner.invoke(
            app, ["admin", "smoke-test", "--api-key", "secret", "--format", "json"]
        )

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        metrics = next(c for c in payload["checks"] if c["name"] == "metrics")
        assert metrics["status"] == "fail"
        assert "Prometheus" in metrics["error"]


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


class TestSmokeTestUrlResolution:
    def test_url_flag_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(monkeypatch, _healthy_handler("secret"))
        monkeypatch.setenv("TRELLIS_API_HOST", "10.0.0.1")
        monkeypatch.setenv("TRELLIS_API_PORT", "9000")

        result = runner.invoke(
            app,
            [
                "admin",
                "smoke-test",
                "--url",
                "http://override:1234",
                "--api-key",
                "secret",
                "--format",
                "json",
            ],
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["url"] == "http://override:1234"

    def test_default_url_falls_back_to_env_then_loopback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_client(monkeypatch, _healthy_handler("secret"))
        monkeypatch.delenv("TRELLIS_API_HOST", raising=False)
        monkeypatch.delenv("TRELLIS_API_PORT", raising=False)

        result = runner.invoke(
            app, ["admin", "smoke-test", "--api-key", "secret", "--format", "json"]
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["url"] == "http://127.0.0.1:8420"
