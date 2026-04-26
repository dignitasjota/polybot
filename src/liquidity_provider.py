"""Liquidity Provider — Phase 2+3 market making engine with risk management.

Places two-sided quotes (bid + ask) on the top reward markets identified
by RewardScanner.  Each market gets:
  - BUY YES at bid_price   (bid side)
  - BUY NO  at 1-ask_price (ask side, equivalent to SELL YES)

Both orders use post_only=True (GTC) to guarantee maker status (0% fees).
Orders are refreshed every ``quote_refresh_s`` seconds — cancelled and
re-placed when the midpoint drifts more than ``reprice_threshold``.

Phase 3 additions:
  - Inventory tracking (YES/NO fills per position)
  - Rebalanceo automático: skew-based spread/size adjustment
  - Adverse selection monitoring with market abandonment
  - Emergency cancel on sudden price moves (>5% in <30s)

In paper mode the provider simulates orders without a ClobClient.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from src.config import CredentialsConfig
    from src.liquidity_metrics import LiquidityMetrics
    from src.market_tracker import MarketTracker
    from src.reward_scanner import RewardMarket, RewardScanner

CLOB_URL = "https://clob.polymarket.com"

logger = structlog.get_logger("polymarket.liquidity_provider")


# ── Data classes ──────────────────────────────────────────────────────


@dataclass
class QuoteOrder:
    """A single outstanding quote order."""

    order_id: str
    token_id: str
    side: str  # always "BUY"
    price: float
    size: float
    is_yes: bool  # True = bid side (BUY YES), False = ask side (BUY NO)
    condition_id: str
    placed_at: float = 0.0
    status: str = "active"  # active / filled / cancelled


@dataclass
class MarketPosition:
    """Quoting state for a single market."""

    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    max_spread: float
    min_size: float = 0.0  # Min order size (shares) to earn rewards
    midpoint: float = 0.5

    bid_order: QuoteOrder | None = None
    ask_order: QuoteOrder | None = None
    capital_allocated: float = 0.0

    # Fill tracking (Phase 2)
    fills_yes: float = 0.0
    fills_no: float = 0.0
    fill_count: int = 0

    # Inventory tracking (Phase 3)
    total_rewards_earned: float = 0.0
    total_adverse_loss: float = 0.0
    last_midpoint_time: float = 0.0  # For emergency cancel detection
    last_midpoint_value: float = 0.0
    abandoned: bool = False  # Marked for removal due to high adverse ratio

    # Unmatched inventory tracking (for auto-exit)
    unmatched_since: float = 0.0  # timestamp when unmatched inventory first detected

    # Dynamic spread tracking (Phase 3.5: adaptive quoting)
    consecutive_no_moves: int = 0  # Counts how many refresh cycles midpoint didn't move >0.5¢
    current_spread_pct: float = 0.65  # Dynamic spread % (default 0.65=3¢, can reduce to 0.50=2¢ if stable)
    last_checked_midpoint: float = 0.5  # Previous midpoint to detect movement
    market_volume_24h: float = 0.0  # Cache volume for low-volume detection

    @property
    def inventory_skew(self) -> float:
        """Positive = long YES, negative = long NO. Range [-1, 1]."""
        total = self.fills_yes + self.fills_no
        if total == 0:
            return 0.0
        return (self.fills_yes - self.fills_no) / total

    @property
    def adverse_ratio(self) -> float:
        """Ratio of adverse losses to rewards earned. Target < 0.7."""
        if self.total_rewards_earned <= 0:
            return 0.0
        return self.total_adverse_loss / self.total_rewards_earned

    def to_dict(self) -> dict:
        return {
            "condition_id": self.condition_id,
            "question": self.question[:60],
            "midpoint": round(self.midpoint, 4),
            "max_spread": self.max_spread,
            "min_size": self.min_size,
            "bid": round(self.bid_order.price, 2) if self.bid_order else None,
            "ask": round(1.0 - self.ask_order.price, 2) if self.ask_order else None,
            "bid_size": round(self.bid_order.size, 2) if self.bid_order else None,
            "ask_size": round(self.ask_order.size, 2) if self.ask_order else None,
            "bid_order_id": self.bid_order.order_id[:12] if self.bid_order else None,
            "ask_order_id": self.ask_order.order_id[:12] if self.ask_order else None,
            "fills_yes": round(self.fills_yes, 2),
            "fills_no": round(self.fills_no, 2),
            "fill_count": self.fill_count,
            "inventory_skew": round(self.inventory_skew, 4),
            "adverse_ratio": round(self.adverse_ratio, 4),
            "total_rewards": round(self.total_rewards_earned, 2),
            "total_adverse": round(self.total_adverse_loss, 2),
            "capital_allocated": round(self.capital_allocated, 2),
            "abandoned": self.abandoned,
        }


# ── Provider ──────────────────────────────────────────────────────────


class LiquidityProvider:
    """Places and manages two-sided quotes on reward markets.

    Lifecycle:
        provider = LiquidityProvider(config, creds, tracker)
        provider.set_scanner(scanner)
        await provider.start()
        ...
        await provider.stop()   # cancels all orders, cleans up
    """

    def __init__(
        self,
        config,  # LiquidityConfig
        credentials: CredentialsConfig | None = None,
        tracker: MarketTracker | None = None,
        metrics: LiquidityMetrics | None = None,
    ):
        self._config = config
        self._credentials = credentials
        self._tracker = tracker
        self._metrics = metrics

        # ClobClient — initialized in start() for live/dry_run
        self._client = None
        self._funder = None  # Store funder address for Data API queries
        self._initialized = False

        # Scanner reference (set externally)
        self._scanner: RewardScanner | None = None

        # Active positions keyed by condition_id
        self._positions: dict[str, MarketPosition] = {}

        # Stats
        self._running = False
        self._quote_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._scoring_task: asyncio.Task | None = None
        self._total_orders_placed = 0
        self._total_orders_cancelled = 0
        self._total_fills = 0
        self._errors = 0
        self._started_at: float = 0.0
        self._emergency_cancels = 0
        self._markets_abandoned = 0

        # Auto-redeem callback: called when matched pairs detected
        # signature: async callback(condition_id: str) -> bool
        self._on_redeem: asyncio.coroutine | None = None
        self._total_redeems = 0
        self._total_auto_exits = 0
        self._redeem_lock: set[str] = set()  # condition_ids currently being redeemed

        # Heartbeat
        self._heartbeat_active = False
        self._heartbeat_count = 0
        self._heartbeat_errors = 0

        # Order scoring
        self._orders_scoring = 0       # Orders currently earning rewards
        self._orders_not_scoring = 0   # Orders NOT earning rewards
        self._scoring_checks = 0
        self._last_scoring_check: float = 0.0

        # Real rewards tracking (from Data API)
        self._rewards_task: asyncio.Task | None = None
        self._last_reward_check: float = 0.0
        self._last_reward_timestamp: float = 0.0  # Last seen reward timestamp

        # Dynamic capital tracking (updated every 5 min from Polymarket)
        self._cached_available_capital: float | None = None
        self._last_capital_check: float = 0.0

    # ── Configuration ─────────────────────────────────────────────────

    @property
    def is_paper(self) -> bool:
        return self._config.mode == "paper"

    @property
    def is_dry_run(self) -> bool:
        return self._config.mode == "dry_run"

    @property
    def should_simulate(self) -> bool:
        """True if orders should be simulated (paper or dry_run)."""
        return self._config.mode in ("paper", "dry_run")

    @property
    def quote_refresh_s(self) -> float:
        return getattr(self._config, "quote_refresh_s", 30.0)

    @property
    def reprice_threshold(self) -> float:
        """Min midpoint change (in price units) to trigger cancel+replace."""
        return 0.005  # 0.5¢ — react fast to small moves, flee before fills

    def set_scanner(self, scanner: RewardScanner):
        self._scanner = scanner

    def set_metrics(self, metrics: LiquidityMetrics):
        self._metrics = metrics

    def _get_available_balance(self) -> float | None:
        """Query USDC balance from CLOB API. Returns None if unavailable."""
        if not self._client:
            return None
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
            resp = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            if not resp:
                return None
            balance_raw = float(resp.get("balance", 0) or 0)
            return balance_raw / 1e6 if balance_raw > 1000 else balance_raw
        except Exception as e:
            logger.warning("balance_check_failed", error=str(e))
            return None

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self):
        """Initialize ClobClient (if live) and begin quote loop + background tasks."""
        if self._running:
            return

        if not self.is_paper:
            await self._init_clob_client()
            # Sync available capital from Polymarket on startup (live mode)
            await self._update_available_capital(force=True)

        # Cancel any orphaned orders from previous runs (live mode only)
        if self._initialized and self._client:
            await self._cancel_all_on_startup()
            # Mark any unmatched positions for immediate auto-exit
            await self._handle_orphan_unmatched_positions()

        self._running = True
        self._started_at = time.time()
        self._quote_task = asyncio.create_task(self._quote_loop())

        # Heartbeat: only in live mode when enabled (not dry_run)
        if not self.should_simulate and getattr(self._config, "use_heartbeat", False):
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            self._heartbeat_active = True

        # Order scoring check: only in live mode with ClobClient
        if not self.should_simulate and self._initialized:
            self._scoring_task = asyncio.create_task(self._scoring_loop())

        # Real rewards tracking: query Data API periodically (live mode only)
        if not self.should_simulate:
            self._rewards_task = asyncio.create_task(self._rewards_check_loop())

        logger.info(
            "provider_started",
            mode=self._config.mode,
            max_markets=self._config.max_markets,
            capital_per_market=self._config.capital_per_market,
            heartbeat=self._heartbeat_active,
        )

    def ensure_scoring_loop(self):
        """Start the scoring loop if not already running. Called on mode transitions."""
        if self._scoring_task and not self._scoring_task.done():
            return  # Already running
        if not self._initialized:
            return
        self._scoring_task = asyncio.create_task(self._scoring_loop())
        logger.info("scoring_loop_started_on_mode_change")

    def ensure_rewards_loop(self):
        """Start the rewards check loop if not already running. Called on mode transitions to live."""
        if self._rewards_task and not self._rewards_task.done():
            return  # Already running
        if not self._initialized:
            return
        self._rewards_task = asyncio.create_task(self._rewards_check_loop())
        logger.info("rewards_check_loop_started_on_mode_change")

    async def stop(self):
        """Cancel all orders, stop heartbeat, and shut down."""
        self._running = False

        # Stop background tasks
        for task in (self._quote_task, self._heartbeat_task, self._scoring_task, self._rewards_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._quote_task = None
        self._heartbeat_task = None
        self._scoring_task = None
        self._rewards_task = None
        self._heartbeat_active = False

        # Cancel all outstanding orders
        await self._cancel_all_orders()
        self._positions.clear()
        logger.info("provider_stopped")

    async def clear_positions(self, old_mode: str, new_mode: str):
        """Clear positions on mode transition to avoid ghost orders.

        When switching from paper/dry_run to live, paper orders (paper-xxx)
        don't exist in the CLOB. Trying to cancel them causes API errors.
        When switching from live to paper, real orders should be cancelled first.
        """
        if not self._positions:
            return

        paper_modes = ("paper", "dry_run")

        if old_mode in paper_modes and new_mode == "live":
            # Paper → live: just discard paper positions (they don't exist in CLOB)
            count = len(self._positions)
            self._positions.clear()
            logger.info("cleared_paper_positions_for_live", count=count)

        elif old_mode == "live" and new_mode in paper_modes:
            # Live → paper: cancel real orders first, then clear and reset funder
            await self._cancel_all_orders()
            self._positions.clear()
            self._funder = None  # Reset funder when leaving live mode
            logger.info("cancelled_live_positions_for_paper")

        elif old_mode in paper_modes and new_mode in paper_modes:
            # dry_run ↔ paper: just clear
            self._positions.clear()
            logger.info("cleared_positions_mode_change", old=old_mode, new=new_mode)

    # ── ClobClient init (mirrors executor.py pattern) ─────────────────

    async def _cancel_all_on_startup(self):
        """Cancel ALL open orders on startup to free capital from orphaned orders.

        After a redeploy, old orders remain on the CLOB but the bot loses
        track of them. This frees the locked capital so we can place fresh quotes.
        """
        try:
            resp = self._client.cancel_all()
            logger.info("startup_cancel_all", response=str(resp)[:200])
        except Exception as e:
            logger.warning("startup_cancel_all_failed", error=str(e))

    async def _handle_orphan_unmatched_positions(self):
        """Mark any unmatched positions from previous runs for immediate exit.

        If a position has fills on only one side from a previous run,
        mark it as "unmatched since 120+ seconds ago" so auto-exit
        will liquidate it in the next check cycle (60s timeout).
        """
        now = time.time()
        orphan_threshold = 120  # Mark positions as if they've been unmatched 120s

        for cid, pos in list(self._positions.items()):
            # Check if position has unmatched fills (only one side filled)
            unmatched_yes = pos.fills_yes - min(pos.fills_yes, pos.fills_no)
            unmatched_no = pos.fills_no - min(pos.fills_yes, pos.fills_no)

            if (unmatched_yes > 0.5 or unmatched_no > 0.5) and pos.unmatched_since == 0:
                # Mark as orphan: pretend it's been unmatched for 120s
                # so auto-exit will liquidate it immediately
                pos.unmatched_since = now - orphan_threshold
                logger.info(
                    "orphan_unmatched_detected",
                    condition_id=cid[:16],
                    fills_yes=round(pos.fills_yes, 2),
                    fills_no=round(pos.fills_no, 2),
                    action="marked_for_immediate_exit",
                )

    async def _init_clob_client(self):
        """Initialize ClobClient with credentials — same pattern as Executor."""
        if not self._credentials:
            logger.error("no_credentials_for_live_mode")
            return

        # Validate credentials object
        if not hasattr(self._credentials, 'get_private_key'):
            logger.error(
                "invalid_credentials_type",
                type=type(self._credentials).__name__,
                expected="CredentialsConfig",
            )
            return

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            private_key = self._credentials.get_private_key()
            sig_type = self._credentials.signature_type
            proxy_address = self._credentials.get_proxy_address()

            funder = proxy_address
            if not funder:
                from eth_account import Account
                account = Account.from_key(private_key)
                funder = account.address

            # Try env var creds first, then derive
            try:
                api_key = self._credentials.get_api_key()
                api_secret = self._credentials.get_api_secret()
                passphrase = self._credentials.get_passphrase()
            except EnvironmentError:
                logger.info("deriving_api_creds_provider")
                tmp = ClobClient(
                    host=CLOB_URL,
                    key=private_key,
                    chain_id=137,
                    signature_type=sig_type,
                    funder=funder,  # Use calculated funder for derivation too
                )
                creds = tmp.derive_api_key()
                api_key = creds.api_key
                api_secret = creds.api_secret
                passphrase = creds.api_passphrase

            self._client = ClobClient(
                host=CLOB_URL,
                key=private_key,
                chain_id=137,
                signature_type=sig_type,
                funder=funder,  # Use calculated funder (proxy_address or account.address fallback)
                creds=ApiCreds(
                    api_key=api_key,
                    api_secret=api_secret,
                    api_passphrase=passphrase,
                ),
            )
            # Store funder address for Data API queries (ClobClient doesn't expose it publicly)
            self._funder = funder
            self._initialized = True
            logger.info("provider_clob_initialized", sig_type=sig_type, funder=funder[:10])

        except Exception as e:
            logger.error("provider_clob_init_failed", error=str(e))
            self._errors += 1

    # ── Quote loop ────────────────────────────────────────────────────

    async def _quote_loop(self):
        """Periodically refresh quotes on top markets."""
        while self._running:
            try:
                await self._refresh_all()
            except Exception as e:
                logger.error("quote_loop_error", error=str(e))
                self._errors += 1
            await asyncio.sleep(self.quote_refresh_s)

    # ── Heartbeat ─────────────────────────────────────────────────────

    async def _heartbeat_loop(self):
        """Send POST /heartbeat every N seconds to keep orders alive.

        Polymarket CLOB heartbeat: if the server doesn't receive a heartbeat
        within 10 seconds, ALL open orders are cancelled. This is a safety
        mechanism for market makers — if the bot crashes, orders get cleaned up.

        Only active in live mode when use_heartbeat=True.
        """
        interval = getattr(self._config, "heartbeat_interval", 5.0)
        logger.info("heartbeat_started", interval=interval)

        while self._running:
            try:
                if self._client:
                    # py_clob_client doesn't have a heartbeat method,
                    # so we call the REST endpoint directly
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        headers = self._client.create_or_derive_api_creds()  # type: ignore
                        async with session.post(
                            f"{CLOB_URL}/heartbeat",
                            headers=self._get_auth_headers(),
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as resp:
                            if resp.status == 200:
                                self._heartbeat_count += 1
                            else:
                                logger.warning("heartbeat_failed", status=resp.status)
                                self._heartbeat_errors += 1
            except Exception as e:
                logger.error("heartbeat_error", error=str(e))
                self._heartbeat_errors += 1
            await asyncio.sleep(interval)

    def _get_auth_headers(self) -> dict:
        """Build auth headers for direct REST calls.

        Uses the ClobClient's stored credentials.
        """
        if not self._client:
            return {}
        try:
            creds = self._client.creds  # type: ignore
            return {
                "POLY_ADDRESS": self._client.funder or "",  # type: ignore
                "POLY_SIGNATURE": "",  # Heartbeat may not need full sig
                "POLY_TIMESTAMP": str(int(time.time())),
                "POLY_API_KEY": creds.api_key if creds else "",
                "POLY_PASSPHRASE": creds.api_passphrase if creds else "",
            }
        except Exception:
            return {}

    # ── Order Scoring ─────────────────────────────────────────────────
    # NOTE: el endpoint GET /order-scoring no funciona como esperado.
    # Siempre devuelve not-scoring aunque las órdenes SÍ acumulan rewards
    # en Polymarket. Verificar rewards directamente en la UI de Polymarket.
    # Los campos orders_scoring/orders_not_scoring en get_stats() NO son fiables.

    async def _scoring_loop(self):
        """Periodically check if active orders are earning rewards.

        Calls GET /order-scoring?order_id=X for each active order.
        Tracks scoring rate (orders earning rewards / total orders).

        WARNING: este check NO es fiable — siempre reporta not-scoring
        aunque los rewards sí se acumulan. Solo útil para debug.
        """
        interval = getattr(self._config, "scoring_check_interval", 60.0)
        logger.info("scoring_check_started", interval=interval)

        # Wait for initial quotes to be placed
        await asyncio.sleep(interval)

        while self._running:
            try:
                await self._check_order_scoring()
            except Exception as e:
                logger.error("scoring_check_error", error=str(e))
                self._errors += 1
            await asyncio.sleep(interval)

    async def _check_order_scoring(self):
        """Check all active orders against the /order-scoring endpoint."""
        if not self._client or self.should_simulate:
            return

        scoring = 0
        not_scoring = 0

        for pos in self._positions.values():
            for order in (pos.bid_order, pos.ask_order):
                if not order or order.status != "active":
                    continue
                # Skip paper orders
                if order.order_id.startswith("paper-"):
                    continue

                is_scoring = await self._check_single_order_scoring(order.order_id)
                if is_scoring:
                    scoring += 1
                else:
                    not_scoring += 1

        self._orders_scoring = scoring
        self._orders_not_scoring = not_scoring
        self._scoring_checks += 1
        self._last_scoring_check = time.time()
        if self._metrics:
            self._metrics.record_scoring(scoring, not_scoring)

        total = scoring + not_scoring
        rate = scoring / total if total > 0 else 0
        logger.info(
            "scoring_check_complete",
            scoring=scoring,
            not_scoring=not_scoring,
            rate=round(rate, 3),
        )

    async def _check_single_order_scoring(self, order_id: str) -> bool:
        """Check if a single order is earning rewards via GET /order-scoring."""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"{CLOB_URL}/order-scoring"
                params = {"order_id": order_id}
                async with session.get(
                    url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        logger.debug(
                            "scoring_api_non_200",
                            order_id=order_id[:12],
                            status=resp.status,
                        )
                        return False
                    data = await resp.json()
                    # Log full response to diagnose field names
                    logger.info(
                        "scoring_api_response",
                        order_id=order_id[:12],
                        response=str(data)[:200],
                    )
                    # Check multiple possible field names
                    if data.get("scoring"):
                        return True
                    if data.get("is_scoring"):
                        return True
                    if data.get("score", 0) > 0:
                        return True
                    return False
        except Exception as e:
            logger.debug("scoring_check_exception", order_id=order_id[:12], error=str(e))
            return False

    # ── Real Rewards Tracking ────────────────────────────────────────

    async def _rewards_check_loop(self):
        """Periodically query the Data API for actual rewards earned.

        Endpoint: GET https://data-api.polymarket.com/activity?user=<address>&type=REWARD
        No auth required — uses the public proxy/funder address.
        Runs every 5 minutes (rewards are distributed daily at midnight UTC).
        """
        # Wait for initial setup
        await asyncio.sleep(30)
        logger.info("rewards_check_loop_started")

        while self._running:
            try:
                await self._fetch_real_rewards()
            except Exception as e:
                logger.warning("rewards_check_error", error=str(e))
            await asyncio.sleep(300)  # Every 5 minutes

    async def _update_available_capital(self, force: bool = False):
        """Update cached available capital from Polymarket (live mode) or config (paper mode).

        Args:
            force: If True, skip the 300-second interval check (for urgent updates like fills/orders)
                   If False, only update if 300+ seconds since last check (prevents API saturation)

        In live mode: queries balance_allowance from CLOB API when:
          - Bot starts in live mode (force=True)
          - Order is placed (force=True)
          - Position is closed/fills detected (force=True)
          - Periodic refresh in _refresh_all() (force=False, only if 300s passed)
        In paper mode: uses simulated balance from config.

        This avoids unnecessary API calls while keeping balance accurate.
        """
        now = time.time()
        # If not forced and recently checked, skip
        if not force and (now - self._last_capital_check) < 300:
            return

        if not self.should_simulate and self._client:
            # Live mode: get real balance from Polymarket (same pattern as executor.py)
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                resp = self._client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                )
                if resp and "balance" in resp:
                    raw = float(resp["balance"])
                    # Auto-detect format: if raw > 1000, it's in smallest units (6 decimals)
                    balance = raw / 1e6 if raw > 1000 else raw
                    self._cached_available_capital = balance
                    logger.debug(
                        "available_capital_updated",
                        balance_usdc=round(balance, 2),
                        source="polymarket_live",
                    )
                else:
                    logger.warning("available_capital_empty_response", response=str(resp))
                    self._cached_available_capital = self._config.total_capital
            except Exception as e:
                logger.warning(
                    "available_capital_fetch_failed",
                    error=str(e),
                    fallback="using_config",
                )
                self._cached_available_capital = self._config.total_capital
        else:
            # Paper mode: use simulated balance from config
            self._cached_available_capital = self._config.total_capital
            logger.debug(
                "available_capital_updated",
                balance_usdc=round(self._cached_available_capital, 2),
                source="paper_mode",
            )

        self._last_capital_check = now

    def get_available_capital(self) -> float:
        """Get the currently available capital (cached or from config)."""
        if self._cached_available_capital is not None:
            return self._cached_available_capital
        # Fallback to config if cache is empty
        return self._config.total_capital

    async def _fetch_real_rewards(self):
        """Fetch REWARD activities from Data API and update metrics."""
        if not self._client:
            logger.debug("fetch_real_rewards_skipped_no_client")
            return

        # Get our address (funder/proxy) — stored during ClobClient init
        address = self._funder
        if not address:
            logger.warning("fetch_real_rewards_skipped_no_funder")
            return

        import aiohttp
        from datetime import datetime, timezone

        # Fetch recent reward events
        url = "https://data-api.polymarket.com/activity"
        params = {
            "user": address,
            "type": "REWARD",
            "limit": 100,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        logger.warning("fetch_real_rewards_api_error", status=resp.status)
                        return
                    data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("fetch_real_rewards_network_error", error=str(e))
            return

        if not data:
            logger.debug("fetch_real_rewards_empty_response", address=address[:10])
            return

        # Parse reward events and sum today's rewards
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_total = 0.0
        new_rewards = 0.0

        for event in data:
            # Each reward event has: timestamp, amount, asset, etc.
            ts = event.get("timestamp") or event.get("created_at") or ""
            amount_raw = event.get("amount") or event.get("value") or 0

            # Amount might be in USDC (6 decimals) or already in dollars
            amount = float(amount_raw)
            if amount > 1000:  # Likely in raw units (1e6 = $1)
                amount = amount / 1e6

            # Check if this reward is from today
            event_date = ""
            if isinstance(ts, str) and len(ts) >= 10:
                event_date = ts[:10]
            elif isinstance(ts, (int, float)) and ts > 1e9:
                event_date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

            if event_date == today:
                today_total += amount

            # Track new rewards since last check
            event_ts = 0.0
            if isinstance(ts, (int, float)):
                event_ts = ts
            elif isinstance(ts, str):
                try:
                    event_ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError):
                    pass

            if event_ts > self._last_reward_timestamp:
                new_rewards += amount

        # Update metrics with real rewards from Polymarket
        if self._metrics:
            current = self._metrics.get_today()
            # Always sync with Polymarket's ground truth (ignore our internal estimate)
            # get_today() returns a dict, access via dict notation
            current_rewards = current.get('rewards_earned', 0) if isinstance(current, dict) else getattr(current, 'rewards_earned', 0)
            if today_total > current_rewards:
                diff = today_total - current_rewards
                self._metrics.record_rewards(diff)
                logger.info(
                    "real_rewards_synced",
                    today_total=round(today_total, 4),
                    diff=round(diff, 4),
                    address=address[:10],
                )
            elif today_total == 0:
                logger.debug("real_rewards_zero", address=address[:10])
            else:
                logger.debug(
                    "real_rewards_no_change",
                    today_total=round(today_total, 4),
                    current=round(current_rewards, 4),
                    address=address[:10],
                )
        else:
            logger.warning("real_rewards_no_metrics_object")

        # Update last check timestamp
        if data:
            # Use the newest event timestamp as marker
            newest_ts = 0.0
            for event in data:
                ts = event.get("timestamp") or event.get("created_at") or 0
                if isinstance(ts, str):
                    try:
                        ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    except (ValueError, TypeError):
                        ts = 0
                if isinstance(ts, (int, float)) and ts > newest_ts:
                    newest_ts = ts
            if newest_ts > self._last_reward_timestamp:
                self._last_reward_timestamp = newest_ts

        self._last_reward_check = time.time()
        logger.debug("fetch_real_rewards_complete", address=address[:10])

    async def _refresh_all(self):
        """Sync active positions with scanner's top markets using dynamic capital allocation.

        Phase 3: also checks adverse selection and emergency conditions.
        Dynamic capital allocation: each market gets capital_needed = min_size * midpoint * 2 * 1.2
        This allows opening more markets with remaining capital instead of fixed per-market allocation.

        Capital is fetched from Polymarket every 5 minutes (caching to avoid saturation).
        """
        if not self._scanner:
            return

        # Check fills on all active orders BEFORE making any changes
        if not self.should_simulate:
            await self._check_active_fills()

        # Update available capital from Polymarket (cached every 5 minutes)
        await self._update_available_capital()

        # Get all markets and allocate capital dynamically
        all_markets = self._scanner.get_all_markets()

        # Even if scanner finds 0 new markets, still refresh existing positions
        # (replace missing orders, check fills, etc.)
        if not all_markets and self._positions:
            for pos in list(self._positions.values()):
                if pos.bid_order is None or pos.ask_order is None:
                    logger.info(
                        "replacing_missing_orders_no_scanner",
                        condition_id=pos.condition_id[:16],
                        bid_missing=pos.bid_order is None,
                        ask_missing=pos.ask_order is None,
                    )
                    await self._place_quotes(pos)
            await self._check_auto_exits()
            return

        if not all_markets:
            return

        # Start with available balance, subtract capital already in open positions
        total_available = self.get_available_capital()
        capital_in_use = sum(pos.capital_allocated for pos in self._positions.values())
        remaining_capital = total_available - capital_in_use

        # Existing positions keep their spot — capital is already committed.
        # Only use remaining capital for NEW markets.
        existing_ids = set(self._positions.keys())
        allocated_markets = []  # List of (market, capital_needed)
        new_count = 0  # Count of NEW markets added

        for market in all_markets:
            # Existing position: keep it, no capital re-check needed
            if market.condition_id in existing_ids:
                allocated_markets.append((market, 0))  # 0 = no new capital needed
                continue

            # Cap at max_markets (existing + new)
            if len(existing_ids) + new_count >= self._config.max_markets:
                break

            # Calculate capital needed for NEW market
            yes_token_id = ""
            for tok in market.tokens:
                if tok.get("outcome", "").lower() == "yes":
                    yes_token_id = tok.get("token_id", "")
                    break
            if not yes_token_id and len(market.tokens) > 0:
                yes_token_id = market.tokens[0].get("token_id", "")

            midpoint = market.midpoint  # Use scanner data as default
            if yes_token_id and self._tracker:
                tracked_mid = self._tracker.get_midpoint(yes_token_id)
                if tracked_mid is not None:
                    midpoint = tracked_mid

            # Capital needed: min_size * max(midpoint, 1-midpoint) * 2 * 1.2
            expensive_side = max(midpoint, 1.0 - midpoint)
            capital_needed = market.min_size * expensive_side * 2 * 1.2

            if capital_needed > 0 and remaining_capital >= capital_needed:
                allocated_markets.append((market, capital_needed))
                remaining_capital -= capital_needed
                new_count += 1

        desired_ids = {m[0].condition_id for m in allocated_markets}

        logger.info(
            "dynamic_capital_allocation",
            total_available=round(total_available, 2),
            in_use=round(capital_in_use, 2),
            existing_positions=len(existing_ids),
            new_markets=new_count,
            total_allocated=len(allocated_markets),
            capital_remaining=round(remaining_capital, 2),
        )

        # Check adverse selection — abandon markets with high loss ratio
        to_abandon = []
        for cid, pos in self._positions.items():
            if pos.adverse_ratio > self._config.max_adverse_ratio and pos.total_rewards_earned > 0:
                to_abandon.append(cid)
                logger.warning(
                    "market_abandoned_adverse",
                    condition_id=cid[:16],
                    adverse_ratio=round(pos.adverse_ratio, 3),
                    rewards=round(pos.total_rewards_earned, 2),
                    losses=round(pos.total_adverse_loss, 2),
                )
                self._markets_abandoned += 1
                if self._metrics:
                    self._metrics.record_market_abandoned()

        # Remove abandoned + no longer in allocated set
        to_remove = set(to_abandon) | {cid for cid in self._positions if cid not in desired_ids}
        for cid in to_remove:
            await self._close_position(cid)

        # Add/refresh positions with calculated capital
        for market, capital_needed in allocated_markets:
            if market.condition_id in to_remove:
                continue  # Just abandoned, don't re-open immediately
            if market.condition_id in self._positions:
                await self._refresh_quotes(market)
            else:
                await self._open_position(market, capital_allocated=capital_needed)

        # Update active markets count in metrics
        if self._metrics:
            self._metrics.update_active_markets(len(self._positions))

        # Auto-exit unmatched inventory (live mode only)
        await self._check_auto_exits()

        # Paper/dry_run mode: simulate fills for testing
        if self.should_simulate:
            await self._simulate_paper_fills()

    async def _open_position(self, market: RewardMarket, capital_allocated: float | None = None):
        """Start quoting a new market with optional dynamic capital allocation.

        Args:
            market: RewardMarket from scanner
            capital_allocated: Capital to use for this market. If None, uses config.capital_per_market (legacy)
        """
        # Need token IDs from the market
        tokens = market.tokens
        if len(tokens) < 2:
            logger.warning("skip_market_no_tokens", condition_id=market.condition_id[:16])
            return

        yes_token_id = ""
        no_token_id = ""
        for tok in tokens:
            outcome = tok.get("outcome", "").lower()
            if outcome == "yes":
                yes_token_id = tok.get("token_id", "")
            elif outcome == "no":
                no_token_id = tok.get("token_id", "")

        # Fallback: first two tokens
        if not yes_token_id and not no_token_id and len(tokens) >= 2:
            yes_token_id = tokens[0].get("token_id", "")
            no_token_id = tokens[1].get("token_id", "")

        if not yes_token_id or not no_token_id:
            logger.warning("skip_market_missing_token_ids", condition_id=market.condition_id[:16])
            return

        midpoint = self._get_midpoint(market, yes_token_id)

        # Use provided capital or fallback to config default
        if capital_allocated is None:
            capital_allocated = self._config.capital_per_market

        pos = MarketPosition(
            condition_id=market.condition_id,
            question=market.question,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            max_spread=market.max_spread / 100.0,  # centavos → price units
            min_size=market.min_size,
            midpoint=midpoint,
            capital_allocated=capital_allocated,
        )
        # Cache market volume for stability-based spread adjustment
        pos.market_volume_24h = market.volume_24h
        pos.last_checked_midpoint = midpoint
        self._positions[market.condition_id] = pos

        # Place initial quotes
        await self._place_quotes(pos)
        logger.info(
            "position_opened",
            condition_id=market.condition_id[:16],
            question=market.question[:50],
            midpoint=round(midpoint, 4),
            capital_allocated=round(capital_allocated, 2),
        )

    async def _close_position(self, condition_id: str):
        """Cancel orders and remove position."""
        pos = self._positions.pop(condition_id, None)
        if not pos:
            return

        if pos.bid_order and pos.bid_order.status == "active":
            await self._cancel_order(pos.bid_order)
        if pos.ask_order and pos.ask_order.status == "active":
            await self._cancel_order(pos.ask_order)

        # Update available capital after closing position (frees up capital)
        await self._update_available_capital(force=True)

        logger.info(
            "position_closed",
            condition_id=condition_id[:16],
            fills=pos.fill_count,
        )

    async def _refresh_quotes(self, market: RewardMarket):
        """Check if quotes need repricing and refresh them.

        Phase 3: includes emergency cancel on rapid price moves
        and inventory-aware spread adjustment.
        """
        pos = self._positions.get(market.condition_id)
        if not pos or pos.abandoned:
            return

        new_midpoint = self._get_midpoint(market, pos.yes_token_id)
        now = time.time()

        # DEBUG: log order state at start of refresh
        logger.info(
            "refresh_quotes_entry",
            condition_id=pos.condition_id[:16],
            bid_status=pos.bid_order.status if pos.bid_order else "NONE",
            ask_status=pos.ask_order.status if pos.ask_order else "NONE",
            bid_id=(pos.bid_order.order_id[:12] if pos.bid_order else "NONE"),
            ask_id=(pos.ask_order.order_id[:12] if pos.ask_order else "NONE"),
        )

        # Emergency cancel: price moved >5% in <30s
        if pos.last_midpoint_time > 0 and pos.last_midpoint_value > 0:
            elapsed = now - pos.last_midpoint_time
            if elapsed < 30.0 and elapsed > 0:
                move_pct = abs(new_midpoint - pos.last_midpoint_value) / pos.last_midpoint_value
                if move_pct > self._config.emergency_move_pct:
                    logger.error(
                        "emergency_cancel",
                        condition_id=pos.condition_id[:16],
                        move_pct=round(move_pct * 100, 2),
                        elapsed_s=round(elapsed, 1),
                    )
                    await self._emergency_cancel_position(pos)
                    self._emergency_cancels += 1
                    if self._metrics:
                        self._metrics.record_emergency_cancel()
                    return

        pos.last_midpoint_time = now
        pos.last_midpoint_value = new_midpoint

        # Adaptive spread: check market stability and volume (Phase 3.5)
        midpoint_moved = abs(new_midpoint - pos.last_checked_midpoint) >= self.reprice_threshold
        pos.last_checked_midpoint = new_midpoint

        # Detect low-volume markets: wide natural spread OR low 24h volume
        low_volume = (market.spread > 0.03 or market.volume_24h < 5000)
        pos.market_volume_24h = market.volume_24h

        # FEATURE DISABLED (2026-04-21): Adaptive spread tightening
        # TODO: Re-enable after testing to see if rewards improve with tighter spreads
        # if not midpoint_moved:
        #     # Midpoint stable: increment counter
        #     pos.consecutive_no_moves += 1
        #
        #     # After 3 checks without movement (90s) AND low volume → get closer
        #     if pos.consecutive_no_moves >= 3 and low_volume:
        #         pos.current_spread_pct = 0.50  # 2¢ instead of 3¢ → 2× more rewards
        #         logger.info(
        #             "adaptive_spread_tightened",
        #             condition_id=market.condition_id[:16],
        #             consecutive_no_moves=pos.consecutive_no_moves,
        #             new_spread_pct=0.50,
        #             volume_24h=round(market.volume_24h, 0),
        #         )
        #     elif pos.consecutive_no_moves >= 3 and not low_volume:
        #         # Market stable but HIGH volume → stay defensive
        #         pos.current_spread_pct = 0.65
        # else:
        #     # Midpoint moved or volume is high → stay at default 3¢
        #     pos.consecutive_no_moves = 0
        #     pos.current_spread_pct = 0.65

        # For now, always use default spread from config (0.65 = 3¢)
        pos.current_spread_pct = 0.65

        # Clear dead orders: cancelled/expired orders are zombie references
        # that block replacement. Clear them so they're treated as missing.
        if pos.bid_order and pos.bid_order.status not in ("active", "pending"):
            logger.info(
                "clearing_zombie_bid",
                condition_id=pos.condition_id[:16],
                status=pos.bid_order.status,
                order_id=pos.bid_order.order_id[:12],
            )
            pos.bid_order = None
        if pos.ask_order and pos.ask_order.status not in ("active", "pending"):
            logger.info(
                "clearing_zombie_ask",
                condition_id=pos.condition_id[:16],
                status=pos.ask_order.status,
                order_id=pos.ask_order.order_id[:12],
            )
            pos.ask_order = None

        # Check if orders are missing (None, cancelled, filled, or lost)
        # If so, re-place them even if midpoint hasn't moved
        orders_missing = pos.bid_order is None or pos.ask_order is None
        if orders_missing:
            logger.info(
                "replacing_missing_orders",
                condition_id=market.condition_id[:16],
                bid_missing=pos.bid_order is None,
                ask_missing=pos.ask_order is None,
            )
            # Cancel any surviving order before re-placing both sides
            if pos.bid_order and pos.bid_order.status == "active":
                await self._cancel_order(pos.bid_order)
            if pos.ask_order and pos.ask_order.status == "active":
                await self._cancel_order(pos.ask_order)
            pos.bid_order = None
            pos.ask_order = None
            pos.midpoint = new_midpoint
            await self._place_quotes(pos)
            return

        # Check if midpoint moved enough to reprice
        if not midpoint_moved:
            return  # No significant change, orders are active

        pos.midpoint = new_midpoint

        # Cancel existing active orders and re-place
        if pos.bid_order and pos.bid_order.status == "active":
            await self._cancel_order(pos.bid_order)
        if pos.ask_order and pos.ask_order.status == "active":
            await self._cancel_order(pos.ask_order)
        pos.bid_order = None
        pos.ask_order = None

        await self._place_quotes(pos)

    # ── Pricing ───────────────────────────────────────────────────────

    def _calculate_prices(
        self, midpoint: float, max_spread: float, skew: float = 0.0,
        spread_pct_of_max: float | None = None,
    ) -> tuple[float, float]:
        """Calculate bid and ask prices with inventory-aware adjustment.

        When inventory is skewed (|skew| > max_inventory_skew), the spread
        is widened on the long side and tightened on the short side to
        encourage rebalancing fills.

        Phase 3.5: spread_pct_of_max can be dynamic based on market stability.

        Returns (bid_price, ask_price) in [0.01, 0.99].
        """
        # Use dynamic spread if provided, otherwise use config default
        if spread_pct_of_max is None:
            spread_pct_of_max = self._config.spread_pct_of_max

        # max_spread is the max distance FROM MIDPOINT per side (not total spread)
        # See: "the farthest distance your limit order can be from the midpoint"
        distance = max_spread * spread_pct_of_max

        # Inventory-aware spread adjustment (Phase 3)
        abs_skew = abs(skew)
        max_skew = self._config.max_inventory_skew

        if abs_skew > max_skew:
            if abs_skew > 0.8:
                long_mult = 1.5  # Push long side further (less aggressive)
                short_mult = 0.7
            elif abs_skew > 0.7:
                long_mult = 1.3
                short_mult = 0.8
            else:
                long_mult = 1.15
                short_mult = 0.9

            if skew > 0:
                bid = midpoint - distance * long_mult
                ask = midpoint + distance * short_mult
            else:
                bid = midpoint - distance * short_mult
                ask = midpoint + distance * long_mult
        else:
            bid = midpoint - distance
            ask = midpoint + distance

        # Clamp to valid CLOB price range
        bid = max(0.01, min(0.99, bid))
        ask = max(0.01, min(0.99, ask))

        # Round to 2 decimals (CLOB tick size)
        bid = round(bid, 2)
        ask = round(ask, 2)

        # Ensure bid < ask
        if bid >= ask:
            bid = round(midpoint - 0.01, 2)
            ask = round(midpoint + 0.01, 2)
            bid = max(0.01, bid)
            ask = min(0.99, ask)

        return bid, ask

    def _get_midpoint(self, market: RewardMarket, yes_token_id: str) -> float:
        """Get current midpoint from tracker or market data."""
        if self._tracker:
            mp = self._tracker.get_midpoint(yes_token_id)
            if mp is not None:
                return mp
        # Fallback to scanner data
        return market.midpoint

    # ── Order Block Hiding ────────────────────────────────────────────

    @staticmethod
    def find_order_blocks(
        book_levels: list, min_block_usd: float = 500.0,
    ) -> list[dict]:
        """Find protective order blocks in the book.

        Scans book levels (list of PriceLevel or dicts with price/size)
        and returns levels where cumulative USD exceeds min_block_usd.
        """
        blocks = []
        cumulative_usd = 0.0

        for level in book_levels:
            price = level.price if hasattr(level, "price") else level.get("price", 0)
            size = level.size if hasattr(level, "size") else level.get("size", 0)
            usd_value = size * price
            cumulative_usd += usd_value

            if cumulative_usd >= min_block_usd:
                blocks.append({
                    "price": price,
                    "size": size,
                    "cumulative_usd": round(cumulative_usd, 2),
                })

        return blocks

    def _get_block_protected_price(
        self, pos: MarketPosition, is_bid: bool, fallback_price: float,
    ) -> float:
        """Get price positioned behind an order block for protection.

        For bids (BUY YES): look at bids_yes for large orders, place 1 tick behind.
        For asks (BUY NO): look at bids_no for large orders, place 1 tick behind.

        If no block found or order_block_hiding is disabled, returns fallback_price.
        """
        if not self._config.use_order_block_hiding or not self._tracker:
            return fallback_price

        min_block = self._config.min_block_usd
        tick = 0.01  # CLOB tick size

        # Get market state from tracker
        token_id = pos.yes_token_id if is_bid else pos.no_token_id
        state = self._tracker.get_by_token(token_id)
        if not state:
            return fallback_price

        # For BUY YES (bid): look at existing bids_yes for protection
        # We want to place behind a large bid (1 tick lower)
        if is_bid:
            book = state.bids_yes if token_id == state.yes_token_id else state.bids_no
        else:
            # For BUY NO (ask side): look at bids_no for protection
            book = state.bids_no if token_id == state.no_token_id else state.bids_yes

        blocks = self.find_order_blocks(book, min_block)
        if not blocks:
            return fallback_price

        # Place 1 tick behind (worse than) the first block
        block_price = blocks[0]["price"]
        if is_bid:
            protected = block_price - tick  # 1 tick below the block
        else:
            protected = block_price - tick  # 1 tick below for BUY NO too

        protected = round(protected, 2)
        protected = max(0.01, min(0.99, protected))

        # Only use block price if it's better than or close to our fallback
        # Don't sacrifice too much spread for protection
        if is_bid and protected < fallback_price - 0.05:
            return fallback_price  # Block is too far from our desired price
        if not is_bid and protected < fallback_price - 0.05:
            return fallback_price

        logger.debug(
            "order_block_hiding",
            condition_id=pos.condition_id[:16],
            side="bid" if is_bid else "ask",
            block_price=block_price,
            protected_price=protected,
            fallback=fallback_price,
        )
        return protected

    # ── Order placement ───────────────────────────────────────────────

    async def _place_quotes(self, pos: MarketPosition):
        """Place bid + ask quotes for a position.

        Phase 3: inventory-aware sizing + order block hiding.
        - Severe skew (>0.8): only quote the rebalancing side
        - Moderate skew (0.7-0.8): reduce long side size by 50%
        - Order block hiding: place behind large orders for protection
        """
        skew = pos.inventory_skew
        abs_skew = abs(skew)
        # FEATURE DISABLED (2026-04-21): Adaptive spread tightening
        # TODO: Re-enable if adaptive spread improves rewards
        bid_price, ask_price = self._calculate_prices(
            pos.midpoint, pos.max_spread, skew,
            # spread_pct_of_max=pos.current_spread_pct  # DISABLED: Use dynamic spread if market is stable + low-volume
        )

        # Order block hiding: adjust prices to hide behind large orders
        bid_price = self._get_block_protected_price(pos, is_bid=True, fallback_price=bid_price)
        ask_no_fallback = round(1.0 - ask_price, 2)
        ask_no_price_hidden = self._get_block_protected_price(pos, is_bid=False, fallback_price=ask_no_fallback)
        # Recalculate ask_price from the hidden NO price
        ask_price = round(1.0 - ask_no_price_hidden, 2)

        # Base sizes — split capital between bid and ask (half each side)
        half_capital = pos.capital_allocated / 2
        bid_size = round(half_capital / bid_price, 2) if bid_price > 0 else 0
        ask_no_price = round(1.0 - ask_price, 2)
        ask_size = round(half_capital / ask_no_price, 2) if ask_no_price > 0 else 0

        # Inventory-aware size adjustments
        skip_bid = False
        skip_ask = False

        if abs_skew > 0.8:
            # Severe: only quote rebalancing side
            if skew > 0:
                skip_bid = True  # Stop buying YES (already long)
            else:
                skip_ask = True  # Stop buying NO (already long)
            logger.info(
                "severe_skew_one_sided",
                condition_id=pos.condition_id[:16],
                skew=round(skew, 3),
                skip="bid" if skip_bid else "ask",
            )
        elif abs_skew > 0.7:
            # Moderate: reduce long side, increase short side
            if skew > 0:
                bid_size = round(bid_size * 0.5, 2)
                ask_size = round(ask_size * 1.5, 2)
            else:
                bid_size = round(bid_size * 1.5, 2)
                ask_size = round(ask_size * 0.5, 2)

        # ── Min size check: skip orders that won't earn rewards ──
        # CRITICAL: if EITHER side fails min_size, skip the ENTIRE market.
        # One-sided quoting = pure directional risk with no spread capture.
        min_size = pos.min_size
        if min_size > 0:
            bid_fails = bid_size < min_size and not skip_bid
            ask_fails = ask_size < min_size and not skip_ask

            if bid_fails or ask_fails:
                logger.warning(
                    "skip_market_min_size_one_side",
                    condition_id=pos.condition_id[:16],
                    bid_size=round(bid_size, 2),
                    ask_size=round(ask_size, 2),
                    min_size=min_size,
                    bid_fails=bid_fails,
                    ask_fails=ask_fails,
                )
                return

        # Check balance before placing orders (live mode only)
        if not self.should_simulate and self._client:
            available = self._get_available_balance()
            bid_cost = round(bid_price * bid_size, 2) if not skip_bid else 0
            ask_cost = round(ask_no_price * ask_size, 2) if not skip_ask else 0
            total_cost = bid_cost + ask_cost
            if available is not None and total_cost > available:
                logger.warning(
                    "insufficient_balance_skip_quotes",
                    condition_id=pos.condition_id[:16],
                    needed=total_cost,
                    available=available,
                )
                return

        logger.info(
            "placing_quotes",
            condition_id=pos.condition_id[:16],
            midpoint=round(pos.midpoint, 4),
            max_spread=round(pos.max_spread, 4),
            bid_price=round(bid_price, 4),
            ask_price=round(ask_price, 4),
            ask_no_price=round(ask_no_price, 4),
            bid_size=round(bid_size, 2),
            ask_size=round(ask_size, 2),
            skip_bid=skip_bid,
            skip_ask=skip_ask,
            mode=self._config.mode,
        )

        # Place bid: BUY YES at bid_price
        if bid_size > 0 and not skip_bid:
            bid_result = await self._place_order(
                token_id=pos.yes_token_id,
                price=bid_price,
                size=bid_size,
                is_yes=True,
                condition_id=pos.condition_id,
            )
            pos.bid_order = bid_result
            logger.info(
                "bid_order_assigned",
                condition_id=pos.condition_id[:16],
                order_id=bid_result.order_id[:12] if bid_result else "NONE",
                status=bid_result.status if bid_result else "NONE",
            )

        # Place ask: BUY NO at (1 - ask_price)
        if ask_size > 0 and not skip_ask:
            ask_result = await self._place_order(
                token_id=pos.no_token_id,
                price=ask_no_price,
                size=ask_size,
                is_yes=False,
                condition_id=pos.condition_id,
            )
            pos.ask_order = ask_result
            logger.info(
                "ask_order_assigned",
                condition_id=pos.condition_id[:16],
                order_id=ask_result.order_id[:12] if ask_result else "NONE",
                status=ask_result.status if ask_result else "NONE",
            )

    async def _place_order(
        self,
        token_id: str,
        price: float,
        size: float,
        is_yes: bool,
        condition_id: str,
    ) -> QuoteOrder | None:
        """Place a single order (live or paper)."""
        if self.should_simulate:
            if self.is_dry_run:
                logger.info(
                    "dry_run_order",
                    token_id=token_id[:16],
                    price=price,
                    size=size,
                    side="BUY_YES" if is_yes else "BUY_NO",
                )
            return self._paper_place(token_id, price, size, is_yes, condition_id)

        if not self._client or not self._initialized:
            logger.warning("clob_not_initialized_skip_order")
            return None

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            order_args = OrderArgs(
                price=price,
                size=size,
                side=BUY,
                token_id=token_id,
            )

            signed = self._client.create_order(order_args)
            resp = self._client.post_order(signed, OrderType.GTC, post_only=True)

            order_id = resp.get("orderID", "") if resp else ""
            if not order_id:
                # Log full response to diagnose API rejections
                error_msg = resp.get("errorMsg", "") if resp else "empty response"
                logger.warning(
                    "order_no_id",
                    token_id=token_id[:16],
                    price=price,
                    size=size,
                    side="YES" if is_yes else "NO",
                    error=error_msg,
                    full_resp=str(resp)[:200] if resp else "None",
                )
                return None

            self._total_orders_placed += 1
            if self._metrics:
                self._metrics.record_order_placed()

            # Update available capital after order is placed (changes balance)
            await self._update_available_capital(force=True)

            order = QuoteOrder(
                order_id=order_id,
                token_id=token_id,
                side="BUY",
                price=price,
                size=size,
                is_yes=is_yes,
                condition_id=condition_id,
                placed_at=time.time(),
            )
            logger.info(
                "quote_placed",
                order_id=order_id[:12],
                side="YES" if is_yes else "NO",
                price=f"${price:.4f}",
                size=size,
            )
            return order

        except Exception as e:
            logger.error("place_order_failed", error=str(e), token_id=token_id[:16])
            self._errors += 1
            return None

    def _paper_place(
        self, token_id: str, price: float, size: float,
        is_yes: bool, condition_id: str,
    ) -> QuoteOrder:
        """Simulate order placement in paper mode."""
        order_id = f"paper-{uuid.uuid4().hex[:12]}"
        self._total_orders_placed += 1
        if self._metrics:
            self._metrics.record_order_placed()
        logger.info(
            "paper_quote_placed",
            order_id=order_id,
            side="YES" if is_yes else "NO",
            price=f"${price:.4f}",
            size=size,
        )
        return QuoteOrder(
            order_id=order_id,
            token_id=token_id,
            side="BUY",
            price=price,
            size=size,
            is_yes=is_yes,
            condition_id=condition_id,
            placed_at=time.time(),
        )

    async def _simulate_paper_fills(self):
        """Simulate realistic fills, rewards, and spread income in paper mode.

        Simulates the full P&L picture a real market maker would see:
        1. Fills: ~3% chance per cycle per market (~8-12 fills/day/market)
        2. Spread income: half-spread earned on each matched pair
        3. Rewards: proportional to capital vs market's daily_rate
        4. Adverse: small random slippage on fills (realistic)
        """
        cycle_seconds = self.quote_refresh_s  # typically 30s
        seconds_per_day = 86400

        for cid, pos in list(self._positions.items()):
            if pos.abandoned:
                continue

            # ── 1. Simulate fill (3% chance per cycle, one side only) ──
            # 3% × 2880 cycles/day = ~86 attempts → ~86 fills/day across all
            # markets. With 5 markets: ~17 fills/market/day → realistic
            if random.random() < 0.03 and (pos.bid_order or pos.ask_order):
                if pos.bid_order and pos.ask_order:
                    is_bid = random.random() < 0.50
                elif pos.bid_order:
                    is_bid = True
                else:
                    is_bid = False

                size = random.uniform(5, 15)
                if is_bid and pos.bid_order:
                    slip = random.uniform(0.0, 0.003)
                    fill_price = pos.bid_order.price - slip
                    self.record_fill(cid, is_yes=True, size=size, fill_price=fill_price)
                elif not is_bid and pos.ask_order:
                    slip = random.uniform(0.0, 0.003)
                    fill_price = pos.ask_order.price + slip
                    self.record_fill(cid, is_yes=False, size=size, fill_price=fill_price)

            # ── 2. Simulate spread income (on matched volume) ──
            # Spread income = half_spread × matched_volume, accrued per cycle
            if pos.fills_yes > 0 and pos.fills_no > 0:
                matched = min(pos.fills_yes, pos.fills_no)
                half_spread = (pos.max_spread * self._config.spread_pct_of_max) / 2
                # Income from matched inventory, prorated per cycle
                income_per_cycle = half_spread * matched * (cycle_seconds / seconds_per_day)
                if income_per_cycle > 0.0001 and self._metrics:
                    self._metrics.record_spread_income(income_per_cycle)

            # ── 3. Simulate rewards (proportional to capital/daily_rate) ──
            daily_rate = self._get_market_daily_rate(cid)
            if daily_rate > 0 and pos.capital_allocated > 0:
                # Conservative: we capture 1-5% of the market's daily rewards
                our_fraction = min(0.05, pos.capital_allocated / max(daily_rate, 1.0))
                reward_per_cycle = daily_rate * our_fraction * (cycle_seconds / seconds_per_day)
                if reward_per_cycle > 0.0001:
                    self.record_rewards(cid, reward_per_cycle)

    def _get_market_daily_rate(self, condition_id: str) -> float:
        """Get daily reward rate for a market from the scanner."""
        if not self._scanner:
            return 0.0
        for m in self._scanner.get_top_markets(self._config.max_markets + 5):
            if m.condition_id == condition_id:
                return m.daily_rate
        return 0.0

    # ── Emergency & risk (Phase 3) ───────────────────────────────────

    async def _emergency_cancel_position(self, pos: MarketPosition):
        """Cancel all orders in a position due to rapid price move."""
        if pos.bid_order and pos.bid_order.status == "active":
            await self._cancel_order(pos.bid_order)
            pos.bid_order = None
        if pos.ask_order and pos.ask_order.status == "active":
            await self._cancel_order(pos.ask_order)
            pos.ask_order = None
        pos.abandoned = True

    def record_fill(self, condition_id: str, is_yes: bool, size: float, fill_price: float):
        """Record a fill and estimate adverse selection loss.

        Adverse selection = price moved against us after fill.
        Estimated as: |fill_price - current_midpoint| * size
        """
        pos = self._positions.get(condition_id)
        if not pos:
            return

        if is_yes:
            pos.fills_yes += size
        else:
            pos.fills_no += size
        pos.fill_count += 1
        self._total_fills += 1

        # Estimate adverse selection: difference between fill price and midpoint
        midpoint = pos.midpoint
        if is_yes:
            adverse = max(0, fill_price - midpoint) * size
        else:
            adverse = max(0, (1 - fill_price) - midpoint) * size
        pos.total_adverse_loss += adverse
        if self._metrics:
            self._metrics.record_fill(adverse_amount=adverse)

        logger.info(
            "fill_recorded",
            condition_id=condition_id[:16],
            side="YES" if is_yes else "NO",
            size=size,
            fill_price=round(fill_price, 4),
            adverse=round(adverse, 4),
            skew=round(pos.inventory_skew, 3),
        )

        # Track when unmatched inventory started
        unmatched_yes = pos.fills_yes - min(pos.fills_yes, pos.fills_no)
        unmatched_no = pos.fills_no - min(pos.fills_yes, pos.fills_no)
        if (unmatched_yes > 0 or unmatched_no > 0) and pos.unmatched_since == 0:
            pos.unmatched_since = time.time()
        elif unmatched_yes == 0 and unmatched_no == 0:
            pos.unmatched_since = 0.0

        # Check for matched pairs to redeem (live mode only)
        if not self.should_simulate and pos.fills_yes > 0 and pos.fills_no > 0:
            asyncio.create_task(self._try_redeem_matched(condition_id))

    def record_rewards(self, condition_id: str, amount: float):
        """Record rewards earned for a position."""
        pos = self._positions.get(condition_id)
        if pos:
            pos.total_rewards_earned += amount
        if self._metrics:
            self._metrics.record_rewards(amount)

    # ── Auto-redeem matched pairs ────────────────────────────────────

    def set_redeem_callback(self, callback):
        """Set async callback for redeeming matched pairs.

        The callback receives a condition_id and should return True on success.
        Typically wired to executor.redeem_position().
        """
        self._on_redeem = callback

    async def _try_redeem_matched(self, condition_id: str):
        """Check if a position has matched YES+NO fills and trigger redeem.

        When both sides are filled, the matched amount (min of YES, NO fills)
        can be redeemed for $1 per pair, capturing the spread as profit and
        freeing up capital for more quoting.
        """
        pos = self._positions.get(condition_id)
        if not pos:
            return

        matched = min(pos.fills_yes, pos.fills_no)
        if matched <= 0:
            return

        # Avoid duplicate redeems
        if condition_id in self._redeem_lock:
            return

        if not self._on_redeem:
            return

        self._redeem_lock.add(condition_id)
        logger.info(
            "matched_pair_redeem",
            condition_id=condition_id[:16],
            matched_shares=round(matched, 2),
            fills_yes=round(pos.fills_yes, 2),
            fills_no=round(pos.fills_no, 2),
        )

        try:
            success = await self._on_redeem(condition_id)
            if success:
                # Clear the matched portion from fills — remaining is unmatched inventory
                pos.fills_yes -= matched
                pos.fills_no -= matched
                self._total_redeems += 1
                logger.info(
                    "matched_pair_redeemed",
                    condition_id=condition_id[:16],
                    redeemed_shares=round(matched, 2),
                    remaining_yes=round(pos.fills_yes, 2),
                    remaining_no=round(pos.fills_no, 2),
                )
            else:
                logger.warning(
                    "matched_pair_redeem_failed",
                    condition_id=condition_id[:16],
                )
        except Exception as e:
            logger.error("matched_pair_redeem_error", error=str(e))
        finally:
            self._redeem_lock.discard(condition_id)

    # ── Auto-exit unmatched inventory ─────────────────────────────────

    async def _check_auto_exits(self):
        """Sell unmatched inventory that's been sitting too long.

        When only one side fills (e.g., YES but not NO), the capital is locked
        in a directional position earning zero rewards. Better to sell at a
        small loss and free up capital for more quoting.
        """
        if self.should_simulate:
            return
        if not getattr(self._config, 'auto_exit_enabled', True):
            return
        if not self._client:
            return

        timeout = getattr(self._config, 'auto_exit_timeout_s', 300.0)
        max_loss_pct = getattr(self._config, 'auto_exit_max_loss_pct', 0.10)
        now = time.time()

        for cid, pos in list(self._positions.items()):
            if pos.unmatched_since <= 0:
                continue
            if cid in self._redeem_lock:
                continue

            elapsed = now - pos.unmatched_since
            if elapsed < timeout:
                continue

            # Determine which side is unmatched
            unmatched_yes = pos.fills_yes - min(pos.fills_yes, pos.fills_no)
            unmatched_no = pos.fills_no - min(pos.fills_yes, pos.fills_no)

            if unmatched_yes > 0.5:
                # We have excess YES tokens — sell them
                # Sell YES = place SELL order on YES token at current midpoint
                sell_price = round(pos.midpoint * (1 - max_loss_pct), 2)
                sell_price = max(0.01, sell_price)
                await self._sell_tokens(
                    pos, token_id=pos.yes_token_id, size=round(unmatched_yes, 2),
                    price=sell_price, side_label="YES",
                )

            elif unmatched_no > 0.5:
                # We have excess NO tokens — sell them
                no_price = round(1.0 - pos.midpoint, 2)
                sell_price = round(no_price * (1 - max_loss_pct), 2)
                sell_price = max(0.01, sell_price)
                await self._sell_tokens(
                    pos, token_id=pos.no_token_id, size=round(unmatched_no, 2),
                    price=sell_price, side_label="NO",
                )

    async def _sell_tokens(
        self, pos: MarketPosition, token_id: str, size: float,
        price: float, side_label: str,
    ):
        """Place a SELL order to exit unmatched inventory."""
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import SELL

            order_args = OrderArgs(
                price=price,
                size=size,
                side=SELL,
                token_id=token_id,
            )
            signed = self._client.create_order(order_args)
            resp = self._client.post_order(signed, OrderType.GTC)

            order_id = resp.get("orderID", "") if resp else ""
            if order_id:
                logger.info(
                    "auto_exit_sell_placed",
                    condition_id=pos.condition_id[:16],
                    side=side_label,
                    size=size,
                    price=price,
                    order_id=order_id[:12],
                )
                # Clear unmatched fills — capital will return when order fills
                if side_label == "YES":
                    pos.fills_yes -= size
                else:
                    pos.fills_no -= size
                pos.unmatched_since = 0.0
                self._total_auto_exits += 1
            else:
                logger.warning("auto_exit_no_order_id", condition_id=pos.condition_id[:16])

        except Exception as e:
            logger.error(
                "auto_exit_sell_failed",
                condition_id=pos.condition_id[:16],
                side=side_label,
                error=str(e),
            )

    # ── Fill detection ─────────────────────────────────────────────────

    async def _check_active_fills(self):
        """Check all active orders for fills. Called each refresh cycle.

        This is critical: without this, the bot would place orders that get
        filled but never detect it — losing capital silently.

        Updates available capital if any fills are detected (capital is freed when fills close positions).
        """
        if not self._client:
            return

        fills_detected = False
        for pos in list(self._positions.values()):
            for order in (pos.bid_order, pos.ask_order):
                if not order or order.status != "active":
                    continue
                if order.order_id.startswith("paper-"):
                    continue
                status = await self._check_order_status(order)
                if order.status != "active":
                    logger.info(
                        "order_status_changed",
                        order_id=order.order_id[:12],
                        new_status=order.status,
                        clob_status=status,
                        side="YES" if order.is_yes else "NO",
                        condition_id=order.condition_id[:16],
                    )
                # If order was filled, clear reference so a new one can be placed
                if order.status == "filled":
                    fills_detected = True
                    if order.is_yes:
                        pos.bid_order = None
                    else:
                        pos.ask_order = None

        # If any fills were detected, update capital (balance likely changed)
        if fills_detected:
            await self._update_available_capital(force=True)

    # ── Order status & fill detection ────────────────────────────────

    async def _check_order_status(self, order: QuoteOrder) -> str:
        """Query CLOB API for real order status.

        Returns the order status string (MATCHED, LIVE, CANCELLED, etc.)
        and records any fills detected.
        """
        if not self._client or order.order_id.startswith("paper-"):
            return order.status

        try:
            order_data = self._client.get_order(order.order_id)
            if not order_data:
                return order.status

            status = (order_data.get("status", "") or "").upper()
            size_matched = float(order_data.get("size_matched", 0) or 0)

            if size_matched > 0:
                # Order was (partially) filled! Record the fill
                logger.info(
                    "fill_detected_on_check",
                    order_id=order.order_id[:12],
                    status=status,
                    size_matched=round(size_matched, 4),
                    price=order.price,
                    side="YES" if order.is_yes else "NO",
                    condition_id=order.condition_id[:16],
                )
                self.record_fill(
                    condition_id=order.condition_id,
                    is_yes=order.is_yes,
                    size=size_matched,
                    fill_price=order.price,
                )
                order.status = "filled"
                return "MATCHED"

            if status in ("CANCELLED", "EXPIRED"):
                order.status = "cancelled"
                return status

            return status

        except Exception as e:
            logger.warning(
                "order_status_check_failed",
                order_id=order.order_id[:12],
                error=str(e),
            )
            return order.status

    # ── Order cancellation ────────────────────────────────────────────

    async def _cancel_order(self, order: QuoteOrder):
        """Cancel a single order, checking for fills first."""
        if order.status != "active":
            return

        if self.should_simulate:
            order.status = "cancelled"
            self._total_orders_cancelled += 1
            if self._metrics:
                self._metrics.record_order_cancelled()
            return

        if not self._client:
            return

        # Check if the order was filled before trying to cancel
        status = await self._check_order_status(order)
        if status == "MATCHED" or order.status == "filled":
            # Already filled — don't cancel, fill was recorded in _check_order_status
            logger.info(
                "skip_cancel_already_filled",
                order_id=order.order_id[:12],
                side="YES" if order.is_yes else "NO",
            )
            return

        if status in ("CANCELLED", "EXPIRED"):
            # Already cancelled/expired by the exchange
            order.status = "cancelled"
            self._total_orders_cancelled += 1
            return

        try:
            self._client.cancel(order.order_id)
            order.status = "cancelled"
            self._total_orders_cancelled += 1
            if self._metrics:
                self._metrics.record_order_cancelled()
            logger.info("quote_cancelled", order_id=order.order_id[:12])
        except Exception as e:
            # Cancel failed — the order may have been filled between our check
            # and the cancel attempt. Check status one more time.
            logger.warning("cancel_failed_checking_fill", order_id=order.order_id[:12], error=str(e))
            status = await self._check_order_status(order)
            if status != "MATCHED" and order.status != "filled":
                logger.error("cancel_failed_not_filled", order_id=order.order_id[:12], error=str(e))
                self._errors += 1

    async def _cancel_all_orders(self):
        """Emergency: cancel every outstanding order."""
        cancelled = 0
        for pos in self._positions.values():
            if pos.bid_order and pos.bid_order.status == "active":
                await self._cancel_order(pos.bid_order)
                pos.bid_order = None  # Clear reference so new order can be placed
                cancelled += 1
            if pos.ask_order and pos.ask_order.status == "active":
                await self._cancel_order(pos.ask_order)
                pos.ask_order = None  # Clear reference so new order can be placed
                cancelled += 1
        logger.info("all_orders_cancelled", count=cancelled)

    # ── Stats / API ───────────────────────────────────────────────────

    def get_stats(self) -> dict:
        active_bids = sum(1 for p in self._positions.values() if p.bid_order and p.bid_order.status == "active")
        active_asks = sum(1 for p in self._positions.values() if p.ask_order and p.ask_order.status == "active")
        total_rewards = sum(p.total_rewards_earned for p in self._positions.values())
        total_adverse = sum(p.total_adverse_loss for p in self._positions.values())
        total_scoring = self._orders_scoring + self._orders_not_scoring
        scoring_rate = self._orders_scoring / total_scoring if total_scoring > 0 else 0
        return {
            "running": self._running,
            "mode": self._config.mode,
            "active_markets": len(self._positions),
            "active_bids": active_bids,
            "active_asks": active_asks,
            "total_orders_placed": self._total_orders_placed,
            "total_orders_cancelled": self._total_orders_cancelled,
            "total_fills": self._total_fills,
            "total_rewards": round(total_rewards, 2),
            "total_adverse": round(total_adverse, 2),
            "adverse_ratio": round(total_adverse / total_rewards, 3) if total_rewards > 0 else 0,
            "emergency_cancels": self._emergency_cancels,
            "markets_abandoned": self._markets_abandoned,
            "total_redeems": self._total_redeems,
            "total_auto_exits": self._total_auto_exits,
            # Heartbeat
            "heartbeat_active": self._heartbeat_active,
            "heartbeat_count": self._heartbeat_count,
            "heartbeat_errors": self._heartbeat_errors,
            # Order scoring
            "orders_scoring": self._orders_scoring,
            "orders_not_scoring": self._orders_not_scoring,
            "scoring_rate": round(scoring_rate, 3),
            "scoring_checks": self._scoring_checks,
            "errors": self._errors,
            "uptime_s": round(time.time() - self._started_at, 1) if self._started_at else 0,
            "positions": [p.to_dict() for p in self._positions.values()],
        }

    def get_active_positions(self) -> list[dict]:
        return [p.to_dict() for p in self._positions.values()]
