"""Tests for the ``/api/v1/classify/candidates`` and ``/classify/rules`` routes.

Mirrors ``tests/unit/api/test_settings_routes.py``'s fixture pattern exactly
(a fresh in-memory ``StoreRegistry`` bound to ``trellis_api.app``, and a
``TestClient`` that is never entered as a context manager so the lifespan
never re-inits ``_registry`` out from under the fixture).

Covers the governed Apply/Revoke review flow end to end: overlay
precedence with no restart required, revoke-works and revoke-refused (both
a genuinely-unknown key and a config.yaml-sourced one), the audit event's
actor/metrics, non-admin 403 on every write route and on ``GET /candidates``
(its text is mined from every document), ``GET /candidates``
never invoking a miner, and a dismissed candidate dropping out of the open
listing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import SCOPE_ADMIN, SCOPE_READ, generate_api_key
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app


@pytest.fixture
def registry(tmp_path: Path):
    """Fresh registry bound to the app module for each test."""
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry: StoreRegistry) -> TestClient:
    """In-memory app over the fixture registry. Not context-managed."""
    app = create_app()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.delenv("TRELLIS_AUTH_MODE", raising=False)


def _mint(registry: StoreRegistry, scopes: list[str], name: str = "test-key") -> str:
    token, record = generate_api_key(name, scopes)
    registry.operational.api_key_store.create(record)
    return token


def _mint_with_key_id(
    registry: StoreRegistry, scopes: list[str], name: str = "test-key"
) -> tuple[str, str]:
    token, record = generate_api_key(name, scopes)
    registry.operational.api_key_store.create(record)
    return token, record.key_id


def _emit_tag_keyword_candidate(
    registry: StoreRegistry,
    *,
    candidate_id: str = "cand-1",
    keyword: str = "widget",
    tag: str = "manufacturing",
) -> str:
    event = registry.operational.event_log.emit(
        EventType.TAG_KEYWORD_CANDIDATE,
        source="test",
        entity_id=candidate_id,
        entity_type="tag_keyword",
        payload={
            "candidate_id": candidate_id,
            "keyword": keyword,
            "tag": tag,
            "support": 40,
            "precision": 0.8,
            "lift": 0.3,
            "recurrence_count": 1,
            "example_count": 3,
            "notes": [],
        },
    )
    return event.event_id


def _rule_row(body: dict[str, Any], kind: str, key: str) -> dict[str, Any]:
    (match,) = (
        row for row in body["rules"] if row["kind"] == kind and row["key"] == key
    )
    return match


# ---------------------------------------------------------------------------
# GET /classify/candidates — never mines, dedupes, reflects dismiss/apply
# ---------------------------------------------------------------------------


class TestListCandidates:
    def test_empty_when_no_events(self, client: TestClient) -> None:
        resp = client.get("/api/v1/classify/candidates")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "count": 0,
            "candidates": [],
            "store_degradation": None,
        }

    def test_does_not_invoke_a_miner(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A GET must only read events a prior mine run emitted."""

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            msg = "GET /classify/candidates must never mine"
            raise AssertionError(msg)

        monkeypatch.setattr(
            "trellis_api.routes.classify_rules.analyze_tag_keyword_candidates", _boom
        )
        monkeypatch.setattr(
            "trellis_api.routes.classify_rules.analyze_domain_alias_candidates", _boom
        )
        resp = client.get("/api/v1/classify/candidates")
        assert resp.status_code == 200, resp.text

    def test_open_candidate_is_listed(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        _emit_tag_keyword_candidate(registry)
        resp = client.get("/api/v1/classify/candidates")
        body = resp.json()
        assert body["count"] == 1
        row = body["candidates"][0]
        assert row["candidate_id"] == "cand-1"
        assert row["kind"] == "tag_keyword"
        assert row["key"] == "widget"
        assert row["target"] == "manufacturing"
        assert row["status"] == "open"
        assert row["metrics"]["support"] == 40
        assert row["metrics"]["precision"] == 0.8

    def test_dismissed_candidate_is_excluded_from_open(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        _emit_tag_keyword_candidate(registry)
        resp = client.post("/api/v1/classify/candidates/cand-1/dismiss")
        assert resp.status_code == 200, resp.text

        body = client.get("/api/v1/classify/candidates").json()
        assert body["count"] == 1
        assert body["candidates"][0]["status"] == "dismissed"
        assert not any(
            c["status"] == "open" and c["candidate_id"] == "cand-1"
            for c in body["candidates"]
        )

    def test_dismiss_missing_candidate_is_404(self, client: TestClient) -> None:
        resp = client.post("/api/v1/classify/candidates/does-not-exist/dismiss")
        assert resp.status_code == 404

    def test_dismiss_nul_in_candidate_id_is_400_not_500(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        """Issue #869 F4: a NUL reaching the audit payload used to fail

        inside ``event_log.emit`` (a Postgres ``jsonb`` column refuses
        ``\\u0000`` outright -- SQLite accepts it, which is why this
        failure mode was invisible to the default test run) and surface
        as an uncaught 500. The route now checks the payload itself
        before emitting, so the refusal is a clean 400 -- on every
        backend, not just Postgres -- and nothing is ever written.
        """
        candidate_id = "bad\x00cand"
        _emit_tag_keyword_candidate(registry, candidate_id=candidate_id)

        # The NUL must be percent-encoded to form a valid URL at all (an
        # HTTP client refuses a raw NUL in the request line); FastAPI
        # decodes it straight back to ``"\x00"`` in the path parameter,
        # which is the shape this guard exists to catch.
        resp = client.post(
            "/api/v1/classify/candidates/bad%00cand/dismiss",
        )
        assert resp.status_code == 400, resp.text
        assert "NUL" in resp.json()["detail"]

        events = registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_CANDIDATE_DISMISSED, order="asc", limit=10
        )
        assert events == []

    def test_dismiss_nul_in_candidate_id_never_reaches_event_log_lookup(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """On Postgres, a NUL in ``candidate_id`` never reaches a query.

        ``test_dismiss_nul_in_candidate_id_is_400_not_500`` proves the 400
        on SQLite, which has no client-side NUL check and so cannot tell
        "refused before the query" apart from "refused after it fails".
        psycopg does refuse a NUL parameter client-side, with its own
        uncaught error ("PostgreSQL text fields cannot contain NUL (0x00)
        bytes") -- so if the route still queried the event log *before*
        checking ``candidate_id`` (the old order), this would surface as
        an uncaught 500 on Postgres even though the route's own guard
        exists and is reachable. This wraps ``get_events`` to raise the
        way psycopg does on any NUL-bearing string argument, without
        requiring the optional psycopg dependency, and asserts both the
        400 and that the wrapped method was never called at all.
        """
        calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        original_get_events = registry.operational.event_log.get_events
        psycopg_nul_message = "PostgreSQL text fields cannot contain NUL (0x00) bytes"

        def psycopg_shaped_get_events(*args: Any, **kwargs: Any) -> Any:
            for value in (*args, *kwargs.values()):
                if isinstance(value, str) and "\x00" in value:
                    raise RuntimeError(psycopg_nul_message)
            calls.append((args, kwargs))
            return original_get_events(*args, **kwargs)

        monkeypatch.setattr(
            registry.operational.event_log, "get_events", psycopg_shaped_get_events
        )

        resp = client.post(
            "/api/v1/classify/candidates/bad%00cand/dismiss",
        )
        assert resp.status_code == 400, resp.text
        assert "NUL" in resp.json()["detail"]
        assert calls == [], "the event log must never be queried with the bad value"

    def test_dismiss_spreads_identity_and_dismissed_by_prefers_key_id(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``CLASSIFY_CANDIDATE_DISMISSED``'s payload spreads
        ``_audit_identity(ctx)`` (``key_id``/``key_name``) alongside
        ``dismissed_by``, and with a real authenticated caller
        ``dismissed_by`` is that caller's ``key_id`` -- not the
        ``"unknown"`` fallback that only applies with no identity at
        all."""
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        token, key_id = _mint_with_key_id(registry, [SCOPE_ADMIN])
        _emit_tag_keyword_candidate(registry)

        resp = client.post(
            "/api/v1/classify/candidates/cand-1/dismiss",
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 200, resp.text

        events = registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_CANDIDATE_DISMISSED, order="asc", limit=10
        )
        payload = events[-1].payload
        assert payload["key_id"] == key_id
        assert payload["dismissed_by"] == key_id

    def test_dismiss_nul_in_key_name_is_400_not_500(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same issue #869 F4 guard, reached through ``key_name`` instead

        of ``candidate_id``: an admin key minted with a NUL in its own
        name (the ``dismissed_by``/``key_name`` fallback when no
        ``key_id`` -- here both are present, but the payload walk checks
        every field) lands in the audit payload the same way.
        """
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        token, record = generate_api_key("bad\x00name", [SCOPE_ADMIN])
        registry.operational.api_key_store.create(record)
        _emit_tag_keyword_candidate(registry)

        resp = client.post(
            "/api/v1/classify/candidates/cand-1/dismiss",
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 400, resp.text
        assert "NUL" in resp.json()["detail"]

        events = registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_CANDIDATE_DISMISSED, order="asc", limit=10
        )
        assert events == []

    def test_applied_candidate_reads_status_applied(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        event_id = _emit_tag_keyword_candidate(registry)
        apply_resp = client.post(
            "/api/v1/classify/rules",
            json={
                "kind": "tag_keyword",
                "key": "widget",
                "target": "manufacturing",
                "source_event_id": event_id,
            },
        )
        assert apply_resp.status_code == 200, apply_resp.text

        body = client.get("/api/v1/classify/candidates").json()
        assert body["candidates"][0]["status"] == "applied"


class TestListCandidatesIsAdminOnly:
    """The listing serves keyword and alias text mined from every document.

    The payloads carry no document ids, so a read key's clearance cannot
    filter them per row; the route is admin-scoped instead (#1029).
    """

    def test_read_key_is_403_and_sees_no_candidate_text(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _emit_tag_keyword_candidate(registry, keyword="zebracrossing")
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        token = _mint(registry, [SCOPE_READ])
        resp = client.get("/api/v1/classify/candidates", headers={"X-API-Key": token})
        assert resp.status_code == 403
        assert "zebracrossing" not in resp.text

    def test_missing_key_is_401(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        assert client.get("/api/v1/classify/candidates").status_code == 401

    def test_admin_key_gets_the_same_rows_as_an_unauthenticated_default_client(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _emit_tag_keyword_candidate(registry, keyword="zebracrossing")
        before = client.get("/api/v1/classify/candidates")
        assert before.status_code == 200, before.text

        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        token = _mint(registry, [SCOPE_ADMIN])
        resp = client.get("/api/v1/classify/candidates", headers={"X-API-Key": token})
        assert resp.status_code == 200, resp.text
        assert resp.json() == before.json()
        assert resp.json()["candidates"][0]["key"] == "zebracrossing"

    def test_rules_listing_stays_readable_with_a_read_key(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only the candidate text is mined from documents; rules are config."""
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        token = _mint(registry, [SCOPE_READ])
        resp = client.get("/api/v1/classify/rules", headers={"X-API-Key": token})
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# POST /classify/candidates/mine — admin only, defaulted thresholds
# ---------------------------------------------------------------------------


class TestMineCandidates:
    def test_mine_with_recommended_defaults_runs_clean_on_empty_corpus(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/api/v1/classify/candidates/mine", json={"kinds": ["tag_keyword"]}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["tag_keyword_count"] == 0
        assert body["domain_alias_count"] == 0
        assert "learning.tag_evolution" in body["used_recommended_defaults"]

    def test_unknown_kind_is_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/classify/candidates/mine", json={"kinds": ["not_a_kind"]}
        )
        assert resp.status_code == 400

    def test_empty_kinds_is_400(self, client: TestClient) -> None:
        resp = client.post("/api/v1/classify/candidates/mine", json={"kinds": []})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /classify/rules — apply, governed through MutationExecutor
# ---------------------------------------------------------------------------


class TestApplyRule:
    def test_apply_creates_overlay_row_visible_without_restart(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/api/v1/classify/rules",
            json={
                "kind": "tag_keyword",
                "key": "xyz",
                "target": "finance",
                "source_event_id": "01TESTEVENT0000000000000",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "success"
        assert body["created_id"] == "tag_keyword:xyz"

        # Same process, same request cycle, no restart -- the whole point
        # of re-reading the overlay store per call rather than caching it.
        rules = client.get("/api/v1/classify/rules").json()
        row = _rule_row(rules, "tag_keyword", "xyz")
        assert row["source"] == "overlay"
        assert row["target"] == "finance"
        assert row["source_event_id"] == "01TESTEVENT0000000000000"

    def test_audit_event_carries_actor_and_metrics(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        token, key_id = _mint_with_key_id(registry, [SCOPE_ADMIN])
        resp = client.post(
            "/api/v1/classify/rules",
            json={
                "kind": "tag_keyword",
                "key": "xyz",
                "target": "finance",
                "source_event_id": "01TESTEVENT0000000000000",
                "support": 40,
                "precision": 0.8,
                "lift": 0.3,
            },
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 200, resp.text

        events = registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_APPLIED, order="asc", limit=10
        )
        payload = events[-1].payload
        assert payload["applied_by"] == key_id
        assert payload["support"] == 40
        assert payload["precision"] == 0.8
        assert payload["lift"] == 0.3

    def test_anonymous_apply_pins_the_constant_requested_by_label(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        """F1: pins M21. With no caller identity at all (the default
        ``client`` fixture: ``TRELLIS_AUTH_MODE`` unset, no API key
        header), ``_audit_identity(ctx)`` is ``{"key_id": None,
        "key_name": None}``, so ``applied_by``'s
        ``key_id or key_name or requested_by`` fallback resolves to
        ``command.requested_by`` verbatim -- the only path that reveals
        whether the route still passes the literal
        ``"api:classify.apply-rule"`` rather than embedding the (here,
        absent) caller identity into it."""
        resp = client.post(
            "/api/v1/classify/rules",
            json={
                "kind": "tag_keyword",
                "key": "xyz",
                "target": "finance",
                "source_event_id": "01TESTEVENT0000000000000",
            },
        )
        assert resp.status_code == 200, resp.text

        events = registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_APPLIED, order="asc", limit=10
        )
        assert events[-1].payload["applied_by"] == "api:classify.apply-rule"

    def test_apply_missing_fields_is_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/classify/rules", json={"kind": "tag_keyword"})
        assert resp.status_code == 422

    def test_non_admin_key_is_403(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        token = _mint(registry, [SCOPE_READ])
        resp = client.post(
            "/api/v1/classify/rules",
            json={
                "kind": "tag_keyword",
                "key": "xyz",
                "target": "finance",
                "source_event_id": "01TESTEVENT0000000000000",
            },
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 403

    def test_self_mapping_alias_is_refused_4xx(self, client: TestClient) -> None:
        """``a -> a`` is a no-op that reads as intent -- refused at apply
        time, before it ever reaches the overlay file."""
        resp = client.post(
            "/api/v1/classify/rules",
            json={
                "kind": "domain_alias",
                "key": "widgets",
                "target": "widgets",
                "source_event_id": "01TESTEVENT0000000000000",
            },
        )
        assert resp.status_code == 400, resp.text
        assert "itself" in resp.json()["detail"]

        rules = client.get("/api/v1/classify/rules").json()
        assert not any(
            r["kind"] == "domain_alias" and r["key"] == "widgets"
            for r in rules["rules"]
        )

    def test_chained_alias_is_refused_4xx(self, client: TestClient) -> None:
        """Applying ``a -> b`` when ``b -> c`` is already applied would
        introduce a transitive chain ``normalize_domain_tags`` never
        performs -- refused, and the first row is left untouched."""
        first = client.post(
            "/api/v1/classify/rules",
            json={
                "kind": "domain_alias",
                "key": "b-alias",
                "target": "c-canonical",
                "source_event_id": "01TESTEVENT0000000000000",
            },
        )
        assert first.status_code == 200, first.text

        chained = client.post(
            "/api/v1/classify/rules",
            json={
                "kind": "domain_alias",
                "key": "a-alias",
                "target": "b-alias",
                "source_event_id": "01TESTEVENT0000000000000",
            },
        )
        assert chained.status_code == 400, chained.text
        assert "chain" in chained.json()["detail"].lower() or (
            "both an alias and a merge destination" in chained.json()["detail"]
        )

        rules = client.get("/api/v1/classify/rules").json()
        assert not any(
            r["kind"] == "domain_alias" and r["key"] == "a-alias"
            for r in rules["rules"]
        )

    def test_reserved_namespace_target_is_refused_4xx_for_tag_keyword(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/api/v1/classify/rules",
            json={
                "kind": "tag_keyword",
                "key": "secretword",
                "target": "sensitivity",
                "source_event_id": "01TESTEVENT0000000000000",
            },
        )
        assert resp.status_code == 400, resp.text
        assert "reserved namespace" in resp.json()["detail"]

    def test_reserved_namespace_target_is_refused_4xx_for_domain_alias(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/api/v1/classify/rules",
            json={
                "kind": "domain_alias",
                "key": "secretalias",
                "target": "lifecycle",
                "source_event_id": "01TESTEVENT0000000000000",
            },
        )
        assert resp.status_code == 400, resp.text
        assert "reserved namespace" in resp.json()["detail"]

    def test_reserved_namespace_alias_key_is_refused_4xx(
        self, client: TestClient
    ) -> None:
        """``sensitivity -> privacy`` was previously accepted: only the
        clean *target* was checked against the reserved namespace, never
        the alias *key*, which becomes a domain name of its own once
        merged (the alias side of ``alias -> canonical``)."""
        resp = client.post(
            "/api/v1/classify/rules",
            json={
                "kind": "domain_alias",
                "key": "sensitivity",
                "target": "privacy",
                "source_event_id": "01TESTEVENT0000000000000",
            },
        )
        assert resp.status_code == 400, resp.text
        assert "reserved namespace" in resp.json()["detail"]

        rules = client.get("/api/v1/classify/rules").json()
        assert not any(
            r["kind"] == "domain_alias" and r["key"] == "sensitivity"
            for r in rules["rules"]
        )

    def test_nan_precision_is_422_and_writes_no_row(self, client: TestClient) -> None:
        """``allow_inf_nan=False`` on the DTO refuses at parse time, before
        the governed pipeline is ever entered -- standard JSON has no NaN
        token, but Python's own encoder (and many JS clients) emit the
        bare ``NaN``/``Infinity`` literal, which FastAPI's JSON parsing
        otherwise accepts as an extension."""
        resp = client.post(
            "/api/v1/classify/rules",
            content=(
                b'{"kind": "tag_keyword", "key": "xyz", "target": "finance", '
                b'"source_event_id": "01TESTEVENT0000000000000", '
                b'"precision": NaN}'
            ),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422, resp.text

        rules = client.get("/api/v1/classify/rules").json()
        assert not any(
            r["kind"] == "tag_keyword" and r["key"] == "xyz" for r in rules["rules"]
        )

    def test_infinite_lift_is_422_and_writes_no_row(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/classify/rules",
            content=(
                b'{"kind": "tag_keyword", "key": "xyz", "target": "finance", '
                b'"source_event_id": "01TESTEVENT0000000000000", '
                b'"lift": Infinity}'
            ),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422, resp.text

        rules = client.get("/api/v1/classify/rules").json()
        assert not any(
            r["kind"] == "tag_keyword" and r["key"] == "xyz" for r in rules["rules"]
        )

    def test_negative_support_is_422_and_writes_no_row(
        self, client: TestClient
    ) -> None:
        """``support`` is a count of matching observations -- negative is
        meaningless, and the DTO's ``ge=0`` refuses it before the
        governed pipeline is ever entered."""
        resp = client.post(
            "/api/v1/classify/rules",
            json={
                "kind": "tag_keyword",
                "key": "xyz",
                "target": "finance",
                "source_event_id": "01TESTEVENT0000000000000",
                "support": -1,
            },
        )
        assert resp.status_code == 422, resp.text

        rules = client.get("/api/v1/classify/rules").json()
        assert not any(
            r["kind"] == "tag_keyword" and r["key"] == "xyz" for r in rules["rules"]
        )

    def test_precision_above_one_is_422_and_writes_no_row(
        self, client: TestClient
    ) -> None:
        """``precision`` is a proportion -- over 1.0 is meaningless, and
        the DTO's ``le=1.0`` refuses it before the governed pipeline is
        ever entered."""
        resp = client.post(
            "/api/v1/classify/rules",
            json={
                "kind": "tag_keyword",
                "key": "xyz",
                "target": "finance",
                "source_event_id": "01TESTEVENT0000000000000",
                "precision": 1.5,
            },
        )
        assert resp.status_code == 422, resp.text

        rules = client.get("/api/v1/classify/rules").json()
        assert not any(
            r["kind"] == "tag_keyword" and r["key"] == "xyz" for r in rules["rules"]
        )

    def test_audit_event_records_prior_overlay_value_on_reapply(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        """Re-applying an already-applied key must record what it
        replaces, not just what it now is."""
        first = client.post(
            "/api/v1/classify/rules",
            json={
                "kind": "tag_keyword",
                "key": "xyz",
                "target": "finance",
                "source_event_id": "01TESTEVENT0000000000000",
            },
        )
        assert first.status_code == 200, first.text

        second = client.post(
            "/api/v1/classify/rules",
            json={
                "kind": "tag_keyword",
                "key": "xyz",
                "target": "operations",
                "source_event_id": "01TESTEVENT0000000000001",
            },
        )
        assert second.status_code == 200, second.text

        events = registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_APPLIED, order="asc", limit=10
        )
        reapply_payload = events[-1].payload
        assert reapply_payload["target"] == "operations"
        assert reapply_payload["prior_target"] == "finance"
        assert reapply_payload["prior_source_event_id"] == "01TESTEVENT0000000000000"
        assert reapply_payload["prior_applied_at"] is not None
        assert reapply_payload["prior_applied_by"] is not None

        # The first apply had nothing to replace.
        first_payload = events[0].payload
        assert first_payload["prior_target"] is None

    def test_audit_event_records_shadowed_config_value_for_alias(
        self, tmp_path: Path
    ) -> None:
        """Applying an alias whose key also exists in ``config.yaml``
        shadows that config.yaml value -- the audit event must say what
        it shadowed, not just what now wins."""
        reg = StoreRegistry(
            stores_dir=tmp_path / "stores",
            classify_config={"domain_aliases": {"legacyname": "config-canonical"}},
        )
        app_module._registry = reg
        try:
            client = TestClient(create_app())
            resp = client.post(
                "/api/v1/classify/rules",
                json={
                    "kind": "domain_alias",
                    "key": "legacyname",
                    "target": "overlay-canonical",
                    "source_event_id": "01TESTEVENT0000000000000",
                },
            )
            assert resp.status_code == 200, resp.text
            events = reg.operational.event_log.get_events(
                event_type=EventType.CLASSIFY_RULE_APPLIED, order="asc", limit=10
            )
            payload = events[-1].payload
            assert payload["config_target"] == "config-canonical"
        finally:
            reg.close()
            app_module._registry = None


# ---------------------------------------------------------------------------
# DELETE /classify/rules/{kind}/{key} — revoke
# ---------------------------------------------------------------------------


class TestRevokeRule:
    def _apply(
        self, client: TestClient, key: str = "xyz", target: str = "finance"
    ) -> None:
        resp = client.post(
            "/api/v1/classify/rules",
            json={
                "kind": "tag_keyword",
                "key": key,
                "target": target,
                "source_event_id": "01TESTEVENT0000000000000",
            },
        )
        assert resp.status_code == 200, resp.text

    def test_revoke_overlay_row_succeeds_and_removes_it(
        self, client: TestClient
    ) -> None:
        self._apply(client)
        resp = client.delete("/api/v1/classify/rules/tag_keyword/xyz")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "success"

        rules = client.get("/api/v1/classify/rules").json()
        assert not any(
            r["kind"] == "tag_keyword" and r["key"] == "xyz" for r in rules["rules"]
        )

    def test_revoke_unknown_key_is_400(self, client: TestClient) -> None:
        resp = client.delete("/api/v1/classify/rules/tag_keyword/does-not-exist")
        assert resp.status_code == 400
        assert "No applied tag_keyword rule found" in resp.json()["detail"]

    def test_revoke_config_sourced_rule_is_refused_4xx(self, tmp_path: Path) -> None:
        reg = StoreRegistry(
            stores_dir=tmp_path / "stores",
            classify_config={"domain_keywords": {"finance": ["cfgword"]}},
        )
        app_module._registry = reg
        try:
            client = TestClient(create_app())
            resp = client.delete("/api/v1/classify/rules/tag_keyword/cfgword")
            assert resp.status_code == 400
            assert "config.yaml" in resp.json()["detail"]
        finally:
            reg.close()
            app_module._registry = None

    def test_non_admin_key_is_403(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        token = _mint(registry, [SCOPE_READ])
        resp = client.delete(
            "/api/v1/classify/rules/tag_keyword/xyz", headers={"X-API-Key": token}
        )
        assert resp.status_code == 403

    def test_revoke_exposing_a_config_chain_is_refused_4xx(
        self, tmp_path: Path
    ) -> None:
        """Revoking an overlay alias can resurface a config.yaml alias
        that chains with a different, still-applied overlay alias --
        refused with the same ``invalid_alias_rule`` Apply uses, and the
        shadowing row is left in place (mirrors
        ``test_classify_rules_cli.py::TestRevokeRule::
        test_revoke_exposing_a_config_chain_is_refused_both_formats``).

        Setup: config.yaml has ``b-alias -> x-canonical``. Overlay applies
        ``b-alias -> y-canonical`` (shadows config's row for that key, so
        the merged map right now is just ``{b-alias: y-canonical}``) and
        then ``x-canonical -> q-final`` (fine at the time: config's
        ``b-alias`` is shadowed, so nothing points at ``x-canonical`` as a
        key yet). Revoking ``b-alias`` un-shadows config's
        ``b-alias -> x-canonical``, which now chains with the still-applied
        ``x-canonical -> q-final``.
        """
        reg = StoreRegistry(
            stores_dir=tmp_path / "stores",
            classify_config={"domain_aliases": {"b-alias": "x-canonical"}},
        )
        app_module._registry = reg
        try:
            client = TestClient(create_app())
            shadow = client.post(
                "/api/v1/classify/rules",
                json={
                    "kind": "domain_alias",
                    "key": "b-alias",
                    "target": "y-canonical",
                    "source_event_id": "01TESTEVENT0000000000000",
                },
            )
            assert shadow.status_code == 200, shadow.text

            second = client.post(
                "/api/v1/classify/rules",
                json={
                    "kind": "domain_alias",
                    "key": "x-canonical",
                    "target": "q-final",
                    "source_event_id": "01TESTEVENT0000000000000",
                },
            )
            assert second.status_code == 200, second.text

            revoke = client.delete("/api/v1/classify/rules/domain_alias/b-alias")
            assert revoke.status_code == 400, revoke.text
            detail = revoke.json()["detail"].lower()
            assert "chain" in detail or "both an alias and a merge destination" in (
                detail
            )

            rules = client.get("/api/v1/classify/rules").json()
            row = _rule_row(rules, "domain_alias", "b-alias")
            assert row["source"] == "overlay"
            assert row["target"] == "y-canonical"
        finally:
            reg.close()
            app_module._registry = None

    def test_audit_event_records_removed_fields(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        self._apply(client, key="xyz", target="finance")
        resp = client.delete("/api/v1/classify/rules/tag_keyword/xyz")
        assert resp.status_code == 200, resp.text

        events = registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_REVOKED, order="asc", limit=10
        )
        payload = events[-1].payload
        assert payload["target"] == "finance"
        assert payload["removed_applied_by"] is not None
        assert payload["removed_applied_at"] is not None
        assert payload["removed_source_event_id"] == "01TESTEVENT0000000000000"
        assert payload["config_target"] is None

    def test_audit_event_carries_actor_identity_and_constant_requested_by(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mirrors ``TestApplyRule::test_audit_event_carries_actor_and_metrics``
        for Revoke: the payload's ``key_id``/``key_name`` come from the
        authenticated caller (``_audit_identity(ctx)``), but unlike Apply's
        ``applied_by``, ``revoked_by`` is the route's own constant label,
        not the caller's identity."""
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        token, key_id = _mint_with_key_id(registry, [SCOPE_ADMIN])
        headers = {"X-API-Key": token}
        apply_resp = client.post(
            "/api/v1/classify/rules",
            json={
                "kind": "tag_keyword",
                "key": "xyz",
                "target": "finance",
                "source_event_id": "01TESTEVENT0000000000000",
            },
            headers=headers,
        )
        assert apply_resp.status_code == 200, apply_resp.text

        resp = client.delete("/api/v1/classify/rules/tag_keyword/xyz", headers=headers)
        assert resp.status_code == 200, resp.text

        events = registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_REVOKED, order="asc", limit=10
        )
        payload = events[-1].payload
        assert payload["key_id"] == key_id
        assert payload["revoked_by"] == "api:classify.revoke-rule"


# ---------------------------------------------------------------------------
# Degraded overlay store — reads degrade, writes refuse (B4)
# ---------------------------------------------------------------------------


class TestDegradedOverlayStore:
    def _corrupt_overlay(self, tmp_path: Path) -> StoreRegistry:
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True, exist_ok=True)
        (stores_dir / "classify_overrides.json").write_text(
            "{not valid json", encoding="utf-8"
        )
        return StoreRegistry(stores_dir=stores_dir)

    def test_apply_on_degraded_store_is_409(self, tmp_path: Path) -> None:
        reg = self._corrupt_overlay(tmp_path)
        app_module._registry = reg
        try:
            client = TestClient(create_app())
            resp = client.post(
                "/api/v1/classify/rules",
                json={
                    "kind": "tag_keyword",
                    "key": "xyz",
                    "target": "finance",
                    "source_event_id": "01TESTEVENT0000000000000",
                },
            )
            assert resp.status_code == 409, resp.text
            detail = resp.json()["detail"]
            assert detail["code"] == "degraded_store_write"
            assert detail["store_degradation"] is not None
        finally:
            reg.close()
            app_module._registry = None

    def test_revoke_on_degraded_store_is_409_not_400(self, tmp_path: Path) -> None:
        reg = self._corrupt_overlay(tmp_path)
        app_module._registry = reg
        try:
            client = TestClient(create_app())
            resp = client.delete("/api/v1/classify/rules/tag_keyword/anything")
            assert resp.status_code == 409, resp.text
            detail = resp.json()["detail"]
            assert detail["code"] == "degraded_store_write"
            assert "No applied" not in str(detail.get("message", ""))
        finally:
            reg.close()
            app_module._registry = None

    def test_candidates_list_reports_store_degradation(self, tmp_path: Path) -> None:
        reg = self._corrupt_overlay(tmp_path)
        app_module._registry = reg
        try:
            client = TestClient(create_app())
            resp = client.get("/api/v1/classify/candidates")
            assert resp.status_code == 200, resp.text
            assert resp.json()["store_degradation"] is not None
        finally:
            reg.close()
            app_module._registry = None

    def test_rules_list_reports_store_degradation(self, tmp_path: Path) -> None:
        reg = self._corrupt_overlay(tmp_path)
        app_module._registry = reg
        try:
            client = TestClient(create_app())
            resp = client.get("/api/v1/classify/rules")
            assert resp.status_code == 200, resp.text
            assert resp.json()["store_degradation"] is not None
        finally:
            reg.close()
            app_module._registry = None

    def test_healthy_store_reports_no_degradation(self, client: TestClient) -> None:
        resp = client.get("/api/v1/classify/rules")
        assert resp.status_code == 200, resp.text
        assert resp.json()["store_degradation"] is None


# ---------------------------------------------------------------------------
# GET /classify/rules — effective rules, badged by source
# ---------------------------------------------------------------------------


class TestListRules:
    def test_default_rules_are_badged_default(self, client: TestClient) -> None:
        resp = client.get("/api/v1/classify/rules")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["count"] > 0
        assert all(
            row["source"] in ("default", "config", "overlay") for row in body["rules"]
        )
        assert all(row["source"] != "overlay" for row in body["rules"])

    def test_config_sourced_rule_is_badged_config(self, tmp_path: Path) -> None:
        reg = StoreRegistry(
            stores_dir=tmp_path / "stores",
            classify_config={"domain_keywords": {"finance": ["cfgword"]}},
        )
        app_module._registry = reg
        try:
            client = TestClient(create_app())
            body = client.get("/api/v1/classify/rules").json()
            row = _rule_row(body, "tag_keyword", "cfgword")
            assert row["source"] == "config"
            assert row["applied_at"] is None
        finally:
            reg.close()
            app_module._registry = None

    def test_config_only_chain_is_400_not_500(self, tmp_path: Path) -> None:
        """A chain introduced purely by editing config.yaml -- no overlay
        write to have caught it -- must read as a composed 400, never a
        500/traceback (mirrors
        ``test_classify_rules_cli.py::TestEffectiveRules::
        test_config_only_chain_is_exit_internal_not_traceback_both_formats``).
        """
        reg = StoreRegistry(
            stores_dir=tmp_path / "stores",
            classify_config={
                "domain_aliases": {"fin": "finance", "finance": "treasury"}
            },
        )
        app_module._registry = reg
        try:
            client = TestClient(create_app())
            resp = client.get("/api/v1/classify/rules")
            assert resp.status_code == 400, resp.text
            detail = resp.json()["detail"].lower()
            assert "chain" in detail or "both an alias and a merge destination" in (
                detail
            )
        finally:
            reg.close()
            app_module._registry = None

    def test_overlay_rule_count_increases_after_apply(self, client: TestClient) -> None:
        before = client.get("/api/v1/classify/rules").json()["count"]
        client.post(
            "/api/v1/classify/rules",
            json={
                "kind": "tag_keyword",
                "key": "xyz",
                "target": "finance",
                "source_event_id": "01TESTEVENT0000000000000",
            },
        )
        after = client.get("/api/v1/classify/rules").json()
        assert after["count"] == before + 1
        row = _rule_row(after, "tag_keyword", "xyz")
        assert row["source"] == "overlay"
        assert row["applied_at"] is not None
