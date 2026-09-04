"""``PackAssemblyError`` is legible at the CLI boundary (#493).

:class:`~trellis.retrieve.pack_builder.PackAssemblyError` subclasses
``RuntimeError``, not :class:`~trellis.errors.TrellisError`, and
:class:`trellis_cli.main._BoundaryGroup` dispatched on ``TrellisError``
alone — so an all-axes-failed build walked straight past the boundary #483
built. The operator got a Typer traceback: no exit-code contract, no
``--format json`` envelope, no recovery framing. It became reachable when
#488 routed ``trellis retrieve pack`` through ``PackBuilder``; before that
the command read the document store directly and could not raise it.

**Two commands, not one, and that is why the fix is at the boundary.**
``trellis analyze pack-quality`` assembles a pack per scenario through the
same ``build_pack_builder`` and had the identical exposure. A local
``try``/``except`` in ``retrieve.py`` would have closed the filed half and
left the other to be found again — the "roster of two" #492 warns about,
one issue over. :func:`test_every_cli_pack_build_is_covered_here` derives
the call sites from the tree and fails if a third appears without a case
below, so the roster cannot go stale silently.

**What is *not* tested here, because it is not true.** #493's body says a
CLI-local fix "leaves the REST and MCP surfaces with the same gap". Both
already handle it: ``trellis.mcp.server`` wraps the build in ``except
Exception`` and documents ``PackAssemblyError`` → ``INTERNAL_ERROR``
(pinned by ``tests/unit/mcp/test_server.py``), and ``trellis_api.app``
registers an ``Exception`` handler that answers a structured 500. Adding
assertions here about surfaces that were never broken would make this file
look more thorough and prove nothing.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from tests.cli_output import assert_coloured, plain
from trellis.core.error_sanitize import SUPPRESSED_MARKER
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import SearchStrategy
from trellis_cli.exit_codes import EXIT_INTERNAL
from trellis_cli.main import app

runner = CliRunner()


class _ExplodingStrategy(SearchStrategy):
    """A retrieval axis that is down.

    A real strategy raising a real exception, so ``PackBuilder`` raises the
    real ``PackAssemblyError`` with its real message and
    ``strategy_failures``. Stubbing ``PackBuilder.build`` itself would
    exercise the boundary against an exception this codebase never
    constructs the same way.
    """

    def __init__(self, name: str, message: str) -> None:
        self._name = name
        self._message = message

    @property
    def name(self) -> str:
        return self._name

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[Any]:
        raise RuntimeError(self._message)


#: Two axes, both down. **Two** rather than one, and the reason is a
#: mutation that survived with one: ``PackBuilder``'s *single*-strategy
#: message is ``"Required strategy 'keyword' failed: RuntimeError: index
#: unavailable"``, which already names the axis and the cause — so deleting
#: the boundary's per-failure render changed nothing any assertion could
#: see. The all-failed message is ``"All 2 configured strategies failed;
#: no candidates available for flat assembly"`` and names neither, which
#: is what makes ``strategy_failures`` the only route to the actionable
#: part. It is also the condition #493's title actually describes.
_DOWN_AXES = (
    ("keyword", "fts5 index unavailable"),
    ("semantic", "embedder connect refused"),
)


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


@pytest.fixture
def all_axes_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every pack surface assembles through a builder whose only axis fails.

    Both binding sites are patched because the two commands import
    ``build_pack_builder`` differently: ``retrieve.py`` binds it at module
    import, ``analyze.py`` imports it inside the function to keep the
    strategy graph out of non-quality commands. Patching only the
    originating module would leave ``retrieve pack`` running the real
    builder and the test passing for the wrong reason.
    """
    import trellis.retrieve.builder_factory as factory
    import trellis_cli.retrieve as cli_retrieve

    def _failing(*_args: object, **_kwargs: object) -> PackBuilder:
        return PackBuilder(
            strategies=[_ExplodingStrategy(name, msg) for name, msg in _DOWN_AXES]
        )

    monkeypatch.setattr(factory, "build_pack_builder", _failing)
    monkeypatch.setattr(cli_retrieve, "build_pack_builder", _failing)


def _assert_the_boundary_swallowed_it(result: Any) -> None:
    """No exception escaped ``_BoundaryGroup.invoke``.

    ``typer.Exit`` leaves ``CliRunner`` a ``SystemExit``; anything else is
    the traceback #493 is about. Asserted separately from the exit code
    because the two can disagree: an escaping ``PackAssemblyError`` also
    ends the process at 1.
    """
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"the boundary let an exception escape: {result.exception!r}"
    )


def _scenarios_file(tmp_path: Path) -> Path:
    path = tmp_path / "scenarios.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scenarios": [
                    {
                        "name": "canary",
                        "intent": "canary rollout",
                        "required_coverage": ["rollback"],
                    }
                ]
            }
        )
    )
    return path


