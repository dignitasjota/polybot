"""Base Strategy abstraction, configuration, and account context.

Defines the interface that all trading strategies must implement.
Concrete strategies live in sibling modules (directional.py, copy_trade.py)
and are wired up via src/strategies/registry.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal

import structlog

logger = structlog.get_logger("polymarket.strategies")

# Hard system limit (paper trades per strategy per UTC day).
# Cannot be overridden by per-account config — protects DB from saturation.
PAPER_DAILY_TRADE_CAP = 500


# ─── Configuration ────────────────────────────────────────────────────────

@dataclass
class StrategyConfig(ABC):
    """Base configuration shared by all strategies.

    Subclasses add their own strategy-specific fields. All fields use
    defaults so subclassing with additional defaulted fields stays valid.
    """
    mode: Literal["disabled", "paper", "live"] = "disabled"
    priority: int = 1                          # Higher wins on conflicts (informational)
    max_concurrent_bets: int = 3               # Soft limit per strategy
    max_bet_per_trade: float = 50.0            # Soft limit per strategy
    paper_daily_trade_cap: int = PAPER_DAILY_TRADE_CAP  # Hard limit, read-only


# ─── AccountContext ───────────────────────────────────────────────────────

class AccountContext:
    """Read-only view of account state for strategies to consult.

    Strategies should NEVER mutate account state directly — they must go
    through the Executor. This context provides query helpers used during
    opportunity evaluation (balance check, dedupe, conflict detection).
    """

    def __init__(self, account_name: str, executor: Any):
        self.account_name = account_name
        self._executor = executor

    def get_balance(self, mode: str) -> float:
        """Get current balance for a given mode (live or paper).

        Falls back to 0.0 if the executor doesn't yet have a dual-ledger
        attribute (Fase 5 introduces _ledger_live / _ledger_paper).
        """
        ledger_attr = "_ledger_live" if mode == "live" else "_ledger_paper"
        ledger = getattr(self._executor, ledger_attr, None)
        if ledger is not None:
            return getattr(ledger, "balance", 0.0) or 0.0
        # Fallback to legacy single-balance executor
        bal = getattr(self._executor, "get_balance", lambda: None)()
        return bal or 0.0

    def get_open_positions(
        self, mode: str, strategy: str | None = None
    ) -> list[Any]:
        """Open positions (pending or confirmed) for the given mode.

        Optionally filter by source_strategy.
        """
        ledger_attr = "_ledger_live" if mode == "live" else "_ledger_paper"
        ledger = getattr(self._executor, ledger_attr, None)
        trades = getattr(ledger, "trades", []) if ledger else []
        if strategy:
            trades = [
                t for t in trades
                if getattr(t, "source_strategy", None) == strategy
            ]
        return [
            t for t in trades
            if getattr(t, "status", None) in ("pending", "confirmed")
        ]

    def has_position(self, condition_id: str, side: str, mode: str) -> bool:
        """True if there's already an open position for this market+side."""
        return any(
            getattr(p, "condition_id", None) == condition_id
            and getattr(p, "token_side", None) == side
            for p in self.get_open_positions(mode)
        )

    def has_opposite_position(
        self, condition_id: str, side: str, mode: str
    ) -> tuple[bool, str | None]:
        """True if there's an open position on the opposite side.

        Returns (exists, source_strategy_of_opposite) for logging/audit.
        """
        opposite = "NO" if side == "YES" else "YES"
        for p in self.get_open_positions(mode):
            if (
                getattr(p, "condition_id", None) == condition_id
                and getattr(p, "token_side", None) == opposite
            ):
                return True, getattr(p, "source_strategy", None)
        return False, None

    def count_active_positions(
        self, mode: str, strategy: str | None = None
    ) -> int:
        return len(self.get_open_positions(mode, strategy))

    def count_paper_trades_today(self, strategy: str) -> int:
        """Count paper trades created today (UTC) for a given strategy.

        Used to enforce the hard PAPER_DAILY_TRADE_CAP limit.
        """
        now = datetime.now(timezone.utc)
        start_of_day = datetime(
            now.year, now.month, now.day, tzinfo=timezone.utc
        ).timestamp()
        return sum(
            1 for p in self.get_open_positions("paper", strategy)
            if getattr(p, "created_at", 0) >= start_of_day
        )


