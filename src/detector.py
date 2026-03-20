from __future__ import annotations

import time
from dataclasses import dataclass

import structlog

from src.config import StrategyConfig
from src.market_tracker import MarketState, MarketTracker

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


class ClosingArbitrageDetector:
    """Detects closing arbitrage opportunities.

    Looks for markets where:
    - A token is priced >= min_implied_probability (e.g., $0.95+)
    - The margin after fees is >= min_margin_net
    - Optionally, the market has already resolved (safest)
    """

    def __init__(self, config: StrategyConfig, tracker: MarketTracker):
        self.config = config
        self.tracker = tracker
        self._opportunities_log: list[Opportunity] = []
        # Track last logged price per market+side to avoid spam
        self._last_logged: dict[str, float] = {}  # "condition_id:side" -> price
        self._stats = {
            "total_scans": 0,
            "opportunities_found": 0,
            "resolved_opportunities": 0,
        }

    async def check(self, token_id: str = "", event_type: str = ""):
        """Check all markets for closing arbitrage opportunities."""
        self._stats["total_scans"] += 1

        for market in self.tracker.all_markets:
            if market.is_stale:
                continue

            # Check resolved markets (safest opportunity)
            if market.resolved and market.winning_token_id:
                self._check_resolved_market(market)
                continue

            # Check pre-resolution (higher risk, token priced high)
            self._check_pre_resolution(market)

    def _check_resolved_market(self, market: MarketState):
        """Post-resolution: the winner is known, buy if price < $1.00."""
        winning_id = market.winning_token_id
        is_yes = winning_id == market.yes_token_id
        price = market.best_ask_yes if is_yes else market.best_ask_no

        if price <= 0 or price >= 1.0:
            return

        margin_gross = 1.0 - price
        fee = self._calculate_fee(price, 1.0)  # For 1 share
        margin_net = margin_gross - fee - GAS_REDEEM_USD

        if margin_net < self.config.min_margin_net:
            return

        side = "YES" if is_yes else "NO"
        depth = self._get_depth_at_best_ask(market, is_yes)

        opp = Opportunity(
            timestamp=time.time(),
            condition_id=market.condition_id,
            question=market.question,
            token_side=side,
            token_price=price,
            implied_probability=price,
            margin_gross=margin_gross,
            fee_estimated=fee,
            margin_net=margin_net,
            depth_at_price=depth,
            resolved=True,
            winning_token_id=winning_id,
        )

        self._log_opportunity(opp)

    def _check_pre_resolution(self, market: MarketState):
        """Pre-resolution: look for tokens priced >= min probability for their time remaining."""
        hours = market.hours_to_resolution
        if hours is None:
            return

        min_prob = self.config.get_min_probability(hours)

        # Check YES side
        if market.best_ask_yes >= min_prob:
            self._evaluate_side(market, is_yes=True, min_prob=min_prob, hours_remaining=hours)

        # Check NO side
        if market.best_ask_no >= min_prob:
            self._evaluate_side(market, is_yes=False, min_prob=min_prob, hours_remaining=hours)

    def _evaluate_side(self, market: MarketState, is_yes: bool, min_prob: float = 0.95, hours_remaining: float = 0.0):
        price = market.best_ask_yes if is_yes else market.best_ask_no

        if price <= 0 or price >= 1.0:
            return

        margin_gross = 1.0 - price
        fee = self._calculate_fee(price, 1.0)
        margin_net = margin_gross - fee - GAS_REDEEM_USD

        if margin_net < self.config.min_margin_net:
            return

        side = "YES" if is_yes else "NO"
        depth = self._get_depth_at_best_ask(market, is_yes)

        opp = Opportunity(
            timestamp=time.time(),
            condition_id=market.condition_id,
            question=market.question,
            token_side=side,
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
        )

        self._log_opportunity(opp)

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
            resolved=opp.resolved,
        )

    def get_stats(self) -> dict:
        return {
            **self._stats,
            "opportunities_logged": len(self._opportunities_log),
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
                "token_price": o.token_price,
                "implied_probability": o.implied_probability,
                "margin_gross": o.margin_gross,
                "fee_estimated": o.fee_estimated,
                "margin_net": o.margin_net,
                "depth_at_price": o.depth_at_price,
                "resolved": o.resolved,
                "hours_remaining": o.hours_remaining,
                "min_probability_required": o.min_probability_required,
            }
            for o in self._opportunities_log
        ]