#: ``{enclosing function in src/trellis_cli: argv that reaches it}``. Every
#: pack build the CLI can perform must appear here, and
#: :func:`test_every_cli_pack_build_is_covered_here` pins the mapping back
#: to a scan of the tree — a hand-written roster nothing checks is the
#: failure mode #461's capture-surface roster exists to prevent.
_PACK_BUILD_COMMANDS: dict[str, list[str]] = {
    "pack": ["retrieve", "pack", "--intent", "canary rollout"],
    "_assemble_pack_for_scenario": ["analyze", "pack-quality", "--scenarios", "{path}"],
}


def _cli_pack_build_sites() -> dict[str, str]:
    """``{enclosing function: file:line}`` for every builder ``.build`` call.

    Matched on the method name and a receiver mentioning ``builder``, which
    is the same narrow-receiver trade
    ``tests/unit/test_chunk_visibility_rule.py`` documents: ``.build`` is a
    common method name, and over-matching here would demand a boundary case
    for something that assembles no pack.

    The receiver test is **case-insensitive**, and that is not cosmetic:
    both live sites read ``builder.build(...)``, but
    ``PackBuilder(strategies=[...]).build(...)`` — the spelling this
    file's own fixture uses — carries a capital ``B`` and slipped a
    synthetic third site past the roster with every test green.
    """
    root = Path(__file__).resolve().parents[3] / "src" / "trellis_cli"
    assert root.is_dir(), f"trellis_cli not found at {root}"

    found: dict[str, str] = {}
    for py_file in sorted(root.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for parent in ast.walk(tree):
            if not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(parent):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("build", "build_sectioned")
                    and "builder" in ast.unparse(node.func.value).lower()
                ):
                    found[parent.name] = f"{py_file.name}:{node.lineno}"
    return found


def test_every_cli_pack_build_is_covered_here() -> None:
    """The roster is derived, not declared.

    #493 was filed against one command and the tree held two. A third
    ``PackBuilder`` caller — a ``curate`` preview, an ``explore`` command —
    would inherit the boundary fix automatically, which is the point of
    fixing it there, but nothing would then *check* that it does. This
    fails on the new function name rather than on the missing coverage,
    which is the earliest a test can notice.
    """
    sites = _cli_pack_build_sites()
    assert len(sites) >= 2, (
        "the scan found "
        f"{len(sites)} PackBuilder.build call site(s) in trellis_cli; it has "
        "probably stopped matching (the receiver is expected to be named "
        f"*builder*). Found: {sites}"
    )
    uncovered = sorted(set(sites) - set(_PACK_BUILD_COMMANDS))
    assert not uncovered, (
        "these functions assemble a pack and have no all-axes-down case in "
        "this file, so nothing checks that the CLI boundary renders their "
        f"failure legibly: {[f'{name} ({sites[name]})' for name in uncovered]}"
    )


@pytest.mark.parametrize("command", sorted(_PACK_BUILD_COMMANDS))
def test_text_arm_renders_a_legible_failure(
    command: str, tmp_path: Path, all_axes_down: None
) -> None:
    """No traceback, a named error, the failing axis, and exit 1.

    ``EXIT_INTERNAL`` is what
    :func:`~trellis_cli.exit_codes.exit_code_for` already returns for
    anything that is not a typed Trellis error, so reparenting
    ``PackAssemblyError`` to ``TrellisError`` would have produced the same
    code. Asserting it here is what makes that claim in
    ``main._render_pack_assembly_failure``'s docstring checkable.
    """
    argv = [
        arg.format(path=_scenarios_file(tmp_path))
        for arg in _PACK_BUILD_COMMANDS[command]
    ]
    result = runner.invoke(app, argv)

    assert result.exit_code == EXIT_INTERNAL, result.output
    _assert_the_boundary_swallowed_it(result)
    out = " ".join(plain(result.output).split())
    assert "PackAssemblyError" in out, out
    assert "All 2 configured strategies failed" in out, out
    for axis, message in _DOWN_AXES:
        assert axis in out, f"{axis} is the actionable part and is missing: {out}"
        assert message in out, out
    assert "Traceback" not in out


