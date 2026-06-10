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
    STATION_COORDS,
    CITY_STATION,
)


@dataclass
class MockWeatherConfig:
    mode: str = "paper"
    scan_interval: float = 900.0
    forecast_cache_ttl: float = 3600.0
    max_forecast_days: int = 3
    min_edge: float = 0.08
    min_forecast_prob: float = 0.15
    min_price: float = 0.10
    min_agreement: float = 0.20
    max_price: float = 0.75
    forecast_uncertainty_c: float = 0.01  # ~0 dressing: bucketing tests assert hard assignment
    max_bet_per_trade: float = 10.0
    bankroll: float = 200.0
    kelly_multiplier: float = 0.25
    max_bets_per_cycle: int = 3
    resolution_check_interval: float = 3600.0
    use_metar_resolution: bool = True
    bias_correction: bool = True
    bias_min_samples: int = 10
    bias_max_correction_c: float = 3.0


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

    def test_no_edge_bucket_no_false_assignment(self, scanner):
        """Temps outside all buckets when no edge bucket → prob stays 0, not falsely 100%."""
        # Buckets only cover 26-28°C, NO "or higher"/"or below"
        outcomes = ["26°C", "27°C", "28°C"]
        # All members predict ~30°C, outside all buckets
        max_temps = [30.0] * 50

        dist = scanner._build_distribution(max_temps, outcomes)
        # None should be assigned to "28°C" (the bug was assigning all 50 here)
        assert dist["28°C"] == pytest.approx(0.0)
        assert dist["27°C"] == pytest.approx(0.0)
        assert dist["26°C"] == pytest.approx(0.0)

    def test_partial_edge_only_low(self, scanner):
        """Only 'or below' edge bucket, temps above all → unmatched, not assigned to last."""
        outcomes = ["17°C or below", "18°C", "19°C"]
        max_temps = [22.0] * 50  # All above 19.5°C

        dist = scanner._build_distribution(max_temps, outcomes)
        # No "or higher" bucket, so 22°C should NOT be assigned to "19°C"
        assert dist["19°C"] == pytest.approx(0.0)

    def test_or_above_recognized(self, scanner):
        """'or above' should be treated same as 'or higher'."""
        outcomes = ["17°C or below", "18°C", "19°C", "20°C or above"]
        max_temps = [22.0] * 50

        dist = scanner._build_distribution(max_temps, outcomes)
        assert dist["20°C or above"] == pytest.approx(1.0)


class TestStationCoords:
    """Airport-station resolution: forecast/resolve at the station Polymarket
    settles against (ICAO from resolutionSource), not the city center."""

    def test_extract_icao_us_url(self, scanner):
        url = "https://www.wunderground.com/history/daily/us/tx/dallas/KDAL"
        assert scanner._extract_station_icao(url) == "KDAL"

    def test_extract_icao_intl_url(self, scanner):
        url = "https://www.wunderground.com/history/daily/cn/jinan/ZSJN"
        assert scanner._extract_station_icao(url) == "ZSJN"

    def test_extract_icao_empty(self, scanner):
        assert scanner._extract_station_icao("") == ""
        assert scanner._extract_station_icao("https://example.com/no/code") == ""

    def test_station_coords_uses_airport(self, scanner):
        """An explicit ICAO returns the airport coords, not the city center."""
        lat, lon, tz = scanner._station_coords("denver", "KBKF")
        assert (round(lat, 3), round(lon, 3)) == STATION_COORDS["KBKF"]
        # Must differ from the city-center coords
        assert (lat, lon) != CITY_COORDS["denver"][:2]
        assert tz == CITY_COORDS["denver"][2]  # timezone still from the city

    def test_station_coords_fallback_to_city_station(self, scanner):
        """No ICAO given → use the mapped station for the city."""
        lat, lon, tz = scanner._station_coords("london")
        assert (round(lat, 3), round(lon, 3)) == STATION_COORDS[CITY_STATION["london"]]

    def test_station_coords_unmapped_icao_falls_back_to_center(self, scanner):
        """Unknown ICAO but known city → city-center coords (graceful)."""
        lat, lon, tz = scanner._station_coords("madrid", "XXXX")
        assert (lat, lon) == CITY_COORDS["madrid"][:2]

    def test_station_coords_unknown_city(self, scanner):
        assert scanner._station_coords("atlantis", "") is None

    def test_every_city_station_has_coords(self):
        """Integrity: each CITY_STATION ICAO must be in STATION_COORDS."""
        for city, icao in CITY_STATION.items():
            assert icao in STATION_COORDS, f"{city} → {icao} missing coords"


