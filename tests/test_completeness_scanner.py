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
    best_bid_yes: float = 0.0
    best_bid_no: float = 0.0
    asks_yes: list = field(default_factory=lambda: [MockPriceLevel(0.48, 100)])
    asks_no: list = field(default_factory=lambda: [MockPriceLevel(0.50, 100)])
    resolved: bool = False
    last_update: float = 1000.0  # Non-zero = has received WS data
    tags: list = field(default_factory=list)
    hours_to_resolution: float | None = None  # None = skip close-to-resolution guard


@dataclass
class MockConfig:
    mode: str = "paper"
    scan_interval: float = 5.0
    min_profit_per_share: float = 0.005
    min_shares: float = 5.0
    max_cost_per_trade: float = 50.0
    cooldown_s: float = 0.0  # No cooldown in tests
    category: str = "crypto"
    # Reality guards neutralized by default so the legacy gap/fee/sizing tests
    # below exercise that mechanic in isolation. Each guard has dedicated tests
    # (TestRealityGuards) that enable it with production values.
    max_plausible_gap: float = 1.0       # disabled (allow legacy 0.10 gaps)
    max_quote_age_s: float = 0.0         # disabled (allow fixed last_update=1000.0)
    require_book_depth: bool = False     # legacy fallback sizing enabled


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


# ── Tests: Reality Guards (anti phantom/stale fills) ─────────────────


class TestRealityGuards:
    """Guards that stop paper mode from booking fake profit on phantom asks.

    See _evaluate_market: sanity cap on gap, quote freshness, and the
    require_book_depth flag (no fabricated `max_cost/price` sizing).
    """

    # --- Sanity cap on gap -------------------------------------------------

    def test_implausible_gap_rejected(self):
        """A 42¢ gap (the kind seen on stale 5-min legs) must be rejected."""
        cfg = MockConfig(max_plausible_gap=0.05)
        scanner = CompletenessScanner(config=cfg)
        # YES 0.05 (stale loser) + NO 0.53 → gap 0.42
        market = MockMarketState(
            best_ask_yes=0.05, best_ask_no=0.53,
            asks_yes=[MockPriceLevel(0.05, 100)],
            asks_no=[MockPriceLevel(0.53, 100)],
        )
        assert scanner._evaluate_market(market) is None

    def test_plausible_small_gap_passes_cap(self):
        """A real sub-cap gap still detects (geopolitics 0% fees)."""
        cfg = MockConfig(max_plausible_gap=0.05, category="geopolitics")
        scanner = CompletenessScanner(config=cfg)
        # gap 0.04 < cap 0.05, profitable at 0% fees
        market = MockMarketState(best_ask_yes=0.48, best_ask_no=0.48)
        opp = scanner._evaluate_market(market)
        assert opp is not None
        assert opp.gap == pytest.approx(0.04, abs=0.001)

    def test_gap_just_below_cap_passes(self):
        """A gap just under the cap is allowed (only clearly-large gaps reject)."""
        cfg = MockConfig(max_plausible_gap=0.05, category="geopolitics")
        scanner = CompletenessScanner(config=cfg)
        market = MockMarketState(best_ask_yes=0.48, best_ask_no=0.475)  # gap ~0.045
        assert scanner._evaluate_market(market) is not None

    # --- Quote freshness ---------------------------------------------------

    def test_stale_quote_rejected(self):
        """A quote older than max_quote_age_s is rejected as stale."""
        cfg = MockConfig(max_quote_age_s=5.0, category="geopolitics")
        scanner = CompletenessScanner(config=cfg)
        market = MockMarketState(
            best_ask_yes=0.48, best_ask_no=0.48,
            last_update=time.time() - 60,  # 60s old
        )
        assert scanner._evaluate_market(market) is None

    def test_fresh_quote_passes(self):
        """A recently-updated quote is accepted."""
        cfg = MockConfig(max_quote_age_s=5.0, category="geopolitics")
        scanner = CompletenessScanner(config=cfg)
        market = MockMarketState(
            best_ask_yes=0.48, best_ask_no=0.48,
            last_update=time.time(),
        )
        assert scanner._evaluate_market(market) is not None

    def test_never_updated_quote_rejected(self):
        """last_update == 0 (never received WS data) is rejected when guard on."""
        cfg = MockConfig(max_quote_age_s=5.0, category="geopolitics")
        scanner = CompletenessScanner(config=cfg)
        market = MockMarketState(
            best_ask_yes=0.48, best_ask_no=0.48, last_update=0,
        )
        assert scanner._evaluate_market(market) is None

    # --- require_book_depth ------------------------------------------------

    def test_no_depth_rejected_when_required(self):
        """With require_book_depth, an empty book yields no opportunity."""
        cfg = MockConfig(require_book_depth=True, category="geopolitics")
        scanner = CompletenessScanner(config=cfg)
        market = MockMarketState(
            best_ask_yes=0.48, best_ask_no=0.48,
            asks_yes=[], asks_no=[],  # no fillable depth
        )
        assert scanner._evaluate_market(market) is None

    def test_real_depth_passes_when_required(self):
        """With require_book_depth, real book depth still detects the gap."""
        cfg = MockConfig(require_book_depth=True, category="geopolitics")
        scanner = CompletenessScanner(config=cfg)
        market = MockMarketState(
            best_ask_yes=0.48, best_ask_no=0.48,
            asks_yes=[MockPriceLevel(0.48, 100)],
            asks_no=[MockPriceLevel(0.48, 100)],
        )
        assert scanner._evaluate_market(market) is not None

    def test_one_sided_depth_rejected_when_required(self):
        """One leg with depth, the other empty → rejected (can't fill both)."""
        cfg = MockConfig(require_book_depth=True, category="geopolitics")
        scanner = CompletenessScanner(config=cfg)
        market = MockMarketState(
            best_ask_yes=0.48, best_ask_no=0.48,
            asks_yes=[MockPriceLevel(0.48, 100)],
            asks_no=[],
        )
        assert scanner._evaluate_market(market) is None


