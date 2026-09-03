"""The one PackBuilder construction, and the axis report over it (#410).

Two things are pinned here. That every pack surface builds the *same*
builder — asserted structurally, over ``src/``, because a roster of
surfaces rots and this repo has watched it rot (#443 declared three
control keys against six call sites). And that
:func:`~trellis.retrieve.builder_factory.describe_axes` distinguishes the
four things "the semantic axis is not in this pack" can mean, since
``build_strategies`` reports all four identically: as absence.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from trellis.retrieve.builder_factory import (
    SEMANTIC_AXIS_NOTES,
    build_pack_builder,
    describe_axes,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryEvidence
from trellis.stores.advisory_source import ADVISORY_FILENAME
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from trellis.retrieve.strategies import SearchStrategy


class _NamedStrategy:
    """Minimal SearchStrategy stand-in — only ``name`` is under test here."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def search(self, query: str, **kwargs: Any) -> list[Any]:
        return []


def _builder(*names: str) -> PackBuilder:
    strategies: list[SearchStrategy] = [_NamedStrategy(n) for n in names]  # type: ignore[misc]
    return PackBuilder(strategies=strategies)


class TestStrategyNames:
    def test_names_come_from_the_strategies_not_a_constant(self) -> None:
        assert _builder("keyword", "graph").strategy_names == ["keyword", "graph"]
        assert _builder("graph").strategy_names == ["graph"]
        assert _builder().strategy_names == []

    def test_names_follow_add_strategy(self) -> None:
        builder = _builder("keyword")
        builder.add_strategy(_NamedStrategy("semantic"))  # type: ignore[arg-type]
        assert builder.strategy_names == ["keyword", "semantic"]


class TestDescribeAxes:
    """Four states, and none of them is reachable by a constant.

    ``build_strategies`` appends the semantic axis only when an embedder
    resolves, and swallows a vector-backend init failure with a log line
    the CLI's ``WARNING`` default never prints — so "absent" covers a
    deployment that never had the axis, one whose backend is broken, and
    one whose axis raised during this build. Those want three different
    fixes, so they get three different words.
    """

    def test_present_and_ran(self) -> None:
        axes = describe_axes(
            _builder("keyword", "graph", "semantic"),
            ["keyword", "graph", "semantic"],
            embedder_configured=True,
        )
        assert axes == {
            "available": ["keyword", "graph", "semantic"],
            "ran": ["keyword", "graph", "semantic"],
            "failed": [],
            "semantic": "ran",
        }
        assert SEMANTIC_AXIS_NOTES[axes["semantic"]] == ""

    def test_absent_with_no_embedder_is_not_configured(self) -> None:
        axes = describe_axes(
            _builder("keyword", "graph"),
            ["keyword", "graph"],
            embedder_configured=False,
        )
        assert axes["semantic"] == "not_configured"
        assert "no embeddings provider" in SEMANTIC_AXIS_NOTES[axes["semantic"]]

    def test_absent_with_an_embedder_is_misconfigured(self) -> None:
        """The case a boolean "is semantic missing?" would have merged.

        An embedder resolved and the axis is still gone, which means the
        vector backend failed to initialise and ``build_strategies``
        logged and carried on. Telling the operator "configure an
        embedder" here would send them to fix something already correct.
        """
        axes = describe_axes(
            _builder("keyword", "graph"),
            ["keyword", "graph"],
            embedder_configured=True,
        )
        assert axes["semantic"] == "misconfigured"
        assert "did not initialise" in SEMANTIC_AXIS_NOTES[axes["semantic"]]

    def test_present_but_did_not_run_is_failed(self) -> None:
        axes = describe_axes(
            _builder("keyword", "graph", "semantic"),
            ["keyword", "graph"],
            embedder_configured=True,
        )
        assert axes["semantic"] == "failed"
        assert axes["failed"] == ["semantic"]
        # ``available`` is what the builder *has*, and here it is strictly
        # larger than what ran. Without this the returned ``available``
        # could just be ``ran`` echoed back and every other case in this
        # class — where the two coincide — would still pass.
        assert axes["available"] == ["keyword", "graph", "semantic"]
        assert axes["ran"] == ["keyword", "graph"]
        assert "failed during this build" in SEMANTIC_AXIS_NOTES[axes["semantic"]]

    def test_a_non_semantic_axis_failure_is_reported_without_blaming_semantic(
        self,
    ) -> None:
        """``failed`` is every axis, ``semantic`` is only about that one.

        Without a non-semantic member in the fixture, ``failed`` could be
        hard-coded to the semantic axis and every other case here would
        still pass.
        """
        axes = describe_axes(
            _builder("keyword", "graph", "semantic"),
            ["keyword", "semantic"],
            embedder_configured=True,
        )
        assert axes["failed"] == ["graph"]
        assert axes["semantic"] == "ran"


