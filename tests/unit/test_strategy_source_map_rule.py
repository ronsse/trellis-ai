"""Enforcement for the ``component_id`` join rule (#557 D2).

``component_id`` is a join key between three sites that are written
independently and never import each other:

1. the component that resolves its parameters under the id
   (``trellis.retrieve.strategies``);
2. the tuning rule that targets the id
   (``trellis.learning.tuners.rule_tuner.DEFAULT_RULES``);
3. the feedback bridge that stamps the id onto an emitted ``OutcomeEvent``
   (``trellis.feedback.recording``).

Spelled inline at each site they drift, and #557 found them already drifted
in production: the bridge stamped ``retrieve.pack_builder.PackBuilder``
while every shipped rule targeted a strategy, so the tuner's cells and the
rules' targets named different components and the learning loop's two
halves could not meet. It is the same failure
:data:`trellis.schemas.memory_op.REF_TYPE_DOCUMENT` exists to prevent — a
join-key spelling that two independent emitters got different.

The fan-out that reconnects them adds a *fourth* site with the same
property: :data:`~trellis.schemas.outcome.COMPONENT_ID_BY_SOURCE_STRATEGY`
maps a served item's ``strategy_source`` onto the ``component_id`` that
strategy reads its parameters under. A strategy that starts stamping a new
``strategy_source`` and is not added to the map contributes **no** outcome
rows — silently, because dropping an unmappable serving is the correct
behaviour for every *other* reason it can happen.

Two invariants, both derived by AST over ``src/`` rather than from a list.
This repo has shipped rosters that rotted — #443 declared three control
keys against six ``pop`` sites, and three successive lists of
``updated_at`` readers were each wrong — so a hand-maintained roster of
strategies is not an option:

1. **Every ``source_strategy`` a component stamps in ``src/`` has a map
   entry.** This is the one that goes quiet rather than red.
2. **No component id is spelled inline in ``src/``.** One definition site,
   so sites 1-3 above cannot drift again.

``src/`` only. ``tests/`` spells both on purpose: a test asserting
``component_id == "retrieve.pack_builder.PackBuilder"`` is what pins the
constant to a stable wire value, and forbidding it would delete the pin.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.ast_rules import assert_hand_read_floor, iter_modules
from trellis.schemas.outcome import COMPONENT_ID_BY_SOURCE_STRATEGY

#: Metadata key a strategy writes onto every item it returns.
#: ``PackBuilder._promote_strategy_source`` lifts it to the first-class
#: ``PackItem.strategy_source`` field, and the pack's ``PACK_ASSEMBLED``
#: payload carries it into ``injected_items[]`` — which is where the
#: fan-out reads it back.
_METADATA_KEY = "source_strategy"

#: The first-class field the metadata key is promoted to. A strategy could
#: equally stamp it directly, so both spellings are scanned.
_FIELD_NAME = "strategy_source"

#: Prefixes of the ``component_id`` vocabulary. A literal starting with one
#: of these is a component id, wherever it appears.
_COMPONENT_ID_PREFIXES = (
    "retrieve.strategies.",
    "retrieve.rerankers.",
    "retrieve.pack_builder.",
)

#: The one module allowed to spell a component id, because it defines them.
_DEFINITION_SITE = Path("trellis/schemas/outcome.py")


def _src_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "src"
    assert root.is_dir(), f"src not found at {root}"
    return root


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def stamped_strategy_sources(root: Path | None = None) -> dict[str, str]:
    """Every ``source_strategy`` literal ``src/`` stamps → where it is stamped.

    Two shapes, because both are how a strategy could write one:

    * a dict entry ``{"source_strategy": "keyword"}`` — what all four
      shipped strategies do, via ``PackItem(metadata={...})``;
    * a keyword argument ``strategy_source="keyword"`` — the first-class
      field, which a strategy could set directly instead.

    Only *constant* values are collected. ``{"source_strategy": name}`` is
    invisible to a static scan and is deliberately not reported: a scan
    that guessed at computed values would produce failures nobody can act
    on. That is a real hole, and it is why invariant 1 is a floor-guarded
    scan rather than the only thing standing between a new strategy and a
    silent zero — the fan-out also *logs* every source it cannot map.
    """
    found: dict[str, str] = {}
    for py_file, tree in iter_modules(root or _src_root()):
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if _const_str(key) != _METADATA_KEY:
                        continue
                    literal = _const_str(value)
                    if literal:
                        found.setdefault(literal, f"{py_file.name}:{node.lineno}")
            elif isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg != _FIELD_NAME:
                        continue
                    literal = _const_str(kw.value)
                    if literal:
                        found.setdefault(literal, f"{py_file.name}:{node.lineno}")
    return found


def inline_component_ids(root: Path | None = None) -> list[str]:
    """Component-id string literals in ``src/`` outside the definition site.

    Every string constant is checked, not only assignments: an id inlined
    into a ``TuningRule(target_component_id=...)``, a comparison, or a dict
    default is the same drift with a different syntax.
    """
    src = root or _src_root()
    strays: list[str] = []
    for py_file, tree in iter_modules(src):
        if py_file.relative_to(src) == _DEFINITION_SITE:
            continue
        for node in ast.walk(tree):
            literal = _const_str(node)
            if literal and literal.startswith(_COMPONENT_ID_PREFIXES):
                rel = py_file.relative_to(src)
                strays.append(f"{rel}:{node.lineno}: {literal!r}")
    return strays


# ---------------------------------------------------------------------------
# The invariants
# ---------------------------------------------------------------------------


def test_every_stamped_strategy_source_can_be_attributed() -> None:
    """A strategy whose source has no map entry contributes no outcome rows.

    The failure is *silence*, not an error: ``_emit_strategy_outcomes``
    drops an unmappable source on purpose, because an unattributable
    serving must not inflate some other component's denominator. So the
    strategy keeps serving items, keeps being graded, and never reaches the
    tuner — which is #557's defect in a new place.
    """
    stamped = stamped_strategy_sources()
    unmapped = {
        source: site
        for source, site in stamped.items()
        if source not in COMPONENT_ID_BY_SOURCE_STRATEGY
    }
    assert not unmapped, (
        "strategy_source values stamped in src/ with no entry in "
        "COMPONENT_ID_BY_SOURCE_STRATEGY — items they serve are invisible "
        "to the learning loop:\n"
        + "\n".join(
            f"  {source!r} at {site}" for source, site in sorted(unmapped.items())
        )
    )


def test_component_ids_are_spelled_once() -> None:
    """One definition site, so the three join sites cannot drift again."""
    strays = inline_component_ids()
    assert not strays, (
        "component_id spelled inline instead of imported from "
        "trellis.schemas.outcome — this is how the bridge and the tuning "
        "rules came to name different components (#557):\n"
        + "\n".join(f"  {s}" for s in strays)
    )


def test_the_map_only_claims_components_that_serve_items() -> None:
    """Rerankers are deliberately absent, and must stay absent.

    A reranker reorders every candidate and serves none under its own name,
    so no item ever carries its ``strategy_source``. An entry for one could
    never be reached by the fan-out; adding it would look like attribution
    and supply none. ``DEFAULT_RULES``' RRF rule is therefore unreachable
    from this path by construction, which is a fact to state rather than
    paper over.
    """
    assert not [
        component
        for component in COMPONENT_ID_BY_SOURCE_STRATEGY.values()
        if ".rerankers." in component
    ]


# ---------------------------------------------------------------------------
# Vacuity guards
# ---------------------------------------------------------------------------

#: Hand-read off ``src/`` on 2026-09-12: ``strategies.py`` stamps
#: ``keyword``, ``semantic`` and ``graph``; ``observation_strategy.py``
#: stamps ``observation``. A number a person counted is the only floor a
#: silently-narrowing scan cannot also satisfy.
_STAMP_FLOOR = 4

#: Hand-read off ``src/trellis/schemas/outcome.py`` on the same date: seven
#: ``*_COMPONENT_ID`` constants — four strategies, two rerankers, the pack
#: builder. The stray scan divides by the *absence* of literals elsewhere,
#: so it has no population of its own; this floors the definition site
#: instead, which is the thing whose disappearance would make the stray
#: scan trivially satisfiable.
_DEFINITION_FLOOR = 7


def test_the_scan_finds_the_stamps_it_is_meant_to_police() -> None:
    """A scan that stops matching enforces nothing and stays green."""
    stamped = stamped_strategy_sources()
    assert_hand_read_floor(
        len(stamped),
        _STAMP_FLOOR,
        subject="stamped strategy_source literal",
        hint="keyword/semantic/graph in strategies.py, observation in "
        "observation_strategy.py.",
    )


def test_the_definition_site_still_defines_the_vocabulary() -> None:
    """The stray scan is vacuous if the constants stop existing."""
    import trellis.schemas.outcome as outcome_module

    constants = [
        name
        for name in dir(outcome_module)
        if name.endswith("_COMPONENT_ID")
        and isinstance(getattr(outcome_module, name), str)
    ]
    assert_hand_read_floor(
        len(constants),
        _DEFINITION_FLOOR,
        subject="*_COMPONENT_ID constant",
        hint="four strategies, two rerankers, the pack builder.",
    )
    for name in constants:
        assert getattr(outcome_module, name).startswith(_COMPONENT_ID_PREFIXES), name


_SYNTHETIC = """
from trellis.schemas.outcome import KEYWORD_SEARCH_COMPONENT_ID

