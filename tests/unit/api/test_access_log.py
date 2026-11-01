"""The per-key access log over REST: who is recorded and what a row holds."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.retrieve.public_corpus import (
    EMBED_FN_PATH,
    QUERY,
    build_world,
    differences,
    mask,
)
from trellis.analyze.holdout import analyze_holdout
from trellis.auth import generate_api_key
from trellis.core import access_log
from trellis.ops.capture_health import accept_events_for, check_capture_health
from trellis.ops.write_health import summarize_write_health
from trellis.retrieve.effectiveness import analyze_effectiveness
from trellis.schemas.enums import TraceSource
from trellis.schemas.trace import Trace, TraceContext
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.access_log import ITEM_ROUTES, items_from_body
from trellis_api.app import create_app


def _settled(value: Any) -> Any:
    """``value`` with floats rounded: scores jitter below the fourth decimal."""
    if isinstance(value, dict):
        return {k: _settled(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_settled(v) for v in value]
    return round(value, 4) if isinstance(value, float) else value


class World:
    def __init__(self, registry: StoreRegistry) -> None:
        self.registry = registry
        self.client = TestClient(create_app())
        self.headers: dict[str, dict[str, str]] = {}
        self.key_ids: dict[str, str] = {}
        for name, scopes in (
            ("read", ["read"]),
            ("admin", ["admin"]),
            ("external", ["read", "external"]),
        ):
            token, record = generate_api_key(name, scopes)
            registry.operational.api_key_store.create(record)
            self.headers[name] = {"Authorization": f"Bearer {token}"}
            self.key_ids[name] = record.key_id

    def rows(self, event_type: EventType = EventType.KEY_READ) -> list[Any]:
        assert access_log.get_writer().wait_idle(5), "the writer did not drain"
        return self.registry.operational.event_log.get_events(
            event_type=event_type, limit=1000
        )

    def pack(self, who: str = "read") -> dict[str, Any]:
        resp = self.client.post(
            "/api/v1/packs", json={"intent": QUERY}, headers=self.headers[who]
        )
        assert resp.status_code == 200, resp.text
        return resp.json()


@pytest.fixture
def world(tmp_path, monkeypatch):
    for var in ("TRELLIS_API_KEY", "TRELLIS_ACCESS_LOG", "TRELLIS_PACK_HOLDOUT_RATE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_FN_PATH)
    monkeypatch.setenv("TRELLIS_ACCESS_LOG", "1")
    access_log.reset_failure_state()
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    build_world(reg, "full")
    try:
        yield World(reg)
    finally:
        access_log.get_writer().wait_idle(5)
        reg.close()
        app_module._registry = None


class TestRecorded:
    def test_a_keyed_search_records_one_row_with_its_ids(self, world: World) -> None:
        resp = world.client.get(
            "/api/v1/search", params={"q": QUERY}, headers=world.headers["read"]
        )
        assert resp.status_code == 200
        served = [r["doc_id"] for r in resp.json()["results"]]
        assert served
        (row,) = world.rows()
        assert row.entity_id == world.key_ids["read"]
        assert row.payload == {
            "key_id": world.key_ids["read"],
            "surface": "rest",
            "route": "GET /api/v1/search",
            "item_count": len(served),
            "item_ids": served[: access_log.MAX_LOGGED_ITEM_IDS],
            "item_ids_truncated": len(served) > access_log.MAX_LOGGED_ITEM_IDS,
        }

    def test_a_row_holds_no_query_text_and_no_secret(self, world: World) -> None:
        pack = world.pack()
        body = json.dumps([r.payload for r in world.rows()])
        assert QUERY not in body
        assert "trellis_ak_" not in body
        assert all(i["excerpt"] not in body for i in pack["items"])

    def test_a_flat_and_a_sectioned_pack_record_the_ids_they_served(
        self, world: World
    ) -> None:
        flat = world.pack()
        sectioned = world.client.post(
            "/api/v1/packs/sectioned",
            json={"intent": QUERY, "sections": [{"name": "A", "max_items": 2}]},
            headers=world.headers["read"],
        ).json()
        flat_row, sectioned_row = world.rows()
        assert flat_row.payload["item_ids"] == [i["item_id"] for i in flat["items"]]
        assert sectioned_row.payload["item_ids"] == [
            i["item_id"] for s in sectioned["sections"] for i in s["items"]
        ]
        assert sectioned_row.payload["item_count"] == len(
            sectioned_row.payload["item_ids"]
        )

    def test_a_document_read_records_that_document(self, world: World) -> None:
        doc_id = world.client.get(
            "/api/v1/search", params={"q": QUERY}, headers=world.headers["read"]
        ).json()["results"][0]["doc_id"]
        resp = world.client.get(
            f"/api/v1/documents/{doc_id}", headers=world.headers["read"]
        )
        assert resp.status_code == 200
        last = world.rows()[-1]
        assert last.payload["route"] == "GET /api/v1/documents/{doc_id:path}"
        assert last.payload["item_ids"] == [doc_id]

    def test_ids_are_capped_and_the_true_count_stays(self) -> None:
        ids = [f"d{n}" for n in range(60)]
        payload = access_log.build_payload(
            key_id="k", surface="rest", route="GET /x", item_ids=ids, item_count=60
        )
        assert payload["item_count"] == 60
        assert len(payload["item_ids"]) == access_log.MAX_LOGGED_ITEM_IDS
        assert payload["item_ids_truncated"] is True

    def test_an_answer_that_is_not_the_expected_shape_has_no_count(self) -> None:
        spec = ITEM_ROUTES["POST /api/v1/packs"]
        assert items_from_body(b'[{"item_id": "a"}]', spec) == ([], None)
        assert items_from_body(b'{"items": [{"id": "a"}]}', spec) == ([], None)
        assert items_from_body(b"not json", spec) == ([], None)
        assert items_from_body(b"", spec) == ([], None)
        assert items_from_body(b'{"items": []}', spec) == ([], 0)

    def test_a_route_off_the_allowlist_records_no_ids_and_no_count(
        self, world: World
    ) -> None:
        # The events answer is full of entity ids; none of them is an item.
        world.pack()
        resp = world.client.get("/api/v1/events", headers=world.headers["admin"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["events"], "control: the answer carries event rows"
        last = world.rows()[-1]
        assert last.payload["route"] == "GET /api/v1/events"
        assert last.payload["item_count"] is None
        assert last.payload["item_ids"] == []


class TestEveryAllowlistedRouteNamesItsIds:
    """The id field of each route is read from the route's real answer."""

    NODE = "ent-zebra-1"

    def _seed(self, world: World) -> str:
        world.registry.knowledge.graph_store.upsert_node(
            self.NODE, "entity", {"name": "zebra"}
        )
        trace = Trace(
            source=TraceSource.AGENT,
            intent="a task",
            steps=[],
            context=TraceContext(agent_id="a-1", domain="d"),
        )
        world.registry.operational.trace_store.append(trace)
        return trace.trace_id

    def _read(self, world: World, path: str, **params: str) -> Any:
        resp = world.client.get(path, params=params, headers=world.headers["read"])
        assert resp.status_code == 200, resp.text
        (row,) = world.rows()
        assert row.payload["route"].startswith("GET /api/v1/")
        return resp.json(), row.payload

    def test_graph_search(self, world: World) -> None:
        self._seed(world)
        body, payload = self._read(world, "/api/v1/graph/search", q="zebra")
        named = [r["entity_id"] for r in body["results"]]
        assert self.NODE in named
        assert payload["route"] == "GET /api/v1/graph/search"
        assert payload["item_ids"] == named
        assert payload["item_count"] == len(named)

    def test_entities(self, world: World) -> None:
        self._seed(world)
        body, payload = self._read(world, f"/api/v1/entities/{self.NODE}")
        assert body["entity"]["node_id"] == self.NODE
        assert payload["route"] == "GET /api/v1/entities/{entity_id:path}"
        assert payload["item_ids"] == [self.NODE]
        assert payload["item_count"] == 1

    def test_traces(self, world: World) -> None:
        trace_id = self._seed(world)
        body, payload = self._read(world, "/api/v1/traces")
        named = [t["trace_id"] for t in body["traces"]]
        assert trace_id in named
        assert payload["route"] == "GET /api/v1/traces"
        assert payload["item_ids"] == named
        assert payload["item_count"] == len(named)

    def test_one_trace(self, world: World) -> None:
        trace_id = self._seed(world)
        body, payload = self._read(world, f"/api/v1/traces/{trace_id}")
        assert body["trace"]["trace_id"] == trace_id
        assert payload["route"] == "GET /api/v1/traces/{trace_id}"
        assert payload["item_ids"] == [trace_id]
        assert payload["item_count"] == 1


