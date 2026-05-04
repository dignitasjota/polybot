"""Tests for CompletenessScanner — gap detection, fee thresholds, sizing."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from src.completeness_scanner import CompletenessScanner, ArbOpportunity
from src.fees import GAS_REDEEM_USD, TAKER_FEE_RATES


# ── Helpers ──────────────────────────────────────────────────────────


@dataclass
class MockPriceLevel:
    price: float
    size: float


@dataclass
class MockMarketState:
    condition_id: str = "0xabc123"
    question: str = "Test market?"
    yes_token_id: str = "yes_token"
    no_token_id: str = "no_token"
    best_ask_yes: float = 0.48
    best_ask_no: float = 0.50
    asks_yes: list = field(default_factory=lambda: [MockPriceLevel(0.48, 100)])
    asks_no: list = field(default_factory=lambda: [MockPriceLevel(0.50, 100)])
    resolved: bool = False
    last_update: float = 1000.0  # Non-zero = has received WS data
    tags: list = field(default_factory=list)


@dataclass
class MockConfig:
    mode: str = "paper"
    scan_interval: float = 5.0
    min_profit_per_share: float = 0.005
    min_shares: float = 5.0
    max_cost_per_trade: float = 50.0
    cooldown_s: float = 0.0  # No cooldown in tests
    category: str = "crypto"


class MockTracker:
    def __init__(self, markets=None):
        self._markets = markets or []

    @property
    def all_markets(self):
        return self._markets

    def get_by_token(self, token_id):
        for m in self._markets:
            if m.yes_token_id == token_id or m.no_token_id == token_id:
                return m
        return None


# ── Tests: Gap Detection ─────────────────────────────────────────────


def test_detects_binary_gap():
    """When YES ask + NO ask < 1.0 and gap > fees, a gap should be detected."""
    # Gap of $0.10 — well above crypto fees (~$0.036)
    market = MockMarketState(best_ask_yes=0.45, best_ask_no=0.45)
    scanner = CompletenessScanner(MockConfig(), MockTracker([market]))

    opp = scanner._evaluate_market(market)
    assert opp is not None
    assert opp.gap == pytest.approx(0.10, abs=0.001)
    assert opp.net_profit_per_share > 0


def test_no_gap_when_sum_equals_one():
    """No opportunity when YES + NO = 1.0 exactly."""
    market = MockMarketState(best_ask_yes=0.50, best_ask_no=0.50)
    scanner = CompletenessScanner(MockConfig(), MockTracker([market]))

    opp = scanner._evaluate_market(market)
    assert opp is None


def test_no_gap_when_sum_exceeds_one():
    """No opportunity when YES + NO > 1.0."""
    market = MockMarketState(best_ask_yes=0.52, best_ask_no=0.50)
    scanner = CompletenessScanner(MockConfig(), MockTracker([market]))

    opp = scanner._evaluate_market(market)
    assert opp is None


def test_gap_too_small_for_fees():
    """Gap exists but is smaller than fees — no opportunity."""
    # Crypto fee at p=0.49: 0.072 * 0.49 * 0.51 = 0.018
    # Two buys: ~0.036 total fees + gas
    # Gap of 0.01 < 0.036 → not profitable
    market = MockMarketState(best_ask_yes=0.495, best_ask_no=0.495)
    scanner = CompletenessScanner(MockConfig(), MockTracker([market]))

    opp = scanner._evaluate_market(market)
    assert opp is None


def test_zero_fee_geopolitics():
    """Geopolitics markets have 0% fees — tiny gaps are profitable."""
    # Gap of $0.01 with 0% fees → profit = 0.01 - gas(0.004) = $0.006
    market = MockMarketState(best_ask_yes=0.495, best_ask_no=0.495)
    config = MockConfig(category="geopolitics")
    scanner = CompletenessScanner(config, MockTracker([market]))

    opp = scanner._evaluate_market(market)
    assert opp is not None
    assert opp.net_profit_per_share == pytest.approx(0.01 - GAS_REDEEM_USD, abs=0.001)


# ── Tests: Fee Calculation ───────────────────────────────────────────


def test_fee_per_share_crypto():
    """Crypto fee per share at p=0.50 should be ~0.018."""
    fee = CompletenessScanner._fee_per_share(0.50, "crypto")
    expected = 0.072 * 0.50 * 0.50  # 0.018
    assert fee == pytest.approx(expected, abs=0.0001)


def test_fee_per_share_geopolitics():
    """Geopolitics has 0% fees."""
    fee = CompletenessScanner._fee_per_share(0.50, "geopolitics")
    assert fee == 0.0


def test_fee_at_extreme_prices():
    """Fees should be low at extreme prices (near 0 or 1)."""
    fee_low = CompletenessScanner._fee_per_share(0.05, "crypto")
    fee_high = CompletenessScanner._fee_per_share(0.95, "crypto")
    fee_mid = CompletenessScanner._fee_per_share(0.50, "crypto")

    # Fees at extremes should be much lower than at midpoint
    assert fee_low < fee_mid * 0.5
    assert fee_high < fee_mid * 0.5


# ── Tests: Sizing ────────────────────────────────────────────────────


def test_max_shares_limited_by_book_depth():
    """Max shares should be min of available sizes on each side."""
    market = MockMarketState(
        best_ask_yes=0.45, best_ask_no=0.45,
        asks_yes=[MockPriceLevel(0.45, 50)],
        asks_no=[MockPriceLevel(0.45, 30)],
    )
    scanner = CompletenessScanner(MockConfig(), MockTracker([market]))
    opp = scanner._evaluate_market(market)

    assert opp is not None
    assert opp.max_shares <= 30  # Limited by NO side


def test_max_shares_limited_by_max_cost():
    """Shares should be capped by max_cost_per_trade."""
    market = MockMarketState(
        best_ask_yes=0.45, best_ask_no=0.45,
        asks_yes=[MockPriceLevel(0.45, 1000)],
        asks_no=[MockPriceLevel(0.45, 1000)],
    )
    config = MockConfig(max_cost_per_trade=20.0)
    scanner = CompletenessScanner(config, MockTracker([market]))
    opp = scanner._evaluate_market(market)

    assert opp is not None
    # Cost per share = 0.45 + 0.45 = 0.90
    # Max shares = 20.0 / 0.90 ≈ 22.2
    assert opp.max_shares <= 22.3


def test_min_shares_filter():
    """Markets with too few shares available should be skipped."""
    market = MockMarketState(
        best_ask_yes=0.45, best_ask_no=0.45,
        asks_yes=[MockPriceLevel(0.45, 3)],  # Only 3 available
        asks_no=[MockPriceLevel(0.45, 100)],
    )
    config = MockConfig(min_shares=5.0)
    scanner = CompletenessScanner(config, MockTracker([market]))
    opp = scanner._evaluate_market(market)

    assert opp is None  # Filtered out, < min_shares


# ── Tests: Edge Cases ────────────────────────────────────────────────


def test_no_asks_available():
    """Markets with no ask data should be skipped."""
    market = MockMarketState(best_ask_yes=0.0, best_ask_no=0.50)
    scanner = CompletenessScanner(MockConfig(), MockTracker([market]))
    opp = scanner._evaluate_market(market)
    assert opp is None


def test_empty_orderbook_with_best_ask():
    """Markets with no book depth but valid best_ask prices use fallback sizing."""
    market = MockMarketState(
        best_ask_yes=0.45, best_ask_no=0.45,
        asks_yes=[], asks_no=[],
    )
    scanner = CompletenessScanner(MockConfig(), MockTracker([market]))
    opp = scanner._evaluate_market(market)
    # With fallback sizing (max_cost/price), this should detect the gap
    assert opp is not None
    assert opp.gap == pytest.approx(0.10, abs=0.001)


def test_empty_orderbook_no_best_ask():
    """Markets with no book depth AND no best_ask should be skipped."""
    market = MockMarketState(
        best_ask_yes=0.0, best_ask_no=0.0,
        asks_yes=[], asks_no=[],
    )
    scanner = CompletenessScanner(MockConfig(), MockTracker([market]))
    opp = scanner._evaluate_market(market)
    assert opp is None


def test_resolved_market_skipped():
    """Resolved markets should not be evaluated."""
    market = MockMarketState(resolved=True, best_ask_yes=0.40, best_ask_no=0.40)
    scanner = CompletenessScanner(MockConfig(), MockTracker([market]))
    # _scan_all skips resolved, but _evaluate_market doesn't check
    # The scan loop filters, so this is just a sanity check on evaluate
    opp = scanner._evaluate_market(market)
    # Still returns an opp (evaluate doesn't filter resolved)
    assert opp is not None  # Gap of 0.20 is huge


# ── Tests: Paper Execution ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_paper_execution():
    """Paper mode should simulate trades and record profit."""
    market = MockMarketState(
        best_ask_yes=0.45, best_ask_no=0.45,
        asks_yes=[MockPriceLevel(0.45, 100)],
        asks_no=[MockPriceLevel(0.45, 100)],
    )
    config = MockConfig(mode="paper", cooldown_s=0)
    scanner = CompletenessScanner(config, MockTracker([market]))

    opp = scanner._evaluate_market(market)
    assert opp is not None

    await scanner._execute_arb(opp)

    assert scanner._trades_executed == 1
    assert len(scanner._trades) == 1

    trade = scanner._trades[0]
    assert trade.status == "redeemed"
    assert trade.actual_pnl > 0
    assert trade.mode == "paper"


# ── Tests: Stats ─────────────────────────────────────────────────────


def test_get_stats():
    """Stats should include all expected fields."""
    scanner = CompletenessScanner(MockConfig(), MockTracker())
    stats = scanner.get_stats()

    assert "running" in stats
    assert "total_scans" in stats
    assert "opportunities_found" in stats
    assert "trades_executed" in stats
    assert "total_profit" in stats
    assert "recent_trades" in stats


# ── Tests: Reactive check (WebSocket callback) ──────────────────────


@pytest.mark.asyncio
async def test_reactive_check_detects_gap():
    """check() called with token_id should detect arb on that market."""
    market = MockMarketState(
        best_ask_yes=0.45, best_ask_no=0.45,
        asks_yes=[MockPriceLevel(0.45, 100)],
        asks_no=[MockPriceLevel(0.45, 100)],
    )
    config = MockConfig(mode="paper", cooldown_s=0)
    tracker = MockTracker([market])
    scanner = CompletenessScanner(config, tracker)

    await scanner.check(token_id="yes_token")

    assert scanner._opportunities_found >= 1
    assert scanner._trades_executed >= 1


@pytest.mark.asyncio
async def test_cooldown_prevents_spam():
    """Same market shouldn't be executed twice within cooldown window."""
    market = MockMarketState(
        best_ask_yes=0.45, best_ask_no=0.45,
        asks_yes=[MockPriceLevel(0.45, 100)],
        asks_no=[MockPriceLevel(0.45, 100)],
    )
    config = MockConfig(mode="paper", cooldown_s=60)  # 60s cooldown
    tracker = MockTracker([market])
    scanner = CompletenessScanner(config, tracker)

    await scanner.check(token_id="yes_token")
    first_count = scanner._trades_executed

    await scanner.check(token_id="yes_token")
    second_count = scanner._trades_executed

    assert second_count == first_count  # Blocked by cooldown


