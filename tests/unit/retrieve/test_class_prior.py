"""Tests for the class-prior serving gate (trellis.retrieve.class_prior)."""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from trellis.retrieve.class_prior import (
    CLASS_PRIOR_AXES_ENV,
    CLASS_PRIOR_FORMS_ENV,
    CLASS_PRIOR_MODE_ENV,
    CLASS_PRIOR_MODE_SETTING,
    CLASS_PRIOR_REJECTION_REASON,
    CONVERSATION_DOCUMENT_FORM,
    KNOWN_AXES,
    KNOWN_FORMS,
    OFF,
    ClassPriorConfig,
    apply_class_prior,
    class_prior_telemetry,
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
        config = resolve_class_prior_config(settings={"class_prior_mode": "withhold"})
        assert config.mode == "withhold"

    def test_env_overrides_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CLASS_PRIOR_MODE_ENV, "withhold")
        config = resolve_class_prior_config(settings={"class_prior_mode": "off"})
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
        assert lines[0]["var"] == CLASS_PRIOR_AXES_ENV
        assert lines[0]["unknown_count"] == 1
        assert "names" not in lines[0]

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

    def test_unrecognised_env_mode_warning_names_the_variable_not_the_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unrecognised mode is a config mistake worth logging about, but
        the raw value never belongs in the log line — only which variable
        held it."""
        monkeypatch.setenv(CLASS_PRIOR_MODE_ENV, "not-a-mode")
        with capture_logs() as logs:
            resolve_class_prior_config(settings=None)
        lines = [e for e in logs if e["event"] == "class_prior_mode_env_unrecognised"]
        assert len(lines) == 1
        assert lines[0]["var"] == CLASS_PRIOR_MODE_ENV
        assert "value" not in lines[0]

    def test_unrecognised_stored_mode_degrades_to_off_with_a_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The settings-store sibling of
        ``test_unrecognised_env_mode_warning_names_the_variable_not_the_
        value`` above: an unrecognised *stored* mode (not ``demote``,
        which has its own dedicated warning) must still degrade to
        ``off`` rather than raise or silently resolve to something else,
        and must still warn once, naming the setting and never the
        value it held — matching the existing
        ``class_prior_mode_demote_removed`` style."""
        monkeypatch.delenv(CLASS_PRIOR_MODE_ENV, raising=False)
        with capture_logs() as logs:
            config = resolve_class_prior_config(
                settings={"class_prior_mode": "not-a-mode"}
            )
        assert config.mode == "off"
        lines = [
            e for e in logs if e["event"] == "class_prior_mode_settings_unrecognised"
        ]
        assert len(lines) == 1
        assert lines[0]["setting"] == CLASS_PRIOR_MODE_SETTING
        assert "value" not in lines[0]
        assert "var" not in lines[0]

    def test_stored_mode_is_case_normalised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stored value is stripped and lower-cased exactly like the env
        var, so a stored "WITHHOLD" resolves the same as env WITHHOLD."""
        monkeypatch.delenv(CLASS_PRIOR_MODE_ENV, raising=False)
        config = resolve_class_prior_config(settings={"class_prior_mode": "WITHHOLD"})
        assert config.mode == "withhold"

    def test_stored_mode_strips_surrounding_whitespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CLASS_PRIOR_MODE_ENV, raising=False)
        config = resolve_class_prior_config(settings={"class_prior_mode": " withhold "})
        assert config.mode == "withhold"

    def test_stored_demote_uppercase_resolves_to_off_with_setting_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stored "DEMOTE" (any case) degrades to off, with the same
        setting-naming warning the exact-lowercase-"demote" case gets —
        never a silent fall to off that skips the warning."""
        monkeypatch.delenv(CLASS_PRIOR_MODE_ENV, raising=False)
        with capture_logs() as logs:
            config = resolve_class_prior_config(settings={"class_prior_mode": "DEMOTE"})
        assert config.mode == "off"
        lines = [e for e in logs if e["event"] == "class_prior_mode_demote_removed"]
        assert len(lines) == 1
        assert lines[0]["setting"] == CLASS_PRIOR_MODE_SETTING

    def test_stored_demote_with_whitespace_resolves_to_off_with_setting_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CLASS_PRIOR_MODE_ENV, raising=False)
        with capture_logs() as logs:
            config = resolve_class_prior_config(
                settings={"class_prior_mode": " demote "}
            )
        assert config.mode == "off"
        lines = [e for e in logs if e["event"] == "class_prior_mode_demote_removed"]
        assert len(lines) == 1
        assert lines[0]["setting"] == CLASS_PRIOR_MODE_SETTING

    def test_governed_put_still_refuses_non_canonical_spelling(self) -> None:
        """Read-side normalisation (above) must not loosen the governed
        write path: the registry's enum check is an exact match, so a
        non-canonical spelling is still refused at the PUT boundary, not
        quietly accepted and normalised on write."""
        from trellis.core.settings_registry import SETTINGS_REGISTRY

        spec = SETTINGS_REGISTRY[CLASS_PRIOR_MODE_SETTING]
        assert spec.enum is not None
        assert "WITHHOLD" not in spec.enum
        assert " withhold " not in spec.enum
        assert "demote" not in spec.enum

    def test_forms_env_is_case_normalised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CLASS_PRIOR_FORMS_ENV, "CONVERSATION,Entity_Summary")
        config = resolve_class_prior_config(settings=None)
        assert config.forms == frozenset({"conversation", "entity_summary"})

    def test_forms_env_unknown_name_warns_but_is_not_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CLASS_PRIOR_FORMS_ENV, "conversation,not_a_real_form")
        with capture_logs() as logs:
            config = resolve_class_prior_config(settings=None)
        # Advisory only: document_form is open vocabulary, so an
        # unrecognised name is still accepted, never dropped or refused.
        assert config.forms == frozenset({"conversation", "not_a_real_form"})
        lines = [e for e in logs if e["event"] == "class_prior_forms_env_unrecognised"]
        assert len(lines) == 1
        assert lines[0]["var"] == CLASS_PRIOR_FORMS_ENV
        assert lines[0]["unknown_count"] == 1
        assert "names" not in lines[0]

    def test_forms_env_known_names_raise_no_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CLASS_PRIOR_FORMS_ENV, "conversation,entity_summary")
        with capture_logs() as logs:
            config = resolve_class_prior_config(settings=None)
        assert config.forms <= KNOWN_FORMS
        assert not [
            e for e in logs if e["event"] == "class_prior_forms_env_unrecognised"
        ]