class TestNotRecorded:
    def test_a_refused_read_records_nothing(self, world: World) -> None:
        resp = world.client.get("/api/v1/search", params={"q": "x"})
        assert resp.status_code == 401
        assert world.rows() == []

    def test_the_shared_secret_records_nothing(self, world: World, monkeypatch) -> None:
        monkeypatch.setenv("TRELLIS_API_KEY", "shared-secret-value")
        resp = world.client.get(
            "/api/v1/search",
            params={"q": QUERY},
            headers={"X-API-Key": "shared-secret-value"},
        )
        assert resp.status_code == 200
        assert world.rows() == []

    @pytest.mark.parametrize("mode", ["off", "optional"])
    def test_a_keyless_call_records_nothing(
        self, world: World, monkeypatch, mode: str
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", mode)
        assert (
            world.client.get("/api/v1/search", params={"q": QUERY}).status_code == 200
        )
        assert world.rows() == []

    @pytest.mark.parametrize(
        "value", [None, "", "0", "false", "nonsense", "n", "f", "disabled", "none"]
    )
    def test_off_is_the_default_and_an_unrecognised_value_is_off(
        self, world: World, monkeypatch, value: str | None
    ) -> None:
        if value is None:
            monkeypatch.delenv("TRELLIS_ACCESS_LOG")
        else:
            monkeypatch.setenv("TRELLIS_ACCESS_LOG", value)
        world.pack()
        assert world.rows() == []
        assert world.rows(EventType.ACCESS_LOG_FAILED) == []

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "ON"])
    def test_only_the_truthy_spellings_turn_it_on(
        self, world: World, monkeypatch, value: str
    ) -> None:
        monkeypatch.setenv("TRELLIS_ACCESS_LOG", value)
        world.pack()
        assert len(world.rows()) == 1

    def test_a_write_is_not_a_read(self, world: World) -> None:
        world.client.post(
            "/api/v1/curate/feedback",
            json={"target_id": "x", "rating": 1.0},
            headers=world.headers["admin"],
        )
        assert world.rows() == []


