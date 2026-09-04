"""#489 — one unusable config file under ``stores_dir``, one exit code.

A damaged ``advisories.json`` and a damaged ``policies.json`` are the same
condition wearing different filenames: a file the deployment cannot use,
which no argument the caller passes will fix. They disagreed about how to
say so — the advisory surfaces exited ``2`` ("fix your input, retry") while
the policy surface exited ``5`` — and ``2`` is the value the two rules in
play actually conflict at, because a wrapper that retries with corrected
arguments on ``2`` loops forever against a malformed file.

The claim under test is *cross-surface*, so the tests are too. Each case
drives a real command through ``CliRunner`` against a real damaged file and
asserts the code, on **both** ``--format`` arms — the arms are parametrised
rather than asserted in one arm and assumed in the other, because
``tests/unit/test_format_exit_parity_rule.py`` is per-module and cannot see
a helper imported from elsewhere (#491).

Three things here are deliberately not satisfiable by a constant:

* the codes are asserted against the literal ``5`` **and** against
  :data:`~trellis_cli.exit_codes.EXIT_STORE`, so neither renaming the
  constant nor redefining it can make a wrong code pass (the constant's own
  value is pinned separately, in ``test_exit_codes.py``);
* :class:`TestASurfaceThisChangeMustNotHaveTouched` pins that a genuine
  validation failure still exits ``2``, so a blanket ``2 -> 5`` rewrite
  fails here;
* every damage case has a clean-run control on the same surface, so a
  command that exits ``5`` unconditionally fails here too.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from trellis.errors import StaleStoreWriteError
from trellis.schemas.advisory import (
    Advisory,
    AdvisoryCategory,
    AdvisoryEvidence,
)
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli import analyze
from trellis_cli.exit_codes import EXIT_STORE, EXIT_VALIDATION
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

if TYPE_CHECKING:
    from click.testing import Result

runner = CliRunner()

#: The refusal a second writer's ``refuse_if_stale`` raises, as a name:
#: ruff's ``EM101`` forbids raising with a string literal inline.
_REFUSAL_MESSAGE = "advisories.json changed after this process read it"

#: The value, written out. Asserting only ``== EXIT_STORE`` would pass
#: against a constant redefined to ``2``, which is the exact regression
#: this module exists to prevent.
_STORE = 5

ArgvFor = Callable[[Path], list[str]]

#: Every surface that meets a damaged ``advisories.json``.
#:
#: ``generate-advisories`` is here because it is the one that routes
#: through *neither* named helper — it raises inline in each ``--format``
#: arm — so a fix applied only to the helpers would leave it behind with
#: the whole suite green.
ADVISORY_SURFACES: dict[str, ArgvFor] = {
    "generate-advisories": lambda _: ["analyze", "generate-advisories"],
    "advisory-effectiveness": lambda _: ["analyze", "advisory-effectiveness"],
    "advisory-effectiveness --dry-run": lambda _: [
        "analyze",
        "advisory-effectiveness",
        "--dry-run",
    ],
    "worker curate": lambda tmp: [
        "worker",
        "curate",
        "--output-dir",
        str(tmp / "review"),
    ],
}

FORMAT_ARMS: dict[str, list[str]] = {"text": [], "json": ["--format", "json"]}


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    """Point every CLI store at a temp directory, as the CLI resolves them."""
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()
    return StoreRegistry(stores_dir=stores_dir)


@pytest.fixture
def temp_stores(_temp_stores: StoreRegistry) -> StoreRegistry:
    """Expose the autouse registry for the tests that seed through it."""
    return _temp_stores


def _advisory_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "stores" / "advisories.json"


def _policy_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "stores" / "policies.json"


def _corrupt(path: Path, envelope_key: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{{"{envelope_key}": [ torn write', encoding="utf-8")
    return path


def _run(tmp_path: Path, argv_for: ArgvFor, arm: list[str]) -> Result:
    return runner.invoke(app, [*argv_for(tmp_path), *arm])


class TestADamagedAdvisoryFile:
    """The four surfaces that read ``advisories.json``, on both arms."""

    @pytest.mark.parametrize("surface", sorted(ADVISORY_SURFACES))
    @pytest.mark.parametrize("arm", sorted(FORMAT_ARMS))
    def test_exits_store_not_validation(
        self, tmp_path: Path, surface: str, arm: str
    ) -> None:
        _corrupt(_advisory_path(tmp_path), "advisories")

        result = _run(tmp_path, ADVISORY_SURFACES[surface], FORMAT_ARMS[arm])

        assert result.exit_code == _STORE, result.output
        assert result.exit_code == EXIT_STORE, result.output

    @pytest.mark.parametrize("surface", sorted(ADVISORY_SURFACES))
    def test_both_format_arms_produce_the_same_code(
        self, tmp_path: Path, surface: str
    ) -> None:
        """Executed parity for one input, not the AST scan's structural one.

        The per-module scan in ``test_format_exit_parity_rule.py`` proves a
        non-zero exit is *reachable* from both arms of the module it reads;
        two of these surfaces exit through a helper defined in another
        module, which that scan cannot follow.
        """
        _corrupt(_advisory_path(tmp_path), "advisories")
        argv_for = ADVISORY_SURFACES[surface]

        text = _run(tmp_path, argv_for, FORMAT_ARMS["text"])
        as_json = _run(tmp_path, argv_for, FORMAT_ARMS["json"])

        assert text.exit_code == as_json.exit_code == _STORE, (
            text.output,
            as_json.output,
        )

    @pytest.mark.parametrize("surface", sorted(ADVISORY_SURFACES))
    @pytest.mark.parametrize("arm", sorted(FORMAT_ARMS))
    def test_a_clean_store_still_exits_zero(
        self, tmp_path: Path, surface: str, arm: str
    ) -> None:
        """The control: an unconditional ``5`` would satisfy everything above."""
        result = _run(tmp_path, ADVISORY_SURFACES[surface], FORMAT_ARMS[arm])

        assert result.exit_code == 0, result.output

    def test_the_json_payload_still_parses(self, tmp_path: Path) -> None:
        """The code moved; the machine surface did not stop being one."""
        _corrupt(_advisory_path(tmp_path), "advisories")

        result = _run(
            tmp_path, ADVISORY_SURFACES["advisory-effectiveness"], FORMAT_ARMS["json"]
        )

        assert result.exit_code == _STORE, result.output
        assert json.loads(result.stdout.strip())["status"] == "degraded"


class TestARefusedAdvisoryWrite:
    """The other refusal: the file read fine, another process wrote it.

    Different condition, same answer — the deployment's state is wrong and
    no argument fixes it — and the two must agree with each other, which is
    the rule #481 stated and this change preserves at the new value.
    """

    @staticmethod
    def _advisory(message: str) -> Advisory:
        return Advisory(
            category=AdvisoryCategory.ENTITY,
            message=message,
            scope="global",
            confidence=0.7,
            evidence=AdvisoryEvidence(
                sample_size=10,
                success_rate_with=0.8,
                success_rate_without=0.4,
                effect_size=0.4,
            ),
        )

    @classmethod
    def _seed_scored_advisory(cls, registry: StoreRegistry, path: Path) -> str:
        """One advisory in the file, plus enough graded packs to score it.

        The fitness loop writes once per scored advisory, so this is what
        makes the command reach a write at all — without it the refusal
        never fires and every assertion below passes vacuously at ``0``.
        """
        advisory = cls._advisory("seeded for the refusal path")
        AdvisoryStore(path).put(advisory)

        event_log = registry.operational.event_log
        for index in range(4):  # > the command's --min-presentations default
            pack_id = f"parity-adv-pack-{index}"
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "intent": "advisory probe",
                    "advisory_ids": [advisory.advisory_id],
                },
            )
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"pack_id": pack_id, "success": True, "rating": 1.0},
            )
        return advisory.advisory_id

    @classmethod
    def _second_writer_after_load(
        cls, monkeypatch: pytest.MonkeyPatch, path: Path
    ) -> str:
        """Another process appends a row between the command's load and save.

        Patched at the store constructor the command calls, not at any
        guard: the command gets a real store that really loaded the file,
        and the file really moves on underneath it.
        """
        theirs = cls._advisory("written by the other process")

        def _racing_store(store_path: Path) -> AdvisoryStore:
            store = AdvisoryStore(store_path)
            AdvisoryStore(path).put(theirs)
            return store

        monkeypatch.setattr(analyze, "AdvisoryStore", _racing_store)
        return theirs.advisory_id

    @pytest.mark.parametrize("arm", sorted(FORMAT_ARMS))
    def test_effectiveness_exits_store(
        self,
        tmp_path: Path,
        temp_stores: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
        arm: str,
    ) -> None:
        path = _advisory_path(tmp_path)
        landed = self._seed_scored_advisory(temp_stores, path)
        theirs = self._second_writer_after_load(monkeypatch, path)

        result = _run(
            tmp_path,
            ADVISORY_SURFACES["advisory-effectiveness"],
            FORMAT_ARMS[arm],
        )

        assert result.exit_code == _STORE, result.output
        assert result.exit_code == EXIT_STORE
        # The refusal really fired: the other process's row is still in the
        # file, and so is the one this command was scoring.
        store = AdvisoryStore(path)
        assert store.get(theirs) is not None
        assert store.get(landed) is not None

    @pytest.mark.parametrize("arm", sorted(FORMAT_ARMS))
    def test_generate_exits_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str
    ) -> None:
        """``generate-advisories`` refuses through the shared helper.

        Driven by making the generator raise: generation only writes when
        the window yields advisories, and a seed engineered to clear the
        effect-size floor would pin the generator's statistics rather than
        this command's exit code.
        """
        path = _advisory_path(tmp_path)

        class _RefusingGenerator:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def generate(self, *, days: int = 30) -> None:
                raise StaleStoreWriteError(
                    _REFUSAL_MESSAGE,
                    store="advisory",
                    path=str(path),
                    recovery="trellis analyze advisory-effectiveness --dry-run",
                )

        monkeypatch.setattr(analyze, "AdvisoryGenerator", _RefusingGenerator)

        result = _run(
            tmp_path, ADVISORY_SURFACES["generate-advisories"], FORMAT_ARMS[arm]
        )

        assert result.exit_code == _STORE, result.output
        assert result.exit_code == EXIT_STORE


class TestThePolicySurfaceThisChangeDidNotTouch:
    """The other half of the pair, unchanged — and now agreeing.

    Asserted here rather than only in ``test_policy.py`` because the claim
    is the *equality*: closing the gap by moving the policy surface to ``2``
    would satisfy a per-file test on either side and re-open the issue.
    """

    @pytest.mark.parametrize("arm", sorted(FORMAT_ARMS))
    def test_policy_list_on_a_damaged_file_exits_store(
        self, tmp_path: Path, arm: str
    ) -> None:
        _corrupt(_policy_path(tmp_path), "policies")

        result = runner.invoke(app, ["policy", "list", *FORMAT_ARMS[arm]])

        assert result.exit_code == _STORE, result.output
        assert result.exit_code == EXIT_STORE

    @pytest.mark.parametrize("surface", sorted(ADVISORY_SURFACES))
    def test_the_two_files_produce_the_same_code(
        self, tmp_path: Path, surface: str
    ) -> None:
        """One condition, one code — stated as an equality, not two literals."""
        _corrupt(_advisory_path(tmp_path), "advisories")
        _corrupt(_policy_path(tmp_path), "policies")

        advisory = _run(tmp_path, ADVISORY_SURFACES[surface], FORMAT_ARMS["json"])
        policy = runner.invoke(app, ["policy", "list", "--format", "json"])

        assert advisory.exit_code == policy.exit_code, (
            advisory.output,
            policy.output,
        )
        assert advisory.exit_code == _STORE


class TestASurfaceThisChangeMustNotHaveTouched:
    """``2`` still means what the ADR says it means.

    The boundary this change draws is *whose* file failed, not whether a
    file failed. ``advisories.json`` and ``policies.json`` live under
    ``stores_dir``: the deployment owns them, the caller passed no argument
    naming them, and no corrected argument fixes one. Both cases below are
    the other side of that line — the caller's own input failed a check and
    retrying with a corrected one is exactly the right response — so both
    keep ``2``. A blanket ``2 -> 5`` rewrite fails here.
    """

    @pytest.mark.parametrize("arm", sorted(FORMAT_ARMS))
    def test_a_bad_argument_still_exits_validation(self, arm: str) -> None:
        result = runner.invoke(
            app, ["analyze", "replay", "--body-items", "0", *FORMAT_ARMS[arm]]
        )

        assert result.exit_code == EXIT_VALIDATION, result.output
        assert result.exit_code == 2

    @pytest.mark.parametrize("arm", sorted(FORMAT_ARMS))
    def test_a_config_file_the_caller_named_still_exits_validation(
        self, tmp_path: Path, arm: str
    ) -> None:
        """The closest neighbour, and it stays on the other side of the line.

        ``admin migrate-graph`` also fails on an unreadable YAML config —
        but on one the caller pointed it at with ``--from-config``. Fixing
        the argument really is the retry, so this is the ``2`` the ADR
        describes, not the ``2`` #489 removed.
        """
        result = runner.invoke(
            app,
            [
                "admin",
                "migrate-graph",
                "--from-config",
                str(tmp_path / "no-such-config.yaml"),
                "--to-config",
                str(tmp_path / "no-such-config.yaml"),
                *FORMAT_ARMS[arm],
            ],
        )

        assert result.exit_code == EXIT_VALIDATION, result.output
        assert result.exit_code == 2
