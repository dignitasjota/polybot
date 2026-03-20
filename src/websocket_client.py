from __future__ import annotations

import asyncio
import json
import random
import time

import structlog
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from src.config import WebSocketConfig
from src.market_tracker import MarketTracker

WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

logger = structlog.get_logger("polymarket.websocket")


class WebSocketClient:
    """WebSocket client for the Polymarket CLOB Market channel."""

    def __init__(self, config: WebSocketConfig, tracker: MarketTracker):
        self.config = config
        self.tracker = tracker
        self._ws = None
        self._running = False
        self._connected = False
        self._reconnect_attempt = 0
        self._last_pong: float = 0
        self._on_opportunity_callback = None

    def on_opportunity(self, callback):
        """Register a callback for when opportunity-relevant data arrives."""
        self._on_opportunity_callback = callback

    async def start(self):
        """Start the WebSocket connection with auto-reconnection."""
        self._running = True
        while self._running:
            try:
                await self._connect_and_listen()
            except (ConnectionClosed, InvalidStatus, OSError) as e:
                self._connected = False
                if not self._running:
                    break
                delay = self._get_reconnect_delay()
                logger.warning(
                    "ws_disconnected",
                    error=str(e),
                    reconnect_in_ms=delay,
                    attempt=self._reconnect_attempt,
                )
                await asyncio.sleep(delay / 1000)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                if not self._running:
                    break
                logger.error("ws_unexpected_error", error=str(e), type=type(e).__name__)
                await asyncio.sleep(2)

    async def stop(self):
        """Stop the WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def _connect_and_listen(self):
        token_ids = self.tracker.all_token_ids
        if not token_ids:
            logger.info("ws_no_markets", msg="No markets to subscribe, waiting...")
            await asyncio.sleep(5)
            return

        logger.info("ws_connecting", url=WS_MARKET_URL, tokens=len(token_ids))

        async with connect(WS_MARKET_URL, ping_interval=None, close_timeout=5) as ws:
            self._ws = ws
            self._connected = True
            self._reconnect_attempt = 0

            # Subscribe
            subscribe_msg = {
                "assets_ids": token_ids,
                "type": "market",
                "custom_feature_enabled": True,
            }
            await ws.send(json.dumps(subscribe_msg))
            logger.info("ws_subscribed", tokens=len(token_ids))

            # Run ping and listen concurrently
            ping_task = asyncio.create_task(self._ping_loop(ws))
            listen_task = asyncio.create_task(self._listen(ws))

            try:
                done, pending = await asyncio.wait(
                    [ping_task, listen_task],
                    return_when=asyncio.FIRST_EXCEPTION,
                )
                for task in pending:
                    task.cancel()
                # Re-raise any exception from completed tasks
                for task in done:
                    task.result()
            except asyncio.CancelledError:
                ping_task.cancel()
                listen_task.cancel()
                raise

    async def _ping_loop(self, ws):
        """Send PING every N seconds."""
        while self._running:
            try:
                await ws.send("PING")
                await asyncio.sleep(self.config.ping_interval_seconds)
            except ConnectionClosed:
                break

    async def _listen(self, ws):
        """Listen for incoming messages."""
        async for raw_msg in ws:
            if not self._running:
                break

            if isinstance(raw_msg, bytes):
                raw_msg = raw_msg.decode("utf-8", errors="replace")

            if raw_msg == "PONG":
                self._last_pong = time.time()
                continue

            try:
                data = json.loads(raw_msg)
            except json.JSONDecodeError:
                logger.debug("ws_invalid_json", msg=raw_msg[:200])
                continue

            await self._handle_message(data)

    async def _handle_message(self, data: dict | list):
        """Route WebSocket messages to the appropriate handler."""
        events = data if isinstance(data, list) else [data]

        for event in events:
            if not isinstance(event, dict):
                continue

            event_type = event.get("event_type", "")
            asset_id = event.get("asset_id", "")

            if event_type == "book":
                self._handle_book(event)
            elif event_type == "price_change":
                self._handle_price_change(event)
            elif event_type == "best_bid_ask":
                self._handle_best_bid_ask(event)
            elif event_type == "last_trade_price":
                self._handle_last_trade(event)
            elif event_type == "market_resolved":
                self._handle_market_resolved(event)
            elif event_type == "tick_size_change":
                logger.info(
                    "tick_size_change",
                    asset_id=asset_id,
                    old=event.get("old_tick_size"),
                    new=event.get("new_tick_size"),
                )

            # Notify detector after any price-relevant update
            if event_type in (
                "book", "price_change", "best_bid_ask",
                "last_trade_price", "market_resolved",
            ):
                if self._on_opportunity_callback:
                    await self._on_opportunity_callback(asset_id, event_type)

    def _handle_book(self, event: dict):
        asset_id = event.get("asset_id", "")
        bids = event.get("bids", [])
        asks = event.get("asks", [])
        self.tracker.update_book(asset_id, bids, asks)

        state = self.tracker.get_by_token(asset_id)
        if state:
            side = "YES" if asset_id == state.yes_token_id else "NO"
            logger.debug(
                "book_snapshot",
                asset_id=asset_id[:16],
                side=side,
                bids=len(bids),
                asks=len(asks),
            )

    def _handle_price_change(self, event: dict):
        changes = event.get("price_changes", event.get("changes", []))
        if not changes and "asset_id" in event:
            changes = [event]

        for change in changes:
            asset_id = change.get("asset_id", "")
            state = self.tracker.get_by_token(asset_id)
            if not state:
                continue

            best_bid = change.get("best_bid")
            best_ask = change.get("best_ask")
            if best_bid is not None and best_ask is not None:
                self.tracker.update_best_bid_ask(
                    asset_id, float(best_bid), float(best_ask)
                )

    def _handle_best_bid_ask(self, event: dict):
        asset_id = event.get("asset_id", "")
        best_bid = event.get("best_bid")
        best_ask = event.get("best_ask")
        if best_bid is not None and best_ask is not None:
            self.tracker.update_best_bid_ask(asset_id, float(best_bid), float(best_ask))

    def _handle_last_trade(self, event: dict):
        asset_id = event.get("asset_id", "")
        price = event.get("price")
        if price is not None:
            self.tracker.update_last_trade(asset_id, float(price))

    def _handle_market_resolved(self, event: dict):
        condition_id = event.get("market", event.get("condition_id", ""))
        winning_asset_id = event.get("winning_asset_id", "")

        if condition_id and winning_asset_id:
            self.tracker.mark_resolved(condition_id, winning_asset_id)
        else:
            logger.warning(
                "market_resolved_incomplete",
                event_keys=list(event.keys()),
            )

    def _get_reconnect_delay(self) -> int:
        """Calculate reconnect delay with exponential backoff and jitter."""
        self._reconnect_attempt += 1
        delays = [0, 100, 500, 1000, 2000]
        idx = min(self._reconnect_attempt - 1, len(delays) - 1)
        base_delay = delays[idx]
        jitter = random.uniform(
            -self.config.reconnect_jitter_pct / 100,
            self.config.reconnect_jitter_pct / 100,
        )
        return max(0, int(base_delay * (1 + jitter)))

    async def resubscribe(self):
        """Resubscribe with updated token list."""
        if self._ws:
            new_tokens = self.tracker.all_token_ids
            subscribe_msg = {
                "assets_ids": new_tokens,
                "type": "market",
                "custom_feature_enabled": True,
            }
            await self._ws.send(json.dumps(subscribe_msg))
            logger.info("ws_resubscribed", tokens=len(new_tokens))
