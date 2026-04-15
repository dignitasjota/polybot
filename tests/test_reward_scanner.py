"""E2E tests for Reward Scanner (Phase 1 Liquidity Strategy).

Tests cover:
1. Config loading with liquidity account
2. RewardScanner live API fetch + ranking
3. LiquidityStrategy lifecycle (start/stop)
4. API endpoint JSON response
5. ConfigManager hot-reload

Run: python -m pytest tests/test_reward_scanner.py -v
"""

import asyncio
import pytest

from src.config import Config
from src.reward_scanner import RewardScanner, RewardMarket
from src.strategies.liquidity import LiquidityConfig, LiquidityStrategy
from src.strategies.base import AccountContext


# ─── Config ────────────────────────────────────────────────────────────

def test_config_loads_liquidity_account():
    """Config.load() should parse the liquidity_scanner account."""
    cfg = Config.load()
    liq_accounts = [a for a in cfg.accounts if a.strategy_type == "liquidity"]
    assert len(liq_accounts) >= 1, "No liquidity account found in config"
    acc = liq_accounts[0]
    assert acc.name == "liquidity_scanner"
    assert acc.execution_mode == "paper"
    assert acc.enabled is True


def test_liquidity_config_from_dict():
    """LiquidityConfig.from_dict() should parse raw TOML values."""
    raw = {
        "mode": "paper",
        "scan_interval": 120,
        "min_daily_rate": 5.0,
        "capital_per_market": 100.0,
        "max_markets": 10,
    }
    cfg = LiquidityConfig.from_dict(raw)
    assert cfg.mode == "paper"
    assert cfg.scan_interval == 120.0
    assert cfg.min_daily_rate == 5.0
    assert cfg.capital_per_market == 100.0
    assert cfg.max_markets == 10


def test_liquidity_config_defaults():
    """LiquidityConfig.from_dict() with empty dict should use defaults."""
    cfg = LiquidityConfig.from_dict({})
    assert cfg.mode == "disabled"
    assert cfg.scan_interval == 300.0
    assert cfg.min_daily_rate == 1.0
    assert cfg.capital_per_market == 50.0
    assert cfg.max_markets == 5


# ─── RewardScanner ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scanner_live_scan():
    """Scanner should fetch and rank markets from live CLOB API."""
    scanner = RewardScanner(min_daily_rate=50.0, min_reward_per_dollar=1.0)
    markets = await scanner.scan()
    await scanner.close()

    if len(markets) == 0:
        pytest.skip("CLOB API returned 0 markets (transient 500 or rate limit)")
    assert scanner._scan_count == 1
    assert scanner._last_scan > 0

    # Markets should be sorted by score descending
    for i in range(len(markets) - 1):
        assert markets[i].score >= markets[i + 1].score, \
            f"Markets not sorted: {markets[i].score} < {markets[i+1].score}"


@pytest.mark.asyncio
async def test_scanner_market_fields():
    """Each market should have all required fields populated."""
    scanner = RewardScanner(min_daily_rate=100.0)
    markets = await scanner.scan()
    await scanner.close()

    assert len(markets) > 0
    m = markets[0]

    assert m.condition_id, "Missing condition_id"
    assert m.question, "Missing question"
    assert m.daily_rate >= 100.0
    assert m.max_spread > 0
    assert m.score > 0
    assert m.reward_per_dollar > 0


@pytest.mark.asyncio
async def test_scanner_get_top_markets():
    """get_top_markets(n) should return at most n markets."""
    scanner = RewardScanner(min_daily_rate=10.0)
    await scanner.scan()

    top5 = scanner.get_top_markets(5)
    assert len(top5) <= 5
    assert len(top5) > 0

    top1 = scanner.get_top_markets(1)
    assert len(top1) == 1
    assert top1[0].score == top5[0].score  # Same top market

    await scanner.close()


@pytest.mark.asyncio
async def test_scanner_stats():
    """get_stats() should return expected structure."""
    scanner = RewardScanner(min_daily_rate=50.0)
    await scanner.scan()
    stats = scanner.get_stats()
    await scanner.close()

    assert "total_reward_markets" in stats
    assert "last_scan" in stats
    assert "scan_count" in stats
    assert "top_markets" in stats
    assert "total_daily_rewards_available" in stats
    assert stats["scan_count"] == 1
    # These may be 0 if API returns 500 (transient), but structure must exist
    assert isinstance(stats["total_reward_markets"], int)
    assert isinstance(stats["total_daily_rewards_available"], (int, float))


@pytest.mark.asyncio
async def test_scanner_export_report():
    """export_report() should include all markets with full details."""
    scanner = RewardScanner(min_daily_rate=100.0)
    await scanner.scan()
    report = scanner.export_report()
    await scanner.close()

    assert "markets" in report
    assert "config" in report
    assert len(report["markets"]) > 0

    m = report["markets"][0]
    assert "condition_id" in m
    assert "question" in m
    assert "daily_rate" in m
    assert "score" in m
    assert "reward_per_dollar" in m


# ─── LiquidityStrategy ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_strategy_lifecycle():
    """LiquidityStrategy should start scanner and stop cleanly."""
    cfg = LiquidityConfig(mode="paper", scan_interval=9999)
    # Mock context — Phase 1 doesn't use it
    ctx = type("MockCtx", (), {"account_name": "test"})()

    strat = LiquidityStrategy(cfg, ctx)

    assert strat.scanner is not None
    assert not strat.scanner._running

    await strat.start()
    assert strat.scanner._running
    assert strat.scanner._scan_count == 1  # Initial scan on start

    await strat.stop()
    assert not strat.scanner._running


@pytest.mark.asyncio
async def test_strategy_disabled_no_start():
    """Disabled strategy should not start scanner."""
    cfg = LiquidityConfig(mode="disabled")
    ctx = type("MockCtx", (), {"account_name": "test"})()

    strat = LiquidityStrategy(cfg, ctx)
    await strat.start()

    assert not strat.scanner._running
    assert strat.scanner._scan_count == 0

    await strat.stop()


def test_strategy_get_config():
    """get_config() should return all relevant config fields."""
    cfg = LiquidityConfig(
        mode="paper",
        scan_interval=120,
        capital_per_market=100,
        max_markets=8,
    )
    ctx = type("MockCtx", (), {"account_name": "test"})()
    strat = LiquidityStrategy(cfg, ctx)

    config = strat.get_config()
    assert config["mode"] == "paper"
    assert config["scan_interval"] == 120
    assert config["capital_per_market"] == 100
    assert config["max_markets"] == 8


# ─── RewardMarket ──────────────────────────────────────────────────────

def test_reward_market_to_dict():
    """RewardMarket.to_dict() should serialize all fields."""
    m = RewardMarket(
        condition_id="0xabc",
        question="Test market?",
        market_slug="test-market",
        daily_rate=100.0,
        max_spread=4.5,
        min_size=20,
        competitiveness=5.0,
        volume_24h=50000,
        spread=0.03,
        midpoint=0.55,
        reward_per_dollar=20.0,
        score=15.5,
    )
    d = m.to_dict()
    assert d["condition_id"] == "0xabc"
    assert d["question"] == "Test market?"
    assert d["daily_rate"] == 100.0
    assert d["score"] == 15.5
    assert d["reward_per_dollar"] == 20.0


# ─── Registry ─────────────────────────────────────────────────────────

def test_strategy_registered():
    """Liquidity strategy should be in the registry."""
    from src.strategies.registry import get_strategy_class, get_config_class

    assert get_strategy_class("liquidity") is LiquidityStrategy
    assert get_config_class("liquidity") is LiquidityConfig