class TestStatsReport:
    """``get_stats()`` must split pending vs resolved trades and surface the
    forecast_prob discriminator so manual review can spot a sobre-confident
    model (high-prob bets lose disproportionately) vs a healthy one."""

    def _mk(self, scanner, *, status, prob, pnl=0.0, created=1.0, resolved=0.0):
        from src.weather_scanner import WeatherTrade
        t = WeatherTrade(
            trade_id=f"t{len(scanner._trades)}",
            condition_id=f"c{len(scanner._trades)}",
            question="", city="madrid", outcome="20°C",
            shares=10, price=0.3, cost=3.0,
            forecast_prob=prob, edge=0.2,
            status=status, pnl=pnl,
            created_at=created, resolved_at=resolved,
        )
        scanner._trades.append(t)
        return t

    def test_open_and_closed_split(self, scanner):
        scanner._trades.clear()
        self._mk(scanner, status="pending", prob=0.30)
        self._mk(scanner, status="confirmed", prob=0.35)
        self._mk(scanner, status="won", prob=0.50, pnl=10, resolved=200)
        self._mk(scanner, status="lost", prob=0.20, pnl=-3, resolved=300)
        self._mk(scanner, status="expired", prob=0.25, resolved=100)

        stats = scanner.get_stats()
        open_ids = {t["trade_id"] for t in stats["recent_trades"]}
        closed_ids = {t["trade_id"] for t in stats["resolved_trades"]}
        assert len(open_ids) == 2 and len(closed_ids) == 3
        assert open_ids.isdisjoint(closed_ids)
        # forecast_prob must be present on both
        assert all("forecast_prob" in t for t in stats["recent_trades"])
        assert all("forecast_prob" in t for t in stats["resolved_trades"])
        # resolved entries carry resolved_at
        for t in stats["resolved_trades"]:
            if t["status"] in ("won", "lost", "expired"):
                # Built with non-zero resolved → must be exposed
                src = next(s for s in scanner._trades if s.trade_id == t["trade_id"])
                if src.resolved_at:
                    assert t["resolved_at"] == src.resolved_at

    def test_resolved_sorted_newest_first(self, scanner):
        scanner._trades.clear()
        self._mk(scanner, status="lost", prob=0.2, resolved=100)
        self._mk(scanner, status="won", prob=0.5, resolved=300)
        self._mk(scanner, status="lost", prob=0.3, resolved=200)
        stats = scanner.get_stats()
        ts = [t["resolved_at"] for t in stats["resolved_trades"]]
        assert ts == sorted(ts, reverse=True)

    def test_discriminator_buckets_by_forecast_prob(self, scanner):
        """A healthy model wins more on high-prob bets than on low-prob ones."""
        scanner._trades.clear()
        # low (<0.25): 1W 2L → 33%
        self._mk(scanner, status="won", prob=0.20, resolved=1)
        self._mk(scanner, status="lost", prob=0.20, resolved=2)
        self._mk(scanner, status="lost", prob=0.10, resolved=3)
        # mid (0.25-0.40): 2W 1L → 67%
        self._mk(scanner, status="won", prob=0.30, resolved=4)
        self._mk(scanner, status="won", prob=0.35, resolved=5)
        self._mk(scanner, status="lost", prob=0.28, resolved=6)
        # high (>=0.40): 3W 0L → 100%
        self._mk(scanner, status="won", prob=0.50, resolved=7)
        self._mk(scanner, status="won", prob=0.60, resolved=8)
        self._mk(scanner, status="won", prob=0.45, resolved=9)
        # expired must NOT count
        self._mk(scanner, status="expired", prob=0.40, resolved=10)

        d = scanner.get_stats()["discriminator_by_forecast_prob"]
        assert d["low_<0.25"] == {"wins": 1, "losses": 2, "n": 3, "win_rate": 0.333}
        assert d["mid_0.25-0.40"] == {"wins": 2, "losses": 1, "n": 3, "win_rate": 0.667}
        assert d["high_>=0.40"] == {"wins": 3, "losses": 0, "n": 3, "win_rate": 1.0}


