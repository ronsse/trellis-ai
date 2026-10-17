"""Tests for the class-prior serving gate (trellis.retrieve.class_prior)."""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from trellis.retrieve.class_prior import (
    CLASS_PRIOR_AXES_ENV,
    CLASS_PRIOR_MODE_ENV,
    CLASS_PRIOR_MODE_SETTING,
    CLASS_PRIOR_REJECTION_REASON,
    CONVERSATION_DOCUMENT_FORM,
    KNOWN_AXES,
    OFF,
    ClassPriorConfig,
    apply_class_prior,
    resolve_class_prior_config,
)
from trellis.schemas.pack import PackItem


def _item(item_id: str, *, form: str | None, score: float = 0.5) -> PackItem:
    metadata = {"document_form": form} if form is not None else {}
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt="text",
        relevance_score=score,
        metadata=metadata,
    )


class TestClassPriorConfig:
    def test_default_is_off(self) -> None:
        assert OFF.mode == "off"
        assert ClassPriorConfig().mode == "off"

    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="mode"):
            ClassPriorConfig(mode="demote_harder")  # type: ignore[arg-type]

    def test_demote_is_not_a_valid_mode(self) -> None:
        """Demote was removed entirely (see the module docstring); the
        dataclass itself must refuse it, not just the resolver."""
        with pytest.raises(ValueError, match="mode"):
            ClassPriorConfig(mode="demote")  # type: ignore[arg-type]


class TestConstantPinnedToImporter:
    def test_conversation_document_form_matches_the_importer(self) -> None:
        """The by-value duplicate in this module must never drift from
        the writer's constant — see the module docstring's explanation of
        why this is a duplicate rather than an import."""
        from trellis.ingest_corpus.conversations import (
            CONVERSATION_DOCUMENT_FORM as IMPORTER_CONVERSATION_DOCUMENT_FORM,
        )

        assert CONVERSATION_DOCUMENT_FORM == IMPORTER_CONVERSATION_DOCUMENT_FORM


class TestApplyClassPriorOff:
    def test_off_is_a_true_noop(self) -> None:
        """Shipped default: byte-identical items, same object, no rejects."""
        items = [_item("c1", form="conversation"), _item("m1", form=None)]
        kept, rejected = apply_class_prior(items, config=OFF, strategy_name="keyword")
        assert kept is items
        assert rejected == []

    def test_axis_scoping_short_circuits_even_when_mode_is_withhold(self) -> None:
        config = ClassPriorConfig(mode="withhold")
        items = [_item("c1", form="conversation")]
        kept, rejected = apply_class_prior(
            items, config=config, strategy_name="semantic"
        )
        assert kept is items
        assert rejected == []

    def test_empty_forms_short_circuits(self) -> None:
        config = ClassPriorConfig(mode="withhold", forms=frozenset())
        items = [_item("c1", form="conversation")]
        kept, rejected = apply_class_prior(
            items, config=config, strategy_name="keyword"
        )
        assert kept is items
        assert rejected == []


class TestApplyClassPriorWithhold:
    def test_withhold_drops_matching_form_and_keeps_the_rest(self) -> None:
        config = ClassPriorConfig(mode="withhold")
        conv = _item("c1", form="conversation")
        other = _item("m1", form="capture")
        unlabelled = _item("m2", form=None)
        kept, rejected = apply_class_prior(
            [conv, other, unlabelled], config=config, strategy_name="keyword"
        )
        assert kept == [other, unlabelled]
        assert len(rejected) == 1
        assert rejected[0].item_id == "c1"
        assert rejected[0].reason == CLASS_PRIOR_REJECTION_REASON
        assert rejected[0].strategy_source == "keyword"
        # Distinct from every other collect-seam reason.
        assert CLASS_PRIOR_REJECTION_REASON not in {
            "noise",
            "archived",
            "demoted",
            "superseded",
            "dedup",
        }

    def test_a_document_of_another_form_is_never_touched(self) -> None:
        config = ClassPriorConfig(mode="withhold")
        other = _item("m1", form="capture", score=0.42)
        kept, rejected = apply_class_prior(
            [other], config=config, strategy_name="keyword"
        )
        assert kept == [other]
        assert kept[0] is other  # same object, not a copy
        assert rejected == []

    def test_axis_scoping_holds(self) -> None:
        """A matching form on an axis outside config.axes is left alone."""
        config = ClassPriorConfig(mode="withhold", axes=frozenset({"keyword"}))
        conv = _item("c1", form="conversation")
        kept, rejected = apply_class_prior(
            [conv], config=config, strategy_name="semantic"
        )
        assert kept == [conv]
        assert kept[0] is conv
        assert rejected == []


