"""Tests for LiquidityProvider (Phase 2 market making engine).

Tests cover:
1. Price calculation (bid/ask from midpoint + max_spread)
2. Paper mode lifecycle (start/stop, quote placement)
3. Position management (open/close/refresh)
4. Stats structure
5. Integration with LiquidityStrategy

Run: python -m pytest tests/test_liquidity_provider.py -v
"""

import asyncio
import time
import pytest

from src.liquidity_provider import LiquidityProvider, QuoteOrder, MarketPosition
from src.reward_scanner import RewardScanner, RewardMarket
from src.strategies.liquidity import LiquidityConfig, LiquidityStrategy


# ── Helpers ───────────────────────────────────────────────────────────

def _make_config(**overrides) -> LiquidityConfig:
    defaults = dict(
        mode="paper",
        scan_interval=9999,
        min_daily_rate=1.0,
        capital_per_market=100.0,
        max_markets=3,
        spread_pct_of_max=0.20,
    )
    defaults.update(overrides)
    return LiquidityConfig(**defaults)


def _make_market(
    condition_id="0xabc",
    question="Test market?",
    daily_rate=50.0,
    max_spread=4.0,
    midpoint=0.50,
    competitiveness=10.0,
) -> RewardMarket:
    return RewardMarket(
        condition_id=condition_id,
        question=question,
        market_slug="test-market",
        daily_rate=daily_rate,
        max_spread=max_spread,
        min_size=20,
        competitiveness=competitiveness,
        volume_24h=5000,
        spread=0.03,
        midpoint=midpoint,
        reward_per_dollar=daily_rate / max(competitiveness, 1),
        score=5.0,
        tokens=[
            {"token_id": "tok_yes_1", "outcome": "Yes", "price": midpoint - 0.01},
            {"token_id": "tok_no_1", "outcome": "No", "price": 1 - midpoint + 0.01},
        ],
    )


# ── Price Calculation ─────────────────────────────────────────────────

def test_calculate_prices_basic():
    """Bid and ask should be symmetric around midpoint."""
    cfg = _make_config(spread_pct_of_max=0.20)
    provider = LiquidityProvider(config=cfg)

    # max_spread=0.04, 20% of that = 0.008, rounded to 0.01 per side
    bid, ask = provider._calculate_prices(midpoint=0.50, max_spread=0.04)
    assert bid < 0.50
    assert ask > 0.50
    assert bid == 0.49  # 0.50 - 0.004 → rounded to 0.49
    assert ask == 0.51  # 0.50 + 0.004 → rounded to 0.51


def test_calculate_prices_clamp():
    """Prices should be clamped to [0.01, 0.99]."""
    cfg = _make_config(spread_pct_of_max=0.50)
    provider = LiquidityProvider(config=cfg)

    # Extreme midpoint near 0
    bid, ask = provider._calculate_prices(midpoint=0.02, max_spread=0.10)
    assert bid >= 0.01
    assert ask <= 0.99

    # Extreme midpoint near 1
    bid, ask = provider._calculate_prices(midpoint=0.98, max_spread=0.10)
    assert bid >= 0.01
    assert ask <= 0.99


def test_calculate_prices_bid_less_than_ask():
    """Bid should always be strictly less than ask."""
    cfg = _make_config(spread_pct_of_max=0.001)  # Very tight spread
    provider = LiquidityProvider(config=cfg)

    bid, ask = provider._calculate_prices(midpoint=0.50, max_spread=0.01)
    assert bid < ask


def test_calculate_prices_rounding():
    """Prices should be rounded to 2 decimal places."""
    cfg = _make_config(spread_pct_of_max=0.33)
    provider = LiquidityProvider(config=cfg)

    bid, ask = provider._calculate_prices(midpoint=0.55, max_spread=0.05)
    assert bid == round(bid, 2)
    assert ask == round(ask, 2)


# ── Paper Mode ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_paper_place_order():
    """Paper mode should create orders with paper- prefix IDs."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    order = await provider._place_order(
        token_id="tok_yes_1",
        price=0.48,
        size=10.0,
        is_yes=True,
        condition_id="0xabc",
    )

    assert order is not None
    assert order.order_id.startswith("paper-")
    assert order.price == 0.48
    assert order.size == 10.0
    assert order.is_yes is True
    assert order.status == "active"
    assert provider._total_orders_placed == 1


@pytest.mark.asyncio
async def test_paper_cancel_order():
    """Paper cancel should mark order as cancelled."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    order = await provider._place_order("tok_yes_1", 0.48, 10.0, True, "0xabc")
    assert order.status == "active"

    await provider._cancel_order(order)
    assert order.status == "cancelled"
    assert provider._total_orders_cancelled == 1


