"""Tests for weather prediction scanner."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date, timedelta
from dataclasses import dataclass

from src.weather_scanner import (
    WeatherScanner,
    WeatherMarket,
    ForecastDistribution,
    CITY_COORDS,
)


@dataclass
class MockWeatherConfig:
    mode: str = "paper"
    scan_interval: float = 900.0
    forecast_cache_ttl: float = 3600.0
    max_forecast_days: int = 3
    min_edge: float = 0.08
    min_forecast_prob: float = 0.15
    min_agreement: float = 0.20
    max_price: float = 0.75
    max_bet_per_trade: float = 10.0
    bankroll: float = 200.0
    kelly_multiplier: float = 0.25
    max_bets_per_cycle: int = 3
    resolution_check_interval: float = 3600.0


@pytest.fixture
def config():
    return MockWeatherConfig()


@pytest.fixture
def scanner(config):
    return WeatherScanner(config=config)


# ── City coordinates ─────────────────────────────────────────────────────

class TestCityCoords:
    def test_major_cities_present(self):
        """All major weather market cities should be in CITY_COORDS."""
        expected = [
            "hong-kong", "taipei", "seoul", "tokyo", "paris",
            "london", "new-york", "chicago", "singapore", "shanghai",
        ]
        for city in expected:
            assert city in CITY_COORDS, f"{city} missing from CITY_COORDS"

    def test_coords_valid(self):
        """Coordinates should be within valid ranges."""
        for city, (lat, lon, tz) in CITY_COORDS.items():
            assert -90 <= lat <= 90, f"{city} latitude {lat} out of range"
            assert -180 <= lon <= 180, f"{city} longitude {lon} out of range"
            assert "/" in tz, f"{city} timezone {tz} doesn't look like IANA"


# ── Outcome parsing ──────────────────────────────────────────────────────

class TestOutcomeParsing:
    def test_parse_celsius(self, scanner):
        assert scanner._parse_outcome_temp("18°C") == 18.0

    def test_parse_celsius_with_space(self, scanner):
        assert scanner._parse_outcome_temp("18 °C") == 18.0

    def test_parse_fahrenheit(self, scanner):
        # 72°F = 22.22°C
        result = scanner._parse_outcome_temp("72°F")
        assert abs(result - 22.22) < 0.1

    def test_parse_below(self, scanner):
        assert scanner._parse_outcome_temp("17°C or below") == 17.0

    def test_parse_higher(self, scanner):
        assert scanner._parse_outcome_temp("27°C or higher") == 27.0

    def test_parse_negative(self, scanner):
        assert scanner._parse_outcome_temp("-5°C") == -5.0

    def test_parse_no_match(self, scanner):
        assert scanner._parse_outcome_temp("Unknown") is None


# ── Distribution building ────────────────────────────────────────────────

class TestBuildDistribution:
    def test_unanimous_agreement(self, scanner):
        """All members predict same temp → 100% on one bucket."""
        outcomes = ["17°C or below", "18°C", "19°C", "20°C", "21°C or higher"]
        max_temps = [19.2] * 51  # All predict ~19°C

        dist = scanner._build_distribution(max_temps, outcomes)
        assert dist["19°C"] == pytest.approx(1.0)
        assert dist["18°C"] == pytest.approx(0.0)

    def test_split_distribution(self, scanner):
        """Members split between two buckets."""
        outcomes = ["14°C or below", "15°C", "16°C", "17°C or higher"]
        # 30 members predict 15, 21 predict 16
        max_temps = [15.1] * 30 + [16.2] * 21

        dist = scanner._build_distribution(max_temps, outcomes)
        assert dist["15°C"] == pytest.approx(30 / 51, abs=0.01)
        assert dist["16°C"] == pytest.approx(21 / 51, abs=0.01)

    def test_edge_buckets(self, scanner):
        """Extreme temperatures go to edge buckets."""
        outcomes = ["17°C or below", "18°C", "19°C", "20°C or higher"]
        max_temps = [15.0] * 10 + [19.0] * 20 + [22.0] * 21

        dist = scanner._build_distribution(max_temps, outcomes)
        assert dist["17°C or below"] == pytest.approx(10 / 51, abs=0.01)
        assert dist["20°C or higher"] == pytest.approx(21 / 51, abs=0.01)

    def test_empty_members(self, scanner):
        """No members → empty distribution."""
        outcomes = ["18°C", "19°C"]
        dist = scanner._build_distribution([], outcomes)
        assert dist == {}


# ── Market evaluation ────────────────────────────────────────────────────

class TestEvaluateMarket:
    def _make_market(self, prices, outcomes=None):
        if outcomes is None:
            outcomes = ["14°C or below", "15°C", "16°C", "17°C", "18°C or higher"]
        return WeatherMarket(
            event_slug="test",
            condition_id="0x123",
            question="Highest temperature in Paris on May 5?",
            city_slug="paris",
            target_date=date.today() + timedelta(days=1),
            outcomes=outcomes,
            outcome_prices=prices,
            token_ids=[f"tok_{i}" for i in range(len(outcomes))],
            fee_rate=0.05,
        )

    def _make_forecast(self, buckets):
        return ForecastDistribution(
            city_slug="paris",
            target_date=date.today() + timedelta(days=1),
            buckets=buckets,
            ensemble_max_temps=[15.0] * 51,
            agreement=max(buckets.values()) if buckets else 0,
            fetched_at=1000000.0,
        )

    def test_clear_edge_detected(self, scanner):
        """Large edge (forecast 60% vs market 40%) should trigger."""
        market = self._make_market([0.05, 0.40, 0.30, 0.15, 0.10])
        forecast = self._make_forecast({
            "14°C or below": 0.02,
            "15°C": 0.60,  # Forecast says 60%, market says 40% → 20% edge
            "16°C": 0.25,
            "17°C": 0.08,
            "18°C or higher": 0.05,
        })

        opp = scanner._evaluate_market(market, forecast)
        assert opp is not None
        assert opp.best_outcome_label == "15°C"
        assert opp.edge == pytest.approx(0.20, abs=0.01)
        assert opp.forecast_prob == pytest.approx(0.60, abs=0.01)

    def test_no_edge(self, scanner):
        """Market prices match forecast → no opportunity."""
        market = self._make_market([0.05, 0.50, 0.30, 0.10, 0.05])
        forecast = self._make_forecast({
            "14°C or below": 0.05,
            "15°C": 0.50,
            "16°C": 0.30,
            "17°C": 0.10,
            "18°C or higher": 0.05,
        })

        opp = scanner._evaluate_market(market, forecast)
        assert opp is None  # No edge above min_edge (0.08)

    def test_small_edge_filtered(self, scanner):
        """Edge below min_edge threshold → no opportunity."""
        market = self._make_market([0.05, 0.45, 0.30, 0.15, 0.05])
        forecast = self._make_forecast({
            "14°C or below": 0.05,
            "15°C": 0.50,  # 5% edge — below min_edge=8%
            "16°C": 0.30,
            "17°C": 0.10,
            "18°C or higher": 0.05,
        })

        opp = scanner._evaluate_market(market, forecast)
        assert opp is None

    def test_low_agreement_filtered(self, scanner):
        """Low model agreement → skip (too uncertain)."""
        market = self._make_market([0.05, 0.10, 0.30, 0.30, 0.25])
        forecast = self._make_forecast({
            "14°C or below": 0.10,
            "15°C": 0.10,
            "16°C": 0.10,
            "17°C": 0.10,
            "18°C or higher": 0.10,
        })
        # agreement = max(probs) = 0.10 < min_agreement=0.20
        forecast.agreement = 0.10

        opp = scanner._evaluate_market(market, forecast)
        assert opp is None

    def test_max_price_filter(self, scanner):
        """Don't buy outcomes priced too high."""
        market = self._make_market([0.02, 0.03, 0.80, 0.10, 0.05])
        forecast = self._make_forecast({
            "14°C or below": 0.01,
            "15°C": 0.01,
            "16°C": 0.95,  # Huge edge, but market_price=0.80 > max_price=0.75
            "17°C": 0.02,
            "18°C or higher": 0.01,
        })

        opp = scanner._evaluate_market(market, forecast)
        assert opp is None


