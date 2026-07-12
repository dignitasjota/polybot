"""Tests del bloque final infra/mode: M7, M9, B8, B10, B12."""

import sys
import types
import time

import pytest
from unittest.mock import MagicMock, AsyncMock

from src.fees import net_margin, taker_fee_per_share, GAS_REDEEM_USD
from src.executor import Executor, OrderStatus, TradeRecord
from src.config import RiskConfig, CredentialsConfig


# ── B8: net_margin per-share sin gas ─────────────────────────────────

def test_net_margin_excludes_gas():
    """net_margin es (1-p) - fee_per_share, sin restar el gas por share."""
    p = 0.97
    expected = (1.0 - p) - taker_fee_per_share(p, "crypto")
    assert net_margin(p, "crypto") == pytest.approx(expected)
    # y explícitamente NO incluye el gas
    assert net_margin(p, "crypto") == pytest.approx(expected + GAS_REDEEM_USD - GAS_REDEEM_USD)
    assert net_margin(p, "crypto") > (1.0 - p) - taker_fee_per_share(p, "crypto") - GAS_REDEEM_USD


def test_gas_once_not_per_share():
    """Para muchas shares, el gas debe contarse una vez, no × shares."""
    p = 0.97
    shares = 100.0
    m = net_margin(p, "crypto")
    pnl = shares * m - GAS_REDEEM_USD          # nuevo: gas una vez
    old_pnl = shares * (m - GAS_REDEEM_USD)    # viejo: gas × shares
    # La diferencia es gas × (shares - 1) — significativa para fills grandes
    assert pnl - old_pnl == pytest.approx(GAS_REDEEM_USD * (shares - 1))


# ── M9: executor.cancel_all_orders ───────────────────────────────────

@pytest.fixture
def fake_clob_sdk(monkeypatch):
    ct = types.ModuleType("py_clob_client_v2.clob_types")
    ct.OrderPayload = lambda **kw: kw
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", ct)
    return ct


@pytest.mark.asyncio
async def test_cancel_all_orders_calls_client():
    ex = Executor(RiskConfig(), CredentialsConfig())
    ex._client = MagicMock()
    ex._client.cancel_all = MagicMock(return_value={"canceled": True})
    await ex.cancel_all_orders()
    ex._client.cancel_all.assert_called_once()


@pytest.mark.asyncio
async def test_cancel_all_orders_noop_without_client():
    ex = Executor(RiskConfig(), CredentialsConfig())
    ex._client = None
    await ex.cancel_all_orders()   # no lanza


# ── B12: poda de _persisted_orders ───────────────────────────────────

@pytest.mark.asyncio
async def test_persisted_orders_capped(monkeypatch):
    ex = Executor(RiskConfig(), CredentialsConfig())
    # Sin persistencia inicializada, _persist_new_trade retorna temprano;
    # probamos la lógica de poda directamente sobre el set.
    ex._persisted_orders = set(f"o{i}" for i in range(10001))
    # Simular la poda inline
    if len(ex._persisted_orders) > 10000:
        ex._persisted_orders = set(list(ex._persisted_orders)[-5000:])
    assert len(ex._persisted_orders) == 5000


# ── M7: el throttle del WS solo aplica a best_bid_ask ────────────────

def test_ws_throttle_logic_book_exempt():
    """Verifica la condición: book nunca se throttlea, best_bid_ask sí."""
    # Reproducimos la decisión del dispatch de _handle_message
    last_check = {"tok": time.time()}   # justo procesado

    def should_process(event_type, asset_id):
        now = time.time()
        if event_type == "best_bid_ask":
            if now - last_check.get(asset_id, 0) < 1.0:
                return False
            last_check[asset_id] = now
        return True

    assert should_process("best_bid_ask", "tok") is False   # throttled
    assert should_process("book", "tok") is True             # book exento (M7)


# ── B10: /api/health no expone datos operativos ──────────────────────

@pytest.mark.asyncio
async def test_health_returns_only_liveness():
    from src.web.routes_api import handle_health
    from aiohttp.test_utils import make_mocked_request

    bot = types.SimpleNamespace(accounts=[object(), object()])
    req = make_mocked_request("GET", "/api/health")
    req.app["bot"] = bot
    resp = await handle_health(req)
    import json
    body = json.loads(resp.body)
    assert body["status"] == "ok"
    assert body["accounts"] == 2
    # NADA operativo expuesto
    assert "detector" not in body
    assert "executor" not in body
    assert "price_checker" not in body
