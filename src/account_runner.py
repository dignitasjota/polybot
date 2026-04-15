"""Account runner: per-account orchestrator with multi-strategy support.

Fase 8 refactor: each account can host N strategies (directional, copy_trade,
future strategies). Strategies share one Executor (with dual ledger) and one
AccountContext. The runner provides centralized opportunity routing with
cross-strategy conflict resolution (opposite-side rejection) and persistence.

Backward compat: ``.detector``, ``.copy_trader``, ``.strategy_type`` properties
still work so main.py, web panel, and config_manager don't need changes yet.
"""

from __future__ import annotations

import asyncio
import time

import structlog

from src.config import AccountConfig, StrategyConfig as LegacyStrategyConfig, DataConfig, WebSocketConfig
from src.copy_trader import CopyTrader
from src.db import get_wallet_overrides
from src.detector import ClosingArbitrageDetector, Opportunity
from src.executor import Executor, ExecutionMode
from src.gamma_client import GammaClient
from src.market_tracker import MarketTracker
from src.strategies.base import AccountContext, PAPER_DAILY_TRADE_CAP, Strategy
from src.strategies.copy_trade import CopyTradeConfig, CopyTradeStrategy
from src.strategies.directional import DirectionalConfig, DirectionalStrategy
from src.strategies.liquidity import LiquidityConfig, LiquidityStrategy
from src.wallet_scanner import WalletScanner
from src.websocket_client import WebSocketClient