# ── Slug/question parsing ────────────────────────────────────────────────

class TestMarketParsing:
    def test_parse_slug_standard(self, scanner):
        """Standard slug format parsing."""
        mkt = {
            "slug": "highest-temperature-in-paris-on-may-5-2026",
            "question": "Highest temperature in Paris on May 5?",
            "conditionId": "0xabc",
            "outcomes": ["14°C or below", "15°C", "16°C", "17°C", "18°C or higher"],
            "outcomePrices": ["0.05", "0.51", "0.37", "0.05", "0.02"],
            "clobTokenIds": ["t1", "t2", "t3", "t4", "t5"],
            "active": True,
            "closed": False,
            "volume": "84000",
            "liquidity": "5000",
        }
        event = {"slug": "highest-temperature-in-paris-on-may-5-2026"}

        result = scanner._parse_temperature_market(mkt, event)
        # May be None if date is in the past, but test the parsing logic
        if result:
            assert result.city_slug == "paris"
            assert result.target_date.month == 5
            assert result.target_date.day == 5

    def test_parse_question_fallback(self, scanner):
        """Parse from question when slug doesn't match pattern."""
        tomorrow = date.today() + timedelta(days=1)
        month_name = tomorrow.strftime("%B")
        day = tomorrow.day

        mkt = {
            "slug": "some-random-slug",
            "question": f"Highest temperature in Seoul on {month_name} {day}?",
            "conditionId": "0xdef",
            "outcomes": ["20°C or below", "21°C", "22°C", "23°C", "24°C or higher"],
            "outcomePrices": ["0.10", "0.20", "0.40", "0.20", "0.10"],
            "clobTokenIds": ["t1", "t2", "t3", "t4", "t5"],
            "active": True,
            "closed": False,
        }
        event = {"slug": ""}

        result = scanner._parse_temperature_market(mkt, event)
        assert result is not None
        assert result.city_slug == "seoul"
        assert result.target_date == tomorrow

    def test_unknown_city_returns_none(self, scanner):
        """Unknown city → None."""
        tomorrow = date.today() + timedelta(days=1)
        month_name = tomorrow.strftime("%B")
        day = tomorrow.day

        mkt = {
            "slug": f"highest-temperature-in-timbuktu-on-{month_name.lower()}-{day}-2026",
            "question": f"Highest temperature in Timbuktu on {month_name} {day}?",
            "conditionId": "0xghi",
            "outcomes": ["30°C", "31°C", "32°C"],
            "outcomePrices": ["0.33", "0.34", "0.33"],
            "clobTokenIds": ["t1", "t2", "t3"],
            "active": True,
            "closed": False,
        }
        event = {"slug": ""}

        result = scanner._parse_temperature_market(mkt, event)
        assert result is None

    def test_binary_market_rejected(self, scanner):
        """Binary Yes/No markets → None (need multi-outcome)."""
        tomorrow = date.today() + timedelta(days=1)
        month_name = tomorrow.strftime("%B")

        mkt = {
            "slug": f"high-temperature-in-chicago-above-60f-{month_name.lower()}-{tomorrow.day}",
            "question": f"High temperature in Chicago above 60°F on {month_name} {tomorrow.day}?",
            "conditionId": "0xjkl",
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.70", "0.30"],
            "clobTokenIds": ["t1", "t2"],
            "active": True,
            "closed": False,
        }
        event = {"slug": ""}

        result = scanner._parse_temperature_market(mkt, event)
        assert result is None  # < 3 outcomes


