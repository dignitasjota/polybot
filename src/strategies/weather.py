"""Weather Prediction strategy — temperature forecast arbitrage.

Uses Open-Meteo ensemble API (51 ECMWF IFS models) to predict daily
high temperatures, then bets on mispriced outcomes in Polymarket
temperature markets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from src.strategies.base import AccountContext, Strategy, StrategyConfig
from src.strategies.registry import register_strategy
from src.weather_scanner import WeatherScanner

logger = structlog.get_logger("polymarket.strategies.weather")


@dataclass
class WeatherConfig(StrategyConfig):
    """Configuration for weather prediction strategy."""

    # Scan settings
    scan_interval: float = 900.0       # Seconds between scans (15 min — forecasts don't change fast)
    forecast_cache_ttl: float = 3600.0  # Cache forecasts for 1 hour
    max_forecast_days: int = 2          # Only bet on markets within 2 days (ensemble most reliable)

    # Edge thresholds
    min_edge: float = 0.10             # Minimum edge (forecast_prob - market_price) to trade (10%)
    min_forecast_prob: float = 0.30    # Don't bet on outcomes with <30% forecast prob — require
                                        # real conviction, not cheap tails (was 0.15: too low, let
                                        # long-shots through that dominated PnL by luck)
    min_price: float = 0.10            # Floor on outcome price: never buy below 10¢. Long-shots at
                                        # 2-6¢ are noise/lottery tickets (47× payouts on flukes);
                                        # they also rarely fill in live. (was a hardcoded 2¢)
    min_agreement: float = 0.30        # Minimum model agreement (30% = at least 15/50 agree)
    max_price: float = 0.65            # Don't buy outcomes priced above 65¢ (more upside)
    forecast_uncertainty_c: float = 2.0  # Calibration σ (°C) for kernel dressing: inflates the
                                          # ensemble spread to cover model bias + grid-vs-station
                                          # error. Ensemble alone is underdispersive (~0.25°C),
                                          # real error vs the resolution source is ~1-1.5°C.

    # Resolution source
    use_metar_resolution: bool = True  # Resolve dry_run/paper trades against real METAR observations
                                        # (IEM ASOS, by ICAO) instead of Open-Meteo. Avoids the
                                        # circularity of scoring the forecast with its own source.
                                        # Falls back to Open-Meteo if METAR is unavailable.

    # Bet sizing
    max_bet_per_trade: float = 15.0    # Max $ per trade (Kelly sizing is the real driver)
    bankroll: float = 300.0            # Total bankroll for weather strategy
    kelly_multiplier: float = 0.30     # 30% Kelly (slightly more aggressive)
    max_bets_per_cycle: int = 8        # Max trades per scan cycle (cities are independent)

    # Resolution
    resolution_check_interval: float = 3600.0  # Check resolutions every hour

    @classmethod
    def from_dict(cls, raw: dict, mode: str = "disabled") -> WeatherConfig:
        """Build config from TOML dict."""
        return cls(
            mode=raw.get("mode", mode),
            scan_interval=float(raw.get("scan_interval", 900.0)),
            forecast_cache_ttl=float(raw.get("forecast_cache_ttl", 3600.0)),
            max_forecast_days=int(raw.get("max_forecast_days", 2)),
            min_edge=float(raw.get("min_edge", 0.10)),
            min_forecast_prob=float(raw.get("min_forecast_prob", 0.30)),
            min_price=float(raw.get("min_price", 0.10)),
            min_agreement=float(raw.get("min_agreement", 0.30)),
            max_price=float(raw.get("max_price", 0.65)),
            forecast_uncertainty_c=float(raw.get("forecast_uncertainty_c", 2.0)),
            use_metar_resolution=bool(raw.get("use_metar_resolution", True)),
            max_bet_per_trade=float(raw.get("max_bet_per_trade", 15.0)),
            bankroll=float(raw.get("bankroll", 300.0)),
            kelly_multiplier=float(raw.get("kelly_multiplier", 0.30)),
            max_bets_per_cycle=int(raw.get("max_bets_per_cycle", 8)),
            resolution_check_interval=float(raw.get("resolution_check_interval", 3600.0)),
            max_concurrent_bets=int(raw.get("max_concurrent_bets", 10)),
        )


class WeatherStrategy(Strategy):
    """Weather prediction: bet on temperature outcomes using ensemble forecasts."""

    def __init__(
        self,
        config: WeatherConfig,
        context: AccountContext,
        credentials=None,
    ):
        super().__init__(config, context)
        self._scanner = WeatherScanner(config=config, credentials=credentials)
        self._resolution_task: asyncio.Task | None = None

    @property
    def scanner(self) -> WeatherScanner:
        return self._scanner

    async def start(self) -> None:
        import asyncio

        if self.config.mode == "disabled":
            return
        await self._scanner.start()
        self._resolution_task = asyncio.create_task(self._resolution_loop())
        logger.info("weather_strategy_started", mode=self.config.mode)

    async def stop(self) -> None:
        if self._resolution_task and not self._resolution_task.done():
            self._resolution_task.cancel()
            try:
                await self._resolution_task
            except asyncio.CancelledError:
                pass
        await self._scanner.stop()
        logger.info("weather_strategy_stopped")

    async def _resolution_loop(self):
        """Periodically check if pending trades resolved."""
        import asyncio
        logger.info(
            "weather_resolution_loop_started",
            interval_s=self.config.resolution_check_interval,
        )
        while True:
            try:
                await asyncio.sleep(self.config.resolution_check_interval)
                await self._scanner.check_resolutions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("weather_resolution_error", error=str(e))

    def get_stats(self) -> dict:
        return self._scanner.get_stats()

    def export_full_report(self) -> dict:
        stats = self._scanner.get_stats()
        return {
            "strategy": "weather",
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
            "forecast_cache_ttl": cfg.forecast_cache_ttl,
            "max_forecast_days": cfg.max_forecast_days,
            "min_edge": cfg.min_edge,
            "min_forecast_prob": cfg.min_forecast_prob,
            "min_price": cfg.min_price,
            "min_agreement": cfg.min_agreement,
            "max_price": cfg.max_price,
            "forecast_uncertainty_c": cfg.forecast_uncertainty_c,
            "use_metar_resolution": cfg.use_metar_resolution,
            "max_bet_per_trade": cfg.max_bet_per_trade,
            "bankroll": cfg.bankroll,
            "kelly_multiplier": cfg.kelly_multiplier,
            "max_bets_per_cycle": cfg.max_bets_per_cycle,
        }

    async def set_mode(self, new_mode: str) -> None:
        old_mode = self.config.mode
        if new_mode != old_mode:
            # Reset stats and trades when switching mode so live only shows live data
            self._scanner.reset_stats()
        await super().set_mode(new_mode)
        if new_mode in ("dry_run", "live") and not self._scanner._initialized:
            await self._scanner._init_clob_client()

    async def restore_open_positions(self, positions: list[Any]) -> None:
        pass


# Import asyncio at module level for type checking
import asyncio  # noqa: E402

register_strategy("weather", WeatherStrategy, WeatherConfig)