class TestFillDetection:
    """Nivel A — Detección robusta de fills en post_order V2.

    El bug original: leíamos solo `orderID` y perdíamos ~$45 en fills que
    llegaban con orderID="" pero success=True o tradeIDs poblados.
    """

    def test_classic_resting_order(self, scanner):
        r = {"success": True, "orderID": "0xabc", "tradeIDs": [], "status": "live", "errorMsg": ""}
        filled, oid, tids, ok, _, _ = scanner._parse_post_order_response(r)
        assert filled is True and oid == "0xabc"

    def test_fak_matched_no_resting_order(self, scanner):
        """FAK matched al instante: sin orderID pero CON tradeIDs → es un fill."""
        r = {"success": True, "orderID": "", "tradeIDs": ["t1", "t2"], "status": "matched"}
        filled, oid, tids, *_ = scanner._parse_post_order_response(r)
        assert filled is True and oid == "" and len(tids) == 2

    def test_success_true_without_ids(self, scanner):
        """Algunas respuestas V2 marcan success=True sin IDs poblados todavía."""
        r = {"success": True, "orderID": "", "tradeIDs": [], "status": "delayed"}
        filled, *_ = scanner._parse_post_order_response(r)
        assert filled is True

    def test_rejected_order(self, scanner):
        r = {"success": False, "orderID": "", "tradeIDs": [], "errorMsg": "insufficient balance"}
        filled, _, _, ok, err, _ = scanner._parse_post_order_response(r)
        assert filled is False and ok is False and "insufficient" in err

    def test_non_dict_response(self, scanner):
        """post_order ocasionalmente puede devolver None u otro tipo."""
        assert scanner._parse_post_order_response(None)[0] is False
        assert scanner._parse_post_order_response("oops")[0] is False


class TestOrphanReconciliation:
    """Nivel B — El sweep periódico rescata fills que el bot marcó failed."""

    def test_synthetic_orphan_built_from_fill(self, scanner):
        """Un fill de la Data API sin trade local se materializa con sus campos."""
        from src.weather_scanner import WeatherMarket
        from datetime import date

        market = WeatherMarket(
            event_slug="highest-temperature-in-madrid-on-may-28-2026",
            event_title="Highest temperature in Madrid on May 28?",
            city_slug="madrid",
            target_date=date(2026, 5, 28),
            outcomes=["33°C", "34°C", "35°C"],
            outcome_prices=[0.2, 0.5, 0.2],
            condition_ids=["cidA", "cidB", "cidC"],
            token_ids=["tA", "tB", "tC"],
            station_icao="LEMD",
        )
        scanner._markets = [market]
        before = len(scanner._trades)
        scanner._record_synthetic_orphan({
            "conditionId": "cidB",
            "price": 0.42,
            "size": 10,
            "timestamp": 1748000000,
            "transactionHash": "0xdead",
        })
        assert len(scanner._trades) == before + 1
        t = scanner._trades[-1]
        assert t.condition_id == "cidB"
        assert t.outcome == "34°C"
        assert t.city == "madrid"
        assert t.station_icao == "LEMD"
        assert t.status == "confirmed"
        assert t.cost == pytest.approx(4.20, abs=0.01)
        assert t.trade_id.startswith("wx_orphan_")