@pytest.mark.asyncio
async def test_paper_lifecycle():
    """Provider should start and stop cleanly in paper mode."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    assert not provider._running

    await provider.start()
    assert provider._running
    assert provider._quote_task is not None

    await provider.stop()
    assert not provider._running
    assert provider._quote_task is None


# ── Position Management ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_open_position():
    """Opening a position should place bid + ask orders."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    market = _make_market()
    await provider._open_position(market)

    assert market.condition_id in provider._positions
    pos = provider._positions[market.condition_id]
    assert pos.bid_order is not None
    assert pos.ask_order is not None
    assert pos.bid_order.is_yes is True
    assert pos.ask_order.is_yes is False
    assert provider._total_orders_placed == 2


@pytest.mark.asyncio
async def test_close_position():
    """Closing a position should cancel orders and remove it."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    market = _make_market()
    await provider._open_position(market)
    assert market.condition_id in provider._positions

    await provider._close_position(market.condition_id)
    assert market.condition_id not in provider._positions
    assert provider._total_orders_cancelled == 2


@pytest.mark.asyncio
async def test_refresh_quotes_no_change():
    """Refresh should not cancel/replace if midpoint hasn't moved."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    market = _make_market(midpoint=0.50)
    await provider._open_position(market)
    initial_placed = provider._total_orders_placed
    initial_cancelled = provider._total_orders_cancelled

    # Refresh with same midpoint
    await provider._refresh_quotes(market)
    assert provider._total_orders_placed == initial_placed  # No new orders
    assert provider._total_orders_cancelled == initial_cancelled


@pytest.mark.asyncio
async def test_refresh_quotes_reprice():
    """Refresh should cancel+replace if midpoint moved significantly."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    market = _make_market(midpoint=0.50)
    await provider._open_position(market)
    initial_placed = provider._total_orders_placed  # 2

    # Move midpoint significantly (> reprice_threshold of 0.005)
    market2 = _make_market(midpoint=0.52)
    await provider._refresh_quotes(market2)

    # Should have cancelled 2 old + placed 2 new
    assert provider._total_orders_cancelled >= 2
    assert provider._total_orders_placed > initial_placed


# ── Stats ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stats_structure():
    """get_stats() should return expected fields."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    stats = provider.get_stats()
    assert "running" in stats
    assert "mode" in stats
    assert "active_markets" in stats
    assert "active_bids" in stats
    assert "active_asks" in stats
    assert "total_orders_placed" in stats
    assert "total_fills" in stats
    assert "errors" in stats
    assert "positions" in stats
    assert isinstance(stats["positions"], list)


