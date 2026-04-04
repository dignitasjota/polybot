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
                shared_tracker=self.tracker if acc_cfg.strategy_type == "directional" else None,
                shared_ws=self.ws_client if acc_cfg.strategy_type == "directional" else None,
                shared_gamma=self.gamma if acc_cfg.strategy_type == "directional" else None,
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

        # Start all account runners
        for acc in self.accounts:
            await acc.start()

        # Initial market discovery (for directional accounts)
        has_directional = any(a.strategy_type == "directional" for a in self.accounts)
        if has_directional:
            await self._discover_markets()

            if not self.tracker.all_token_ids:
                self.log.warning("no_markets_found", msg="No markets match criteria, will retry...")

        # Build task list
        tasks = [
            self._run_stats_reporter(),
            self._run_web_server(),
        ]
        if has_directional:
            tasks.extend([
                self._run_websocket(),
                self._run_gamma_poller(),
                self._run_resolution_checker(),
                self._run_market_cleanup(),
                self._run_data_exporter(),
            ])

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
                active_markets = len(self.tracker.all_markets)
                stale = sum(1 for m in self.tracker.all_markets if m.is_stale)
                resolved = sum(1 for m in self.tracker.all_markets if m.resolved)
                self.log.info(
                    "market_stats",
                    markets_active=active_markets,
                    markets_stale=stale,
                    markets_resolved=resolved,
                    ws_connected=self.ws_client.is_connected,
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
