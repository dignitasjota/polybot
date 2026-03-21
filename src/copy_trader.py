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

    def on_opportunity(self, callback):
        """Register callback for when a copy-trade opportunity is detected."""
        self._on_opportunity_cb = callback

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
        tasks = [self._poll_wallet(w) for w in self.config.target_wallets]
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

    async def _emit_opportunity(self, trade: WalletTrade):
        outcome_lower = trade.outcome.lower()
        token_side = "YES" if outcome_lower in ("yes", "y", "up") else "NO"

        # Check if we already bet on this market+side
        key = f"{trade.condition_id}:{token_side}"
        if key in self._bets:
            return  # Already copied this market

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

        # Record the bet
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

        # Fire executor callback
        if self._on_opportunity_cb:
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
            try:
                await self._on_opportunity_cb(opp)
            except Exception as e:
                logger.error("copy_trade_callback_error", error=str(e))

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

        # Check each target wallet for REDEEM events
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

                # Build map of redeemed condition_ids -> won (usdcSize > 0)
                redeemed: dict[str, bool] = {}
                for r in redeems:
                    cid = r.get("conditionId", "")
                    usdc = float(r.get("usdcSize", 0))
                    if cid:
                        # If any redeem for this cid has usdc > 0, it's a win
                        if cid not in redeemed:
                            redeemed[cid] = usdc > 0
                        elif usdc > 0:
                            redeemed[cid] = True

                # Settle matching bets
                for bet in pending:
                    if bet.condition_id in redeemed:
                        self._settle_bet(bet, won=redeemed[bet.condition_id])

            except Exception as e:
                logger.debug("copy_settle_wallet_error", wallet=wallet[:10], error=str(e))

        # Also check if markets have ended without a redeem (loss)
        # If the target wallet has new trades on different markets but
        # no redeem for our bet's market, and enough time passed, mark as loss
        now = time.time()
        for bet in pending:
            age = now - bet.timestamp
            if age > 300:  # 5 min — market should have resolved by now
                # Check via Gamma API if market is resolved
                try:
                    await self._check_resolution_gamma(bet)
                except Exception:
                    pass

    async def _check_resolution_gamma(self, bet: CopyBet):
        """Check market resolution via Gamma API."""
        if not self._session:
            return
        try:
            url = f"{GAMMA_MARKETS_API}"
            params = {"condition_id": bet.condition_id}
            async with self._session.get(
                url, params=params,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()

            if not data:
                return

            market = data[0] if isinstance(data, list) else data
            if not market.get("resolved", False):
                return

            # Market is resolved — check if we won
            winning_outcome = market.get("outcome", "").lower()
            our_outcome = "yes" if bet.token_side == "YES" else "no"

            # For Up/Down: outcome could be "Up"/"Down"
            if winning_outcome in ("up", "yes"):
                won = bet.token_side == "YES"
            elif winning_outcome in ("down", "no"):
                won = bet.token_side == "NO"
            else:
                # Try winning_token_id
                tokens = market.get("tokens", [])
                winning_token = None
                for t in tokens:
                    if t.get("winner", False):
                        winning_token = t.get("token_id", "")
                        break
                if winning_token:
                    won = bet.token_id == winning_token
                else:
                    return  # Can't determine

            self._settle_bet(bet, won=won)

        except Exception:
            pass

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
                }
                for b in bets
            ],
            "balance_history": balance_history,
        }

    def get_stats(self) -> dict:
        return {**self._stats, "seen_cache_size": len(self._seen_trades)}
