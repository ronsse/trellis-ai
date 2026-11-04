"""Implicit signals and disclosure tiers in ``analyze value`` (report-only).

Two cheap signals sit on disk beside the explicit verdicts: an agent that
fetches an item's full body, and a second pack assembled in the same session
soon after the first. ``summarize_pack_value`` reports both in an ``implicit``
block, and splits served tokens by disclosure tier in ``by_tier``, without
touching a field that existed before them.

The properties guarded here:

* the existing fields are byte-identical (the pin against a golden captured
  from the reader as it stood before this change);
* a fetch joins by ``(pack_id, item_id)`` and every miss is counted by reason;
* agreement on the fetched-and-graded overlap is withheld below its floor;
* the re-retrieval window is inclusive at its edge, per session, and a pack
  with no session is counted apart;
* a tier is read off the payload, never inferred, and is ``unknown`` when no
  row records it;
* a held-out pack is in none of it;
* nothing in ``src/`` outside the reader, the CLI and the API route reads any
  of it, because a missing fetch is an absence of evidence and demotion needs
  evidence of unhelpfulness.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.ast_rules import assert_hand_read_floor, iter_modules
from tests.unit.retrieve.test_pack_value import _FakeEventLog
from trellis.retrieve import pack_value
from trellis.retrieve.disclosure import POINTER_SELECTION_REASON
from trellis.retrieve.pack_value import MIN_ATTRIBUTED_PACKS, summarize_pack_value
from trellis.stores.base.event_log import Event, EventType

_T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
_SINCE = _T0 - timedelta(days=1)
_GOLDEN = Path(__file__).parent / "pack_value_pre_implicit.json"
#: Added to the report by this change; every other key is pinned byte for byte.
_NEW_KEYS = ("implicit", "by_tier")


def _put(
    log: _FakeEventLog,
    event_type: EventType,
    payload: dict[str, Any],
    *,
    at: datetime,
    entity_id: str | None = None,
    entity_type: str | None = None,
) -> None:
    log.append(
        Event(
            event_type=event_type,
            source="test",
            entity_id=entity_id,
            entity_type=entity_type,
            payload=payload,
            occurred_at=at,
        )
    )


def _pack(
    log: _FakeEventLog,
    pack_id: str,
    rows: list[tuple[str, int]],
    *,
    at: datetime = _T0,
    session: str | None = None,
    pointers: tuple[str, ...] = (),
    disclosure: str | None = "graduated",
    index_mode: bool | None = False,
    holdout: bool = False,
) -> None:
    """A flat ``PACK_ASSEMBLED``. ``rows`` are ``(item_id, tokens)``.

    ``disclosure`` / ``index_mode`` of ``None`` leave the key off, which is how
    a pack from before the disclosure telemetry reads.
    """
    payload: dict[str, Any] = {
        "intent_family": "general_context",
        "injected_item_ids": [item_id for item_id, _ in rows],
        "injected_items": [
            {
                "item_id": item_id,
                "estimated_tokens": tokens,
                "strategy_source": "semantic",
                "item_type": "document",
                "rank": rank,
                **(
                    {"selection_reason": POINTER_SELECTION_REASON}
                    if item_id in pointers
                    else {}
                ),
            }
            for rank, (item_id, tokens) in enumerate(rows)
        ],
    }
    if session is not None:
        payload["session_id"] = session
    if disclosure is not None:
        payload["disclosure"] = {"mode": disclosure, "pointer_count": len(pointers)}
    if index_mode is not None:
        payload["index_mode"] = index_mode
    if holdout:
        payload["holdout"] = True
    _put(
        log,
        EventType.PACK_ASSEMBLED,
        payload,
        at=at,
        entity_id=pack_id,
        entity_type="pack",
    )


def _feedback(
    log: _FakeEventLog,
    pack_id: str,
    *,
    helpful: tuple[str, ...] = (),
    unhelpful: tuple[str, ...] = (),
    at: datetime = _T0,
) -> None:
    _put(
        log,
        EventType.FEEDBACK_RECORDED,
        {
            "pack_id": pack_id,
            "helpful_item_ids": list(helpful),
            "unhelpful_item_ids": list(unhelpful),
            "intent_family": "general_context",
            "rating": 0.5,
            "success": True,
        },
        at=at + timedelta(seconds=5),
        entity_id=pack_id,
    )


def _fetch(
    log: _FakeEventLog,
    pack_id: str | None,
    served: list[str],
    *,
    requested: list[str] | None = None,
    at: datetime = _T0,
) -> None:
    """A ``PACK_ITEMS_FETCHED`` as the ``get_items`` emitter writes it."""
    _put(
        log,
        EventType.PACK_ITEMS_FETCHED,
        {
            "pack_id": pack_id,
            "requested_item_ids": requested if requested is not None else served,
            "served_item_ids": served,
        },
        at=at + timedelta(seconds=10),
        entity_id=pack_id,
        entity_type="pack" if pack_id else None,
    )


def _report(log: _FakeEventLog) -> pack_value.PackValueReport:
    return summarize_pack_value(
        log, since=_SINCE, model="claude-opus", price_per_mtok=15.0
    )


def _seed_pin_scenario(log: _FakeEventLog) -> None:
    """Every axis, a mix of tiers, fetches and sessions.

    Enough that each pre-existing field is non-trivial, and that the new
    signals have something to say that could leak into one of them.
    """
    for index in range(6):
        pack_id = f"pin_{index}"
        a, b, c = f"doc:{pack_id}_a", f"entity:{pack_id}_b", f"{pack_id}_c"
        _pack(
            log,
            pack_id,
            [(a, 100), (b, 50), (c, 10)],
            at=_T0 + timedelta(minutes=index * 7),
            session="s-even" if index % 2 == 0 else "s-odd",
            pointers=(c,) if index % 3 else (),
            index_mode=False if index != 5 else None,
            disclosure="graduated" if index != 5 else None,
        )
        _feedback(
            log,
            pack_id,
            helpful=(a,),
            unhelpful=(b,) if index < 4 else (),
            at=_T0 + timedelta(minutes=index * 7),
        )
    _fetch(log, "pin_0", ["doc:pin_0_a", "pin_0_c"], at=_T0)
    _fetch(log, "pin_1", ["pin_1_c"], at=_T0)
    _fetch(log, "not-a-pack", ["x"], at=_T0)
    _fetch(log, None, ["y"], at=_T0)
    _put(
        log,
        EventType.TOKEN_TRACKED,
        {"pack_id": "pin_0", "response_tokens": 321},
        at=_T0,
    )


# ---------------------------------------------------------------------------
# The existing fields are byte-identical
# ---------------------------------------------------------------------------


def _pre_existing(report: pack_value.PackValueReport) -> str:
    dump = report.model_dump()
    return json.dumps({k: v for k, v in dump.items() if k not in _NEW_KEYS})


def test_existing_fields_are_byte_identical_to_the_pre_change_reader() -> None:
    """Every key the report had before this change, value and order.

    ``_GOLDEN`` was captured by running ``_seed_pin_scenario`` through the
    reader on the commit before this change. An additive block that quietly
    moved a headline number, folded a fetch into ``useful_token_fraction`` or
    reordered a key fails here.
    """
    log = _FakeEventLog()
    _seed_pin_scenario(log)
    report = _report(log)

    assert _pre_existing(report) == _GOLDEN.read_text(encoding="utf-8").strip()
    assert set(report.model_dump()) - set(json.loads(_GOLDEN.read_text())) == set(
        _NEW_KEYS
    )
    # The pin is not vacuous: the new blocks have content from the same log.
    assert report.implicit.fetched_items > 0
    assert {cell.tier for cell in report.by_tier if cell.items} >= {"body", "pointer"}


# ---------------------------------------------------------------------------
# Fetch, joined and unjoined
# ---------------------------------------------------------------------------


def test_fetch_joins_by_pack_and_item_and_counts_every_miss() -> None:
    log = _FakeEventLog()
    for index in range(6):
        pack_id = f"p{index}"
        _pack(
            log,
            pack_id,
            [(f"{pack_id}_a", 100), (f"{pack_id}_b", 50), (f"{pack_id}_c", 10)],
        )
        _feedback(log, pack_id, helpful=(f"{pack_id}_a",), unhelpful=(f"{pack_id}_b",))
    _fetch(log, "p0", ["p0_a", "p0_c"])
    _fetch(log, "p1", ["p1_c"])
    _fetch(log, "p0", ["p0_a"])  # the same item twice is one fetched item
    _fetch(log, "ghost", ["ghost_x"])  # a pack outside the window
    _fetch(log, None, ["orphan_1", "orphan_2"])  # names no pack
    _fetch(log, "p2", ["p2_zzz"])  # a pack in the window, an item it never served
    _fetch(log, "p3", [], requested=["p3_a", "p3_b"])  # asked for, none served

    implicit = _report(log).implicit

    assert implicit.fetch_events == 7
    assert implicit.fetched_items == 3  # p0_a, p0_c, p1_c
    assert implicit.fetched_tokens == 100 + 10 + 10
    assert implicit.injected_tokens == 6 * 160
    assert implicit.fetched_token_fraction == pytest.approx(120 / 960, abs=1e-4)
    assert implicit.fetched_unjudged_tokens == 20  # the two ungraded c items
    assert implicit.unjudged_tokens == 60
    assert implicit.fetched_unjudged_token_share == pytest.approx(20 / 60, abs=1e-4)
    assert implicit.fetch_unjoined == 4
    assert implicit.fetch_unjoined_no_pack == 2
    assert implicit.fetch_unjoined_pack_not_in_window == 1
    assert implicit.fetch_unjoined_item_not_served == 1
    assert implicit.fetch_unjoined == (
        implicit.fetch_unjoined_no_pack
        + implicit.fetch_unjoined_pack_not_in_window
        + implicit.fetch_unjoined_item_not_served
    )
    assert implicit.fetch_suppressed is False


def test_fetch_share_is_refused_below_the_pack_minimum_but_counts_print() -> None:
    log = _FakeEventLog()
    for index in range(MIN_ATTRIBUTED_PACKS - 1):
        pack_id = f"t{index}"
        _pack(log, pack_id, [(f"{pack_id}_a", 100)])
        _feedback(log, pack_id, helpful=(f"{pack_id}_a",))
    _fetch(log, "t0", ["t0_a"])

    implicit = _report(log).implicit

    assert implicit.fetch_suppressed is True
    assert implicit.fetch_suppressed_reason == pack_value.SUPPRESSED_THIN_PACK_SAMPLE
    assert implicit.fetched_token_fraction is None
    assert implicit.fetched_unjudged_token_share is None
    assert implicit.fetched_items == 1
    assert implicit.fetched_tokens == 100


def test_no_fetch_events_is_zero_not_an_error() -> None:
    log = _FakeEventLog()
    _pack(log, "only", [("only_a", 10)])
    implicit = _report(log).implicit
    assert (implicit.fetch_events, implicit.fetched_items, implicit.fetch_unjoined) == (
        0,
        0,
        0,
    )


# ---------------------------------------------------------------------------
# Agreement on the overlap, and its floor
# ---------------------------------------------------------------------------


def _agreement_world(*, fetched_graded: int) -> _FakeEventLog:
    """40 packs, items ``a`` helpful and ``b`` unhelpful; some ``b`` fetched."""
    log = _FakeEventLog()
    for index in range(40):
        pack_id = f"g{index}"
        _pack(log, pack_id, [(f"{pack_id}_a", 10), (f"{pack_id}_b", 10)])
        _feedback(log, pack_id, helpful=(f"{pack_id}_a",), unhelpful=(f"{pack_id}_b",))
    # ``fetched_graded`` fetches, every one an ``a`` (graded helpful) up to 40,
    # so the overlap is exactly that many servings.
    for index in range(fetched_graded):
        _fetch(log, f"g{index}", [f"g{index}_a"])
    return log


def test_agreement_matches_hand_counts() -> None:
    log = _agreement_world(fetched_graded=40)
    # Add 10 fetched-and-unhelpful servings: overlap 50, 40 of them helpful.
    for index in range(10):
        _fetch(log, f"g{index}", [f"g{index}_b"])

    implicit = _report(log).implicit

    assert implicit.graded_servings == 80
    assert implicit.graded_helpful_servings == 40
    assert implicit.fetched_graded_servings == 50
    assert implicit.fetched_graded_helpful_servings == 40
    assert implicit.p_helpful == pytest.approx(40 / 80, abs=1e-4)
    assert implicit.p_helpful_given_fetched == pytest.approx(40 / 50, abs=1e-4)
    assert implicit.agreement_suppressed is False
    assert implicit.agreement_suppressed_reason == ""
    assert implicit.min_fetch_overlap == 30


def test_agreement_is_withheld_one_below_the_floor_and_stated_at_it() -> None:
    below = _report(_agreement_world(fetched_graded=29)).implicit
    at_floor = _report(_agreement_world(fetched_graded=30)).implicit

    assert below.fetched_graded_servings == 29
    assert below.agreement_suppressed is True
    assert below.agreement_suppressed_reason == pack_value.SUPPRESSED_THIN_FETCH_OVERLAP
    # Both ratios go together, so neither can be read against a refused other.
    assert below.p_helpful is None
    assert below.p_helpful_given_fetched is None
    # The counts still print: only the ratios are refused.
    assert below.graded_servings == 80
    assert below.fetched_graded_helpful_servings == 29

    assert at_floor.agreement_suppressed is False
    assert at_floor.p_helpful_given_fetched == pytest.approx(1.0, abs=1e-4)
    assert at_floor.p_helpful == pytest.approx(0.5, abs=1e-4)


# ---------------------------------------------------------------------------
# Re-retrieval window
# ---------------------------------------------------------------------------


def _two_packs(gap: timedelta, *, first: str | None, second: str | None) -> Any:
    log = _FakeEventLog()
    _pack(log, "r1", [("r1_a", 10)], at=_T0, session=first)
    _pack(log, "r2", [("r2_a", 10)], at=_T0 + gap, session=second)
    return _report(log).implicit


@pytest.mark.parametrize(
    ("gap", "expected"),
    [
        (timedelta(minutes=29, seconds=59), 1),
        (timedelta(minutes=30), 1),  # the edge is inclusive
        (timedelta(minutes=30, seconds=1), 0),
        (timedelta(seconds=1), 1),
    ],
)
def test_re_retrieval_window_edges(gap: timedelta, expected: int) -> None:
    implicit = _two_packs(gap, first="s", second="s")
    assert (
        implicit.re_retrieval_window_minutes == pack_value.RE_RETRIEVAL_WINDOW_MINUTES
    )
    assert pack_value.RE_RETRIEVAL_WINDOW_MINUTES == 30
    assert implicit.re_retrieved_packs == expected
    assert implicit.packs_with_session == 2
    assert implicit.packs_without_session == 0


def test_re_retrieval_ignores_another_session() -> None:
    implicit = _two_packs(timedelta(minutes=1), first="s1", second="s2")
    assert implicit.re_retrieved_packs == 0
    assert implicit.packs_with_session == 2


def test_packs_without_a_session_are_counted_apart_never_paired() -> None:
    implicit = _two_packs(timedelta(minutes=1), first=None, second=None)
    assert implicit.re_retrieved_packs == 0
    assert implicit.packs_with_session == 0
    assert implicit.packs_without_session == 2
    # A blank session is no session.
    blank = _two_packs(timedelta(minutes=1), first="  ", second="  ")
    assert (blank.re_retrieved_packs, blank.packs_without_session) == (0, 2)


def test_re_retrieval_measures_to_the_next_pack_not_any_later_one() -> None:
    log = _FakeEventLog()
    for pack_id, minutes in (("n1", 0), ("n2", 31), ("n3", 40)):
        _pack(
            log,
            pack_id,
            [(f"{pack_id}_a", 10)],
            at=_T0 + timedelta(minutes=minutes),
            session="s",
        )
    # n1 -> n2 is 31 minutes (outside), n2 -> n3 is 9 (inside): one pack.
    assert _report(log).implicit.re_retrieved_packs == 1

    chain = _FakeEventLog()
    for pack_id, minutes in (("c1", 0), ("c2", 10), ("c3", 20)):
        _pack(
            chain,
            pack_id,
            [(f"{pack_id}_a", 10)],
            at=_T0 + timedelta(minutes=minutes),
            session="s",
        )
    assert _report(chain).implicit.re_retrieved_packs == 2  # the last has no successor


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------


def _tier_world() -> _FakeEventLog:
    """Five attributed packs, four recorded shapes, hand-counted below."""
    log = _FakeEventLog()
    for pack_id in ("tg1", "tg2"):  # graduated: two bodies, two pointers
        _pack(
            log,
            pack_id,
            [
                (f"{pack_id}_b1", 100),
                (f"{pack_id}_b2", 100),
                (f"{pack_id}_p1", 20),
                (f"{pack_id}_p2", 20),
            ],
            pointers=(f"{pack_id}_p1", f"{pack_id}_p2"),
        )
        _feedback(
            log,
            pack_id,
            helpful=(f"{pack_id}_b1", f"{pack_id}_p2"),
            unhelpful=(f"{pack_id}_b2",),
        )
    # From before disclosure was recorded: nothing says what tier a row was.
    _pack(log, "tl", [("tl_1", 40), ("tl_2", 40)], disclosure=None, index_mode=None)
    _feedback(log, "tl", helpful=("tl_1",))
    # An index pack: every row is an index line, though none carries the stamp.
    _pack(log, "ti", [("ti_1", 10), ("ti_2", 10)], index_mode=True)
    _feedback(log, "ti", helpful=("ti_1",))
    # Disclosure off and no index: every row kept its excerpt.
    _pack(log, "to", [("to_1", 30), ("to_2", 30)], disclosure="off", index_mode=False)
    _feedback(log, "to", unhelpful=("to_1",))
    return log


def test_tier_split_is_hand_counted_and_includes_unknown() -> None:
    report = _report(_tier_world())
    tiers = {cell.tier: cell for cell in report.by_tier}

    assert list(tiers) == ["body", "pointer", "unknown"]
    body, pointer, unknown = tiers["body"], tiers["pointer"], tiers["unknown"]
    assert (body.items, body.injected_tokens) == (6, 460)
    assert (body.helpful_tokens, body.unhelpful_tokens, body.unjudged_tokens) == (
        200,
        230,
        30,
    )
    assert (pointer.items, pointer.injected_tokens) == (6, 100)
    assert (
        pointer.helpful_tokens,
        pointer.unhelpful_tokens,
        pointer.unjudged_tokens,
    ) == (50, 0, 50)
    # Rank 0 and rank 1 rows, and still ``unknown``: never inferred from rank.
    assert (unknown.items, unknown.injected_tokens, unknown.unjudged_tokens) == (
        2,
        80,
        40,
    )

    assert (
        sum(cell.injected_tokens for cell in report.by_tier)
        == report.injected_tokens
        == 640
    )
    assert (
        sum(cell.helpful_tokens for cell in report.by_tier)
        == report.helpful_tokens
        == 290
    )
    assert (
        sum(cell.unhelpful_tokens for cell in report.by_tier)
        == report.unhelpful_tokens
        == 230
    )
    assert (
        sum(cell.unjudged_tokens for cell in report.by_tier)
        == report.unjudged_tokens
        == 120
    )
    assert body.unjudged_token_fraction == pytest.approx(30 / 460, abs=1e-4)
    assert pointer.unjudged_token_fraction == pytest.approx(0.5, abs=1e-4)
    assert pointer.share_of_unjudged_tokens == pytest.approx(50 / 120, abs=1e-4)
    assert not any(cell.suppressed for cell in report.by_tier)


def test_tier_fractions_are_refused_below_the_pack_minimum() -> None:
    log = _FakeEventLog()
    _pack(log, "one", [("one_a", 10)], pointers=("one_a",))
    _feedback(log, "one", helpful=("one_a",))
    cells = {cell.tier: cell for cell in _report(log).by_tier}
    assert cells["pointer"].items == 1
    assert cells["pointer"].suppressed is True
    assert cells["pointer"].suppressed_reason == pack_value.SUPPRESSED_THIN_SAMPLE
    assert cells["pointer"].unjudged_token_fraction is None


@pytest.mark.parametrize(
    ("row", "payload", "expected"),
    [
        ({"selection_reason": "disclosure_pointer"}, {}, "pointer"),
        ({"selection_reason": "disclosure_pointer"}, {"index_mode": False}, "pointer"),
        ({}, {"index_mode": True}, "pointer"),
        ({}, {"index_mode": False, "disclosure": {"mode": "graduated"}}, "body"),
        ({}, {"index_mode": False, "disclosure": {"mode": "off"}}, "body"),
        ({"rank": 0}, {}, "unknown"),
        ({"rank": 99}, {}, "unknown"),
        ({}, {"index_mode": False}, "unknown"),
        ({}, {"disclosure": {"mode": "graduated"}}, "unknown"),
        ({}, {"index_mode": False, "disclosure": {"mode": "nonsense"}}, "unknown"),
        ({}, {"index_mode": False, "disclosure": "graduated"}, "unknown"),
        ({"selection_reason": "semantic"}, {"index_mode": None}, "unknown"),
    ],
)
def test_served_tier_reads_the_payload_and_never_infers(
    row: dict[str, Any], payload: dict[str, Any], expected: str
) -> None:
    assert pack_value.served_tier(row, payload) == expected


# ---------------------------------------------------------------------------
# Holdout
# ---------------------------------------------------------------------------


def test_a_held_out_pack_is_in_none_of_it() -> None:
    log = _FakeEventLog()
    for index in range(5):
        pack_id = f"live{index}"
        _pack(
            log,
            pack_id,
            [(f"{pack_id}_a", 10)],
            session="live",
            at=_T0 + timedelta(minutes=index * 40),
        )
        _feedback(log, pack_id, helpful=(f"{pack_id}_a",))
    # A withheld pack: 1,000 tokens, in the same session two minutes after a
    # live one, fetched from, and graded.
    _pack(
        log,
        "held",
        [("held_a", 1000)],
        session="live",
        at=_T0 + timedelta(minutes=2),
        holdout=True,
        pointers=("held_a",),
    )
    _feedback(log, "held", helpful=("held_a",))
    _fetch(log, "held", ["held_a"])

    report = _report(log)
    implicit = report.implicit

    assert implicit.packs == 5
    assert implicit.injected_tokens == 50
    assert implicit.fetched_items == 0
    assert (
        implicit.fetch_unjoined_pack_not_in_window == 1
    )  # counted, not silently dropped
    assert implicit.re_retrieved_packs == 0  # the 2-minute gap is to a dropped pack
    assert implicit.packs_with_session == 5
    assert {cell.tier: cell.items for cell in report.by_tier}["pointer"] == 0
    assert report.injected_tokens == 50


# ---------------------------------------------------------------------------
# Nothing feeds anything
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parents[3] / "src"
#: The only modules that may name the new surface: the reader, the CLI that
#: prints it, and the REST route that serves it. ``analyze trend`` composes
#: ``summarize_pack_value`` and must not read it.
_MAY_READ = frozenset(
    {
        "trellis/retrieve/pack_value.py",
        "trellis_cli/analyze.py",
        "trellis_api/routes/analyze.py",
    }
)
_NAMES = frozenset(
    {
        "ImplicitSignals",
        "TierBreakdown",
        "served_tier",
        "TIER_BODY",
        "TIER_POINTER",
        "TIER_UNKNOWN",
        "MIN_FETCH_OVERLAP",
        "RE_RETRIEVAL_WINDOW_MINUTES",
        "SUPPRESSED_THIN_FETCH_OVERLAP",
        "SUPPRESSED_THIN_PACK_SAMPLE",
    }
)
#: Report attributes, read as ``report.implicit`` or as a dumped dict's key.
_FIELDS = frozenset({"implicit", "by_tier"})


def _references(tree: ast.AST) -> list[int]:
    """Lines where *tree* names the implicit-signal surface, however spelled."""
    lines: list[int] = []
    for node in ast.walk(tree):
        hit = False
        if isinstance(node, ast.ImportFrom):
            hit = any(alias.name in _NAMES for alias in node.names)
        elif isinstance(node, ast.Name):
            hit = node.id in _NAMES
        elif isinstance(node, ast.Attribute):
            hit = node.attr in _NAMES or node.attr in _FIELDS
        elif isinstance(node, ast.Subscript):
            hit = isinstance(node.slice, ast.Constant) and node.slice.value in _FIELDS
        elif isinstance(node, ast.Call):
            is_read = (
                isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            ) or (isinstance(node.func, ast.Name) and node.func.id == "getattr")
            hit = is_read and any(
                isinstance(arg, ast.Constant) and arg.value in _FIELDS
                for arg in node.args
            )
        if hit:
            lines.append(node.lineno)  # type: ignore[attr-defined]
    return lines


def _module_hits(root: Path) -> dict[str, list[int]]:
    hits: dict[str, list[int]] = {}
    for path, tree in iter_modules(root):
        lines = _references(tree)
        if lines:
            hits[path.relative_to(root).as_posix()] = lines
    return hits


def test_the_implicit_signals_feed_nothing() -> None:
    """Only the reader, the CLI and the API route name the new surface.

    A fetch is a positive hint and its absence is no evidence an item was
    unhelpful; the project rule is that demotion needs evidence of
    unhelpfulness. So no ``learning/``, ``classify/``, demotion, promotion or
    precedent module may read these, and neither may ``analyze trend``.
    """
    hits = _module_hits(_SRC)

    stray = sorted(set(hits) - _MAY_READ)
    assert not stray, (
        f"{stray} reference the implicit-signal / tier surface of "
        "analyze value. It is report-only; see this test's docstring."
    )
    # Hand-read: the reader and the CLI printer both name it; the REST route
    # serves ``model_dump()`` and so names nothing. A scan that found fewer
    # has stopped matching.
    assert_hand_read_floor(
        len(hits), 2, subject="modules that name the implicit-signal surface"
    )
    assert {"trellis/retrieve/pack_value.py", "trellis_cli/analyze.py"} <= set(hits)


@pytest.mark.parametrize(
    "source",
    [
        "from trellis.retrieve.pack_value import ImplicitSignals",
        "from trellis.retrieve.pack_value import served_tier as st",
        "import trellis.retrieve.pack_value as pv\nx = pv.TierBreakdown",
        "x = pv.MIN_FETCH_OVERLAP",
        "x = report.implicit.fetched_items",
        "x = report.by_tier",
        "x = dumped['implicit']",
        "x = dumped.get('by_tier')",
        "x = getattr(report, 'implicit')",
        "def f(r):\n    return [c for c in r.by_tier if c.items]",
    ],
)
def test_the_scan_sees_every_spelling_a_consumer_could_use(source: str) -> None:
    assert _references(ast.parse(source)), source


def test_the_scan_leaves_unrelated_code_alone() -> None:
    clean = (
        "from trellis.retrieve.pack_value import summarize_pack_value\n"
        "x = summarize_pack_value(log).useful_token_fraction\n"
        "y = d.get('scan')\n"
        "z = getattr(o, 'name')\n"
    )
    assert _references(ast.parse(clean)) == []
