"""Asynchronous Trellis SDK client — HTTP only with bounded concurrency.

Like :class:`TrellisClient`, the async variant is HTTP-only after the
Step 3 refactor.  It adds two concurrency primitives the sync client
doesn't need:

* A bounded :class:`asyncio.Semaphore` that caps in-flight requests
  per client instance.  Default ``max_concurrency=16`` is a reasonable
  ceiling for a single agent; bump explicitly when fanning out.
* Typed ``429`` / ``Retry-After`` surfacing via
  :class:`trellis_sdk.exceptions.TrellisRateLimitError` so callers
  can implement their own backoff policy.

See :func:`trellis.testing.in_memory_async_client` for an
ASGI-transport fixture that drops the network entirely in tests.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import httpx
import structlog

from trellis_sdk._format import format_sectioned_pack_as_markdown
from trellis_sdk._http import (
    SDK_API_MAJOR,
    SDK_API_MINOR,
    check_handshake,
    raise_for_status,
    wrap_transport_error,
)
from trellis_wire import (
    BatchStrategy,
    DraftSubmissionRequest,
    DraftSubmissionResult,
    ExtractionBatch,
    PackFeedbackRequest,
    PackFeedbackResponse,
)

if TYPE_CHECKING:
    from types import TracebackType

logger = structlog.get_logger(__name__)

_HTTP_NOT_FOUND = 404
_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_CONCURRENCY = 16


class AsyncTrellisClient:
    """Async HTTP client for the Trellis REST API.

    Example::

        async with AsyncTrellisClient("http://localhost:8420") as client:
            await client.ingest_trace(trace)

    ``max_concurrency`` bounds how many requests can be in flight
    from a single client instance.  Raise it for parallel fan-out
    workloads; lower it to be gentle on shared infrastructure.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
        verify_version: bool = True,
    ) -> None:
        if base_url is None and http is None:
            msg = (
                "AsyncTrellisClient requires either base_url= or http=. "
                "In-process mode was removed in Step 3 — use "
                "trellis.testing.in_memory_async_client() for test fixtures."
            )
            raise ValueError(msg)
        if http is not None and base_url is not None:
            msg = "Pass base_url OR http, not both."
            raise ValueError(msg)

        self._owns_http = http is None
        if http is not None:
            self._http = http
        else:
            self._http = httpx.AsyncClient(
                base_url=cast("str", base_url).rstrip("/"),
                timeout=timeout,
            )
        self._verify_version = verify_version
        self._handshake_done = False
        self._handshake_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._max_concurrency = max_concurrency

    # -- Context manager --

    async def __aenter__(self) -> AsyncTrellisClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # -- Introspection --

    @property
    def max_concurrency(self) -> int:
        """The upper bound on in-flight requests from this client."""
        return self._max_concurrency

    # -- Internals --

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        await self._ensure_handshake()
        try:
            async with self._semaphore:
                resp = await self._http.request(
                    method, path, json=json, params=params, headers=headers
                )
        except httpx.HTTPError as exc:
            # Network/transport-level failure — no response was received.
            # Wrap into a typed SDK exception so callers don't have to
            # know about httpx.
            raise wrap_transport_error(exc, request_path=path) from exc
        raise_for_status(resp, request_path=path)
        return resp

    async def _ensure_handshake(self) -> None:
        if self._handshake_done or not self._verify_version:
            return
        async with self._handshake_lock:
            if self._handshake_done:
                return
            try:
                resp = await self._http.get("/api/version")
            except httpx.HTTPError as exc:
                # Handshake transport failure surfaces as a typed
                # TrellisTransportError. Pre-Phase-6 this was silently
                # swallowed, which masked DNS/connection failures
                # behind cryptic "method not allowed" errors on the
                # subsequent real call.
                logger.warning(
                    "sdk_async_handshake_transport_failed",
                    request_path="/api/version",
                    error=str(exc),
                )
                raise wrap_transport_error(exc, request_path="/api/version") from exc
            if resp.status_code != 200:  # noqa: PLR2004
                # Older servers may not expose ``/api/version`` at all —
                # documented graceful degradation: skip the version
                # check rather than blocking every subsequent call.
                # The version check is best-effort; the real failure
                # mode (incompatible API) surfaces on the next call
                # with full context.
                logger.debug(
                    "sdk_async_handshake_endpoint_missing",
                    status_code=resp.status_code,
                )
                return
            check_handshake(resp.json())
            self._handshake_done = True
            logger.debug(
                "sdk_async_handshake_ok",
                sdk_api_major=SDK_API_MAJOR,
                sdk_api_minor=SDK_API_MINOR,
            )

    # -- Ingest --

    async def ingest_trace(self, trace: dict[str, Any]) -> str:
        resp = await self._request("POST", "/api/v1/traces", json=trace)
        return cast("str", resp.json()["trace_id"])

    async def ingest_evidence(self, evidence: dict[str, Any]) -> str:
        resp = await self._request("POST", "/api/v1/evidence", json=evidence)
        return cast("str", resp.json()["evidence_id"])

    # -- Retrieve --

    async def search(
        self,
        query: str,
        *,
        domain: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"q": query, "limit": limit}
        if domain:
            params["domain"] = domain
        resp = await self._request("GET", "/api/v1/search", params=params)
        return cast("list[dict[str, Any]]", resp.json().get("results", []))

    async def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        await self._ensure_handshake()
        path = f"/api/v1/traces/{trace_id}"
        try:
            async with self._semaphore:
                resp = await self._http.get(path)
        except httpx.HTTPError as exc:
            raise wrap_transport_error(exc, request_path=path) from exc
        if resp.status_code == _HTTP_NOT_FOUND:
            return None
        raise_for_status(resp, request_path=path)
        return cast("dict[str, Any] | None", resp.json().get("trace"))

    async def list_traces(
        self,
        *,
        domain: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if domain:
            params["domain"] = domain
        resp = await self._request("GET", "/api/v1/traces", params=params)
        return cast("list[dict[str, Any]]", resp.json().get("traces", []))

    async def assemble_pack(
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
        resp = await self._request("POST", "/api/v1/packs", json=payload)
        return cast("dict[str, Any]", resp.json())

    async def assemble_sectioned_pack(
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
        resp = await self._request("POST", "/api/v1/packs/sectioned", json=payload)
        return cast("dict[str, Any]", resp.json())

    async def get_objective_context(
        self,
        intent: str,
        *,
        domain: str | None = None,
        max_tokens: int = 4000,
    ) -> str:
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
        pack = await self.assemble_sectioned_pack(
            intent, sections, domain=domain, agent_id="objective"
        )
        return format_sectioned_pack_as_markdown(
            pack.get("sections", []),
            intent,
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
        pack = await self.assemble_sectioned_pack(
            intent, sections, domain=domain, agent_id="task"
        )
        return format_sectioned_pack_as_markdown(
            pack.get("sections", []),
            intent,
            max_tokens=max_tokens,
        )

    async def get_entity(self, entity_id: str) -> dict[str, Any] | None:
        await self._ensure_handshake()
        path = f"/api/v1/entities/{entity_id}"
        try:
            async with self._semaphore:
                resp = await self._http.get(path)
        except httpx.HTTPError as exc:
            raise wrap_transport_error(exc, request_path=path) from exc
        if resp.status_code == _HTTP_NOT_FOUND:
            return None
        raise_for_status(resp, request_path=path)
        return cast("dict[str, Any] | None", resp.json().get("entity"))

    # -- Curate --

    async def create_entity(
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
        resp = await self._request("POST", "/api/v1/entities", json=payload)
        return cast("str", resp.json()["node_id"])

    async def create_link(
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
        resp = await self._request("POST", "/api/v1/links", json=payload)
        return cast("str", resp.json()["edge_id"])

    async def record_feedback(
        self,
        pack_id: str,
        success: bool,
        *,
        helpful_item_ids: list[str] | None = None,
        unhelpful_item_ids: list[str] | None = None,
        followed_advisory_ids: list[str] | None = None,
        target_id: str | None = None,
        rating: float | None = None,
        comment: str | None = None,
    ) -> PackFeedbackResponse:
        """Async variant of :meth:`TrellisClient.record_feedback`.

        Records element-level feedback on a context pack. The server
        routes the signal through
        :func:`trellis.feedback.recording.record_feedback`, appending the
        durable ``pack_feedback.jsonl`` row and emitting the authoritative
        ``FEEDBACK_RECORDED`` event to the operational EventLog. See the
        sync docstring for full parameter semantics.

        Returns:
            :class:`~trellis_wire.PackFeedbackResponse`. Check
            ``event_log_in_sync`` to confirm the authoritative event
            reached the log — ``False`` means only the JSONL audit row
            landed and a reconcile is owed.
        """
        body = PackFeedbackRequest(
            success=success,
            helpful_item_ids=helpful_item_ids or [],
            unhelpful_item_ids=unhelpful_item_ids or [],
            followed_advisory_ids=followed_advisory_ids or [],
            target_id=target_id,
            rating=rating,
            comment=comment,
        )
        resp = await self._request(
            "POST",
            f"/api/v1/packs/{pack_id}/feedback",
            json=body.model_dump(mode="json"),
        )
        return PackFeedbackResponse.model_validate(resp.json())

    # -- Observations + Measurements (Item 1 Phase 1) --

    async def record_observation(self, observation: dict[str, Any]) -> str:
        """POST /api/v1/observations. Returns the new ``observation_id``."""
        resp = await self._request("POST", "/api/v1/observations", json=observation)
        return cast("str", resp.json()["observation_id"])

    async def query_observations(
        self,
        *,
        subject_entity_id: str | None = None,
        observer_agent_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """GET /api/v1/observations."""
        params: dict[str, Any] = {"limit": limit}
        if subject_entity_id is not None:
            params["subject_entity_id"] = subject_entity_id
        if observer_agent_id is not None:
            params["observer_agent_id"] = observer_agent_id
        resp = await self._request("GET", "/api/v1/observations", params=params)
        return cast("list[dict[str, Any]]", resp.json().get("observations", []))

    async def record_measurement(self, measurement: dict[str, Any]) -> str:
        """POST /api/v1/measurements. Returns the new ``measurement_id``."""
        resp = await self._request("POST", "/api/v1/measurements", json=measurement)
        return cast("str", resp.json()["measurement_id"])

    async def query_measurements(
        self,
        *,
        subject_entity_id: str | None = None,
        metric_name: str | None = None,
        observer_agent_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """GET /api/v1/measurements."""
        params: dict[str, Any] = {"limit": limit}
        if subject_entity_id is not None:
            params["subject_entity_id"] = subject_entity_id
        if metric_name is not None:
            params["metric_name"] = metric_name
        if observer_agent_id is not None:
            params["observer_agent_id"] = observer_agent_id
        resp = await self._request("GET", "/api/v1/measurements", params=params)
        return cast("list[dict[str, Any]]", resp.json().get("measurements", []))

    # -- Extract (client-side extractor contract) --

    async def submit_drafts(
        self,
        batch: ExtractionBatch,
        *,
        strategy: BatchStrategy = BatchStrategy.CONTINUE_ON_ERROR,
        requested_by: str | None = None,
        idempotency_key: str | None = None,
    ) -> DraftSubmissionResult:
        """Async variant of :meth:`TrellisClient.submit_drafts`.

        Uses the bounded concurrency semaphore like any other request.
        See the sync docstring for semantics of ``idempotency_key`` +
        ``requested_by``.
        """
        body = DraftSubmissionRequest(
            batch=batch,
            strategy=strategy,
            requested_by=requested_by,
        )
        headers: dict[str, str] = {}
        effective_key = idempotency_key or batch.idempotency_key
        if effective_key:
            headers["Idempotency-Key"] = effective_key
        resp = await self._request(
            "POST",
            "/api/v1/extract/drafts",
            json=body.model_dump(mode="json"),
            headers=headers or None,
        )
        return DraftSubmissionResult.model_validate(resp.json())

    # -- Lifecycle --

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()