@pytest.mark.asyncio
async def test_stats_with_positions():
    """Stats should reflect active positions."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    market = _make_market()
    await provider._open_position(market)

    stats = provider.get_stats()
    assert stats["active_markets"] == 1
    assert stats["active_bids"] == 1
    assert stats["active_asks"] == 1
    assert stats["total_orders_placed"] == 2
    assert len(stats["positions"]) == 1


# ── MarketPosition ───────────────────────────────────────────────────

def test_market_position_to_dict():
    """MarketPosition.to_dict() should serialize all fields."""
    pos = MarketPosition(
        condition_id="0xabc",
        question="Test market?",
        yes_token_id="tok_yes",
        no_token_id="tok_no",
        max_spread=0.04,
        midpoint=0.50,
        capital_allocated=100.0,
    )
    d = pos.to_dict()
    assert d["condition_id"] == "0xabc"
    assert d["midpoint"] == 0.5
    assert d["capital_allocated"] == 100.0
    assert d["fill_count"] == 0


def test_market_position_inventory_skew():
    """Inventory skew should reflect fill imbalance."""
    pos = MarketPosition(
        condition_id="0x1",
        question="Q",
        yes_token_id="y",
        no_token_id="n",
        max_spread=0.04,
        fills_yes=10.0,
        fills_no=5.0,
    )
    # (10 - 5) / (10 + 5) = 0.333...
    assert abs(pos.inventory_skew - 0.333) < 0.01


def test_market_position_zero_fills_skew():
    """Zero fills should give zero skew."""
    pos = MarketPosition(
        condition_id="0x1",
        question="Q",
        yes_token_id="y",
        no_token_id="n",
        max_spread=0.04,
    )
    assert pos.inventory_skew == 0.0


# ── QuoteOrder ────────────────────────────────────────────────────────

def test_quote_order_defaults():
    """QuoteOrder should have sane defaults."""
    order = QuoteOrder(
        order_id="test-123",
        token_id="tok",
        side="BUY",
        price=0.48,
        size=10.0,
        is_yes=True,
        condition_id="0xabc",
    )
    assert order.status == "active"
    assert order.placed_at == 0.0


# ── Strategy Integration ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_strategy_has_provider():
    """LiquidityStrategy should expose provider property."""
    cfg = _make_config()
    ctx = type("MockCtx", (), {"account_name": "test"})()
    strat = LiquidityStrategy(cfg, ctx)

    assert strat.provider is not None
    assert isinstance(strat.provider, LiquidityProvider)


@pytest.mark.asyncio
async def test_strategy_stats_include_provider():
    """Strategy get_stats() should include provider stats."""
    cfg = _make_config()
    ctx = type("MockCtx", (), {"account_name": "test"})()
    strat = LiquidityStrategy(cfg, ctx)

    # Need to do a scan first for scanner stats
    await strat.scanner.scan()
    stats = strat.get_stats()

    assert "provider" in stats
    assert "running" in stats["provider"]
    assert "active_markets" in stats["provider"]

    await strat.scanner.close()


@pytest.mark.asyncio
async def test_strategy_lifecycle_with_provider():
    """Start/stop should manage both scanner and provider."""
    cfg = _make_config(scan_interval=9999)
    ctx = type("MockCtx", (), {"account_name": "test"})()
    strat = LiquidityStrategy(cfg, ctx)

    await strat.start()
    assert strat.scanner._running
    assert strat.provider._running

    await strat.stop()
    assert not strat.scanner._running
    assert not strat.provider._running


# ── Phase 3: Inventory & Risk ────────────────────────────────────────

def test_adverse_ratio():
    """Adverse ratio should be losses / rewards."""
    pos = MarketPosition(
        condition_id="0x1",
        question="Q",
        yes_token_id="y",
        no_token_id="n",
        max_spread=0.04,
        total_rewards_earned=10.0,
        total_adverse_loss=3.0,
    )
    assert abs(pos.adverse_ratio - 0.3) < 0.01


def test_adverse_ratio_zero_rewards():
    """Adverse ratio should be 0 when no rewards earned."""
    pos = MarketPosition(
        condition_id="0x1",
        question="Q",
        yes_token_id="y",
        no_token_id="n",
        max_spread=0.04,
        total_adverse_loss=5.0,
    )
    assert pos.adverse_ratio == 0.0


@pytest.mark.asyncio
async def test_record_fill():
    """record_fill should update position fills and adverse loss."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    market = _make_market(midpoint=0.50)
    await provider._open_position(market)

    # Record a fill on YES side at a price above midpoint (adverse)
    provider.record_fill(market.condition_id, is_yes=True, size=10.0, fill_price=0.55)

    pos = provider._positions[market.condition_id]
    assert pos.fills_yes == 10.0
    assert pos.fill_count == 1
    assert pos.total_adverse_loss > 0  # 0.55 > 0.50 midpoint
    assert provider._total_fills == 1


@pytest.mark.asyncio
async def test_record_fill_no_adverse():
    """Fill at midpoint should have zero adverse loss."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    market = _make_market(midpoint=0.50)
    await provider._open_position(market)

    # Fill at exactly midpoint — no adverse selection
    provider.record_fill(market.condition_id, is_yes=True, size=10.0, fill_price=0.50)

    pos = provider._positions[market.condition_id]
    assert pos.total_adverse_loss == 0.0


@pytest.mark.asyncio
async def test_record_rewards():
    """record_rewards should update position rewards."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    market = _make_market()
    await provider._open_position(market)

    provider.record_rewards(market.condition_id, 5.0)
    pos = provider._positions[market.condition_id]
    assert pos.total_rewards_earned == 5.0


@pytest.mark.asyncio
async def test_inventory_skew_affects_pricing():
    """Skewed inventory should widen spread on the long side."""
    cfg = _make_config(spread_pct_of_max=0.50, max_inventory_skew=0.6)
    provider = LiquidityProvider(config=cfg)

    # Use wider spread (0.20 * 0.50 = 0.10) so rounding doesn't hide effects
    # No skew: symmetric
    bid_sym, ask_sym = provider._calculate_prices(0.50, 0.20, skew=0.0)
    spread_sym = ask_sym - bid_sym

    # High positive skew (long YES): should widen bid side
    bid_skew, ask_skew = provider._calculate_prices(0.50, 0.20, skew=0.7)
    spread_skew = ask_skew - bid_skew

    assert spread_skew > spread_sym  # Wider total spread when skewed
    assert bid_skew < bid_sym  # Bid moved further from midpoint


