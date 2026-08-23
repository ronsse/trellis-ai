"""Tests for ``trellis classify shadow`` / ``shadow-report`` / ``tag-candidates``.

Same harness as ``test_classify_backfill``: CliRunner against real SQLite
stores in a tmp config/data dir.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from typer.testing import CliRunner

from trellis.learning.tag_evolution import (
    PARAM_COMPONENT_ID,
    RECOMMENDED_SEED_VALUES,
)
from trellis.schemas.classification import SHADOW_TAGS_KEY, ShadowTags
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.base.event_log import EventType
from trellis_cli.admin import admin_app
from trellis_cli.classify import classify_app
from trellis_cli.stores import _get_registry, _reset_registry

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    init = runner.invoke(admin_app, ["init"])
    assert init.exit_code == 0, init.output
    _reset_registry()


def _run_json(*args: str) -> dict[str, Any]:
    result = runner.invoke(classify_app, [*args, "--format", "json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output.strip().splitlines()[-1])


def _seed_shadowed(docs: list[tuple[str, str, list[str]]]) -> None:
    store = _get_registry().knowledge.document_store
    for doc_id, content, domains in docs:
        shadow = ShadowTags(
            domain=domains,
            content_type="reference",
            classified_at=datetime.now(UTC),
            model_id="hermes3:8b",
        ).model_dump(mode="json")
        store.put(doc_id, content, {SHADOW_TAGS_KEY: shadow})


def _seed_thresholds(**overrides: float | int) -> None:
    values: dict[str, float | int | str | bool] = dict(RECOMMENDED_SEED_VALUES)
    values["tag_keyword_min_support"] = 3
    values["tag_keyword_min_corpus"] = 3
    values.update(overrides)
    _get_registry().operational.parameter_store.put(
        ParameterSet(
            scope=ParameterScope(component_id=PARAM_COMPONENT_ID),
            values=values,
            source="test",
        )
    )


class TestShadowCommand:
    def test_requires_an_llm_and_says_so(self, cli_env) -> None:
        """No ``llm:`` block configured -> a loud, actionable exit, not a no-op."""
        result = runner.invoke(classify_app, ["shadow"])
        assert result.exit_code != 0
        assert "llm" in result.output.lower()


class TestShadowReportCommand:
    def test_reports_nothing_shadowed(self, cli_env) -> None:
        _get_registry().knowledge.document_store.put("d1", "plain content", {})
        payload = _run_json("shadow-report")
        assert payload["scanned"] == 1
        assert payload["with_shadow"] == 0

    def test_reports_agreement_and_vocabulary_gap(self, cli_env) -> None:
        _seed_shadowed([("d1", "todoist notes", ["task-management"])])
        payload = _run_json("shadow-report")
        assert payload["with_shadow"] == 1
        # The live side has no tags at all, so the LLM's values are pure
        # coverage gain rather than disagreement.
        assert payload["facets"]["content_type"]["live_missing"] == 1
        assert payload["facets"]["content_type"]["agreement_rate"] is None
        assert payload["out_of_vocabulary_content_types"] == {"reference": 1}

    def test_per_document_rows_are_opt_in(self, cli_env) -> None:
        _seed_shadowed([("d1", "todoist notes", ["task-management"])])
        assert "comparisons" not in _run_json("shadow-report")
        detailed = _run_json("shadow-report", "--per-document")
        assert [row["item_id"] for row in detailed["comparisons"]] == ["d1"]

    def test_text_output_points_at_the_shadow_command(self, cli_env) -> None:
        _get_registry().knowledge.document_store.put("d1", "plain", {})
        result = runner.invoke(classify_app, ["shadow-report"])
        assert result.exit_code == 0
        assert "trellis classify shadow" in result.output


class TestTagCandidatesCommand:
    def test_unseeded_deployment_runs_on_recommended_defaults(self, cli_env) -> None:
        """The command must work out of the box, like its sibling loop.

        Nothing seeds ``learning.tag_evolution`` — ``admin init-learning-params``
        does not know about it — so hard-failing on an unseeded store meant
        hard-failing on every deployment. The analyzer keeps its
        no-silent-defaults rule; the CLI chooses the default, loudly.
        """
        result = runner.invoke(classify_app, ["tag-candidates"])
        assert result.exit_code == 0, result.output
        assert "none surfaced" in result.output

    def test_seeded_thresholds_win_over_the_fallback(self, cli_env) -> None:
        _seed_thresholds(tag_keyword_min_corpus=999)
        payload = _run_json("tag-candidates")
        assert payload["status"] == "ok"
        assert payload["candidates"] == []

    def test_out_of_range_threshold_reports_cleanly(self, cli_env) -> None:
        """An out-of-range threshold is misconfiguration, not a crash.

        The missing-key path already reported cleanly; a ValueError from the
        same resolver (or from a malformed ``classify.domain_keywords`` block
        reached via ``domain_keyword_map()``) used to escape as a traceback.
        """
        _seed_thresholds(tag_keyword_min_precision=1.5)
        result = runner.invoke(classify_app, ["tag-candidates"])
        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "min_precision" in result.output

    def test_out_of_range_threshold_reports_cleanly_as_json(self, cli_env) -> None:
        _seed_thresholds(tag_keyword_min_lift=-1.0)
        result = runner.invoke(classify_app, ["tag-candidates", "--format", "json"])
        assert result.exit_code != 0
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["status"] == "error"
        assert "min_lift" in payload["message"]

    def test_surfaces_candidates_and_a_paste_ready_fragment(self, cli_env) -> None:
        _seed_thresholds()
        _seed_shadowed(
            [
                (f"t{i}", f"todoist sprint planning notes {i}", ["task-management"])
                for i in range(4)
            ]
            + [
                (f"o{i}", f"unrelated musings about weather {i}", ["journal"])
                for i in range(4)
            ]
        )
        payload = _run_json("tag-candidates")
        keywords = {c["keyword"] for c in payload["candidates"]}
        assert "todoist" in keywords
        assert payload["domain_keywords_fragment"]["task-management"]
        # Every candidate states the limit of what the measurement supports.
        assert all(c["notes"] for c in payload["candidates"])

    def test_emits_events_by_default_and_not_with_no_emit(self, cli_env) -> None:
        _seed_thresholds()
        _seed_shadowed(
            [
                (f"t{i}", f"todoist sprint planning notes {i}", ["task-management"])
                for i in range(4)
            ]
            + [
                (f"o{i}", f"unrelated musings about weather {i}", ["journal"])
                for i in range(4)
            ]
        )
        log = _get_registry().operational.event_log

        _run_json("tag-candidates", "--no-emit")
        assert log.get_events(event_type=EventType.TAG_KEYWORD_CANDIDATE) == []

        payload = _run_json("tag-candidates")
        emitted = log.get_events(event_type=EventType.TAG_KEYWORD_CANDIDATE)
        assert len(emitted) == len(payload["candidates"]) > 0

    def test_empty_corpus_explains_why(self, cli_env) -> None:
        _seed_thresholds()
        result = runner.invoke(classify_app, ["tag-candidates"])
        assert result.exit_code == 0
        assert "none surfaced" in result.output
        assert "shadow-report" in result.output

    def test_built_in_keywords_are_never_re_proposed(self, cli_env) -> None:
        """The loop must not propose vocabulary the classifier already owns."""
        _seed_thresholds()
        _seed_shadowed(
            [
                (f"k{i}", f"kubernetes cluster rollout number {i}", ["platform"])
                for i in range(4)
            ]
            + [
                (f"o{i}", f"unrelated musings about weather {i}", ["journal"])
                for i in range(4)
            ]
        )
        payload = _run_json("tag-candidates")
        # `kubernetes` is a built-in `infrastructure` keyword.
        assert "kubernetes" not in {c["keyword"] for c in payload["candidates"]}
