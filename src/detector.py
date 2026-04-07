from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import structlog

from src.config import RiskConfig, StrategyConfig
from src.market_tracker import MarketState, MarketTracker
from src.price_checker import CRYPTO_BUFFER_PCT, CRYPTO_SYMBOLS, PriceChecker

# All crypto names we recognize (CRYPTO_SYMBOLS keys + extras not on Binance)
_CRYPTO_NAMES = set(CRYPTO_SYMBOLS.keys()) | {
    "hyperliquid", "toncoin", "shiba",
}

logger = structlog.get_logger("polymarket.detector")

# Polymarket fee: 0.003 * min(price, 1-price) * size (taker, 30bps)
TAKER_FEE_RATE = 0.003
GAS_REDEEM_USD = 0.004


@dataclass
class Opportunity:
    """Represents a detected closing arbitrage opportunity."""

    timestamp: float
    condition_id: str
    question: str
    token_side: str  # YES or NO
    token_id: str  # The actual token ID to buy
    token_price: float  # Best ask of the candidate token
    implied_probability: float
    margin_gross: float  # 1.00 - price
    fee_estimated: float
    margin_net: float
    depth_at_price: float  # Available size at best ask
    resolved: bool  # Whether the market has already resolved
    winning_token_id: str
    hours_remaining: float = 0.0
    min_probability_required: float = 0.0
    suggested_bet: float = 0.0  # Suggested bet size based on Kelly %
    potential_profit: float = 0.0  # Estimated profit for suggested bet

    # Outcome tracking (filled when market resolves)
    outcome: str = "pending"  # "pending", "win", "loss"
    actual_pnl: float = 0.0  # Simulated P&L (positive = profit, negative = loss)
    resolved_at: float = 0.0  # Timestamp when outcome was determined

    # Duration tracking
    disappeared_at: float = 0.0  # When opportunity was no longer detected
    duration_seconds: float = 0.0  # How long the opportunity lasted

    # Strategy type for analytics
    strategy_type: str = "updown_directional"  # "updown_directional", "closing_arb_pre", "closing_arb_post"