class TestKernelDressing:
    """Validate that calibration σ widens an underdispersive ensemble.

    Without dressing, a tight ensemble (the real case at +1 day, std ~0.25°C)
    puts ~85% in one 1°C bucket → fake 60-90% edges. Dressing spreads the mass.
    """

    @pytest.fixture
    def dressed_scanner(self):
        cfg = MockWeatherConfig()
        cfg.forecast_uncertainty_c = 2.0  # realistic calibration σ
        return WeatherScanner(config=cfg)

    def test_tight_ensemble_is_spread_out(self, dressed_scanner):
        """A near-unanimous ensemble must NOT yield ~100% on one 1°C bucket."""
        outcomes = ["30°C or below", "31°C", "32°C", "33°C", "34°C or higher"]
        max_temps = [32.1] * 50  # extremely tight, like Madrid/Shenzhen at +1d

        dist = dressed_scanner._build_distribution(max_temps, outcomes)
        # The peak bucket should be well below the undressed ~1.0
        assert dist["32°C"] < 0.45
        # Neighbors must carry meaningful mass (uncertainty is real)
        assert dist["31°C"] > 0.10
        assert dist["33°C"] > 0.10

    def test_dressing_collapses_fake_edge(self, dressed_scanner):
        """The Miami case: ensemble ~83°F, narrow bucket priced cheap.

        Undressed gave forecast_prob ~0.78 → edge 0.74. Dressed must be far
        lower so it no longer clears a realistic min_edge.
        """
        outcomes = ["81-82°F", "83-84°F", "85-86°F", "87°F or higher"]
        max_temps = [28.3] * 50  # 28.3°C ≈ 82.9°F → peak near "83-84°F"

        dist = dressed_scanner._build_distribution(max_temps, outcomes)
        assert dist["83-84°F"] < 0.45  # was ~0.78 with hard assignment

    def test_mass_mostly_conserved_with_open_edges(self, dressed_scanner):
        """With open-ended edge buckets covering the tails, mass stays ~1.

        A small leak (<10%) comes from the half-degree gap between single-degree
        buckets ([32.5,33.5) for "33°C") and the "X or higher" boundary (34) —
        a pre-existing contiguity quirk, harmless and conservative here.
        """
        outcomes = ["30°C or below", "31°C", "32°C", "33°C", "34°C or higher"]
        max_temps = [32.0] * 50

        dist = dressed_scanner._build_distribution(max_temps, outcomes)
        assert 0.90 < sum(dist.values()) <= 1.0


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

class TestDeduplication:
    def test_pending_trade_blocks_duplicate(self, scanner):
        """A pending trade on a condition_id should prevent re-betting."""
        from src.weather_scanner import WeatherTrade
        # Simulate a pending trade
        scanner._trades.append(WeatherTrade(
            trade_id="wx_test1",
            condition_id="0x001",
            question="Will temp be 18°C?",
            city="paris",
            outcome="18°C",
            shares=100,
            price=0.10,
            cost=10.0,
            forecast_prob=0.90,
            edge=0.80,
            status="pending",
        ))
        # Build pending set (same logic as _run_scan_cycle)
        pending_cids = {t.condition_id for t in scanner._trades if t.status == "pending"}
        assert "0x001" in pending_cids

    def test_resolved_trade_allows_new_bet(self, scanner):
        """A resolved (won/lost) trade should NOT block new bets."""
        from src.weather_scanner import WeatherTrade
        scanner._trades.append(WeatherTrade(
            trade_id="wx_test2",
            condition_id="0x002",
            question="Will temp be 19°C?",
            city="paris",
            outcome="19°C",
            shares=100,
            price=0.10,
            cost=10.0,
            forecast_prob=0.90,
            edge=0.80,
            status="won",
        ))
        pending_cids = {t.condition_id for t in scanner._trades if t.status == "pending"}
        assert "0x002" not in pending_cids


class TestStats:
    def test_initial_stats(self, scanner):
        stats = scanner.get_stats()
        assert stats["running"] is False
        assert stats["total_scans"] == 0
        assert stats["markets_found"] == 0
        assert stats["opportunities_found"] == 0
        assert stats["total_pnl"] == 0.0
        assert stats["win_rate"] == 0.0


# ── Selection & price floor (_evaluate_market) ───────────────────────────