class AccountRunner:
    """Independent runner for a single trading account.

    Supports N strategies per account (currently 1 with the old TOML format,
    N after Fase 9 migrates the config).

    Each account has:
    - One Executor (with dual ledger: paper + live)
    - One AccountContext (read-only view for strategies)
    - N Strategy wrappers, each with its own mode (disabled/paper/live)
    - Shared market infrastructure for directional strategies

    Cross-strategy conflict resolution:
    - Same side (reinforcement): both strategies buy → allowed, both execute
    - Opposite side: one buys YES, another buys NO → rejected (the second one)
    """

    def __init__(
        self,
        account: AccountConfig,
        strategy: LegacyStrategyConfig,
        data: DataConfig,
        ws_config: WebSocketConfig,
        shared_tracker: MarketTracker | None = None,
        shared_ws: WebSocketClient | None = None,
        shared_gamma: GammaClient | None = None,
    ):
        self.account = account
        self.name = account.name
        self.log = structlog.get_logger(f"polymarket.account.{self.name}")

        # Execution mode (legacy: account-wide. Fase 8+: per-strategy)
        self.exec_mode = {
            "paper": ExecutionMode.PAPER,
            "dry_run": ExecutionMode.DRY_RUN,
            "live": ExecutionMode.LIVE,
        }.get(account.execution_mode, ExecutionMode.PAPER)

        # Single executor per account (shared across strategies)
        self.executor = Executor(
            account.risk,
            mode=self.exec_mode,
            account_name=account.name,
        )

        # AccountContext: read-only view for strategies (Fase 2)
        self.context = AccountContext(account.name, self.executor)

        # Strategy registry (Fase 8)
        self.strategies: dict[str, Strategy] = {}

        # Legacy: still construct the raw detector/copy_trader for backward
        # compat (main.py, web panel). They're wrapped in Strategy adapters.
        self._legacy_strategy_type = account.strategy_type
        self._legacy_detector: ClosingArbitrageDetector | None = None
        self._legacy_copy_trader: CopyTrader | None = None
        self.wallet_scanner = WalletScanner()

        # Market infrastructure (directional only)
        self.tracker: MarketTracker | None = None
        self.ws_client: WebSocketClient | None = None
        self.gamma: GammaClient | None = None
        self._owns_infra = False

        # Determine which strategies to create.
        # New format (Fase 9): account.strategies dict is populated.
        # Old format: single strategy from account.strategy_type.
        strategy_names = list(account.strategies.keys()) if account.strategies else [account.strategy_type]

        for strat_name in strategy_names:
            strat_raw = account.strategies.get(strat_name, {})
            # Per-strategy mode from new format, or fallback to account mode
            strat_mode = strat_raw.get("mode", account.execution_mode)
            if strat_mode == "dry_run":
                strat_mode = "paper"

            if strat_name == "directional":
                self._init_directional(
                    strategy, strat_mode, strat_raw,
                    shared_tracker, shared_ws, shared_gamma, ws_config,
                )
            elif strat_name == "copy_trade":
                self._init_copy_trade(account, strat_mode, strat_raw)
            elif strat_name == "liquidity":
                self._init_liquidity(strat_mode, strat_raw)

        self._running = False
        self._data_config = data
        self._strategy_config = strategy
        self._orphan_scan_task: asyncio.Task | None = None

    # ── Strategy construction helpers ─────────────────────────────────

    def _init_directional(
        self,
        legacy_strategy: LegacyStrategyConfig,
        mode_str: str,
        strat_raw: dict,
        shared_tracker, shared_ws, shared_gamma, ws_config,
    ):
        """Create directional strategy + infrastructure."""
        if shared_tracker and shared_ws:
            self.tracker = shared_tracker
            self.ws_client = shared_ws
            self.gamma = shared_gamma
        else:
            self.tracker = MarketTracker()
            self.ws_client = WebSocketClient(ws_config, self.tracker)
            self.gamma = GammaClient()
            self._owns_infra = True

        det = ClosingArbitrageDetector(
            legacy_strategy, self.tracker, self.account.risk,
            starting_balance=self.account.risk.simulated_balance,
        )
        self._legacy_detector = det

        dcfg = DirectionalConfig.from_legacy(legacy_strategy, mode=mode_str)
        # Override with new-format values if present
        for key in ("max_price", "min_buffer_pct", "min_margin_net", "tag",
                     "max_concurrent_bets", "priority"):
            if key in strat_raw:
                setattr(dcfg, key, strat_raw[key])

        dstrat = DirectionalStrategy(dcfg, self.context, det)
        self.strategies["directional"] = dstrat

    def _init_liquidity(self, mode_str: str, strat_raw: dict):
        """Create liquidity strategy (Phase 2: scanner + provider)."""
        lcfg = LiquidityConfig.from_dict(strat_raw, mode=mode_str)
        lstrat = LiquidityStrategy(
            lcfg,
            self.context,
            credentials=self.account.credentials,
            tracker=self.tracker,
        )
        self.strategies["liquidity"] = lstrat

    def _init_copy_trade(self, account: AccountConfig, mode_str: str, strat_raw: dict):
        """Create copy-trade strategy."""
        ct = CopyTrader(
            account.copy_trade,
            starting_balance=account.risk.simulated_balance,
        )
        self._legacy_copy_trader = ct

        ccfg = CopyTradeConfig.from_legacy(account.copy_trade, mode=mode_str)
        for key in ("fixed_bet_size", "poll_interval_ms", "max_latency_ms",
                     "min_price", "max_concurrent_bets", "spread_arb_multiplier",
                     "priority"):
            if key in strat_raw:
                setattr(ccfg, key, strat_raw[key])

        cstrat = CopyTradeStrategy(ccfg, self.context, ct)
        self.strategies["copy_trade"] = cstrat

    # ── Backward-compat properties ────────────────────────────────────

    @property
    def strategy_type(self) -> str:
        """Legacy: primary strategy type for this account."""
        return self._legacy_strategy_type

    @property
    def detector(self) -> ClosingArbitrageDetector | None:
        """Legacy: underlying detector (for main.py resolution checks etc.)."""
        dstrat = self.strategies.get("directional")
        if isinstance(dstrat, DirectionalStrategy):
            return dstrat.detector
        return self._legacy_detector

    @property
    def copy_trader(self) -> CopyTrader | None:
        """Legacy: underlying CopyTrader (for panel wallet overrides etc.)."""
        cstrat = self.strategies.get("copy_trade")
        if isinstance(cstrat, CopyTradeStrategy):
            return cstrat.copy_trader
        return self._legacy_copy_trader

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self):
        """Start this account's trading loop."""
        strategy_names = list(self.strategies.keys())
        self.log.info(
            "account_starting",
            strategies=strategy_names,
            mode=self.exec_mode.value,
            name=self.name,
        )
        self._running = True

        # Initialize executor
        if self.exec_mode != ExecutionMode.PAPER:
            self.executor._credentials = self.account.credentials
        await self.executor.initialize()

        # Fase 7: rehydrate persisted trades and restore strategy dedupe state
        try:
            restored = await self.executor.load_persisted_state()
        except Exception as e:
            self.log.warning("load_persisted_state_error", error=str(e))
            restored = 0
        if restored:
            for strat_name, strat in self.strategies.items():
                try:
                    await strat.restore_open_positions(self.executor._trades)
                except Exception as e:
                    self.log.warning(
                        "restore_open_positions_error",
                        strategy=strat_name,
                        error=str(e),
                    )

        # Register balance sync callback
        self.executor.on_balance_update(self._on_balance_changed)
        self._sync_live_balance()

        # In LIVE mode, set starting_balance to real balance for accurate ROI
        if self.exec_mode == ExecutionMode.LIVE:
            live_balance = self.executor.get_balance()
            if live_balance is not None:
                if self.detector:
                    self.detector._starting_balance = live_balance
                    self.detector._balance = live_balance
                if self.copy_trader:
                    self.copy_trader._starting_balance = live_balance
                    self.copy_trader._balance = live_balance

        # Live mode: orphan scan
        if self.exec_mode == ExecutionMode.LIVE:
            await self._do_orphan_scan("startup")
            if self._orphan_scan_task is None or self._orphan_scan_task.done():
                self._orphan_scan_task = asyncio.create_task(self._run_orphan_scan_loop())

        # Start each strategy with centralized opportunity routing
        for strat_name, strat in self.strategies.items():
            if strat_name == "directional":
                await self._start_directional(strat)
            elif strat_name == "copy_trade":
                await self._start_copy_trade(strat)
            else:
                # Future strategies: generic start
                if strat.is_active:
                    strat.on_opportunity(
                        lambda opp, sn=strat_name: asyncio.ensure_future(
                            self._handle_opportunity(opp, sn)
                        )
                    )
                    strat.on_redeem(self.executor.redeem_position)
                    await strat.start()

    async def _start_directional(self, strat: DirectionalStrategy):
        """Wire and start directional strategy."""
        det = strat.detector

        # Central opportunity routing (Fase 8): detector → runner → executor
        det.on_opportunity(
            lambda opp: asyncio.ensure_future(
                self._handle_opportunity(opp, "directional")
            )
        )
        det.on_redeem(self.executor.redeem_position)

        # WebSocket → detector (price updates trigger checks)
        if self.ws_client:
            self.ws_client.on_opportunity(det.check)

        # Executor → detector callbacks (balance sync, order tracking)
        self.executor.on_order_confirmed(det._on_executor_order_confirmed)
        self.executor.on_order_cancelled(det._on_executor_order_cancelled)
        self.executor.on_position_redeemed(det._on_executor_position_redeemed)

        # Start the strategy (price checker)
        await strat.start()

        self.log.info(
            "account_directional_ready",
            name=self.name,
            redeem_registered=bool(det._on_redeem_cb),
            mode=strat.config.mode,
        )

    async def _start_copy_trade(self, strat: CopyTradeStrategy):
        """Wire and start copy-trade strategy."""
        ct = strat.copy_trader

        # Load wallet overrides from DB
        overrides = await get_wallet_overrides()
        ct.set_wallet_overrides(overrides)

        # Central opportunity routing (Fase 8)
        ct.on_opportunity(
            lambda opp: asyncio.ensure_future(
                self._handle_opportunity(opp, "copy_trade")
            )
        )
        ct.on_redeem(self.executor.redeem_position)
        ct.on_trade(self.wallet_scanner.on_trade)
        ct.on_resolve(self.wallet_scanner.on_market_resolved_with_side)

        # Start the strategy (poll loop)
        await strat.start()

        self.log.info(
            "account_copy_trade_ready",
            name=self.name,
            wallets=len(self.account.copy_trade.target_wallets),
            redeem_registered=bool(ct._on_redeem_cb),
            mode=strat.config.mode,
        )

    # ── Central opportunity handler (Fase 8) ──────────────────────────

    async def _handle_opportunity(self, opp: Opportunity, source_strategy: str):
        """Central opportunity routing with cross-strategy conflict resolution.

        1. Tag the opportunity with source_strategy and mode
        2. Reject if opposite side exists (cross-strategy protection)
        3. Enforce paper daily trade cap (system hard limit)
        4. Execute via the shared executor
        5. Persist the opportunity record
        """
        # 1. Tag
        opp.source_strategy = source_strategy
        strategy = self.strategies.get(source_strategy)
        if strategy:
            opp.mode = strategy.config.mode
        else:
            opp.mode = self.exec_mode.value

        # Sanity
        if opp.suggested_bet <= 0:
            self._persist_opportunity_async(opp, "rejected_zero_bet", "")
            return

        # 2. Cross-strategy opposite side rejection
        has_opposite, from_strat = self.context.has_opposite_position(
            opp.condition_id, opp.token_side, opp.mode
        )
        if has_opposite:
            reason = f"opposite_side_from_{from_strat or 'unknown'}"
            self.log.info(
                "opposite_side_rejected",
                strategy=source_strategy,
                condition_id=opp.condition_id[:20],
                side=opp.token_side,
                conflicting_strategy=from_strat,
            )
            self._persist_opportunity_async(opp, "rejected_opposite_side", reason)
            return

        # 3. Paper daily trade cap (hard system limit)
        if opp.mode == "paper":
            count = self.context.count_paper_trades_today(source_strategy)
            if count >= PAPER_DAILY_TRADE_CAP:
                self.log.debug(
                    "paper_daily_cap_reached",
                    strategy=source_strategy,
                    count=count,
                )
                self._persist_opportunity_async(opp, "rejected_paper_cap", "")
                return

        # 4. Execute
        trade = await self.executor.execute(opp)

        # 5. Persist opportunity
        decision = "placed" if trade else "rejected_execution"
        self._persist_opportunity_async(opp, decision, "")

    def _persist_opportunity_async(self, opp: Opportunity, decision: str, reason: str):
        """Fire-and-forget persistence of an opportunity record."""
        try:
            from src.persistence import get_persistence
            persistence = get_persistence()
            asyncio.ensure_future(persistence.record_opportunity(
                account_name=self.name,
                source_strategy=getattr(opp, "source_strategy", "") or "unknown",
                mode=getattr(opp, "mode", "paper") or "paper",
                condition_id=opp.condition_id,
                question=getattr(opp, "question", "") or "",
                token_side=opp.token_side,
                token_id=opp.token_id,
                token_price=opp.token_price,
                margin_net=getattr(opp, "margin_net", 0.0),
                suggested_bet=getattr(opp, "suggested_bet", 0.0),
                decision=decision,
                decision_reason=reason,
            ))
        except Exception:
            pass  # Persistence not available

    # ── Per-strategy mode changes ─────────────────────────────────────

    async def set_strategy_mode(self, strategy_name: str, new_mode: str):
        """Change mode of a single strategy (disabled/paper/live).

        This is the Fase 8+ interface. The legacy ``set_execution_mode``
        below still works and delegates to this method.
        """
        strat = self.strategies.get(strategy_name)
        if not strat:
            self.log.warning("set_strategy_mode_unknown", strategy=strategy_name)
            return

        old_mode = strat.config.mode
        if old_mode == new_mode:
            return

        # If switching ANY strategy to live, ensure executor is initialized
        switching_to_live = new_mode == "live" and self.exec_mode == ExecutionMode.PAPER
        if switching_to_live:
            self.executor._credentials = self.account.credentials
            self.exec_mode = ExecutionMode.LIVE
            self.account.execution_mode = "live"
            await self.executor.set_mode(ExecutionMode.LIVE)

        await strat.set_mode(new_mode)

        # Reset stats when switching modes (same as set_execution_mode)
        if new_mode == "live" and old_mode != "live":
            live_balance = self.executor.get_balance()
            self.executor.reset_trades()
            if self.detector:
                self.detector.reset_stats(new_balance=live_balance)
            if self.copy_trader:
                self.copy_trader.reset_stats(new_balance=live_balance)
            self._sync_live_balance()
            self.log.info(
                "stats_reset_for_live",
                account=self.name,
                strategy=strategy_name,
                live_balance=f"${live_balance:.2f}" if live_balance is not None else "unknown",
            )
            # Start orphan scan if not already running
            if self._orphan_scan_task is None or self._orphan_scan_task.done():
                await self._do_orphan_scan("strategy_mode_change")
                self._orphan_scan_task = asyncio.create_task(self._run_orphan_scan_loop())
        elif new_mode in ("paper", "disabled") and old_mode == "live":
            sim_balance = self.account.risk.simulated_balance
            self.executor.reset_trades()
            if self.detector:
                self.detector.reset_stats(new_balance=sim_balance)
            if self.copy_trader:
                self.copy_trader.reset_stats(new_balance=sim_balance)

        # If all strategies are paper/disabled, downgrade executor to paper
        any_live = any(
            s.config.mode == "live" for s in self.strategies.values()
        )
        if not any_live and self.exec_mode == ExecutionMode.LIVE:
            self.exec_mode = ExecutionMode.PAPER
            self.account.execution_mode = "paper"

        self.log.info(
            "strategy_mode_changed",
            account=self.name,
            strategy=strategy_name,
            old=old_mode,
            new=new_mode,
        )

    async def set_execution_mode(self, mode_str: str):
        """Legacy: change execution mode for ALL strategies at once.

        Kept for backward compat with the web panel Settings page.
        Delegates to set_strategy_mode for each strategy.
        """
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

        # Map ExecutionMode to strategy mode string
        strat_mode = "paper" if mode == ExecutionMode.PAPER else mode_str

        # Update all strategies
        for strat_name, strat in self.strategies.items():
            strat.config.mode = strat_mode

        # Reset stats when switching modes
        if mode in (ExecutionMode.LIVE, ExecutionMode.DRY_RUN):
            live_balance = self.executor.get_balance()
            self.executor.reset_trades()
            if self.detector:
                self.detector.reset_stats(new_balance=live_balance)
            if self.copy_trader:
                self.copy_trader.reset_stats(new_balance=live_balance)
            self._sync_live_balance()
            self.log.info(
                "stats_reset_for_live",
                account=self.name,
                live_balance=f"${live_balance:.2f}" if live_balance is not None else "unknown",
            )
            # Orphan scan
            if mode == ExecutionMode.LIVE:
                await self._do_orphan_scan("mode_change")
                if self._orphan_scan_task is None or self._orphan_scan_task.done():
                    self._orphan_scan_task = asyncio.create_task(self._run_orphan_scan_loop())
        else:
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

    # ── Balance sync ──────────────────────────────────────────────────

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

    # ── Orphan scan ───────────────────────────────────────────────────

    async def _do_orphan_scan(self, context: str):
        """Run a single orphan scan and log."""
        try:
            triggered = await self.executor.scan_and_redeem_orphan_positions()
            self.log.info(
                f"orphan_scan_{context}",
                account=self.name,
                triggered=triggered,
            )
        except Exception as e:
            self.log.warning(
                f"orphan_scan_{context}_error",
                account=self.name,
                error=str(e),
            )

    async def _run_orphan_scan_loop(self):
        """Periodically scan for orphan redeemable positions."""
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

    # ── Shutdown ──────────────────────────────────────────────────────

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

        # Stop all strategies
        for strat_name, strat in self.strategies.items():
            try:
                await strat.stop()
            except Exception as e:
                self.log.warning("strategy_stop_error", strategy=strat_name, error=str(e))

        if self._owns_infra:
            if self.ws_client:
                await self.ws_client.stop()
            if self.gamma:
                await self.gamma.close()

    # ── Stats & export ────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Get combined stats for this account."""
        stats = {
            "account": self.name,
            "strategy": self.strategy_type,  # Legacy compat
            "mode": self.exec_mode.value,
            "executor": self.executor.get_stats(),
            "strategies": {},
        }
        # Per-strategy stats
        for strat_name, strat in self.strategies.items():
            try:
                stats["strategies"][strat_name] = strat.get_stats()
            except Exception:
                stats["strategies"][strat_name] = {}

        # Legacy compat: keep detector/copy_trader keys
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
