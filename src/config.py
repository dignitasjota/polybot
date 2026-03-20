from __future__ import annotations

import os
import signal
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import sys

if sys.version_info >= (3, 11):
    import tomllib as tomli
else:
    import tomli


@dataclass
class ProbabilityTier:
    """Maps time remaining to minimum required probability."""
    max_hours: float  # If time remaining < max_hours, this tier applies
    min_probability: float


@dataclass
class StrategyConfig:
    enabled: bool = True
    name: str = "closing_arbitrage"
    max_time_to_resolution: timedelta = field(default_factory=lambda: timedelta(hours=24))
    min_margin_net: float = 0.008
    probability_tiers: list[ProbabilityTier] = field(default_factory=lambda: [
        ProbabilityTier(max_hours=0.5, min_probability=0.93),
        ProbabilityTier(max_hours=2, min_probability=0.95),
        ProbabilityTier(max_hours=6, min_probability=0.97),
        ProbabilityTier(max_hours=24, min_probability=0.99),
    ])

    def get_min_probability(self, hours_remaining: float) -> float:
        """Get minimum probability required for a given time remaining."""
        for tier in self.probability_tiers:
            if hours_remaining <= tier.max_hours:
                return tier.min_probability
        # Beyond all tiers, use the strictest
        return self.probability_tiers[-1].min_probability if self.probability_tiers else 0.99


@dataclass
class RiskConfig:
    max_bet_pct: float = 2.0  # % of balance per bet
    max_bet_per_trade: float = 200.0  # Absolute cap
    max_position_per_market: float = 400.0
    max_total_exposure: float = 1000.0
    max_daily_loss: float = 100.0
    max_concurrent_positions: int = 5
    kill_switch: bool = False
    simulated_balance: float = 500.0  # Starting balance for paper trading


@dataclass
class DataConfig:
    stale_data_threshold_seconds: int = 5
    gamma_poll_interval_seconds: int = 300
    max_markets_monitored: int = 20


@dataclass
class WebSocketConfig:
    ping_interval_seconds: int = 10
    pong_timeout_seconds: int = 5
    reconnect_max_delay_ms: int = 2000
    reconnect_jitter_pct: int = 20
    fallback_rest_interval_ms: int = 500


@dataclass
class LoggingConfig:
    level: str = "INFO"
    format: str = "json"
    file: str = "/app/logs/bot.jsonl"
    max_file_size_mb: int = 100
    rotate_count: int = 5


def _parse_duration(value: str) -> timedelta:
    """Parse duration strings like '24h', '30m', '5s'."""
    value = value.strip().lower()
    if value.endswith("h"):
        return timedelta(hours=float(value[:-1]))
    if value.endswith("m"):
        return timedelta(minutes=float(value[:-1]))
    if value.endswith("s"):
        return timedelta(seconds=float(value[:-1]))
    return timedelta(hours=float(value))


@dataclass
class Config:
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    data: DataConfig = field(default_factory=DataConfig)
    websocket: WebSocketConfig = field(default_factory=WebSocketConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    _path: str = ""

    @classmethod
    def load(cls, path: str | Path = "config/config.toml") -> Config:
        path = Path(path)
        with open(path, "rb") as f:
            raw = tomli.load(f)

        strategy_raw = raw.get("strategy", {})
        if "max_time_to_resolution" in strategy_raw:
            strategy_raw["max_time_to_resolution"] = _parse_duration(
                strategy_raw["max_time_to_resolution"]
            )

        # Parse probability tiers
        tiers_raw = strategy_raw.pop("probability_tiers", None)
        if tiers_raw:
            strategy_raw["probability_tiers"] = [
                ProbabilityTier(
                    max_hours=float(t["max_hours"]),
                    min_probability=float(t["min_probability"]),
                )
                for t in tiers_raw
            ]

        cfg = cls(
            strategy=StrategyConfig(**strategy_raw),
            risk=RiskConfig(**raw.get("risk", {})),
            data=DataConfig(**raw.get("data", {})),
            websocket=WebSocketConfig(**raw.get("websocket", {})),
            logging=LoggingConfig(**raw.get("logging", {})),
            _path=str(path),
        )
        return cfg

    def reload(self) -> Config:
        return Config.load(self._path)


def get_secret(name: str) -> str:
    """Read a secret from environment variables."""
    value = os.environ.get(name)
    if not value:
        raise EnvironmentError(f"Required environment variable {name} is not set")
    return value