# ── Tests: Fill Verification & Unwind (live execution) ───────────────


import sys
import types
from unittest.mock import AsyncMock


@pytest.fixture
def fake_clob_sdk(monkeypatch):
    """Inject a fake py_clob_client_v2 so live-path lazy imports resolve."""
    fake = types.ModuleType("py_clob_client_v2")
    fake.OrderArgs = lambda **kw: kw
    ob = types.ModuleType("py_clob_client_v2.order_builder")
    const = types.ModuleType("py_clob_client_v2.order_builder.constants")
    const.BUY = "BUY"
    const.SELL = "SELL"
    clob_types = types.ModuleType("py_clob_client_v2.clob_types")
    clob_types.OrderPayload = lambda **kw: kw
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", fake)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.order_builder", ob)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.order_builder.constants", const)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", clob_types)
    return fake


def _live_scanner(market):
    """Scanner in live mode with a mocked CLOB client.

    Book: 0.45/0.45 with 50 shares depth each → trade.shares = 50.
    """
    config = MockConfig(mode="live", cooldown_s=0)
    scanner = CompletenessScanner(config, MockTracker([market]))
    scanner._client = MagicMock()
    scanner._client.create_order = MagicMock(return_value="signed")
    scanner._refresh_balance = AsyncMock(return_value=None)
    scanner._post_order_async = AsyncMock(
        side_effect=[{"orderID": "o-yes"}, {"orderID": "o-no"}]
    )
    scanner._cancel_partial_orders = AsyncMock()
    return scanner


def _arb_market(**kw):
    return MockMarketState(
        best_ask_yes=0.45, best_ask_no=0.45,
        asks_yes=[MockPriceLevel(0.45, 50)],
        asks_no=[MockPriceLevel(0.45, 50)],
        **kw,
    )


