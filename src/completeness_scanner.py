"""Completeness Arbitrage Scanner — risk-free profit from pricing gaps.

In any Polymarket binary market, YES + NO must equal $1.00.
When the sum of best asks < $1.00, buying both sides and redeeming
yields guaranteed profit: $1.00 - cost_YES - cost_NO - fees - gas.

For multi-outcome markets (3+ options), the same logic applies:
if sum of all best asks < $1.00, buy all outcomes for guaranteed profit.

This module:
  1. Monitors all tracked markets via MarketTracker (fed by WebSocket)
  2. Detects gaps where sum(best_asks) < 1.0 - threshold
  3. Executes atomic buy of all outcomes (parallel order submission)
  4. Triggers redeem when both sides are held

In paper mode, simulates everything without ClobClient.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from src.fees import GAS_REDEEM_USD, taker_fee, TAKER_FEE_RATES, DEFAULT_CATEGORY, category_from_tags

if TYPE_CHECKING:
    from src.config import CredentialsConfig
    from src.market_tracker import MarketTracker, MarketState

CLOB_URL = "https://clob.polymarket.com"

# Polymarket requires marketable BUY orders to be at least $1 per leg.
MIN_ORDER_USD = 1.0

# Skip markets within this window of resolution: order book asks become stale
# (matching engine winding down), and any large gap detected here is almost
# certainly phantom liquidity priced from a leftover 1¢ ask we'd never fill.
MIN_SECONDS_TO_CLOSE = 90.0

# When only best_bid_ask is available (no book depth), the fallback sizing
# `max_cost / price` produces absurd sizes for very cheap legs (e.g. 5000 shares
# at $0.01). Refuse to trust deep-OTM phantom asks without explicit book depth.
PHANTOM_ASK_PRICE_THRESHOLD = 0.05

logger = structlog.get_logger("polymarket.completeness")


# ── Data classes ─────────────────────────────────────────────────────


@dataclass
class ArbOpportunity:
    """A detected completeness arbitrage opportunity."""

    condition_id: str
    question: str
    token_ids: list[str]       # [yes_token_id, no_token_id] or N outcomes
    prices: list[float]        # best ask for each outcome
    sizes: list[float]         # available size at best ask for each outcome
    gap: float                 # 1.0 - sum(prices) — positive = arb exists
    total_fees: float          # sum of taker fees for all buys
    gas_cost: float            # redeem gas
    net_profit_per_share: float  # gap - total_fees - gas_cost
    max_shares: float          # min(sizes) — max executable shares
    detected_at: float = 0.0
    category: str = "crypto"


@dataclass
class ArbTrade:
    """Record of an executed arb trade."""

    trade_id: str
    condition_id: str
    question: str
    shares: float
    cost_total: float          # sum of (price_i * shares) for all outcomes
    expected_profit: float     # (1.0 * shares) - cost_total - fees - gas
    fees_paid: float
    status: str = "pending"    # pending, confirmed, redeemed, failed
    created_at: float = 0.0
    redeemed_at: float = 0.0
    actual_pnl: float = 0.0
    order_ids: list[str] = field(default_factory=list)
    mode: str = "paper"


# ── Scanner ──────────────────────────────────────────────────────────


class CompletenessScanner:
    """Monitors markets for completeness arbitrage opportunities.

    Lifecycle:
        scanner = CompletenessScanner(config, tracker, credentials)
        await scanner.start()
        ...
        await scanner.stop()
    """

    def __init__(
        self,
        config,  # CompletenessConfig
        tracker: MarketTracker | None = None,
        credentials: CredentialsConfig | None = None,
    ):
        self._config = config
        self._tracker = tracker
        self._credentials = credentials

        # ClobClient — initialized in start() for live
        self._client = None
        self._initialized = False

        # State
        self._running = False
        self._scan_task: asyncio.Task | None = None
        self._trades: list[ArbTrade] = []
        self._pending_redeems: set[str] = set()  # condition_ids waiting for redeem

        # Cooldown: avoid spamming the same market
        self._cooldown: dict[str, float] = {}  # condition_id -> last_attempt_time

        # Stats
        self._total_scans = 0
        self._opportunities_found = 0
        self._trades_executed = 0
        self._trades_failed = 0
        self._total_profit = 0.0
        self._started_at: float = 0.0

        # Live balance cache (pUSD free) — refreshed on demand before execution
        self._live_balance: float | None = None
        self._last_balance_fetch: float = 0.0

        # Redeem callback (set by strategy/runner)
        self._on_redeem = None

    def reset_stats(self) -> None:
        """Reset all stats and trades — called on mode change."""
        self._trades.clear()
        self._pending_redeems.clear()
        self._cooldown.clear()
        self._total_scans = 0
        self._opportunities_found = 0
        self._trades_executed = 0
        self._trades_failed = 0
        self._total_profit = 0.0
        self._started_at = time.time()
        logger.info("completeness_stats_reset")

    # ── Properties ───────────────────────────────────────────────────

    @property
    def is_paper(self) -> bool:
        return self._config.mode == "paper"

    @property
    def should_simulate(self) -> bool:
        return self._config.mode in ("paper", "dry_run")

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self):
        if self._running:
            return

        if not self.should_simulate:
            await self._init_clob_client()

        self._running = True
        self._started_at = time.time()
        self._scan_task = asyncio.create_task(self._scan_loop())

        logger.info(
            "completeness_scanner_started",
            mode=self._config.mode,
            scan_interval=self._config.scan_interval,
            min_profit=self._config.min_profit_per_share,
        )

    async def stop(self):
        self._running = False
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
        self._scan_task = None
        logger.info("completeness_scanner_stopped")

    def set_redeem_callback(self, callback):
        """Set callback for redeeming positions: async callback(condition_id) -> bool."""
        self._on_redeem = callback

    # ── ClobClient init ──────────────────────────────────────────────

    async def _init_clob_client(self):
        if not self._credentials:
            logger.error("no_credentials_for_completeness_live")
            return

        try:
            from py_clob_client_v2 import ApiCreds, ClobClient

            private_key = self._credentials.get_private_key()
            sig_type = self._credentials.signature_type
            proxy_address = self._credentials.get_proxy_address()

            funder = proxy_address
            if not funder:
                from eth_account import Account
                account = Account.from_key(private_key)
                funder = account.address

            try:
                api_key = self._credentials.get_api_key()
                api_secret = self._credentials.get_api_secret()
                passphrase = self._credentials.get_passphrase()
            except EnvironmentError:
                logger.info("deriving_api_creds_completeness")
                tmp = ClobClient(
                    host=CLOB_URL, key=private_key,
                    chain_id=137, signature_type=sig_type, funder=funder,
                )
                creds = tmp.create_or_derive_api_key()
                api_key = creds.api_key
                api_secret = creds.api_secret
                passphrase = creds.api_passphrase

            self._client = ClobClient(
                host=CLOB_URL, key=private_key, chain_id=137,
                signature_type=sig_type, funder=funder,
                creds=ApiCreds(
                    api_key=api_key,
                    api_secret=api_secret,
                    api_passphrase=passphrase,
                ),
            )
            self._initialized = True
            logger.info("completeness_clob_initialized", sig_type=sig_type)

        except Exception as e:
            logger.error("completeness_clob_init_failed", error=str(e))

    # ── Scan loop ────────────────────────────────────────────────────

    async def _scan_loop(self):
        """Periodically scan all tracked markets for arb opportunities."""
        while self._running:
            try:
                await self._scan_all()
            except Exception as e:
                logger.error("completeness_scan_error", error=str(e))
            await asyncio.sleep(self._config.scan_interval)

    async def _scan_all(self):
        """Scan all markets in tracker for completeness gaps."""
        if not self._tracker:
            return

        self._total_scans += 1
        now = time.time()

        # Check all tracked markets
        seen_conditions: set[str] = set()
        total_markets = 0
        skipped_resolved = 0
        skipped_no_data = 0
        skipped_no_asks = 0
        skipped_cooldown = 0
        evaluated = 0
        best_gap = -99.0  # Track closest to gap (negative = sum > 1.0)
        best_gap_question = ""
        best_gap_fee_rate = -1.0
        positive_gaps = 0  # Markets with gap > 0 (but maybe not enough after fees)

        for market in self._tracker.all_markets:
            if market.condition_id in seen_conditions:
                continue
            seen_conditions.add(market.condition_id)
            total_markets += 1

            # Skip resolved markets
            if market.resolved:
                skipped_resolved += 1
                continue

            # Skip markets with no price data (never received WS update)
            if market.last_update == 0:
                skipped_no_data += 1
                continue

            # Skip markets without valid asks on both sides
            if market.best_ask_yes <= 0 or market.best_ask_no <= 0:
                skipped_no_asks += 1
                continue

            # Track raw gap for diagnostics (before cooldown/filters)
            raw_gap = 1.0 - market.best_ask_yes - market.best_ask_no
            if raw_gap > 0:
                positive_gaps += 1
            if raw_gap > best_gap:
                best_gap = raw_gap
                best_gap_question = market.question[:50]
                best_gap_fee_rate = getattr(market, 'fee_rate', -1.0)

            # Cooldown check
            last_attempt = self._cooldown.get(market.condition_id, 0)
            if now - last_attempt < self._config.cooldown_s:
                skipped_cooldown += 1
                continue

            evaluated += 1
            opp = self._evaluate_market(market)
            if opp and opp.net_profit_per_share >= self._config.min_profit_per_share:
                self._opportunities_found += 1
                logger.info(
                    "arb_opportunity_detected",
                    condition_id=market.condition_id[:16],
                    question=market.question[:50],
                    gap=round(opp.gap, 4),
                    net_profit=round(opp.net_profit_per_share, 4),
                    max_shares=round(opp.max_shares, 2),
                    total_profit=round(opp.net_profit_per_share * opp.max_shares, 4),
                    category=opp.category,
                )
                await self._execute_arb(opp)

        # Periodic diagnostic log (every 20 scans)
        if self._total_scans % 20 == 1:
            logger.info(
                "completeness_scan_diag",
                total_markets=total_markets,
                skipped_no_data=skipped_no_data,
                skipped_no_asks=skipped_no_asks,
                skipped_resolved=skipped_resolved,
                skipped_cooldown=skipped_cooldown,
                evaluated=evaluated,
                positive_gaps=positive_gaps,
                best_gap=round(best_gap, 5) if best_gap > -99 else "none",
                best_gap_fee_rate=best_gap_fee_rate,
                best_gap_question=best_gap_question,
                opportunities=self._opportunities_found,
            )

    def _evaluate_market(self, market: MarketState) -> ArbOpportunity | None:
        """Check if a binary market has a completeness gap.

        Returns ArbOpportunity if gap > fees + gas, else None.
        """
        # Need valid asks on both sides
        if market.best_ask_yes <= 0 or market.best_ask_no <= 0:
            return None

        # Skip markets too close to resolution: book is unreliable and we
        # can't redeem before close. Large gaps here are almost always stale.
        hours_left = market.hours_to_resolution
        if hours_left is not None and hours_left * 3600 < MIN_SECONDS_TO_CLOSE:
            return None

        # Binary market: 2 outcomes
        prices = [market.best_ask_yes, market.best_ask_no]
        token_ids = [market.yes_token_id, market.no_token_id]

        # Sizes available at best ask. Prefer real order book depth.
        yes_size = market.asks_yes[0].size if market.asks_yes else 0
        no_size = market.asks_no[0].size if market.asks_no else 0

        # Fallback when only best_bid_ask is available (no book snapshot yet).
        # For deep-OTM legs (price < threshold), refuse to fabricate size — those
        # asks are typically stale 1¢ leftovers, not real fillable liquidity.
        if yes_size == 0 and market.best_ask_yes > 0:
            if market.best_ask_yes < PHANTOM_ASK_PRICE_THRESHOLD:
                return None
            yes_size = self._config.max_cost_per_trade / market.best_ask_yes
        if no_size == 0 and market.best_ask_no > 0:
            if market.best_ask_no < PHANTOM_ASK_PRICE_THRESHOLD:
                return None
            no_size = self._config.max_cost_per_trade / market.best_ask_no

        sizes = [yes_size, no_size]

        # Min executable shares (limited by smallest side)
        max_shares = min(sizes)
        if max_shares < self._config.min_shares:
            return None

        # Cap shares to max_cost
        max_cost = self._config.max_cost_per_trade
        cost_per_share = sum(prices)
        if cost_per_share > 0:
            shares_by_cost = max_cost / cost_per_share
            max_shares = min(max_shares, shares_by_cost)

        if max_shares < self._config.min_shares:
            return None

        # Polymarket requires each marketable BUY leg to be ≥ $1 of notional.
        # Binding constraint is the cheap leg: shares × min(prices) ≥ 1.
        # If we can't satisfy this within max_cost_per_trade, skip.
        min_price = min(p for p in prices if p > 0)
        if max_shares * min_price < MIN_ORDER_USD:
            return None

        # Gap calculation
        price_sum = sum(prices)
        gap = 1.0 - price_sum

        if gap <= 0:
            return None  # No arb (sum >= 1.0)

        # Fee calculation: we're taker on all buys
        # Use per-market fee_rate from Gamma API if available, else fallback to tag/config
        if hasattr(market, 'fee_rate') and market.fee_rate >= 0:
            fee_rate = market.fee_rate
            category = "api"  # Indicates fee_rate came from API, not category lookup
        elif hasattr(market, 'tags') and market.tags:
            category = category_from_tags(market.tags)
            fee_rate = TAKER_FEE_RATES.get(category, TAKER_FEE_RATES[DEFAULT_CATEGORY])
        else:
            category = self._config.category
            fee_rate = TAKER_FEE_RATES.get(category, TAKER_FEE_RATES[DEFAULT_CATEGORY])
        total_fees_per_share = sum(
            fee_rate * p * (1.0 - p) for p in prices
        )

        net_profit_per_share = gap - total_fees_per_share - GAS_REDEEM_USD

        if net_profit_per_share <= 0:
            return None

        return ArbOpportunity(
            condition_id=market.condition_id,
            question=market.question,
            token_ids=token_ids,
            prices=prices,
            sizes=sizes,
            gap=gap,
            total_fees=total_fees_per_share * max_shares,
            gas_cost=GAS_REDEEM_USD,
            net_profit_per_share=net_profit_per_share,
            max_shares=round(max_shares, 2),
            detected_at=time.time(),
            category=category,
        )

    @staticmethod
    def _fee_per_share(price: float, category: str) -> float:
        """Taker fee per share at a given price."""
        rate = TAKER_FEE_RATES.get(category, TAKER_FEE_RATES[DEFAULT_CATEGORY])
        return rate * price * (1.0 - price)

    # ── Execution ────────────────────────────────────────────────────

    async def _execute_arb(self, opp: ArbOpportunity):
        """Execute the arb: buy all outcomes, then redeem."""
        self._cooldown[opp.condition_id] = time.time()

        shares = opp.max_shares
        cost_total = sum(p * shares for p in opp.prices)
        fees = opp.total_fees
        expected_profit = (1.0 * shares) - cost_total - fees - GAS_REDEEM_USD

        trade = ArbTrade(
            trade_id=f"arb_{uuid.uuid4().hex[:12]}",
            condition_id=opp.condition_id,
            question=opp.question,
            shares=shares,
            cost_total=round(cost_total, 4),
            expected_profit=round(expected_profit, 4),
            fees_paid=round(fees, 4),
            created_at=time.time(),
            mode="paper" if self.should_simulate else "live",
        )

        if self.should_simulate:
            await self._paper_execute(trade, opp)
        else:
            await self._live_execute(trade, opp)

    async def _paper_execute(self, trade: ArbTrade, opp: ArbOpportunity):
        """Simulate the arb trade."""
        trade.order_ids = [f"paper_{uuid.uuid4().hex[:8]}" for _ in opp.token_ids]
        trade.status = "redeemed"
        trade.redeemed_at = time.time()
        trade.actual_pnl = trade.expected_profit
        self._trades.append(trade)
        self._trades_executed += 1
        self._total_profit += trade.actual_pnl

        logger.info(
            "paper_arb_executed",
            trade_id=trade.trade_id,
            condition_id=opp.condition_id[:16],
            shares=trade.shares,
            cost=f"${trade.cost_total:.4f}",
            profit=f"${trade.actual_pnl:.4f}",
        )

    async def _live_execute(self, trade: ArbTrade, opp: ArbOpportunity):
        """Execute real arb: buy all outcomes in parallel, then redeem."""
        if not self._client:
            logger.error("no_clob_client_for_arb")
            trade.cost_total = 0.0
            trade.status = "failed"
            self._trades_failed += 1
            self._trades.append(trade)
            return

        # Pre-flight: refresh balance and skip if not enough pUSD for the full arb.
        # Avoids the half-fill scenario where leg 1 consumes balance and leg 2 fails.
        balance = await self._refresh_balance()
        if balance is not None and balance < trade.cost_total:
            logger.warning(
                "arb_skipped_insufficient_balance",
                trade_id=trade.trade_id,
                balance=f"${balance:.2f}",
                required=f"${trade.cost_total:.2f}",
                question=opp.question[:50],
            )
            trade.cost_total = 0.0
            trade.status = "failed"
            trade.actual_pnl = 0.0
            self._trades_failed += 1
            self._trades.append(trade)
            return

        try:
            from py_clob_client_v2 import OrderArgs
            from py_clob_client_v2.order_builder.constants import BUY

            # Prepare all orders
            signed_orders = []
            for i, token_id in enumerate(opp.token_ids):
                order_args = OrderArgs(
                    price=opp.prices[i],
                    size=round(trade.shares, 2),
                    side=BUY,
                    token_id=token_id,
                )
                signed = self._client.create_order(order_args)
                signed_orders.append(signed)

            # Submit all orders in parallel for atomic execution
            results = await asyncio.gather(
                *[self._post_order_async(signed) for signed in signed_orders],
                return_exceptions=True,
            )

            # Check results
            order_ids = []
            all_success = True
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(
                        "arb_order_failed",
                        token_id=opp.token_ids[i][:12],
                        error=str(result),
                    )
                    all_success = False
                    break
                order_id = result.get("orderID", "") if isinstance(result, dict) else ""
                if not order_id:
                    logger.error(
                        "arb_order_no_id",
                        token_id=opp.token_ids[i][:12],
                        response=str(result)[:200],
                    )
                    all_success = False
                    break
                order_ids.append(order_id)

            trade.order_ids = order_ids

            if not all_success:
                # Partial execution — cancel any placed orders.
                # Reset cost_total to 0: the displayed "cost" was the *expected* spend;
                # since cancellation reverses any partial fills (best-effort), the
                # net realized cost is effectively 0 when no leg matched, and the
                # half-filled case is already a residual position outside the arb.
                await self._cancel_partial_orders(order_ids)
                trade.cost_total = 0.0
                trade.status = "failed"
                self._trades_failed += 1
                self._trades.append(trade)
                return

            trade.status = "confirmed"
            self._trades.append(trade)
            self._trades_executed += 1

            # Force balance refresh on next check — both legs consumed pUSD.
            self._last_balance_fetch = 0.0

            logger.info(
                "arb_orders_placed",
                trade_id=trade.trade_id,
                condition_id=opp.condition_id[:16],
                shares=trade.shares,
                cost=f"${trade.cost_total:.4f}",
                expected_profit=f"${trade.expected_profit:.4f}",
                order_ids=[oid[:12] for oid in order_ids],
            )

            # Wait briefly for fills, then trigger redeem
            await asyncio.sleep(2)
            await self._try_redeem(trade)

        except Exception as e:
            logger.error("arb_execution_error", error=str(e))
            trade.cost_total = 0.0
            trade.status = "failed"
            self._trades_failed += 1
            self._trades.append(trade)

    async def _refresh_balance(self) -> float | None:
        """Fetch free pUSD from CLOB. Cached for 30s to avoid excess RPC calls."""
        if not self._client:
            return None
        now = time.time()
        if self._live_balance is not None and (now - self._last_balance_fetch) < 30.0:
            return self._live_balance
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType

            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: self._client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                ),
            )
            if not (isinstance(resp, dict) and "balance" in resp):
                return self._live_balance
            raw = float(resp["balance"] or 0)
            # Auto-detect format (matches executor.py): raw > 1000 ⇒ 6-decimal units.
            self._live_balance = raw / 1e6 if raw > 1000 else raw
            self._last_balance_fetch = now
            return self._live_balance
        except Exception as e:
            logger.warning("completeness_balance_fetch_failed", error=str(e))
            return self._live_balance  # fall back to cached value (may be None)

    async def _post_order_async(self, signed_order) -> dict:
        """Post an order to CLOB (sync call wrapped in executor for parallel)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self._client.post_order(signed_order)
        )

    async def _cancel_partial_orders(self, order_ids: list[str]):
        """Cancel any orders that were placed before a failure."""
        if not self._client or not order_ids:
            return
        try:
            from py_clob_client_v2.clob_types import OrderPayload
            for oid in order_ids:
                if oid:
                    try:
                        self._client.cancel_order(OrderPayload(orderID=oid))
                        logger.info("arb_partial_cancelled", order_id=oid[:12])
                    except Exception as e:
                        logger.warning(
                            "arb_partial_cancel_failed",
                            order_id=oid[:12],
                            error=str(e),
                        )
        except Exception as e:
            logger.error("arb_cancel_import_error", error=str(e))

    async def _try_redeem(self, trade: ArbTrade):
        """Attempt to redeem a confirmed arb trade."""
        if trade.status != "confirmed":
            return

        if self._on_redeem:
            try:
                success = await self._on_redeem(trade.condition_id)
                if success:
                    trade.status = "redeemed"
                    trade.redeemed_at = time.time()
                    trade.actual_pnl = trade.expected_profit
                    self._total_profit += trade.actual_pnl
                    logger.info(
                        "arb_redeemed",
                        trade_id=trade.trade_id,
                        profit=f"${trade.actual_pnl:.4f}",
                    )
                else:
                    # Redeem failed or pending — will retry later
                    self._pending_redeems.add(trade.condition_id)
                    logger.info(
                        "arb_redeem_pending",
                        trade_id=trade.trade_id,
                        condition_id=trade.condition_id[:16],
                    )
            except Exception as e:
                logger.warning("arb_redeem_failed", error=str(e))
                self._pending_redeems.add(trade.condition_id)

    # ── WebSocket callback ───────────────────────────────────────────

    async def check(self, token_id: str = "", event_type: str = ""):
        """Called by WebSocket on price updates. Checks specific market for arb.

        This enables reactive (event-driven) detection in addition to
        the periodic scan loop — catching fleeting gaps faster.
        Uses a shorter cooldown (3s) than execution cooldown (30s) to allow
        rapid re-checking when prices are moving.
        """
        if not self._tracker or not token_id:
            return

        market = self._tracker.get_by_token(token_id)
        if not market or market.resolved:
            return

        # Reactive cooldown: shorter than execution cooldown (3s vs 30s)
        # Only block if we recently EXECUTED on this market (full cooldown)
        # or recently CHECKED it reactively (short cooldown to avoid spam)
        now = time.time()
        last_attempt = self._cooldown.get(market.condition_id, 0)
        reactive_cooldown = min(self._config.cooldown_s, 3.0)
        if now - last_attempt < reactive_cooldown:
            return

        opp = self._evaluate_market(market)
        if opp and opp.net_profit_per_share >= self._config.min_profit_per_share:
            self._opportunities_found += 1
            logger.info(
                "arb_opportunity_realtime",
                condition_id=market.condition_id[:16],
                question=market.question[:50],
                gap=round(opp.gap, 4),
                net_profit=round(opp.net_profit_per_share, 4),
                max_shares=round(opp.max_shares, 2),
                category=opp.category,
            )
            await self._execute_arb(opp)

    # ── Stats ────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        recent_trades = self._trades[-20:]  # Last 20 trades

        # Quick diagnostic: scan current state
        diag = self._get_market_diagnostic()

        return {
            "running": self._running,
            "mode": self._config.mode,
            "total_scans": self._total_scans,
            "opportunities_found": self._opportunities_found,
            "trades_executed": self._trades_executed,
            "trades_failed": self._trades_failed,
            "total_profit": round(self._total_profit, 4),
            "pending_redeems": len(self._pending_redeems),
            "uptime_s": round(time.time() - self._started_at, 1) if self._started_at else 0,
            "diagnostic": diag,
            "recent_trades": [
                {
                    "trade_id": t.trade_id,
                    "question": t.question[:50],
                    "shares": t.shares,
                    "cost": round(t.cost_total, 4),
                    "profit": round(t.actual_pnl, 4),
                    "status": t.status,
                    "mode": t.mode,
                }
                for t in recent_trades
            ],
        }

    def _get_market_diagnostic(self) -> dict:
        """Quick snapshot of market state for debugging."""
        if not self._tracker:
            return {"markets_tracked": 0}

        total = 0
        with_prices = 0
        positive_gaps = 0
        best_gap = -99.0
        best_question = ""
        best_fee_rate = -1.0

        seen = set()
        for m in self._tracker.all_markets:
            if m.condition_id in seen:
                continue
            seen.add(m.condition_id)
            total += 1
            if m.best_ask_yes > 0 and m.best_ask_no > 0 and not m.resolved:
                with_prices += 1
                gap = 1.0 - m.best_ask_yes - m.best_ask_no
                if gap > 0:
                    positive_gaps += 1
                if gap > best_gap:
                    best_gap = gap
                    best_question = m.question[:50]
                    best_fee_rate = getattr(m, 'fee_rate', -1.0)

        return {
            "markets_tracked": total,
            "markets_with_prices": with_prices,
            "positive_gaps": positive_gaps,
            "best_gap": round(best_gap, 5) if best_gap > -99 else None,
            "best_gap_fee_rate": best_fee_rate,
            "best_gap_market": best_question,
        }
