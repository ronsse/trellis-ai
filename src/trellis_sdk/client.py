"""Synchronous Trellis SDK client — HTTP only.

Talks to a running ``trellis-api`` instance.  Dual-mode (in-process
``StoreRegistry`` direct access) was removed in the Step 3 refactor
([TODO.md](../../TODO.md)) so the SDK has exactly one code path and
zero dependency on ``trellis`` core modules.

For fast tests without a real network listener, use
:func:`trellis.testing.in_memory_client` which mounts the FastAPI
app via ``httpx.ASGITransport`` and hands back a ``TrellisClient``
pointed at it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import httpx
import structlog

from trellis_sdk._format import (
    format_sectioned_pack_as_markdown,
)
from trellis_sdk._http import (
    SDK_API_MAJOR,
    SDK_API_MINOR,
    check_handshake,
    raise_for_status,
)

if TYPE_CHECKING:
    from types import TracebackType

logger = structlog.get_logger(__name__)

_HTTP_NOT_FOUND = 404
_DEFAULT_TIMEOUT_SECONDS = 30.0


class TrellisClient:
    """Synchronous HTTP client for the Trellis REST API.

    Construct with either ``base_url`` (for a real network target) or
    an injected ``httpx.Client`` (for tests — see
    :func:`trellis.testing.in_memory_client`).

    The version handshake fires lazily on the first request, not in
    ``__init__`` — so constructing a client never issues network IO.
    Disable via ``verify_version=False`` for scripts that want to skip
    the check.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        http: httpx.Client | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        verify_version: bool = True,
    ) -> None:
        if base_url is None and http is None:
            msg = (
                "TrellisClient requires either base_url= or http=. "
                "In-process mode was removed in Step 3 — use "
                "trellis.testing.in_memory_client() for test fixtures."
            )
            raise ValueError(msg)
        if http is not None and base_url is not None:
            msg = "Pass base_url OR http, not both."
            raise ValueError(msg)

        self._owns_http = http is None
        if http is not None:
            self._http = http
        else:
            self._http = httpx.Client(
                base_url=cast("str", base_url).rstrip("/"),
                timeout=timeout,
            )
        self._verify_version = verify_version
        self._handshake_done = False

    # -- Context manager --

    def __enter__(self) -> TrellisClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- Internals --

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        self._ensure_handshake()
        resp = self._http.request(method, path, json=json, params=params)
        raise_for_status(resp, request_path=path)
        return resp

    def _ensure_handshake(self) -> None:
        if self._handshake_done or not self._verify_version:
            return
        # Mark early so a recursive call (should never happen, but…)
        # doesn't loop.
        self._handshake_done = True
        try:
            resp = self._http.get("/api/version")
        except httpx.HTTPError:
            # Network glitch on first call — don't block usage.  If
            # the subsequent real call fails, caller sees the real
            # error with the real context.
            self._handshake_done = False
            return
        if resp.status_code != 200:  # noqa: PLR2004
            self._handshake_done = False
            return
        try:
            check_handshake(resp.json())
        except Exception:
            self._handshake_done = False
            raise
        logger.debug(
            "sdk_handshake_ok",
            sdk_api_major=SDK_API_MAJOR,
            sdk_api_minor=SDK_API_MINOR,
        )

    # -- Ingest --

    def ingest_trace(self, trace: dict[str, Any]) -> str:
        resp = self._request("POST", "/api/v1/traces", json=trace)
        return cast("str", resp.json()["trace_id"])

    def ingest_evidence(self, evidence: dict[str, Any]) -> str:
        resp = self._request("POST", "/api/v1/evidence", json=evidence)
        return cast("str", resp.json()["evidence_id"])

    # -- Retrieve --

    def search(
        self,
        query: str,
        *,
        domain: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"q": query, "limit": limit}
        if domain:
            params["domain"] = domain
        resp = self._request("GET", "/api/v1/search", params=params)
        return cast("list[dict[str, Any]]", resp.json().get("results", []))

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        self._ensure_handshake()
        resp = self._http.get(f"/api/v1/traces/{trace_id}")
        if resp.status_code == _HTTP_NOT_FOUND:
            return None
        raise_for_status(resp, request_path=f"/api/v1/traces/{trace_id}")
        return cast("dict[str, Any] | None", resp.json().get("trace"))

    def list_traces(
        self,
        *,
        domain: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if domain:
            params["domain"] = domain
        resp = self._request("GET", "/api/v1/traces", params=params)
        return cast("list[dict[str, Any]]", resp.json().get("traces", []))

    def assemble_pack(
        self,
        intent: str,
        *,
        domain: str | None = None,
        agent_id: str | None = None,
        max_items: int = 50,
        max_tokens: int = 8000,
    ) -> dict[str, Any]:
        payload = {
            "intent": intent,
            "domain": domain,
            "agent_id": agent_id,
            "max_items": max_items,
            "max_tokens": max_tokens,
        }
        resp = self._request("POST", "/api/v1/packs", json=payload)
        return cast("dict[str, Any]", resp.json())

    def assemble_sectioned_pack(
        self,
        intent: str,
        sections: list[dict[str, Any]],
        *,
        domain: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "intent": intent,
            "sections": sections,
            "domain": domain,
            "agent_id": agent_id,
        }
        resp = self._request("POST", "/api/v1/packs/sectioned", json=payload)
        return cast("dict[str, Any]", resp.json())

    def get_objective_context(
        self,
        intent: str,
        *,
        domain: str | None = None,
        max_tokens: int = 4000,
    ) -> str:
        """Objective-level context as markdown.

        Assembles a two-section pack (domain knowledge + operational)
        remotely, then formats it locally via the pure-wire
        :mod:`trellis_sdk._format` helpers.
        """
        sections = [
            {
                "name": "domain_knowledge",
                "retrieval_affinities": ["governance", "ownership", "conventions"],
                "content_types": ["document", "entity"],
                "scopes": ["domain"],
                "max_tokens": max_tokens // 2,
                "max_items": 15,
            },
            {
                "name": "operational",
                "retrieval_affinities": ["execution_trace", "incident", "runbook"],
                "content_types": ["trace", "evidence"],
                "scopes": ["operational"],
                "max_tokens": max_tokens // 2,
                "max_items": 10,
            },
        ]
        pack = self.assemble_sectioned_pack(
            intent, sections, domain=domain, agent_id="objective"
        )
        return format_sectioned_pack_as_markdown(
            pack.get("sections", []),
            intent,
            max_tokens=max_tokens,
        )

    def get_task_context(
        self,
        intent: str,
        *,
        entity_ids: list[str] | None = None,
        domain: str | None = None,
        max_tokens: int = 4000,
    ) -> str:
        """Task-level context as markdown."""
        sections: list[dict[str, Any]] = [
            {
                "name": "technical_pattern",
                "retrieval_affinities": ["schema", "code_pattern", "sql_template"],
                "content_types": ["document", "code"],
                "scopes": ["technical"],
                "entity_ids": entity_ids or [],
                "max_tokens": max_tokens // 2,
                "max_items": 15,
            },
            {
                "name": "reference",
                "retrieval_affinities": ["example", "prior_output", "test_case"],
                "content_types": ["document", "evidence"],
                "scopes": ["reference"],
                "entity_ids": entity_ids or [],
                "max_tokens": max_tokens // 2,
                "max_items": 10,
            },
        ]
        pack = self.assemble_sectioned_pack(
            intent, sections, domain=domain, agent_id="task"
        )
        return format_sectioned_pack_as_markdown(
            pack.get("sections", []),
            intent,
            max_tokens=max_tokens,
        )

    def get_entity(self, entity_id: str) -> dict[str, Any] | None:
        self._ensure_handshake()
        path = f"/api/v1/entities/{entity_id}"
        resp = self._http.get(path)
        if resp.status_code == _HTTP_NOT_FOUND:
            return None
        raise_for_status(resp, request_path=path)
        return cast("dict[str, Any] | None", resp.json().get("entity"))

    # -- Curate --

    def create_entity(
        self,
        name: str,
        entity_type: str = "concept",
        properties: dict[str, Any] | None = None,
    ) -> str:
        payload = {
            "entity_type": entity_type,
            "name": name,
            "properties": properties or {},
        }
        resp = self._request("POST", "/api/v1/entities", json=payload)
        return cast("str", resp.json()["node_id"])

    def create_link(
        self,
        source_id: str,
        target_id: str,
        edge_kind: str = "entity_related_to",
    ) -> str:
        payload = {
            "source_id": source_id,
            "target_id": target_id,
            "edge_kind": edge_kind,
        }
        resp = self._request("POST", "/api/v1/links", json=payload)
        return cast("str", resp.json()["edge_id"])

    # -- Lifecycle --

    def close(self) -> None:
        """Close the underlying httpx client (if we own it)."""
        if self._owns_http:
            self._http.close()
