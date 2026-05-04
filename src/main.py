from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from pathlib import Path

import structlog

from aiohttp import web

from src.account_runner import AccountRunner
from src.config import Config
from src.detector import ClosingArbitrageDetector
from src.executor import Executor, ExecutionMode
from src.gamma_client import GammaClient
from src.logger import setup_logging
from src.market_tracker import MarketTracker
from src.web import create_app
from src.websocket_client import WebSocketClient


class Bot:
    """Main bot orchestrator — supports multiple independent accounts.

    Each account runs its own strategy (directional or copy-trade) with
    its own executor, risk limits, and credentials.

    Directional accounts share market infrastructure (tracker, WS, Gamma)
    to avoid duplicate connections. Copy-trade accounts are fully independent.
    """

    def __init__(self, config_path: str = "config/config.toml"):
        self.config = Config.load(config_path)
        self.log = setup_logging(self.config.logging)

        # Shared market infrastructure (used by all directional accounts)
        self.tracker = MarketTracker()
        self.gamma = GammaClient()
        self.ws_client = WebSocketClient(self.config.websocket, self.tracker)

        # Account runners
        self.accounts: list[AccountRunner] = []
        for acc_cfg in self.config.accounts:
            if not acc_cfg.enabled:
                continue
            runner = AccountRunner(
                account=acc_cfg,
                strategy=self.config.strategy,
                data=self.config.data,
                ws_config=self.config.websocket,
                shared_tracker=self.tracker if acc_cfg.strategy_type in ("directional", "completeness") else None,
                shared_ws=self.ws_client if acc_cfg.strategy_type in ("directional", "completeness") else None,
                shared_gamma=self.gamma if acc_cfg.strategy_type in ("directional", "completeness") else None,
            )
            self.accounts.append(runner)

        # Keep references to the first directional account's detector for
        # backward compat (web dashboard, data export, resolution checker)
        self.detector: ClosingArbitrageDetector | None = None
        self.executor: Executor | None = None
        for acc in self.accounts:
            if acc.detector:
                self.detector = acc.detector
                self.executor = acc.executor
                break

        self._running = False
        self._stats_interval = 60

    async def start(self):
        account_names = [a.name for a in self.accounts]
        self.log.info(
            "bot_starting",
            accounts=account_names,
            strategy=self.config.strategy.name,
            max_time=str(self.config.strategy.max_time_to_resolution),
            max_markets=self.config.data.max_markets_monitored,
        )

        self._running = True

        # Ensure DB schema is up to date before anything reads it
        from src.db import init_db
        await init_db()

        has_directional = any(a.strategy_type == "directional" for a in self.accounts)
        has_completeness = any(a.strategy_type == "completeness" for a in self.accounts)
        has_shared_infra = has_directional or has_completeness

        # Build task list — web server starts FIRST, before slow HTTP init
        tasks = [
            self._run_web_server(),
            self._run_init_and_loops(has_directional, has_completeness, has_shared_infra),
        ]

        await asyncio.gather(*tasks)

    async def _run_init_and_loops(self, has_directional: bool, has_completeness: bool, has_shared_infra: bool):
        """Initialize accounts and start background loops.

        Runs after the web server is already listening, so slow HTTP calls
        (reward scanner, Gamma API) don't block the panel.
        """
        # Start all account runners (may do HTTP: reward scanner, etc.)
        for acc in self.accounts:
            try:
                await acc.start()
            except Exception as e:
                self.log.error("account_start_error", account=acc.name, error=str(e))

        # Initial market discovery (for directional accounts)
        if has_directional:
            try:
                await self._discover_markets()
            except Exception as e:
                self.log.error("initial_discover_error", error=str(e))

            if not self.tracker.all_token_ids:
                self.log.warning("no_markets_found", msg="No markets match criteria, will retry...")

        # Initial broad market discovery (for completeness — all categories)
        if has_completeness:
            try:
                await self._discover_completeness_markets()
            except Exception as e:
                self.log.error("initial_completeness_discover_error", error=str(e))

        # Background loops
        tasks = [
            self._run_stats_reporter(),
            self._run_snapshot_loop(),   # Fase 10: periodic stats snapshots
            self._run_cleanup_loop(),    # Fase 10: monthly data cleanup
            self._run_backup_loop(),     # Fase 13: DB backups every 6h
        ]
        if has_shared_infra:
            tasks.extend([
                self._run_websocket(),
                self._run_resolution_checker(),
                self._run_market_cleanup(),
                self._run_data_exporter(),
            ])
        if has_directional:
            tasks.append(self._run_gamma_poller())
        if has_completeness:
            tasks.append(self._run_completeness_gamma_poller())

        await asyncio.gather(*tasks)

    async def stop(self):
        self.log.info("bot_stopping")
        self._running = False

        # Stop all accounts
        for acc in self.accounts:
            await acc.stop()

        await self.ws_client.stop()
        await self.gamma.close()
        self._export_data()

        stats = {a.name: a.get_stats() for a in self.accounts}
        self.log.info("bot_stopped", account_stats=stats)

    async def _run_websocket(self):
        """WebSocket connection with auto-reconnection."""
        while self._running:
            try:
                await self.ws_client.start()
            except Exception as e:
                self.log.error("ws_fatal_error", error=str(e))
                if self._running:
                    await asyncio.sleep(5)

    async def _run_gamma_poller(self):
        """Periodically discover new markets via Gamma API."""
        while self._running:
            await asyncio.sleep(self.config.data.gamma_poll_interval_seconds)
            if not self._running:
                break
            try:
                await self._discover_markets()
            except Exception as e:
                self.log.error("gamma_poll_error", error=str(e))

    async def _run_resolution_checker(self):
        """Periodically check if tracked markets have resolved via Gamma API."""
        check_interval = 30
        while self._running:
            await asyncio.sleep(check_interval)
            if not self._running:
                break
            try:
                candidates = [
                    m.condition_id
                    for m in self.tracker.all_markets
                    if not m.resolved
                    and m.end_date is not None
                    and m.hours_to_resolution is not None
                    and m.hours_to_resolution <= 0.1
                ]
                if not candidates:
                    continue

                self.log.debug(
                    "resolution_check_candidates",
                    count=len(candidates),
                    condition_ids=[c[:16] for c in candidates],
                )

                resolved = await self.gamma.check_resolution(candidates)
                for condition_id, winning_token_id in resolved.items():
                    self.tracker.mark_resolved(condition_id, winning_token_id)

                if resolved:
                    self.log.info("resolution_check", resolved=len(resolved), checked=len(candidates))
                    # Trigger all directional detectors to settle
                    for acc in self.accounts:
                        if acc.detector:
                            await acc.detector.check("", "resolution_check")
                elif candidates:
                    self.log.debug("resolution_check_none_resolved", checked=len(candidates))

            except Exception as e:
                self.log.error("resolution_check_error", error=str(e))

    async def _run_market_cleanup(self):
        """Periodically remove expired/resolved markets to free memory."""
        cleanup_interval = 120
        keep_resolved_seconds = 600

        while self._running:
            await asyncio.sleep(cleanup_interval)
            if not self._running:
                break
            try:
                now_ts = time.time()
                to_remove = []
                for m in self.tracker.all_markets:
                    if m.resolved and m.last_update > 0:
                        if (now_ts - m.last_update) > keep_resolved_seconds:
                            to_remove.append(m.condition_id)
                            continue
                    if m.end_date is not None and m.hours_to_resolution == 0.0:
                        time_since_end = now_ts - m.end_date.timestamp()
                        if time_since_end > 900:
                            to_remove.append(m.condition_id)

                for cid in to_remove:
                    self.tracker.remove_market(cid)
                    for acc in self.accounts:
                        if acc.detector:
                            acc.detector.cleanup_market(cid)

                if to_remove:
                    self.log.info(
                        "market_cleanup",
                        removed=len(to_remove),
                        remaining=len(self.tracker.all_markets),
                    )
                    try:
                        await self.ws_client.resubscribe()
                    except Exception as e:
                        self.log.warning("ws_resubscribe_failed_cleanup", error=str(e))

            except Exception as e:
                self.log.error("market_cleanup_error", error=str(e))

    async def _run_stats_reporter(self):
        """Periodically log statistics for all accounts."""
        while self._running:
            await asyncio.sleep(self._stats_interval)
            if not self._running:
                break

            # Per-account stats
            for acc in self.accounts:
                stats = acc.get_stats()
                self.log.info("account_stats", **stats)

            # Shared market stats (if directional accounts exist)
            if self.tracker:
                all_markets = self.tracker.all_markets
                active_markets = len(all_markets)
                stale = sum(1 for m in all_markets if m.is_stale)
                resolved = sum(1 for m in all_markets if m.resolved)
                fresh = active_markets - stale - resolved
                with_prices = sum(
                    1 for m in all_markets
                    if (m.best_ask_yes or 0) > 0 or (m.best_ask_no or 0) > 0
                )
                self.log.info(
                    "market_stats",
                    markets_active=active_markets,
                    markets_fresh=fresh,
                    markets_stale=stale,
                    markets_resolved=resolved,
                    markets_with_prices=with_prices,
                    ws_connected=self.ws_client.is_connected,
                )

                # Diagnostic heartbeat: detector/price_checker state per directional account
                now_ts = time.time()
                for acc in self.accounts:
                    if not acc.detector:
                        continue
                    det = acc.detector
                    pc = det._price_checker
                    pc_stale = (now_ts - pc._last_update) if pc._last_update > 0 else -1
                    self.log.info(
                        "heartbeat_diagnostic",
                        account=acc.name,
                        mode=acc.execution_mode if hasattr(acc, "execution_mode") else "?",
                        bets_pending=sum(1 for o in det._bet_placed.values() if o.outcome == "pending"),
                        bets_total=len(det._bet_placed),
                        opportunities_logged=len(det._opportunities_log),
                        last_check_price_entries=len(det._last_check_price),
                        last_log_time_entries=len(det._last_log_time),
                        settled_conditions=len(det._settled_conditions),
                        active_opportunities=len(det._active_opportunities),
                        pc_active_symbols=len(pc._active_symbols),
                        pc_current_prices=len(pc._current_prices),
                        pc_pending_opens=len(pc._pending_open_requests),
                        pc_open_prices=len(pc._open_prices),
                        pc_parse_cache=len(pc._parse_cache),
                        pc_seconds_since_update=round(pc_stale, 1),
                        total_scans=det._stats.get("total_scans", 0),
                    )

    async def _run_web_server(self):
        """Run the web dashboard."""
        app = create_app(self)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", 8080)
        await site.start()
        self.log.info("web_server_started", port=8080)

        while self._running:
            await asyncio.sleep(1)

        await runner.cleanup()

    async def _run_data_exporter(self):
        """Periodically export opportunity data to file."""
        export_interval = 300
        while self._running:
            await asyncio.sleep(export_interval)
            if not self._running:
                break
            self._export_data()

    async def _discover_markets(self):
        """Fetch markets from Gamma API and subscribe to them."""
        markets = await self.gamma.fetch_active_markets(
            max_time_to_resolution=self.config.strategy.max_time_to_resolution,
            max_results=self.config.data.max_markets_monitored,
            tag=self.config.strategy.tag,
        )

        new_count = 0
        for market in markets[: self.config.data.max_markets_monitored]:
            existing = self.tracker.get_by_condition(market.condition_id)
            if existing:
                continue

            self.tracker.add_market(
                condition_id=market.condition_id,
                question=market.question,
                yes_token_id=market.yes_token_id,
                no_token_id=market.no_token_id,
                end_date=market.end_date,
                tags=market.tags,
            )
            new_count += 1

            self.log.info(
                "market_added",
                question=market.question[:80],
                condition_id=market.condition_id[:16],
                yes_price=f"${market.yes_price:.2f}",
                no_price=f"${market.no_price:.2f}",
                end_date=str(market.end_date),
            )

        if new_count > 0:
            self.log.info(
                "markets_discovered",
                new=new_count,
                total=len(self.tracker.all_markets),
            )
            try:
                await self.ws_client.resubscribe()
            except Exception as e:
                self.log.warning("ws_resubscribe_failed", error=str(e),
                                msg="New markets won't receive WS prices until reconnect")

    async def _discover_completeness_markets(self):
        """Fetch markets from ALL categories for completeness arbitrage.

        Unlike _discover_markets() (crypto-only, short time-to-resolution),
        this fetches broadly: no tag filter, longer time horizon.
        Completeness gaps can appear in any category, especially in low-fee
        categories like geopolitics (0%) and sports (3%).
        """
        from datetime import timedelta

        markets = await self.gamma.fetch_active_markets(
            max_time_to_resolution=timedelta(hours=48),
            max_results=200,
            tag="",  # No tag filter — all categories
        )

        new_count = 0
        for market in markets[:200]:
            existing = self.tracker.get_by_condition(market.condition_id)
            if existing:
                continue

            self.tracker.add_market(
                condition_id=market.condition_id,
                question=market.question,
                yes_token_id=market.yes_token_id,
                no_token_id=market.no_token_id,
                end_date=market.end_date,
                tags=market.tags,
            )
            new_count += 1

        if new_count > 0:
            self.log.info(
                "completeness_markets_discovered",
                new=new_count,
                total=len(self.tracker.all_markets),
            )
            try:
                await self.ws_client.resubscribe()
            except Exception as e:
                self.log.warning("ws_resubscribe_failed", error=str(e))

    async def _run_completeness_gamma_poller(self):
        """Periodically discover new markets for completeness (all categories)."""
        while self._running:
            await asyncio.sleep(120)  # Every 2 minutes (lightweight HTTP)
            if not self._running:
                break
            try:
                await self._discover_completeness_markets()
            except Exception as e:
                self.log.error("completeness_gamma_poll_error", error=str(e))

    async def _run_snapshot_loop(self):
        """Record stats snapshots every 5 minutes per account per strategy."""
        from src.persistence import get_persistence

        while self._running:
            await asyncio.sleep(300)  # 5 min
            if not self._running:
                break
            try:
                persistence = get_persistence()
                if persistence is None:
                    continue

                for acc in self.accounts:
                    stats = acc.get_stats()
                    strategies = stats.get("strategies", {})
                    executor_stats = stats.get("executor", {})

                    for strat_name, strat_stats in strategies.items():
                        # Determine mode from strategy config
                        strat_obj = acc.strategies.get(strat_name)
                        if not strat_obj:
                            continue
                        mode = strat_obj.config.mode if hasattr(strat_obj, "config") else "paper"
                        if mode == "disabled":
                            continue

                        # Get balance from the appropriate ledger
                        if mode == "live":
                            balance = executor_stats.get("live", {}).get("balance", 0.0)
                        else:
                            balance = executor_stats.get("paper", {}).get("balance", 0.0)

                        trades_count = strat_stats.get("total_bets", strat_stats.get("total_scans", 0))

                        await persistence.snapshot_stats(
                            account_name=acc.name,
                            source_strategy=strat_name,
                            mode=mode,
                            balance=balance,
                            daily_pnl=0.0,
                            total_pnl=strat_stats.get("total_pnl", 0.0),
                            trades_count=trades_count,
                            wins=strat_stats.get("wins", 0),
                            losses=strat_stats.get("losses", 0),
                            pending=strat_stats.get("pending", 0),
                            open_positions=strat_stats.get("open_positions", 0),
                            opportunities_detected=strat_stats.get("opportunities_detected", strat_stats.get("total_scans", 0)),
                            opportunities_placed=strat_stats.get("opportunities_placed", strat_stats.get("total_bets", 0)),
                        )

                self.log.debug("snapshots_recorded", accounts=len(self.accounts))

            except Exception as e:
                self.log.error("snapshot_error", error=str(e))

    async def _run_cleanup_loop(self):
        """Cleanup old data: live >1 year, paper >90 days. Runs monthly."""
        from src.persistence import get_persistence

        # Wait 1 hour before first run
        await asyncio.sleep(3600)

        while self._running:
            try:
                persistence = get_persistence()
                if persistence:
                    deleted = await persistence.cleanup_old_data()
                    await persistence.vacuum()
                    self.log.info(
                        "maintenance_completed",
                        deleted_records=deleted if isinstance(deleted, int) else 0,
                    )
            except Exception as e:
                self.log.error("maintenance_error", error=str(e))

            # Sleep ~30 days before next run
            for _ in range(30 * 24):
                if not self._running:
                    return
                await asyncio.sleep(3600)

    async def _run_backup_loop(self):
        """Backup SQLite DB every 6 hours, retain 14 days."""
        import sqlite3
        import shutil
        from datetime import datetime, timedelta

        backup_dir = Path("data/backups")
        backup_dir.mkdir(parents=True, exist_ok=True)

        while self._running:
            # Sleep first (6 hours)
            for _ in range(6 * 60):
                if not self._running:
                    return
                await asyncio.sleep(60)

            if not self._running:
                break

            try:
                now = datetime.now()
                backup_path = backup_dir / f"panel_{now.strftime('%Y%m%d_%H%M%S')}.db"

                # Safe copy using sqlite3.backup()
                src_conn = sqlite3.connect("data/panel.db")
                dst_conn = sqlite3.connect(str(backup_path))
                with dst_conn:
                    src_conn.backup(dst_conn)
                src_conn.close()
                dst_conn.close()

                self.log.info("backup_completed", path=str(backup_path))

                # Cleanup old backups (>14 days)
                cutoff = now - timedelta(days=14)
                for old_backup in backup_dir.glob("panel_*.db"):
                    try:
                        if datetime.fromtimestamp(old_backup.stat().st_mtime) < cutoff:
                            old_backup.unlink()
                            self.log.info("backup_deleted", path=str(old_backup))
                    except Exception:
                        pass

            except Exception as e:
                self.log.error("backup_error", error=str(e))

    def _export_data(self):
        """Export full report per account to separate JSON files."""
        export_dir = Path("data")
        export_dir.mkdir(parents=True, exist_ok=True)

        for acc in self.accounts:
            report = acc.export_full_report()
            if not report:
                continue

            # Use bets key (copy_trade) or opportunities key (directional)
            items = report.get("bets", report.get("opportunities", []))
            if not items:
                continue

            export_path = export_dir / f"{acc.name}.json"
            with open(export_path, "w") as f:
                json.dump(report, f, indent=2)

            self.log.info(
                "data_exported",
                account=acc.name,
                path=str(export_path),
                bets=report["summary"].get("total_bets", len(items)),
                settled=report["summary"].get("settled", 0),
                pnl=report["summary"].get("total_pnl", 0),
            )


async def main():
    bot = Bot()

    # Handle graceful shutdown
    loop = asyncio.get_running_loop()

    def shutdown_handler():
        asyncio.ensure_future(bot.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_handler)

    try:
        await bot.start()
    except KeyboardInterrupt:
        pass
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