@pytest.mark.asyncio
async def test_severe_skew_one_sided_quoting():
    """Severe skew (>0.8) should only quote rebalancing side."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    market = _make_market(midpoint=0.50)
    await provider._open_position(market)

    # Simulate severe skew by setting fills directly
    pos = provider._positions[market.condition_id]
    pos.fills_yes = 100.0
    pos.fills_no = 5.0
    assert abs(pos.inventory_skew) > 0.8

    # Cancel existing orders
    await provider._cancel_order(pos.bid_order)
    await provider._cancel_order(pos.ask_order)
    pos.bid_order = None
    pos.ask_order = None

    # Re-place with severe skew — should skip bid (long YES)
    placed_before = provider._total_orders_placed
    await provider._place_quotes(pos)

    assert pos.bid_order is None  # Skipped (long YES)
    assert pos.ask_order is not None  # Still quoting NO side
    assert provider._total_orders_placed == placed_before + 1


@pytest.mark.asyncio
async def test_emergency_cancel():
    """Emergency cancel should trigger on >5% price move in <30s."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    market = _make_market(midpoint=0.50)
    await provider._open_position(market)

    pos = provider._positions[market.condition_id]
    # Set last midpoint to simulate rapid detection
    pos.last_midpoint_time = time.time() - 5  # 5 seconds ago
    pos.last_midpoint_value = 0.50

    # Simulate a 10% price move
    market_moved = _make_market(midpoint=0.55)
    # Override tracker midpoint by manipulating market directly
    await provider._refresh_quotes(market_moved)

    assert pos.abandoned is True
    assert provider._emergency_cancels == 1


@pytest.mark.asyncio
async def test_adverse_abandonment():
    """Market should be abandoned when adverse ratio > max."""
    cfg = _make_config(max_markets=3)
    cfg.max_adverse_ratio = 0.7
    provider = LiquidityProvider(config=cfg)

    # Mock scanner
    market = _make_market()

    class MockScanner:
        def get_top_markets(self, n):
            return [market]
        def get_all_markets(self):
            return [market]

    provider.set_scanner(MockScanner())

    # Open position and set high adverse ratio
    await provider._open_position(market)
    pos = provider._positions[market.condition_id]
    pos.total_rewards_earned = 10.0
    pos.total_adverse_loss = 8.0  # 80% adverse ratio > 70% max

    # Refresh should abandon
    await provider._refresh_all()

    assert market.condition_id not in provider._positions
    assert provider._markets_abandoned == 1


@pytest.mark.asyncio
async def test_stats_include_phase3_fields():
    """Stats should include Phase 3 fields."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    stats = provider.get_stats()
    assert "total_rewards" in stats
    assert "total_adverse" in stats
    assert "adverse_ratio" in stats
    assert "emergency_cancels" in stats
    assert "markets_abandoned" in stats


def test_position_to_dict_phase3_fields():
    """to_dict should include Phase 3 fields."""
    pos = MarketPosition(
        condition_id="0x1",
        question="Q",
        yes_token_id="y",
        no_token_id="n",
        max_spread=0.04,
        total_rewards_earned=10.0,
        total_adverse_loss=3.0,
    )
    d = pos.to_dict()
    assert "adverse_ratio" in d
    assert "total_rewards" in d
    assert "total_adverse" in d
    assert "abandoned" in d
    assert d["adverse_ratio"] == 0.3


# ── Order Block Hiding ───────────────────────────────────────────────

def test_find_order_blocks_basic():
    """Should find blocks when cumulative USD exceeds threshold."""
    from src.market_tracker import PriceLevel

    levels = [
        PriceLevel(price=0.50, size=100),   # $50
        PriceLevel(price=0.49, size=200),   # $98 → cumulative $148
        PriceLevel(price=0.48, size=1000),  # $480 → cumulative $628
        PriceLevel(price=0.47, size=500),   # $235 → cumulative $863
    ]
    blocks = LiquidityProvider.find_order_blocks(levels, min_block_usd=500)
    assert len(blocks) >= 1
    assert blocks[0]["price"] == 0.48  # First level crossing $500
    assert blocks[0]["cumulative_usd"] >= 500


def test_find_order_blocks_none():
    """Should return empty list when no block found."""
    from src.market_tracker import PriceLevel

    levels = [
        PriceLevel(price=0.50, size=10),  # $5
        PriceLevel(price=0.49, size=10),  # $4.9 → $9.9
    ]
    blocks = LiquidityProvider.find_order_blocks(levels, min_block_usd=500)
    assert len(blocks) == 0


def test_find_order_blocks_empty():
    """Empty book should return empty list."""
    blocks = LiquidityProvider.find_order_blocks([], min_block_usd=500)
    assert len(blocks) == 0


def test_block_hiding_disabled():
    """When use_order_block_hiding=False, should return fallback."""
    cfg = _make_config(use_order_block_hiding=False)
    provider = LiquidityProvider(config=cfg)

    pos = MarketPosition(
        condition_id="0x1",
        question="Q",
        yes_token_id="y",
        no_token_id="n",
        max_spread=0.04,
    )
    result = provider._get_block_protected_price(pos, is_bid=True, fallback_price=0.48)
    assert result == 0.48


def test_block_hiding_no_tracker():
    """Without tracker, should return fallback."""
    cfg = _make_config(use_order_block_hiding=True)
    provider = LiquidityProvider(config=cfg, tracker=None)

    pos = MarketPosition(
        condition_id="0x1",
        question="Q",
        yes_token_id="y",
        no_token_id="n",
        max_spread=0.04,
    )
    result = provider._get_block_protected_price(pos, is_bid=True, fallback_price=0.48)
    assert result == 0.48


# ── Heartbeat & Scoring ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_heartbeat_not_started_paper():
    """Heartbeat should NOT start in paper mode."""
    cfg = _make_config(use_heartbeat=True)
    provider = LiquidityProvider(config=cfg)

    await provider.start()
    assert not provider._heartbeat_active
    assert provider._heartbeat_task is None

    await provider.stop()


@pytest.mark.asyncio
async def test_heartbeat_fields_in_stats():
    """Stats should include heartbeat fields."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    stats = provider.get_stats()
    assert "heartbeat_active" in stats
    assert "heartbeat_count" in stats
    assert "heartbeat_errors" in stats
    assert stats["heartbeat_active"] is False
    assert stats["heartbeat_count"] == 0


