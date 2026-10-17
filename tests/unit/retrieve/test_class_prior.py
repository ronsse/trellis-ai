"""Tests for the class-prior serving gate (trellis.retrieve.class_prior)."""

from __future__ import annotations

import pytest

from trellis.retrieve.class_prior import (
    CLASS_PRIOR_FACTOR_ENV,
    CLASS_PRIOR_MODE_ENV,
    CLASS_PRIOR_REJECTION_REASON,
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

    @pytest.mark.parametrize("factor", [0.0, 1.0, -0.1, 1.5])
    def test_factor_must_be_open_interval(self, factor: float) -> None:
        with pytest.raises(ValueError, match="factor"):
            ClassPriorConfig(mode="demote", factor=factor)


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


class TestApplyClassPriorDemote:
    def test_demote_rescales_score_and_stamps_metadata_without_dropping(self) -> None:
        config = ClassPriorConfig(mode="demote", factor=0.5)
        conv = _item("c1", form="conversation", score=0.8)
        kept, rejected = apply_class_prior(
            [conv], config=config, strategy_name="keyword"
        )
        assert rejected == []
        assert len(kept) == 1
        demoted = kept[0]
        assert demoted.item_id == "c1"
        assert demoted.relevance_score == pytest.approx(0.4)
        assert demoted.metadata["class_prior"] == {
            "mode": "demote",
            "form": "conversation",
            "axis": "keyword",
            "factor": 0.5,
        }
        # The input item itself is untouched (model_copy, not mutation).
        assert conv.relevance_score == 0.8
        assert "class_prior" not in conv.metadata

    def test_demote_never_touches_another_form(self) -> None:
        config = ClassPriorConfig(mode="demote", factor=0.5)
        other = _item("m1", form="capture", score=0.8)
        kept, rejected = apply_class_prior(
            [other], config=config, strategy_name="keyword"
        )
        assert rejected == []
        assert kept == [other]
        assert kept[0] is other


class TestResolveClassPriorConfig:
    def test_default_with_nothing_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(CLASS_PRIOR_MODE_ENV, raising=False)
        monkeypatch.delenv(CLASS_PRIOR_FACTOR_ENV, raising=False)
        config = resolve_class_prior_config(settings=None)
        assert config == OFF

    def test_settings_value_used_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CLASS_PRIOR_MODE_ENV, raising=False)
        monkeypatch.delenv(CLASS_PRIOR_FACTOR_ENV, raising=False)
        config = resolve_class_prior_config(
            settings={"class_prior_mode": "withhold", "class_prior_factor": 0.25}
        )
        assert config.mode == "withhold"
        assert config.factor == 0.25

    def test_env_overrides_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CLASS_PRIOR_MODE_ENV, "demote")
        monkeypatch.setenv(CLASS_PRIOR_FACTOR_ENV, "0.75")
        config = resolve_class_prior_config(
            settings={"class_prior_mode": "withhold", "class_prior_factor": 0.25}
        )
        assert config.mode == "demote"
        assert config.factor == 0.75

    def test_unrecognised_env_mode_degrades_to_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CLASS_PRIOR_MODE_ENV, "not-a-mode")
        monkeypatch.delenv(CLASS_PRIOR_FACTOR_ENV, raising=False)
        config = resolve_class_prior_config(settings=None)
        assert config.mode == "off"

    def test_out_of_range_env_factor_degrades_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CLASS_PRIOR_MODE_ENV, raising=False)
        monkeypatch.setenv(CLASS_PRIOR_FACTOR_ENV, "1.0")
        config = resolve_class_prior_config(settings=None)
        assert config.factor == 0.5

    def test_out_of_range_settings_factor_degrades_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CLASS_PRIOR_MODE_ENV, raising=False)
        monkeypatch.delenv(CLASS_PRIOR_FACTOR_ENV, raising=False)
        config = resolve_class_prior_config(settings={"class_prior_factor": 1.0})
        assert config.factor == 0.5