def _mk_market(outcomes, prices, agreement_ok=True, **kw):
    return WeatherMarket(
        event_slug="ev", event_title="Highest temp?", city_slug="dallas",
        target_date=date(2026, 6, 6), outcomes=outcomes, outcome_prices=prices,
        condition_ids=["c"] * len(outcomes), token_ids=["t"] * len(outcomes),
        **kw,
    )


def _mk_forecast(buckets, agreement=0.5):
    return ForecastDistribution(
        city_slug="dallas", target_date=date(2026, 6, 6),
        buckets=buckets, ensemble_max_temps=[], agreement=agreement,
    )


class TestSelectionAndFloor:
    def test_selects_conviction_over_nominal_edge(self, scanner):
        """Picks the highest-prob underpriced outcome, not the largest edge.

        'tail' has a bigger nominal edge (0.25) but only 40% conviction;
        'conviction' has a smaller edge (0.15) but 65% prob. New logic must
        pick 'conviction' (the old max-edge logic would pick 'tail').
        """
        market = _mk_market(["tail", "conviction"], [0.15, 0.50])
        forecast = _mk_forecast({"tail": 0.40, "conviction": 0.65})
        opp = scanner._evaluate_market(market, forecast)
        assert opp is not None
        assert opp.best_outcome_label == "conviction"
        assert opp.forecast_prob == pytest.approx(0.65)
        assert opp.market_price == pytest.approx(0.50)

    def test_price_floor_rejects_cheap_longshot(self, scanner):
        """A cheap long-shot below min_price is rejected even with huge edge."""
        market = _mk_market(["cheap"], [0.05])
        forecast = _mk_forecast({"cheap": 0.30})  # edge 0.25, but price < 0.10
        assert scanner._evaluate_market(market, forecast) is None

    def test_outcome_just_above_floor_passes(self, scanner):
        """An outcome exactly at the floor still qualifies."""
        market = _mk_market(["ok"], [0.10])
        forecast = _mk_forecast({"ok": 0.30})  # edge 0.20
        opp = scanner._evaluate_market(market, forecast)
        assert opp is not None
        assert opp.best_outcome_label == "ok"

    def test_no_candidate_when_edge_too_small(self, scanner):
        market = _mk_market(["a"], [0.50])
        forecast = _mk_forecast({"a": 0.52})  # edge 0.02 < min_edge 0.08
        assert scanner._evaluate_market(market, forecast) is None

    def test_low_agreement_rejected(self, scanner):
        market = _mk_market(["ok"], [0.30])
        forecast = _mk_forecast({"ok": 0.60}, agreement=0.10)  # < min_agreement 0.20
        assert scanner._evaluate_market(market, forecast) is None


# ── METAR resolution helpers (#3) ────────────────────────────────────────


class TestMetarResolution:
    def test_iem_station_id_us_strips_k(self):
        assert WeatherScanner._iem_station_id("KDAL") == "DAL"

    def test_iem_station_id_intl_unchanged(self):
        assert WeatherScanner._iem_station_id("EGLC") == "EGLC"

    def test_iem_station_id_blank(self):
        assert WeatherScanner._iem_station_id("") == ""

    def test_parse_iem_csv_returns_max_in_celsius(self):
        text = (
            "station,valid,tmpf\n"
            "DAL,2026-06-06 12:00,80.0\n"
            "DAL,2026-06-06 15:00,95.0\n"
            "DAL,2026-06-06 18:00,88.0\n"
        )
        c = WeatherScanner._parse_iem_csv(text)
        assert c == pytest.approx((95.0 - 32.0) * 5.0 / 9.0, abs=0.01)  # 35°C

    def test_parse_iem_csv_skips_missing(self):
        text = (
            "station,valid,tmpf\n"
            "DAL,2026-06-06 12:00,M\n"
            "DAL,2026-06-06 15:00,70.0\n"
        )
        c = WeatherScanner._parse_iem_csv(text)
        assert c == pytest.approx((70.0 - 32.0) * 5.0 / 9.0, abs=0.01)

    def test_parse_iem_csv_empty_returns_none(self):
        assert WeatherScanner._parse_iem_csv("") is None
        assert WeatherScanner._parse_iem_csv("station,valid,tmpf\n") is None