class TestClassPriorTelemetry:
    """The compact ``PACK_ASSEMBLED.class_prior`` payload — pinned directly
    on the pure function; ``test_pack_holdout_seam.py`` separately pins
    that the *key* reaches every flat and sectioned row, including off."""

    def test_off_reports_mode_off_with_default_forms_and_axes(self) -> None:
        assert class_prior_telemetry(OFF) == {
            "mode": "off",
            "forms": ["conversation"],
            "axes": ["keyword"],
        }

    def test_withhold_reports_the_configured_forms_and_axes_sorted(self) -> None:
        config = ClassPriorConfig(
            mode="withhold",
            forms=frozenset({"entity_summary", "conversation"}),
            axes=frozenset({"semantic", "keyword"}),
        )
        assert class_prior_telemetry(config) == {
            "mode": "withhold",
            "forms": ["conversation", "entity_summary"],
            "axes": ["keyword", "semantic"],
        }

    def test_forms_and_axes_are_lists_not_the_underlying_frozenset(self) -> None:
        """``extra="forbid"`` wire DTOs and JSON serialization both need a
        list, not a frozenset — and a sorted list, so the payload is
        deterministic across runs with the same config."""
        payload = class_prior_telemetry(OFF)
        assert isinstance(payload["forms"], list)
        assert isinstance(payload["axes"], list)
