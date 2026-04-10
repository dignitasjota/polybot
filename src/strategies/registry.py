"""Strategy registry: name → (Strategy class, Config class).

Concrete strategies register themselves here so the AccountRunner can
instantiate them dynamically based on the TOML config.
"""

from __future__ import annotations

from typing import Type

from src.strategies.base import Strategy, StrategyConfig

# Populated by concrete strategy modules at import time (Fase 4).
STRATEGIES: dict[str, tuple[Type[Strategy], Type[StrategyConfig]]] = {}


def register_strategy(
    name: str,
    strategy_cls: Type[Strategy],
    config_cls: Type[StrategyConfig],
) -> None:
    """Register a strategy and its config class under a name."""
    STRATEGIES[name] = (strategy_cls, config_cls)


def get_strategy_class(name: str) -> Type[Strategy] | None:
    entry = STRATEGIES.get(name)
    return entry[0] if entry else None


def get_config_class(name: str) -> Type[StrategyConfig] | None:
    entry = STRATEGIES.get(name)
    return entry[1] if entry else None


def list_strategies() -> list[str]:
    """Return registered strategy names."""
    return sorted(STRATEGIES.keys())
