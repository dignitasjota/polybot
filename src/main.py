from __future__ import annotations

import asyncio
import json
import signal
import time
from pathlib import Path

import structlog

from aiohttp import web

from src.config import Config
from src.detector import ClosingArbitrageDetector
from src.gamma_client import GammaClient
from src.logger import setup_logging
from src.market_tracker import MarketTracker
from src.web import create_app
from src.websocket_client import WebSocketClient


class Bot:
    """Main bot orchestrator for the Closing Arbitrage observer (Phase 1.1)."""

    def __init__(self, config_path: str = "config/config.toml"):
        self.config = Config.load(config_path)
        self.log = setup_logging(self.config.logging)
        self.tracker = MarketTracker()
        self.gamma = GammaClient()
        self.ws_client = WebSocketClient(self.config.websocket, self.tracker)
        self.detector = ClosingArbitrageDetector(self.config.strategy, self.tracker)
        self._running = False
        self._stats_interval = 60  # Log stats every 60 seconds

    async def start(self):
        tiers_str = " | ".join(
            f"<{t.max_hours}h:{t.min_probability}" for t in self.config.strategy.probability_tiers
        )
        self.log.info(
            "bot_starting",
            strategy=self.config.strategy.name,
            probability_tiers=tiers_str,
            max_time=str(self.config.strategy.max_time_to_resolution),
            max_markets=self.config.data.max_markets_monitored,
        )

        self._running = True

        # Register the detector callback on the WebSocket
        self.ws_client.on_opportunity(self.detector.check)

        # Initial market discovery
        await self._discover_markets()

        if not self.tracker.all_token_ids:
            self.log.warning("no_markets_found", msg="No markets match criteria, will retry...")

        # Run all tasks concurrently
        await asyncio.gather(
            self._run_websocket(),
            self._run_gamma_poller(),
            self._run_stats_reporter(),
            self._run_data_exporter(),
            self._run_web_server(),
        )

    async def stop(self):
        self.log.info("bot_stopping")
        self._running = False
        await self.ws_client.stop()
        await self.gamma.close()
        self._export_data()
        self.log.info("bot_stopped", stats=self.detector.get_stats())

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

    async def _run_stats_reporter(self):
        """Periodically log statistics."""
        while self._running:
            await asyncio.sleep(self._stats_interval)
            if not self._running:
                break

            stats = self.detector.get_stats()
            active_markets = len(self.tracker.all_markets)
            stale = sum(1 for m in self.tracker.all_markets if m.is_stale)
            resolved = sum(1 for m in self.tracker.all_markets if m.resolved)

            self.log.info(
                "stats",
                markets_active=active_markets,
                markets_stale=stale,
                markets_resolved=resolved,
                ws_connected=self.ws_client.is_connected,
                **stats,
            )

            # Log current market prices
            for market in self.tracker.all_markets:
                if market.is_stale:
                    continue
                self.log.debug(
                    "market_price",
                    question=market.question[:60],
                    yes_ask=f"${market.best_ask_yes:.4f}",
                    no_ask=f"${market.best_ask_no:.4f}",
                    spread_sum=f"${market.spread_sum:.4f}",
                    resolved=market.resolved,
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
        export_interval = 300  # Every 5 minutes
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
            # Resubscribe WebSocket with new tokens
            try:
                await self.ws_client.resubscribe()
            except Exception:
                pass  # Will resubscribe on next reconnect

    def _export_data(self):
        """Export opportunities to a JSON file for analysis."""
        opportunities = self.detector.export_opportunities()
        if not opportunities:
            return

        export_path = Path("data/opportunities.json")
        export_path.parent.mkdir(parents=True, exist_ok=True)

        with open(export_path, "w") as f:
            json.dump(
                {
                    "exported_at": time.time(),
                    "stats": self.detector.get_stats(),
                    "opportunities": opportunities,
                },
                f,
                indent=2,
            )

        self.log.info("data_exported", path=str(export_path), count=len(opportunities))


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
