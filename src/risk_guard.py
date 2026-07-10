"""Shared daily loss guard for per-strategy stop-loss.

Each strategy owns a DailyLossGuard, feeds it realized P&L as trades settle,
and consults it before placing a new bet. This is the uniform implementation
of C1 (max_daily_loss was dead code across the whole bot): the guard lives in
the same object that decides to bet AND settles P&L, so it works identically
in paper and live without coupling to the central Executor.

The counter resets at the UTC day boundary, so a tripped guard lifts on its
own the next day (matching LiquidityMetrics' rollover).
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_day() -> str:
    """Current UTC calendar day, e.g. '2026-07-10'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class DailyLossGuard:
    """Tracks realized P&L per UTC day; trips when it breaches -max_daily_loss.

    Usage:
        guard = DailyLossGuard(max_daily_loss=100.0)
        ...
        if guard.tripped():
            return  # skip: daily stop-loss hit
        ...
        guard.record(pnl)  # on each settlement (profit positive, loss negative)

    max_daily_loss <= 0 disables the guard (never trips).
    """

    def __init__(self, max_daily_loss: float = 0.0):
        self.max_daily_loss = max_daily_loss
        self._daily_pnl = 0.0
        self._day = utc_day()

    def _roll(self) -> None:
        today = utc_day()
        if today != self._day:
            self._daily_pnl = 0.0
            self._day = today

    def record(self, pnl: float) -> None:
        """Add a realized P&L amount to today's counter (loss = negative)."""
        self._roll()
        self._daily_pnl += pnl

    def tripped(self) -> bool:
        """True if today's realized P&L breached -max_daily_loss."""
        self._roll()
        return self.max_daily_loss > 0 and self._daily_pnl <= -self.max_daily_loss

    @property
    def daily_pnl(self) -> float:
        self._roll()
        return self._daily_pnl

    def reset(self) -> None:
        """Clear the counter (e.g. on mode switch). Keeps max_daily_loss."""
        self._daily_pnl = 0.0
        self._day = utc_day()
