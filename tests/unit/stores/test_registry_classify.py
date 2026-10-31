"""Tests for ``classify:`` config wiring in ``StoreRegistry`` (WP7 Part 1).

Exercise :meth:`StoreRegistry.build_ingestion_pipeline` against the
``classify.domain_keywords`` block of ``config.yaml``: a domain defined only in
config is assigned by the ingestion pipeline, and reserved policy namespaces are
rejected loudly at build time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from trellis.errors import ConfigError
from trellis.schemas.classify_overrides import ClassifyOverrideRow
from trellis.stores.base.event_log import EventType
from trellis.stores.classify_overrides_store import (
    CLASSIFY_OVERLAY_SURFACE,
    CLASSIFY_OVERRIDES_FILENAME,
    ClassifyOverridesStore,
    resolve_classify_overrides_path,
)
from trellis.stores.registry import StoreRegistry


def _apply_overlay_row(stores_dir: Path, *, kind: str, key: str, target: str) -> None:
    """Write one applied rule straight to the overlay file.

    Mirrors what ``ClassifyRuleApplyHandler`` persists -- these tests care
    about the overlay's effect on the ingestion pipeline, not the governed
    write path, so writing the row directly (rather than through
    ``MutationExecutor``) keeps the fixture small.
    """
    path = resolve_classify_overrides_path(stores_dir)
    assert path is not None
    store = ClassifyOverridesStore(path)
    store.put(
        ClassifyOverrideRow(
            kind=kind,  # type: ignore[arg-type]
            key=key,
            target=target,
            applied_at=datetime.now(UTC),
            applied_by="test-operator",
            source_event_id="evt-1",
        )
    )


def _write_config(tmp_path: Path, classify_block: dict[str, Any] | None) -> Path:
    data: dict[str, Any] = {"knowledge": {}}
    if classify_block is not None:
        data["classify"] = classify_block
    config_dir = tmp_path / ".trellis"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(yaml.safe_dump(data))
    return config_dir


def test_no_classify_block_uses_defaults(tmp_path: Path) -> None:
    config_dir = _write_config(tmp_path, None)
    registry = StoreRegistry.from_config_dir(config_dir=config_dir)
    pipeline = registry.build_ingestion_pipeline()
    result = pipeline.classify("dbt spark warehouse etl transform")
    assert "data-pipeline" in result.tags.get("domain", [])


def test_config_only_domain_assigned_in_ingestion_mode(tmp_path: Path) -> None:
    config_dir = _write_config(
        tmp_path,
        {"domain_keywords": {"payments": ["stripe", "invoice", "chargeback"]}},
    )
    registry = StoreRegistry.from_config_dir(config_dir=config_dir)
    pipeline = registry.build_ingestion_pipeline()
    assert pipeline.mode == "ingestion"
    result = pipeline.classify("stripe invoice and chargeback reconciliation")
    assert "payments" in result.tags.get("domain", [])


def test_reserved_domain_in_config_rejected(tmp_path: Path) -> None:
    config_dir = _write_config(
        tmp_path, {"domain_keywords": {"sensitivity": ["secret"]}}
    )
    registry = StoreRegistry.from_config_dir(config_dir=config_dir)
    with pytest.raises(ValueError, match="reserved namespace 'sensitivity'"):
        registry.build_ingestion_pipeline()


def test_from_config_dict_carries_classify(tmp_path: Path) -> None:
    registry = StoreRegistry.from_config_dict(
        {
            "knowledge": {"document": {"backend": "sqlite"}},
            "classify": {"domain_keywords": {"widgets": ["sprocket", "gizmo"]}},
        },
        data_dir=tmp_path,
    )
    pipeline = registry.build_ingestion_pipeline()
    result = pipeline.classify("the sprocket and gizmo subassembly")
    assert "widgets" in result.tags.get("domain", [])


class TestOverlayReachesTheIngestionPipeline:
    """The applied-rule overlay must actually drive ``build_ingestion_pipeline``.

    ``domain_keyword_map()`` (what ``GET /api/v1/classify/rules`` lists as
    "effective") and ``build_ingestion_pipeline()`` (what backfill and
    ingestion actually classify with) both derive from
    ``trellis.classify.factory.effective_domain_keywords`` -- these tests
    pin that the two cannot read differently: an applied rule the listing
    shows as effective is a rule the pipeline actually uses, the overlay
    adds a keyword without erasing a domain's other keywords, and the
    precedence order (built-in defaults < ``config.yaml`` < operator
    overlay) holds end to end through a real ``StoreRegistry``.
    """

    def test_applied_overlay_keyword_is_used_by_the_built_pipeline(
        self, tmp_path: Path
    ) -> None:
        """A keyword that reaches a domain ONLY via the overlay still fires.

        ``config.yaml`` gives ``payments`` one keyword (``stripe``); the
        overlay applies a second (``chargeback``). Content carrying both
        hits the ``_MIN_HITS = 2`` floor only if the overlay keyword was
        actually folded into the pipeline's keyword map -- if
        ``build_ingestion_pipeline`` silently ignored the overlay (or
        built its classifier from a stale/duplicated merge), this would
        see only the one config-sourced hit and never assign the domain.
        """
        registry = StoreRegistry(
            stores_dir=tmp_path / "stores",
            classify_config={"domain_keywords": {"payments": ["stripe"]}},
        )
        _apply_overlay_row(
            registry.stores_dir,
            kind="tag_keyword",
            key="chargeback",
            target="payments",
        )
        pipeline = registry.build_ingestion_pipeline()
        result = pipeline.classify("We reconciled the stripe chargeback today.")
        assert "payments" in result.tags.get("domain", [])

    def test_overlay_keyword_does_not_erase_the_domain_s_other_keywords(
        self, tmp_path: Path
    ) -> None:
        """Applying one new keyword must not drop a domain's existing ones.

        ``config.yaml`` gives ``payments`` two keywords; the overlay
        applies a third, unrelated one. Content hitting only the two
        original config keywords must still assign the domain -- a
        pipeline built from a merge that *replaces* a domain's keyword
        list on overlay collision (instead of unioning onto it) would
        drop ``invoice``/``stripe`` the moment any overlay row targeted
        ``payments``, and this content would then score only zero hits.
        """
        registry = StoreRegistry(
            stores_dir=tmp_path / "stores",
            classify_config={"domain_keywords": {"payments": ["stripe", "invoice"]}},
        )
        _apply_overlay_row(
            registry.stores_dir,
            kind="tag_keyword",
            key="chargeback",
            target="payments",
        )
        pipeline = registry.build_ingestion_pipeline()
        result = pipeline.classify("The stripe invoice cleared without issue.")
        assert "payments" in result.tags.get("domain", [])

    def test_precedence_default_then_config_then_overlay_end_to_end(
        self, tmp_path: Path
    ) -> None:
        """default < config.yaml < overlay, proven through the built pipeline.

        ``config.yaml`` replaces the built-in ``api`` domain's keyword
        list wholesale with a custom pair (the documented config-over-
        default precedence); the overlay then adds one more keyword to
        that same domain. Content hitting only the built-in defaults
        (never mentioned by config or overlay) must NOT assign ``api``
        -- proving config really did replace the built-in list, not
        merely add to it -- while content hitting one config keyword
        plus the overlay keyword DOES assign it, proving the overlay
        unions onto config's list rather than the discarded built-in one.
        """
        registry = StoreRegistry(
            stores_dir=tmp_path / "stores",
            classify_config={"domain_keywords": {"api": ["custom-endpoint-term"]}},
        )
        _apply_overlay_row(
            registry.stores_dir,
            kind="tag_keyword",
            key="custom-overlay-term",
            target="api",
        )
        pipeline = registry.build_ingestion_pipeline()

        # Built-in "api" keywords alone (endpoint/route/rest/...) no longer
        # assign the domain: config.yaml replaced the built-in list.
        builtin_only = pipeline.classify("endpoint route rest graphql request")
        assert "api" not in builtin_only.tags.get("domain", [])

        # One config-sourced keyword plus the overlay-applied keyword
        # together clear _MIN_HITS, proving the overlay merged onto
        # config's list.
        config_plus_overlay = pipeline.classify(
            "custom-endpoint-term and custom-overlay-term both logged."
        )
        assert "api" in config_plus_overlay.tags.get("domain", [])


class TestDegradedOverlayRejectionIsObservable:
    """A corrupt overlay must not silently mine a partial view -- and the
    refusal must not be invisible either.

    ``build_ingestion_pipeline`` already refused to build from a degraded
    overlay with a ``ConfigError`` rather than quietly seeding the pipeline
    from whatever happened to parse. What it left out is the same gap
    #425 closed for ``policies.json``: this raise happens before any
    ``Command`` exists, so there is no ``MUTATION_REJECTED``, and every
    caller builds the pipeline outside its own ``WRITE_REJECTED`` boundary,
    so there is no boundary rejection either -- ``trellis analyze health``
    was structurally unable to see a degraded overlay. Modeled on
    ``tests/unit/mutate/test_policy_wiring.py::TestGateLoadFailureIsObservable``.
    """

    @staticmethod
    def _damage(stores_dir: Path, contents: str = "{ broken") -> None:
        stores_dir.mkdir(parents=True, exist_ok=True)
        (stores_dir / CLASSIFY_OVERRIDES_FILENAME).write_text(
            contents, encoding="utf-8"
        )

    def test_degraded_overlay_still_refuses_rather_than_mining_a_partial_view(
        self, tmp_path: Path
    ) -> None:
        stores_dir = tmp_path / "stores"
        self._damage(stores_dir)
        registry = StoreRegistry(stores_dir=stores_dir)

        with pytest.raises(ConfigError, match="Refusing to build"):
            registry.build_ingestion_pipeline()

    def test_degraded_overlay_emits_write_rejected(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        self._damage(stores_dir)
        registry = StoreRegistry(stores_dir=stores_dir)

        with pytest.raises(ConfigError):
            registry.build_ingestion_pipeline()

        events = registry.operational.event_log.get_events(
            event_type=EventType.WRITE_REJECTED, limit=10
        )
        assert len(events) == 1
        event = events[0]
        assert event.payload["tool"] == CLASSIFY_OVERLAY_SURFACE
        assert event.payload["error_class"] == "ConfigError"
        assert [r["kind"] for r in event.payload["rejections"]] == ["config_unreadable"]
        # The recovery advice the ConfigError carries survives into the
        # event, so an operator reading it gets the fix without re-running
        # ``backfill``.
        assert "classify_overrides.json" in event.payload["rejections"][0]["msg"]

    def test_analyze_health_sees_the_degraded_overlay(self, tmp_path: Path) -> None:
        from trellis.ops.write_health import summarize_write_health

        stores_dir = tmp_path / "stores"
        self._damage(stores_dir)
        registry = StoreRegistry(stores_dir=stores_dir)

        for _ in range(3):
            with pytest.raises(ConfigError):
                registry.build_ingestion_pipeline()

        report = summarize_write_health(registry.operational.event_log, days=1)
        assert report.by_tool[CLASSIFY_OVERLAY_SURFACE].boundary_rejected == 3
        assert report.status == "warn"

    def test_a_healthy_overlay_emits_nothing(self, tmp_path: Path) -> None:
        """No overlay file at all is the common case; it must stay quiet."""
        stores_dir = tmp_path / "stores"
        registry = StoreRegistry(stores_dir=stores_dir)
        registry.build_ingestion_pipeline()

        events = registry.operational.event_log.get_events(
            event_type=EventType.WRITE_REJECTED, limit=10
        )
        assert events == []
