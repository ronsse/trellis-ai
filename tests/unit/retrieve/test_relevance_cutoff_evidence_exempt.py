"""The relevance cutoff's evidence exemption (default off)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.unit.retrieve.test_relevance_cutoff import (
    _pack_assembled,
    _sem_pool,
    _strategies,
)
from trellis.retrieve.excerpts import ContentFloorConfig
from trellis.retrieve.feedback_prior import (
    STATE_APPLIED,
    EvidenceLedger,
    EvidenceResult,
    FeedbackEvidenceProvider,
    FeedbackPriorConfig,
    ItemEvidence,
    _Signal,
    factor_for,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.relevance_cutoff import (
    MIN_KEEP,
    MIN_POSITIVE_PACKS,
    RELEVANCE_CUTOFF_EVIDENCE_EXEMPT_ENV,
    RELEVANCE_CUTOFF_REJECTION_REASON,
    RelevanceCutoffConfig,
    apply_relevance_cutoff,
    evidence_exempts,
    resolve_relevance_cutoff_config,
)
from trellis.schemas.pack import PackItem, SectionRequest
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.event_log import SQLiteEventLog


class _Provider(FeedbackEvidenceProvider):
    """A fixed evidence table that counts how often it is read."""

    def __init__(self, table: dict[str, ItemEvidence] | None = None) -> None:
        self.table = table or {}
        self.calls = 0

    def evidence(self) -> EvidenceResult:
        self.calls += 1
        return EvidenceResult(state=STATE_APPLIED, table=self.table)


EXEMPT_ON = RelevanceCutoffConfig(mode="on", semantic_min=0.5, evidence_exempt="on")
#: ``b`` (0.4) and ``c`` (0.35) fail the 0.5 floor; ``b`` has net positive
#: evidence, ``c`` net negative.
TABLE = {
    "b": ItemEvidence(helpful=2, unhelpful=0, fetched=0),
    "c": ItemEvidence(helpful=1, unhelpful=2, fetched=0),
}


def _exempt_builder(
    *,
    config: RelevanceCutoffConfig = EXEMPT_ON,
    provider: FeedbackEvidenceProvider | None,
    event_log: SQLiteEventLog | None = None,
    **kwargs: Any,
) -> PackBuilder:
    return PackBuilder(
        strategies=_strategies(),
        content_floor=ContentFloorConfig(mode="off"),
        relevance_cutoff_config=config,
        feedback_evidence=provider,
        event_log=event_log,
        **kwargs,
    )


def _cut_rejected(pack: Any) -> set[str]:
    return {
        r.item_id
        for r in pack.retrieval_report.rejected_items
        if r.reason == RELEVANCE_CUTOFF_REJECTION_REASON
    }


class TestRule:
    @pytest.mark.parametrize(
        ("evidence", "expected"),
        [
            (None, False),
            (ItemEvidence(), False),
            (ItemEvidence(helpful=1), False),
            (ItemEvidence(fetched=1), False),
            (ItemEvidence(helpful=2), True),
            (ItemEvidence(fetched=2), True),
            (ItemEvidence(helpful=1, fetched=1), True),
            (ItemEvidence(helpful=1, unhelpful=1), False),
            (ItemEvidence(helpful=2, unhelpful=1), True),
            (ItemEvidence(helpful=2, unhelpful=2), False),
            (ItemEvidence(unhelpful=1), False),
        ],
    )
    def test_net_positive_from_two_packs(
        self, evidence: ItemEvidence | None, expected: bool
    ) -> None:
        assert MIN_POSITIVE_PACKS == 2
        assert evidence_exempts(evidence) is expected


_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _sig(pack: str, kind: str, minute: int = 0) -> _Signal:
    return _Signal(_T0 + timedelta(minutes=minute), pack, "x", kind, _T0)


def _exempts(*signals: _Signal) -> bool:
    return evidence_exempts(EvidenceLedger(list(signals)).table().get("x"))


class TestRuleFromLedger:
    """The rule over real ledger counts, so "distinct packs" is the ledger's."""

    def test_one_fetch_does_not_exempt(self) -> None:
        assert not _exempts(_sig("p1", "fetched"))

    def test_repeated_fetches_in_one_pack_do_not_exempt(self) -> None:
        assert not _exempts(*(_sig("p1", "fetched", m) for m in range(5)))

    def test_helpful_and_fetch_in_one_pack_does_not_exempt(self) -> None:
        assert not _exempts(_sig("p1", "helpful"), _sig("p1", "fetched", 1))

    def test_fetches_in_two_packs_exempt(self) -> None:
        assert _exempts(_sig("p1", "fetched"), _sig("p2", "fetched"))

    def test_two_positives_and_two_unhelpful_do_not_exempt(self) -> None:
        assert not _exempts(
            _sig("p1", "helpful"),
            _sig("p2", "fetched"),
            _sig("p3", "unhelpful"),
            _sig("p4", "unhelpful"),
        )

    def test_pack_time_leaves_the_prior_bit_identical(self) -> None:
        """``pack_at`` is read only by ``pack_since``; serving's table is unchanged."""
        kinds = ["helpful", "unhelpful", "fetched", "helpful", "fetched"]
        with_time = [
            _Signal(_T0, f"p{i}", f"i{i % 3}", k, _T0 - timedelta(days=200))
            for i, k in enumerate(kinds)
        ]
        without = [
            _Signal(s.occurred_at, s.pack_id, s.item_id, s.kind) for s in with_time
        ]
        a = EvidenceLedger(with_time).table()
        b = EvidenceLedger(without).table()
        assert a == b
        config = FeedbackPriorConfig(mode="on")
        for item_id in a:
            assert factor_for(a[item_id], config) == factor_for(b[item_id], config)


