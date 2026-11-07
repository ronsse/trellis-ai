"""A listed promotion with nothing left to judge it by is withheld (#1080, item 4b).

``PRECEDENT_PROMOTED`` events are immutable, so one outlives what the listing
reads to judge it: its node (a redaction purges it, and a failed re-create leaves
none), its document row, and the source id its payload may never have carried.
With all three gone the listing found no label, an id with no row reads as
readable, and the row was served at ``internal``. Every other read fails closed on
what it cannot judge; this one now does too.

The state is built by hand: an event in the learning namespace, no node, no
document row, no source id. Every listing door is asked: ``list_precedents`` (the
CLI door is the trusted local one, which asks at ``restricted`` and so reads
nothing), ``GET /precedents``, ``GET /precedents/domains`` and MCP ``get_lessons``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import trellis.mcp.server as server_mod
import trellis.mcp.wiring as wiring_mod
import trellis_api.app as app_module
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.test_sensitivity_surfaces import (
    ADMIN,
    READ,
    RESTRICTED,
    as_http,
    as_stdio,
)
from tests.unit.retrieve.test_precedent_listing_clearance import (
    ids_of,
    listing,
    promote,
    text_of,
)
from trellis.auth import generate_api_key
from trellis.retrieve.precedents import (
    PRECEDENT_WITHHELD_LABELLED,
    PRECEDENT_WITHHELD_UNREADABLE,
    PrecedentClearance,
    list_precedents,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

get_lessons = unwrap_tool(server_mod.get_lessons)

#: A learning promotion whose node, row and source are all gone.
GONE = "precedent://learning/asset_generation/gone"
GONE_TITLE = "Vanished precedent title"
GONE_DOMAIN = "vanished-domain"
SOURCE = "doc-source"
BODY = "Heron opening line ZEBRAWORD and the rest of the body."


def emit_promotion(
    registry: StoreRegistry,
    entity_id: str,
    *,
    title: str,
    domain: str | None = None,
    **payload: Any,
) -> None:
    """A ``PRECEDENT_PROMOTED`` event as the log holds it, whatever else survives."""
    registry.operational.event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        "mutation_executor",
        entity_id=entity_id,
        entity_type="precedent",
        payload={
            "title": title,
            "description": f"description of {title}",
            **({"domain": domain} if domain else {}),
            **payload,
        },
    )


@pytest.fixture
def registry(tmp_path, monkeypatch) -> Iterator[StoreRegistry]:
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    reg = StoreRegistry(stores_dir=stores_dir)
    monkeypatch.setattr(wiring_mod, "_registry", reg)
    monkeypatch.setattr(app_module, "_registry", reg)
    reg.knowledge.graph_store.upsert_node("entity://shared", "entity", {"name": "x"})
    reg.knowledge.document_store.put(SOURCE, BODY, {"title": "Source"})
    yield reg
    reg.close()


@pytest.fixture
def judgeable(registry: StoreRegistry) -> str:
    """A normal promotion: node, row and source all there."""
    return promote(registry, "cand:one", SOURCE)


@pytest.fixture
def gone(registry: StoreRegistry, judgeable: str) -> str:
    """The unjudgeable one, promoted after the judgeable one so it lists first."""
    emit_promotion(registry, GONE, title=GONE_TITLE, domain=GONE_DOMAIN)
    # The premise: nothing the listing could read is there.
    assert registry.knowledge.graph_store.get_node(GONE) is None
    assert registry.knowledge.document_store.get(GONE) is None
    return GONE


class TestListPrecedents:
    @pytest.mark.parametrize("clearance", ["internal", "confidential"])
    def test_below_restricted_it_is_withheld(
        self, registry: StoreRegistry, judgeable: str, gone: str, clearance: str
    ) -> None:
        assert ids_of(listing(registry, clearance)) == [judgeable]

    def test_a_restricted_caller_is_served_it(
        self, registry: StoreRegistry, judgeable: str, gone: str
    ) -> None:
        assert ids_of(listing(registry, "restricted")) == [gone, judgeable]

    def test_it_takes_no_slot_from_a_servable_row(
        self, registry: StoreRegistry, judgeable: str, gone: str
    ) -> None:
        """Decided before the ``limit`` cap, as the other two rules are."""
        assert ids_of(listing(registry, "internal", limit=1)) == [judgeable]

    def test_it_is_counted_as_unreadable_not_as_a_label_that_said_no(
        self, registry: StoreRegistry, judgeable: str, gone: str
    ) -> None:
        events = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_PROMOTED, limit=10
        )
        gate = PrecedentClearance(
            registry.knowledge.document_store,
            "internal",
            registry.knowledge.graph_store,
        )

        barred = gate.barred_events(events, lambda e: None)

        assert {e.entity_id for e in events if e.event_id in barred} == {gone}
        assert gate.counts == {
            PRECEDENT_WITHHELD_LABELLED: 0,
            PRECEDENT_WITHHELD_UNREADABLE: 1,
        }

    def test_the_rule_is_what_withholds_it(
        self, registry: StoreRegistry, judgeable: str, gone: str, monkeypatch
    ) -> None:
        """Without it the row is served, as on the base: the premise of the fix."""
        monkeypatch.setattr(
            PrecedentClearance, "_nothing_to_judge", lambda *args, **kw: False
        )

        assert ids_of(listing(registry, "internal")) == [gone, judgeable]


class TestWhatStillHasSomethingToJudge:
    """Each state below keeps a label to read, so the listing serves it as before."""

    def test_a_trace_precedent_is_served(self, registry: StoreRegistry) -> None:
        emit_promotion(registry, "trace-1", title="t", trace_id="trace-1")

        assert ids_of(listing(registry, "internal")) == ["trace-1"]

    def test_an_entity_with_its_own_document_row_is_judged_by_it(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put(GONE, "the precedent's text", {})
        emit_promotion(registry, GONE, title=GONE_TITLE)

        assert ids_of(listing(registry, "internal")) == [GONE]

    def test_a_named_source_is_judged_by_its_own_label(
        self, registry: StoreRegistry
    ) -> None:
        """No node and no row, but the payload names a source: that is a label."""
        emit_promotion(registry, GONE, title=GONE_TITLE, source_item_id=SOURCE)
        assert ids_of(listing(registry, "internal")) == [GONE]

        registry.knowledge.document_store.put(
            SOURCE, BODY, {"data_classification": {"sensitivity": "confidential"}}
        )

        assert listing(registry, "internal") == []
        assert ids_of(listing(registry, "confidential")) == [GONE]

    def test_the_nodes_source_is_a_label_to_read_too(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.graph_store.upsert_node(
            GONE, "precedent", {"name": "n", "source_item_id": SOURCE}
        )
        emit_promotion(registry, GONE, title=GONE_TITLE)

        assert ids_of(listing(registry, "internal")) == [GONE]

    def test_a_node_that_is_still_there_is_not_withheld(
        self, registry: StoreRegistry
    ) -> None:
        """Only a purged node: every node-based door serves a node it holds, and
        the listing does not contradict them."""
        registry.knowledge.graph_store.upsert_node(
            GONE, "precedent", {"name": "n", "description": "d"}
        )
        emit_promotion(registry, GONE, title=GONE_TITLE)

        assert ids_of(listing(registry, "internal")) == [GONE]

    def test_a_row_the_store_failed_to_read_is_withheld_by_the_older_rule(
        self, registry: StoreRegistry, gone: str, monkeypatch
    ) -> None:
        docs = registry.knowledge.document_store
        real_get = docs.get

        def get(doc_id: str, *args: Any, **kwargs: Any) -> Any:
            if doc_id == GONE:
                msg = "store unavailable"
                raise RuntimeError(msg)
            return real_get(doc_id, *args, **kwargs)

        monkeypatch.setattr(docs, "get", get)
        gate = PrecedentClearance(docs, "internal", registry.knowledge.graph_store)
        events = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_PROMOTED, limit=10
        )

        gate.barred_events(events, lambda e: None)

        # One withheld, once: the failed read is counted by the older rule and the
        # new one does not count the same precedent a second time.
        assert gate.counts[PRECEDENT_WITHHELD_UNREADABLE] == 1

    def test_restricted_reads_nothing(self, registry: StoreRegistry, gone: str) -> None:
        store = MagicMock(spec=registry.knowledge.document_store)

        rows = list_precedents(
            registry.operational.event_log,
            graph_store=registry.knowledge.graph_store,
            document_store=store,
            clearance="restricted",
        )

        assert GONE in ids_of(rows)
        store.get.assert_not_called()


class TestAnOutageBuiltPromotionIsJudgedByItsSource:
    """``lookup_failed`` says the label was unknown, not that it was high (#1080).

    The read side exempts a row from its source's label only when the node reads
    ``source_labelled``. An outage-built node reads ``lookup_failed``, so the
    listing judges it by the source's label now: lowered, it is served again;
    still above the caller, it stays withheld.
    """

    def _outage_built(self, registry: StoreRegistry, status: str) -> None:
        registry.knowledge.graph_store.upsert_node(
            GONE,
            "precedent",
            {
                "name": "Learning: asset_generation :: gone",
                "name_source": "neutral",
                "description": f"description of {GONE_TITLE}",
                "source_item_id": SOURCE,
                "source_excerpt_status": status,
            },
        )
        emit_promotion(registry, GONE, title=GONE_TITLE, source_item_id=SOURCE)

    def _label(self, registry: StoreRegistry, sensitivity: str) -> None:
        registry.knowledge.document_store.put(
            SOURCE, BODY, {"data_classification": {"sensitivity": sensitivity}}
        )

    def test_a_source_still_above_the_caller_keeps_it_withheld(
        self, registry: StoreRegistry
    ) -> None:
        self._outage_built(registry, "lookup_failed")
        self._label(registry, "confidential")

        assert listing(registry, "internal") == []
        assert ids_of(listing(registry, "confidential")) == [GONE]

    def test_a_source_since_lowered_serves_it(self, registry: StoreRegistry) -> None:
        self._outage_built(registry, "lookup_failed")
        self._label(registry, "confidential")
        assert listing(registry, "internal") == []

        self._label(registry, "internal")

        assert ids_of(listing(registry, "internal")) == [GONE]

    def test_only_source_labelled_is_exempt_from_the_source(
        self, registry: StoreRegistry
    ) -> None:
        """The contrast: a node that records the label was high, and holds none of
        the source, is served whatever the source reads now."""
        self._outage_built(registry, "source_labelled")
        self._label(registry, "confidential")

        assert ids_of(listing(registry, "internal")) == [GONE]


class TestUnlabelledStoreIsUnchanged:
    def test_every_clearance_serves_the_same_bytes_with_the_rule_off(
        self, registry: StoreRegistry, monkeypatch
    ) -> None:
        """Normal promotions, a trace precedent and one that names a source whose
        node is gone: all judgeable, so the rule changes nothing in the response."""
        docs = registry.knowledge.document_store
        docs.put("doc-plain", BODY, {})
        promote(registry, "cand:0", "doc-plain")
        promote(registry, "cand:1", SOURCE)
        emit_promotion(registry, "trace-1", title="t", trace_id="trace-1")
        emit_promotion(registry, GONE, title=GONE_TITLE, source_item_id=SOURCE)
        trusted = text_of(listing(registry, "restricted"))
        assert len(json.loads(trusted)) == 4

        served = {
            c: text_of(listing(registry, c)) for c in ("internal", "confidential")
        }
        monkeypatch.setattr(
            PrecedentClearance, "_nothing_to_judge", lambda *args, **kw: False
        )
        rule_off = {c: text_of(listing(registry, c)) for c in served}

        assert served == rule_off
        assert all(body == trusted for body in served.values())


def _headers(registry: StoreRegistry, scopes: list[str]) -> dict[str, str]:
    token, record = generate_api_key("k", scopes)
    registry.operational.api_key_store.create(record)
    return {"Authorization": f"Bearer {token}"}


class TestRestListing:
    def _get(self, registry: StoreRegistry, scopes: list[str]) -> list[dict[str, Any]]:
        resp = TestClient(create_app()).get(
            "/api/v1/precedents", headers=_headers(registry, scopes)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["count"] == len(body["precedents"])
        rows: list[dict[str, Any]] = body["precedents"]
        return rows

    def test_an_internal_key_does_not_see_it(
        self, registry: StoreRegistry, judgeable: str, gone: str
    ) -> None:
        assert ids_of(self._get(registry, ["read"])) == [judgeable]

    def test_a_restricted_or_admin_key_does(
        self, registry: StoreRegistry, judgeable: str, gone: str
    ) -> None:
        assert ids_of(self._get(registry, ["read", "confidential"])) == [judgeable]
        assert ids_of(self._get(registry, ["read", "restricted"])) == [gone, judgeable]
        assert ids_of(self._get(registry, ["admin"])) == [gone, judgeable]

    def test_an_unlabelled_store_answers_as_it_did_before_the_rule(
        self, registry: StoreRegistry, judgeable: str, monkeypatch
    ) -> None:
        emit_promotion(registry, "trace-1", title="t", trace_id="trace-1")
        served = self._get(registry, ["read"])
        monkeypatch.setattr(
            PrecedentClearance, "_nothing_to_judge", lambda *args, **kw: False
        )

        assert served == self._get(registry, ["read"])
        assert ids_of(served) == ["trace-1", judgeable]


class TestRestDomains:
    def _domains(self, registry: StoreRegistry, scopes: list[str]) -> dict[str, Any]:
        resp = TestClient(create_app()).get(
            "/api/v1/precedents/domains", headers=_headers(registry, scopes)
        )
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        return body

    def test_an_internal_key_is_not_offered_its_domain(
        self, registry: StoreRegistry, judgeable: str, gone: str
    ) -> None:
        body = self._domains(registry, ["read"])

        assert GONE_DOMAIN not in [d["domain"] for d in body["domains"]]

    def test_a_restricted_key_is_offered_it(
        self, registry: StoreRegistry, judgeable: str, gone: str
    ) -> None:
        body = self._domains(registry, ["read", "restricted"])

        assert {"domain": GONE_DOMAIN, "count": 1} in body["domains"]

    def test_the_counts_match_what_the_listing_serves(
        self, registry: StoreRegistry, judgeable: str, gone: str
    ) -> None:
        emit_promotion(registry, "trace-1", title="t", domain="ops", trace_id="trace-1")

        body = self._domains(registry, ["read"])
        served = listing(registry, "internal")

        assert body["total"] == sum(1 for r in served if r.get("domain"))
        assert body["domains"] == [{"domain": "ops", "count": 1}]


class TestMcpGetLessons:
    def test_internal_clearance_omits_it(
        self, registry: StoreRegistry, judgeable: str, gone: str
    ) -> None:
        with as_http(READ):
            served = get_lessons()
        assert GONE_TITLE not in served
        assert GONE not in served

    @pytest.mark.parametrize(
        "caller", [lambda: as_http(ADMIN), lambda: as_http(RESTRICTED), as_stdio]
    )
    def test_a_cleared_or_trusted_caller_is_served_it(
        self, registry: StoreRegistry, judgeable: str, gone: str, caller: Any
    ) -> None:
        with caller():
            served = get_lessons()
        assert GONE_TITLE in served

    def test_an_unlabelled_store_reads_the_same_with_the_rule_off(
        self, registry: StoreRegistry, judgeable: str, monkeypatch
    ) -> None:
        emit_promotion(registry, "trace-1", title="Trace lesson", trace_id="trace-1")
        with as_http(READ):
            served = get_lessons()
        monkeypatch.setattr(
            PrecedentClearance, "_nothing_to_judge", lambda *args, **kw: False
        )
        with as_http(READ):
            rule_off = get_lessons()

        assert served == rule_off
        assert "Trace lesson" in served
