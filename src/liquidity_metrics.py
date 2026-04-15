"""Liquidity Metrics — daily P&L tracking for the liquidity strategy.

Collects daily snapshots of provider performance: orders, fills, rewards,
adverse selection losses, and computes ROI.  Snapshots are stored in memory
and exposed via get_daily()/get_summary() for the panel and API.

Metrics roll over at midnight UTC automatically.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from src.liquidity_provider import LiquidityProvider

logger = structlog.get_logger("polymarket.liquidity_metrics")


@dataclass
class DailySnapshot:
    """One day of liquidity provider metrics."""

    date: str  # "2026-04-15"

    # Orders
    orders_placed: int = 0
    orders_cancelled: int = 0
    orders_filled: int = 0

    # Scoring
    scoring_checks: int = 0
    orders_scoring: int = 0
    orders_not_scoring: int = 0

    # Markets
    markets_active: int = 0
    markets_abandoned: int = 0
    emergency_cancels: int = 0

    # Revenue (USDC)
    rewards_earned: float = 0.0
    spread_income: float = 0.0

    # Losses (USDC)
    adverse_loss: float = 0.0

    # Computed
    @property
    def fill_rate(self) -> float:
        total = self.orders_placed
        return self.orders_filled / total if total > 0 else 0.0

    @property
    def scoring_rate(self) -> float:
        total = self.orders_scoring + self.orders_not_scoring
        return self.orders_scoring / total if total > 0 else 0.0

    @property
    def total_gross(self) -> float:
        return self.rewards_earned + self.spread_income

    @property
    def total_loss(self) -> float:
        return self.adverse_loss

    @property
    def net_pnl(self) -> float:
        return self.total_gross - self.total_loss

    @property
    def adverse_ratio(self) -> float:
        gross = self.total_gross
        return self.adverse_loss / gross if gross > 0 else 0.0

    def roi_pct(self, total_capital: float) -> float:
        """Daily ROI as percentage of total capital."""
        if total_capital <= 0:
            return 0.0
        return (self.net_pnl / total_capital) * 100

    def to_dict(self, total_capital: float = 250.0) -> dict:
        return {
            "date": self.date,
            "markets_active": self.markets_active,
            "markets_abandoned": self.markets_abandoned,
            "emergency_cancels": self.emergency_cancels,
            "orders_placed": self.orders_placed,
            "orders_cancelled": self.orders_cancelled,
            "orders_filled": self.orders_filled,
            "fill_rate": round(self.fill_rate, 3),
            "orders_scoring": self.orders_scoring,
            "orders_not_scoring": self.orders_not_scoring,
            "scoring_rate": round(self.scoring_rate, 3),
            "rewards_earned": round(self.rewards_earned, 2),
            "spread_income": round(self.spread_income, 2),
            "total_gross": round(self.total_gross, 2),
            "adverse_loss": round(self.adverse_loss, 2),
            "total_loss": round(self.total_loss, 2),
            "net_pnl": round(self.net_pnl, 2),
            "adverse_ratio": round(self.adverse_ratio, 3),
            "roi_pct": round(self.roi_pct(total_capital), 2),
        }


class LiquidityMetrics:
    """Collects and aggregates daily metrics for the liquidity provider.

    Usage:
        metrics = LiquidityMetrics(total_capital=250.0)
        metrics.record_order_placed()
        metrics.record_fill(adverse_amount=0.5)
        metrics.record_rewards(1.23)
        ...
        daily = metrics.get_today()
        summary = metrics.get_summary(days=7)
    """

    def __init__(self, total_capital: float = 250.0, max_days: int = 90):
        self._total_capital = total_capital
        self._max_days = max_days
        self._days: dict[str, DailySnapshot] = {}
        self._current_date = self._today()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _get_today(self) -> DailySnapshot:
        """Get or create today's snapshot, rolling over if date changed."""
        today = self._today()
        if today != self._current_date:
            self._current_date = today
            # Prune old days
            if len(self._days) > self._max_days:
                dates = sorted(self._days.keys())
                for d in dates[: len(dates) - self._max_days]:
                    del self._days[d]

        if today not in self._days:
            self._days[today] = DailySnapshot(date=today)
        return self._days[today]

    # ── Recording methods ─────────────────────────────────────────────

    def record_order_placed(self):
        self._get_today().orders_placed += 1

    def record_order_cancelled(self):
        self._get_today().orders_cancelled += 1

    def record_fill(self, adverse_amount: float = 0.0):
        day = self._get_today()
        day.orders_filled += 1
        day.adverse_loss += adverse_amount

    def record_rewards(self, amount: float):
        self._get_today().rewards_earned += amount

    def record_spread_income(self, amount: float):
        self._get_today().spread_income += amount

    def record_scoring(self, scoring: int, not_scoring: int):
        day = self._get_today()
        day.scoring_checks += 1
        day.orders_scoring = scoring
        day.orders_not_scoring = not_scoring

    def record_emergency_cancel(self):
        self._get_today().emergency_cancels += 1

    def record_market_abandoned(self):
        self._get_today().markets_abandoned += 1

    def update_active_markets(self, count: int):
        self._get_today().markets_active = count

    # ── Query methods ─────────────────────────────────────────────────

    def get_today(self) -> dict:
        return self._get_today().to_dict(self._total_capital)

    def get_daily(self, date: str) -> dict | None:
        snap = self._days.get(date)
        return snap.to_dict(self._total_capital) if snap else None

    def get_history(self, days: int = 7) -> list[dict]:
        """Get last N days of snapshots, most recent first."""
        dates = sorted(self._days.keys(), reverse=True)[:days]
        return [self._days[d].to_dict(self._total_capital) for d in dates]

    def get_summary(self, days: int = 7) -> dict:
        """Aggregate stats over last N days."""
        dates = sorted(self._days.keys(), reverse=True)[:days]
        if not dates:
            return {
                "period": "no data",
                "days": 0,
                "total_rewards": 0,
                "total_spread_income": 0,
                "total_gross": 0,
                "total_adverse": 0,
                "adverse_ratio": 0,
                "cumulative_pnl": 0,
                "roi_pct": 0,
                "avg_fill_rate": 0,
                "avg_scoring_rate": 0,
                "annualized_apy": 0,
            }

        snaps = [self._days[d] for d in dates]
        total_rewards = sum(s.rewards_earned for s in snaps)
        total_spread = sum(s.spread_income for s in snaps)
        total_gross = total_rewards + total_spread
        total_adverse = sum(s.adverse_loss for s in snaps)
        cumulative_pnl = total_gross - total_adverse
        n_days = len(snaps)

        # Average rates
        fill_rates = [s.fill_rate for s in snaps if s.orders_placed > 0]
        scoring_rates = [s.scoring_rate for s in snaps if s.scoring_checks > 0]

        roi_pct = (cumulative_pnl / self._total_capital * 100) if self._total_capital > 0 else 0
        daily_roi = roi_pct / n_days if n_days > 0 else 0
        annualized = daily_roi * 365

        return {
            "period": f"{dates[-1]} to {dates[0]}",
            "days": n_days,
            "total_rewards": round(total_rewards, 2),
            "total_spread_income": round(total_spread, 2),
            "total_gross": round(total_gross, 2),
            "total_adverse": round(total_adverse, 2),
            "adverse_ratio": round(total_adverse / total_gross, 3) if total_gross > 0 else 0,
            "cumulative_pnl": round(cumulative_pnl, 2),
            "roi_pct": round(roi_pct, 2),
            "avg_fill_rate": round(sum(fill_rates) / len(fill_rates), 3) if fill_rates else 0,
            "avg_scoring_rate": round(sum(scoring_rates) / len(scoring_rates), 3) if scoring_rates else 0,
            "annualized_apy": round(annualized, 1),
        }
