"""Liquidity strategy — Phase 1: Reward Scanner only.

Future phases will add LiquidityProvider (market making) with order
placement, inventory tracking, and adverse selection monitoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from src.liquidity_metrics import LiquidityMetrics
from src.liquidity_provider import LiquidityProvider
from src.reward_scanner import RewardScanner
from src.strategies.base import AccountContext, Strategy, StrategyConfig
from src.strategies.registry import register_strategy

logger = structlog.get_logger("polymarket.strategies.liquidity")


@dataclass
class LiquidityConfig(StrategyConfig):
    """Configuration for liquidity/market-making strategy."""

    # Scanner settings
    scan_interval: float = 300.0          # Seconds between reward scans
    min_daily_rate: float = 1.0           # Min $/day to consider a market
    min_reward_per_dollar: float = 0.5    # Min reward/$ (after competition)

    # Capital allocation (Phase 2+)
    capital_per_market: float = 50.0      # USDC per market
    max_markets: int = 5                  # Max markets to quote simultaneously
    total_capital: float = 250.0          # Verification cap

    # Filtering
    max_min_size: float = 0.0             # Only markets with min_size <= this (0 = no filter)

    # Quoting (Phase 2+)
    spread_pct_of_max: float = 0.20       # Quote at 20% of max_spread
    use_order_block_hiding: bool = True
    min_block_usd: float = 500.0

    # Inventory (Phase 3+)
    max_inventory_skew: float = 0.6
    max_adverse_ratio: float = 0.70
    emergency_move_pct: float = 0.05

    # Heartbeat & scoring
    use_heartbeat: bool = False          # POST /heartbeat every 5s (miss 10s → all cancelled)
    heartbeat_interval: float = 5.0     # Seconds between heartbeats
    scoring_check_interval: float = 60.0  # Seconds between order-scoring checks
    quote_refresh_s: float = 30.0       # Seconds between quote refresh cycles

    @classmethod
    def from_dict(cls, raw: dict, mode: str = "disabled") -> LiquidityConfig:
        """Build config from TOML dict."""
        return cls(
            mode=raw.get("mode", mode),
            scan_interval=float(raw.get("scan_interval", 300)),
            min_daily_rate=float(raw.get("min_daily_rate", 1.0)),
            min_reward_per_dollar=float(raw.get("min_reward_per_dollar", 0.0005)),
            capital_per_market=float(raw.get("capital_per_market", 50.0)),
            max_markets=int(raw.get("max_markets", 5)),
            total_capital=float(raw.get("total_capital", 250.0)),
            max_min_size=float(raw.get("max_min_size", 0.0)),
            spread_pct_of_max=float(raw.get("spread_pct_of_max", 0.20)),
            use_order_block_hiding=raw.get("use_order_block_hiding", True),
            min_block_usd=float(raw.get("min_block_usd", 500.0)),
            max_inventory_skew=float(raw.get("max_inventory_skew", 0.6)),
            max_adverse_ratio=float(raw.get("max_adverse_ratio", 0.70)),
            emergency_move_pct=float(raw.get("emergency_move_pct", 0.05)),
            use_heartbeat=raw.get("use_heartbeat", False),
            heartbeat_interval=float(raw.get("heartbeat_interval", 5.0)),
            scoring_check_interval=float(raw.get("scoring_check_interval", 60.0)),
            quote_refresh_s=float(raw.get("quote_refresh_s", 30.0)),
            max_concurrent_bets=int(raw.get("max_concurrent_bets", 5)),
            max_bet_per_trade=float(raw.get("max_bet_per_trade", 50.0)),
        )


class LiquidityStrategy(Strategy):
    """Phase 1: Scan and rank reward markets (read-only, no trading).

    Future phases will add:
    - Phase 2: LiquidityProvider with GTC order placement
    - Phase 3: Inventory tracking and rebalancing
    - Phase 4: Full integration with executor
    """

    def __init__(
        self,
        config: LiquidityConfig,
        context: AccountContext,
        scanner: RewardScanner | None = None,
        credentials=None,
        tracker=None,
    ):
        super().__init__(config, context)
        self._scanner = scanner or RewardScanner(
            scan_interval=config.scan_interval,
            min_daily_rate=config.min_daily_rate,
            min_reward_per_dollar=config.min_reward_per_dollar,
            capital_per_market=config.capital_per_market,
            max_min_size=config.max_min_size,
        )
        self._metrics = LiquidityMetrics(total_capital=config.total_capital)
        self._provider = LiquidityProvider(
            config=config,
            credentials=credentials,
            tracker=tracker,
            metrics=self._metrics,
        )
        self._provider.set_scanner(self._scanner)

        # Wire auto-redeem: when matched pairs are detected, use executor's redeem
        executor = context._executor
        if hasattr(executor, 'redeem_position'):
            self._provider.set_redeem_callback(executor.redeem_position)

    @property
    def scanner(self) -> RewardScanner:
        return self._scanner

    @property
    def provider(self) -> LiquidityProvider:
        return self._provider

    @property
    def metrics(self) -> LiquidityMetrics:
        return self._metrics

    async def start(self) -> None:
        if self.config.mode == "disabled":
            return
        await self._scanner.start()
        await self._provider.start()
        logger.info("liquidity_strategy_started", mode=self.config.mode)

    async def stop(self) -> None:
        await self._provider.stop()
        await self._scanner.close()
        logger.info("liquidity_strategy_stopped")

    def get_stats(self) -> dict:
        stats = self._scanner.get_stats()
        stats["provider"] = self._provider.get_stats()
        stats["metrics_today"] = self._metrics.get_today()
        stats["metrics_summary"] = self._metrics.get_summary(days=7)
        return stats

    def get_config(self) -> dict:
        cfg = self.config
        return {
            "mode": cfg.mode,
            "scan_interval": cfg.scan_interval,
            "min_daily_rate": cfg.min_daily_rate,
            "min_reward_per_dollar": cfg.min_reward_per_dollar,
            "capital_per_market": cfg.capital_per_market,
            "max_markets": cfg.max_markets,
            "spread_pct_of_max": cfg.spread_pct_of_max,
            "max_inventory_skew": cfg.max_inventory_skew,
            "max_adverse_ratio": cfg.max_adverse_ratio,
        }

    async def set_mode(self, new_mode: str) -> None:
        old_mode = self.config.mode
        await super().set_mode(new_mode)

        # If transitioning from paper to dry_run/live, initialize ClobClient
        if old_mode == "paper" and new_mode in ("dry_run", "live"):
            if not self._provider._initialized:
                await self._provider._init_clob_client()
                logger.info("provider_clob_initialized", mode=new_mode)

    async def restore_open_positions(self, positions: list[Any]) -> None:
        # Phase 1: no positions to restore (scanner only)
        pass

    def export_full_report(self) -> dict:
        """Export complete liquidity strategy state (for API endpoints)."""
        return {
            "strategy": "liquidity",
            "mode": self.config.mode,
            "scanner": self._scanner.get_stats(),
            "provider": self._provider.get_stats(),
            "metrics_today": self._metrics.get_today(),
            "metrics_summary": self._metrics.get_summary(days=7),
            "config": self.get_config(),
        }


register_strategy("liquidity", LiquidityStrategy, LiquidityConfig)
