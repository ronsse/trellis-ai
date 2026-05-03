"""Advisory loop — inject the headline feature, suppress at the cutoff.

Two complementary tests for the advisory subsystem the project shipped
in #65:

* :func:`test_advisory_inject` proves the full closed loop end-to-end
  through the live REST surface — pack rounds, feedback, advisory
  generation, and re-attachment on the next pack.
* :func:`test_advisory_suppress` writes advisories directly into the
  store at calibrated confidences and asserts the pack-time cutoff
  (``PackBuilder._ADVISORY_MIN_CONFIDENCE = 0.1``) actually filters
  the low-confidence one out. Mirrors the inside-out
  ``test_suppresses_failing_advisory`` but at the public surface,
  using :class:`AdvisoryStore`'s file format as the seam.

The inside-out version of the inject loop runs in the
``agent_loop_convergence`` scenario; the README there documents why
organic suppression is hard to provoke on a balanced corpus, which is
why the suppress half here uses direct store manipulation rather than
trying to engineer a regime shift through the public surface.

Skipped when ``TRELLIS_TEST_NEO4J_URI`` or ``TRELLIS_TEST_PG_DSN``
is unset — same gating as the rest of the loop suite.
"""

from __future__ import annotations

import httpx
import pytest

from tests.integration.loops.conftest import LoopEnvironment

pytestmark = pytest.mark.asyncio


# Each bucket gets its own single-word marker. Postgres FTS tokenizes
# multi-word intents to terms that have to *both* be present in
# document content; mixing in a generic word like "winner" empties
# every result set, which the noise-demote loop's docstring also
# documents. Two distinct markers keeps each bucket's intent
# single-word and selectively retrieves only that bucket's docs.
_WINNER_MARKER = "advisorywinner"
_LOSER_MARKER = "advisoryloser"
_SUPPRESS_MARKER = "advisorysuppress"

# AdvisoryGenerator's defaults are min_sample_size=5 / min_effect=0.15;
# the API endpoint exposes both as query params. Two passes through a
# 3+3 success/failure corpus is plenty to fire one ENTITY advisory and
# one ANTI_PATTERN advisory at confidence >=0.1, while keeping the
# round count low enough that the test wall-clock stays reasonable.
_PACK_ROUNDS_PER_BUCKET = 3
_GENERATE_MIN_SAMPLE = 2
_GENERATE_MIN_EFFECT = 0.15

# Above PackBuilder._ADVISORY_MIN_CONFIDENCE (0.1).
_HIGH_CONFIDENCE = 0.5
# Below the cutoff.
_LOW_CONFIDENCE = 0.05


def _seed_inject_corpus(api_url: str, *, domain: str) -> tuple[list[str], list[str]]:
    """Seed two distinguishable buckets within one domain.

    Returns ``(winner_doc_ids, loser_doc_ids)``. Both buckets share
    ``_INTENT_MARKER`` so a single search retrieves them, but the
    winner docs carry "winner" content and the loser docs carry "loser"
    content so the test can issue searches that selectively retrieve
    one bucket or the other.
    """
    winners = [f"adv:winner:{i}" for i in range(2)]
    losers = [f"adv:loser:{i}" for i in range(2)]
    with httpx.Client(base_url=api_url, timeout=30.0) as client:
        for doc_id in winners:
            resp = client.post(
                "/api/v1/documents",
                json={
                    "doc_id": doc_id,
                    "content": (
                        f"{_WINNER_MARKER} reference document {doc_id}. "
                        "Canonical workflow that should appear in successful "
                        "packs."
                    ),
                    "metadata": {"domain": domain, "source": "advisory-test"},
                },
            )
            assert resp.status_code == 200, resp.text
        for doc_id in losers:
            resp = client.post(
                "/api/v1/documents",
                json={
                    "doc_id": doc_id,
                    "content": (
                        f"{_LOSER_MARKER} reference document {doc_id}. "
                        "Misleading workflow that should appear in failing "
                        "packs."
                    ),
                    "metadata": {"domain": domain, "source": "advisory-test"},
                },
            )
            assert resp.status_code == 200, resp.text
    return winners, losers


def _build_and_grade(
    client: httpx.Client,
    *,
    intent: str,
    domain: str,
    success: bool,
) -> dict:
    """Build one pack and immediately attach a success/failure verdict.

    Returns the pack response body so callers can inspect items if
    needed. Token budget kept generous so the small corpus always fits
    — budget pressure isn't what this loop is exercising.
    """
    resp = client.post(
        "/api/v1/packs",
        json={
            "intent": intent,
            "domain": domain,
            "max_items": 10,
            "max_tokens": 2000,
        },
    )
    assert resp.status_code == 200, resp.text
    pack = resp.json()
    feedback = client.post(
        f"/api/v1/packs/{pack['pack_id']}/feedback",
        params={"success": success},
    )
    assert feedback.status_code == 200, feedback.text
    return pack