# ── Resolution ───────────────────────────────────────────────────────────

class TestResolution:
    def test_determine_winner_exact(self, scanner):
        outcomes = ["17°C or below", "18°C", "19°C", "20°C or higher"]
        assert scanner._determine_winner(18.3, outcomes) == "18°C"
        assert scanner._determine_winner(19.4, outcomes) == "19°C"

    def test_determine_winner_edge_low(self, scanner):
        outcomes = ["17°C or below", "18°C", "19°C", "20°C or higher"]
        assert scanner._determine_winner(16.0, outcomes) == "17°C or below"
        assert scanner._determine_winner(17.2, outcomes) == "17°C or below"

    def test_determine_winner_edge_high(self, scanner):
        outcomes = ["17°C or below", "18°C", "19°C", "20°C or higher"]
        assert scanner._determine_winner(20.1, outcomes) == "20°C or higher"
        assert scanner._determine_winner(25.0, outcomes) == "20°C or higher"

    def test_determine_winner_boundary(self, scanner):
        """Boundary: "17°C or below" covers <17.5, "18°C" covers [17.5, 18.5)."""
        outcomes = ["17°C or below", "18°C", "19°C"]
        # 17.4 falls in "17°C or below" (< 17.5)
        assert scanner._determine_winner(17.4, outcomes) == "17°C or below"
        # 17.5 falls in "18°C" bucket [17.5, 18.5)
        assert scanner._determine_winner(17.5, outcomes) == "18°C"
        assert scanner._determine_winner(18.49, outcomes) == "18°C"
        assert scanner._determine_winner(18.5, outcomes) == "19°C"


# ── Date parsing ─────────────────────────────────────────────────────────

class TestDateParsing:
    def test_full_month_name(self, scanner):
        result = scanner._parse_market_date("May", "5", "2026")
        assert result == date(2026, 5, 5)

    def test_abbreviated_month(self, scanner):
        result = scanner._parse_market_date("Jan", "15", "2026")
        assert result == date(2026, 1, 15)

    def test_no_year_defaults_current(self, scanner):
        result = scanner._parse_market_date("December", "25", None)
        assert result.year == date.today().year
        assert result.month == 12
        assert result.day == 25

    def test_invalid_month(self, scanner):
        result = scanner._parse_market_date("Foo", "1", "2026")
        assert result is None


# ── Fee extraction ───────────────────────────────────────────────────────

class TestFeeExtraction:
    def test_from_fee_schedule(self, scanner):
        mkt = {"feeSchedule": {"rate": 0.05, "rebateRate": 0.25}}
        assert scanner._extract_fee_rate(mkt) == 0.05

    def test_fallback_default(self, scanner):
        mkt = {}
        assert scanner._extract_fee_rate(mkt) == 0.05  # weather_fees default


# ── Stats ────────────────────────────────────────────────────────────────

class TestStats:
    def test_initial_stats(self, scanner):
        stats = scanner.get_stats()
        assert stats["running"] is False
        assert stats["total_scans"] == 0
        assert stats["markets_found"] == 0
        assert stats["opportunities_found"] == 0
        assert stats["total_pnl"] == 0.0
        assert stats["win_rate"] == 0.0