class TestResolve:
    def test_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(RELEVANCE_CUTOFF_EVIDENCE_EXEMPT_ENV, raising=False)
        assert resolve_relevance_cutoff_config({}).evidence_exempt == "off"

    def test_env_beats_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RELEVANCE_CUTOFF_EVIDENCE_EXEMPT_ENV, "off")
        settings = {"relevance_cutoff_evidence_exempt": "on"}
        assert resolve_relevance_cutoff_config(settings).evidence_exempt == "off"
        monkeypatch.delenv(RELEVANCE_CUTOFF_EVIDENCE_EXEMPT_ENV)
        assert resolve_relevance_cutoff_config(settings).evidence_exempt == "on"

    def test_unrecognised_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RELEVANCE_CUTOFF_EVIDENCE_EXEMPT_ENV, "yes")
        assert resolve_relevance_cutoff_config({}).evidence_exempt == "off"


class TestOn:
    def test_exempted_item_is_served_and_unrecorded(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        provider = _Provider(TABLE)
        pack = _exempt_builder(provider=provider, event_log=log).build(
            "q", clearance="restricted"
        )
        assert {i.item_id for i in pack.items} == {"a", "b", "d", "e"}
        assert _cut_rejected(pack) == {"c"}
        assert [
            r["item_id"] for r in pack.metadata["relevance_cutoff"]["cut_items"]
        ] == ["c"]
        payload = _pack_assembled(log)
        block = payload["relevance_cutoff"]
        assert [r["item_id"] for r in block["cut_items"]] == ["c"]
        assert block["evidence_exempt"] == {
            "state": STATE_APPLIED,
            "count": 1,
            "item_ids": ["b"],
            "evidence": {"b": {"helpful": 2, "unhelpful": 0, "fetched": 0}},
        }
        assert "b" not in {r["item_id"] for r in payload["rejected_items"]}
        assert provider.calls == 1

    def test_sectioned_path_exempts(self) -> None:
        pack = _exempt_builder(provider=_Provider(TABLE)).build_sectioned(
            intent="q", sections=[SectionRequest(name="all")], clearance="restricted"
        )
        served = {i.item_id for s in pack.sections for i in s.items}
        assert "b" in served
        assert "c" not in served

    def test_unavailable_evidence_exempts_nothing(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        pack = _exempt_builder(provider=None, event_log=log).build(
            "q", clearance="restricted"
        )
        assert _cut_rejected(pack) == {"b", "c"}
        block = _pack_assembled(log)["relevance_cutoff"]["evidence_exempt"]
        assert block["count"] == 0
        assert block["state"] != STATE_APPLIED

    def test_failing_provider_exempts_nothing(self, tmp_path: Path) -> None:
        class _Boom(FeedbackEvidenceProvider):
            def evidence(self) -> EvidenceResult:
                msg = "boom"
                raise RuntimeError(msg)

        log = SQLiteEventLog(tmp_path / "events.db")
        pack = _exempt_builder(provider=_Boom(), event_log=log).build(
            "q", clearance="restricted"
        )
        assert _cut_rejected(pack) == {"b", "c"}
        block = _pack_assembled(log)["relevance_cutoff"]["evidence_exempt"]
        assert (block["state"], block["count"]) == ("error", 0)

    def test_works_with_feedback_prior_off(self) -> None:
        provider = _Provider(TABLE)
        pack = _exempt_builder(
            provider=provider, feedback_prior_config=FeedbackPriorConfig(mode="off")
        ).build("q", clearance="restricted")
        assert "b" in {i.item_id for i in pack.items}
        assert provider.calls == 1

    def test_failing_provider_read_once_with_feedback_prior_on(
        self, tmp_path: Path
    ) -> None:
        """A raising provider is memoized too: one read, both blocks ``error``."""

        class _Boom(FeedbackEvidenceProvider):
            calls = 0

            def evidence(self) -> EvidenceResult:
                type(self).calls += 1
                msg = "boom"
                raise RuntimeError(msg)

        log = SQLiteEventLog(tmp_path / "events.db")
        pack = _exempt_builder(
            provider=_Boom(),
            event_log=log,
            feedback_prior_config=FeedbackPriorConfig(mode="on"),
        ).build("q", clearance="restricted")
        assert _Boom.calls == 1
        assert _cut_rejected(pack) == {"b", "c"}
        payload = _pack_assembled(log)
        assert payload["relevance_cutoff"]["evidence_exempt"]["state"] == "error"
        assert payload["feedback_prior"]["state"] == "error"

    def test_ledger_read_once_with_feedback_prior_on(self) -> None:
        provider = _Provider(TABLE)
        pack = _exempt_builder(
            provider=provider, feedback_prior_config=FeedbackPriorConfig(mode="on")
        ).build("q", clearance="restricted")
        assert "b" in {i.item_id for i in pack.items}
        assert provider.calls == 1


class TestShadow:
    def test_reports_would_be_exemptions_and_cuts_nothing(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        config = RelevanceCutoffConfig(
            mode="shadow", semantic_min=0.5, evidence_exempt="on"
        )
        pack = _exempt_builder(
            config=config, provider=_Provider(TABLE), event_log=log
        ).build("q", clearance="restricted")
        assert {i.item_id for i in pack.items} == {"a", "b", "c", "d", "e"}
        assert "relevance_cutoff" not in pack.metadata
        block = _pack_assembled(log)["relevance_cutoff"]
        assert block["evidence_exempt"]["item_ids"] == ["b"]
        assert [r["item_id"] for r in block["cut_items"]] == ["c"]
        assert _cut_rejected(pack) == set()


class TestMinKeepOrder:
    def test_exempted_ids_count_toward_min_keep_first(self) -> None:
        # s0 passes; s1..s3 fail. Without the exemption min_keep restores
        # the two best failures (s1, s2). Exempting the worst (s3) first
        # leaves two survivors, so min_keep restores only s1 and s2 is cut.
        pool = _sem_pool([0.9, 0.4, 0.35, 0.3])
        plain = apply_relevance_cutoff(
            pool,
            config=RelevanceCutoffConfig(mode="on", semantic_min=0.5),
            price=lambda _i: 1,
        )
        assert {i.item_id for i in plain.kept} == {"s0", "s1", "s2"}
        table = {"s3": ItemEvidence(helpful=2)}
        result = apply_relevance_cutoff(
            pool,
            config=RelevanceCutoffConfig(
                mode="on", semantic_min=0.5, evidence_exempt="on"
            ),
            price=lambda _i: 1,
            evidence=lambda: EvidenceResult(state=STATE_APPLIED, table=table),
        )
        assert result.exempted == ["s3"]
        assert result.restored == ["s1"]
        assert {i.item_id for i in result.kept} == {"s0", "s1", "s3"}
        assert len(result.kept) == MIN_KEEP
        assert [i.item_id for i in result.cut] == ["s2"]


class TestNearby:
    def test_exempted_item_is_not_listed(self) -> None:
        builder = PackBuilder(
            strategies=_strategies(),
            content_floor=ContentFloorConfig(mode="off"),
            relevance_cutoff_config=EXEMPT_ON,
            feedback_evidence=_Provider(TABLE),
            nearby_mode="on",
        )
        pack = builder.build("q", clearance="restricted")
        nearby = json.dumps(pack.metadata.get("nearby", {}))
        assert '"c"' in nearby
        assert '"b"' not in nearby


class TestHeldOut:
    def test_row_carries_config_only(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        _exempt_builder(
            provider=_Provider(TABLE), event_log=log, holdout_rate=1.0
        ).build("q", clearance="restricted")
        assert _pack_assembled(log)["relevance_cutoff"] == {
            "mode": "on",
            "semantic_min": 0.5,
            "semantic_ratio": None,
            "evidence_exempt_mode": "on",
        }


class TestPublicOnly:
    def test_on_equals_off(self, tmp_path: Path) -> None:
        from tests.unit.retrieve.test_public_clearance import _PoolSemantic
        from tests.unit.retrieve.test_sensitivity import label

        docs = SQLiteDocumentStore(tmp_path / "documents.db")
        for n in range(5):
            docs.put(f"p{n}", "body", {"title": f"t{n}", **label("public")})
        pool = [
            PackItem(
                item_id=f"p{n}",
                item_type="document",
                excerpt="body",
                relevance_score=s,
                score_breakdown={"native_semantic": s},
            )
            for n, s in enumerate([0.9, 0.2, 0.1, 0.05, 0.01])
        ]
        packs = []
        for exempt in ("off", "on"):
            provider = _Provider({"p1": ItemEvidence(helpful=3)})
            builder = PackBuilder(
                strategies=[_PoolSemantic(pool)],
                document_store=docs,
                content_floor=ContentFloorConfig(mode="off"),
                relevance_cutoff_config=RelevanceCutoffConfig(
                    mode="on", semantic_min=0.5, evidence_exempt=exempt
                ),
                feedback_evidence=provider,
            )
            packs.append(builder.build("intent", clearance="public"))
            assert provider.calls == 0
        off, on = packs
        assert [i.model_dump() for i in on.items] == [i.model_dump() for i in off.items]
        assert "relevance_cutoff" not in on.metadata


_VOLATILE = {"pack_id", "assembled_at", "created_at", "timestamp", "duration_ms"}


def _normalise(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    return value


class TestFlagOffIdentity:
    """With the exemption off, a provider changes nothing and is never read."""

    @pytest.mark.parametrize("cutoff_mode", ["off", "shadow", "on"])
    def test_pack_metadata_and_event_identical(
        self, tmp_path: Path, cutoff_mode: str
    ) -> None:
        config = RelevanceCutoffConfig(
            mode=cutoff_mode,  # type: ignore[arg-type]
            semantic_min=0.5 if cutoff_mode != "off" else None,
        )
        outputs = []
        for n, provider in enumerate([None, _Provider(TABLE)]):
            log = SQLiteEventLog(tmp_path / f"events{n}.db")
            pack = _exempt_builder(
                config=config,
                provider=provider,
                event_log=log,
                feedback_prior_config=FeedbackPriorConfig(mode="off"),
            ).build("q", clearance="restricted")
            if provider is not None:
                assert provider.calls == 0
            outputs.append(
                _normalise(
                    {
                        "items": [i.model_dump(mode="json") for i in pack.items],
                        "metadata": json.loads(json.dumps(pack.metadata, default=str)),
                        "event": _pack_assembled(log),
                    }
                )
            )
        differing = {
            part: sorted(
                k
                for k in set(outputs[0][part]) | set(outputs[1][part])
                if outputs[0][part].get(k) != outputs[1][part].get(k)
            )
            for part in ("metadata", "event")
        }
        assert outputs[0] == outputs[1], differing
        assert "evidence_exempt" not in json.dumps(outputs[0])
