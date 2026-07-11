"""Tests del bloque directional: A4, A5, A7, A8, A9, M5.

(M6 —guard live en settlements del detector— se cubre por inspección; su
ruta requiere un mercado resuelto en el tracker.)
"""

import sys
import time
import types
from datetime import datetime

import pytest
from unittest.mock import MagicMock, AsyncMock

from src.executor import Executor, OrderStatus, TradeRecord
from src.config import RiskConfig, CredentialsConfig, StrategyConfig
from src.detector import ClosingArbitrageDetector, Opportunity
from src.market_tracker import MarketTracker
from src.price_checker import _et_to_utc


# ── A9: DST correcto ─────────────────────────────────────────────────

def test_et_to_utc_winter_is_est():
    """15 enero 6:00 ET = EST (UTC-5) → 11:00 UTC. El bug daba 10:00 (EDT)."""
    utc = _et_to_utc(datetime(2026, 1, 15, 6, 0))
    assert utc.hour == 11


def test_et_to_utc_summer_is_edt():
    """15 julio 6:00 ET = EDT (UTC-4) → 10:00 UTC."""
    utc = _et_to_utc(datetime(2026, 7, 15, 6, 0))
    assert utc.hour == 10


# ── A8: balance disponible = on-chain - committed ────────────────────

def _executor():
    ex = Executor(RiskConfig(), CredentialsConfig())
    return ex


def test_available_balance_subtracts_committed():
    ex = _executor()
    ex._live_balance = 100.0
    ex._pending_orders = {
        "o1": TradeRecord("o1", "c1", "q", "t", "YES", 0.5, 20, 30.0,
                          status=OrderStatus.LIVE),
        "o2": TradeRecord("o2", "c2", "q", "t", "YES", 0.5, 20, 25.0,
                          status=OrderStatus.PENDING),
    }
    # 100 - 30 - 25 = 45 disponible (no 100)
    assert ex._available_balance() == pytest.approx(45.0)


def test_available_balance_ignores_terminal_orders():
    ex = _executor()
    ex._live_balance = 100.0
    ex._pending_orders = {
        "o1": TradeRecord("o1", "c1", "q", "t", "YES", 0.5, 20, 30.0,
                          status=OrderStatus.MATCHED),  # ya llenó, no committed
    }
    assert ex._available_balance() == pytest.approx(100.0)


# ── A7: max_concurrent no es un tope vitalicio ───────────────────────

def _opp(bet=5.0):
    return Opportunity(
        timestamp=0.0, condition_id="0xnew", question="Q", token_side="YES",
        token_id="tok", token_price=0.97, implied_probability=0.97,
        margin_gross=0.03, fee_estimated=0.0, margin_net=0.02, depth_at_price=100,
        resolved=False, winning_token_id="", suggested_bet=bet,
    )


def test_stale_matched_does_not_count():
    ex = _executor()
    ex.mode = ex.mode  # PAPER default → no balance check
    ex.risk.max_concurrent_positions = 2
    now = time.time()
    old = now - (5 * 3600)  # más viejo que el TTL de 4h
    ex._trades = [
        TradeRecord("a", "c1", "q", "t", "YES", 0.5, 10, 5.0,
                    status=OrderStatus.MATCHED, matched_at=old),
        TradeRecord("b", "c2", "q", "t", "YES", 0.5, 10, 5.0,
                    status=OrderStatus.MATCHED, matched_at=old),
    ]
    # Dos MATCHED viejos → resueltos → no bloquean
    assert ex._check_risk(_opp()) is True


def test_fresh_matched_counts():
    ex = _executor()
    ex.risk.max_concurrent_positions = 2
    now = time.time()
    ex._trades = [
        TradeRecord("a", "c1", "q", "t", "YES", 0.5, 10, 5.0,
                    status=OrderStatus.MATCHED, matched_at=now),
        TradeRecord("b", "c2", "q", "t", "YES", 0.5, 10, 5.0,
                    status=OrderStatus.MATCHED, matched_at=now),
    ]
    # Dos MATCHED recientes → activos → bloquean
    assert ex._check_risk(_opp()) is False