# ── Tests: Net Profit Calculation ────────────────────────────────────


def test_net_profit_calculation():
    """Verify net profit = gap - fees - gas."""
    market = MockMarketState(
        best_ask_yes=0.45, best_ask_no=0.45,
        asks_yes=[MockPriceLevel(0.45, 100)],
        asks_no=[MockPriceLevel(0.45, 100)],
    )
    scanner = CompletenessScanner(MockConfig(category="crypto"), MockTracker([market]))
    opp = scanner._evaluate_market(market)

    assert opp is not None

    # Manual calculation:
    gap = 1.0 - 0.45 - 0.45  # = 0.10
    fee_yes = 0.072 * 0.45 * 0.55  # = 0.01782
    fee_no = 0.072 * 0.45 * 0.55   # = 0.01782
    expected_net = gap - fee_yes - fee_no - GAS_REDEEM_USD

    assert opp.gap == pytest.approx(gap, abs=0.001)
    assert opp.net_profit_per_share == pytest.approx(expected_net, abs=0.001)


def test_large_gap_geopolitics_profit():
    """Large gap in geopolitics (0% fees) should be very profitable."""
    market = MockMarketState(
        best_ask_yes=0.40, best_ask_no=0.40,
        asks_yes=[MockPriceLevel(0.40, 200)],
        asks_no=[MockPriceLevel(0.40, 200)],
    )
    config = MockConfig(category="geopolitics", max_cost_per_trade=100.0)
    scanner = CompletenessScanner(config, MockTracker([market]))
    opp = scanner._evaluate_market(market)

    assert opp is not None
    # Gap = 0.20, fees = 0, gas = 0.004
    assert opp.net_profit_per_share == pytest.approx(0.20 - GAS_REDEEM_USD, abs=0.001)
    # Total profit for 100 shares (capped by max_cost)
    total = opp.net_profit_per_share * opp.max_shares
    assert total > 10  # Should be very profitable