async def test_advisory_inject(loop_env: LoopEnvironment) -> None:
    """Outcome data + advisory generation → next pack carries advisories.

    Drives the canonical happy path entirely through REST: seed two
    buckets, run pack rounds with deterministic success/failure
    verdicts, kick off advisory generation, then assemble a fresh pack
    and assert ``advisories`` is non-empty in the response body.
    """
    domain = "advisory-inject"
    _seed_inject_corpus(loop_env.api_url, domain=domain)

    with httpx.Client(base_url=loop_env.api_url, timeout=30.0) as client:
        for _ in range(_PACK_ROUNDS_PER_BUCKET):
            _build_and_grade(
                client,
                intent=_WINNER_MARKER,
                domain=domain,
                success=True,
            )
        for _ in range(_PACK_ROUNDS_PER_BUCKET):
            _build_and_grade(
                client,
                intent=_LOSER_MARKER,
                domain=domain,
                success=False,
            )

        gen_resp = client.post(
            "/api/v1/advisories/generate",
            params={
                "min_sample": _GENERATE_MIN_SAMPLE,
                "min_effect": _GENERATE_MIN_EFFECT,
            },
        )
        assert gen_resp.status_code == 200, gen_resp.text
        gen_body = gen_resp.json()
        assert gen_body["status"] == "ok", gen_body
        # The corpus is engineered to produce at least one advisory.
        # If this is zero the math broke — surface it before asserting
        # downstream attachment, which would fail more confusingly.
        assert gen_body["advisories_stored"] >= 1, gen_body

        list_resp = client.get(
            "/api/v1/advisories",
            params={"min_confidence": 0.1},
        )
        assert list_resp.status_code == 200, list_resp.text
        list_body = list_resp.json()
        assert list_body["count"] >= 1, list_body

        # Now build a fresh pack and assert the advisory attached. The
        # advisory's scope is either the test domain or "global", and
        # PackBuilder filters by ``scope in {"global", domain}``, so a
        # pack on the same domain should always pick it up.
        next_pack_resp = client.post(
            "/api/v1/packs",
            json={
                "intent": _WINNER_MARKER,
                "domain": domain,
                "max_items": 10,
                "max_tokens": 2000,
            },
        )
        assert next_pack_resp.status_code == 200, next_pack_resp.text
        next_pack = next_pack_resp.json()
        assert next_pack["advisories"], (
            "Expected at least one advisory attached to the pack after "
            f"generation reported {gen_body['advisories_stored']} stored, "
            f"but got: {next_pack.get('advisories')}"
        )


async def test_advisory_suppress(loop_env: LoopEnvironment) -> None:
    """Advisories below ``_ADVISORY_MIN_CONFIDENCE`` don't reach packs.

    Bypasses the generator and writes two advisories directly to the
    file-backed store: one above the 0.1 cutoff and one below it. Then
    builds a pack via REST and asserts the cutoff filter actually fires
    — only the high-confidence advisory should attach.

    Direct store manipulation (rather than provoking suppression
    organically through feedback) is deliberate: the inside-out
    scenario README documents why organic suppression is hard to
    reproduce on a balanced corpus, and the cutoff filter itself is
    the unit under test here, not the confidence-decay maths.
    """
    # Both subprocesses (uvicorn + trellis-mcp) read advisories from
    # ``<stores_dir>/advisories.json``. ``StoreRegistry.from_config_dir``
    # derives stores_dir as ``data_dir / "stores"``, so the test process
    # writes the same path the API server reads.
    from trellis.schemas.advisory import (
        Advisory,
        AdvisoryCategory,
        AdvisoryEvidence,
    )
    from trellis.stores.advisory_store import AdvisoryStore

    domain = "advisory-suppress"
    advisory_path = loop_env.data_dir / "stores" / "advisories.json"
    advisory_path.parent.mkdir(parents=True, exist_ok=True)

    # Minimal evidence body — none of these numbers are read by the
    # cutoff filter, but the schema requires them.
    fake_evidence = AdvisoryEvidence(
        sample_size=10,
        success_rate_with=0.9,
        success_rate_without=0.4,
        effect_size=0.5,
    )
    high = Advisory(
        category=AdvisoryCategory.ENTITY,
        confidence=_HIGH_CONFIDENCE,
        message="High-confidence advisory should attach.",
        evidence=fake_evidence,
        scope=domain,
    )
    low = Advisory(
        category=AdvisoryCategory.ENTITY,
        confidence=_LOW_CONFIDENCE,
        message="Low-confidence advisory should be filtered.",
        evidence=fake_evidence,
        scope=domain,
    )
    store = AdvisoryStore(advisory_path)
    store.put_many([high, low])

    # A single document is enough for the pack to assemble; this test
    # is about advisory routing, not retrieval scoring.
    with httpx.Client(base_url=loop_env.api_url, timeout=30.0) as client:
        seed = client.post(
            "/api/v1/documents",
            json={
                "doc_id": "adv:suppress:seed",
                "content": (
                    f"{_SUPPRESS_MARKER} suppress test fixture. A single "
                    "document so the pack assembles cleanly."
                ),
                "metadata": {"domain": domain, "source": "advisory-test"},
            },
        )
        assert seed.status_code == 200, seed.text

        pack_resp = client.post(
            "/api/v1/packs",
            json={
                "intent": _SUPPRESS_MARKER,
                "domain": domain,
                "max_items": 10,
                "max_tokens": 2000,
            },
        )
        assert pack_resp.status_code == 200, pack_resp.text
        pack = pack_resp.json()

    attached_ids = {a["advisory_id"] for a in pack["advisories"]}
    assert high.advisory_id in attached_ids, (
        f"High-confidence advisory missing from pack. attached={attached_ids}"
    )
    assert low.advisory_id not in attached_ids, (
        f"Low-confidence advisory should have been filtered by the "
        f"_ADVISORY_MIN_CONFIDENCE cutoff. attached={attached_ids}"
    )
