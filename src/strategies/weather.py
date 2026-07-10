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
    min_forecast_prob: float = 0.40    # Don't bet on outcomes with <40% forecast prob — require
                                        # real conviction, not cheap tails. 0.15→0.30→0.40: the
                                        # discriminator showed the 0.25-0.40 bucket at 0/5 (pure
                                        # loss) while >=0.40 ran 50% win rate.
    min_price: float = 0.10            # Floor on outcome price: never buy below 10¢. Long-shots at
                                        # 2-6¢ are noise/lottery tickets (47× payouts on flukes);
                                        # they also rarely fill in live. (was a hardcoded 2¢)
    min_agreement: float = 0.30        # Minimum model agreement (30% = at least 15/50 agree)
    max_price: float = 0.65            # Don't buy outcomes priced above 65¢ (more upside)
    forecast_uncertainty_c: float = 2.0  # Calibration σ (°C) for kernel dressing: inflates the
                                          # ensemble spread to cover model bias + grid-vs-station
                                          # error. Ensemble alone is underdispersive (~0.25°C),
                                          # real error vs the resolution source is ~1-1.5°C.
                                          # Acts as a FLOOR: sigma_calibration can only widen it.

    # Empirical σ calibration (#1): derive the dressing width from measured
    # ensemble-vs-METAR dispersion per lead_days instead of trusting the fixed
    # forecast_uncertainty_c. Cures the structural overconfidence (declared
    # prob ≥0.40 resolved at 0.167 win rate).
    sigma_calibration: bool = True
    sigma_min_samples: int = 12        # Min verified residuals per lead before trusting empirical σ

    # Probability calibration map (#2): isotonic remap of declared forecast_prob
    # → historical win rate, blended toward identity by sample count. Inert
    # until calibration_min_samples closed outcomes exist.
    prob_calibration: bool = True
    calibration_min_samples: int = 25
    calibration_shrink: float = 20.0   # Pseudo-count: weight = n/(n+shrink) toward the empirical map

    # Open-tail haircut (#3): "X or higher"/"X or below" buckets leak mass to the
    # ±999 sentinels and are where every loser landed. Discount their forecast_prob.
    open_tail_haircut: float = 0.80    # Multiply open-ended bucket prob by this before filters

    # Discriminator gate (#4): in live, refuse to trade a forecast_prob bucket whose
    # historical win rate is below the bucket's lower bound (i.e. not yet calibrated).
    discriminator_gate: bool = True
    discriminator_min_samples: int = 10  # Min closed outcomes in a bucket before the gate can block

    # Resolution source
    use_metar_resolution: bool = True  # Resolve dry_run/paper trades against real METAR observations
                                        # (IEM ASOS, by ICAO) instead of Open-Meteo. Avoids the
                                        # circularity of scoring the forecast with its own source.
                                        # Falls back to Open-Meteo if METAR is unavailable.

    # Per-station bias correction (the dressing widens; this recenters)
    bias_correction: bool = True       # Shift member temps by the measured station bias
                                        # (mean METAR-vs-raw-forecast error) before bucketing
    bias_min_samples: int = 6          # Verified forecasts required before correcting a station.
                                        # 6 (was 10): measured biases are large and consistent
                                        # (jeddah -1.78, China hot) and waiting costs real losses;
                                        # the ±bias_max_correction_c clamp guards the noisy tail.
    bias_max_correction_c: float = 4.0  # Clamp on the shift (#5, raised 3.0→4.0): KLAX measured
                                         # -4.31°C and a ±3 clamp left 1.3°C uncorrected. Stations
                                         # beyond bias_exclude_threshold_c are dropped, not clamped.
    bias_exclude_threshold_c: float = 6.0  # |raw bias| above this = broken data → block the station

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
            min_forecast_prob=float(raw.get("min_forecast_prob", 0.40)),
            min_price=float(raw.get("min_price", 0.10)),
            min_agreement=float(raw.get("min_agreement", 0.30)),
            max_price=float(raw.get("max_price", 0.65)),
            forecast_uncertainty_c=float(raw.get("forecast_uncertainty_c", 2.0)),
            sigma_calibration=bool(raw.get("sigma_calibration", True)),
            sigma_min_samples=int(raw.get("sigma_min_samples", 12)),
            prob_calibration=bool(raw.get("prob_calibration", True)),
            calibration_min_samples=int(raw.get("calibration_min_samples", 25)),
            calibration_shrink=float(raw.get("calibration_shrink", 20.0)),
            open_tail_haircut=float(raw.get("open_tail_haircut", 0.80)),
            discriminator_gate=bool(raw.get("discriminator_gate", True)),
            discriminator_min_samples=int(raw.get("discriminator_min_samples", 10)),
            use_metar_resolution=bool(raw.get("use_metar_resolution", True)),
            bias_correction=bool(raw.get("bias_correction", True)),
            bias_min_samples=int(raw.get("bias_min_samples", 6)),
            bias_max_correction_c=float(raw.get("bias_max_correction_c", 4.0)),
            bias_exclude_threshold_c=float(raw.get("bias_exclude_threshold_c", 6.0)),
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
        # Shared RiskConfig (same object the panel mutates) → daily stop-loss.
        risk = getattr(getattr(context, "_executor", None), "risk", None)
        self._scanner = WeatherScanner(
            config=config,
            credentials=credentials,
            max_daily_loss=getattr(risk, "max_daily_loss", 0.0) if risk else 0.0,
        )
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
            "sigma_calibration": cfg.sigma_calibration,
            "sigma_min_samples": cfg.sigma_min_samples,
            "prob_calibration": cfg.prob_calibration,
            "calibration_min_samples": cfg.calibration_min_samples,
            "calibration_shrink": cfg.calibration_shrink,
            "open_tail_haircut": cfg.open_tail_haircut,
            "discriminator_gate": cfg.discriminator_gate,
            "discriminator_min_samples": cfg.discriminator_min_samples,
            "use_metar_resolution": cfg.use_metar_resolution,
            "bias_correction": cfg.bias_correction,
            "bias_min_samples": cfg.bias_min_samples,
            "bias_max_correction_c": cfg.bias_max_correction_c,
            "bias_exclude_threshold_c": cfg.bias_exclude_threshold_c,
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
