"""Tests for the dbt loader's lineage-of-X category index.

Loads the Jaffle Shop fixture into an in-memory SQLite registry and
asserts that ``build_category_index`` emits phrases that map the
multi_hop_lineage ground-truth intent to the full upstream closure of
the customers mart — closing the gap where the prior depth=2 traversal
+ ranking dropped staging/raw entities under the 8-item pack budget.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from eval.corpora.dbt_loader import (
    build_category_index,
    build_lineage_index,
    build_name_index,
    extract_category_seeds,
    extract_seed_ids,
    load_jaffle_shop_corpus,
)

from trellis.stores.registry import StoreRegistry

_M_CUSTOMERS = "model.jaffle_shop.customers"
_M_STG_CUSTOMERS = "model.jaffle_shop.stg_customers"
_M_STG_ORDERS = "model.jaffle_shop.stg_orders"
_M_STG_PAYMENTS = "model.jaffle_shop.stg_payments"
_S_RAW_CUSTOMERS = "source.jaffle_shop.raw.customers"
_S_RAW_ORDERS = "source.jaffle_shop.raw.orders"
_S_RAW_PAYMENTS = "source.jaffle_shop.raw.payments"

_REQUIRED_UPSTREAM = {
    _M_STG_CUSTOMERS,
    _M_STG_ORDERS,
    _M_STG_PAYMENTS,
    _S_RAW_CUSTOMERS,
    _S_RAW_ORDERS,
    _S_RAW_PAYMENTS,
}


@pytest.fixture
def loaded_registry(tmp_path: Path):
    config = {
        "knowledge": {
            "graph": {"backend": "sqlite"},
            "vector": {"backend": "sqlite"},
            "document": {"backend": "sqlite"},
            "blob": {"backend": "local"},
        },
        "operational": {
            "trace": {"backend": "sqlite"},
            "event_log": {"backend": "sqlite"},
        },
    }
    with StoreRegistry(config=config, stores_dir=tmp_path) as registry:
        load_jaffle_shop_corpus(registry)
        yield registry


def test_category_index_emits_lineage_of_x_phrases(loaded_registry) -> None:
    index = build_category_index(loaded_registry)
    # Every template variant for the customers mart should be present.
    expected_phrases = {
        "upstream of customers",
        "upstream of the customers",
        "lineage of customers",
        "lineage of the customers",
        "ancestors of customers",
        "ancestors of the customers",
        "full upstream lineage of customers",
        "full upstream lineage of the customers",
        "upstream lineage of customers",
        "upstream lineage of the customers",
        # mart-suffixed variants for marts-schema models
        "lineage of the customers mart",
        "full upstream lineage of the customers mart",
        "upstream lineage of the customers mart",
    }
    missing = expected_phrases - set(index)
    assert not missing, f"missing lineage-of-X phrases: {sorted(missing)}"


def test_lineage_of_x_seeds_include_model_and_full_closure(
    loaded_registry,
) -> None:
    index = build_category_index(loaded_registry)
    seeds = set(index["full upstream lineage of the customers"])
    # Model itself + every required upstream entity must be in the seed list.
    assert _M_CUSTOMERS in seeds
    assert _REQUIRED_UPSTREAM.issubset(seeds), (
        f"missing upstream entities: {_REQUIRED_UPSTREAM - seeds}"
    )


def test_extract_category_seeds_resolves_multi_hop_lineage_intent(
    loaded_registry,
) -> None:
    """The exact ground-truth intent should produce the full closure."""
    intent = "What is the full upstream lineage of the customers mart?"
    index = build_category_index(loaded_registry)
    seeds = set(extract_category_seeds(intent, index))
    assert _M_CUSTOMERS in seeds
    assert _REQUIRED_UPSTREAM.issubset(seeds), (
        f"intent did not resolve to required closure; missing: "
        f"{_REQUIRED_UPSTREAM - seeds}"
    )
    # Combined with name-seed extraction the union remains the same shape
    # — the dbt scenario unions both then sets depth=0.
    name_index = build_name_index(loaded_registry)
    name_seeds = set(extract_seed_ids(intent, name_index))
    union = seeds | name_seeds
    assert union >= _REQUIRED_UPSTREAM | {_M_CUSTOMERS}


def test_lineage_of_x_only_emitted_for_models_with_upstream(
    loaded_registry,
) -> None:
    """Sources have no outbound dependsOn → no lineage-of-X phrases.

    Raw sources' ``properties.name`` (e.g., "customers" for raw.customers)
    matches the model's display name, so we want to be sure the absence
    of source-side phrases doesn't shadow the model-side phrases. The
    closure for the customers mart includes the source, so the seeds
    are correct regardless.
    """
    index = build_category_index(loaded_registry)
    # Staging models have closures (they depend on raw sources), so they
    # should also have lineage-of-X phrases.
    assert "lineage of stg_customers" in index
    # Raw payments has no outbound dependsOn → no closure → no
    # "lineage of payments" entry from the source side. Marts/staging
    # may legitimately add it; the assertion is that it does not point
    # to the source itself as a lone seed.
    closure = build_lineage_index(loaded_registry)
    assert _S_RAW_PAYMENTS not in closure  # sources are leaves


def test_pack_build_proxy_covers_multi_hop_lineage_required(
    loaded_registry,
) -> None:
    """End-to-end proxy: build a pack the way the scenario does and
    confirm it covers the multi_hop_lineage required-coverage set.

    This is the unit-level proxy for the full scenario run (which needs
    ``MOONSHOT_API_KEY`` / ``OPENAI_API_KEY`` env vars). It exercises
    the exact ``_build_pack`` flow — name index + lineage expansion +
    category seeds, ``depth=0`` GraphSearch — that the scenario uses,
    proving the new lineage-of-X category fixes the
    ``multi_hop_lineage`` coverage gap. With seeded ``depth=0``,
    KeywordSearch + SemanticSearch are not needed for coverage; we
    therefore use only ``_SeededGraphSearch`` to keep the test
    embedder-free.
    """
    from eval.scenarios._strategies import _SeededGraphSearch
    from trellis.retrieve.pack_builder import PackBuilder
    from trellis.schemas.pack import PackBudget

    intent = "What is the full upstream lineage of the customers mart?"
    name_index = build_name_index(loaded_registry)
    category_index = build_category_index(loaded_registry)

    name_seeds = extract_seed_ids(intent, name_index)
    category_seeds = extract_category_seeds(intent, category_index)
    seed_ids = list(dict.fromkeys(name_seeds + category_seeds))
    # Sanity: the category index must contribute the full closure.
    assert _REQUIRED_UPSTREAM.issubset(set(seed_ids))

    builder = PackBuilder(
        strategies=[_SeededGraphSearch(loaded_registry.knowledge.graph_store)],
        event_log=loaded_registry.operational.event_log,
    )
    pack = builder.build(
        intent=intent,
        domain="data_pipeline",
        budget=PackBudget(max_items=8, max_tokens=4000),
        filters={"seed_ids": seed_ids, "depth": 0},
    )
    # _SeededGraphSearch rewrites graph item_ids to ``doc:<entity_id>``
    # for cross-strategy dedup; the scenario's grader accepts both.
    pack_ids = {item.item_id for item in pack.items}
    covered = sum(
        1
        for eid in _REQUIRED_UPSTREAM
        if eid in pack_ids or f"doc:{eid}" in pack_ids
    )
    coverage = covered / len(_REQUIRED_UPSTREAM)
    assert coverage == 1.0, (
        f"multi_hop_lineage proxy coverage {coverage:.2%}; "
        f"pack item_ids={sorted(pack_ids)}"
    )
