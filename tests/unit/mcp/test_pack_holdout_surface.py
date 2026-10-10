"""A withheld pack reaches an MCP agent as an ordinary empty pack.

Every MCP pack tool builds through
:func:`~trellis.retrieve.builder_factory.build_pack_builder`, and the
holdout draw sits inside :class:`~trellis.retrieve.pack_builder.PackBuilder`,
so no tool can serve around it. What these tests pin is the agent's side:

* **Blind.** A withheld response is the response the same tool gives on a
  corpus that holds nothing (no items, no advisories), apart from its
  ``pack_id``. Nothing in it mentions a holdout.
* **Joinable.** It carries its ``pack_id`` in the header, where the session
  capture parser (#694) reads it, so the capture join sees withheld packs.
  The flat tools used to answer an empty pack with one bare line and no
  ``pack_id``; with the flag on, every empty flat pack, withheld or not,
  now renders through the formatter, so the two stay indistinguishable.
* **Recorded.** ``PACK_ASSEMBLED`` says ``holdout: true`` and keeps the
  would-be items and advisories under their own keys, never as served.
* **Transparent at rate 0.** The flat tools' empty-pack line is unchanged,
  byte for byte, and no response moves.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.workers.session_capture.conftest import (
    assistant_tools,
    assistant_turn,
    mcp_envelope,
    pack_result_turn,
    user_turn,
    write_transcript,
)
from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryEvidence
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_workers.session_capture.transcripts import parse_session

get_context = unwrap_tool(server_mod.get_context)
search = unwrap_tool(server_mod.search)
get_task_context = unwrap_tool(server_mod.get_task_context)
get_objective_context = unwrap_tool(server_mod.get_objective_context)
get_sectioned_context = unwrap_tool(server_mod.get_sectioned_context)

RATE_ENV = "TRELLIS_PACK_HOLDOUT_RATE"
INTENT = "failover runbook"

_PACK_ID = re.compile(r"\*\*pack_id:\*\* `([0-9A-HJKMNP-TV-Z]{26})`")
_ANY_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")

#: Words a blind response must never contain, matched case-insensitively.
_TELLS = (
    "holdout",
    "hold-out",
    "held out",
    "held-out",
    "withheld",
    "experiment",
    "treatment",
    "randomi",
    "control arm",
)

#: Distinct filler per document, so semantic dedup keeps all four.
_SUBJECTS = (
    "replication lag",
    "connection pooling",
    "vacuum scheduling",
    "wal shipping",
)

#: Every MCP tool that assembles a pack, by rendering path.
_CALLS: dict[str, Callable[[], str]] = {
    "get_context": lambda: get_context(INTENT),
    "get_context_index": lambda: get_context(INTENT, index=True),
    "search": lambda: search(INTENT),
    "get_context_sections": lambda: get_context(
        INTENT,
        sections=[{"name": "All"}, {"name": "Patterns", "content_types": ["pattern"]}],
    ),
    "get_task_context": lambda: get_task_context(INTENT),
    "get_objective_context": lambda: get_objective_context(INTENT),
    "get_sectioned_context": lambda: get_sectioned_context(
        INTENT, sections=[{"name": "Configuration", "content_types": ["configuration"]}]
    ),
}
_FLAT = frozenset({"get_context", "get_context_index", "search"})


def _body(subject: str, mentions: int) -> str:
    """``mentions`` of the intent in a body of near-constant length.

    Distinct counts keep the documents' keyword scores apart. Repeating one
    sentence per document instead tied them to within float noise, so two
    consecutive builds could rank them in different orders.
    """
    mention = f"failover runbook for {subject}: promote the replica. "
    filler = f"then restart the {subject} sidecar once the write queue drains. "
    return mention * mentions + filler * (8 - mentions)


def _seed_documents(registry: StoreRegistry) -> None:
    """Four documents every tool can retrieve."""
    store = registry.knowledge.document_store
    for i, subject in enumerate(_SUBJECTS):
        content_type = "pattern" if i % 2 == 0 else "configuration"
        store.put(
            f"doc-{i}",
            _body(subject, mentions=i + 2),
            {"title": f"Runbook {i}", "content_tags": {"content_type": content_type}},
        )


def _seed_advisories(registry: StoreRegistry) -> None:
    """Two global advisories, matching every undomained pack regardless of
    whether it has items — this is what makes an item-less pack a tell
    (R1/#844) unless the builder blinds advisories on it too."""
    advisories = AdvisoryStore(registry.stores_dir / "advisories.json")
    for advisory_id, confidence, category in [
        ("adv-entity", 0.82, AdvisoryCategory.ENTITY),
        ("adv-approach", 0.61, AdvisoryCategory.APPROACH),
    ]:
        advisories.put(
            Advisory(
                advisory_id=advisory_id,
                category=category,
                confidence=confidence,
                message=f"Synthetic advisory {advisory_id}",
                evidence=AdvisoryEvidence(
                    sample_size=9,
                    success_rate_with=0.7,
                    success_rate_without=0.4,
                    effect_size=0.3,
                ),
                scope="global",
            )
        )


def _seed(registry: StoreRegistry) -> None:
    """Four documents every tool can retrieve, and two global advisories."""
    _seed_documents(registry)
    _seed_advisories(registry)


def _pack_id(response: str) -> str:
    found = _PACK_ID.search(response)
    assert found is not None, response
    return found.group(1)


def _mask(response: str) -> str:
    return _ANY_ULID.sub("<PACK_ID>", response)


def _assembled(registry: StoreRegistry, pack_id: str) -> dict[str, Any]:
    events = [
        event
        for event in registry.operational.event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=100
        )
        if event.entity_id == pack_id
    ]
    assert len(events) == 1, events
    return events[0].payload


