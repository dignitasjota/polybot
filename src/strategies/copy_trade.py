"""CopyTradeStrategy: Strategy ABC adapter over CopyTrader.

Composes the existing CopyTrader instance and exposes it through the
Strategy ABC interface. The legacy module (src/copy_trader.py) is left
untouched so the running bot keeps working until Fase 8 wires this
adapter into the AccountRunner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from src.copy_trader import CopyTrader
from src.strategies.base import AccountContext, Strategy, StrategyConfig
from src.strategies.registry import register_strategy

logger = structlog.get_logger("polymarket.strategies.copy_trade")


@dataclass
class CopyTradeConfig(StrategyConfig):
    """Configuration for the copy-trading strategy.

    Inherits the common StrategyConfig fields and adds copy-specific ones.
    Translated from the legacy ``src.config.CopyTradeConfig`` via
    ``from_legacy()`` in Fase 8.
    """
    target_wallets: list[str] = field(default_factory=list)
    poll_interval_ms: int = 500
    max_latency_ms: int = 120000
    copy_size_mode: str = "fixed"
    fixed_bet_size: float = 5.0
    proportional_factor: float = 1.0
    min_price: float = 0.50
    spread_arb_multiplier: float = 3.0

    @classmethod
    def from_legacy(cls, legacy: Any, mode: str = "disabled") -> CopyTradeConfig:
        return cls(
            mode=mode,
            max_concurrent_bets=getattr(legacy, "max_concurrent_bets", 3),
            target_wallets=list(getattr(legacy, "target_wallets", []) or []),
            poll_interval_ms=getattr(legacy, "poll_interval_ms", 500),
            max_latency_ms=getattr(legacy, "max_latency_ms", 120000),
            copy_size_mode=getattr(legacy, "copy_size_mode", "fixed"),
            fixed_bet_size=getattr(legacy, "fixed_bet_size", 5.0),
            proportional_factor=getattr(legacy, "proportional_factor", 1.0),
            min_price=getattr(legacy, "min_price", 0.50),
            spread_arb_multiplier=getattr(legacy, "spread_arb_multiplier", 3.0),
        )


class CopyTradeStrategy(Strategy):
    """Strategy ABC wrapper around an existing CopyTrader."""

    def __init__(
        self,
        config: CopyTradeConfig,
        context: AccountContext,
        copy_trader: CopyTrader,
    ):
        super().__init__(config, context)
        self.config: CopyTradeConfig = config
        self._copy_trader = copy_trader

    @property
    def copy_trader(self) -> CopyTrader:
        return self._copy_trader

    async def start(self) -> None:
        try:
            await self._copy_trader.start()
        except Exception as e:
            logger.warning("copy_trade_start_error", error=str(e))

    async def stop(self) -> None:
        try:
            await self._copy_trader.close()
        except Exception as e:
            logger.warning("copy_trade_stop_error", error=str(e))

    def get_stats(self) -> dict[str, Any]:
        stats = self._copy_trader.get_stats() if self._copy_trader else {}
        for k in ("balance", "starting_balance", "total_pnl", "daily_pnl",
                  "wins", "losses", "win_rate"):
            stats.pop(k, None)
        return stats

    def get_config(self) -> dict[str, Any]:
        return {
            "mode": self.config.mode,
            "priority": self.config.priority,
            "max_concurrent_bets": self.config.max_concurrent_bets,
            "max_bet_per_trade": self.config.max_bet_per_trade,
            "paper_daily_trade_cap": self.config.paper_daily_trade_cap,
            "target_wallets": list(self.config.target_wallets),
            "poll_interval_ms": self.config.poll_interval_ms,
            "max_latency_ms": self.config.max_latency_ms,
            "fixed_bet_size": self.config.fixed_bet_size,
            "min_price": self.config.min_price,
            "spread_arb_multiplier": self.config.spread_arb_multiplier,
        }

    async def restore_open_positions(self, positions: list[Any]) -> None:
        """Restore in-memory bet tracking from persisted trades after redeploy."""
        if not self._copy_trader:
            return
        restored = 0
        # CopyTrader keeps its in-flight set in _bets (dict) — guard against
        # the attribute not existing or having a different shape.
        bets_attr = getattr(self._copy_trader, "_bets", None)
        for trade in positions:
            if getattr(trade, "source_strategy", None) != "copy_trade":
                continue
            condition_id = getattr(trade, "condition_id", None)
            token_side = getattr(trade, "token_side", None)
            if not condition_id or not token_side:
                continue
            key = f"{condition_id}:{token_side}"
            if isinstance(bets_attr, dict) and key not in bets_attr:
                bets_attr[key] = {
                    "condition_id": condition_id,
                    "token_side": token_side,
                    "token_id": getattr(trade, "token_id", "") or "",
                    "price": getattr(trade, "price", 0.0) or 0.0,
                    "size": getattr(trade, "size", 0.0) or 0.0,
                    "created_at": getattr(trade, "created_at", 0.0),
                    "restored": True,
                }
                restored += 1
        if restored:
            logger.info("copy_trade_restored_positions", count=restored)


# Registrar en el registry global
register_strategy("copy_trade", CopyTradeStrategy, CopyTradeConfig)
