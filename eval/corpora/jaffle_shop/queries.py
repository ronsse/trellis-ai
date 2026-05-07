"""Ground-truth queries for the Jaffle Shop Phase B-1 scenario.

Each query targets a specific retrieval skill — column-level
transformation lookup, multi-hop lineage tracing, change-impact
analysis, or test cross-referencing. ``required_coverage`` lists every
``entity_id`` (from ``manifest.json``) that *must* be in the pack for
the round to count as a success at threshold 0.6.

Difficulty tiers (informational only — the scenario doesn't read this):

- **easy**: 1-2 required, distractors are obviously different content.
  Keyword search alone usually solves it.
- **medium**: 2-4 required, semantic understanding helps disambiguate.
  Keyword surfaces some right + some wrong; semantic helps rank.
- **hard**: 4-6 required, multi-hop graph traversal or column-level
  reasoning. KeywordSearch alone fails; success requires either
  GraphSearch traversal or strong semantic embeddings + advisory
  reinforcement over rounds.

Every required entity appears in
``eval/corpora/jaffle_shop/manifest.json``. If you change the manifest,
update these query coverage lists or queries will silently degrade.
"""

from __future__ import annotations

from dataclasses import dataclass

# Domain tag — single-domain corpus until B-2 lands.
DBT_DOMAIN = "data_pipeline"


@dataclass(frozen=True)
class JaffleShopQuery:
    """A single ground-truth retrieval question against the corpus."""

    intent: str
    required_coverage: list[str]
    difficulty: str  # "easy" | "medium" | "hard" — informational
    skill: str  # short tag for per-skill metric aggregation
    rationale: str  # why this is the ground truth


# Models / sources / tests use their dbt unique_id as entity_id. These
# strings are duplicated rather than computed from the manifest so the
# coverage assertions are explicit and grep-friendly.
_M_STG_CUSTOMERS = "model.jaffle_shop.stg_customers"
_M_STG_ORDERS = "model.jaffle_shop.stg_orders"
_M_STG_PAYMENTS = "model.jaffle_shop.stg_payments"
_M_CUSTOMERS = "model.jaffle_shop.customers"
_M_ORDERS = "model.jaffle_shop.orders"
_S_RAW_CUSTOMERS = "source.jaffle_shop.raw.customers"
_S_RAW_ORDERS = "source.jaffle_shop.raw.orders"
_S_RAW_PAYMENTS = "source.jaffle_shop.raw.payments"
_T_UNIQUE_STG_CUSTOMERS_ID = (
    "test.jaffle_shop.unique_stg_customers_customer_id.c7614daacb"
)
_T_NOT_NULL_STG_CUSTOMERS_ID = (
    "test.jaffle_shop.not_null_stg_customers_customer_id.e2cfb1f9aa"
)
_T_UNIQUE_STG_ORDERS_ID = "test.jaffle_shop.unique_stg_orders_order_id.e3b0c44298"
_T_NOT_NULL_STG_ORDERS_ID = (
    "test.jaffle_shop.not_null_stg_orders_order_id.81cfe2fe64"
)
_T_ACCEPTED_VALUES_STG_ORDERS_STATUS = (
    "test.jaffle_shop.accepted_values_stg_orders_status.080fb20aad"
)
_T_UNIQUE_STG_PAYMENTS_ID = (
    "test.jaffle_shop.unique_stg_payments_payment_id.3744510712"
)
_T_NOT_NULL_STG_PAYMENTS_ID = (
    "test.jaffle_shop.not_null_stg_payments_payment_id.c19cc50095"
)
_T_ACCEPTED_VALUES_STG_PAYMENTS_METHOD = (
    "test.jaffle_shop.accepted_values_stg_payments_payment_method.e1bdf5d472"
)
_T_UNIQUE_CUSTOMERS_ID = "test.jaffle_shop.unique_customers_customer_id.c5af1ff4f7"
_T_NOT_NULL_CUSTOMERS_ID = (
    "test.jaffle_shop.not_null_customers_customer_id.3b4e0ddfc6"
)
_T_UNIQUE_ORDERS_ID = "test.jaffle_shop.unique_orders_order_id.fed79b3a6e"
_T_NOT_NULL_ORDERS_ID = "test.jaffle_shop.not_null_orders_order_id.cf6c17daed"
_T_RELATIONSHIPS_ORDERS_CUSTOMER = (
    "test.jaffle_shop.relationships_orders_customer_id__customer_id__ref_customers_.c6ec7f58f2"
)