# ── A4: cleanup_market preserva bets pending ─────────────────────────

def _detector():
    return ClosingArbitrageDetector(StrategyConfig(), MarketTracker(), risk=RiskConfig())


def test_cleanup_preserves_pending_bet():
    det = _detector()
    pending = _opp(); pending.outcome = "pending"
    resolved = _opp(); resolved.outcome = "win"
    det._bet_placed = {"0xAAA:YES": pending, "0xAAA:NO": resolved}
    det.cleanup_market("0xAAA")
    # La resuelta se borra; la pending se conserva para el sweep
    assert "0xAAA:YES" in det._bet_placed
    assert "0xAAA:NO" not in det._bet_placed


# ── A5: verificar fill antes de cancelar por timeout ─────────────────

@pytest.fixture
def fake_clob_sdk(monkeypatch):
    ct = types.ModuleType("py_clob_client_v2.clob_types")
    ct.OrderPayload = lambda **kw: kw
    ct.BalanceAllowanceParams = lambda **kw: kw
    ct.AssetType = types.SimpleNamespace(COLLATERAL="COLLATERAL")
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", ct)
    return ct


@pytest.mark.asyncio
async def test_cancel_books_fill_if_matched(fake_clob_sdk):
    """Si la orden se llenó antes del cancel → MATCHED, no CANCELLED (A5)."""
    ex = _executor()
    ex._client = MagicMock()
    ex._client.get_order = MagicMock(return_value={"size_matched": "10"})
    ex._client.cancel_order = MagicMock()
    ex._refresh_balance = AsyncMock()
    ex._persist_status_update = AsyncMock()

    trade = TradeRecord("o1", "c1", "q", "t", "YES", 0.5, 10, 5.0,
                        status=OrderStatus.LIVE)
    await ex._cancel_order("o1", trade)

    assert trade.status == OrderStatus.MATCHED     # fill contabilizado
    assert trade.size_matched == pytest.approx(10.0)
    ex._client.cancel_order.assert_not_called()    # no se canceló una orden llena


@pytest.mark.asyncio
async def test_cancel_proceeds_if_not_filled(fake_clob_sdk):
    ex = _executor()
    ex._client = MagicMock()
    ex._client.get_order = MagicMock(return_value={"size_matched": "0"})
    ex._client.cancel_order = MagicMock()
    ex._refresh_balance = AsyncMock()
    ex._persist_status_update = AsyncMock()

    trade = TradeRecord("o1", "c1", "q", "t", "YES", 0.5, 10, 5.0,
                        status=OrderStatus.LIVE)
    await ex._cancel_order("o1", trade)

    assert trade.status == OrderStatus.CANCELLED
    ex._client.cancel_order.assert_called_once()


# ── M5: restore directional copia cost_usd ───────────────────────────

@pytest.mark.asyncio
async def test_restore_copies_cost_and_skips_ghosts():
    from src.strategies.directional import DirectionalStrategy, DirectionalConfig

    ctx = type("Ctx", (), {"account_name": "t", "_executor": _executor()})()
    strat = DirectionalStrategy(DirectionalConfig(mode="paper"), ctx, _detector())

    real = TradeRecord("o1", "c1", "q", "tok", "YES", 0.97, 10, 9.7,
                       status=OrderStatus.MATCHED, source_strategy="directional")
    ghost = TradeRecord("o2", "c2", "q", "tok", "YES", 0.0, 0, 0.0,
                        status=OrderStatus.MATCHED, source_strategy="directional")

    await strat.restore_open_positions([real, ghost])

    bp = strat._detector._bet_placed
    assert "c1:YES" in bp
    assert bp["c1:YES"].suggested_bet == pytest.approx(9.7)   # cost_usd copiado
    assert bp["c1:YES"].margin_net > 0                        # reconstruido
    assert "c2:YES" not in bp                                 # ghost (cost 0) saltado

