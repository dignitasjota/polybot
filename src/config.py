from __future__ import annotations

import os
import signal
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import tomli


@dataclass
class StrategyConfig:
    enabled: bool = True
    name: str = "closing_arbitrage"
    min_implied_probability: float = 0.95
    max_time_to_resolution: timedelta = field(default_factory=lambda: timedelta(hours=24))
    min_margin_net: float = 0.008


@dataclass
class RiskConfig:
    max_bet_per_trade: float = 200.0
    max_position_per_market: float = 400.0
    max_total_exposure: float = 1000.0
    max_daily_loss: float = 100.0
    max_concurrent_positions: int = 5
    kill_switch: bool = False


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