@pytest.mark.asyncio
async def test_scoring_not_started_paper():
    """Scoring loop should NOT start in paper mode."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    await provider.start()
    assert provider._scoring_task is None

    await provider.stop()


@pytest.mark.asyncio
async def test_scoring_fields_in_stats():
    """Stats should include order scoring fields."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    stats = provider.get_stats()
    assert "orders_scoring" in stats
    assert "orders_not_scoring" in stats
    assert "scoring_rate" in stats
    assert "scoring_checks" in stats
    assert stats["scoring_rate"] == 0
    assert stats["scoring_checks"] == 0


def test_scoring_rate_calculation():
    """Scoring rate should be scoring / total."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    provider._orders_scoring = 8
    provider._orders_not_scoring = 2

    stats = provider.get_stats()
    assert stats["scoring_rate"] == 0.8  # 8/10


def test_config_heartbeat_defaults():
    """LiquidityConfig should have heartbeat/scoring defaults."""
    cfg = LiquidityConfig.from_dict({})
    assert cfg.use_heartbeat is False
    assert cfg.heartbeat_interval == 5.0
    assert cfg.scoring_check_interval == 60.0
    assert cfg.quote_refresh_s == 30.0


def test_config_heartbeat_from_dict():
    """LiquidityConfig should parse heartbeat/scoring from dict."""
    raw = {
        "mode": "paper",
        "use_heartbeat": True,
        "heartbeat_interval": 3.0,
        "scoring_check_interval": 120.0,
        "quote_refresh_s": 15.0,
    }
    cfg = LiquidityConfig.from_dict(raw)
    assert cfg.use_heartbeat is True
    assert cfg.heartbeat_interval == 3.0
    assert cfg.scoring_check_interval == 120.0
    assert cfg.quote_refresh_s == 15.0


@pytest.mark.asyncio
async def test_get_auth_headers_no_client():
    """_get_auth_headers should return empty dict without client."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)

    headers = provider._get_auth_headers()
    assert headers == {}


# ── Auditoría Jul 2026: fixes M4, A2+B1, #13, C2 ─────────────────────

class _FakeRisk:
    """Minimal RiskConfig stand-in."""
    def __init__(self, kill_switch=False, max_daily_loss=100.0):
        self.kill_switch = kill_switch
        self.max_daily_loss = max_daily_loss


import sys as _sys
import types as _types


@pytest.fixture
def fake_clob_sdk(monkeypatch):
    """Inject a fake py_clob_client_v2 so live-path lazy imports resolve."""
    fake = _types.ModuleType("py_clob_client_v2")
    fake.OrderArgs = lambda **kw: kw
    ob = _types.ModuleType("py_clob_client_v2.order_builder")
    const = _types.ModuleType("py_clob_client_v2.order_builder.constants")
    const.BUY = "BUY"
    const.SELL = "SELL"
    clob_types = _types.ModuleType("py_clob_client_v2.clob_types")
    clob_types.OrderPayload = lambda **kw: kw
    monkeypatch.setitem(_sys.modules, "py_clob_client_v2", fake)
    monkeypatch.setitem(_sys.modules, "py_clob_client_v2.order_builder", ob)
    monkeypatch.setitem(_sys.modules, "py_clob_client_v2.order_builder.constants", const)
    monkeypatch.setitem(_sys.modules, "py_clob_client_v2.clob_types", clob_types)
    return fake


class _FakeClobPartial:
    """Fake CLOB client: one order with controllable size_matched/status."""
    def __init__(self, size_matched=0.0, status="LIVE", missing=False):
        self.size_matched = size_matched
        self.order_status = status
        self.missing = missing
        self.cancelled_ids = []

    def get_order(self, order_id):
        if self.missing:
            return None
        return {"status": self.order_status, "size_matched": self.size_matched}

    def cancel_order(self, payload):
        oid = payload.get("orderID", "") if isinstance(payload, dict) else getattr(payload, "orderID", "")
        self.cancelled_ids.append(oid)
        return {"canceled": True}