class TestBuildPackBuilder:
    """The construction itself, asserted through behaviour not attributes."""

    @staticmethod
    def _registry(tmp_path: Path) -> StoreRegistry:
        config_dir = tmp_path / "config"
        data_dir = tmp_path / "data"
        (data_dir / "stores").mkdir(parents=True)
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(f"data_dir: {data_dir}\n")
        return StoreRegistry.from_config_dir(config_dir=config_dir, data_dir=data_dir)

    def test_wires_the_axes_the_deployment_has(self, tmp_path: Path) -> None:
        builder = build_pack_builder(self._registry(tmp_path), surface="test")
        # No embedder configured in this registry, so no semantic axis —
        # which is exactly what ``describe_axes`` is for.
        assert builder.strategy_names == ["keyword", "graph"]

    def test_the_pack_carries_a_pack_id_and_an_event(self, tmp_path: Path) -> None:
        registry = self._registry(tmp_path)
        builder = build_pack_builder(registry, surface="test")
        registry.knowledge.document_store.put("d1", "a canary rollout runbook body")

        pack = builder.build("canary rollout")

        assert pack.pack_id
        from trellis.stores.base.event_log import EventType

        events = registry.operational.event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=10
        )
        assert [e.entity_id for e in events] == [pack.pack_id]

    def test_advisories_are_wired(self, tmp_path: Path) -> None:
        """The half that had already drifted.

        ``trellis analyze pack-quality`` built its own PackBuilder and
        passed **no advisory store**, under a comment claiming it mirrored
        the MCP / API wire-up — so a scenario was scored against a builder
        production does not use. Pinning it here is what makes the fourth
        copy's removal a fix rather than a tidy-up.
        """
        registry = self._registry(tmp_path)
        store = AdvisoryStore(Path(registry.stores_dir) / ADVISORY_FILENAME)
        store.put(
            Advisory(
                category=AdvisoryCategory.ENTITY,
                confidence=0.8,
                message="prefer the canary path",
                evidence=AdvisoryEvidence(
                    sample_size=10,
                    success_rate_with=0.8,
                    success_rate_without=0.4,
                    effect_size=0.4,
                ),
                scope="global",
            )
        )

        pack = build_pack_builder(registry, surface="test").build("anything")

        assert [a.message for a in pack.advisories] == ["prefer the canary path"]


# ---------------------------------------------------------------------------
# The structural rule
# ---------------------------------------------------------------------------

#: Path, relative to ``src/``, allowed to construct a ``PackBuilder``. The
#: factory is the construction; nothing else builds one. Kept as a *set of
#: one* rather than an "everything except" check so adding a surface is a
#: deliberate edit to this line with a reviewer looking at it.
#:
#: Matched on the ``src``-relative path, **not** on ``path.name``: a bare
#: basename exempts *any* file called ``builder_factory.py`` anywhere under
#: ``src/``, which is a one-``mkdir`` hole in a rule whose whole job is to
#: stop a fifth wiring appearing somewhere new.
_ALLOWED_CONSTRUCTION_SITES = frozenset({"trellis/retrieve/builder_factory.py"})

