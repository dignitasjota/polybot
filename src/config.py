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
class CryptoDirectionalConfig:
    """Per-crypto configuration for directional Up/Down strategy."""
    enabled: bool = True
    buffer_pct: float = 0.03  # Min buffer % for price confirmation


@dataclass
class StrategyConfig:
    enabled: bool = True
    name: str = "closing_arbitrage"
    max_time_to_resolution: timedelta = field(default_factory=lambda: timedelta(hours=24))
    min_margin_net: float = 0.008
    max_price: float = 0.60          # Max price for up/down directional bets
    min_buffer_pct: float = 0.10     # Min buffer % for price confirmation (global fallback)
    max_concurrent_bets: int = 3     # Max concurrent directional bets
    tag: str = ""                    # Filter markets by tag (e.g. "crypto")
    crypto_configs: dict[str, CryptoDirectionalConfig] = field(default_factory=dict)
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


@dataclass
class CredentialsConfig:
    """Credentials for a Polymarket account (env var names or direct values)."""
    private_key_env: str = "PRIVATE_KEY"
    api_key_env: str = "POLYMARKET_API_KEY"
    api_secret_env: str = "POLYMARKET_SECRET"
    passphrase_env: str = "POLYMARKET_PASSPHRASE"
    signature_type_env: str = "WALLET_TYPE"  # env var name for wallet type
    proxy_address_env: str = "POLYMARKET_PROXY_ADDRESS"  # env var for proxy/funder address
    signature_type: int = -1  # -1=auto-detect from env var, 0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE

    def __post_init__(self):
        if self.signature_type == -1:
            # Read from env var, fallback to WALLET_TYPE, then default magic_link
            wallet_type = os.environ.get(self.signature_type_env, "").strip()
            if not wallet_type:
                wallet_type = os.environ.get("WALLET_TYPE", "magic_link")
            wallet_type = wallet_type.lower().strip()
            if wallet_type in ("metamask", "eoa", "0"):
                self.signature_type = 0
            elif wallet_type in ("gnosis", "gnosis_safe", "safe", "2"):
                self.signature_type = 2
            else:
                self.signature_type = 1  # magic_link / poly_proxy / default

    def get_private_key(self) -> str:
        return get_secret(self.private_key_env)

    def get_api_key(self) -> str:
        return get_secret(self.api_key_env)

    def get_api_secret(self) -> str:
        return get_secret(self.api_secret_env)

    def get_proxy_address(self) -> str | None:
        """Get Polymarket proxy address (needed for Magic Link wallets to sign orders)."""
        # Try account-specific env var first, then fallback to global
        addr = os.environ.get(self.proxy_address_env, "").strip()
        if not addr:
            addr = os.environ.get("POLYMARKET_PROXY_ADDRESS", "").strip()
        return addr or None

    def get_passphrase(self) -> str:
        return get_secret(self.passphrase_env)

    # Builder Relayer credentials (for auto-redeem of winning positions)
    builder_key_env: str = "BUILDER_API_KEY"
    builder_secret_env: str = "BUILDER_SECRET"
    builder_passphrase_env: str = "BUILDER_PASSPHRASE"

    def get_builder_creds(self) -> tuple[str, str, str] | None:
        """Get Builder API credentials for the Relayer (gasless redeem).

        Returns (key, secret, passphrase) or None if not configured.
        """
        key = os.environ.get(self.builder_key_env, "").strip()
        secret = os.environ.get(self.builder_secret_env, "").strip()
        passphrase = os.environ.get(self.builder_passphrase_env, "").strip()

        # Debug: log which env vars are being looked for
        if not (key and secret and passphrase):
            import structlog
            log = structlog.get_logger("config")
            log.debug(
                "builder_creds_incomplete",
                looking_for_key=self.builder_key_env,
                key_found=bool(key),
                looking_for_secret=self.builder_secret_env,
                secret_found=bool(secret),
                looking_for_passphrase=self.builder_passphrase_env,
                passphrase_found=bool(passphrase),
            )

        if key and secret and passphrase:
            return (key, secret, passphrase)
        return None