@pytest.mark.parametrize("command", sorted(_PACK_BUILD_COMMANDS))
def test_json_arm_returns_the_error_envelope_and_the_same_exit_code(
    command: str, tmp_path: Path, all_axes_down: None
) -> None:
    """The machine surface must not be the one that reports success.

    #437's rule, on a path the format/exit parity scan cannot reach: it
    grades ``if output_format == ...`` branches inside a command, and this
    failure is rendered above every command, in the root group. So the
    parity is asserted behaviourally instead — same exit code on both arms,
    and a payload whose ``status`` says ``error``.
    """
    argv = [
        *[
            arg.format(path=_scenarios_file(tmp_path))
            for arg in _PACK_BUILD_COMMANDS[command]
        ],
        "--format",
        "json",
    ]
    result = runner.invoke(app, argv)

    assert result.exit_code == EXIT_INTERNAL, result.output
    # Not implied by the exit code, and the omission was found by
    # mutation: moving the ``raise typer.Exit`` inside the text arm — the
    # #437 shape this repo keeps producing — leaves the JSON arm falling
    # through to ``_BoundaryGroup``'s bare ``raise``. That re-raises the
    # original ``PackAssemblyError``, which still ends the process at 1
    # *and* still prints the payload, so exit code and stdout both look
    # right while the operator gets a traceback under them.
    _assert_the_boundary_swallowed_it(result)
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["error_type"] == "PackAssemblyError"
    assert payload["error_code"] == "PackAssemblyError"
    assert payload["strategy_failures"] == [
        {"strategy": axis, "error_class": "RuntimeError", "message": message}
        for axis, message in _DOWN_AXES
    ]


def test_the_two_format_arms_agree_about_failure(
    tmp_path: Path, all_axes_down: None
) -> None:
    """Stated as its own case because it is the property, not a side effect.

    Two commands times two format arms is four assertions about four
    renderings; this is one assertion about the *pair*, so a future change
    that gives the JSON arm its own exit path has to break this line rather
    than quietly diverge inside one of the four above.
    """
    text = runner.invoke(app, ["retrieve", "pack", "--intent", "canary rollout"])
    machine = runner.invoke(
        app, ["retrieve", "pack", "--intent", "canary rollout", "--format", "json"]
    )
    assert text.exit_code == machine.exit_code != 0
    _assert_the_boundary_swallowed_it(text)
    _assert_the_boundary_swallowed_it(machine)


def test_the_machine_arm_suppresses_a_leaky_axis_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A driver's error text is not the boundary's to publish.

    ``strategy_failures`` carries whatever the axis raised, and a
    ``psycopg`` connect failure echoes the DSN it could not reach. #206's
    rule is that a machine-readable artifact — which is what ``--format
    json`` output becomes in a CI log or a review bundle — never carries
    it, and ``_render_boundary_failure`` already routes ``TrellisError``
    messages through the same guard. Without this, the
    ``sanitize_error_message`` call in the JSON arm could be deleted and
    every other assertion in this file would stay green: the axis messages
    they use are clean, so the guard is a no-op on them.

    The text arm deliberately keeps the raw message — it is going to a
    terminal, not into an artifact, which is exactly what
    ``_render_boundary_failure`` does one clause up.
    """
    import trellis.retrieve.builder_factory as factory
    import trellis_cli.retrieve as cli_retrieve

    leaky = "could not connect to postgres://trellis:hunter2@db:5432/kb"

    def _failing(*_args: object, **_kwargs: object) -> PackBuilder:
        return PackBuilder(
            strategies=[
                _ExplodingStrategy("keyword", leaky),
                _ExplodingStrategy("semantic", leaky),
            ]
        )

    monkeypatch.setattr(factory, "build_pack_builder", _failing)
    monkeypatch.setattr(cli_retrieve, "build_pack_builder", _failing)

    machine = runner.invoke(
        app, ["retrieve", "pack", "--intent", "canary rollout", "--format", "json"]
    )
    payload = json.loads(machine.stdout)
    assert all(
        failure["message"] == SUPPRESSED_MARKER
        for failure in payload["strategy_failures"]
    ), payload["strategy_failures"]
    assert "hunter2" not in machine.stdout

    text = runner.invoke(app, ["retrieve", "pack", "--intent", "canary rollout"])
    assert "hunter2" in " ".join(plain(text.output).split())


def test_the_failure_is_legible_under_colour(
    monkeypatch: pytest.MonkeyPatch, all_axes_down: None
) -> None:
    """The rendering CI and a real terminal actually take.

    #495 found 21 CLI tests blind to the coloured path, four of them
    written specifically to prove Rich does not mangle operator output.
    ``force_colour`` on ``trellis_cli.main`` is needed because the boundary
    renders through *that* module's console, not the command's.
    """
    import trellis_cli.main as cli_main
    from tests.cli_output import force_colour

    force_colour(monkeypatch, cli_main)
    result = runner.invoke(app, ["retrieve", "pack", "--intent", "canary rollout"])

    assert result.exit_code == EXIT_INTERNAL
    out = " ".join(assert_coloured(result.output).split())
    assert "PackAssemblyError" in out, out
    assert "fts5 index unavailable" in out, out
