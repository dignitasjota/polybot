from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import aiohttp
import structlog

from src.config import CopyTradeConfig
from src.detector import Opportunity

logger = structlog.get_logger("polymarket.copy_trader")

ACTIVITY_API = "https://data-api.polymarket.com/activity"
GAMMA_MARKETS_API = "https://gamma-api.polymarket.com/markets"
CLOB_MARKETS_API = "https://clob.polymarket.com/markets"

# Taker fee formula: 0.003 * min(price, 1-price) * size
FEE_RATE = 0.003
GAS_REDEEM_USD = 0.0005


@dataclass
class WalletTrade:
    """A trade detected from a target wallet."""
    trade_id: str
    maker_address: str
    condition_id: str
    token_id: str
    side: str  # "BUY" or "SELL"
    outcome: str  # "Up", "Down", "Yes", "No"
    price: float
    size: float
    timestamp: float
    market_slug: str
    question: str


@dataclass
class CopyBet:
    """A paper bet placed by the copy trader."""
    condition_id: str
    question: str
    token_id: str
    token_side: str  # YES or NO
    price: float
    bet_size: float  # USD cost
    potential_profit: float
    wallet_source: str  # which wallet we copied
    timestamp: float
    # Settlement
    outcome: str = "pending"  # "win", "loss", "pending"
    actual_pnl: float = 0.0
    resolved_at: float = 0.0
    duration_seconds: float = 0.0
    is_arb_boost: bool = False  # True if bet was boosted due to spread arb detection