@dataclass
class CopyTradeConfig:
    """Configuration for copy-trading strategy."""
    target_wallets: list[str] = field(default_factory=list)
    poll_interval_ms: int = 500  # How often to poll for new trades
    max_latency_ms: int = 5000   # Ignore trades older than this
    copy_size_mode: str = "fixed"  # "fixed" or "proportional"
    fixed_bet_size: float = 5.0   # Fixed bet size in USD
    proportional_factor: float = 1.0  # Multiplier for proportional mode
    min_price: float = 0.35       # Skip trades below this price
    max_concurrent_bets: int = 3  # Max concurrent copy bets
    spread_arb_multiplier: float = 3.0  # Bet multiplier when spread arb detected (both sides < $1)


@dataclass
class AccountConfig:
    """Configuration for a single trading account.

    Supports both old format (strategy_type + copy_trade) and new multi-
    strategy format (strategies dict). Auto-detected at load time.
    """
    name: str = "default"
    enabled: bool = True
    strategy_type: str = "directional"  # Legacy: "directional" or "copy_trade"
    execution_mode: str = "paper"  # Legacy: "paper", "dry_run", "live" (all strategies)
    credentials: CredentialsConfig = field(default_factory=CredentialsConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    copy_trade: CopyTradeConfig = field(default_factory=CopyTradeConfig)  # Legacy
    # Fase 9: per-strategy raw config dicts (new multi-strategy format).
    # Keys are strategy names ("directional", "copy_trade"), values are raw
    # config dicts with a "mode" field (disabled/paper/live).
    strategies: dict = field(default_factory=dict)


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
    accounts: list[AccountConfig] = field(default_factory=list)
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

        # Parse per-crypto directional configs
        crypto_configs_raw = strategy_raw.pop("crypto_configs", None)
        if crypto_configs_raw:
            strategy_raw["crypto_configs"] = {
                name: CryptoDirectionalConfig(
                    enabled=cfg.get("enabled", True),
                    buffer_pct=float(cfg.get("buffer_pct", 0.03)),
                )
                for name, cfg in crypto_configs_raw.items()
            }

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

        risk_raw = raw.get("risk", {})

        # Parse accounts (auto-detect old vs new format)
        accounts = []
        for acc_raw in raw.get("accounts", []):
            creds_raw = acc_raw.pop("credentials", {})
            creds = CredentialsConfig(**creds_raw) if creds_raw else CredentialsConfig()

            # Account-level risk overrides (falls back to global risk)
            acc_risk_raw = acc_raw.pop("risk", None)
            if acc_risk_raw:
                merged_risk = {**risk_raw, **acc_risk_raw}
                acc_risk = RiskConfig(**merged_risk)
            else:
                acc_risk = RiskConfig(**risk_raw)

            # Auto-detect: new multi-strategy format has "strategies" key
            strategies_raw = acc_raw.pop("strategies", None)

            if strategies_raw:
                # ── New format (Fase 9) ──
                # Determine primary strategy_type and execution_mode from
                # the strategies dict for backward compat.
                first_strat = next(iter(strategies_raw), "directional")
                first_mode = strategies_raw.get(first_strat, {}).get("mode", "paper")

                # Extract copy_trade config if present (for legacy CopyTrader)
                ct_raw = strategies_raw.get("copy_trade", {}).copy()
                ct_raw.pop("mode", None)
                ct_raw.pop("priority", None)
                ct_raw.pop("enabled", None)
                copy_cfg = CopyTradeConfig(**ct_raw) if ct_raw else CopyTradeConfig()

                accounts.append(AccountConfig(
                    credentials=creds,
                    risk=acc_risk,
                    copy_trade=copy_cfg,
                    strategy_type=first_strat,
                    execution_mode=first_mode,
                    strategies=strategies_raw,
                    **acc_raw,
                ))
            else:
                # ── Old format (pre-Fase 9) ──
                copy_raw = acc_raw.pop("copy_trade", {})
                copy_cfg = CopyTradeConfig(**copy_raw) if copy_raw else CopyTradeConfig()

                accounts.append(AccountConfig(
                    credentials=creds,
                    copy_trade=copy_cfg,
                    risk=acc_risk,
                    **acc_raw,
                ))

        # Fallback: if no accounts defined, create one default account
        if not accounts:
            accounts.append(AccountConfig(
                name="default",
                strategy_type="directional",
                execution_mode="paper",
                risk=RiskConfig(**risk_raw),
            ))

        cfg = cls(
            strategy=StrategyConfig(**strategy_raw),
            risk=RiskConfig(**risk_raw),
            data=DataConfig(**raw.get("data", {})),
            websocket=WebSocketConfig(**raw.get("websocket", {})),
            logging=LoggingConfig(**raw.get("logging", {})),
            accounts=accounts,
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
