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
            "atlanta", "los-angeles", "miami", "san-francisco",
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

    def test_parse_fahrenheit_no_conversion(self, scanner):
        """_parse_outcome_temp now returns original value without converting."""
        assert scanner._parse_outcome_temp("72°F") == 72.0

    def test_parse_below(self, scanner):
        assert scanner._parse_outcome_temp("17°C or below") == 17.0

    def test_parse_higher(self, scanner):
        assert scanner._parse_outcome_temp("27°C or higher") == 27.0

    def test_parse_negative(self, scanner):
        assert scanner._parse_outcome_temp("-5°C") == -5.0

    def test_parse_no_match(self, scanner):
        assert scanner._parse_outcome_temp("Unknown") is None

    def test_parse_range_fahrenheit(self, scanner):
        """Range outcomes like '66-67°F' return midpoint."""
        assert scanner._parse_outcome_temp("66-67°F") == 66.5

    def test_parse_range_celsius(self, scanner):
        assert scanner._parse_outcome_temp("20-21°C") == 20.5


# ── Label extraction ────────────────────────────────────────────────────

class TestLabelExtraction:
    def test_between_range(self, scanner):
        q = "Will the highest temperature in LA be between 66-67°F on May 6?"
        assert scanner._extract_outcome_label(q) == "66-67°F"

    def test_or_below(self, scanner):
        q = "Will the highest temperature in LA be 57°F or below on May 6?"
        assert scanner._extract_outcome_label(q) == "57°F or below"

    def test_or_higher(self, scanner):
        q = "Will the highest temperature in LA be 76°F or higher on May 6?"
        assert scanner._extract_outcome_label(q) == "76°F or higher"

    def test_single_temp(self, scanner):
        q = "Will the highest temperature in LA be 65°F on May 6?"
        assert scanner._extract_outcome_label(q) == "65°F"

    def test_no_match(self, scanner):
        q = "Will it rain tomorrow?"
        assert scanner._extract_outcome_label(q) is None


# ── Distribution building ────────────────────────────────────────────────

class TestBuildDistribution:
    def test_unanimous_agreement_celsius(self, scanner):
        """All members predict same temp → 100% on one bucket."""
        outcomes = ["17°C or below", "18°C", "19°C", "20°C", "21°C or higher"]
        max_temps = [19.2] * 51  # All predict ~19°C

        dist = scanner._build_distribution(max_temps, outcomes)
        assert dist["19°C"] == pytest.approx(1.0)
        assert dist["18°C"] == pytest.approx(0.0)

    def test_split_distribution_celsius(self, scanner):
        """Members split between two buckets."""
        outcomes = ["14°C or below", "15°C", "16°C", "17°C or higher"]
        max_temps = [15.1] * 30 + [16.2] * 21

        dist = scanner._build_distribution(max_temps, outcomes)
        assert dist["15°C"] == pytest.approx(30 / 51, abs=0.01)
        assert dist["16°C"] == pytest.approx(21 / 51, abs=0.01)

    def test_edge_buckets_celsius(self, scanner):
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

    def test_fahrenheit_range_distribution(self, scanner):
        """Fahrenheit range outcomes: temps in °C converted to °F for bucketing."""
        outcomes = ["57°F or below", "58-59°F", "60-61°F", "62°F or higher"]
        # All ensemble members predict 15.5°C = 59.9°F → should fall in "58-59°F"
        max_temps = [15.5] * 51  # 15.5°C = 59.9°F

        dist = scanner._build_distribution(max_temps, outcomes)
        assert dist["58-59°F"] == pytest.approx(1.0)

    def test_fahrenheit_edge_bucket(self, scanner):
        """Cold temps go to 'or below' bucket."""
        outcomes = ["57°F or below", "58-59°F", "60-61°F", "62°F or higher"]
        # 13°C = 55.4°F → "57°F or below"
        max_temps = [13.0] * 51

        dist = scanner._build_distribution(max_temps, outcomes)
        assert dist["57°F or below"] == pytest.approx(1.0)

    def test_fahrenheit_high_bucket(self, scanner):
        """Hot temps go to 'or higher' bucket."""
        outcomes = ["57°F or below", "58-59°F", "60-61°F", "62°F or higher"]
        # 20°C = 68°F → "62°F or higher"
        max_temps = [20.0] * 51

        dist = scanner._build_distribution(max_temps, outcomes)
        assert dist["62°F or higher"] == pytest.approx(1.0)


# ── Market evaluation ────────────────────────────────────────────────────