class TestResolveClassPriorConfig:
    def test_default_with_nothing_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(CLASS_PRIOR_MODE_ENV, raising=False)
        config = resolve_class_prior_config(settings=None)
        assert config == OFF

    def test_settings_value_used_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CLASS_PRIOR_MODE_ENV, raising=False)
        config = resolve_class_prior_config(
            settings={"class_prior_mode": "withhold"}
        )
        assert config.mode == "withhold"

    def test_env_overrides_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CLASS_PRIOR_MODE_ENV, "withhold")
        config = resolve_class_prior_config(
            settings={"class_prior_mode": "off"}
        )
        assert config.mode == "withhold"

    def test_unrecognised_env_mode_degrades_to_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CLASS_PRIOR_MODE_ENV, "not-a-mode")
        config = resolve_class_prior_config(settings=None)
        assert config.mode == "off"

    def test_env_demote_resolves_to_off_not_withhold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Demote was removed entirely. A stored or env
        ``class_prior_mode=demote`` must resolve to off, and must never
        silently become withhold."""
        monkeypatch.setenv(CLASS_PRIOR_MODE_ENV, "demote")
        config = resolve_class_prior_config(settings=None)
        assert config.mode == "off"

    def test_settings_demote_resolves_to_off_not_withhold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CLASS_PRIOR_MODE_ENV, raising=False)
        config = resolve_class_prior_config(settings={"class_prior_mode": "demote"})
        assert config.mode == "off"

    def test_env_demote_warning_names_the_setting_not_the_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CLASS_PRIOR_MODE_ENV, "demote")
        with capture_logs() as logs:
            resolve_class_prior_config(settings=None)
        lines = [e for e in logs if e["event"] == "class_prior_mode_demote_removed"]
        assert len(lines) == 1
        assert lines[0]["var"] == CLASS_PRIOR_MODE_ENV
        assert "setting" not in lines[0]
        assert "value" not in lines[0]

    def test_settings_demote_warning_names_the_setting_not_the_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CLASS_PRIOR_MODE_ENV, raising=False)
        with capture_logs() as logs:
            resolve_class_prior_config(settings={"class_prior_mode": "demote"})
        lines = [e for e in logs if e["event"] == "class_prior_mode_demote_removed"]
        assert len(lines) == 1
        assert lines[0]["setting"] == CLASS_PRIOR_MODE_SETTING
        assert "var" not in lines[0]
        assert "value" not in lines[0]

    def test_axes_env_is_case_normalised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CLASS_PRIOR_AXES_ENV, "KEYWORD,Semantic")
        config = resolve_class_prior_config(settings=None)
        assert config.axes == frozenset({"keyword", "semantic"})

    def test_axes_env_unknown_name_warns_but_is_not_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CLASS_PRIOR_AXES_ENV, "keyword,not_a_real_axis")
        with capture_logs() as logs:
            config = resolve_class_prior_config(settings=None)
        # Advisory only: the open strategy set means an unrecognised name is
        # still accepted, never dropped or refused.
        assert config.axes == frozenset({"keyword", "not_a_real_axis"})
        lines = [e for e in logs if e["event"] == "class_prior_axes_env_unrecognised"]
        assert len(lines) == 1
        assert lines[0]["names"] == ["not_a_real_axis"]

    def test_axes_env_known_names_raise_no_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CLASS_PRIOR_AXES_ENV, "keyword,graph")
        with capture_logs() as logs:
            config = resolve_class_prior_config(settings=None)
        assert config.axes <= KNOWN_AXES
        assert not [
            e for e in logs if e["event"] == "class_prior_axes_env_unrecognised"
        ]
