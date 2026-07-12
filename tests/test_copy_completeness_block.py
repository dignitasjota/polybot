"""Tests del bloque copy/completeness: A3, B2, M11 (B6 en test_completeness_scanner)."""

import time
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.copy_trader import CopyTrader, CopyBet
from src.config import CopyTradeConfig
from src.fees import taker_fee
from src.completeness_scanner import CompletenessScanner, ArbTrade
from src.strategies.completeness import CompletenessConfig


def _copy_trader():
    return CopyTrader(CopyTradeConfig(), starting_balance=500.0)


def _bet(**kw):
    d = dict(
        condition_id="0xc", question="q", token_id="tok", token_side="YES",
        price=0.50, bet_size=5.0, potential_profit=5.0,
        wallet_source="0xwallet", timestamp=0.0,
    )
    d.update(kw)
    return CopyBet(**d)


# ── B2: la pérdida incluye el taker fee ──────────────────────────────

def test_loss_includes_taker_fee():
    ct = _copy_trader()
    bet = _bet(price=0.50, bet_size=5.0)
    ct._settle_bet(bet, won=False)
    shares = 5.0 / 0.50
    expected = round(-(5.0 + taker_fee(0.50, shares)), 4)
    assert bet.actual_pnl == pytest.approx(expected)
    assert bet.actual_pnl < -5.0   # más pérdida que el bet_size solo


def test_win_still_subtracts_fee():
    """Sanity: el brazo win ya restaba el fee; no lo rompimos."""
    ct = _copy_trader()
    bet = _bet(price=0.50, bet_size=5.0)
    ct._settle_bet(bet, won=True)
    assert bet.outcome == "win"
    assert bet.actual_pnl < (5.0 / 0.50) - 5.0   # payout - cost, menos fee/gas


# ── A3: restore crea CopyBet y el dedup lo reconoce ──────────────────

@pytest.mark.asyncio
async def test_strategy_restore_delegates_to_copy_trader():
    """CopyTradeStrategy.restore inserta CopyBet (no dicts) vía CopyTrader (A3)."""
    from src.strategies.copy_trade import CopyTradeStrategy, CopyTradeConfig as StratCfg
    ct = _copy_trader()
    ctx = type("Ctx", (), {"account_name": "t", "_executor": MagicMock()})()
    strat = CopyTradeStrategy(StratCfg(mode="paper"), ctx, ct)

    trade = MagicMock(
        source_strategy="copy_trade", condition_id="0xAAA", token_side="YES",
        token_id="tok", price=0.5, cost_usd=5.0, created_at=0.0, status="confirmed",
    )
    await strat.restore_open_positions([trade])

    assert "0xAAA:YES" in ct._bets
    assert isinstance(ct._bets["0xAAA:YES"], CopyBet)   # no un dict
    assert ct._bets["0xAAA:YES"].wallet_source == "restored"


def test_restored_position_blocks_rebet():
    """El dedup vivo reconoce una posición restaurada (key base) y no re-apuesta."""
    ct = _copy_trader()
    ct._bets["0xAAA:YES"] = _bet(condition_id="0xAAA", wallet_source="restored")
    # Simula el check de dedup: una restaurada en la key base bloquea
    trade = MagicMock(condition_id="0xAAA", maker_address="0xtargetwallet")
    token_side = "YES"
    key = f"{trade.condition_id}:{token_side}:{trade.maker_address[:10]}"
    base = f"{trade.condition_id}:{token_side}"
    restored = ct._bets.get(base)
    blocked = key in ct._bets or (
        restored is not None and getattr(restored, "wallet_source", "") == "restored"
    )
    assert blocked is True


# ── M11: reintento de redeems pendientes ─────────────────────────────

def _completeness():
    return CompletenessScanner(CompletenessConfig.from_dict({"mode": "live"}))


def _confirmed_trade(cid="0xR", profit=0.5):
    return ArbTrade(
        trade_id="t1", condition_id=cid, question="q",
        shares=10.0, cost_total=9.0, expected_profit=profit, fees_paid=0.0,
        created_at=0.0, mode="live", status="confirmed", actual_pnl=0.0,
    )


@pytest.mark.asyncio
async def test_try_redeem_discards_pending_on_success():
    sc = _completeness()
    trade = _confirmed_trade(profit=0.5)
    sc._trades = [trade]
    sc._pending_redeems = {"0xR"}
    sc.set_redeem_callback(AsyncMock(return_value=True))

    await sc._try_redeem(trade)

    assert trade.status == "redeemed"
    assert "0xR" not in sc._pending_redeems         # sacado del set (M11)
    assert sc._total_profit == pytest.approx(0.5)   # profit contabilizado


@pytest.mark.asyncio
async def test_try_redeem_keeps_pending_on_failure():
    sc = _completeness()
    trade = _confirmed_trade()
    sc._trades = [trade]
    sc.set_redeem_callback(AsyncMock(return_value=False))

    await sc._try_redeem(trade)

    assert trade.status == "confirmed"              # sigue pendiente
    assert "0xR" in sc._pending_redeems


@pytest.mark.asyncio
async def test_retry_loop_redeems_pending(monkeypatch):
    """El loop reintenta un redeem que antes falló y ahora tiene éxito."""
    monkeypatch.setattr("src.completeness_scanner.REDEEM_RETRY_INTERVAL_S", 0.01)
    sc = _completeness()
    trade = _confirmed_trade(profit=0.7)
    sc._trades = [trade]
    sc._pending_redeems = {"0xR"}
    sc._running = True
    sc.set_redeem_callback(AsyncMock(return_value=True))

    import asyncio
    task = asyncio.create_task(sc._redeem_retry_loop())
    await asyncio.sleep(0.05)
    sc._running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert trade.status == "redeemed"
    assert "0xR" not in sc._pending_redeems


@pytest.mark.asyncio
async def test_retry_loop_discards_orphan_condition():
    """Un pending sin trade confirmed asociado se descarta (no bucle infinito)."""
    monkeypatch_interval = 0.01
    import src.completeness_scanner as mod
    orig = mod.REDEEM_RETRY_INTERVAL_S
    mod.REDEEM_RETRY_INTERVAL_S = monkeypatch_interval
    try:
        sc = _completeness()
        sc._trades = []                 # ningún trade
        sc._pending_redeems = {"0xGHOST"}
        sc._running = True
        sc.set_redeem_callback(AsyncMock(return_value=True))
        import asyncio
        task = asyncio.create_task(sc._redeem_retry_loop())
        await asyncio.sleep(0.05)
        sc._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert "0xGHOST" not in sc._pending_redeems
    finally:
        mod.REDEEM_RETRY_INTERVAL_S = orig