_MAPPED = KEYWORD_SEARCH_COMPONENT_ID


def serves_a_new_axis():
    # MARK: unmapped-dict
    return PackItem(metadata={"source_strategy": "telepathy"})


def serves_via_the_field():
    # MARK: unmapped-kwarg
    return PackItem(strategy_source="haruspicy")


def serves_a_known_axis():
    return PackItem(metadata={"source_strategy": "keyword"})


def inlines_an_id():
    # MARK: stray
    return TuningRule(target_component_id="retrieve.strategies.KeywordSearch")


def inlines_a_reranker_id():
    # MARK: stray
    if component == "retrieve.rerankers.MMRReranker":
        return None


def computed_source(name):
    # Invisible to a static scan, by design — see stamped_strategy_sources.
    return PackItem(metadata={"source_strategy": name})
"""


@pytest.fixture
def synthetic_tree(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    (root / "trellis" / "retrieve").mkdir(parents=True)
    (root / "trellis" / "schemas").mkdir(parents=True)
    (root / "trellis" / "retrieve" / "axes.py").write_text(_SYNTHETIC)
    # The definition site itself is exempt, and the synthetic tree must
    # prove the exemption rather than assume it: this file spells two ids
    # and must contribute zero strays.
    (root / "trellis" / "schemas" / "outcome.py").write_text(
        'A = "retrieve.strategies.KeywordSearch"\n'
        'B = "retrieve.pack_builder.PackBuilder"\n'
    )
    return root


def test_the_scan_catches_a_new_unmapped_strategy(synthetic_tree: Path) -> None:
    """Run through the *shipped* predicate, not a copy of it."""
    stamped = stamped_strategy_sources(synthetic_tree)
    unmapped = set(stamped) - set(COMPONENT_ID_BY_SOURCE_STRATEGY)
    assert unmapped == {"telepathy", "haruspicy"}, stamped
    # The mapped one is seen too — a scan that only reported violations
    # could be narrowing without the floor above noticing.
    assert "keyword" in stamped


def test_the_scan_catches_a_re_inlined_component_id(synthetic_tree: Path) -> None:
    strays = inline_component_ids(synthetic_tree)
    assert len(strays) == 2, strays
    assert all("axes.py" in s for s in strays), strays
    assert not [s for s in strays if "outcome.py" in s], strays
