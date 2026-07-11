"""Tests para AccountContext — C3: get_open_positions con status enum/string.

El bug: TradeRecord.status es un OrderStatus (Enum), y
`OrderStatus.PENDING in ("pending","confirmed")` es siempre False → la
protección anti-apuesta-cruzada y el cap diario de paper estaban muertos.
"""

import types
import pytest

from src.strategies.base import AccountContext, _status_str, _OPEN_STATUSES
from src.executor import TradeRecord, OrderStatus


# ── Helpers ──────────────────────────────────────────────────────────

class _FakeLedger:
    def __init__(self, trades):
        self.trades = trades


class _FakeExec:
    def __init__(self, paper_trades=None, live_trades=None):
        self._ledger_paper = _FakeLedger(paper_trades or [])
        self._ledger_live = _FakeLedger(live_trades or [])


def _trade(status, side="YES", cid="0xc", strat="directional", created_at=0.0):
    """Real TradeRecord with an OrderStatus enum status."""
    return TradeRecord(
        order_id="o", condition_id=cid, question="q", token_id="t",
        token_side=side, price=0.5, size=10.0, cost_usd=5.0,
        status=status, created_at=created_at, source_strategy=strat,
    )


def _ctx(paper=None, live=None):
    return AccountContext("acct", _FakeExec(paper, live))


# ── _status_str: enum y string ───────────────────────────────────────

def test_status_str_from_enum():
    assert _status_str(_trade(OrderStatus.PENDING)) == "pending"
    assert _status_str(_trade(OrderStatus.CONFIRMED)) == "confirmed"
    assert _status_str(_trade(OrderStatus.FAILED)) == "failed"


def test_status_str_from_plain_string():
    obj = types.SimpleNamespace(status="pending")
    assert _status_str(obj) == "pending"


def test_status_str_none():
    assert _status_str(types.SimpleNamespace(status=None)) == ""
    assert _status_str(types.SimpleNamespace()) == ""


# ── get_open_positions: el fix central ───────────────────────────────

def test_open_positions_counts_enum_statuses():
    """El bug: con enum, esto devolvía [] siempre. Ahora cuenta los activos."""
    ctx = _ctx(paper=[
        _trade(OrderStatus.PENDING),
        _trade(OrderStatus.CONFIRMED),
        _trade(OrderStatus.LIVE),
        _trade(OrderStatus.MATCHED),
    ])
    assert len(ctx.get_open_positions("paper")) == 4


def test_open_positions_excludes_terminal_statuses():
    ctx = _ctx(paper=[
        _trade(OrderStatus.CONFIRMED),
        _trade(OrderStatus.FAILED),
        _trade(OrderStatus.CANCELLED),
    ])
    assert len(ctx.get_open_positions("paper")) == 1   # solo el CONFIRMED


def test_open_positions_filter_by_strategy():
    ctx = _ctx(paper=[
        _trade(OrderStatus.CONFIRMED, strat="directional"),
        _trade(OrderStatus.CONFIRMED, strat="copy_trade"),
    ])
    assert len(ctx.get_open_positions("paper", strategy="copy_trade")) == 1


def test_open_positions_respects_mode():
    ctx = _ctx(
        paper=[_trade(OrderStatus.CONFIRMED)],
        live=[_trade(OrderStatus.MATCHED), _trade(OrderStatus.LIVE)],
    )
    assert len(ctx.get_open_positions("paper")) == 1
    assert len(ctx.get_open_positions("live")) == 2


# ── has_opposite_position: la protección anti-cruce ──────────────────

def test_has_opposite_position_detects_conflict():
    """Ya tenemos YES abierto → apostar NO debe detectar el conflicto."""
    ctx = _ctx(paper=[_trade(OrderStatus.CONFIRMED, side="YES", strat="directional")])
    exists, from_strat = ctx.has_opposite_position("0xc", "NO", "paper")
    assert exists is True
    assert from_strat == "directional"


def test_has_opposite_position_same_side_no_conflict():
    ctx = _ctx(paper=[_trade(OrderStatus.CONFIRMED, side="YES")])
    exists, _ = ctx.has_opposite_position("0xc", "YES", "paper")
    assert exists is False


def test_has_opposite_position_different_market():
    ctx = _ctx(paper=[_trade(OrderStatus.CONFIRMED, side="YES", cid="0xAAA")])
    exists, _ = ctx.has_opposite_position("0xBBB", "NO", "paper")
    assert exists is False


def test_has_position():
    ctx = _ctx(paper=[_trade(OrderStatus.CONFIRMED, side="YES", cid="0xc")])
    assert ctx.has_position("0xc", "YES", "paper") is True
    assert ctx.has_position("0xc", "NO", "paper") is False


# ── count_paper_trades_today: el cap diario ──────────────────────────

def test_count_paper_trades_today():
    import time
    now = time.time()
    ctx = _ctx(paper=[
        _trade(OrderStatus.CONFIRMED, strat="directional", created_at=now),
        _trade(OrderStatus.CONFIRMED, strat="directional", created_at=now),
        _trade(OrderStatus.CONFIRMED, strat="directional", created_at=0.0),  # 1970, no cuenta hoy
        _trade(OrderStatus.FAILED, strat="directional", created_at=now),      # terminal, no cuenta
    ])
    assert ctx.count_paper_trades_today("directional") == 2


def test_count_active_positions():
    ctx = _ctx(paper=[
        _trade(OrderStatus.CONFIRMED),
        _trade(OrderStatus.PENDING),
        _trade(OrderStatus.FAILED),
    ])
    assert ctx.count_active_positions("paper") == 2
