"""Hot-reload config: mutate in-memory config + persist to TOML."""
from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib as tomli
else:
    import tomli

import tomli_w


class ConfigManager:
    """Reads/writes config values on the live bot, persisting changes to TOML."""

    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self._toml_path = Path(self.config._path)

    # ── Copy Trade ─────────────────────────────────────────────────

    def get_copy_trade_params(self) -> dict:
        """Return current copy-trade params from the first copy_trade account."""
        acc = self._find_copy_account()
        if not acc:
            return {}
        cfg = acc.copy_trade
        return {
            "fixed_bet_size": cfg.fixed_bet_size,
            "poll_interval_ms": cfg.poll_interval_ms,
            "min_price": cfg.min_price,
            "max_concurrent_bets": cfg.max_concurrent_bets,
            "spread_arb_multiplier": cfg.spread_arb_multiplier,
            "max_bet_per_trade": acc.risk.max_bet_per_trade,
            "max_daily_loss": acc.risk.max_daily_loss,
            "simulated_balance": acc.risk.simulated_balance,
        }

    def set_copy_trade_params(self, params: dict):
        """Update copy-trade params in-memory and persist."""
        for acc_cfg in self.config.accounts:
            if acc_cfg.strategy_type != "copy_trade":
                continue
            if "fixed_bet_size" in params:
                acc_cfg.copy_trade.fixed_bet_size = float(params["fixed_bet_size"])
            if "poll_interval_ms" in params:
                acc_cfg.copy_trade.poll_interval_ms = int(params["poll_interval_ms"])
            if "min_price" in params:
                acc_cfg.copy_trade.min_price = float(params["min_price"])
            if "max_concurrent_bets" in params:
                acc_cfg.copy_trade.max_concurrent_bets = int(params["max_concurrent_bets"])
            if "spread_arb_multiplier" in params:
                acc_cfg.copy_trade.spread_arb_multiplier = float(params["spread_arb_multiplier"])
            if "max_bet_per_trade" in params:
                acc_cfg.risk.max_bet_per_trade = float(params["max_bet_per_trade"])
            if "max_daily_loss" in params:
                acc_cfg.risk.max_daily_loss = float(params["max_daily_loss"])
            if "simulated_balance" in params:
                acc_cfg.risk.simulated_balance = float(params["simulated_balance"])

        # Also update live AccountRunner objects
        for runner in self.bot.accounts:
            if runner.strategy_type != "copy_trade":
                continue
            cfg = runner.account
            if "fixed_bet_size" in params:
                cfg.copy_trade.fixed_bet_size = float(params["fixed_bet_size"])
            if "poll_interval_ms" in params:
                cfg.copy_trade.poll_interval_ms = int(params["poll_interval_ms"])
            if "min_price" in params:
                cfg.copy_trade.min_price = float(params["min_price"])
            if "max_concurrent_bets" in params:
                cfg.copy_trade.max_concurrent_bets = int(params["max_concurrent_bets"])
            if "spread_arb_multiplier" in params:
                cfg.copy_trade.spread_arb_multiplier = float(params["spread_arb_multiplier"])
            if "max_bet_per_trade" in params:
                cfg.risk.max_bet_per_trade = float(params["max_bet_per_trade"])
            if "max_daily_loss" in params:
                cfg.risk.max_daily_loss = float(params["max_daily_loss"])
            if "simulated_balance" in params:
                cfg.risk.simulated_balance = float(params["simulated_balance"])

        self._persist()

    def get_copy_wallets(self) -> list[str]:
        """Return target wallets from config."""
        acc = self._find_copy_account()
        return list(acc.copy_trade.target_wallets) if acc else []

    def add_copy_wallet(self, address: str):
        """Add wallet to all copy-trade accounts."""
        address = address.lower()
        for acc_cfg in self.config.accounts:
            if acc_cfg.strategy_type == "copy_trade" and address not in acc_cfg.copy_trade.target_wallets:
                acc_cfg.copy_trade.target_wallets.append(address)
        for runner in self.bot.accounts:
            if runner.strategy_type == "copy_trade" and address not in runner.account.copy_trade.target_wallets:
                runner.account.copy_trade.target_wallets.append(address)
        self._persist()

    def remove_copy_wallet(self, address: str):
        """Remove wallet from all copy-trade accounts."""
        address = address.lower()
        for acc_cfg in self.config.accounts:
            if acc_cfg.strategy_type == "copy_trade":
                acc_cfg.copy_trade.target_wallets = [
                    w for w in acc_cfg.copy_trade.target_wallets if w != address
                ]
        for runner in self.bot.accounts:
            if runner.strategy_type == "copy_trade":
                runner.account.copy_trade.target_wallets = [
                    w for w in runner.account.copy_trade.target_wallets if w != address
                ]
        self._persist()

    # ── Directional ────────────────────────────────────────────────

    def get_directional_params(self) -> dict:
        """Return current directional params."""
        s = self.config.strategy
        r = self.config.risk
        # Get balance from first directional account
        balance = r.simulated_balance
        for acc in self.config.accounts:
            if acc.strategy_type == "directional":
                balance = acc.risk.simulated_balance
                break
        return {
            "kill_switch": r.kill_switch,
            "min_margin_net": s.min_margin_net,
            "max_price": s.max_price,
            "min_buffer_pct": s.min_buffer_pct,
            "max_concurrent_bets": s.max_concurrent_bets,
            "max_bet_per_trade": r.max_bet_per_trade,
            "max_daily_loss": r.max_daily_loss,
            "simulated_balance": balance,
            "crypto_only": s.tag == "crypto",
            "max_markets_monitored": self.config.data.max_markets_monitored,
        }

    def set_directional_params(self, params: dict):
        """Update directional params in-memory and persist."""
        s = self.config.strategy
        r = self.config.risk

        if "kill_switch" in params:
            val = params["kill_switch"]
            r.kill_switch = val if isinstance(val, bool) else str(val).lower() == "true"
        if "min_margin_net" in params:
            s.min_margin_net = float(params["min_margin_net"])
        if "max_price" in params:
            s.max_price = float(params["max_price"])
        if "min_buffer_pct" in params:
            s.min_buffer_pct = float(params["min_buffer_pct"])
        if "max_concurrent_bets" in params:
            s.max_concurrent_bets = int(params["max_concurrent_bets"])
        if "max_bet_per_trade" in params:
            r.max_bet_per_trade = float(params["max_bet_per_trade"])
        if "max_daily_loss" in params:
            r.max_daily_loss = float(params["max_daily_loss"])

        if "simulated_balance" in params:
            for acc in self.config.accounts:
                if acc.strategy_type == "directional":
                    acc.risk.simulated_balance = float(params["simulated_balance"])

        if "crypto_only" in params:
            crypto = str(params["crypto_only"]).lower() == "true"
            s.tag = "crypto" if crypto else ""
            if crypto:
                self.config.data.max_markets_monitored = min(
                    self.config.data.max_markets_monitored, 20
                )
        if "max_markets_monitored" in params:
            val = int(params["max_markets_monitored"])
            if s.tag == "crypto":
                val = min(val, 20)
            self.config.data.max_markets_monitored = val

        # Update live runner risk configs
        for runner in self.bot.accounts:
            if runner.strategy_type == "directional":
                runner.account.risk.kill_switch = r.kill_switch
                runner.account.risk.max_bet_per_trade = r.max_bet_per_trade
                runner.account.risk.max_daily_loss = r.max_daily_loss
                if "simulated_balance" in params:
                    runner.account.risk.simulated_balance = float(params["simulated_balance"])

        self._persist()

    # ── Execution Mode ─────────────────────────────────────────────

    def get_account_modes(self) -> list[dict]:
        """Return execution mode per account."""
        return [
            {
                "name": runner.name,
                "strategy_type": runner.strategy_type,
                "execution_mode": runner.exec_mode.value,
            }
            for runner in self.bot.accounts
        ]

    async def set_account_mode(self, account_name: str, mode: str) -> bool:
        """Change execution mode for a specific account. Returns True if changed."""
        if mode not in ("paper", "live"):
            return False

        for runner in self.bot.accounts:
            if runner.name == account_name:
                await runner.set_execution_mode(mode)
                # Update config and persist
                for acc_cfg in self.config.accounts:
                    if acc_cfg.name == account_name:
                        acc_cfg.execution_mode = mode
                self._persist()
                return True
        return False

    # ── Crypto Configs ────────────────────────────────────────────

    def get_crypto_configs(self) -> dict:
        """Return per-crypto directional configs as serializable dicts."""
        return {
            name: {"enabled": cc.enabled, "buffer_pct": cc.buffer_pct}
            for name, cc in self.config.strategy.crypto_configs.items()
        }

    def set_crypto_config(self, crypto_name: str, enabled: bool | None = None, buffer_pct: float | None = None):
        """Update a single crypto's directional config."""
        from src.config import CryptoDirectionalConfig

        cc = self.config.strategy.crypto_configs.get(crypto_name)
        if cc is None:
            cc = CryptoDirectionalConfig()
            self.config.strategy.crypto_configs[crypto_name] = cc

        if enabled is not None:
            cc.enabled = enabled
        if buffer_pct is not None:
            cc.buffer_pct = buffer_pct

        # Update live runners' detector price_checker
        for runner in self.bot.accounts:
            if runner.strategy_type == "directional" and hasattr(runner, 'detector') and runner.detector:
                runner.detector.config.crypto_configs = self.config.strategy.crypto_configs
                runner.detector._price_checker._crypto_configs = self.config.strategy.crypto_configs

        self._persist()

    # ── Persistence ────────────────────────────────────────────────

    def _persist(self):
        """Write current config state back to TOML file."""
        # Read existing TOML to preserve structure/comments as much as possible
        with open(self._toml_path, "rb") as f:
            raw = tomli.load(f)

        # Update strategy section
        s = self.config.strategy
        raw.setdefault("strategy", {})
        raw["strategy"]["min_margin_net"] = s.min_margin_net
        raw["strategy"]["max_price"] = s.max_price
        raw["strategy"]["min_buffer_pct"] = s.min_buffer_pct
        raw["strategy"]["max_concurrent_bets"] = s.max_concurrent_bets
        raw["strategy"]["tag"] = s.tag

        # Per-crypto directional configs
        raw["strategy"]["crypto_configs"] = {
            name: {"enabled": cc.enabled, "buffer_pct": cc.buffer_pct}
            for name, cc in s.crypto_configs.items()
        }

        # Update data section
        raw.setdefault("data", {})
        raw["data"]["max_markets_monitored"] = self.config.data.max_markets_monitored

        # Update risk section
        r = self.config.risk
        raw.setdefault("risk", {})
        raw["risk"]["kill_switch"] = r.kill_switch
        raw["risk"]["max_bet_per_trade"] = r.max_bet_per_trade
        raw["risk"]["max_daily_loss"] = r.max_daily_loss

        # Update accounts
        for i, acc_raw in enumerate(raw.get("accounts", [])):
            if i >= len(self.config.accounts):
                break
            acc_cfg = self.config.accounts[i]

            acc_raw["execution_mode"] = acc_cfg.execution_mode

            if acc_cfg.strategy_type == "copy_trade":
                acc_raw.setdefault("copy_trade", {})
                acc_raw["copy_trade"]["target_wallets"] = list(acc_cfg.copy_trade.target_wallets)
                acc_raw["copy_trade"]["fixed_bet_size"] = acc_cfg.copy_trade.fixed_bet_size
                acc_raw["copy_trade"]["poll_interval_ms"] = acc_cfg.copy_trade.poll_interval_ms
                acc_raw["copy_trade"]["min_price"] = acc_cfg.copy_trade.min_price
                acc_raw["copy_trade"]["max_concurrent_bets"] = acc_cfg.copy_trade.max_concurrent_bets
                acc_raw["copy_trade"]["spread_arb_multiplier"] = acc_cfg.copy_trade.spread_arb_multiplier

            acc_raw.setdefault("risk", {})
            acc_raw["risk"]["max_bet_per_trade"] = acc_cfg.risk.max_bet_per_trade
            acc_raw["risk"]["max_daily_loss"] = acc_cfg.risk.max_daily_loss
            acc_raw["risk"]["simulated_balance"] = acc_cfg.risk.simulated_balance

        with open(self._toml_path, "wb") as f:
            tomli_w.dump(raw, f)

    # ── Helpers ────────────────────────────────────────────────────

    def _find_copy_account(self):
        for acc in self.config.accounts:
            if acc.strategy_type == "copy_trade":
                return acc
        return None