# M4 — signo del adverse selection en el lado NO

@pytest.mark.asyncio
async def test_adverse_no_side_benign_fill_is_zero():
    """BUY NO llenado en su quote normal (venta YES sobre el mid) NO es adverso.

    Antes del fix, este caso registraba adverse = +distance × size en cada
    fill NO benigno, inflando adverse_ratio y abandonando mercados buenos.
    """
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)
    market = _make_market(midpoint=0.50)
    await provider._open_position(market)

    # NO comprado a 0.48 (espacio NO) = vender YES a 0.52, sobre el mid 0.50.
    provider.record_fill(market.condition_id, is_yes=False, size=10.0, fill_price=0.48)

    pos = provider._positions[market.condition_id]
    assert pos.total_adverse_loss == 0.0


@pytest.mark.asyncio
async def test_adverse_no_side_real_adverse_recorded():
    """El mid subió POR ENCIMA de nuestra venta YES efectiva → sí es adverso."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)
    market = _make_market(midpoint=0.50)
    await provider._open_position(market)

    pos = provider._positions[market.condition_id]
    pos.midpoint = 0.60  # el mercado se movió contra nosotros tras el fill

    # NO comprado a 0.48 → venta YES efectiva a 0.52 < mid 0.60 → adverso 0.08×10
    provider.record_fill(market.condition_id, is_yes=False, size=10.0, fill_price=0.48)
    assert pos.total_adverse_loss == pytest.approx(0.8)


# A2 + B1 — fills parciales: delta tracking y cancel del remanente

@pytest.mark.asyncio
async def test_partial_fill_records_delta_and_cancels_remainder(fake_clob_sdk):
    cfg = _make_config(mode="live")
    provider = LiquidityProvider(config=cfg)
    market = _make_market(midpoint=0.50)
    await provider._open_position(market)
    pos = provider._positions[market.condition_id]

    order = QuoteOrder(
        order_id="real-123", token_id="tok_yes_1", side="BUY",
        price=0.48, size=40.0, is_yes=True,
        condition_id=market.condition_id, status="active",
    )
    pos.bid_order = order
    fake = _FakeClobPartial(size_matched=5.0, status="LIVE")
    provider._client = fake
    provider._initialized = True

    status = await provider._check_order_status(order)

    assert status == "MATCHED"
    assert order.status == "filled"
    assert pos.fills_yes == pytest.approx(5.0)         # delta, no el total de la orden
    assert order.size_matched_seen == pytest.approx(5.0)
    assert "real-123" in fake.cancelled_ids            # remanente cancelado (no huérfano)


@pytest.mark.asyncio
async def test_full_fill_no_cancel_needed(fake_clob_sdk):
    cfg = _make_config(mode="live")
    provider = LiquidityProvider(config=cfg)
    market = _make_market(midpoint=0.50)
    await provider._open_position(market)
    pos = provider._positions[market.condition_id]

    order = QuoteOrder(
        order_id="real-456", token_id="tok_yes_1", side="BUY",
        price=0.48, size=40.0, is_yes=True,
        condition_id=market.condition_id, status="active",
    )
    pos.bid_order = order
    fake = _FakeClobPartial(size_matched=40.0, status="MATCHED")
    provider._client = fake
    provider._initialized = True

    status = await provider._check_order_status(order)

    assert status == "MATCHED"
    assert order.status == "filled"
    assert pos.fills_yes == pytest.approx(40.0)
    assert fake.cancelled_ids == []                    # lleno completo: nada que cancelar


@pytest.mark.asyncio
async def test_repeated_check_does_not_double_count(fake_clob_sdk):
    """B1: get_order devuelve size_matched ACUMULADO; no debe re-registrarse."""
    cfg = _make_config(mode="live")
    provider = LiquidityProvider(config=cfg)
    market = _make_market(midpoint=0.50)
    await provider._open_position(market)
    pos = provider._positions[market.condition_id]

    order = QuoteOrder(
        order_id="real-789", token_id="tok_yes_1", side="BUY",
        price=0.48, size=40.0, is_yes=True,
        condition_id=market.condition_id, status="active",
        size_matched_seen=5.0,                          # ya registramos 5
    )
    pos.bid_order = order
    pos.fills_yes = 5.0
    fake = _FakeClobPartial(size_matched=5.0, status="LIVE")  # sin fill nuevo
    provider._client = fake
    provider._initialized = True

    await provider._check_order_status(order)
    assert pos.fills_yes == pytest.approx(5.0)          # sin doble conteo


# #13 — auto-exit: inventario solo se descuenta al confirmarse el fill

@pytest.mark.asyncio
async def test_exit_fill_settles_inventory_and_loss(fake_clob_sdk):
    from src.liquidity_metrics import LiquidityMetrics
    metrics = LiquidityMetrics(total_capital=250.0)
    cfg = _make_config(mode="live")
    provider = LiquidityProvider(config=cfg, metrics=metrics)
    market = _make_market(midpoint=0.50)
    await provider._open_position(market)
    pos = provider._positions[market.condition_id]
    pos.fills_yes = 10.0                                # inventario YES sin match

    pos.exit_order = QuoteOrder(
        order_id="exit-1", token_id="tok_yes_1", side="SELL",
        price=0.45, size=10.0, is_yes=True,
        condition_id=market.condition_id, status="active",
        placed_at=time.time(), ref_price=0.50,
    )
    provider._client = _FakeClobPartial(size_matched=10.0, status="MATCHED")
    provider._initialized = True

    await provider._check_exit_fills()

    assert pos.fills_yes == pytest.approx(0.0)          # descontado AL FILL
    assert pos.exit_order is None
    assert provider._total_auto_exits == 1
    # Pérdida del exit contabilizada: (0.50 - 0.45) × 10 = 0.50
    assert metrics._get_today().exit_loss == pytest.approx(0.5)
    assert metrics._get_today().total_loss == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_exit_unfilled_keeps_inventory_and_expires(fake_clob_sdk):
    cfg = _make_config(mode="live", auto_exit_timeout_s=1.0)
    provider = LiquidityProvider(config=cfg)
    market = _make_market(midpoint=0.50)
    await provider._open_position(market)
    pos = provider._positions[market.condition_id]
    pos.fills_yes = 10.0
    pos.unmatched_since = time.time() - 999

    pos.exit_order = QuoteOrder(
        order_id="exit-2", token_id="tok_yes_1", side="SELL",
        price=0.45, size=10.0, is_yes=True,
        condition_id=market.condition_id, status="active",
        placed_at=time.time() - 10,                     # pasó el timeout
        ref_price=0.50,
    )
    fake = _FakeClobPartial(size_matched=0.0, status="LIVE")
    provider._client = fake
    provider._initialized = True

    await provider._check_exit_fills()

    assert pos.fills_yes == pytest.approx(10.0)         # inventario intacto
    assert pos.exit_order is None                       # orden expirada → reintento
    assert "exit-2" in fake.cancelled_ids
    assert pos.unmatched_since > 0                      # auto-exit reintentará


@pytest.mark.asyncio
async def test_auto_exit_skips_position_with_active_exit_order():
    """No apilar una segunda SELL mientras la primera sigue viva."""
    cfg = _make_config(mode="live", auto_exit_timeout_s=0.0)
    provider = LiquidityProvider(config=cfg)
    market = _make_market(midpoint=0.50)
    await provider._open_position(market)
    pos = provider._positions[market.condition_id]
    pos.fills_yes = 10.0
    pos.unmatched_since = time.time() - 999
    pos.exit_order = QuoteOrder(
        order_id="exit-3", token_id="tok_yes_1", side="SELL",
        price=0.45, size=10.0, is_yes=True,
        condition_id=market.condition_id, status="active",
        placed_at=time.time(),
    )

    placed = []
    async def _fake_sell(pos_, **kw):
        placed.append(kw)
    provider._sell_tokens = _fake_sell
    provider._client = _FakeClobPartial()
    provider._initialized = True

    await provider._check_auto_exits()
    assert placed == []


# C2 — kill switch cubre liquidity

@pytest.mark.asyncio
async def test_kill_switch_blocks_place_order():
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg, risk=_FakeRisk(kill_switch=True))
    result = await provider._place_order(
        token_id="tok", price=0.5, size=10, is_yes=True, condition_id="0xabc",
    )
    assert result is None


@pytest.mark.asyncio
async def test_kill_switch_cancels_all_and_stops_cycle():
    cfg = _make_config()
    risk = _FakeRisk(kill_switch=False)
    provider = LiquidityProvider(config=cfg, risk=risk)

    class _FakeScanner:
        def get_top_markets(self, n):
            return []
        def get_all_markets(self):
            return []
    provider._scanner = _FakeScanner()

    market = _make_market(midpoint=0.50)
    await provider._open_position(market)
    pos = provider._positions[market.condition_id]
    assert pos.bid_order is not None                    # paper quotes colocadas

    risk.kill_switch = True
    await provider._refresh_all()

    assert pos.bid_order is None                        # cancel-all ejecutado
    assert pos.ask_order is None
    assert provider._kill_switch_engaged is True

    # Al desactivar, el ciclo vuelve a operar
    risk.kill_switch = False
    await provider._refresh_all()
    assert provider._kill_switch_engaged is False


@pytest.mark.asyncio
async def test_kill_switch_none_risk_is_noop():
    """Sin RiskConfig (tests antiguos, wiring legacy) todo sigue funcionando."""
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg)                # risk=None
    result = await provider._place_order(
        token_id="tok", price=0.5, size=10, is_yes=True, condition_id="0xabc",
    )
    assert result is not None


# ── C1 (bloque 2): stop-loss diario de liquidity ─────────────────────

from src.liquidity_metrics import LiquidityMetrics


@pytest.mark.asyncio
async def test_daily_loss_halts_quoting_and_cancels():
    """Si el net_pnl del día ≤ -max_daily_loss: cancel-all + stop, guard en place."""
    metrics = LiquidityMetrics(total_capital=250.0)
    cfg = _make_config()
    risk = _FakeRisk(max_daily_loss=20.0)
    provider = LiquidityProvider(config=cfg, metrics=metrics, risk=risk)

    class _FakeScanner:
        def get_top_markets(self, n): return []
        def get_all_markets(self): return []
    provider._scanner = _FakeScanner()

    market = _make_market(midpoint=0.50)
    await provider._open_position(market)
    assert provider._positions[market.condition_id].bid_order is not None

    # Registrar una pérdida por fill que supera el límite (25 > 20)
    metrics._get_today().adverse_loss = 25.0
    assert metrics.today_net_pnl() == pytest.approx(-25.0)

    await provider._refresh_all()

    pos = provider._positions[market.condition_id]
    assert provider._daily_loss_halted is True
    assert pos.bid_order is None and pos.ask_order is None  # cancel-all
    # Guard de última línea: no coloca órdenes nuevas mientras halted
    blocked = await provider._place_order(
        token_id="tok", price=0.5, size=10, is_yes=True, condition_id="0xabc",
    )
    assert blocked is None


@pytest.mark.asyncio
async def test_daily_loss_not_halted_below_limit():
    metrics = LiquidityMetrics(total_capital=250.0)
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg, metrics=metrics, risk=_FakeRisk(max_daily_loss=100.0))

    class _FakeScanner:
        def get_top_markets(self, n): return []
        def get_all_markets(self): return []
    provider._scanner = _FakeScanner()

    metrics._get_today().adverse_loss = 25.0   # -25 > -100 → no halt
    await provider._refresh_all()
    assert provider._daily_loss_halted is False


@pytest.mark.asyncio
async def test_daily_loss_lifts_when_pnl_recovers():
    """El halt se levanta si el net del día vuelve por encima del límite (rollover simula esto)."""
    metrics = LiquidityMetrics(total_capital=250.0)
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg, metrics=metrics, risk=_FakeRisk(max_daily_loss=20.0))

    class _FakeScanner:
        def get_top_markets(self, n): return []
        def get_all_markets(self): return []
    provider._scanner = _FakeScanner()

    metrics._get_today().adverse_loss = 25.0
    await provider._refresh_all()
    assert provider._daily_loss_halted is True

    # Nuevo día = net 0 (o rewards que recuperan): halt se levanta
    metrics._get_today().adverse_loss = 0.0
    await provider._refresh_all()
    assert provider._daily_loss_halted is False


@pytest.mark.asyncio
async def test_daily_loss_disabled_when_no_risk_or_zero_limit():
    """Sin RiskConfig o con max_daily_loss=0, el stop-loss no actúa (opt-out)."""
    metrics = LiquidityMetrics(total_capital=250.0)
    cfg = _make_config()

    class _FakeScanner:
        def get_top_markets(self, n): return []
        def get_all_markets(self): return []

    # Sin risk
    p1 = LiquidityProvider(config=cfg, metrics=metrics)
    p1._scanner = _FakeScanner()
    metrics._get_today().adverse_loss = 999.0
    await p1._refresh_all()
    assert p1._daily_loss_halted is False

    # max_daily_loss = 0 → desactivado
    p2 = LiquidityProvider(config=cfg, metrics=LiquidityMetrics(), risk=_FakeRisk(max_daily_loss=0.0))
    p2._scanner = _FakeScanner()
    p2._metrics._get_today().adverse_loss = 999.0
    await p2._refresh_all()
    assert p2._daily_loss_halted is False


def test_today_net_pnl_helper():
    m = LiquidityMetrics(total_capital=250.0)
    m._get_today().rewards_earned = 10.0
    m._get_today().adverse_loss = 3.0
    m._get_today().exit_loss = 2.0
    assert m.today_net_pnl() == pytest.approx(5.0)  # 10 - (3+2)


def test_stats_expose_risk_halts():
    metrics = LiquidityMetrics(total_capital=250.0)
    cfg = _make_config()
    provider = LiquidityProvider(config=cfg, metrics=metrics, risk=_FakeRisk(max_daily_loss=20.0))
    stats = provider.get_stats()
    assert "daily_loss_halted" in stats
    assert "today_net_pnl" in stats
    assert stats["max_daily_loss"] == 20.0
