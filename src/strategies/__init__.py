"""Strategy package: base abstractions + concrete strategies.

Importing this package triggers the concrete strategies' registration in
``src.strategies.registry`` so callers can look them up by name.
"""

from src.strategies.base import (
    AccountContext,
    PAPER_DAILY_TRADE_CAP,
    Strategy,
    StrategyConfig,
)
from src.strategies.copy_trade import CopyTradeConfig, CopyTradeStrategy
from src.strategies.directional import DirectionalConfig, DirectionalStrategy

__all__ = [
    "AccountContext",
    "PAPER_DAILY_TRADE_CAP",
    "Strategy",
    "StrategyConfig",
    "DirectionalStrategy",
    "DirectionalConfig",
    "CopyTradeStrategy",
    "CopyTradeConfig",
]