def _served_ids(payload: dict[str, Any], *, flat: bool) -> list[list[str]]:
    if flat:
        return [sorted(payload["injected_item_ids"])]
    return [sorted(section["item_ids"]) for section in payload["sections"]]


class TestAWithheldPackReadsAsAnEmptyPack:
    @pytest.mark.parametrize("name", sorted(_CALLS))
    def test_the_response_is_an_empty_packs_response(
        self, name: str, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        call = _CALLS[name]
        # Advisories already exist (global scope, so they match any
        # undomained pack) but no document does yet: a naturally empty
        # pack, served (not drawn into the holdout — the rate is positive
        # but vanishing), while the holdout is live. This is the R1
        # scenario (#844): without the builder blinding an item-less
        # pack's advisories too, this response would carry an advisory
        # block a withheld response never does, telling the two apart.
        _seed_advisories(temp_registry)
        monkeypatch.setenv(RATE_ENV, "1e-12")
        greenfield = call()

        _seed_documents(temp_registry)
        monkeypatch.setenv(RATE_ENV, "0")
        control = call()
        monkeypatch.setenv(RATE_ENV, "1")
        withheld = call()

        # The seeded corpus answers this tool: the comparison is not empty
        # against empty.
        assert _mask(control) != _mask(greenfield)
        # The withheld response is the greenfield one, pack id aside ...
        assert _mask(withheld) == _mask(greenfield)
        # ... names its own pack ...
        withheld_id = _pack_id(withheld)
        assert withheld_id != _pack_id(greenfield)
        # ... and says nothing about why it is empty.
        lowered = withheld.lower()
        assert [tell for tell in _TELLS if tell in lowered] == []

    @pytest.mark.parametrize("name", sorted(_CALLS))
    def test_pack_assembled_keeps_the_would_be_pack_apart(
        self, name: str, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        call = _CALLS[name]
        flat = name in _FLAT
        _seed(temp_registry)
        monkeypatch.setenv(RATE_ENV, "0")
        control = _assembled(temp_registry, _pack_id(call()))
        monkeypatch.setenv(RATE_ENV, "1")
        held = _assembled(temp_registry, _pack_id(call()))

        assert (control["holdout"], control["holdout_rate"]) == (False, 0.0)
        assert (held["holdout"], held["holdout_rate"]) == (True, 1.0)

        # Advisories are part of the treatment: withheld whole.
        assert sorted(control["advisory_ids"]) == ["adv-approach", "adv-entity"]
        assert held["advisory_ids"] == []
        assert held["holdout_advisory_ids"] == control["advisory_ids"]

        if flat:
            assert sorted(control["injected_item_ids"]) == [
                "doc-0",
                "doc-1",
                "doc-2",
                "doc-3",
            ]
            assert held["injected_items"] == []
            assert held["injected_item_ids"] == []
            assert sorted(row["item_id"] for row in held["holdout_items"]) == sorted(
                control["injected_item_ids"]
            )
        else:
            assert [s["name"] for s in held["sections"]] == [
                s["name"] for s in control["sections"]
            ]
            assert all(s["item_ids"] == [] for s in held["sections"])
            assert _served_ids(
                {"sections": held["holdout_sections"]}, flat=False
            ) == _served_ids(control, flat=False)
        assert held["injected_item_hashes"] == {}


class TestRateZeroIsTransparent:
    @pytest.mark.parametrize(
        ("name", "legacy"),
        [
            ("get_context", f"No context found for: {INTENT}"),
            ("get_context_index", f"No context found for: {INTENT}"),
            ("search", f"No results found for: {INTENT}"),
        ],
    )
    @pytest.mark.parametrize("rate", [None, "0"])
    def test_an_empty_flat_pack_keeps_its_one_line_answer(
        self,
        name: str,
        legacy: str,
        rate: str | None,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        if rate is not None:
            monkeypatch.setenv(RATE_ENV, rate)
        assert _CALLS[name]() == legacy
        (event,) = temp_registry.operational.event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=10
        )
        assert (event.payload["holdout"], event.payload["holdout_rate"]) == (
            False,
            0.0,
        )

    @pytest.mark.parametrize("name", sorted(_CALLS))
    def test_a_zero_rate_renders_what_an_unset_one_does(
        self, name: str, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(temp_registry)
        unset = _CALLS[name]()
        monkeypatch.setenv(RATE_ENV, "0")
        zero = _CALLS[name]()

        assert _mask(zero) == _mask(unset)
        for response in (unset, zero):
            payload = _assembled(temp_registry, _pack_id(response))
            assert (payload["holdout"], payload["holdout_rate"]) == (False, 0.0)
            assert not {
                "holdout_items",
                "holdout_sections",
                "holdout_advisory_ids",
            } & set(payload)
            assert payload["advisory_ids"]


class TestTheCaptureJoinSeesAWithheldPack:
    """The #694 parser reads a withheld response's ``pack_id``."""

    @staticmethod
    def _parse(tmp_path: Path, response: str) -> Any:
        path = tmp_path / "transcripts" / "session.jsonl"
        write_transcript(
            path,
            [
                user_turn("look up the failover runbook first"),
                assistant_tools(("mcp__trellis__get_context", "toolu-ctx-1")),
                pack_result_turn("toolu-ctx-1", mcp_envelope(response)),
                assistant_turn("nothing on file, so working from scratch"),
            ],
        )
        return parse_session(path)

    def test_a_withheld_response_joins_on_its_pack_id(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _seed(temp_registry)
        monkeypatch.setenv(RATE_ENV, "1")
        withheld = get_context(INTENT)
        pack_id = _pack_id(withheld)
        assert _assembled(temp_registry, pack_id)["holdout"] is True

        digest = self._parse(tmp_path, withheld)

        assert digest.retrieval_results == 1
        assert digest.pack_ids == [pack_id]
        assert digest.pack_ids_unparsed == 0

    def test_at_rate_zero_an_empty_pack_stays_unjoinable_as_before(
        self, temp_registry: StoreRegistry, tmp_path: Path
    ) -> None:
        """The contrast: the one-line answer names no pack, so the parser
        records a retrieval and no id — which is why the flag changes how an
        empty flat pack renders."""
        digest = self._parse(tmp_path, get_context(INTENT))

        assert digest.retrieval_results == 1
        assert digest.pack_ids == []
        assert digest.pack_ids_unparsed == 0
