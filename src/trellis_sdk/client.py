"""Trellis SDK client -- works locally or via HTTP."""

from __future__ import annotations

from typing import Any, cast

import structlog

logger = structlog.get_logger(__name__)

_HTTP_NOT_FOUND = 404


class TrellisClient:
    """Client for interacting with the Trellis.

    Works in two modes:
    - **Remote mode**: When base_url is provided, uses HTTP to call the REST API.
    - **Local mode**: When no base_url, uses local stores directly via StoreRegistry.
    """

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None
        self._http: Any = None  # lazy httpx.Client
        self._registry: Any = None  # lazy StoreRegistry

    def _get_http(self) -> Any:
        """Get or create an httpx client."""
        if self._http is None:
            import httpx  # noqa: PLC0415

            self._http = httpx.Client(base_url=self._base_url, timeout=30.0)  # type: ignore[arg-type]
        return self._http

    def _get_registry(self) -> Any:
        """Get or create a local StoreRegistry."""
        if self._registry is None:
            from trellis.stores.registry import StoreRegistry  # noqa: PLC0415

            self._registry = StoreRegistry.from_config_dir()
        return self._registry

    @property
    def is_remote(self) -> bool:
        """Whether this client connects to a remote API."""
        return self._base_url is not None

    # -- Ingest --

    def ingest_trace(self, trace: dict[str, Any]) -> str:
        """Ingest a trace. Returns the trace_id."""
        if self.is_remote:
            resp = self._get_http().post("/api/v1/traces", json=trace)
            resp.raise_for_status()
            return cast("str", resp.json()["trace_id"])

        from trellis.schemas.trace import Trace  # noqa: PLC0415

        t = Trace.model_validate(trace)
        registry = self._get_registry()
        return cast("str", registry.trace_store.append(t))

    def ingest_evidence(self, evidence: dict[str, Any]) -> str:
        """Ingest evidence. Returns the evidence_id."""
        if self.is_remote:
            resp = self._get_http().post("/api/v1/evidence", json=evidence)
            resp.raise_for_status()
            return cast("str", resp.json()["evidence_id"])

        from trellis.schemas.evidence import Evidence  # noqa: PLC0415

        e = Evidence.model_validate(evidence)
        registry = self._get_registry()
        registry.document_store.put(
            doc_id=e.evidence_id,
            content=e.content or "",
            metadata={
                "evidence_type": e.evidence_type,
                "source_origin": e.source_origin,
            },
        )
        return str(e.evidence_id)

    # -- Retrieve --

    def search(
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
            resp = self._get_http().get("/api/v1/search", params=params)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return cast("list[dict[str, Any]]", data.get("results", []))

        registry = self._get_registry()
        filters: dict[str, Any] = {}
        if domain:
            filters["domain"] = domain
        results = registry.document_store.search(query, limit=limit, filters=filters)
        return cast("list[dict[str, Any]]", results)

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        """Get a trace by ID."""
        if self.is_remote:
            resp = self._get_http().get(f"/api/v1/traces/{trace_id}")
            if resp.status_code == _HTTP_NOT_FOUND:
                return None
            resp.raise_for_status()
            return cast("dict[str, Any] | None", resp.json().get("trace"))

        registry = self._get_registry()
        trace = registry.trace_store.get(trace_id)
        dumped = trace.model_dump(mode="json") if trace else None
        return cast("dict[str, Any] | None", dumped)

    def list_traces(
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
            resp = self._get_http().get("/api/v1/traces", params=params)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return cast("list[dict[str, Any]]", data.get("traces", []))

        registry = self._get_registry()
        traces = registry.trace_store.query(domain=domain, limit=limit)
        return [t.to_summary_dict() for t in traces]

    def assemble_pack(
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
            resp = self._get_http().post(
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

        from trellis.retrieve.pack_builder import PackBuilder  # noqa: PLC0415
        from trellis.retrieve.strategies import (  # noqa: PLC0415
            GraphSearch,
            KeywordSearch,
        )
        from trellis.schemas.pack import PackBudget  # noqa: PLC0415

        registry = self._get_registry()
        builder = PackBuilder(
            strategies=[
                KeywordSearch(registry.document_store),
                GraphSearch(registry.graph_store),
            ]
        )
        budget = PackBudget(max_items=max_items, max_tokens=max_tokens)
        pack = builder.build(
            intent=intent, domain=domain, agent_id=agent_id, budget=budget
        )
        return {
            "pack_id": pack.pack_id,
            "intent": pack.intent,
            "domain": pack.domain,
            "agent_id": pack.agent_id,
            "count": len(pack.items),
            "items": [item.model_dump() for item in pack.items],
            "advisories": [a.model_dump(mode="json") for a in pack.advisories],
        }

    def assemble_sectioned_pack(
        self,
        intent: str,
        sections: list[dict[str, Any]],
        *,
        domain: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Assemble a sectioned pack with independently budgeted sections.

        Args:
            intent: Natural language query.
            sections: List of section configs, each with keys:
                name, retrieval_affinities, content_types, scopes, entity_ids,
                max_tokens, max_items.
            domain: Optional domain filter.
            agent_id: Optional agent identifier.

        Returns:
            Dict representation of a SectionedPack.
        """
        if self.is_remote:
            try:
                resp = self._get_http().post(
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
                flat = self.assemble_pack(intent, domain=domain, agent_id=agent_id)
                return cast("dict[str, Any]", flat)

        from trellis.retrieve.pack_builder import PackBuilder  # noqa: PLC0415
        from trellis.retrieve.strategies import (  # noqa: PLC0415
            GraphSearch,
            KeywordSearch,
        )
        from trellis.schemas.pack import SectionRequest  # noqa: PLC0415

        registry = self._get_registry()
        builder = PackBuilder(
            strategies=[
                KeywordSearch(registry.document_store),
                GraphSearch(registry.graph_store),
            ]
        )
        section_requests = [SectionRequest.model_validate(s) for s in sections]
        pack = builder.build_sectioned(
            intent=intent,
            sections=section_requests,
            domain=domain,
            agent_id=agent_id,
        )
        return cast("dict[str, Any]", pack.model_dump())

    def get_objective_context(
        self,
        intent: str,
        *,
        domain: str | None = None,
        max_tokens: int = 4000,
    ) -> str:
        """Get objective-level context formatted as markdown."""
        from trellis.retrieve.formatters import (  # noqa: PLC0415
            format_sectioned_pack_as_markdown,
        )

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
        pack_sections = pack.get("sections", [])
        return format_sectioned_pack_as_markdown(
            pack_sections, intent, max_tokens=max_tokens
        )

    def get_task_context(
        self,
        intent: str,
        *,
        entity_ids: list[str] | None = None,
        domain: str | None = None,
        max_tokens: int = 4000,
    ) -> str:
        """Get task-level context formatted as markdown."""
        from trellis.retrieve.formatters import (  # noqa: PLC0415
            format_sectioned_pack_as_markdown,
        )

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
        pack_sections = pack.get("sections", [])
        return format_sectioned_pack_as_markdown(
            pack_sections, intent, max_tokens=max_tokens
        )

    def get_entity(self, entity_id: str) -> dict[str, Any] | None:
        """Get an entity by ID."""
        if self.is_remote:
            resp = self._get_http().get(f"/api/v1/entities/{entity_id}")
            if resp.status_code == _HTTP_NOT_FOUND:
                return None
            resp.raise_for_status()
            return cast("dict[str, Any] | None", resp.json().get("entity"))

        registry = self._get_registry()
        return cast("dict[str, Any] | None", registry.graph_store.get_node(entity_id))

    # -- Curate --

    def create_entity(
        self,
        name: str,
        entity_type: str = "concept",
        properties: dict[str, Any] | None = None,
    ) -> str:
        """Create an entity. Returns the node_id."""
        if self.is_remote:
            resp = self._get_http().post(
                "/api/v1/entities",
                json={
                    "entity_type": entity_type,
                    "name": name,
                    "properties": properties or {},
                },
            )
            resp.raise_for_status()
            return cast("str", resp.json()["node_id"])

        registry = self._get_registry()
        props = dict(properties or {})
        props["name"] = name
        return cast(
            "str",
            registry.graph_store.upsert_node(
                node_id=None, node_type=entity_type, properties=props
            ),
        )

    def create_link(
        self,
        source_id: str,
        target_id: str,
        edge_kind: str = "entity_related_to",
    ) -> str:
        """Create a link between entities. Returns the edge_id."""
        if self.is_remote:
            resp = self._get_http().post(
                "/api/v1/links",
                json={
                    "source_id": source_id,
                    "target_id": target_id,
                    "edge_kind": edge_kind,
                },
            )
            resp.raise_for_status()
            return cast("str", resp.json()["edge_id"])

        registry = self._get_registry()
        return cast(
            "str",
            registry.graph_store.upsert_edge(
                source_id=source_id,
                target_id=target_id,
                edge_type=edge_kind,
            ),
        )

    # -- Lifecycle --

    def close(self) -> None:
        """Close any open connections."""
        if self._http is not None:
            self._http.close()
            self._http = None
        if self._registry is not None:
            self._registry.close()
            self._registry = None
