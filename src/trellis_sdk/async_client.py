"""Async Trellis SDK client -- works locally or via HTTP."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import structlog

logger = structlog.get_logger(__name__)

_HTTP_NOT_FOUND = 404


class AsyncTrellisClient:
    """Async client for interacting with the Trellis.

    Mirrors :class:`TrellisClient` with ``async def`` methods.

    Works in two modes:

    - **Remote mode**: When ``base_url`` is provided, uses
      ``httpx.AsyncClient`` to call the REST API.
    - **Local mode**: When no ``base_url``, delegates to a synchronous
      ``TrellisClient`` running store calls via ``asyncio.to_thread``.

    Supports ``async with`` for resource cleanup::

        async with AsyncTrellisClient("http://localhost:8420") as client:
            trace_id = await client.ingest_trace(trace)
    """

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None
        self._http: Any = None  # lazy httpx.AsyncClient
        self._sync: Any = None  # lazy TrellisClient (for local mode)
        self._local_lock = asyncio.Lock()  # serialise local store access

    # -- Context manager --

    async def __aenter__(self) -> AsyncTrellisClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- Internals --

    def _get_http(self) -> Any:
        """Get or create an async httpx client."""
        if self._http is None:
            import httpx  # noqa: PLC0415

            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=30.0,  # type: ignore[arg-type]
            )
        return self._http

    def _get_sync(self) -> Any:
        """Get or create a sync TrellisClient for local-mode delegation."""
        if self._sync is None:
            from trellis_sdk.client import TrellisClient  # noqa: PLC0415

            self._sync = TrellisClient()
        return self._sync

    @property
    def is_remote(self) -> bool:
        """Whether this client connects to a remote API."""
        return self._base_url is not None

    async def _local(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Call a method on the underlying sync client, serialised via lock.

        SQLite connections are not safe to share across threads, so all
        local-mode calls go through a single lock + ``to_thread``.
        """
        async with self._local_lock:
            fn = getattr(self._get_sync(), method_name)
            return await asyncio.to_thread(fn, *args, **kwargs)

    # -- Ingest --

    async def ingest_trace(self, trace: dict[str, Any]) -> str:
        """Ingest a trace. Returns the trace_id."""
        if self.is_remote:
            resp = await self._get_http().post("/api/v1/traces", json=trace)
            resp.raise_for_status()
            return cast("str", resp.json()["trace_id"])

        return await self._local("ingest_trace", trace)  # type: ignore[no-any-return]

    async def ingest_evidence(self, evidence: dict[str, Any]) -> str:
        """Ingest evidence. Returns the evidence_id."""
        if self.is_remote:
            resp = await self._get_http().post("/api/v1/evidence", json=evidence)
            resp.raise_for_status()
            return cast("str", resp.json()["evidence_id"])

        return await self._local("ingest_evidence", evidence)  # type: ignore[no-any-return]

    # -- Retrieve --

    async def search(
        self,
        query: str,
        *,
        domain: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search documents. Returns list of result dicts."""
        if self.is_remote:
            params: dict[str, Any] = {"q": query, "limit": limit}
            if domain:
                params["domain"] = domain
            resp = await self._get_http().get("/api/v1/search", params=params)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return cast("list[dict[str, Any]]", data.get("results", []))

        return await self._local(  # type: ignore[no-any-return]
            "search", query, domain=domain, limit=limit
        )

    async def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        """Get a trace by ID."""
        if self.is_remote:
            resp = await self._get_http().get(f"/api/v1/traces/{trace_id}")
            if resp.status_code == _HTTP_NOT_FOUND:
                return None
            resp.raise_for_status()
            return cast("dict[str, Any] | None", resp.json().get("trace"))

        return await self._local("get_trace", trace_id)  # type: ignore[no-any-return]

    async def list_traces(
        self,
        *,
        domain: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List recent traces."""
        if self.is_remote:
            params: dict[str, Any] = {"limit": limit}
            if domain:
                params["domain"] = domain
            resp = await self._get_http().get("/api/v1/traces", params=params)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return cast("list[dict[str, Any]]", data.get("traces", []))

        return await self._local(  # type: ignore[no-any-return]
            "list_traces", domain=domain, limit=limit
        )

    async def assemble_pack(
        self,
        intent: str,
        *,
        domain: str | None = None,
        agent_id: str | None = None,
        max_items: int = 50,
        max_tokens: int = 8000,
    ) -> dict[str, Any]:
        """Assemble a context pack. Returns pack dict."""
        if self.is_remote:
            resp = await self._get_http().post(
                "/api/v1/packs",
                json={
                    "intent": intent,
                    "domain": domain,
                    "agent_id": agent_id,
                    "max_items": max_items,
                    "max_tokens": max_tokens,
                },
            )
            resp.raise_for_status()
            return cast("dict[str, Any]", resp.json())

        return await self._local(  # type: ignore[no-any-return]
            "assemble_pack",
            intent,
            domain=domain,
            agent_id=agent_id,
            max_items=max_items,
            max_tokens=max_tokens,
        )

    async def assemble_sectioned_pack(
        self,
        intent: str,
        sections: list[dict[str, Any]],
        *,
        domain: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Assemble a sectioned pack with independently budgeted sections."""
        if self.is_remote:
            try:
                resp = await self._get_http().post(
                    "/api/v1/packs/sectioned",
                    json={
                        "intent": intent,
                        "sections": sections,
                        "domain": domain,
                        "agent_id": agent_id,
                    },
                )
                resp.raise_for_status()
                return cast("dict[str, Any]", resp.json())
            except Exception:
                logger.warning(
                    "sectioned_pack_remote_failed",
                    intent=intent,
                    msg="falling back to flat pack",
                )
                flat = await self.assemble_pack(
                    intent, domain=domain, agent_id=agent_id
                )
                return cast("dict[str, Any]", flat)

        return await self._local(  # type: ignore[no-any-return]
            "assemble_sectioned_pack",
            intent,
            sections,
            domain=domain,
            agent_id=agent_id,
        )

    async def get_objective_context(
        self,
        intent: str,
        *,
        domain: str | None = None,
        max_tokens: int = 4000,
    ) -> str:
        """Get objective-level context formatted as markdown."""
        if self.is_remote:
            # Assemble a pack remotely and return its rendered markdown.
            pack = await self.assemble_pack(
                intent, domain=domain, max_tokens=max_tokens
            )
            return cast("str", pack.get("markdown", ""))

        return await self._local(  # type: ignore[no-any-return]
            "get_objective_context",
            intent,
            domain=domain,
            max_tokens=max_tokens,
        )

    async def get_task_context(
        self,
        intent: str,
        *,
        entity_ids: list[str] | None = None,
        domain: str | None = None,
        max_tokens: int = 4000,
    ) -> str:
        """Get task-level context formatted as markdown."""
        if self.is_remote:
            pack = await self.assemble_pack(
                intent, domain=domain, max_tokens=max_tokens
            )
            return cast("str", pack.get("markdown", ""))

        return await self._local(  # type: ignore[no-any-return]
            "get_task_context",
            intent,
            entity_ids=entity_ids,
            domain=domain,
            max_tokens=max_tokens,
        )

    async def get_entity(self, entity_id: str) -> dict[str, Any] | None:
        """Get an entity by ID."""
        if self.is_remote:
            resp = await self._get_http().get(f"/api/v1/entities/{entity_id}")
            if resp.status_code == _HTTP_NOT_FOUND:
                return None
            resp.raise_for_status()
            return cast("dict[str, Any] | None", resp.json().get("entity"))

        return await self._local(  # type: ignore[no-any-return]
            "get_entity", entity_id
        )

    # -- Curate --

    async def create_entity(
        self,
        name: str,
        entity_type: str = "concept",
        properties: dict[str, Any] | None = None,
    ) -> str:
        """Create an entity. Returns the node_id."""
        if self.is_remote:
            resp = await self._get_http().post(
                "/api/v1/entities",
                json={
                    "entity_type": entity_type,
                    "name": name,
                    "properties": properties or {},
                },
            )
            resp.raise_for_status()
            return cast("str", resp.json()["node_id"])

        return await self._local(  # type: ignore[no-any-return]
            "create_entity",
            name,
            entity_type=entity_type,
            properties=properties,
        )

    async def create_link(
        self,
        source_id: str,
        target_id: str,
        edge_kind: str = "entity_related_to",
    ) -> str:
        """Create a link between entities. Returns the edge_id."""
        if self.is_remote:
            resp = await self._get_http().post(
                "/api/v1/links",
                json={
                    "source_id": source_id,
                    "target_id": target_id,
                    "edge_kind": edge_kind,
                },
            )
            resp.raise_for_status()
            return cast("str", resp.json()["edge_id"])

        return await self._local(  # type: ignore[no-any-return]
            "create_link",
            source_id,
            target_id,
            edge_kind=edge_kind,
        )

    # -- Lifecycle --

    async def close(self) -> None:
        """Close any open connections."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._sync is not None:
            self._sync.close()
            self._sync = None