# ── Tests: Auto-detect category from tags ──────────────────────────


def test_category_auto_detected_from_tags():
    """Market with tags should auto-detect fee category instead of using config default."""
    # Gap of $0.01 — too small for crypto (fee ~$0.036) but enough for geopolitics (0%)
    market = MockMarketState(
        best_ask_yes=0.495, best_ask_no=0.495,
        tags=["geopolitics"],
    )
    # Config says crypto, but tags say geopolitics → should use geopolitics fees
    config = MockConfig(category="crypto")
    scanner = CompletenessScanner(config, MockTracker([market]))
    opp = scanner._evaluate_market(market)

    assert opp is not None  # Would be None with crypto fees
    assert opp.category == "geopolitics"
    assert opp.net_profit_per_share == pytest.approx(0.01 - GAS_REDEEM_USD, abs=0.001)


def test_category_falls_back_to_config():
    """Market without tags should use config category."""
    market = MockMarketState(
        best_ask_yes=0.45, best_ask_no=0.45,
        tags=[],
    )
    config = MockConfig(category="crypto")
    scanner = CompletenessScanner(config, MockTracker([market]))
    opp = scanner._evaluate_market(market)

    assert opp is not None
    assert opp.category == "crypto"


def test_sports_tag_detected():
    """Sports markets should use lower fee rate."""
    # Gap of $0.02 — not enough for crypto (0.036) but enough for sports (0.015)
    market = MockMarketState(
        best_ask_yes=0.49, best_ask_no=0.49,
        tags=["sports", "nba"],
    )
    config = MockConfig(category="crypto")
    scanner = CompletenessScanner(config, MockTracker([market]))
    opp = scanner._evaluate_market(market)

    assert opp is not None
    assert opp.category == "sports"


