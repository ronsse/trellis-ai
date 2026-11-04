"""A fixed corpus and one ``POST /packs`` call, normalised for comparison.

Importing nothing newer than the key store itself, this module runs unchanged
against any revision of the server: the golden pack
``tests/unit/api/golden/pack_no_defaults.json`` was captured by running
:func:`run` on the revision that preceded per-key defaults, and no test
regenerates it. A key that sets no defaults must be served that pack exactly.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import generate_api_key
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

INTENT = "deployment runbook for the alpha service"

DOCS = {
    "doc-alpha-runbook": (
        (
            "Alpha service deployment runbook: drain the queue, roll the pods, "
            "verify health checks before reopening traffic."
        ),
        {"domain": "platform"},
    ),
    "doc-alpha-rollback": (
        (
            "Alpha service rollback procedure: redeploy the previous image and "
            "replay the queue from the checkpoint."
        ),
        {"domain": "platform"},
    ),
    "doc-beta-notes": (
        "Beta service notes on deployment cadence and review ownership.",
        {"domain": "data"},
    ),
    "doc-gamma-unrelated": (
        "Gamma billing export schedule and finance review contacts.",
        {"domain": "finance"},
    ),
}

_VOLATILE_KEY = re.compile(
    r"(^pack_id$|_at$|^timestamp$|duration|latency|elapsed|_ms$|^trace_id$)"
)


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _scrub(v)
            for k, v in sorted(value.items())
            if not _VOLATILE_KEY.search(k)
        }
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, float):
        # Scores carry run-to-run float noise below the 7th digit (a clock-derived
        # decay factor); four significant digits is far inside any real change.
        return float(f"{value:.4g}")
    return value


def run(tmp_path: Path, monkeypatch: Any, scopes: list[str] | None = None) -> Any:
    """The scrubbed ``POST /packs`` response for a key holding ``scopes``."""
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    registry = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = registry
    try:
        for doc_id, (content, metadata) in DOCS.items():
            registry.knowledge.document_store.put(doc_id, content, dict(metadata))
        token, record = generate_api_key("golden", scopes or ["read"])
        registry.operational.api_key_store.create(record)
        client = TestClient(create_app())
        resp = client.post(
            "/api/v1/packs",
            json={"intent": INTENT, "max_items": 10},
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 200, resp.text
        return _scrub(resp.json())
    finally:
        registry.close()
        app_module._registry = None
