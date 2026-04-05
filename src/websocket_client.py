from __future__ import annotations

import asyncio
import json
import random
import time

import aiohttp
import structlog
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from src.config import WebSocketConfig
from src.market_tracker import MarketTracker

WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CLOB_REST_URL = "https://clob.polymarket.com"

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
        self._last_check_time: dict[str, float] = {}  # Throttle: token_id -> last check time
        self._fallback_active = False
        self._http_session: aiohttp.ClientSession | None = None

    def on_opportunity(self, callback):
        """Register a callback for when opportunity-relevant data arrives."""
        self._on_opportunity_callback = callback

    async def start(self):
        """Start the WebSocket connection with auto-reconnection.

        When WS is disconnected, falls back to REST polling to keep prices updated.
        """
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
                # Fallback REST polling while reconnecting
                await self._run_rest_fallback(delay / 1000)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                if not self._running:
                    break
                logger.error("ws_unexpected_error", error=str(e), type=type(e).__name__)
                await self._run_rest_fallback(2.0)

    async def stop(self):
        """Stop the WebSocket connection."""
        self._running = False
        self._fallback_active = False
        if self._ws:
            await self._ws.close()
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

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
            self._last_pong = time.time()  # Initialize so pong timeout doesn't trigger before first PONG

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
        """Send PING every N seconds. Close connection if no PONG received."""
        PONG_TIMEOUT = 3  # max pong intervals without response before reconnect
        while self._running:
            try:
                await ws.send("PING")
                await asyncio.sleep(self.config.ping_interval_seconds)

                # Check if server is still alive
                if self._last_pong > 0:
                    silence = time.time() - self._last_pong
                    max_silence = self.config.ping_interval_seconds * PONG_TIMEOUT
                    if silence > max_silence:
                        logger.warning(
                            "ws_pong_timeout",
                            silence_seconds=round(silence, 1),
                            max_allowed=max_silence,
                        )
                        await ws.close()
                        break
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
        """Route WebSocket messages to the appropriate handler.

        Throttles ALL processing (not just detector callbacks) per token_id
        to reduce CPU. Resolution events always pass through immediately.
        """
        events = data if isinstance(data, list) else [data]

        for event in events:
            if not isinstance(event, dict):
                continue

            event_type = event.get("event_type", "")
            asset_id = event.get("asset_id", "")


            # Resolution events are always processed immediately
            if event_type == "market_resolved":
                self._handle_market_resolved(event)
                if self._on_opportunity_callback:
                    await self._on_opportunity_callback(asset_id, event_type)
                continue

            if event_type == "tick_size_change":
                logger.info(
                    "tick_size_change",
                    asset_id=asset_id,
                    old=event.get("old_tick_size"),
                    new=event.get("new_tick_size"),
                )
                continue

            # price_change has asset_id inside nested array, skip global throttle
            if event_type == "price_change":
                updated_tokens = self._handle_price_change(event)
                # Call callback for each token that was updated, not with empty token_id
                # This allows the detector to throttle per-token instead of doing a full scan
                if self._on_opportunity_callback:
                    for token_id in updated_tokens:
                        await self._on_opportunity_callback(token_id, event_type)
                continue

            # Throttle: skip PROCESSING if same token was handled < 2s ago
            # (doesn't apply to price_change which is handled above)
            # BUT always update tracker to keep market alive
            now = time.time()
            last = self._last_check_time.get(asset_id, 0)
            should_process = now - last >= 2.0

            if should_process:
                self._last_check_time[asset_id] = now
                if event_type == "book":
                    self._handle_book(event)
                elif event_type == "best_bid_ask":
                    self._handle_best_bid_ask(event)
                elif event_type == "last_trade_price":
                    self._handle_last_trade(event)

                # Notify detector only when processing (not when throttled)
                if self._on_opportunity_callback and event_type in (
                    "book", "best_bid_ask", "last_trade_price",
                ):
                    await self._on_opportunity_callback(asset_id, event_type)
            else:
                # Throttled: still update tracker to keep market alive
                # This prevents stale detection (5s timeout) when events arrive frequently
                # But don't notify detector (that's the whole point of throttling)
                if event_type == "best_bid_ask":
                    best_bid = event.get("best_bid")
                    best_ask = event.get("best_ask")
                    if best_bid is not None and best_ask is not None:
                        self.tracker.update_best_bid_ask(asset_id, float(best_bid), float(best_ask))
                elif event_type == "last_trade_price":
                    price = event.get("price")
                    if price is not None:
                        self.tracker.update_last_trade(asset_id, float(price))

    def _handle_book(self, event: dict):
        asset_id = event.get("asset_id", "")
        bids = event.get("bids", [])
        asks = event.get("asks", [])
        self.tracker.update_book(asset_id, bids, asks)

    def _handle_price_change(self, event: dict) -> list[str]:
        """Handle price_change event and return list of updated token_ids."""
        changes = event.get("price_changes", event.get("changes", []))
        if not changes and "asset_id" in event:
            changes = [event]

        updated_tokens = []
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
                updated_tokens.append(asset_id)

        return updated_tokens

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
            logger.info(
                "market_resolved_event_received",
                condition_id=condition_id[:20] + "...",
                winning_asset_id=winning_asset_id[:20] + "...",
            )
            self.tracker.mark_resolved(condition_id, winning_asset_id)
        else:
            logger.warning(
                "market_resolved_incomplete",
                event_keys=list(event.keys()),
                has_condition_id=bool(condition_id),
                has_winning_asset_id=bool(winning_asset_id),
            )

    async def _run_rest_fallback(self, duration_seconds: float):
        """Poll REST API for prices while WS is disconnected.

        Runs for `duration_seconds` then returns so WS can try reconnecting.
        """
        token_ids = self.tracker.all_token_ids
        if not token_ids:
            await asyncio.sleep(duration_seconds)
            return

        self._fallback_active = True
        interval = self.config.fallback_rest_interval_ms / 1000.0
        logger.info("rest_fallback_started", tokens=len(token_ids), interval_ms=self.config.fallback_rest_interval_ms)

        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()

        deadline = time.time() + duration_seconds
        polls = 0
        while self._running and time.time() < deadline:
            try:
                await self._poll_rest_prices(token_ids)
                polls += 1
            except Exception as e:
                logger.debug("rest_fallback_error", error=str(e))
            await asyncio.sleep(interval)

        self._fallback_active = False
        if polls > 0:
            logger.info("rest_fallback_stopped", polls=polls)

    async def _poll_rest_prices(self, token_ids: list[str]):
        """Fetch current prices from CLOB REST API for all tracked tokens."""
        assert self._http_session is not None
        for token_id in token_ids:
            try:
                async with self._http_session.get(
                    f"{CLOB_REST_URL}/price",
                    params={"token_id": token_id, "side": "buy"},
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    price = float(data.get("price", 0))
                    if price > 0:
                        state = self.tracker.get_by_token(token_id)
                        if state:
                            is_yes = token_id == state.yes_token_id
                            if is_yes:
                                state.best_ask_yes = price
                            else:
                                state.best_ask_no = price
                            state.last_update = time.time()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue

        # Trigger detector after REST update
        if self._on_opportunity_callback:
            await self._on_opportunity_callback("", "rest_fallback")

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