class CopyTrader:
    """Monitors target wallets and generates copy-trade opportunities.

    Polls the Polymarket data API for recent trades by target wallets.
    When a new BUY is detected, creates an Opportunity for the executor.
    Tracks all bets and settles them when markets resolve.
    """

    def __init__(self, config: CopyTradeConfig, starting_balance: float = 500.0):
        self.config = config
        self._starting_balance = starting_balance
        self._balance = starting_balance
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._bg_task: asyncio.Task | None = None
        self._settle_task: asyncio.Task | None = None
        self._seen_trades: set[str] = set()
        self._on_opportunity_cb = None
        self._on_redeem_cb = None  # async callback(condition_id: str) for auto-redeem

        # Wallet roles: address -> "primary" or "confirmation"
        # Confirmation wallets only copy if their assigned primary already bet same side
        self._wallet_roles: dict[str, str] = {}
        # Confirmation -> primary mapping: which primary wallet to check
        self._confirms_wallet: dict[str, str] = {}
        # Track which wallets are enabled
        self._wallet_enabled: dict[str, bool] = {}

        # Bet tracking
        self._bets: dict[str, CopyBet] = {}  # "condition_id:side" -> CopyBet
        self._all_bets: list[CopyBet] = []    # All bets for export

        self._stats = {
            "polls": 0,
            "trades_seen": 0,
            "trades_copied": 0,
            "settled_wins": 0,
            "settled_losses": 0,
            "errors": 0,
            "simulated_pnl": 0.0,
            "current_balance": starting_balance,
            "starting_balance": starting_balance,
            "roi_pct": 0.0,
        }

    def set_wallet_overrides(self, overrides: dict[str, dict]):
        """Update wallet roles, confirmations and enabled status from DB overrides.

        overrides: {address: {role, enabled, confirms_wallet}}
        """
        for addr, ov in overrides.items():
            self._wallet_roles[addr] = ov.get("role", "primary")
            self._wallet_enabled[addr] = ov.get("enabled", True)
            if ov.get("confirms_wallet"):
                self._confirms_wallet[addr] = ov["confirms_wallet"]

    def on_opportunity(self, callback):
        """Register callback for when a copy-trade opportunity is detected."""
        self._on_opportunity_cb = callback

    def on_redeem(self, callback):
        """Register async callback for redeeming winning positions."""
        self._on_redeem_cb = callback

    def set_live_balance(self, balance: float):
        """Update balance from live executor (replaces simulated balance)."""
        self._balance = balance
        self._stats["current_balance"] = round(balance, 2)

    def reset_stats(self, new_balance: float | None = None):
        """Reset all stats and bets (e.g. when switching from paper to live)."""
        self._bets.clear()
        self._all_bets.clear()
        if new_balance is not None:
            self._balance = new_balance
            self._starting_balance = new_balance
        else:
            self._balance = self._starting_balance
        self._stats = {
            "polls": self._stats.get("polls", 0),  # keep poll count
            "trades_seen": 0,
            "trades_copied": 0,
            "settled_wins": 0,
            "settled_losses": 0,
            "errors": 0,
            "simulated_pnl": 0.0,
            "current_balance": round(self._balance, 2),
            "starting_balance": round(self._starting_balance, 2),
            "roi_pct": 0.0,
        }
        logger.info("copy_trader_stats_reset", balance=f"${self._balance:.2f}")

    async def start(self):
        if self._running:
            return
        if not self.config.target_wallets:
            logger.warning("copy_trader_no_wallets")
            return

        self._running = True
        self._session = aiohttp.ClientSession()
        self._bg_task = asyncio.create_task(self._poll_loop())
        self._settle_task = asyncio.create_task(self._settle_loop())
        logger.info(
            "copy_trader_started",
            wallets=len(self.config.target_wallets),
            poll_interval_ms=self.config.poll_interval_ms,
            balance=f"${self._balance:.2f}",
        )

    async def close(self):
        self._running = False
        for task in (self._bg_task, self._settle_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Polling loop ──────────────────────────────────────────────────

    async def _poll_loop(self):
        interval = self.config.poll_interval_ms / 1000.0
        while self._running:
            try:
                await self._poll_all_wallets()
            except Exception as e:
                self._stats["errors"] += 1
                logger.warning("copy_trader_poll_error", error=str(e))
            await asyncio.sleep(interval)

    async def _poll_all_wallets(self):
        if not self._session:
            return
        tasks = [
            self._poll_wallet(w)
            for w in self.config.target_wallets
            if self._wallet_enabled.get(w, True)
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._stats["polls"] += 1

    async def _poll_wallet(self, wallet: str):
        try:
            params = {"user": wallet, "type": "TRADE", "limit": 20}
            async with self._session.get(
                ACTIVITY_API, params=params,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()

            if not data:
                return

            now = time.time()
            max_age = self.config.max_latency_ms / 1000.0

            for raw in data:
                trade = self._parse_trade(raw, wallet)
                if not trade:
                    continue
                if trade.trade_id in self._seen_trades:
                    continue

                self._seen_trades.add(trade.trade_id)
                self._stats["trades_seen"] += 1

                if now - trade.timestamp > max_age:
                    continue
                if trade.side != "BUY":
                    continue

                await self._emit_opportunity(trade)

            if len(self._seen_trades) > 5000:
                self._seen_trades = set(list(self._seen_trades)[-3000:])

        except Exception as e:
            logger.debug("copy_trader_wallet_error", wallet=wallet[:10], error=str(e))

    def _parse_trade(self, raw: dict, wallet: str) -> WalletTrade | None:
        try:
            trade_id = raw.get("transactionHash", "")
            if not trade_id:
                return None
            return WalletTrade(
                trade_id=trade_id,
                maker_address=wallet,
                condition_id=raw.get("conditionId", ""),
                token_id=raw.get("asset", ""),
                side=raw.get("side", "BUY").upper(),
                outcome=raw.get("outcome", ""),
                price=float(raw.get("price", 0)),
                size=float(raw.get("size", 0)),
                timestamp=float(raw.get("timestamp", 0)),
                market_slug=raw.get("slug", ""),
                question=raw.get("title", "unknown"),
            )
        except Exception:
            return None

    def _count_recent_pending_bets(self, window_seconds: float = 300) -> int:
        """Count pending copy-trade bets placed in the last N seconds.

        Limits correlated exposure when a wallet bets on multiple
        overlapping 5-min crypto windows simultaneously.
        """
        now = time.time()
        count = 0
        for bet in self._all_bets:
            if bet.outcome != "pending" and now - bet.resolved_at > window_seconds:
                continue
            if bet.outcome == "pending" and now - bet.timestamp < window_seconds:
                count += 1
        return count

    def _find_opposite_pending_bet(self, condition_id: str, opposite_side: str) -> CopyBet | None:
        """Find an existing pending bet on the opposite side of a market.

        Searches all bets (from any wallet) for a pending bet on the given
        side of the same condition_id. Used to detect spread arb opportunities.
        """
        for key, bet in self._bets.items():
            if (bet.condition_id == condition_id
                    and bet.token_side == opposite_side
                    and bet.outcome == "pending"):
                return bet
        return None

    async def _emit_opportunity(self, trade: WalletTrade):
        outcome_lower = trade.outcome.lower()
        token_side = "YES" if outcome_lower in ("yes", "y", "up") else "NO"

        # Key includes wallet to allow confirmation bets alongside primary bets
        wallet_prefix = trade.maker_address[:10]
        key = f"{trade.condition_id}:{token_side}:{wallet_prefix}"
        if key in self._bets:
            return  # Already copied this exact trade

        # Opposite-side hedge logic (Option B):
        # Allow betting both sides ONLY when the combined price makes it a
        # profitable hedge (sum < $0.98 → guaranteed profit after ~1% fees).
        # Block when sum >= $0.98 (fees eat the spread → guaranteed loss).
        opposite_side = "NO" if token_side == "YES" else "YES"
        existing_opposite = self._find_opposite_pending_bet(trade.condition_id, opposite_side)
        if existing_opposite:
            combined_price = existing_opposite.price + trade.price
            if combined_price >= 0.98:
                logger.info(
                    "copy_trade_opposite_side_blocked",
                    wallet=wallet_prefix,
                    question=trade.question[:60],
                    side=token_side,
                    existing_side=opposite_side,
                    existing_wallet=existing_opposite.wallet_source,
                    combined_price=f"${combined_price:.4f}",
                    reason="combined price >= $0.98 — fees eat the spread",
                )
                return
            logger.info(
                "copy_trade_hedge_allowed",
                wallet=wallet_prefix,
                question=trade.question[:60],
                side=token_side,
                existing_side=opposite_side,
                existing_wallet=existing_opposite.wallet_source,
                combined_price=f"${combined_price:.4f}",
                reason="combined price < $0.98 — profitable hedge",
            )

        # Confirmation wallet logic: copy UNLESS the assigned primary wallet
        # already bet on the OPPOSITE side of the same market
        wallet_role = self._wallet_roles.get(trade.maker_address, "primary")
        if wallet_role == "confirmation":
            primary_addr = self._confirms_wallet.get(trade.maker_address, "")
            if not primary_addr:
                logger.info(
                    "copy_trade_confirmation_no_assignment",
                    wallet=wallet_prefix,
                    question=trade.question[:60],
                )
                return

            primary_prefix = primary_addr[:10]
            primary_opp_key = f"{trade.condition_id}:{opposite_side}:{primary_prefix}"

            if primary_opp_key in self._bets:
                # Primary bet opposite side → conflict, skip
                logger.info(
                    "copy_trade_confirmation_conflict",
                    wallet=wallet_prefix,
                    primary=primary_prefix,
                    question=trade.question[:60],
                    side=token_side,
                    primary_side=opposite_side,
                )
                return

            # No conflict → proceed (primary same side = double bet, no primary = solo bet)
            primary_same_key = f"{trade.condition_id}:{token_side}:{primary_prefix}"
            mode = "double" if primary_same_key in self._bets else "solo"
            logger.info(
                "copy_trade_confirmation_ok",
                wallet=wallet_prefix,
                primary=primary_prefix,
                question=trade.question[:60],
                side=token_side,
                mode=mode,
            )

        # Filter out low-price bets — but allow if we already have a bet on the
        # opposite side (hedge).  Data shows hedges at low prices are profitable
        # (+$18/7 bets) while non-hedge low-price bets are 0% WR.
        if trade.price < self.config.min_price:
            opposite = "NO" if token_side == "YES" else "YES"
            has_opposite = any(
                k.startswith(f"{trade.condition_id}:{opposite}:")
                for k in self._bets
            )
            if not has_opposite:
                logger.info(
                    "copy_trade_price_filter",
                    wallet=trade.maker_address[:10],
                    question=trade.question[:60],
                    price=f"${trade.price:.4f}",
                    min_price=self.config.min_price,
                )
                return
            logger.info(
                "copy_trade_hedge_bypass_price_filter",
                wallet=trade.maker_address[:10],
                question=trade.question[:60],
                price=f"${trade.price:.4f}",
                side=token_side,
            )

        # Limit concurrent bets to reduce correlated drawdowns
        # (e.g., 6 consecutive losses 5:30-6:00AM from overlapping windows)
        concurrent = self._count_recent_pending_bets(window_seconds=300)
        if concurrent >= self.config.max_concurrent_bets:
            logger.info(
                "copy_trade_concurrent_limit",
                wallet=trade.maker_address[:10],
                question=trade.question[:60],
                concurrent_bets=concurrent,
                max_allowed=self.config.max_concurrent_bets,
            )
            return

        # Calculate bet size
        if self.config.copy_size_mode == "proportional":
            bet_size = trade.size * trade.price * self.config.proportional_factor
        else:
            bet_size = self.config.fixed_bet_size

        # Don't bet more than balance allows
        bet_size = min(bet_size, self._balance * 0.05)  # Max 5% per trade
        if bet_size < 0.10:
            return

        fee = FEE_RATE * min(trade.price, 1 - trade.price) * (bet_size / trade.price)
        margin_net = (1.0 - trade.price) - fee / (bet_size / trade.price) - GAS_REDEEM_USD
        potential_profit = bet_size * margin_net / trade.price if trade.price > 0 else 0

        # Build opportunity for executor
        opp = Opportunity(
            timestamp=time.time(),
            condition_id=trade.condition_id,
            question=f"[COPY:{trade.maker_address[:8]}] {trade.question}",
            token_side=token_side,
            token_id=trade.token_id,
            token_price=trade.price,
            implied_probability=trade.price,
            margin_gross=1.0 - trade.price,
            fee_estimated=fee,
            margin_net=margin_net,
            depth_at_price=trade.size,
            resolved=False,
            winning_token_id="",
            hours_remaining=0.0,
            min_probability_required=0.0,
            suggested_bet=bet_size,
            potential_profit=potential_profit,
        )
        opp._token_id = trade.token_id

        # Fire executor callback BEFORE recording the bet
        # If executor fails (e.g. insufficient balance, order rejected),
        # we don't record the bet — prevents phantom bets in stats
        executed = True
        if self._on_opportunity_cb:
            try:
                result = await self._on_opportunity_cb(opp)
                if result is None:
                    executed = False
            except Exception as e:
                logger.error("copy_trade_callback_error", error=str(e))
                executed = False

        if not executed:
            logger.warning(
                "copy_trade_bet_skipped",
                wallet=trade.maker_address[:10],
                question=trade.question[:60],
                side=token_side,
                reason="executor rejected or failed",
            )
            return

        # Record the bet only after executor confirmation
        bet = CopyBet(
            condition_id=trade.condition_id,
            question=trade.question,
            token_id=trade.token_id,
            token_side=token_side,
            price=trade.price,
            bet_size=bet_size,
            potential_profit=potential_profit,
            wallet_source=trade.maker_address[:10],
            timestamp=time.time(),
        )
        self._bets[key] = bet
        self._all_bets.append(bet)
        self._balance -= bet_size
        self._stats["trades_copied"] += 1
        self._update_balance_stats()

        logger.info(
            "copy_trade_bet",
            wallet=trade.maker_address[:10],
            question=trade.question[:60],
            side=token_side,
            price=f"${trade.price:.4f}",
            bet=f"${bet_size:.2f}",
            profit=f"${potential_profit:.2f}",
            balance=f"${self._balance:.2f}",
            age_ms=int((time.time() - trade.timestamp) * 1000),
        )

    # ── Settlement loop ───────────────────────────────────────────────

    async def _settle_loop(self):
        """Periodically check if copied markets have resolved."""
        while self._running:
            await asyncio.sleep(15)  # Check every 15s
            if not self._running:
                break
            try:
                await self._settle_pending()
            except Exception as e:
                logger.warning("copy_settle_error", error=str(e))

    async def _settle_pending(self):
        """Check resolution for all pending bets via target wallet activity."""
        if not self._session:
            return

        pending = [b for b in self._all_bets if b.outcome == "pending"]
        if not pending:
            return

        # Check each target wallet for REDEEM events to discover resolved markets
        resolved_cids: set[str] = set()
        for wallet in self.config.target_wallets:
            try:
                params = {"user": wallet, "type": "REDEEM", "limit": 50}
                async with self._session.get(
                    ACTIVITY_API, params=params,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        continue
                    redeems = await resp.json()

                if not redeems:
                    continue

                for r in redeems:
                    cid = r.get("conditionId", "")
                    if cid:
                        resolved_cids.add(cid)

            except Exception as e:
                logger.debug("copy_settle_wallet_error", wallet=wallet[:10], error=str(e))

        # For resolved markets AND old bets, check via CLOB API
        # which compares OUR token_id vs the actual winner
        now = time.time()
        for bet in pending:
            should_check = (
                bet.condition_id in resolved_cids
                or (now - bet.timestamp) > 300  # 5 min — market should have resolved
            )
            if should_check:
                try:
                    await self._check_resolution_clob(bet)
                except Exception:
                    pass

    async def _check_resolution_clob(self, bet: CopyBet):
        """Check market resolution via CLOB API (reliable source of truth)."""
        if not self._session:
            return
        try:
            url = f"{CLOB_MARKETS_API}/{bet.condition_id}"
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()

            if not data:
                return

            # Check if any token has winner=true
            tokens = data.get("tokens", [])
            winning_token_id = None
            for t in tokens:
                if t.get("winner", False):
                    winning_token_id = str(t.get("token_id", ""))
                    break

            if not winning_token_id:
                return  # Not yet resolved

            won = bet.token_id == winning_token_id
            self._settle_bet(bet, won=won)

        except Exception:
            pass

    async def _safe_redeem(self, condition_id: str):
        """Fire redeem callback, swallowing errors."""
        if not self._on_redeem_cb:
            return
        try:
            await self._on_redeem_cb(condition_id)
        except Exception as e:
            logger.warning("copy_redeem_callback_error", error=str(e))

    def _settle_bet(self, bet: CopyBet, won: bool):
        """Settle a bet as win or loss."""
        if bet.outcome != "pending":
            return

        now = time.time()
        bet.resolved_at = now
        bet.duration_seconds = now - bet.timestamp

        if won:
            bet.outcome = "win"
            # Payout: shares * $1.00 = bet_size / price
            shares = bet.bet_size / bet.price if bet.price > 0 else 0
            payout = shares * 1.0
            fee = FEE_RATE * min(bet.price, 1 - bet.price) * shares
            bet.actual_pnl = round(payout - bet.bet_size - fee - GAS_REDEEM_USD, 4)
            self._balance += bet.bet_size + bet.actual_pnl  # Return cost + profit
            self._stats["settled_wins"] += 1
            # Auto-redeem winning position
            if self._on_redeem_cb:
                asyncio.create_task(self._safe_redeem(bet.condition_id))
        else:
            bet.outcome = "loss"
            bet.actual_pnl = round(-bet.bet_size, 4)
            self._stats["settled_losses"] += 1

        self._stats["simulated_pnl"] = round(
            self._balance - self._starting_balance, 2
        )
        self._update_balance_stats()

        logger.info(
            "copy_trade_settled",
            question=bet.question[:60],
            side=bet.token_side,
            outcome=bet.outcome,
            pnl=f"${bet.actual_pnl:+.2f}",
            balance=f"${self._balance:.2f}",
            duration=f"{bet.duration_seconds:.0f}s",
        )

    def _update_balance_stats(self):
        self._stats["current_balance"] = round(self._balance, 2)
        self._stats["simulated_pnl"] = round(self._balance - self._starting_balance, 2)
        pnl = self._balance - self._starting_balance
        self._stats["roi_pct"] = round(pnl / self._starting_balance * 100, 2) if self._starting_balance > 0 else 0

    # ── Export ────────────────────────────────────────────────────────

    def export_opportunities(self) -> list[dict]:
        """Export all bets for the dashboard."""
        return [
            {
                "timestamp": b.timestamp,
                "condition_id": b.condition_id,
                "question": b.question,
                "token_side": b.token_side,
                "token_id": b.token_id,
                "token_price": b.price,
                "implied_probability": b.price,
                "margin_gross": 1.0 - b.price,
                "fee_estimated": 0.0,
                "margin_net": 1.0 - b.price,
                "depth_at_price": 0,
                "resolved": b.outcome != "pending",
                "hours_remaining": 0.0,
                "min_probability_required": 0.0,
                "suggested_bet": b.bet_size,
                "potential_profit": b.potential_profit,
                "outcome": b.outcome,
                "actual_pnl": b.actual_pnl,
                "resolved_at": b.resolved_at,
                "disappeared_at": 0,
                "duration_seconds": b.duration_seconds,
                "wallet_source": b.wallet_source,
                "is_arb_boost": b.is_arb_boost,
            }
            for b in self._all_bets
        ]

    def export_full_report(self) -> dict:
        """Export complete report for JSON analysis."""
        bets = self._all_bets
        settled = [b for b in bets if b.outcome != "pending"]
        wins = [b for b in settled if b.outcome == "win"]
        losses = [b for b in settled if b.outcome == "loss"]
        pending = [b for b in bets if b.outcome == "pending"]

        # Balance history
        balance_history = []
        running = self._starting_balance
        for b in sorted(settled, key=lambda x: x.resolved_at):
            running = round(running + b.actual_pnl, 2)
            balance_history.append({
                "timestamp": b.resolved_at,
                "question": b.question[:60],
                "outcome": b.outcome,
                "pnl": b.actual_pnl,
                "balance": running,
            })

        # Drawdown
        peak = self._starting_balance
        max_drawdown = 0.0
        for entry in balance_history:
            if entry["balance"] > peak:
                peak = entry["balance"]
            dd = (peak - entry["balance"]) / peak * 100 if peak > 0 else 0
            if dd > max_drawdown:
                max_drawdown = dd

        return {
            "exported_at": time.time(),
            "strategy": "copy_trade",
            "config": {
                "starting_balance": self._starting_balance,
                "target_wallets": self.config.target_wallets,
                "fixed_bet_size": self.config.fixed_bet_size,
                "poll_interval_ms": self.config.poll_interval_ms,
                "max_latency_ms": self.config.max_latency_ms,
            },
            "summary": {
                "current_balance": round(self._balance, 2),
                "total_pnl": round(self._balance - self._starting_balance, 2),
                "roi_pct": self._stats["roi_pct"],
                "total_bets": len(bets),
                "settled": len(settled),
                "wins": len(wins),
                "losses": len(losses),
                "pending": len(pending),
                "win_rate_pct": round(len(wins) / len(settled) * 100, 1) if settled else 0,
                "avg_win_pnl": round(sum(b.actual_pnl for b in wins) / len(wins), 2) if wins else 0,
                "avg_loss_pnl": round(sum(b.actual_pnl for b in losses) / len(losses), 2) if losses else 0,
                "best_trade": round(max((b.actual_pnl for b in settled), default=0), 2),
                "worst_trade": round(min((b.actual_pnl for b in settled), default=0), 2),
                "max_drawdown_pct": round(max_drawdown, 2),
                "avg_price": round(sum(b.price for b in bets) / len(bets), 4) if bets else 0,
                "polls": self._stats["polls"],
                "trades_seen_total": self._stats["trades_seen"],
            },
            "bets": [
                {
                    "condition_id": b.condition_id,
                    "question": b.question,
                    "token_side": b.token_side,
                    "price": b.price,
                    "bet_size": b.bet_size,
                    "potential_profit": b.potential_profit,
                    "wallet_source": b.wallet_source,
                    "timestamp": b.timestamp,
                    "outcome": b.outcome,
                    "actual_pnl": b.actual_pnl,
                    "resolved_at": b.resolved_at,
                    "duration_seconds": b.duration_seconds,
                    "is_arb_boost": b.is_arb_boost,
                }
                for b in bets
            ],
            "balance_history": balance_history,
        }

    def get_stats(self) -> dict:
        return {**self._stats, "seen_cache_size": len(self._seen_trades)}