class TestEvaluateMarket:
    def _make_market(self, prices, outcomes=None):
        if outcomes is None:
            outcomes = ["14°C or below", "15°C", "16°C", "17°C", "18°C or higher"]
        n = len(outcomes)
        return WeatherMarket(
            event_slug="test",
            event_title="Highest temperature in Paris on May 5?",
            city_slug="paris",
            target_date=date.today() + timedelta(days=1),
            outcomes=outcomes,
            outcome_prices=prices,
            condition_ids=[f"0x{i:03d}" for i in range(n)],
            token_ids=[f"tok_{i}" for i in range(n)],
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
            "15°C": 0.60,
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
        assert opp is None

    def test_small_edge_filtered(self, scanner):
        """Edge below min_edge threshold → no opportunity."""
        market = self._make_market([0.05, 0.45, 0.30, 0.15, 0.05])
        forecast = self._make_forecast({
            "14°C or below": 0.05,
            "15°C": 0.50,
            "16°C": 0.30,
            "17°C": 0.10,
            "18°C or higher": 0.05,
        })

        opp = scanner._evaluate_market(market, forecast)
        assert opp is None

    def test_low_agreement_filtered(self, scanner):
        """Low model agreement → skip."""
        market = self._make_market([0.05, 0.10, 0.30, 0.30, 0.25])
        forecast = self._make_forecast({
            "14°C or below": 0.10,
            "15°C": 0.10,
            "16°C": 0.10,
            "17°C": 0.10,
            "18°C or higher": 0.10,
        })
        forecast.agreement = 0.10

        opp = scanner._evaluate_market(market, forecast)
        assert opp is None

    def test_max_price_filter(self, scanner):
        """Don't buy outcomes priced too high."""
        market = self._make_market([0.02, 0.03, 0.80, 0.10, 0.05])
        forecast = self._make_forecast({
            "14°C or below": 0.01,
            "15°C": 0.01,
            "16°C": 0.95,
            "17°C": 0.02,
            "18°C or higher": 0.01,
        })

        opp = scanner._evaluate_market(market, forecast)
        assert opp is None


# ── Event parsing ───────────────────────────────────────────────────────

class TestEventParsing:
    def _make_event(self, city, month_name, day, year, outcomes_questions):
        """Create a mock Gamma API event with nested binary markets."""
        slug = f"highest-temperature-in-{city}-on-{month_name.lower()}-{day}-{year}"
        markets = []
        for i, (question, yes_price) in enumerate(outcomes_questions):
            markets.append({
                "question": question,
                "conditionId": f"0x{i:04d}",
                "clobTokenIds": [f"tok_yes_{i}", f"tok_no_{i}"],
                "outcomePrices": [str(yes_price), str(round(1 - yes_price, 2))],
                "active": True,
                "closed": False,
                "volume": "1000",
                "liquidity": "500",
            })
        return {
            "slug": slug,
            "title": f"Highest temperature in {city.replace('-', ' ').title()} on {month_name} {day}?",
            "markets": markets,
        }

    def test_parse_event_standard(self, scanner):
        """Parse a standard temperature event with range outcomes."""
        tomorrow = date.today() + timedelta(days=1)
        month_name = tomorrow.strftime("%B")
        day = tomorrow.day

        event = self._make_event("paris", month_name, day, "2026", [
            (f"Will the highest temperature in Paris be 14°C or below on {month_name} {day}?", 0.05),
            (f"Will the highest temperature in Paris be between 15-16°C on {month_name} {day}?", 0.30),
            (f"Will the highest temperature in Paris be between 17-18°C on {month_name} {day}?", 0.40),
            (f"Will the highest temperature in Paris be 19°C or higher on {month_name} {day}?", 0.25),
        ])

        result = scanner._parse_temperature_event(event)
        assert result is not None
        assert result.city_slug == "paris"
        assert result.target_date == tomorrow
        assert len(result.outcomes) == 4
        assert len(result.condition_ids) == 4
        assert len(result.token_ids) == 4
        assert result.unit == "C"

    def test_parse_event_fahrenheit(self, scanner):
        """Parse a Fahrenheit temperature event."""
        tomorrow = date.today() + timedelta(days=1)
        month_name = tomorrow.strftime("%B")
        day = tomorrow.day

        event = self._make_event("atlanta", month_name, day, "2026", [
            (f"Will the highest temperature in Atlanta be 57°F or below on {month_name} {day}?", 0.05),
            (f"Will the highest temperature in Atlanta be between 58-59°F on {month_name} {day}?", 0.10),
            (f"Will the highest temperature in Atlanta be between 60-61°F on {month_name} {day}?", 0.30),
            (f"Will the highest temperature in Atlanta be 62°F or higher on {month_name} {day}?", 0.55),
        ])

        result = scanner._parse_temperature_event(event)
        assert result is not None
        assert result.city_slug == "atlanta"
        assert result.unit == "F"

    def test_parse_event_unknown_city(self, scanner):
        """Unknown city → None."""
        tomorrow = date.today() + timedelta(days=1)
        month_name = tomorrow.strftime("%B")

        event = self._make_event("timbuktu", month_name, tomorrow.day, "2026", [
            (f"Will the highest temperature in Timbuktu be 30°C on {month_name} {tomorrow.day}?", 0.33),
            (f"Will the highest temperature in Timbuktu be 31°C on {month_name} {tomorrow.day}?", 0.34),
            (f"Will the highest temperature in Timbuktu be 32°C on {month_name} {tomorrow.day}?", 0.33),
        ])

        result = scanner._parse_temperature_event(event)
        assert result is None

    def test_parse_event_too_few_markets(self, scanner):
        """Event with < 3 binary markets → None."""
        tomorrow = date.today() + timedelta(days=1)
        month_name = tomorrow.strftime("%B")

        event = self._make_event("paris", month_name, tomorrow.day, "2026", [
            (f"Will the highest temperature in Paris be 20°C on {month_name} {tomorrow.day}?", 0.50),
            (f"Will the highest temperature in Paris be 21°C on {month_name} {tomorrow.day}?", 0.50),
        ])

        result = scanner._parse_temperature_event(event)
        assert result is None

    def test_parse_event_past_date(self, scanner):
        """Past date → None."""
        yesterday = date.today() - timedelta(days=1)
        month_name = yesterday.strftime("%B")

        event = self._make_event("paris", month_name, yesterday.day, str(yesterday.year), [
            (f"Will the highest temperature in Paris be 18°C on {month_name} {yesterday.day}?", 0.33),
            (f"Will the highest temperature in Paris be 19°C on {month_name} {yesterday.day}?", 0.34),
            (f"Will the highest temperature in Paris be 20°C on {month_name} {yesterday.day}?", 0.33),
        ])

        result = scanner._parse_temperature_event(event)
        assert result is None


# ── Resolution ───────────────────────────────────────────────────────────

class TestResolution:
    def test_determine_winner_celsius_exact(self, scanner):
        outcomes = ["17°C or below", "18°C", "19°C", "20°C or higher"]
        assert scanner._determine_winner(18.3, outcomes) == "18°C"
        assert scanner._determine_winner(19.4, outcomes) == "19°C"

    def test_determine_winner_celsius_edge_low(self, scanner):
        outcomes = ["17°C or below", "18°C", "19°C", "20°C or higher"]
        assert scanner._determine_winner(16.0, outcomes) == "17°C or below"
        assert scanner._determine_winner(17.2, outcomes) == "17°C or below"

    def test_determine_winner_celsius_edge_high(self, scanner):
        outcomes = ["17°C or below", "18°C", "19°C", "20°C or higher"]
        assert scanner._determine_winner(20.1, outcomes) == "20°C or higher"
        assert scanner._determine_winner(25.0, outcomes) == "20°C or higher"

    def test_determine_winner_celsius_boundary(self, scanner):
        """Boundary: '17°C or below' covers <17.5, '18°C' covers [17.5, 18.5)."""
        outcomes = ["17°C or below", "18°C", "19°C"]
        assert scanner._determine_winner(17.4, outcomes) == "17°C or below"
        assert scanner._determine_winner(17.5, outcomes) == "18°C"
        assert scanner._determine_winner(18.49, outcomes) == "18°C"
        assert scanner._determine_winner(18.5, outcomes) == "19°C"

    def test_determine_winner_fahrenheit_range(self, scanner):
        """Fahrenheit range outcomes. Input is actual_temp in °C."""
        outcomes = ["57°F or below", "58-59°F", "60-61°F", "62°F or higher"]
        # 15°C = 59°F → "58-59°F" (range [58, 60))
        assert scanner._determine_winner(15.0, outcomes) == "58-59°F"
        # 13°C = 55.4°F → "57°F or below"
        assert scanner._determine_winner(13.0, outcomes) == "57°F or below"
        # 20°C = 68°F → "62°F or higher"
        assert scanner._determine_winner(20.0, outcomes) == "62°F or higher"


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
        assert scanner._extract_fee_rate(mkt) == 0.05


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
