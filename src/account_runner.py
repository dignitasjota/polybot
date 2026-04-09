from __future__ import annotations

import asyncio
import time

import structlog

from src.config import AccountConfig, Config, StrategyConfig, DataConfig, WebSocketConfig
from src.copy_trader import CopyTrader
from src.db import get_wallet_overrides
from src.detector import ClosingArbitrageDetector
from src.executor import Executor, ExecutionMode
from src.gamma_client import GammaClient
from src.market_tracker import MarketTracker
from src.wallet_scanner import WalletScanner
from src.websocket_client import WebSocketClient


class AccountRunner:
    """Independent runner for a single trading account.

    Each account has its own:
    - MarketTracker + WebSocket (shared market data for directional)
    - Detector or CopyTrader (depending on strategy_type)
    - Executor (with its own credentials and risk limits)

    Accounts do NOT share state — they are fully independent.
    """

    def __init__(
        self,
        account: AccountConfig,
        strategy: StrategyConfig,
        data: DataConfig,
        ws_config: WebSocketConfig,
        shared_tracker: MarketTracker | None = None,
        shared_ws: WebSocketClient | None = None,
        shared_gamma: GammaClient | None = None,
    ):
        self.account = account
        self.name = account.name
        self.log = structlog.get_logger(f"polymarket.account.{self.name}")

        # Execution mode
        self.exec_mode = {
            "paper": ExecutionMode.PAPER,
            "dry_run": ExecutionMode.DRY_RUN,
            "live": ExecutionMode.LIVE,
        }.get(account.execution_mode, ExecutionMode.PAPER)

        self.executor = Executor(account.risk, mode=self.exec_mode)

        # Strategy-specific components
        self.strategy_type = account.strategy_type
        self.detector: ClosingArbitrageDetector | None = None
        self.copy_trader: CopyTrader | None = None
        self.wallet_scanner = WalletScanner()  # Always initialized, used for profitability tracking

        # For directional strategy, share market infra or create own
        if self.strategy_type == "directional":
            if shared_tracker and shared_ws:
                self.tracker = shared_tracker
                self.ws_client = shared_ws
                self.gamma = shared_gamma
                self._owns_infra = False
            else:
                self.tracker = MarketTracker()
                self.ws_client = WebSocketClient(ws_config, self.tracker)
                self.gamma = GammaClient()
                self._owns_infra = True

            self.detector = ClosingArbitrageDetector(
                strategy, self.tracker, account.risk,
                starting_balance=account.risk.simulated_balance,
            )
        elif self.strategy_type == "copy_trade":
            self.copy_trader = CopyTrader(
                account.copy_trade,
                starting_balance=account.risk.simulated_balance,
            )
            self.tracker = None
            self.ws_client = None
            self.gamma = None
            self._owns_infra = False

        self._running = False
        self._data_config = data
        self._strategy_config = strategy
        self._orphan_scan_task: asyncio.Task | None = None

    async def start(self):
        """Start this account's trading loop."""
        self.log.info(
            "account_starting",
            strategy=self.strategy_type,
            mode=self.exec_mode.value,
            name=self.name,
        )
        self._running = True

        # Initialize executor
        if self.exec_mode != ExecutionMode.PAPER:
            self.executor._credentials = self.account.credentials
        await self.executor.initialize()

        # Register balance sync callback
        self.executor.on_balance_update(self._on_balance_changed)

        # Sync live balance to strategy components
        self._sync_live_balance()

        # Live mode: scan for orphan winning positions left over from previous
        # runs and redeem them. Done once on startup, then periodically.
        if self.exec_mode == ExecutionMode.LIVE:
            try:
                triggered = await self.executor.scan_and_redeem_orphan_positions()
                self.log.info(
                    "orphan_scan_startup",
                    account=self.name,
                    triggered=triggered,
                )
            except Exception as e:
                self.log.warning("orphan_scan_startup_error", account=self.name, error=str(e))
            if self._orphan_scan_task is None or self._orphan_scan_task.done():
                self._orphan_scan_task = asyncio.create_task(self._run_orphan_scan_loop())

        if self.strategy_type == "directional":
            await self._start_directional()
        elif self.strategy_type == "copy_trade":
            await self._start_copy_trade()

    async def _run_orphan_scan_loop(self):
        """Periodically scan for orphan redeemable positions on Polymarket.

        This catches winning positions that resolved while the bot was down
        or that were placed in a previous run (and so are not in the in-memory
        _bet_placed dict). Without this, those positions stay locked forever.
        """
        ORPHAN_SCAN_INTERVAL = 300  # 5 minutes
        while self._running:
            try:
                await asyncio.sleep(ORPHAN_SCAN_INTERVAL)
                if not self._running or self.exec_mode != ExecutionMode.LIVE:
                    continue
                triggered = await self.executor.scan_and_redeem_orphan_positions()
                if triggered > 0:
                    self.log.info(
                        "orphan_scan_periodic",
                        account=self.name,
                        triggered=triggered,
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log.warning("orphan_scan_loop_error", account=self.name, error=str(e))

    async def _start_directional(self):
        """Start directional (Binance-based) strategy."""
        # Start price checker
        await self.detector._price_checker.start()

        # Wire callbacks
        self.detector.on_opportunity(self.executor.execute)
        self.detector.on_redeem(self.executor.redeem_position)
        self.ws_client.on_opportunity(self.detector.check)
        # Live balance sync: executor -> detector (for live mode)
        self.executor.on_order_confirmed(self.detector._on_executor_order_confirmed)
        self.executor.on_order_cancelled(self.detector._on_executor_order_cancelled)
        self.executor.on_position_redeemed(self.detector._on_executor_position_redeemed)

        self.log.info(
            "account_directional_ready",
            name=self.name,
            redeem_callback_registered=bool(self.detector._on_redeem_cb),
            mode=self.exec_mode.value,
        )

    async def _start_copy_trade(self):
        """Start copy-trading strategy."""
        # Load wallet overrides (roles + enabled) from DB
        overrides = await get_wallet_overrides()
        self.copy_trader.set_wallet_overrides(overrides)

        # Wire callbacks: copy trader -> executor + scanner
        self.copy_trader.on_opportunity(self.executor.execute)
        self.copy_trader.on_redeem(self.executor.redeem_position)
        self.copy_trader.on_trade(self.wallet_scanner.on_trade)
        self.copy_trader.on_resolve(self.wallet_scanner.on_market_resolved_with_side)
        await self.copy_trader.start()

        self.log.info(
            "account_copy_trade_ready",
            name=self.name,
            wallets=len(self.account.copy_trade.target_wallets),
            redeem_callback_registered=bool(self.copy_trader._on_redeem_cb),
            mode=self.exec_mode.value,
        )

    async def set_execution_mode(self, mode_str: str):
        """Change execution mode at runtime (paper/live)."""
        mode = {
            "paper": ExecutionMode.PAPER,
            "dry_run": ExecutionMode.DRY_RUN,
            "live": ExecutionMode.LIVE,
        }.get(mode_str, ExecutionMode.PAPER)

        if mode == self.exec_mode:
            return

        # For live/dry_run, ensure credentials are set
        if mode in (ExecutionMode.LIVE, ExecutionMode.DRY_RUN):
            self.executor._credentials = self.account.credentials

        old = self.exec_mode
        self.exec_mode = mode
        self.account.execution_mode = mode_str
        await self.executor.set_mode(mode)

        # When switching to live/dry_run: reset paper stats and use real balance
        if mode in (ExecutionMode.LIVE, ExecutionMode.DRY_RUN):
            live_balance = self.executor.get_balance()
            self.executor.reset_trades()
            if self.detector:
                self.detector.reset_stats(new_balance=live_balance)
            if self.copy_trader:
                self.copy_trader.reset_stats(new_balance=live_balance)
            # Sync live balance to strategy components
            self._sync_live_balance()
            self.log.info(
                "stats_reset_for_live",
                account=self.name,
                live_balance=f"${live_balance:.2f}" if live_balance is not None else "unknown",
            )
            # Trigger orphan scan + ensure background loop is running
            if mode == ExecutionMode.LIVE:
                try:
                    triggered = await self.executor.scan_and_redeem_orphan_positions()
                    self.log.info(
                        "orphan_scan_mode_change",
                        account=self.name,
                        triggered=triggered,
                    )
                except Exception as e:
                    self.log.warning("orphan_scan_mode_change_error", account=self.name, error=str(e))
                if self._orphan_scan_task is None or self._orphan_scan_task.done():
                    self._orphan_scan_task = asyncio.create_task(self._run_orphan_scan_loop())
        else:
            # Switching to paper: reset with simulated balance
            sim_balance = self.account.risk.simulated_balance
            self.executor.reset_trades()
            if self.detector:
                self.detector.reset_stats(new_balance=sim_balance)
            if self.copy_trader:
                self.copy_trader.reset_stats(new_balance=sim_balance)

        self.log.info(
            "execution_mode_changed",
            account=self.name,
            old=old.value,
            new=mode.value,
        )

    def _on_balance_changed(self, balance: float):
        """Called by executor when live balance is refreshed."""
        if self.exec_mode == ExecutionMode.LIVE:
            if self.detector:
                self.detector.set_live_balance(balance)
            if self.copy_trader:
                self.copy_trader.set_live_balance(balance)

    def _sync_live_balance(self):
        """Push live USDC balance from executor to detector/copy_trader."""
        live_balance = self.executor.get_balance()
        if live_balance is not None and self.exec_mode == ExecutionMode.LIVE:
            if self.detector:
                self.detector.set_live_balance(live_balance)
            if self.copy_trader:
                self.copy_trader.set_live_balance(live_balance)

    async def stop(self):
        """Stop this account."""
        self.log.info("account_stopping", name=self.name)
        self._running = False

        if self._orphan_scan_task and not self._orphan_scan_task.done():
            self._orphan_scan_task.cancel()
            try:
                await self._orphan_scan_task
            except asyncio.CancelledError:
                pass

        await self.executor.close()
        if self.detector:
            await self.detector.close()
        if self.copy_trader:
            await self.copy_trader.close()
        if self._owns_infra:
            if self.ws_client:
                await self.ws_client.stop()
            if self.gamma:
                await self.gamma.close()

    def get_stats(self) -> dict:
        """Get combined stats for this account."""
        stats = {
            "account": self.name,
            "strategy": self.strategy_type,
            "mode": self.exec_mode.value,
            "executor": self.executor.get_stats(),
        }
        if self.detector:
            stats["detector"] = self.detector.get_stats()
        if self.copy_trader:
            stats["copy_trader"] = self.copy_trader.get_stats()
        return stats

    def export_full_report(self) -> dict | None:
        """Export full report for JSON analysis."""
        if self.detector:
            return self.detector.export_full_report()
        if self.copy_trader:
            return self.copy_trader.export_full_report()
        return None

    def export_opportunities(self) -> list[dict]:
        """Export opportunities for dashboard."""
        if self.detector:
            return self.detector.export_opportunities()
        if self.copy_trader:
            return self.copy_trader.export_opportunities()
        return []
