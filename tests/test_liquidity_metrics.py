"""Tests for LiquidityMetrics (daily P&L tracking).

Run: python -m pytest tests/test_liquidity_metrics.py -v
"""

import pytest

from src.liquidity_metrics import LiquidityMetrics, DailySnapshot


# ── DailySnapshot ─────────────────────────────────────────────────────

def test_snapshot_defaults():
    """Fresh snapshot should have zeroed values."""
    snap = DailySnapshot(date="2026-04-15")
    assert snap.fill_rate == 0.0
    assert snap.scoring_rate == 0.0
    assert snap.total_gross == 0.0
    assert snap.total_loss == 0.0
    assert snap.net_pnl == 0.0
    assert snap.adverse_ratio == 0.0


def test_snapshot_fill_rate():
    snap = DailySnapshot(date="2026-04-15", orders_placed=20, orders_filled=5)
    assert snap.fill_rate == 0.25


def test_snapshot_scoring_rate():
    snap = DailySnapshot(date="2026-04-15", orders_scoring=9, orders_not_scoring=1)
    assert snap.scoring_rate == 0.9


def test_snapshot_pnl():
    snap = DailySnapshot(
        date="2026-04-15",
        rewards_earned=10.0,
        spread_income=2.0,
        adverse_loss=3.0,
    )
    assert snap.total_gross == 12.0
    assert snap.total_loss == 3.0
    assert snap.net_pnl == 9.0
    assert abs(snap.adverse_ratio - 0.25) < 0.01


def test_snapshot_roi():
    snap = DailySnapshot(
        date="2026-04-15",
        rewards_earned=10.0,
        adverse_loss=2.0,
    )
    # net = 8.0, capital = 200 → 4%
    assert snap.roi_pct(200.0) == 4.0


def test_snapshot_to_dict():
    snap = DailySnapshot(
        date="2026-04-15",
        orders_placed=10,
        orders_filled=3,
        rewards_earned=5.0,
        adverse_loss=1.0,
    )
    d = snap.to_dict(total_capital=250.0)
    assert d["date"] == "2026-04-15"
    assert d["fill_rate"] == 0.3
    assert d["net_pnl"] == 4.0
    assert "roi_pct" in d


# ── LiquidityMetrics ─────────────────────────────────────────────────

def test_metrics_record_order():
    m = LiquidityMetrics(total_capital=250.0)
    m.record_order_placed()
    m.record_order_placed()
    m.record_order_cancelled()

    today = m.get_today()
    assert today["orders_placed"] == 2
    assert today["orders_cancelled"] == 1


def test_metrics_record_fill():
    m = LiquidityMetrics()
    m.record_fill(adverse_amount=0.5)
    m.record_fill(adverse_amount=0.0)

    today = m.get_today()
    assert today["orders_filled"] == 2
    assert today["adverse_loss"] == 0.5


def test_metrics_record_rewards():
    m = LiquidityMetrics()
    m.record_rewards(5.0)
    m.record_rewards(3.0)

    today = m.get_today()
    assert today["rewards_earned"] == 8.0


def test_metrics_record_spread_income():
    m = LiquidityMetrics()
    m.record_spread_income(1.5)

    today = m.get_today()
    assert today["spread_income"] == 1.5


def test_metrics_record_scoring():
    m = LiquidityMetrics()
    m.record_scoring(scoring=8, not_scoring=2)

    today = m.get_today()
    assert today["orders_scoring"] == 8
    assert today["orders_not_scoring"] == 2
    assert today["scoring_rate"] == 0.8


def test_metrics_record_emergency():
    m = LiquidityMetrics()
    m.record_emergency_cancel()
    m.record_emergency_cancel()

    today = m.get_today()
    assert today["emergency_cancels"] == 2


def test_metrics_record_abandoned():
    m = LiquidityMetrics()
    m.record_market_abandoned()

    today = m.get_today()
    assert today["markets_abandoned"] == 1


def test_metrics_active_markets():
    m = LiquidityMetrics()
    m.update_active_markets(5)

    today = m.get_today()
    assert today["markets_active"] == 5


def test_metrics_net_pnl():
    m = LiquidityMetrics(total_capital=100.0)
    m.record_rewards(10.0)
    m.record_spread_income(2.0)
    m.record_fill(adverse_amount=3.0)

    today = m.get_today()
    assert today["net_pnl"] == 9.0  # 12 - 3
    assert today["roi_pct"] == 9.0  # 9/100 * 100


def test_metrics_get_daily_missing():
    m = LiquidityMetrics()
    assert m.get_daily("2020-01-01") is None


def test_metrics_get_history():
    m = LiquidityMetrics()
    m.record_rewards(5.0)

    history = m.get_history(days=7)
    assert len(history) == 1
    assert history[0]["rewards_earned"] == 5.0


def test_metrics_get_summary_empty():
    m = LiquidityMetrics()
    summary = m.get_summary(days=7)
    assert summary["days"] == 0
    assert summary["period"] == "no data"


def test_metrics_get_summary():
    m = LiquidityMetrics(total_capital=250.0)
    m.record_rewards(10.0)
    m.record_fill(adverse_amount=2.0)
    m.record_order_placed()
    m.record_order_placed()
    m.record_scoring(9, 1)

    summary = m.get_summary(days=7)
    assert summary["days"] == 1
    assert summary["total_rewards"] == 10.0
    assert summary["total_adverse"] == 2.0
    assert summary["cumulative_pnl"] == 8.0
    assert "roi_pct" in summary
    assert "annualized_apy" in summary
    assert "avg_fill_rate" in summary
    assert "avg_scoring_rate" in summary


def test_metrics_max_days_pruning():
    """Should prune old days when exceeding max_days."""
    m = LiquidityMetrics(max_days=3)
    # Manually insert old days
    m._days["2026-01-01"] = DailySnapshot(date="2026-01-01")
    m._days["2026-01-02"] = DailySnapshot(date="2026-01-02")
    m._days["2026-01-03"] = DailySnapshot(date="2026-01-03")
    m._days["2026-01-04"] = DailySnapshot(date="2026-01-04")

    # Force date rollover check
    m._current_date = "2025-01-01"
    m._get_today()

    # Should have pruned to max_days + today
    assert len(m._days) <= 4