# ─── Strategy ABC ─────────────────────────────────────────────────────────

class Strategy(ABC):
    """Base class for all trading strategies.

    Lifecycle:
      1. __init__(config, context)
      2. start() — open WS, begin polling, etc.
      3. (runtime) emit opportunities via _on_opportunity_cb
      4. set_mode(...) — switch between disabled/paper/live at runtime
      5. stop() — graceful shutdown

    Subclasses implement: start, stop, get_stats, get_config.
    """

    def __init__(self, config: StrategyConfig, context: AccountContext):
        self.config = config
        self.context = context
        self.name = self.__class__.__name__
        self._on_opportunity_cb: Callable | None = None
        self._on_redeem_cb: Callable | None = None

    @property
    def is_active(self) -> bool:
        """Active if mode is paper or live (not disabled)."""
        return self.config.mode != "disabled"

    @abstractmethod
    async def start(self) -> None:
        """Initialize and start the strategy."""

    @abstractmethod
    async def stop(self) -> None:
        """Cleanup and stop the strategy."""

    async def set_mode(self, new_mode: str) -> None:
        """Change execution mode (paper/live/disabled) at runtime.

        Transitions:
          - disabled → paper/live  : start()
          - paper/live → disabled  : stop()
          - paper ↔ live           : no restart, in-flight positions settle
                                     and new opportunities use the new mode
        """
        if new_mode not in ("disabled", "paper", "dry_run", "live"):
            raise ValueError(f"invalid mode: {new_mode}")

        old_mode = self.config.mode
        if old_mode == new_mode:
            return

        self.config.mode = new_mode

        if old_mode == "disabled" and new_mode != "disabled":
            await self.start()
        elif old_mode != "disabled" and new_mode == "disabled":
            await self.stop()
        # paper ↔ live: no-op restart; ledger separation handles the rest.

        logger.info(
            "strategy_mode_changed",
            strategy=self.name,
            old=old_mode,
            new=new_mode,
        )

    def update_config(self, new_cfg: dict[str, Any]) -> None:
        """Hot-reload: update config fields in place.

        Ignores unknown fields and the read-only paper_daily_trade_cap.
        """
        for key, value in new_cfg.items():
            if key == "paper_daily_trade_cap":
                continue  # read-only, hard system limit
            if hasattr(self.config, key):
                setattr(self.config, key, value)
        logger.debug(
            "strategy_config_updated", strategy=self.name, updates=new_cfg
        )

    def on_opportunity(self, callback: Callable) -> None:
        """Register callback when an opportunity is detected."""
        self._on_opportunity_cb = callback

    def on_redeem(self, callback: Callable) -> None:
        """Register callback for position redemption."""
        self._on_redeem_cb = callback

    @abstractmethod
    def get_stats(self) -> dict[str, Any]:
        """Strategy-specific stats (detections, scans — no balance/P&L).

        Balance and P&L belong to the Executor's ledgers.
        """

    @abstractmethod
    def get_config(self) -> dict[str, Any]:
        """Return current config as a serializable dict."""

    async def restore_open_positions(self, positions: list[Any]) -> None:
        """Restore in-memory tracking from persisted positions after redeploy.

        Default no-op; subclasses override if they keep their own dedupe
        sets, in-flight maps, etc.
        """
        return None

    def cleanup_market(self, condition_id: str) -> bool:
        """Hook called before MarketTracker drops a market.

        Return False to veto the cleanup (strategy still has open positions
        in this market). Default: always allow.
        """
        return True
