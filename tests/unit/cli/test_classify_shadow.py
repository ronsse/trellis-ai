"""Tests for ``trellis classify shadow`` / ``shadow-report`` / ``tag-candidates``.

Same harness as ``test_classify_backfill``: CliRunner against real SQLite
stores in a tmp config/data dir.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import assert_coloured, force_colour, plain
from trellis.learning.domain_normalization import (
    SIGNAL_LEXICAL,
    DomainAliasCandidate,
)
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

    def test_the_race_counter_reaches_the_operator(self, cli_env, monkeypatch) -> None:
        """#421 — a counter nobody can read is not a counter.

        The library-side tally is pinned in ``tests/unit/classify/test_shadow.py``;
        what this pins is that the CLI actually forwards it, on both surfaces.
        A concurrent write during a shadow pass is a fact about deployment
        concurrency the operator has to be able to see.
        """
        from trellis.classify.shadow import BatchShadowResult
        from trellis_cli import classify as classify_cli

        monkeypatch.setattr(classify_cli, "_require_llm_facet_classifier", object)
        monkeypatch.setattr(
            classify_cli,
            "shadow_classify_stale",
            lambda **_kwargs: BatchShadowResult(scanned=3, written=3, stale_snapshot=2),
        )

        assert _run_json("shadow")["stale_snapshot"] == 2

        result = runner.invoke(classify_app, ["shadow"])
        assert result.exit_code == 0, result.output
        assert "2 document(s) were written" in plain(result.output)


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


class TestDomainCandidatesCommand:
    """``trellis classify domain-candidates`` (#321 normalization)."""

    @staticmethod
    def _seed_fragmented() -> None:
        _seed_shadowed(
            [(f"canon-{i}", f"body {i}", ["hunting"]) for i in range(20)]
            + [("alias-1", "a fragment", ["budget-hunting"])]
        )

    def test_surfaces_a_merge_and_a_paste_ready_fragment(self, cli_env) -> None:
        self._seed_fragmented()
        payload = _run_json("domain-candidates", "--no-emit")
        assert payload["domain_aliases_fragment"] == {"budget-hunting": "hunting"}

    def test_min_gain_hides_merges_that_change_nothing(self, cli_env) -> None:
        _seed_shadowed(
            [(f"canon-{i}", f"body {i}", ["hunting"]) for i in range(20)]
            + [("both", "carries both", ["hunting", "deer-hunting"])]
        )
        assert _run_json("domain-candidates", "--no-emit")["candidates"]
        assert not _run_json("domain-candidates", "--no-emit", "--min-gain", "1")[
            "candidates"
        ]

    def test_review_markers_survive_rich_markup(self, cli_env) -> None:
        """Regression: an unescaped ``[cross-cutting]`` is read as a markup tag.

        Rich swallowed it, so the run rendered clean while dropping exactly the
        two warnings a reviewer needs to see. Text mode, deliberately — this
        bug is invisible to the JSON path.
        """
        # Two real subjects, not an activity noun: `planning` is a builtin
        # aspect now, so `tax-planning` is `tax` qualified and no longer
        # cross-cutting.
        _seed_shadowed(
            [(f"startup-{i}", f"s {i}", ["startup"]) for i in range(20)]
            + [(f"fin-{i}", f"f {i}", ["finance"]) for i in range(20)]
            + [("cross", "a cross-cutting doc", ["startup-finance"])]
        )
        result = runner.invoke(classify_app, ["domain-candidates", "--no-emit"])
        assert result.exit_code == 0, result.output
        assert "cross-cutting" in result.output
        assert "spelling only" in result.output

    def test_paste_ready_alias_survives_markup_under_colour(
        self,
        cli_env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """LLM vocabulary must reach the review listing *and* the YAML (#522).

        The paste-ready fragment uses ``markup=False``. The grouped review
        lines above it wrap ``canonical`` / ``alias`` in ``[bold]``, so a
        YAML-only fix stays green while Rich still eats ``[...]`` in the
        listing an operator actually reads.
        """
        import trellis_cli.classify as cli_classify

        alias = "budget-[literal-alias]"
        canonical = "hunting-[literal-canonical]"
        competitor = "estate-[literal-competitor]"
        candidate = DomainAliasCandidate(
            alias=alias,
            canonical=canonical,
            alias_documents=1,
            canonical_documents=20,
            corpus_documents=21,
            cooccurrence_documents=0,
            cooccurrence_rate=0.0,
            neighbor_overlap=0.0,
            shared_tokens=("hunting",),
            signals=(SIGNAL_LEXICAL,),
            competing_canonicals=(competitor,),
            documents_gained=1,
            candidate_id="domain_alias:test",
        )
        force_colour(monkeypatch, cli_classify)

        cli_classify._render_domain_candidates([candidate], total=1, emitted=False)

        rendered = assert_coloured(capsys.readouterr().out)
        visible = plain(rendered)
        assert f"{alias}: {canonical}" in visible
        assert f"-> {canonical}" in visible
        assert f"{alias}:" in visible
        assert competitor in visible

    def test_an_operator_mapped_alias_is_not_re_proposed(self, cli_env) -> None:
        """Filters its own writes, via ``classify.domain_aliases`` in config."""
        import yaml

        self._seed_fragmented()
        assert _run_json("domain-candidates", "--no-emit")["candidates"]

        config_path = Path(os.environ["TRELLIS_CONFIG_DIR"]) / "config.yaml"
        config = yaml.safe_load(config_path.read_text()) or {}
        config.setdefault("classify", {})["domain_aliases"] = {
            "budget-hunting": "hunting"
        }
        config_path.write_text(yaml.safe_dump(config))
        _reset_registry()

        assert _run_json("domain-candidates", "--no-emit")["candidates"] == []
