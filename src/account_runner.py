from __future__ import annotations

import asyncio
import time

import structlog

from src.config import AccountConfig, Config, StrategyConfig, DataConfig, WebSocketConfig
from src.copy_trader import CopyTrader
from src.detector import ClosingArbitrageDetector
from src.executor import Executor, ExecutionMode
from src.gamma_client import GammaClient
from src.market_tracker import MarketTracker
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

        if self.strategy_type == "directional":
            await self._start_directional()
        elif self.strategy_type == "copy_trade":
            await self._start_copy_trade()

    async def _start_directional(self):
        """Start directional (Binance-based) strategy."""
        # Start price checker
        await self.detector._price_checker.start()

        # Wire callbacks
        self.detector.on_opportunity(self.executor.execute)
        self.ws_client.on_opportunity(self.detector.check)

        self.log.info("account_directional_ready", name=self.name)

    async def _start_copy_trade(self):
        """Start copy-trading strategy."""
        # Wire callback: copy trader -> executor
        self.copy_trader.on_opportunity(self.executor.execute)
        await self.copy_trader.start()

        self.log.info(
            "account_copy_trade_ready",
            name=self.name,
            wallets=len(self.account.copy_trade.target_wallets),
        )

    async def stop(self):
        """Stop this account."""
        self.log.info("account_stopping", name=self.name)
        self._running = False

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
