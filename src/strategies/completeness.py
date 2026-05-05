"""Completeness Arbitrage strategy — risk-free profit from pricing gaps.

Wraps CompletenessScanner in the Strategy ABC for integration with
AccountRunner. Shares MarketTracker and WebSocket infrastructure with
the directional strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from src.completeness_scanner import CompletenessScanner
from src.strategies.base import AccountContext, Strategy, StrategyConfig
from src.strategies.registry import register_strategy

logger = structlog.get_logger("polymarket.strategies.completeness")


@dataclass
class CompletenessConfig(StrategyConfig):
    """Configuration for completeness arbitrage strategy."""

    # Scan settings
    scan_interval: float = 5.0            # Seconds between periodic scans (fast — gaps are fleeting)
    min_profit_per_share: float = 0.005   # Min $0.005 net profit per share to trigger ($0.50 per 100 shares)
    min_shares: float = 5.0               # Min shares to be worth executing
    max_cost_per_trade: float = 50.0      # Max $ to spend per arb trade
    cooldown_s: float = 30.0              # Seconds to wait before re-trying same market
    category: str = "crypto"              # Fee category (affects threshold)

    @classmethod
    def from_dict(cls, raw: dict, mode: str = "disabled") -> CompletenessConfig:
        """Build config from TOML dict."""
        return cls(
            mode=raw.get("mode", mode),
            scan_interval=float(raw.get("scan_interval", 5.0)),
            min_profit_per_share=float(raw.get("min_profit_per_share", 0.005)),
            min_shares=float(raw.get("min_shares", 5.0)),
            max_cost_per_trade=float(raw.get("max_cost_per_trade", 50.0)),
            cooldown_s=float(raw.get("cooldown_s", 30.0)),
            category=raw.get("category", "crypto"),
            max_concurrent_bets=int(raw.get("max_concurrent_bets", 10)),
            max_bet_per_trade=float(raw.get("max_bet_per_trade", 50.0)),
        )


class CompletenessStrategy(Strategy):
    """Completeness arbitrage: buy all outcomes when sum < $1.00."""

    def __init__(
        self,
        config: CompletenessConfig,
        context: AccountContext,
        tracker=None,
        credentials=None,
    ):
        super().__init__(config, context)
        self._scanner = CompletenessScanner(
            config=config,
            tracker=tracker,
            credentials=credentials,
        )

        # Wire redeem callback from executor
        executor = context._executor
        if hasattr(executor, "redeem_position"):
            self._scanner.set_redeem_callback(executor.redeem_position)

    @property
    def scanner(self) -> CompletenessScanner:
        return self._scanner

    async def start(self) -> None:
        if self.config.mode == "disabled":
            return
        await self._scanner.start()
        logger.info("completeness_strategy_started", mode=self.config.mode)

    async def stop(self) -> None:
        await self._scanner.stop()
        logger.info("completeness_strategy_stopped")

    def get_stats(self) -> dict:
        return self._scanner.get_stats()

    def export_full_report(self) -> dict:
        stats = self._scanner.get_stats()
        return {
            "strategy": "completeness",
            "mode": self.config.mode,
            "config": self.get_config(),
            "stats": stats,
            "recent_trades": stats.get("recent_trades", []),
        }

    def get_config(self) -> dict:
        cfg = self.config
        return {
            "mode": cfg.mode,
            "scan_interval": cfg.scan_interval,
            "min_profit_per_share": cfg.min_profit_per_share,
            "min_shares": cfg.min_shares,
            "max_cost_per_trade": cfg.max_cost_per_trade,
            "cooldown_s": cfg.cooldown_s,
            "category": cfg.category,
        }

    async def set_mode(self, new_mode: str) -> None:
        old_mode = self.config.mode
        if new_mode != old_mode:
            # Reset stats and trades when switching mode so each mode starts clean
            self._scanner.reset_stats()
        await super().set_mode(new_mode)

        # If transitioning to live, init ClobClient
        if new_mode in ("dry_run", "live") and not self._scanner._initialized:
            await self._scanner._init_clob_client()

    async def restore_open_positions(self, positions: list[Any]) -> None:
        pass


register_strategy("completeness", CompletenessStrategy, CompletenessConfig)
