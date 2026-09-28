"""A live-infra failure must leave evidence behind, and it must be uploaded.

The four invariants here each have a silent failure mode: the workflow stays
green, the feature stops working, and nothing says so until the next flake is
already unreproducible.

1. Every service container is captured. A service added later with no
   ``docker logs`` line is uncaptured, and the only signal is an artifact
   missing a file nobody was looking for. Derived from ``services:`` rather
   than a hand-written roster, which is the #443 rot shape.
2. The capture and upload steps are failure-gated. ``upload-artifact``'s own
   default is to run on *success only* — so dropping ``if: failure()`` does
   not disable the step, it inverts it, and the inversion is invisible
   precisely when a failure happens.
3. Everything written as evidence lands under the uploaded path. The junit
   report and the server logs are configured in two different places
   (``PYTEST_ADDOPTS`` on the test step, redirects in the capture step); a
   path edit in one and not the other silently stops uploading a file.
4. The junit report is also read when the tests pass, not only on failure.
   A skip is not a failure, so a Neo4j vector contract that skipped every
   case left the job green. ``scripts/check_live_floor.py`` fails it
   instead, from a step with no condition and nothing softening its exit,
   reading the file the test step writes.

Issue: #556.
"""

import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "live-infra.yml"

TEST_STEP = "Run live + contract suites against the containers"
FLOOR_SCRIPT = "scripts/check_live_floor.py"

# ``${{ job.services.<id>.id }}`` — the documented way to reach a service
# container's id from a step.
SERVICE_ID_EXPR = re.compile(r"\$\{\{\s*job\.services\.([A-Za-z0-9_-]+)\.id\s*\}\}")
# ``> path`` / ``>> path``, quoted or bare, in a shell fragment.
REDIRECT = re.compile(r">>?\s*\"?([A-Za-z0-9_./$-]+)\"?")


def _live_job() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["live-infra"]


def _steps() -> list[dict[str, Any]]:
    return _live_job()["steps"]


def _upload_steps() -> list[dict[str, Any]]:
    return [
        step
        for step in _steps()
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]


def _log_capture_steps() -> list[dict[str, Any]]:
    return [step for step in _steps() if "docker logs" in str(step.get("run", ""))]


def _uploaded_roots() -> frozenset[PurePosixPath]:
    roots: set[PurePosixPath] = set()
    for step in _upload_steps():
        path_spec = str(step["with"]["path"])
        roots.update(
            PurePosixPath(line.strip().rstrip("/"))
            for line in path_spec.splitlines()
            if line.strip() and not line.strip().startswith("!")
        )
    return frozenset(roots)


def _evidence_paths() -> frozenset[PurePosixPath]:
    """Every path this job writes intending it to be collected as evidence."""
    paths: set[PurePosixPath] = set()

    addopts = _step(TEST_STEP)["env"]["PYTEST_ADDOPTS"]
    for token in shlex.split(addopts):
        if token.startswith("--junitxml="):
            paths.add(PurePosixPath(token.split("=", 1)[1]))

    for step in _log_capture_steps():
        for target in REDIRECT.findall(str(step["run"])):
            # ``$name`` is the loop variable standing in for a service id; the
            # directory is what has to match, so normalise it away.
            paths.add(PurePosixPath(target.replace("$name", "SERVICE")))

    return frozenset(paths)


def _step(name: str) -> dict[str, Any]:
    [step] = [step for step in _steps() if step.get("name") == name]
    return step


def test_every_service_container_has_its_log_captured() -> None:
    services = frozenset(_live_job()["services"])
    captured = frozenset(
        service
        for step in _log_capture_steps()
        for service in SERVICE_ID_EXPR.findall(str(step["run"]))
    )

    assert services, "live-infra declares no service containers"
    assert captured == services, (
        "every live-infra service container must have its log captured on "
        f"failure; missing {sorted(services - captured)}, "
        f"unknown {sorted(captured - services)}"
    )


def test_evidence_steps_run_on_failure() -> None:
    evidence_steps = _log_capture_steps() + _upload_steps()

    assert len(evidence_steps) >= 2, "no failure-evidence steps found to check"
    for step in evidence_steps:
        assert step.get("if") == "failure()", (
            f"step {step.get('name')!r} must carry ``if: failure()`` — "
            "actions/upload-artifact otherwise runs on success only, which "
            "makes the whole feature a no-op exactly when it is needed"
        )


def test_written_evidence_lands_inside_the_uploaded_path() -> None:
    roots = _uploaded_roots()
    written = _evidence_paths()

    assert roots, "no upload-artifact path found"
    # Two producers, two configuration sites: the junit report and at least one
    # server log. A regex that stopped matching would otherwise pass vacuously.
    assert len(written) >= 2, (
        f"expected the junit report and a server log, got {sorted(written)}"
    )
    for path in written:
        assert any(root == path or root in path.parents for root in roots), (
            f"{path} is written as failure evidence but lies outside the "
            f"uploaded path(s) {sorted(str(root) for root in roots)}"
        )


def test_the_junit_report_is_checked_when_the_tests_pass() -> None:
    steps = _steps()
    floors = [step for step in steps if FLOOR_SCRIPT in str(step.get("run", ""))]
    [junit] = [
        token.split("=", 1)[1]
        for token in shlex.split(_step(TEST_STEP)["env"]["PYTEST_ADDOPTS"])
        if token.startswith("--junitxml=")
    ]
    evidence = _log_capture_steps() + _upload_steps()

    assert len(floors) == 1, f"expected one step running {FLOOR_SCRIPT}"
    [floor] = floors
    # A name and a command, nothing else: ``if: failure()`` would run it only
    # when the job is already red, ``continue-on-error`` would keep it and
    # stop it failing anything, and ``|| true`` or a second path in ``run``
    # would do the same from the command line.
    assert set(floor) == {"name", "run"}
    assert shlex.split(floor["run"]) == ["python", FLOOR_SCRIPT, junit]
    # After the step that writes the report, and before the failure-gated
    # evidence steps, so a floor failure uploads the report that explains it.
    assert evidence, "no failure-evidence steps found to order against"
    assert steps.index(_step(TEST_STEP)) < steps.index(floor)
    assert steps.index(floor) < min(steps.index(step) for step in evidence)