class TestAdvisory:
    def test_a_raising_emit_never_fails_the_read_and_leaves_a_trace(
        self, world: World, monkeypatch
    ) -> None:
        log = world.registry.operational.event_log
        real_emit = log.emit

        def emit(event_type, *args, **kwargs):
            if event_type == EventType.KEY_READ:
                msg = "dsn=postgres://user:hunter2@host/db"
                raise RuntimeError(msg)
            return real_emit(event_type, *args, **kwargs)

        monkeypatch.setattr(log, "emit", emit)
        resp = world.client.get(
            "/api/v1/search", params={"q": QUERY}, headers=world.headers["read"]
        )
        assert resp.status_code == 200
        assert resp.json()["results"]
        (failed,) = world.rows(EventType.ACCESS_LOG_FAILED)
        assert failed.payload == {
            "surface": "rest",
            "error_type": "RuntimeError",
            "dropped_reads": 0,
            "failed_reads": 1,
        }
        assert "hunter2" not in json.dumps(failed.payload)

    def test_the_failure_signal_is_rate_limited(
        self, world: World, monkeypatch
    ) -> None:
        log = world.registry.operational.event_log
        real_emit = log.emit

        def emit(event_type, *args, **kwargs):
            if event_type == EventType.KEY_READ:
                msg = "down"
                raise RuntimeError(msg)
            return real_emit(event_type, *args, **kwargs)

        monkeypatch.setattr(log, "emit", emit)
        for _ in range(3):
            resp = world.client.get(
                "/api/v1/search", params={"q": QUERY}, headers=world.headers["read"]
            )
            assert resp.status_code == 200
        # Three reads failed; one signal row went out in the minute, and the
        # other two reads are still counted for the next one.
        assert len(world.rows(EventType.ACCESS_LOG_FAILED)) == 1
        assert access_log.pending_unrecorded() == (0, 2)