# ── Per-station bias correction ──────────────────────────────────────────


from src.weather_scanner import VerificationRecord
import time as _time


def _vrec(icao, fc, actual, days_ago=1):
    return VerificationRecord(
        station_icao=icao, city="dallas",
        target_date=date.today() - timedelta(days=days_ago),
        lead_days=1, forecast_mean_c=fc, created_at=_time.time(),
        actual_c=actual, verified_at=_time.time(),
    )


class TestStationBias:
    def test_no_correction_below_min_samples(self, scanner):
        """Fewer verified records than bias_min_samples → bias 0 (no guessing)."""
        scanner._config.bias_min_samples = 10
        scanner._verification = [_vrec("KDAL", 30.0, 31.5) for _ in range(9)]
        assert scanner._station_bias("KDAL") == 0.0

    def test_bias_is_mean_error_when_enough_samples(self, scanner):
        """bias = mean(actual - raw forecast) once n >= min_samples."""
        scanner._config.bias_min_samples = 10
        scanner._verification = [_vrec("KDAL", 30.0, 31.5) for _ in range(10)]
        assert scanner._station_bias("KDAL") == pytest.approx(1.5)

    def test_bias_clamped_to_max_correction(self, scanner):
        """Huge measured bias (likely bad data) is clamped to ±cap."""
        scanner._config.bias_min_samples = 10
        scanner._config.bias_max_correction_c = 3.0
        scanner._verification = [_vrec("KDAL", 30.0, 36.0) for _ in range(10)]
        assert scanner._station_bias("KDAL") == pytest.approx(3.0)

    def test_bias_zero_when_disabled(self, scanner):
        scanner._config.bias_correction = False
        scanner._verification = [_vrec("KDAL", 30.0, 31.5) for _ in range(10)]
        assert scanner._station_bias("KDAL") == 0.0

    def test_bias_per_station_isolated(self, scanner):
        """Other stations' records don't bleed into the bias."""
        scanner._config.bias_min_samples = 5
        scanner._verification = (
            [_vrec("KDAL", 30.0, 31.0) for _ in range(5)]
            + [_vrec("EGLC", 20.0, 18.0) for _ in range(5)]
        )
        assert scanner._station_bias("KDAL") == pytest.approx(1.0)
        assert scanner._station_bias("EGLC") == pytest.approx(-2.0)

    def test_unverified_records_dont_count(self, scanner):
        scanner._config.bias_min_samples = 5
        recs = [_vrec("KDAL", 30.0, 31.0) for _ in range(5)]
        for r in recs[:3]:
            r.actual_c = None  # not yet verified
        scanner._verification = recs
        assert scanner._station_bias("KDAL") == 0.0  # only 2 verified < 5


class TestVerificationRecording:
    def test_record_dedup_overwrites_same_horizon(self, scanner):
        """Same (station, date, lead) → latest forecast wins, no duplicates."""
        market = _mk_market(["30°C"], [0.5])
        market.station_icao = "KDAL"
        market.target_date = date.today() + timedelta(days=1)
        scanner._record_forecast_verification(market, 30.0)
        scanner._record_forecast_verification(market, 31.0)
        assert len(scanner._verification) == 1
        assert scanner._verification[0].forecast_mean_c == pytest.approx(31.0)

    def test_record_skipped_without_station(self, scanner):
        market = _mk_market(["30°C"], [0.5])
        market.city_slug = "nowhere-ville"  # no CITY_STATION mapping
        market.station_icao = ""
        scanner._record_forecast_verification(market, 30.0)
        assert scanner._verification == []

    def test_verified_record_not_overwritten(self, scanner):
        """Once verified, the record is frozen (it's a measurement)."""
        market = _mk_market(["30°C"], [0.5])
        market.station_icao = "KDAL"
        market.target_date = date.today() + timedelta(days=1)
        scanner._record_forecast_verification(market, 30.0)
        scanner._verification[0].actual_c = 32.0
        scanner._record_forecast_verification(market, 99.0)
        assert scanner._verification[0].forecast_mean_c == pytest.approx(30.0)
