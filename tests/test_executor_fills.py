"""Tests de C5: detección de fills invisibles en Executor._live_trade.

El bug: post_order puede señalar un fill de tres formas (orderID, tradeIDs,
success) y el código solo miraba orderID → los FAK matches (tradeIDs sin
orderID) se marcaban 'rejected' con el dinero ya comprometido. Y una excepción
tras enviar marcaba FAILED sin verificar si la orden vive on-chain.
"""

import sys
import types

import pytest
from unittest.mock import MagicMock, AsyncMock

from src.executor import Executor, OrderStatus
from src.config import RiskConfig, CredentialsConfig
from src.detector import Opportunity


@pytest.fixture
def fake_clob_sdk(monkeypatch):
    fake = types.ModuleType("py_clob_client_v2")
    fake.OrderArgs = lambda **kw: kw
    ob = types.ModuleType("py_clob_client_v2.order_builder")
    const = types.ModuleType("py_clob_client_v2.order_builder.constants")
    const.BUY = "BUY"
    const.SELL = "SELL"
    ct = types.ModuleType("py_clob_client_v2.clob_types")
    ct.OrderPayload = lambda **kw: kw
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", fake)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.order_builder", ob)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.order_builder.constants", const)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", ct)
    return fake


def _opp():
    return Opportunity(
        timestamp=0.0, condition_id="0xcond", question="Q?", token_side="YES",
        token_id="tok", token_price=0.5, implied_probability=0.5,
        margin_gross=0.5, fee_estimated=0.0, margin_net=0.4, depth_at_price=100,
        resolved=False, winning_token_id="", suggested_bet=10.0,
    )


def _executor():
    ex = Executor(RiskConfig(), CredentialsConfig())
    ex._client = MagicMock()
    ex._client.create_order = MagicMock(return_value="signed")
    ex._live_balance = 100.0
    ex._persist_new_trade = AsyncMock()
    ex._refresh_balance_delayed = AsyncMock()
    return ex


# ── _parse_post_order_response (static) ──────────────────────────────

def test_parse_orderid_is_fill():
    filled, oid, tids, *_ = Executor._parse_post_order_response({"orderID": "x"})
    assert filled is True and oid == "x"


def test_parse_tradeids_is_fill():
    filled, oid, tids, *_ = Executor._parse_post_order_response({"tradeIDs": ["t1"]})
    assert filled is True and tids == ["t1"]


def test_parse_success_is_fill():
    filled, *_ = Executor._parse_post_order_response({"success": True})
    assert filled is True


def test_parse_rejected_not_fill():
    filled, *_ = Executor._parse_post_order_response({"success": False, "errorMsg": "no"})
    assert filled is False


def test_parse_nondict_not_fill():
    assert Executor._parse_post_order_response(None)[0] is False


# ── _live_trade flujo ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_live_trade_fak_match_is_matched(fake_clob_sdk):
    """FAK match (success + tradeIDs, sin orderID) → MATCHED, no FAILED (el bug)."""
    ex = _executor()
    ex._client.post_order = MagicMock(
        return_value={"success": True, "tradeIDs": ["t1"], "orderID": ""}
    )
    trade = await ex._live_trade(_opp())
    assert trade.status == OrderStatus.MATCHED
    ex._persist_new_trade.assert_awaited()


@pytest.mark.asyncio
async def test_live_trade_orderid_is_live_and_tracked(fake_clob_sdk):
    ex = _executor()
    ex._client.post_order = MagicMock(return_value={"orderID": "o1"})
    trade = await ex._live_trade(_opp())
    assert trade.status == OrderStatus.LIVE
    assert trade.order_id == "o1"
    assert "o1" in ex._pending_orders


@pytest.mark.asyncio
async def test_live_trade_rejected_is_failed(fake_clob_sdk):
    ex = _executor()
    ex._client.post_order = MagicMock(return_value={"success": False, "errorMsg": "bad"})
    trade = await ex._live_trade(_opp())
    assert trade.status == OrderStatus.FAILED
    ex._persist_new_trade.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_trade_exception_recovers_via_api(fake_clob_sdk):
    """Excepción tras enviar + fill en la Data API → MATCHED (orphan recuperado)."""
    ex = _executor()
    ex._client.post_order = MagicMock(side_effect=Exception("timeout"))
    ex._verify_fill_via_api = AsyncMock(
        return_value={"transactionHash": "0xabc", "price": 0.5, "size": 20}
    )
    trade = await ex._live_trade(_opp())
    assert trade.status == OrderStatus.MATCHED
    ex._verify_fill_via_api.assert_awaited()
    ex._persist_new_trade.assert_awaited()


@pytest.mark.asyncio
async def test_live_trade_exception_no_fill_is_failed(fake_clob_sdk):
    ex = _executor()
    ex._client.post_order = MagicMock(side_effect=Exception("timeout"))
    ex._verify_fill_via_api = AsyncMock(return_value=None)
    trade = await ex._live_trade(_opp())
    assert trade.status == OrderStatus.FAILED
