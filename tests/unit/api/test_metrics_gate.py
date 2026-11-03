"""``/metrics`` under ``TRELLIS_METRICS_PUBLIC``: who the gate lets through.

The route exists only with the ``observability`` extra, so these call the gate
directly: they run on every leg, not only the one that installs the extra.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from trellis_api.auth import ALL_SCOPES, AuthContext
from trellis_api.observability import METRICS_PUBLIC_ENV, _require_metrics_access
from trellis_api.routes._principal import PUBLIC_CALLER_REFUSAL


def _ctx(*scopes: str) -> AuthContext:
    return AuthContext(key_id="k", name="n", scopes=frozenset(scopes), mode="required")


def test_gated_refuses_an_external_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(METRICS_PUBLIC_ENV, "false")
    with pytest.raises(HTTPException) as caught:
        _require_metrics_access(_ctx("external"))
    assert caught.value.status_code == 403
    assert caught.value.detail == PUBLIC_CALLER_REFUSAL


def test_gated_refuses_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(METRICS_PUBLIC_ENV, "false")
    with pytest.raises(HTTPException) as caught:
        _require_metrics_access(None)
    assert caught.value.status_code == 401


@pytest.mark.parametrize("scopes", [("read",), ("ingest",), tuple(ALL_SCOPES)])
def test_gated_passes_a_key_above_public(
    monkeypatch: pytest.MonkeyPatch, scopes: tuple[str, ...]
) -> None:
    monkeypatch.setenv(METRICS_PUBLIC_ENV, "false")
    _require_metrics_access(_ctx(*scopes))


def test_open_posture_passes_everyone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(METRICS_PUBLIC_ENV, raising=False)
    _require_metrics_access(None)
    _require_metrics_access(_ctx("external"))
