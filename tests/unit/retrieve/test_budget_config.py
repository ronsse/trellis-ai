"""Tests for BudgetConfig resolution hierarchy."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.retrieve.budget_config import (
    BudgetConfig,
    BudgetSpec,
    DomainBudgetSpec,
)
from trellis.stores.registry import StoreRegistry


class TestFromDict:
    def test_none_returns_defaults(self) -> None:
        cfg = BudgetConfig.from_dict(None)
        budget = cfg.resolve(tool="any_tool")
        assert budget.max_tokens == 4000
        assert budget.max_items == 30

    def test_empty_returns_defaults(self) -> None:
        cfg = BudgetConfig.from_dict({})
        budget = cfg.resolve(tool="any_tool")
        assert budget.max_tokens == 4000
        assert budget.max_items == 30

    def test_malformed_falls_back(self) -> None:
        # ``default`` is expected to be a dict; pass a string to force failure
        cfg = BudgetConfig.from_dict({"default": "not-a-dict"})
        budget = cfg.resolve(tool="any_tool")
        assert budget.max_tokens == 4000
        assert budget.max_items == 30

    def test_loads_default_section(self) -> None:
        cfg = BudgetConfig.from_dict({"default": {"max_tokens": 2500, "max_items": 15}})
        budget = cfg.resolve(tool="unknown")
        assert budget.max_tokens == 2500
        assert budget.max_items == 15

    def test_loads_by_tool_section(self) -> None:
        cfg = BudgetConfig.from_dict(
            {
                "default": {"max_tokens": 2000},
                "by_tool": {
                    "get_objective_context": {"max_tokens": 3500, "max_items": 20},
                },
            }
        )
        budget = cfg.resolve(tool="get_objective_context")
        assert budget.max_tokens == 3500
        assert budget.max_items == 20

    def test_loads_by_domain_section(self) -> None:
        cfg = BudgetConfig.from_dict(
            {
                "by_domain": {
                    "sportsbook": {"max_tokens_multiplier": 1.5},
                },
            }
        )
        budget = cfg.resolve(tool="any", domain="sportsbook")
        assert budget.max_tokens == 6000  # 4000 * 1.5
        assert budget.max_items == 30  # default, multiplier = 1.0


class TestResolutionHierarchy:
    def test_tool_override_wins_over_default(self) -> None:
        cfg = BudgetConfig(
            default=BudgetSpec(max_tokens=4000, max_items=30),
            by_tool={
                "get_task_context": BudgetSpec(max_tokens=2500, max_items=15),
            },
        )
        budget = cfg.resolve(tool="get_task_context")
        assert budget.max_tokens == 2500
        assert budget.max_items == 15

    def test_unknown_tool_uses_default(self) -> None:
        cfg = BudgetConfig(
            default=BudgetSpec(max_tokens=3000, max_items=20),
            by_tool={
                "get_task_context": BudgetSpec(max_tokens=1000, max_items=5),
            },
        )
        budget = cfg.resolve(tool="get_objective_context")
        assert budget.max_tokens == 3000
        assert budget.max_items == 20

    def test_domain_multiplier_applies_to_tool(self) -> None:
        cfg = BudgetConfig(
            by_tool={
                "get_task_context": BudgetSpec(max_tokens=2000, max_items=10),
            },
            by_domain={
                "sportsbook": DomainBudgetSpec(
                    max_tokens_multiplier=1.25, max_items_multiplier=2.0
                ),
            },
        )
        budget = cfg.resolve(tool="get_task_context", domain="sportsbook")
        assert budget.max_tokens == 2500  # 2000 * 1.25
        assert budget.max_items == 20  # 10 * 2.0

    def test_domain_multiplier_applies_to_default(self) -> None:
        cfg = BudgetConfig(
            default=BudgetSpec(max_tokens=4000, max_items=30),
            by_domain={
                "big-domain": DomainBudgetSpec(max_tokens_multiplier=2.0),
            },
        )
        budget = cfg.resolve(tool="unknown-tool", domain="big-domain")
        assert budget.max_tokens == 8000

    def test_unknown_domain_is_noop(self) -> None:
        cfg = BudgetConfig(
            default=BudgetSpec(max_tokens=4000, max_items=30),
            by_domain={
                "known": DomainBudgetSpec(max_tokens_multiplier=0.5),
            },
        )
        budget = cfg.resolve(tool="any", domain="unknown")
        assert budget.max_tokens == 4000

    def test_caller_override_wins_over_tool(self) -> None:
        cfg = BudgetConfig(
            by_tool={
                "get_task_context": BudgetSpec(max_tokens=2000, max_items=10),
            },
        )
        budget = cfg.resolve(
            tool="get_task_context",
            caller_override_tokens=5000,
            caller_override_items=50,
        )
        assert budget.max_tokens == 5000
        assert budget.max_items == 50

    def test_caller_override_wins_over_domain_multiplier(self) -> None:
        cfg = BudgetConfig(
            default=BudgetSpec(max_tokens=4000, max_items=30),
            by_domain={
                "boost": DomainBudgetSpec(max_tokens_multiplier=2.0),
            },
        )
        budget = cfg.resolve(
            tool="any",
            domain="boost",
            caller_override_tokens=1000,
        )
        # Caller override ignores domain multiplier
        assert budget.max_tokens == 1000

    def test_zero_override_is_ignored(self) -> None:
        # ``max_tokens=0`` is the MCP sentinel meaning "use config"
        cfg = BudgetConfig(default=BudgetSpec(max_tokens=4000, max_items=30))
        budget = cfg.resolve(
            tool="any",
            caller_override_tokens=0,
            caller_override_items=0,
        )
        assert budget.max_tokens == 4000
        assert budget.max_items == 30

    def test_negative_override_is_ignored(self) -> None:
        cfg = BudgetConfig(default=BudgetSpec(max_tokens=4000, max_items=30))
        budget = cfg.resolve(
            tool="any",
            caller_override_tokens=-1,
            caller_override_items=-5,
        )
        assert budget.max_tokens == 4000
        assert budget.max_items == 30

    def test_partial_caller_override(self) -> None:
        cfg = BudgetConfig(default=BudgetSpec(max_tokens=4000, max_items=30))
        budget = cfg.resolve(
            tool="any",
            caller_override_tokens=1500,
            # items not overridden → falls back to config
        )
        assert budget.max_tokens == 1500
        assert budget.max_items == 30


class TestRegistryIntegration:
    def test_registry_exposes_default_budget_config(self, tmp_path: Path) -> None:
        registry = StoreRegistry(stores_dir=tmp_path)
        budget = registry.budget_config.resolve(tool="anything")
        assert budget.max_tokens == 4000
        assert budget.max_items == 30

    def test_registry_loads_budgets_from_retrieval_config(self, tmp_path: Path) -> None:
        registry = StoreRegistry(
            stores_dir=tmp_path,
            retrieval_config={
                "budgets": {
                    "default": {"max_tokens": 3000, "max_items": 20},
                    "by_tool": {
                        "get_objective_context": {"max_tokens": 5500},
                    },
                }
            },
        )
        default_budget = registry.budget_config.resolve(tool="unknown")
        assert default_budget.max_tokens == 3000
        assert default_budget.max_items == 20

        tool_budget = registry.budget_config.resolve(tool="get_objective_context")
        assert tool_budget.max_tokens == 5500
        # by_tool is a complete BudgetSpec — unspecified max_items falls
        # back to the BudgetSpec default (not the global default section)
        assert tool_budget.max_items == 30

    def test_registry_from_config_dir_loads_retrieval_section(
        self, tmp_path: Path
    ) -> None:
        yaml = pytest.importorskip("yaml")

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        (config_dir / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "retrieval": {
                        "budgets": {
                            "default": {"max_tokens": 2500},
                            "by_tool": {
                                "get_task_context": {"max_tokens": 2000},
                            },
                            "by_domain": {
                                "sportsbook": {"max_tokens_multiplier": 1.5},
                            },
                        }
                    }
                }
            )
        )

        registry = StoreRegistry.from_config_dir(
            config_dir=config_dir, data_dir=data_dir
        )
        tool_budget = registry.budget_config.resolve(tool="get_task_context")
        assert tool_budget.max_tokens == 2000

        domain_budget = registry.budget_config.resolve(
            tool="get_task_context", domain="sportsbook"
        )
        assert domain_budget.max_tokens == 3000  # 2000 * 1.5

    def test_budget_config_is_cached(self, tmp_path: Path) -> None:
        registry = StoreRegistry(stores_dir=tmp_path)
        first = registry.budget_config
        second = registry.budget_config
        assert first is second
