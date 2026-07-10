"""Tests del bloque 2 de la auditoría: stop-loss diario y staleness del WS.

Cubre:
- M8: el REST fallback del WebSocket refresca bid Y ask (no solo ask) antes
  de marcar el quote como fresco → el midpoint deja de contaminarse con un
  bid stale.
- C1 (executor): _maybe_reset_daily resetea en el límite del día UTC, no en
  una ventana rodante de 24h desde el primer check.

(El stop-loss de liquidity se prueba en test_liquidity_provider.py.)
"""

import time
import pytest

from src.market_tracker import MarketTracker
from src.websocket_client import WebSocketClient
from src.config import WebSocketConfig, RiskConfig
from src.executor import Executor, _utc_day


def _make_ws():
    tracker = MarketTracker()
    tracker.add_market(
        condition_id="0xcond",
        question="Test?",
        yes_token_id="tok_yes",
        no_token_id="tok_no",
    )
    ws = WebSocketClient(WebSocketConfig(), tracker)
    return ws, tracker


# ── M8: REST fallback refresca ambos lados ───────────────────────────

@pytest.mark.asyncio
async def test_rest_fallback_refreshes_bid_and_ask(monkeypatch):
    """buy→best_ask, sell→best_bid; ambos deben actualizarse en el tracker."""
    ws, tracker = _make_ws()

    async def fake_fetch(token_id, side):
        return 0.60 if side == "buy" else 0.40  # ask=0.60, bid=0.40
    monkeypatch.setattr(ws, "_fetch_rest_price", fake_fetch)

    await ws._poll_rest_prices(["tok_yes"])

    state = tracker.get_by_token("tok_yes")
    assert state.best_ask_yes == pytest.approx(0.60)
    assert state.best_bid_yes == pytest.approx(0.40)   # antes quedaba en 0/stale
    assert state.last_update > 0
    # El midpoint ya no se contamina: (0.40 + 0.60)/2 = 0.50
    assert tracker.get_midpoint("tok_yes") == pytest.approx(0.50)


@pytest.mark.asyncio
async def test_rest_fallback_no_stale_bid_poisoning(monkeypatch):
    """Un bid viejo se REEMPLAZA por el fresco, no se mezcla con el ask nuevo."""
    ws, tracker = _make_ws()
    state = tracker.get_by_token("tok_yes")
    state.best_bid_yes = 0.20   # bid stale de un book anterior
    state.best_ask_yes = 0.30

    async def fake_fetch(token_id, side):
        return 0.62 if side == "buy" else 0.58  # el mercado se movió arriba
    monkeypatch.setattr(ws, "_fetch_rest_price", fake_fetch)

    await ws._poll_rest_prices(["tok_yes"])

    # El bid stale (0.20) fue reemplazado por el fresco (0.58), no persiste
    assert state.best_bid_yes == pytest.approx(0.58)
    assert tracker.get_midpoint("tok_yes") == pytest.approx(0.60)


@pytest.mark.asyncio
async def test_rest_fallback_no_update_when_both_fail(monkeypatch):
    """Si ambas consultas fallan, no se marca frescura falsa."""
    ws, tracker = _make_ws()
    state = tracker.get_by_token("tok_yes")
    state.last_update = 0.0

    async def fake_fetch(token_id, side):
        return None
    monkeypatch.setattr(ws, "_fetch_rest_price", fake_fetch)

    await ws._poll_rest_prices(["tok_yes"])
    assert state.last_update == 0.0  # no se certifica fresco sin datos


# ── C1 (executor): reset del daily P&L en el límite UTC ──────────────

def _make_executor():
    return Executor(RiskConfig(), credentials=None)


def test_daily_reset_on_utc_day_change():
    ex = _make_executor()
    ex._daily_pnl = -50.0
    ex._daily_pnl_by_mode = {"paper": -50.0, "live": 0.0}
    ex._daily_reset_day = "2000-01-01"  # forzar un día pasado

    ex._maybe_reset_daily()

    assert ex._daily_pnl == 0.0
    assert ex._daily_pnl_by_mode == {"paper": 0.0, "live": 0.0}
    assert ex._daily_reset_day == _utc_day()


def test_daily_no_reset_same_utc_day():
    ex = _make_executor()
    ex._daily_pnl = -50.0
    ex._daily_reset_day = _utc_day()  # ya es hoy

    ex._maybe_reset_daily()
    assert ex._daily_pnl == -50.0  # no se resetea dentro del mismo día UTC


def test_utc_day_format():
    d = _utc_day()
    assert len(d) == 10 and d.count("-") == 2  # YYYY-MM-DD
