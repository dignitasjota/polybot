"""Tests para DailyLossGuard (C1)."""

import pytest

from src import risk_guard
from src.risk_guard import DailyLossGuard, utc_day


def test_disabled_when_zero_limit():
    g = DailyLossGuard(max_daily_loss=0.0)
    g.record(-999.0)
    assert g.tripped() is False


def test_trips_at_limit():
    g = DailyLossGuard(max_daily_loss=50.0)
    g.record(-30.0)
    assert g.tripped() is False
    g.record(-25.0)   # acumulado -55 <= -50
    assert g.tripped() is True


def test_profit_offsets_loss():
    g = DailyLossGuard(max_daily_loss=50.0)
    g.record(-40.0)
    g.record(20.0)    # -20 neto
    assert g.tripped() is False


def test_rolls_over_at_utc_day(monkeypatch):
    g = DailyLossGuard(max_daily_loss=50.0)
    g.record(-60.0)
    assert g.tripped() is True

    # Simular cambio de día UTC
    monkeypatch.setattr(risk_guard, "utc_day", lambda: "2099-01-01")
    assert g.tripped() is False       # el rollover limpia el contador
    assert g.daily_pnl == 0.0


def test_reset_clears_counter():
    g = DailyLossGuard(max_daily_loss=50.0)
    g.record(-60.0)
    g.reset()
    assert g.daily_pnl == 0.0
    assert g.tripped() is False
    assert g.max_daily_loss == 50.0   # conserva el límite


def test_utc_day_format():
    d = utc_day()
    assert len(d) == 10 and d.count("-") == 2


# ── Integración C1: el guard corta el punto de ejecución de cada estrategia ──

import pytest


@pytest.mark.asyncio
async def test_weather_execute_trade_halts_when_tripped():
    """_execute_trade retorna ANTES de tocar opp.market cuando el guard trippea."""
    from src.weather_scanner import WeatherScanner
    from src.strategies.weather import WeatherConfig

    scanner = WeatherScanner(WeatherConfig.from_dict({"mode": "paper"}), max_daily_loss=20.0)
    scanner._loss_guard.record(-25.0)   # supera el límite
    assert scanner._loss_guard.tripped()

    # Si el guard NO cortara, _execute_trade accedería a opp.market y fallaría
    # con este sentinel. Que no lance prueba que corta primero.
    await scanner._execute_trade(object())
    assert scanner._trades == []


@pytest.mark.asyncio
async def test_weather_execute_trade_proceeds_when_not_tripped():
    """Sin superar el límite, el guard no corta (el sentinel entonces sí falla)."""
    from src.weather_scanner import WeatherScanner
    from src.strategies.weather import WeatherConfig

    scanner = WeatherScanner(WeatherConfig.from_dict({"mode": "paper"}), max_daily_loss=20.0)
    scanner._loss_guard.record(-5.0)    # por debajo del límite
    assert not scanner._loss_guard.tripped()
    with pytest.raises(AttributeError):   # llega a opp.market
        await scanner._execute_trade(object())


@pytest.mark.asyncio
async def test_completeness_execute_arb_halts_when_tripped():
    from src.completeness_scanner import CompletenessScanner
    from src.strategies.completeness import CompletenessConfig

    scanner = CompletenessScanner(
        CompletenessConfig.from_dict({"mode": "paper"}), max_daily_loss=20.0,
    )
    scanner._loss_guard.record(-25.0)
    assert scanner._loss_guard.tripped()
    await scanner._execute_arb(object())   # corta antes de opp.condition_id
    assert scanner._trades == []


@pytest.mark.asyncio
async def test_completeness_settlement_feeds_guard():
    """Un profit/pérdida realizado en paper alimenta el guard diario."""
    from src.completeness_scanner import CompletenessScanner, ArbTrade, ArbOpportunity
    from src.strategies.completeness import CompletenessConfig

    scanner = CompletenessScanner(
        CompletenessConfig.from_dict({"mode": "paper"}), max_daily_loss=20.0,
    )
    trade = ArbTrade(
        trade_id="t1", condition_id="0xc", question="q",
        shares=10.0, cost_total=9.0, expected_profit=-25.0, fees_paid=0.0,
        created_at=0.0, mode="paper",
    )
    opp = ArbOpportunity(
        condition_id="0xc", question="q", token_ids=["a", "b"],
        prices=[0.5, 0.5], sizes=[10.0, 10.0], gap=-1.0, total_fees=0.0,
        gas_cost=0.0, net_profit_per_share=-2.5, max_shares=10.0,
    )
    await scanner._paper_execute(trade, opp)
    assert scanner._loss_guard.daily_pnl == pytest.approx(-25.0)
    assert scanner._loss_guard.tripped()   # -25 <= -20


def test_copy_trader_wires_and_resets_guard():
    from src.copy_trader import CopyTrader
    from src.config import CopyTradeConfig

    ct = CopyTrader(CopyTradeConfig(), starting_balance=500.0, max_daily_loss=30.0)
    assert ct._loss_guard.max_daily_loss == 30.0
    ct._loss_guard.record(-40.0)
    assert ct._loss_guard.tripped()
    ct.reset_stats()
    assert not ct._loss_guard.tripped()    # reset limpia el contador
    assert ct._loss_guard.max_daily_loss == 30.0


def test_detector_wires_guard_from_risk():
    from src.detector import ClosingArbitrageDetector
    from src.market_tracker import MarketTracker
    from src.config import RiskConfig, StrategyConfig

    det = ClosingArbitrageDetector(
        StrategyConfig(), MarketTracker(), risk=RiskConfig(max_daily_loss=15.0),
    )
    assert det._loss_guard.max_daily_loss == 15.0
    det._loss_guard.record(-20.0)
    assert det._loss_guard.tripped()
    det.reset_stats()
    assert not det._loss_guard.tripped()