#: Hand-read floor on the module scan, so a narrowed ``rglob`` cannot make
#: the rule vacuous while every guard below still divides by the scan's own
#: output — the #464 shape, where two "independent" counters shared one file
#: discovery and narrowing it took eight real sites to six with everything
#: green. Bare ``mypy src/`` — the command CI runs, and the only one to use
#: after #398 withdrew the ``--python-version 3.12`` workaround — reports 333
#: source files;
#: the floor is deliberately loose enough to survive ordinary churn and
#: tight enough that dropping a package fails here.
_MIN_SRC_MODULES = 300


def _src_root() -> Path:
    root = Path(__file__).resolve().parents[3] / "src"
    assert root.is_dir(), f"src/ not found at {root}"
    return root


def _bare_name(node: ast.expr) -> str | None:
    """Bare name of a call target: ``f``, ``mod.f``, ``a.b.f`` all give ``f``.

    The same shape ``tests/unit/test_policy_gate_rule.py::_name_of`` and
    ``tests/unit/mcp/test_capture_surface_roster.py::_is_call_to`` already
    use, and for the same reason: ``src/`` writes 563 class-like
    ``module.Attr(...)`` calls across 33 names against 1,536 bare-name ones
    (measured 2026-09-03), so a rule that reads only :class:`ast.Name`
    misses the repo's second-most-common call spelling — and
    ``from trellis.retrieve import pack_builder`` is idiomatic here.

    Over-collecting costs a spurious flag on a same-named attribute;
    under-collecting costs the rule.
    """
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _construction_names(tree: ast.Module) -> set[str]:
    """Every local name in this module that constructs a ``PackBuilder``.

    ``PackBuilder`` itself, plus the three rebindings that produce one
    under another name and would otherwise walk past a literal match:

    * ``from ...pack_builder import PackBuilder as Builder`` → ``Builder``
    * ``PB = PackBuilder`` → ``PB``
    * ``class Mine(PackBuilder)`` → ``Mine`` (a subclass constructor runs
      the same ``__init__`` and takes the same argument list, which is the
      thing this rule exists to stop being copied)

    Resolved per module and iterated to a fixed point, so a chain
    (``PB = PackBuilder``; ``PB2 = PB``) is caught too.
    """
    names = {"PackBuilder"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(
                alias.asname
                for alias in node.names
                if alias.name == "PackBuilder" and alias.asname
            )
    changed = True
    while changed:
        rebound: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _bare_name(node.value) in names:
                rebound.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.ClassDef) and any(
                _bare_name(base) in names for base in node.bases
            ):
                rebound.add(node.name)
        changed = not rebound <= names
        names |= rebound
    return names