class ClosingArbitrageDetector:
    """Detects closing arbitrage opportunities.

    Looks for markets where:
    - A token is priced >= min_implied_probability (e.g., $0.95+)
    - The margin after fees is >= min_margin_net
    - Optionally, the market has already resolved (safest)
    """

    def __init__(self, config: StrategyConfig, tracker: MarketTracker, risk: RiskConfig | None = None,
                 starting_balance: float = 500.0):
        self.config = config
        self.tracker = tracker
        self.risk = risk or RiskConfig()
        self._starting_balance = starting_balance
        self._balance = starting_balance  # Current simulated balance (dynamic)
        self._opportunities_log: list[Opportunity] = []
        # Track last logged price per market+side to avoid spam
        self._last_logged: dict[str, float] = {}  # "condition_id:side" -> price
        # Track last log timestamp to throttle frequent events
        self._last_log_time: dict[str, float] = {}  # "condition_id:side:event" -> timestamp
        self._active_opportunities: dict[str, Opportunity] = {}  # "condition_id:side" -> latest opp
        self._bet_placed: dict[str, Opportunity] = {}  # "condition_id:side" -> first bet (for paper trading)
        self._settled_conditions: set[str] = set()  # Already settled condition_ids
        self._price_checker = PriceChecker(
            min_buffer_pct=self.config.min_buffer_pct,
            crypto_configs=self.config.crypto_configs,
        )
        self._on_opportunity_cb = None  # async callback(Opportunity) for executor
        self._on_redeem_cb = None      # async callback(condition_id: str) for auto-redeem
        # Dirty flag: only re-check a token if its price moved significantly
        self._last_check_price: dict[str, float] = {}  # token_id -> last checked price
        self._dirty_threshold_pct = 1.5  # Min price change (%) to trigger re-check
        self._stats = {
            "total_scans": 0,
            "opportunities_found": 0,
            "resolved_opportunities": 0,
            "settled_wins": 0,
            "settled_losses": 0,
            "simulated_pnl": 0.0,
            "price_checks_confirmed": 0,
            "price_checks_rejected": 0,
            "price_checks_uncertain": 0,
        }

    def on_opportunity(self, callback):
        """Register an async callback to be called when a new bet is placed."""
        self._on_opportunity_cb = callback

    def on_redeem(self, callback):
        """Register async callback for redeeming winning positions."""
        self._on_redeem_cb = callback

    async def _safe_redeem(self, condition_id: str):
        """Fire redeem callback, swallowing errors."""
        if not self._on_redeem_cb:
            logger.warning(
                "redeem_callback_not_registered",
                condition_id=condition_id[:20] + "...",
            )
            return
        logger.info(
            "redeem_callback_calling",
            condition_id=condition_id[:20] + "...",
        )
        try:
            await self._on_redeem_cb(condition_id)
        except Exception as e:
            logger.warning("redeem_callback_error", error=str(e))

    def set_live_balance(self, balance: float):
        """Update balance from live executor (replaces simulated balance)."""
        self._balance = balance

    def reset_stats(self, new_balance: float | None = None):
        """Reset all stats and bets (e.g. when switching from paper to live)."""
        self._opportunities_log.clear()
        self._active_opportunities.clear()
        self._bet_placed.clear()
        self._settled_conditions.clear()
        self._last_logged.clear()
        # Don't clear _last_log_time - keep throttle state to avoid spam on mode switch
        self._last_check_price.clear()  # Clear dirty flags for fresh checks in new mode
        if new_balance is not None:
            self._balance = new_balance
            self._starting_balance = new_balance
        else:
            self._balance = self._starting_balance
        self._stats = {
            "total_scans": self._stats.get("total_scans", 0),  # keep scan count
            "opportunities_found": 0,
            "resolved_opportunities": 0,
            "settled_wins": 0,
            "settled_losses": 0,
            "simulated_pnl": 0.0,
            "price_checks_confirmed": 0,
            "price_checks_rejected": 0,
            "price_checks_uncertain": 0,
        }
        logger.info("detector_stats_reset", balance=f"${self._balance:.2f}")

    async def close(self):
        """Close resources (Binance session)."""
        await self._price_checker.close()

    async def check(self, token_id: str = "", event_type: str = ""):
        """Check market(s) for closing arbitrage opportunities.

        If token_id is provided, only checks the specific market that was
        updated — O(1) instead of O(N). Full scans run periodically or
        on resolution events.
        """
        if token_id and event_type != "resolution_check":
            # Fast path: only check the market that received the update
            market = self.tracker.get_by_token(token_id)
            if not market or market.is_stale:
                return

            # Dirty flag: skip if THIS token's price hasn't moved since last check
            # Always allow resolved markets through (need to settle bets)
            if not market.resolved:
                is_yes = token_id == market.yes_token_id
                current_price = market.best_ask_yes if is_yes else market.best_ask_no
                last_price = self._last_check_price.get(token_id, 0)
                if last_price > 0 and current_price > 0:
                    change_pct = abs(current_price - last_price) / last_price * 100
                    if change_pct < self._dirty_threshold_pct:
                        return  # Price didn't move enough, skip
                self._last_check_price[token_id] = current_price

            self._stats["total_scans"] += 1

            if market.resolved and market.winning_token_id:
                self._settle_pending(market)
                self._check_resolved_market(market)
            else:
                still_active: set[str] = set()
                self._check_pre_resolution(market, still_active)
                self._cleanup_disappeared_for_market(market, still_active)
            return

        # Full scan: resolution checks, rest fallback, or periodic
        still_active: set[str] = set()
        resolved_count = 0
        for market in self.tracker.all_markets:
            if market.is_stale:
                continue
            if market.resolved and market.winning_token_id:
                resolved_count += 1
                logger.debug(
                    "resolved_market_detected",
                    condition_id=market.condition_id[:20] + "...",
                    question=market.question[:40] + "...",
                )
                self._settle_pending(market)
                self._check_resolved_market(market)
                continue
            self._check_pre_resolution(market, still_active)
        if resolved_count > 0:
            logger.debug("full_scan_resolved_count", count=resolved_count)

        # Mark disappeared opportunities
        now = time.time()
        for key in list(self._active_opportunities):
            if key not in still_active:
                opp = self._active_opportunities.pop(key)
                if opp.disappeared_at == 0:
                    opp.disappeared_at = now
                    opp.duration_seconds = round(now - opp.timestamp, 1)
                    logger.info(
                        "opportunity_disappeared",
                        question=opp.question[:60],
                        side=opp.token_side,
                        price=f"${opp.token_price:.4f}",
                        duration=f"{opp.duration_seconds:.0f}s",
                    )

    def _settle_pending(self, market: MarketState):
        """When a market resolves, settle the paper trading bet for this market.

        Only the FIRST bet per market+side counts (stored in _bet_placed).
        All log entries for the same market get their outcome updated for display.
        """
        if market.condition_id in self._settled_conditions:
            logger.debug(
                "settle_already_processed",
                condition_id=market.condition_id[:20] + "...",
            )
            return
        self._settled_conditions.add(market.condition_id)
        logger.info(
            "settle_pending_start",
            condition_id=market.condition_id[:20] + "...",
            question=market.question[:40] + "...",
        )

        winning_id = market.winning_token_id
        winning_side = "YES" if winning_id == market.yes_token_id else "NO"
        now = time.time()

        # Find bet(s) placed for this market (one per side max)
        bets_for_market = {
            k: v for k, v in self._bet_placed.items()
            if v.condition_id == market.condition_id
        }

        for key, bet_opp in bets_for_market.items():
            if bet_opp.token_side == winning_side:
                shares = bet_opp.suggested_bet / bet_opp.token_price if bet_opp.token_price > 0 else 0
                pnl = round(shares * bet_opp.margin_net, 2)
                outcome = "win"
                self._stats["settled_wins"] += 1
            else:
                pnl = round(-bet_opp.suggested_bet, 2)
                outcome = "loss"
                self._stats["settled_losses"] += 1

            self._balance = round(self._balance + pnl, 2)
            self._stats["simulated_pnl"] = round(self._stats["simulated_pnl"] + pnl, 2)

            logger.info(
                "opportunity_settled",
                outcome=outcome,
                question=bet_opp.question[:80],
                side=bet_opp.token_side,
                winning_side=winning_side,
                price=f"${bet_opp.token_price:.4f}",
                bet=f"${bet_opp.suggested_bet:.2f}",
                pnl=f"${pnl:+.2f}",
                balance=f"${self._balance:.2f}",
                cumulative_pnl=f"${self._stats['simulated_pnl']:+.2f}",
            )

            # Auto-redeem winning positions
            if outcome == "win":
                logger.info(
                    "redeem_check",
                    condition_id=market.condition_id[:20] + "...",
                    callback_registered=bool(self._on_redeem_cb),
                )
                if self._on_redeem_cb:
                    asyncio.create_task(self._safe_redeem(market.condition_id))
                else:
                    logger.warning(
                        "redeem_callback_missing",
                        condition_id=market.condition_id[:20] + "...",
                        msg="Win detected but redeem callback not registered",
                    )

            # Update the bet opportunity object
            bet_opp.outcome = outcome
            bet_opp.actual_pnl = pnl
            bet_opp.resolved_at = now
            if bet_opp.duration_seconds == 0:
                bet_opp.duration_seconds = round(now - bet_opp.timestamp, 1)

        # Mark ALL log entries for this market with the outcome (for display)
        for opp in self._opportunities_log:
            if opp.condition_id != market.condition_id or opp.outcome != "pending":
                continue
            side_outcome = "win" if opp.token_side == winning_side else "loss"
            opp.outcome = side_outcome
            opp.resolved_at = now
            if opp.duration_seconds == 0:
                opp.duration_seconds = round(now - opp.timestamp, 1)
            # Only the original bet has actual_pnl; price updates show 0
            # (their suggested_bet was already zeroed in _log_opportunity)

    def _check_resolved_market(self, market: MarketState):
        """Post-resolution: the winner is known, buy if price < $1.00.

        Only buy the winning token when price >= 0.95 to confirm the order book
        has converged and we're truly buying the winner at a small discount.
        Low prices (e.g., $0.38) indicate stale/unconverged data — too risky.
        """
        # When crypto_only is active, skip non-crypto markets
        if self.config.tag == "crypto" and not self._is_crypto_market(market.question):
            return

        # Skip disabled cryptos
        if self._is_crypto_disabled(market.question):
            return

        winning_id = market.winning_token_id
        is_yes = winning_id == market.yes_token_id
        price = market.best_ask_yes if is_yes else market.best_ask_no

        # Only buy when price is high — confirms winner and minimizes risk
        if price < 0.95 or price >= 1.0:
            return

        margin_gross = 1.0 - price
        fee = self._calculate_fee(price, 1.0)  # For 1 share
        margin_net = margin_gross - fee - GAS_REDEEM_USD

        if margin_net < self.config.min_margin_net:
            return

        side = "YES" if is_yes else "NO"
        depth = self._get_depth_at_best_ask(market, is_yes)

        suggested_bet, potential_profit = self._calculate_bet(price, margin_net, depth)

        token_id = market.yes_token_id if is_yes else market.no_token_id

        opp = Opportunity(
            timestamp=time.time(),
            condition_id=market.condition_id,
            question=market.question,
            token_side=side,
            token_id=token_id,
            token_price=price,
            implied_probability=price,
            margin_gross=margin_gross,
            fee_estimated=fee,
            margin_net=margin_net,
            depth_at_price=depth,
            resolved=True,
            winning_token_id=winning_id,
            suggested_bet=suggested_bet,
            potential_profit=potential_profit,
            strategy_type="closing_arb_post",  # Post-resolution closing arbitrage
        )

        self._log_opportunity(opp)

        # Post-resolution bets are always wins — settle immediately
        if opp.suggested_bet > 0:
            key = f"{opp.condition_id}:{opp.token_side}"
            bet_opp = self._bet_placed.get(key)
            if bet_opp and bet_opp.outcome == "pending":
                shares = bet_opp.suggested_bet / bet_opp.token_price if bet_opp.token_price > 0 else 0
                pnl = round(shares * bet_opp.margin_net, 2)
                bet_opp.outcome = "win"
                bet_opp.actual_pnl = pnl
                bet_opp.resolved_at = time.time()
                self._balance = round(self._balance + pnl, 2)
                self._stats["settled_wins"] += 1
                self._stats["simulated_pnl"] = round(self._stats["simulated_pnl"] + pnl, 2)
                logger.info(
                    "post_resolution_settled",
                    question=bet_opp.question[:80],
                    price=f"${bet_opp.token_price:.4f}",
                    pnl=f"${pnl:+.2f}",
                    balance=f"${self._balance:.2f}",
                )
                # Auto-redeem winning position
                if self._on_redeem_cb:
                    asyncio.create_task(self._safe_redeem(market.condition_id))

    def _is_crypto_market(self, question: str) -> bool:
        """Check if a market question mentions a known cryptocurrency."""
        q_lower = question.lower()
        return any(name in q_lower for name in _CRYPTO_NAMES)

    def _is_crypto_disabled(self, question: str) -> bool:
        """Check if the crypto in this question is disabled via config."""
        q_lower = question.lower()
        for name, cc in self.config.crypto_configs.items():
            if name in q_lower and not cc.enabled:
                return True
        return False

    def _check_pre_resolution(self, market: MarketState, still_active: set[str]):
        """Pre-resolution: look for tokens priced >= min probability for their time remaining."""
        hours = market.hours_to_resolution
        if hours is None:
            return

        # Don't bet on markets that have reached resolution time — outcome is unknown
        if hours <= 0:
            return

        # When crypto_only is active, skip non-crypto markets entirely
        if self.config.tag == "crypto" and not self._is_crypto_market(market.question):
            return

        # Skip disabled cryptos (e.g. Hyperliquid)
        if self._is_crypto_disabled(market.question):
            return

        min_prob = self.config.get_min_probability(hours)

        # For Up/Down crypto markets: only use Binance directional confirmation
        # Closing Arb Pre disabled (insufficient margin, high latency disadvantage)
        price_check = self._price_checker.check_direction(market.question)
        if price_check is not None:
            self._check_up_down_market(market, price_check, hours, still_active)
            return

        # If this IS an Up/Down market but Binance data isn't cached yet, skip it.
        # Don't let it fall through to closing arb logic (tiny margins, bad risk/reward).
        if "up or down" in market.question.lower():
            return

        # Non Up/Down markets: use order book price only (original logic)
        if market.best_ask_yes >= market.best_ask_no:
            if market.best_ask_yes >= min_prob:
                self._evaluate_side(market, is_yes=True, min_prob=min_prob, hours_remaining=hours)
                still_active.add(f"{market.condition_id}:YES")
        else:
            if market.best_ask_no >= min_prob:
                self._evaluate_side(market, is_yes=False, min_prob=min_prob, hours_remaining=hours)
                still_active.add(f"{market.condition_id}:NO")

    def _count_recent_bets(self, window_seconds: float = 300) -> int:
        """Count ALL pending bets placed in the last N seconds.

        Counts both directional and closing arb bets to limit total
        correlated exposure. A wrong signal or market reversal causes
        simultaneous losses across all open positions.
        """
        now = time.time()
        count = 0
        for opp in self._bet_placed.values():
            if opp.outcome != "pending":
                continue
            if now - opp.timestamp < window_seconds:
                count += 1
        return count

    def _check_up_down_market(
        self, market: MarketState, price_check: dict, hours: float, still_active: set[str]
    ):
        """Up/Down crypto strategy: buy the Binance-confirmed direction.

        Unlike closing arbitrage (buy at $0.97+, profit $0.03), this buys at
        market price (e.g., $0.55) and profits the full margin to $1.00.
        The edge comes from Binance confirming the direction before the
        Polymarket order book fully adjusts.
        """
        # Skip disabled cryptos
        if price_check.get("disabled"):
            return

        confirmed = price_check["confirmed_side"]
        change_pct = price_check["change_pct"]

        if confirmed is None:
            # Price too close to call — skip
            self._stats["price_checks_uncertain"] += 1
            return

        is_yes = confirmed == "YES"
        price = market.best_ask_yes if is_yes else market.best_ask_no

        # Don't buy at extreme prices: too low = no data, too high = no margin
        if price <= 0 or price >= self.config.max_price:
            self._stats["price_checks_rejected"] += 1
            # Throttle log: only log once per market+side per 5 seconds
            key = f"{market.condition_id}:{confirmed}:price_out_of_range"
            now = time.time()
            last_log = self._last_log_time.get(key, 0)
            if now - last_log >= 5.0:
                logger.info(
                    "price_out_of_range",
                    question=market.question[:60],
                    side=confirmed,
                    price=price,
                    max_price=self.config.max_price,
                )
                self._last_log_time[key] = now
            return

        # Limit concurrent bets (directional + closing arb) to reduce correlated drawdowns
        key = f"{market.condition_id}:{'YES' if is_yes else 'NO'}"
        if key not in self._bet_placed:
            concurrent = self._count_recent_bets(window_seconds=300)
            if concurrent >= self.config.max_concurrent_bets:
                self._stats["price_checks_rejected"] += 1
                # Throttle log: only log once per market per 10 seconds
                log_key = f"{market.condition_id}:concurrent_limit"
                now = time.time()
                last_log = self._last_log_time.get(log_key, 0)
                if now - last_log >= 10.0:
                    logger.info(
                        "concurrent_limit",
                        question=market.question[:60],
                        concurrent_bets=concurrent,
                        max_allowed=self.config.max_concurrent_bets,
                    )
                    self._last_log_time[log_key] = now
                return

        self._stats["price_checks_confirmed"] += 1

        logger.info(
            "updown_direction_confirmed",
            question=market.question[:60],
            side=confirmed,
            price=price,
            change_pct=f"{change_pct:.4f}%",
        )

        margin_gross = 1.0 - price
        fee = self._calculate_fee(price, 1.0)
        margin_net = margin_gross - fee - GAS_REDEEM_USD

        side = "YES" if is_yes else "NO"
        depth = self._get_depth_at_best_ask(market, is_yes)

        # Dynamic bet sizing: scale based on signal strength
        # signal_strength = how far the price moved beyond the minimum buffer
        # Conservative scaling: max 1.3x to avoid amplifying losses on wrong signals
        crypto_name = price_check.get("crypto", "")
        buffer_used = price_check.get("buffer_used", self.config.min_buffer_pct)
        signal_ratio = abs(change_pct) / buffer_used if buffer_used > 0 else 1.0
        # Clamp: 1.0 (just barely confirmed) to 1.3 (strong signal)
        # Gradual: sqrt scaling to dampen extreme ratios
        signal_multiplier = min(1.5, max(1.0, 1.0 + (signal_ratio - 1.0) * 0.3))

        suggested_bet, potential_profit = self._calculate_bet(
            price, margin_net, depth, signal_multiplier=signal_multiplier,
        )
        token_id = market.yes_token_id if is_yes else market.no_token_id

        opp = Opportunity(
            timestamp=time.time(),
            condition_id=market.condition_id,
            question=market.question,
            token_side=side,
            token_id=token_id,
            token_price=price,
            implied_probability=price,
            margin_gross=margin_gross,
            fee_estimated=fee,
            margin_net=margin_net,
            depth_at_price=depth,
            resolved=False,
            winning_token_id="",
            hours_remaining=hours,
            min_probability_required=0.0,
            suggested_bet=suggested_bet,
            potential_profit=potential_profit,
            strategy_type="updown_directional",  # Up/Down with Binance confirmation
        )

        # Only log if this is a new bet (not a price update for an existing one)
        key = f"{market.condition_id}:{side}"
        if key not in self._bet_placed:
            logger.info(
                "updown_opportunity",
                question=market.question[:60],
                side=confirmed,
                price=f"${price:.4f}",
                margin=f"${margin_gross:.4f}",
                change_pct=f"{change_pct:+.4f}%",
                symbol=price_check["symbol"],
                depth=f"{depth:.1f}",
                bet=f"${suggested_bet:.2f}",
                hours_left=f"{hours:.3f}",
                buffer_pct=f"{buffer_used:.3f}%",
                signal_strength=f"{signal_multiplier:.2f}x",
            )

        self._log_opportunity(opp)
        still_active.add(f"{market.condition_id}:{side}")

    def _evaluate_side(self, market: MarketState, is_yes: bool, min_prob: float = 0.95, hours_remaining: float = 0.0,
                       strategy_type: str = "closing_arb_pre"):
        price = market.best_ask_yes if is_yes else market.best_ask_no

        if price <= 0 or price >= 1.0:
            return

        margin_gross = 1.0 - price
        fee = self._calculate_fee(price, 1.0)
        margin_net = margin_gross - fee - GAS_REDEEM_USD

        if margin_net < self.config.min_margin_net:
            return

        # Limit concurrent bets (shared with directional)
        side = "YES" if is_yes else "NO"
        key = f"{market.condition_id}:{side}"
        if key not in self._bet_placed:
            concurrent = self._count_recent_bets(window_seconds=300)
            if concurrent >= self.config.max_concurrent_bets:
                logger.info(
                    "concurrent_limit",
                    question=market.question[:60],
                    concurrent_bets=concurrent,
                    max_allowed=self.config.max_concurrent_bets,
                )
                return

        depth = self._get_depth_at_best_ask(market, is_yes)

        suggested_bet, potential_profit = self._calculate_bet(price, margin_net, depth)

        token_id = market.yes_token_id if is_yes else market.no_token_id

        opp = Opportunity(
            timestamp=time.time(),
            condition_id=market.condition_id,
            question=market.question,
            token_side=side,
            token_id=token_id,
            token_price=price,
            implied_probability=price,
            margin_gross=margin_gross,
            fee_estimated=fee,
            margin_net=margin_net,
            depth_at_price=depth,
            resolved=False,
            winning_token_id="",
            hours_remaining=hours_remaining,
            min_probability_required=min_prob,
            suggested_bet=suggested_bet,
            potential_profit=potential_profit,
            strategy_type=strategy_type,
        )

        self._log_opportunity(opp)

    def _calculate_bet(self, price: float, margin_net: float, depth: float,
                       signal_multiplier: float = 1.0) -> tuple[float, float]:
        """Calculate suggested bet size (Kelly fractional) and potential profit.

        Returns (suggested_bet_usd, potential_profit_usd).
        Bet = min(balance * max_bet_pct% * signal_multiplier, max_bet_per_trade, available_depth)

        signal_multiplier scales the bet based on signal strength:
        - 1.0 = signal just barely confirmed (change_pct == buffer)
        - 2.0 = very strong signal (change_pct >= 2x buffer)
        """
        kelly_bet = self._balance * self.risk.max_bet_pct / 100.0 * signal_multiplier
        bet = min(kelly_bet, self.risk.max_bet_per_trade)
        # Never bet more than current balance
        bet = min(bet, self._balance)

        # Don't suggest more than available depth; reject if no liquidity
        if depth <= 0:
            return 0.0, 0.0
        max_from_depth = depth * price  # depth is in shares, convert to USD
        bet = min(bet, max_from_depth)

        # Shares bought = bet / price, each share pays $1 if wins
        shares = bet / price if price > 0 else 0
        profit = shares * margin_net  # margin_net is per-share profit after fees

        return round(bet, 2), round(profit, 2)

    def _calculate_fee(self, price: float, size: float) -> float:
        """Calculate taker fee: 0.003 * min(price, 1-price) * size."""
        return TAKER_FEE_RATE * min(price, 1.0 - price) * size

    def _get_depth_at_best_ask(self, market: MarketState, is_yes: bool) -> float:
        """Get available size at the best ask level."""
        asks = market.asks_yes if is_yes else market.asks_no
        if asks:
            return asks[0].size
        return 0.0

    def _log_opportunity(self, opp: Opportunity):
        # Deduplicate: only log if price changed for this market+side
        key = f"{opp.condition_id}:{opp.token_side}"
        last_price = self._last_logged.get(key)
        if last_price == opp.token_price:
            return  # Same price, skip logging

        self._last_logged[key] = opp.token_price
        self._active_opportunities[key] = opp

        # For paper trading: only place ONE bet per market+side (first detection)
        # Subsequent price changes are logged but don't create new bets
        # GUARD: never bet on opposite side of a market we already have a bet on
        opposite_side = "NO" if opp.token_side == "YES" else "YES"
        opposite_key = f"{opp.condition_id}:{opposite_side}"
        if opposite_key in self._bet_placed:
            opp.suggested_bet = 0.0
            opp.potential_profit = 0.0
            is_new_bet = False
            logger.warning(
                "opposite_side_blocked",
                condition_id=opp.condition_id,
                blocked_side=opp.token_side,
                existing_side=opposite_side,
            )
        else:
            is_new_bet = key not in self._bet_placed
        if is_new_bet and opp.suggested_bet > 0:
            self._bet_placed[key] = opp
            # Fire executor callback (non-blocking)
            if self._on_opportunity_cb:
                asyncio.create_task(self._on_opportunity_cb(opp))
        elif is_new_bet:
            self._bet_placed[key] = opp
        else:
            # Not a new bet — log the price update but zero out the bet
            opp.suggested_bet = 0.0
            opp.potential_profit = 0.0

        self._opportunities_log.append(opp)
        self._stats["opportunities_found"] += 1
        if opp.resolved:
            self._stats["resolved_opportunities"] += 1

        # Format hours remaining for readability
        if opp.hours_remaining < 1:
            time_left = f"{opp.hours_remaining * 60:.0f}min"
        else:
            time_left = f"{opp.hours_remaining:.1f}h"

        logger.info(
            "opportunity_detected",
            type="CLOSING_RESOLVED" if opp.resolved else "CLOSING_PRE_RESOLUTION",
            question=opp.question[:80],
            side=opp.token_side,
            price=f"${opp.token_price:.4f}",
            margin_gross=f"${opp.margin_gross:.4f}",
            margin_net=f"${opp.margin_net:.4f}",
            fee=f"${opp.fee_estimated:.4f}",
            depth=f"{opp.depth_at_price:.1f}",
            time_left=time_left,
            min_prob_required=f"{opp.min_probability_required:.2f}",
            suggested_bet=f"${opp.suggested_bet:.2f}",
            potential_profit=f"${opp.potential_profit:.2f}",
            resolved=opp.resolved,
        )

    def _cleanup_disappeared_for_market(self, market, still_active: set[str]):
        """Check if opportunities for a specific market have disappeared."""
        now = time.time()
        cid = market.condition_id
        for key in list(self._active_opportunities):
            if key.startswith(cid) and key not in still_active:
                opp = self._active_opportunities.pop(key)
                if opp.disappeared_at == 0:
                    opp.disappeared_at = now
                    opp.duration_seconds = round(now - opp.timestamp, 1)
                    logger.info(
                        "opportunity_disappeared",
                        question=opp.question[:60],
                        side=opp.token_side,
                        price=f"${opp.token_price:.4f}",
                        duration=f"{opp.duration_seconds:.0f}s",
                    )

    def cleanup_market(self, condition_id: str):
        """Remove all tracking data for a market that's been cleaned up."""
        # Clean _last_logged, _active_opportunities, _last_log_time, _bet_placed
        # for this condition
        keys_to_remove = [k for k in self._last_logged if k.startswith(condition_id)]
        for k in keys_to_remove:
            self._last_logged.pop(k, None)
            self._active_opportunities.pop(k, None)

        log_time_keys = [k for k in self._last_log_time if k.startswith(condition_id)]
        for k in log_time_keys:
            self._last_log_time.pop(k, None)

        bet_keys = [k for k in self._bet_placed if k.startswith(condition_id)]
        for k in bet_keys:
            self._bet_placed.pop(k, None)

        # Drop dirty-flag entries for tokens that no longer exist in the tracker
        active_tokens = set(self.tracker.all_token_ids)
        stale_check_keys = [t for t in self._last_check_price if t not in active_tokens]
        for t in stale_check_keys:
            self._last_check_price.pop(t, None)

        # Drop settled condition entries for markets we've cleaned up
        self._settled_conditions.discard(condition_id)

        # Trim opportunities log — keep last 500 entries max
        if len(self._opportunities_log) > 500:
            self._opportunities_log = self._opportunities_log[-500:]

    def get_stats(self) -> dict:
        roi = ((self._balance - self._starting_balance) / self._starting_balance * 100) if self._starting_balance > 0 else 0
        return {
            **self._stats,
            "opportunities_logged": len(self._opportunities_log),
            "starting_balance": self._starting_balance,
            "current_balance": self._balance,
            "roi_pct": round(roi, 2),
        }

    def get_recent_opportunities(self, count: int = 10) -> list[Opportunity]:
        return self._opportunities_log[-count:]

    def export_opportunities(self) -> list[dict]:
        """Export all opportunities as dicts for analysis."""
        return [
            {
                "timestamp": o.timestamp,
                "condition_id": o.condition_id,
                "question": o.question,
                "token_side": o.token_side,
                "token_id": o.token_id,
                "token_price": o.token_price,
                "implied_probability": o.implied_probability,
                "margin_gross": o.margin_gross,
                "fee_estimated": o.fee_estimated,
                "margin_net": o.margin_net,
                "depth_at_price": o.depth_at_price,
                "resolved": o.resolved,
                "hours_remaining": o.hours_remaining,
                "min_probability_required": o.min_probability_required,
                "suggested_bet": o.suggested_bet,
                "potential_profit": o.potential_profit,
                "outcome": o.outcome,
                "actual_pnl": o.actual_pnl,
                "resolved_at": o.resolved_at,
                "disappeared_at": o.disappeared_at,
                "duration_seconds": o.duration_seconds,
                "strategy_type": o.strategy_type,  # Distinguish Up/Down vs Closing Arb
            }
            for o in self._opportunities_log
        ]

    def get_stats_by_strategy(self) -> dict:
        """Aggregate stats by strategy type: updown_directional, closing_arb_pre, closing_arb_post."""
        stats_by_type = {
            "updown_directional": {
                "total_bets": 0,
                "wins": 0,
                "losses": 0,
                "win_rate_pct": 0.0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0,
                "avg_price": 0.0,
            },
            "closing_arb_pre": {
                "total_bets": 0,
                "wins": 0,
                "losses": 0,
                "win_rate_pct": 0.0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0,
                "avg_price": 0.0,
            },
            "closing_arb_post": {
                "total_bets": 0,
                "wins": 0,
                "losses": 0,
                "win_rate_pct": 0.0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0,
                "avg_price": 0.0,
            },
        }

        # Only count bets (first opportunity per market+side)
        for opp in self._bet_placed.values():
            strategy = opp.strategy_type
            if strategy not in stats_by_type:
                continue

            stats_by_type[strategy]["total_bets"] += 1
            if opp.outcome == "win":
                stats_by_type[strategy]["wins"] += 1
            elif opp.outcome == "loss":
                stats_by_type[strategy]["losses"] += 1

            stats_by_type[strategy]["total_pnl"] += opp.actual_pnl

        # Calculate derived metrics
        for strategy in stats_by_type:
            stats = stats_by_type[strategy]
            total = stats["total_bets"]
            if total > 0:
                stats["win_rate_pct"] = round((stats["wins"] / total) * 100, 1)
                stats["avg_pnl"] = round(stats["total_pnl"] / total, 2)

        # Calculate average price (from all opportunities, not just bets)
        for strategy in stats_by_type:
            prices = [o.token_price for o in self._opportunities_log if o.strategy_type == strategy and o.token_price > 0]
            if prices:
                stats_by_type[strategy]["avg_price"] = round(sum(prices) / len(prices), 4)

        return stats_by_type

    def export_full_report(self) -> dict:
        """Export compact report with summary analytics for strategy evaluation."""
        stats = self.get_stats()

        # Use _bet_placed for P&L calculations (one bet per market+side)
        bets = [
            {
                "question": o.question[:70],
                "side": o.token_side,
                "price": round(o.token_price, 4),
                "margin": round(o.margin_net, 4),
                "bet": round(o.suggested_bet, 2),
                "outcome": o.outcome,
                "pnl": round(o.actual_pnl, 2),
                "duration_s": round(o.duration_seconds, 1),
                "hours_left": round(o.hours_remaining, 3),
                "strategy": o.strategy_type,
            }
            for o in self._bet_placed.values()
        ]

        settled_bets = [b for b in bets if b["outcome"] != "pending"]
        wins = [b for b in settled_bets if b["outcome"] == "win"]
        losses = [b for b in settled_bets if b["outcome"] == "loss"]
        pending_bets = [b for b in bets if b["outcome"] == "pending"]

        # Balance history (chronological P&L curve from settled bets)
        balance_history = []
        running_balance = self._starting_balance
        settled_opps = sorted(
            [o for o in self._bet_placed.values() if o.outcome != "pending"],
            key=lambda o: o.resolved_at,
        )
        for o in settled_opps:
            running_balance = round(running_balance + o.actual_pnl, 2)
            balance_history.append({
                "question": o.question[:70],
                "outcome": o.outcome,
                "pnl": round(o.actual_pnl, 2),
                "balance": running_balance,
            })

        # Drawdown calculation
        peak = self._starting_balance
        max_drawdown = 0.0
        for entry in balance_history:
            if entry["balance"] > peak:
                peak = entry["balance"]
            dd = (peak - entry["balance"]) / peak * 100 if peak > 0 else 0
            if dd > max_drawdown:
                max_drawdown = dd

        # Duration stats (from bets)
        durations = [b["duration_s"] for b in bets if b["duration_s"] > 0]
        avg_duration = sum(durations) / len(durations) if durations else 0
        min_duration = min(durations) if durations else 0
        max_duration = max(durations) if durations else 0

        # Average margins (from bets)
        avg_margin_net = sum(b["margin"] for b in bets) / len(bets) if bets else 0
        avg_margin_wins = sum(b["margin"] for b in wins) / len(wins) if wins else 0
        avg_margin_losses = sum(b["margin"] for b in losses) / len(losses) if losses else 0
        avg_price = sum(b["price"] for b in bets) / len(bets) if bets else 0

        return {
            "exported_at": time.time(),
            "config": {
                "starting_balance": self._starting_balance,
                "max_bet_pct": self.risk.max_bet_pct,
                "max_bet_per_trade": self.risk.max_bet_per_trade,
                "min_margin_net": self.config.min_margin_net,
                "probability_tiers": [
                    {"max_hours": t.max_hours, "min_probability": t.min_probability}
                    for t in self.config.probability_tiers
                ],
            },
            "summary": {
                "current_balance": self._balance,
                "total_pnl": stats["simulated_pnl"],
                "roi_pct": stats["roi_pct"],
                "total_bets": len(bets),
                "settled": len(settled_bets),
                "wins": len(wins),
                "losses": len(losses),
                "pending": len(pending_bets),
                "win_rate_pct": round(len(wins) / len(settled_bets) * 100, 1) if settled_bets else 0,
                "avg_win_pnl": round(sum(b["pnl"] for b in wins) / len(wins), 2) if wins else 0,
                "avg_loss_pnl": round(sum(b["pnl"] for b in losses) / len(losses), 2) if losses else 0,
                "best_trade": round(max((b["pnl"] for b in settled_bets), default=0), 2),
                "worst_trade": round(min((b["pnl"] for b in settled_bets), default=0), 2),
                "max_drawdown_pct": round(max_drawdown, 2),
                "avg_token_price": round(avg_price, 4),
                "avg_margin_net": round(avg_margin_net, 4),
                "avg_margin_wins": round(avg_margin_wins, 4),
                "avg_margin_losses": round(avg_margin_losses, 4),
                "total_wagered": round(sum(b["bet"] for b in settled_bets), 2),
                "avg_duration_seconds": round(avg_duration, 1),
                "min_duration_seconds": round(min_duration, 1),
                "max_duration_seconds": round(max_duration, 1),
            },
            "by_strategy": self.get_stats_by_strategy(),
            "balance_history": balance_history,
            "bets": bets,
        }