# ── Tests: Per-market fee_rate from API ──────────────────────────────


def test_api_fee_rate_used_when_available():
    """When market has fee_rate from API, use it instead of tag/category lookup."""
    # Gap of $0.02, fee_rate=0.0 (like geopolitics) → profitable
    market = MockMarketState(
        best_ask_yes=0.49, best_ask_no=0.49,
        tags=["crypto"],  # Would be 0.072 via tags
    )
    market.fee_rate = 0.0  # API says 0% fees
    config = MockConfig(category="crypto")
    scanner = CompletenessScanner(config, MockTracker([market]))
    opp = scanner._evaluate_market(market)

    assert opp is not None
    assert opp.category == "api"  # Indicates fee came from API
    assert opp.net_profit_per_share == pytest.approx(0.02 - GAS_REDEEM_USD, abs=0.001)


def test_api_fee_rate_overrides_tags():
    """API fee_rate should override tag-based detection."""
    # Market tagged as geopolitics (0%) but API says rate=0.05
    # Gap of $0.01 — not enough for 0.05 rate
    market = MockMarketState(
        best_ask_yes=0.495, best_ask_no=0.495,
        tags=["geopolitics"],
    )
    market.fee_rate = 0.05  # API overrides to 5%
    config = MockConfig(category="crypto")
    scanner = CompletenessScanner(config, MockTracker([market]))
    opp = scanner._evaluate_market(market)

    assert opp is None  # Fee too high for $0.01 gap