def _pack_builder_construction_sites(root: Path) -> list[tuple[Path, int]]:
    """Every ``PackBuilder(...)`` construction under *root*.

    A call, not a mention: the class is named in a dozen docstrings and in
    type annotations, and neither constructs anything. ``ast`` sees only
    the calls, which is the whole reason this is a parse rather than a grep.

    Both call spellings, and the aliases :func:`_construction_names`
    resolves — the bare-name-only version of this scan was flagged in
    review as reproducing *inside the rule* the very defect the rule
    removes from the code: it was the third place in ``tests/`` to solve
    "what is this call's target called" and the first to solve it wrongly.
    """
    sites: list[tuple[Path, int]] = []
    for py_file in sorted(root.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        names = _construction_names(tree)
        sites.extend(
            (py_file, node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _bare_name(node.func) in names
        )
    return sites


def _offenders(root: Path) -> list[tuple[str, int]]:
    """Constructions under *root* that are not the allowed one.

    One function, called by the rule **and** by every test that claims
    something about the rule — a test that re-writes the comparison it is
    checking proves a property of its own copy, which is the shape that
    let a basename allowlist survive an explicit test naming it.
    """
    return [
        (path.relative_to(root).as_posix(), lineno)
        for path, lineno in _pack_builder_construction_sites(root)
        if path.relative_to(root).as_posix() not in _ALLOWED_CONSTRUCTION_SITES
    ]


def test_pack_builder_is_constructed_in_exactly_one_place() -> None:
    """Four surfaces, one wiring (#410).

    MCP, ``POST /api/v1/packs``, ``trellis analyze pack-quality`` and now
    ``trellis retrieve pack`` each hand-wrote the same argument list, and
    one of the four had already drifted. Two readers of one seam that each
    silently report a constant is this repo's recurring defect (#325/#326,
    #443); the fix is not to correct the copies but to stop there being
    copies.
    """
    offenders = _offenders(_src_root())
    assert not offenders, (
        "PackBuilder is constructed outside the factory: "
        f"{offenders}. Call trellis.retrieve.builder_factory.build_pack_builder "
        "instead — a fifth copy of the argument list is a fifth chance to drift."
    )


def test_the_construction_scan_sees_the_whole_tree() -> None:
    """The floor the scan cannot compute for itself.

    Every other guard here divides by this scan's own output, so a scan
    that merely *shrinks* satisfies all of them (#457, #464, #466 — three
    shipped rules, one root cause). This asserts a hand-read module count
    against the discovery instead.
    """
    modules = list(_src_root().rglob("*.py"))
    assert len(modules) >= _MIN_SRC_MODULES, len(modules)
    # And the discovery reaches every distributed package, not just the
    # one the factory lives in — narrowing to ``src/trellis/retrieve`` is
    # the single edit that would make the rule green and meaningless.
    top_level = {p.relative_to(_src_root()).parts[0] for p in modules}
    assert {
        "trellis",
        "trellis_cli",
        "trellis_api",
        "trellis_sdk",
        "trellis_workers",
    } <= top_level, sorted(top_level)


#: Synthetic modules run through the **shipped** predicate. Each is a real
#: construction the rule must flag, written in a spelling that was tried
#: against the first cut of this rule — all four of the middle ones walked
#: straight past it, and ``module.Attr(...)`` is the repo's own second-most
#: common call style.
_EVASIONS: dict[str, str] = {
    "bare.py": (
        "from trellis.retrieve.pack_builder import PackBuilder\n"
        "def go(s):\n"
        "    return PackBuilder(strategies=s)\n"
    ),
    "attr.py": (
        "from trellis.retrieve import pack_builder\n"
        "def go(s):\n"
        "    return pack_builder.PackBuilder(strategies=s)\n"
    ),
    "modalias.py": (
        "import trellis.retrieve.pack_builder as pb\n"
        "def go(s):\n"
        "    return pb.PackBuilder(strategies=s)\n"
    ),
    "namealias.py": (
        "from trellis.retrieve.pack_builder import PackBuilder\n"
        "PB = PackBuilder\n"
        "def go(s):\n"
        "    return PB(strategies=s)\n"
    ),
    "aliaschain.py": (
        "from trellis.retrieve.pack_builder import PackBuilder\n"
        "PB = PackBuilder\n"
        "PB2 = PB\n"
        "def go(s):\n"
        "    return PB2(strategies=s)\n"
    ),
    "importas.py": (
        "from trellis.retrieve.pack_builder import PackBuilder as Builder\n"
        "def go(s):\n"
        "    return Builder(strategies=s)\n"
    ),
    "subclass.py": (
        "from trellis.retrieve.pack_builder import PackBuilder\n"
        "class Mine(PackBuilder):\n"
        "    pass\n"
        "def go(s):\n"
        "    return Mine(strategies=s)\n"
    ),
    "inexcept.py": (
        "from trellis.retrieve.pack_builder import PackBuilder\n"
        "def go(s):\n"
        "    try:\n"
        "        raise ValueError\n"
        "    except ValueError:\n"
        "        return PackBuilder(strategies=s)\n"
    ),
    "nested.py": (
        "from trellis.retrieve.pack_builder import PackBuilder\n"
        "def outer(s):\n"
        "    def inner():\n"
        "        return PackBuilder(strategies=s)\n"
        "    return inner\n"
    ),
}


def test_the_construction_scan_is_not_vacuous(tmp_path: Path) -> None:
    """A rule that finds nothing passes for the wrong reason.

    The scan must still find the real construction in ``src/`` — a rename
    or a moved factory would otherwise make the rule above green and
    meaningless. And the *shipped* predicate is run against a synthetic
    tree carrying a construction in every spelling it must reject plus a
    bare mention it must not, so the rule is proved to fail rather than
    assumed to.
    """
    real = _pack_builder_construction_sites(_src_root())
    assert len(real) == 1, real
    assert real[0][0].name == "builder_factory.py"

    fake = tmp_path / "src"
    (fake / "trellis_somewhere").mkdir(parents=True)
    for name, body in _EVASIONS.items():
        (fake / "trellis_somewhere" / name).write_text(body)
    # Mentions that must NOT be flagged, so the predicate is shown to
    # discriminate constructions from names rather than matching the word.
    (fake / "trellis_somewhere" / "mentions.py").write_text(
        '"""See PackBuilder for the walk."""\n'
        "from trellis.retrieve.pack_builder import PackBuilder\n"
        "def annotate(b: PackBuilder) -> PackBuilder:\n"
        "    return b\n"
    )

    found = {p.name for p, _ in _pack_builder_construction_sites(fake)}
    assert found == set(_EVASIONS), sorted(set(_EVASIONS) ^ found)


def test_the_allowlist_is_a_path_not_a_basename(tmp_path: Path) -> None:
    """``builder_factory.py`` *somewhere else* is still an offender.

    Matching on ``path.name`` exempts any file with that basename anywhere
    under ``src/`` — a rule defeated by ``mkdir``. Asserted through the
    same relative-path comparison the rule performs, over a tree where the
    only construction sits at a decoy path.
    """
    fake = tmp_path / "src"
    (fake / "trellis_elsewhere" / "surfaces").mkdir(parents=True)
    (fake / "trellis_elsewhere" / "surfaces" / "builder_factory.py").write_text(
        "from trellis.retrieve.pack_builder import PackBuilder\n"
        "def go(s):\n"
        "    return PackBuilder(strategies=s)\n"
    )
    assert _offenders(fake) == [("trellis_elsewhere/surfaces/builder_factory.py", 3)]


@pytest.mark.parametrize("surface", ["mcp", "api.retrieve", "cli.retrieve"])
def test_every_surface_label_reaches_the_advisory_loader(
    tmp_path: Path, surface: str
) -> None:
    """``surface`` is the #373 log discriminator, not decoration.

    It answers "which caller saw what" in the journal, which is the
    question that went unanswered for the whole life of that issue.
    """
    config_dir = tmp_path / surface / "config"
    data_dir = tmp_path / surface / "data"
    (data_dir / "stores").mkdir(parents=True)
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(f"data_dir: {data_dir}\n")
    registry = StoreRegistry.from_config_dir(config_dir=config_dir, data_dir=data_dir)

    seen: list[str] = []
    import trellis.retrieve.builder_factory as factory

    original = factory.load_advisory_store

    def _spy(stores_dir: Any, *, surface: str) -> Any:
        seen.append(surface)
        return original(stores_dir, surface=surface)

    factory.load_advisory_store = _spy  # type: ignore[assignment]
    try:
        build_pack_builder(registry, surface=surface)
    finally:
        factory.load_advisory_store = original  # type: ignore[assignment]

    assert seen == [surface]