GROUND_TRUTH_QUERIES: list[JaffleShopQuery] = [
    # -----------------------------------------------------------------
    # Column-level transformations — distinguish the model that does the
    # transform from the one that just contains the column name.
    # -----------------------------------------------------------------
    JaffleShopQuery(
        intent=(
            "Which model converts payment amounts from cents to dollars?"
        ),
        required_coverage=[_M_STG_PAYMENTS],
        difficulty="medium",
        skill="column_transformation",
        rationale=(
            "stg_payments is the only model that performs the cents→dollars "
            "conversion. raw.payments mentions 'amount in cents' (the source "
            "side) and orders mart references 'amount' fields (downstream "
            "aggregates), but neither does the conversion. The keyword "
            "'amount' appears in both distractors; only semantic / content "
            "understanding lands on stg_payments."
        ),
    ),
    JaffleShopQuery(
        intent=(
            "Where is the paymentmethod field renamed to payment_method?"
        ),
        required_coverage=[_M_STG_PAYMENTS],
        difficulty="medium",
        skill="column_transformation",
        rationale=(
            "raw.payments has the original 'paymentmethod' (no underscore); "
            "stg_payments does the rename. The distractor here is that "
            "raw.payments will keyword-match 'paymentmethod' strongly, but "
            "the rename happens in staging."
        ),
    ),
    JaffleShopQuery(
        intent=(
            "Which model materializes the customer_lifetime_value column?"
        ),
        required_coverage=[_M_CUSTOMERS],
        difficulty="medium",
        skill="column_transformation",
        rationale=(
            "Only the customers mart computes customer_lifetime_value (per "
            "its description). orders mart aggregates payment-method totals "
            "but not the per-customer lifetime value. stg_payments has "
            "amount but at payment grain, not customer grain."
        ),
    ),
    JaffleShopQuery(
        intent=(
            "What model breaks out order amounts by payment-method type "
            "(credit_card_amount, coupon_amount, bank_transfer_amount, "
            "gift_card_amount)?"
        ),
        required_coverage=[_M_ORDERS],
        difficulty="medium",
        skill="column_transformation",
        rationale=(
            "The orders mart is the only model that produces these "
            "per-order pivoted columns. stg_payments has the raw payment "
            "rows pre-pivot; customers mart aggregates differently."
        ),
    ),
    JaffleShopQuery(
        intent=(
            "What model produces first_order and most_recent_order "
            "timestamps per customer?"
        ),
        required_coverage=[_M_CUSTOMERS],
        difficulty="medium",
        skill="column_transformation",
        rationale=(
            "Customers mart only — the description explicitly lists these "
            "two columns. stg_orders has order_date but no aggregate; the "
            "aggregate happens in the customers mart."
        ),
    ),
    # -----------------------------------------------------------------
    # Lineage — multi-hop graph traversal. KeywordSearch struggles here
    # because the answers are about *structure*, not content overlap.
    # -----------------------------------------------------------------
    JaffleShopQuery(
        intent="What is the full upstream lineage of the customers mart?",
        required_coverage=[
            _M_STG_CUSTOMERS,
            _M_STG_ORDERS,
            _M_STG_PAYMENTS,
            _S_RAW_CUSTOMERS,
            _S_RAW_ORDERS,
            _S_RAW_PAYMENTS,
        ],
        difficulty="hard",
        skill="multi_hop_lineage",
        rationale=(
            "Two-hop traversal: customers depends on 3 staging models, each "
            "of which depends on a distinct raw source. Six required "
            "entities; pack budget is 8, so coverage is achievable but only "
            "if GraphSearch contributes."
        ),
    ),
    JaffleShopQuery(
        intent="What downstream dbt models consume raw.customers?",
        required_coverage=[_M_STG_CUSTOMERS, _M_CUSTOMERS],
        difficulty="hard",
        skill="downstream_lineage",
        rationale=(
            "raw.customers → stg_customers (direct) → customers mart "
            "(transitive). Requires incoming-edge traversal and 2-hop "
            "follow. KeywordSearch will surface raw.customers itself "
            "(matches 'customers'), which is the wrong answer."
        ),
    ),
    JaffleShopQuery(
        intent=(
            "If I change stg_orders, which models and tests are at risk?"
        ),
        required_coverage=[
            _M_CUSTOMERS,
            _M_ORDERS,
            _T_UNIQUE_STG_ORDERS_ID,
            _T_NOT_NULL_STG_ORDERS_ID,
            _T_ACCEPTED_VALUES_STG_ORDERS_STATUS,
        ],
        difficulty="hard",
        skill="change_impact",
        rationale=(
            "Change-impact analysis: 2 mart models depend on stg_orders + "
            "3 schema tests cover stg_orders. Five required, pack budget "
            "8 — only 60% needed (3/5) for success but fuller coverage is "
            "the real win. Keyword retrieval will surface lots of "
            "'orders'-named items; the right answer requires distinguishing "
            "dependents from siblings."
        ),
    ),
    JaffleShopQuery(
        intent=(
            "Trace the path from raw.payments to the customer_lifetime_value "
            "column."
        ),
        required_coverage=[_S_RAW_PAYMENTS, _M_STG_PAYMENTS, _M_CUSTOMERS],
        difficulty="hard",
        skill="end_to_end_lineage",
        rationale=(
            "Cross-cutting query — combines lineage with column-level "
            "reasoning. raw.payments → stg_payments (cents to dollars) → "
            "customers (computes lifetime value from amounts). Three steps; "
            "all three required for full coverage."
        ),
    ),
    # -----------------------------------------------------------------
    # Test cross-referencing — distinguishing test types and targets.
    # -----------------------------------------------------------------
    JaffleShopQuery(
        intent=(
            "Which test enforces the relationship between orders and "
            "customers (foreign-key style)?"
        ),
        required_coverage=[_T_RELATIONSHIPS_ORDERS_CUSTOMER],
        difficulty="medium",
        skill="test_cross_reference",
        rationale=(
            "Only one test in the corpus is a 'relationships' test type. "
            "Distractors: every model description mentions 'customer' or "
            "'orders'. The right answer requires recognizing that "
            "'relationship between A and B' maps to dbt's relationships "
            "test type, not a content match."
        ),
    ),
    JaffleShopQuery(
        intent=(
            "What checks ensure no null order_id values exist anywhere in "
            "the orders pipeline?"
        ),
        required_coverage=[_T_NOT_NULL_STG_ORDERS_ID, _T_NOT_NULL_ORDERS_ID],
        difficulty="medium",
        skill="test_coverage",
        rationale=(
            "Two not-null tests on order_id — one at the staging layer, "
            "one at the mart layer. Full coverage requires both. "
            "Distractors: not_null tests on customer_id and payment_id."
        ),
    ),
    # -----------------------------------------------------------------
    # Layer-aware reasoning — using dbt's conventional layering.
    # -----------------------------------------------------------------
    JaffleShopQuery(
        intent=(
            "Show me all the mart-layer models — the ones that produce "
            "one row per business entity for analytics consumption."
        ),
        required_coverage=[_M_CUSTOMERS, _M_ORDERS],
        difficulty="medium",
        skill="layer_classification",
        rationale=(
            "The two mart models (customers, orders). Distractors: 3 "
            "staging models that have similar names but live at the "
            "staging layer (per their schema='staging' property and tags="
            "['staging'])."
        ),
    ),
]


# Sanity check: every coverage entry should reference a real entity in
# manifest.json. The scenario could enforce this at startup; for now the
# fixture maintenance is on the queries-author.
__all__ = ["JaffleShopQuery", "GROUND_TRUTH_QUERIES", "DBT_DOMAIN"]