class TestFillVerification:
    @pytest.mark.asyncio
    async def test_poll_fills_returns_matched_sizes(self):
        scanner = CompletenessScanner(MockConfig(), MockTracker())
        scanner._client = MagicMock()
        scanner._get_order_async = AsyncMock(
            side_effect=[{"size_matched": "50"}, {"size_matched": "50"}]
        )
        matched = await scanner._poll_fills(["a", "b"], 50.0)
        assert matched == [50.0, 50.0]

    @pytest.mark.asyncio
    async def test_poll_fills_timeout_returns_partials(self, monkeypatch):
        monkeypatch.setattr("src.completeness_scanner.FILL_POLL_TIMEOUT_S", 0.01)
        scanner = CompletenessScanner(MockConfig(), MockTracker())
        scanner._client = MagicMock()
        scanner._get_order_async = AsyncMock(
            return_value={"size_matched": "10"}
        )
        matched = await scanner._poll_fills(["a", "b"], 50.0)
        assert matched == [10.0, 10.0]

    @pytest.mark.asyncio
    async def test_both_legs_filled_confirms_and_redeems(self, fake_clob_sdk):
        """Full fill on both legs → redeemed, profit = expected_profit."""
        market = _arb_market()
        scanner = _live_scanner(market)
        scanner._poll_fills = AsyncMock(return_value=[50.0, 50.0])
        scanner.set_redeem_callback(AsyncMock(return_value=True))

        opp = scanner._evaluate_market(market)
        assert opp is not None
        await scanner._execute_arb(opp)

        trade = scanner._trades[0]
        assert trade.status == "redeemed"
        assert trade.actual_pnl == pytest.approx(trade.expected_profit)
        assert scanner._total_profit == pytest.approx(trade.expected_profit)
        assert scanner._trades_executed == 1
        assert scanner._trades_failed == 0
        scanner._cancel_partial_orders.assert_not_called()

    @pytest.mark.asyncio
    async def test_one_leg_filled_unwinds_with_realized_loss(self, fake_clob_sdk):
        """Half-filled arb → cancel resting leg, sell stranded leg, book the loss."""
        market = _arb_market()
        scanner = _live_scanner(market)
        scanner._poll_fills = AsyncMock(return_value=[50.0, 0.0])
        scanner._unwind_leg = AsyncMock(return_value=0.40)  # sold at 0.40, bought 0.45

        opp = scanner._evaluate_market(market)
        await scanner._execute_arb(opp)

        trade = scanner._trades[0]
        assert trade.status == "unwound"
        # Realized: 50 shares × (0.40 - 0.45) = -2.50
        assert trade.actual_pnl == pytest.approx(-2.50)
        assert scanner._total_profit == pytest.approx(-2.50)
        assert scanner._trades_failed == 1
        assert scanner._trades_executed == 0
        assert scanner._legs_unwound == 1
        # Both orders were resting/cancellable (one never filled, one partially)
        scanner._cancel_partial_orders.assert_called_once()
        cancelled = scanner._cancel_partial_orders.call_args[0][0]
        assert "o-no" in cancelled

    @pytest.mark.asyncio
    async def test_no_fills_marks_failed_without_loss(self, fake_clob_sdk):
        """Nothing filled → cancel both, status failed, zero PnL."""
        market = _arb_market()
        scanner = _live_scanner(market)
        scanner._poll_fills = AsyncMock(return_value=[0.0, 0.0])
        scanner._unwind_leg = AsyncMock()

        opp = scanner._evaluate_market(market)
        await scanner._execute_arb(opp)

        trade = scanner._trades[0]
        assert trade.status == "failed"
        assert trade.actual_pnl == 0.0
        assert scanner._total_profit == 0.0
        assert scanner._trades_failed == 1
        scanner._unwind_leg.assert_not_called()
        scanner._cancel_partial_orders.assert_called_once()

    @pytest.mark.asyncio
    async def test_partial_pair_downsizes_and_unwinds_excess(self, fake_clob_sdk):
        """Legs fill asymmetrically → keep the matched pair, unwind the excess."""
        market = _arb_market()
        scanner = _live_scanner(market)
        scanner._poll_fills = AsyncMock(return_value=[30.0, 50.0])
        scanner._unwind_leg = AsyncMock(return_value=0.40)

        opp = scanner._evaluate_market(market)
        await scanner._execute_arb(opp)

        trade = scanner._trades[0]
        # Pair = 30 shares kept (confirmed, awaiting redeem — no callback set)
        assert trade.status == "confirmed"
        assert trade.shares == pytest.approx(30.0)
        assert trade.cost_total == pytest.approx(30 * 0.90)
        # Excess 20 shares of the NO leg sold: 20 × (0.40 - 0.45) = -1.0
        assert trade.actual_pnl == pytest.approx(-1.0)
        assert scanner._total_profit == pytest.approx(-1.0)
        assert scanner._trades_executed == 1
        assert scanner._legs_unwound == 1
        # Only the under-filled YES order was resting
        cancelled = scanner._cancel_partial_orders.call_args[0][0]
        assert cancelled == ["o-yes"]
        # Unwound the NO token's excess
        unwind_args = scanner._unwind_leg.call_args[0]
        assert unwind_args[0] == "no_token"
        assert unwind_args[1] == pytest.approx(20.0)

    @pytest.mark.asyncio
    async def test_unwind_leg_uses_tracker_bid(self, fake_clob_sdk):
        """Unwind sells at the live best bid from the tracker."""
        market = _arb_market(best_bid_yes=0.42)
        scanner = _live_scanner(market)
        sell_price = await scanner._unwind_leg("yes_token", 20.0, 0.45)
        assert sell_price == pytest.approx(0.42)

    @pytest.mark.asyncio
    async def test_unwind_leg_fallback_price_without_bid(self, fake_clob_sdk):
        """No bid known → sell at aggressive discount off our buy price."""
        market = _arb_market()  # bids default 0.0
        scanner = _live_scanner(market)
        sell_price = await scanner._unwind_leg("yes_token", 20.0, 0.45)
        assert sell_price == pytest.approx(0.40)

    @pytest.mark.asyncio
    async def test_unwind_failure_returns_zero(self, fake_clob_sdk):
        """Rejected unwind order → 0.0 (caller assumes full loss)."""
        market = _arb_market(best_bid_yes=0.42)
        scanner = _live_scanner(market)
        scanner._post_order_async = AsyncMock(return_value={"error": "rejected"})
        sell_price = await scanner._unwind_leg("yes_token", 20.0, 0.45)
        assert sell_price == 0.0