class TestUnchangedByTheLog:
    def test_a_pack_is_the_same_on_or_off(self, world: World, monkeypatch) -> None:
        on = world.pack()
        monkeypatch.setenv("TRELLIS_ACCESS_LOG", "0")
        off = world.pack()
        assert on["pack_id"] != off["pack_id"]
        assert differences(_settled(mask(on)), _settled(mask(off))) == []
        assert [i["item_id"] for i in on["items"]] == [
            i["item_id"] for i in off["items"]
        ]

    def test_a_search_is_byte_identical_on_or_off(
        self, world: World, monkeypatch
    ) -> None:
        def get() -> bytes:
            return world.client.get(
                "/api/v1/search", params={"q": QUERY}, headers=world.headers["read"]
            ).content

        on = get()
        monkeypatch.setenv("TRELLIS_ACCESS_LOG", "0")
        assert get() == on

    def test_no_pack_assembled_row_is_added_by_the_log(
        self, world: World, monkeypatch
    ) -> None:
        world.pack()
        with_log = len(world.rows(EventType.PACK_ASSEMBLED))
        monkeypatch.setenv("TRELLIS_ACCESS_LOG", "0")
        world.pack()
        assert len(world.rows(EventType.PACK_ASSEMBLED)) == with_log + 1


class TestAggregateReadersAreUnaffected:
    UNTIL = datetime.now(UTC) + timedelta(hours=1)

    def _reports(self, world: World) -> list[str]:
        log = world.registry.operational.event_log
        reports = [
            analyze_holdout(
                log, days=1, until=self.UNTIL, permutations=50, bootstraps=50
            ),
            analyze_effectiveness(log, days=1),
            summarize_write_health(log, days=1),
            check_capture_health(log, threshold=1),
        ]
        return [
            json.dumps(
                mask(r.model_dump(mode="json")) if r is not None else None,
                sort_keys=True,
                default=str,
            )
            for r in reports
        ]

    def test_key_reads_move_no_report(self, world: World, monkeypatch) -> None:
        monkeypatch.setenv("TRELLIS_ACCESS_LOG", "0")
        for _ in range(3):
            world.pack()
        before = self._reports(world)
        assert (
            analyze_effectiveness(
                world.registry.operational.event_log, days=1
            ).total_packs
            == 3
        ), "control: the readers see the ordinary packs"
        monkeypatch.setenv("TRELLIS_ACCESS_LOG", "1")
        for _ in range(5):
            world.client.get(
                "/api/v1/search", params={"q": QUERY}, headers=world.headers["read"]
            )
        world.registry.operational.event_log.emit(
            EventType.ACCESS_LOG_FAILED, "access_log", payload={"surface": "rest"}
        )
        assert len(world.rows()) == 5
        assert self._reports(world) == before

    def test_neither_type_is_an_accept_event_for_any_surface(self) -> None:
        for label in (
            "mcp:save_memory",
            "mcp:save_experience",
            "api:ingest",
            "global:x",
        ):
            accepts = set(accept_events_for(label))
            assert EventType.KEY_READ not in accepts
            assert EventType.ACCESS_LOG_FAILED not in accepts


class TestAdminRead:
    def test_per_key_last_used_and_counts(self, world: World) -> None:
        world.pack()
        world.pack()
        world.client.get(
            "/api/v1/search", params={"q": QUERY}, headers=world.headers["external"]
        )
        resp = world.client.get(
            "/api/v1/access-log/keys", headers=world.headers["admin"]
        )
        assert resp.status_code == 200
        report = resp.json()
        by_key = {k["key_id"]: k for k in report["keys"]}
        read = by_key[world.key_ids["read"]]
        assert read["reads"] == 2
        assert read["name"] == "read"
        assert read["by_surface"] == {"rest": 2}
        assert read["items_returned"] > 0
        assert by_key[world.key_ids["external"]]["reads"] == 1
        assert report["failure_signals"] == 0
        assert report["dropped_reads"] == 0
        assert report["failed_reads"] == 0
        assert "log_failures" not in report
        assert report["truncated"] is False
        assert report["note"] == ""

    def test_key_filter_and_window(self, world: World) -> None:
        world.pack()
        url = "/api/v1/access-log/keys"
        only = world.client.get(
            url, params={"key_id": "nope"}, headers=world.headers["admin"]
        ).json()
        assert only["keys"] == []
        assert (
            world.client.get(
                url, params={"hours": 0}, headers=world.headers["admin"]
            ).status_code
            == 422
        )

    @pytest.mark.parametrize("who", ["read", "external"])
    def test_a_non_admin_key_is_refused(self, world: World, who: str) -> None:
        resp = world.client.get("/api/v1/access-log/keys", headers=world.headers[who])
        assert resp.status_code == 403