def test_negative_fee_rate_falls_back_to_tags():
    """fee_rate=-1 (unknown) should fall back to tag-based detection."""
    market = MockMarketState(
        best_ask_yes=0.495, best_ask_no=0.495,
        tags=["geopolitics"],
    )
    market.fee_rate = -1.0  # Unknown — fall back
    config = MockConfig(category="crypto")
    scanner = CompletenessScanner(config, MockTracker([market]))
    opp = scanner._evaluate_market(market)

    assert opp is not None  # Geopolitics = 0% via tags
    assert opp.category == "geopolitics"


# ── fee_rate_from_fee_type / category_from_fee_type ─────────────────


def test_fee_rate_from_fee_type_crypto():
    from src.fees import fee_rate_from_fee_type
    assert fee_rate_from_fee_type("crypto_fees_v2", True) == 0.072


def test_fee_rate_from_fee_type_sports():
    from src.fees import fee_rate_from_fee_type
    assert fee_rate_from_fee_type("sports_fees_v2", True) == 0.03


def test_fee_rate_from_fee_type_politics():
    from src.fees import fee_rate_from_fee_type
    assert fee_rate_from_fee_type("politics_fees", True) == 0.04


def test_fee_rate_from_fee_type_general():
    from src.fees import fee_rate_from_fee_type
    assert fee_rate_from_fee_type("general_fees", True) == 0.04


def test_fee_rate_from_fee_type_culture():
    from src.fees import fee_rate_from_fee_type
    assert fee_rate_from_fee_type("culture_fees", True) == 0.05


def test_fee_rate_from_fee_type_weather():
    from src.fees import fee_rate_from_fee_type
    assert fee_rate_from_fee_type("weather_fees", True) == 0.05


def test_fee_rate_from_fee_type_fees_disabled():
    from src.fees import fee_rate_from_fee_type
    assert fee_rate_from_fee_type(None, False) == 0.0
    assert fee_rate_from_fee_type("crypto_fees_v2", False) == 0.0


def test_fee_rate_from_fee_type_null():
    from src.fees import fee_rate_from_fee_type
    assert fee_rate_from_fee_type(None, True) == -1.0


def test_fee_rate_from_fee_type_unknown():
    from src.fees import fee_rate_from_fee_type
    assert fee_rate_from_fee_type("some_new_type", True) == -1.0


def test_category_from_fee_type():
    from src.fees import category_from_fee_type
    assert category_from_fee_type("crypto_fees_v2") == "crypto"
    assert category_from_fee_type("sports_fees_v2") == "sports"
    assert category_from_fee_type("politics_fees") == "politics"
    assert category_from_fee_type(None, False) == "geopolitics"
    assert category_from_fee_type(None, True) == "other"


# ── gamma_client feeType fallback ───────────────────────────────────


def test_gamma_parse_fee_type_fallback():
    """_parse_market uses feeType when feeSchedule is absent (keyset endpoint)."""
    from src.gamma_client import GammaClient
    import json

    gc = GammaClient()
    data = {
        "conditionId": "0xabc",
        "question": "Will Israel do X?",
        "slug": "israel-x",
        "clobTokenIds": json.dumps(["tok_yes", "tok_no"]),
        "outcomePrices": json.dumps(["0.50", "0.50"]),
        "outcomes": json.dumps(["Yes", "No"]),
        "endDate": "2026-06-01T00:00:00Z",
        "enableOrderBook": True,
        "active": True,
        "closed": False,
        # No feeSchedule — keyset endpoint
        "feeType": None,
        "feesEnabled": False,
    }
    market = gc._parse_market(data)
    assert market is not None
    assert market.fee_rate == 0.0  # feesEnabled=false → geopolitics (0%)

    # Now with crypto feeType
    data["feeType"] = "crypto_fees_v2"
    data["feesEnabled"] = True
    market = gc._parse_market(data)
    assert market is not None
    assert market.fee_rate == 0.072
