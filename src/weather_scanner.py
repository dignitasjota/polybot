"""Weather Prediction Scanner — temperature forecast arbitrage.

Uses Open-Meteo ensemble API (51 ECMWF IFS models) to calculate probability
distributions for daily high temperatures, then compares against Polymarket
pricing to find mispriced outcomes.

Architecture:
  1. Discover temperature markets via Gamma API (textQuery)
  2. Parse city + date from market slug/question
  3. Fetch ensemble forecast from Open-Meteo (51 members)
  4. Build probability distribution per degree bucket
  5. Compare forecast prob vs market price → detect edge
  6. Execute if edge > threshold

Open-Meteo ensemble API:
  - Endpoint: https://ensemble-api.open-meteo.com/v1/ensemble
  - Model: ECMWF IFS 0.25° (51 members, 15-day forecast)
  - Response: temperature_2m_member01..member50 + temperature_2m (mean)
  - Free, no API key required
"""

from __future__ import annotations

import asyncio
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from src.config import CredentialsConfig

CLOB_URL = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
ENSEMBLE_API = "https://ensemble-api.open-meteo.com/v1/ensemble"

logger = structlog.get_logger("polymarket.weather")


# ── City coordinates (hardcoded for speed, avoids geocoding API) ──────────

CITY_COORDS: dict[str, tuple[float, float, str]] = {
    # city_slug: (latitude, longitude, timezone)
    "hong-kong": (22.32, 114.17, "Asia/Hong_Kong"),
    "taipei": (25.07, 121.55, "Asia/Taipei"),
    "seoul": (37.57, 126.97, "Asia/Seoul"),
    "tokyo": (35.69, 139.69, "Asia/Tokyo"),
    "shanghai": (31.23, 121.47, "Asia/Shanghai"),
    "beijing": (39.90, 116.41, "Asia/Shanghai"),
    "singapore": (1.35, 103.82, "Asia/Singapore"),
    "jakarta": (-6.21, 106.85, "Asia/Jakarta"),
    "manila": (14.60, 120.98, "Asia/Manila"),
    "kuala-lumpur": (3.14, 101.69, "Asia/Kuala_Lumpur"),
    "bangkok": (13.76, 100.50, "Asia/Bangkok"),
    "busan": (35.18, 129.08, "Asia/Seoul"),
    "qingdao": (36.07, 120.38, "Asia/Shanghai"),
    "chengdu": (30.57, 104.07, "Asia/Shanghai"),
    "chongqing": (29.56, 106.55, "Asia/Shanghai"),
    # Europe
    "paris": (48.87, 2.35, "Europe/Paris"),
    "london": (51.51, -0.13, "Europe/London"),
    "milan": (45.46, 9.19, "Europe/Rome"),
    "madrid": (40.42, -3.70, "Europe/Madrid"),
    "munich": (48.14, 11.58, "Europe/Berlin"),
    "amsterdam": (52.37, 4.90, "Europe/Amsterdam"),
    "warsaw": (52.23, 21.01, "Europe/Warsaw"),
    "istanbul": (41.01, 28.98, "Europe/Istanbul"),
    "moscow": (55.76, 37.62, "Europe/Moscow"),
    "helsinki": (60.17, 24.94, "Europe/Helsinki"),
    "ankara": (39.93, 32.86, "Europe/Istanbul"),
    # Americas
    "new-york": (40.71, -74.01, "America/New_York"),
    "nyc": (40.71, -74.01, "America/New_York"),
    "chicago": (41.88, -87.63, "America/Chicago"),
    "houston": (29.76, -95.37, "America/Chicago"),
    "austin": (30.27, -97.74, "America/Chicago"),
    "dallas": (32.78, -96.80, "America/Chicago"),
    "denver": (39.74, -104.99, "America/Denver"),
    "seattle": (47.61, -122.33, "America/Los_Angeles"),
    "los-angeles": (33.94, -118.24, "America/Los_Angeles"),
    "miami": (25.76, -80.19, "America/New_York"),
    "toronto": (43.65, -79.38, "America/Toronto"),
    "mexico-city": (19.43, -99.13, "America/Mexico_City"),
    "buenos-aires": (-34.60, -58.38, "America/Argentina/Buenos_Aires"),
    "panama-city": (8.98, -79.52, "America/Panama"),
    "sao-paulo": (-23.55, -46.63, "America/Sao_Paulo"),
    # Africa/Middle East
    "lagos": (6.52, 3.38, "Africa/Lagos"),
    "cape-town": (-33.93, 18.42, "Africa/Johannesburg"),
    "jeddah": (21.49, 39.19, "Asia/Riyadh"),
}


# ── Data classes ─────────────────────────────────────────────────────────

@dataclass
class WeatherMarket:
    """A Polymarket temperature market parsed from Gamma API."""
    event_slug: str
    condition_id: str
    question: str
    city_slug: str
    target_date: date
    outcomes: list[str]           # ["17°C or below", "18°C", "19°C", ..., "27°C or higher"]
    outcome_prices: list[float]   # Current market prices for each outcome
    token_ids: list[str]          # CLOB token IDs for each outcome
    volume: float = 0.0
    liquidity: float = 0.0
    end_date: str = ""
    fee_rate: float = 0.05        # weather_fees default


@dataclass
class ForecastDistribution:
    """Probability distribution from ensemble forecast."""
    city_slug: str
    target_date: date
    buckets: dict[str, float]     # outcome_label → probability (0-1)
    ensemble_max_temps: list[float]  # Raw max temps from each member
    agreement: float              # How much models agree (0-1, 1=unanimous)
    fetched_at: float = 0.0


@dataclass
class WeatherOpportunity:
    """A detected edge between forecast and market."""
    market: WeatherMarket
    forecast: ForecastDistribution
    best_outcome_idx: int         # Index of outcome with most edge
    best_outcome_label: str       # e.g. "15°C"
    forecast_prob: float          # Our estimated probability
    market_price: float           # What market says
    edge: float                   # forecast_prob - market_price
    expected_value: float         # edge * payout_per_share
    kelly_fraction: float         # Optimal bet sizing
    detected_at: float = 0.0


@dataclass
class WeatherTrade:
    """Record of an executed weather trade."""
    trade_id: str
    condition_id: str
    question: str
    city: str
    outcome: str
    shares: float
    price: float
    cost: float
    forecast_prob: float
    edge: float
    status: str = "pending"       # pending, confirmed, won, lost
    created_at: float = 0.0
    resolved_at: float = 0.0
    pnl: float = 0.0
    mode: str = "paper"


# ── Scanner ──────────────────────────────────────────────────────────────

class WeatherScanner:
    """Scans Polymarket temperature markets for forecast-based alpha.

    Lifecycle:
        scanner = WeatherScanner(config, credentials)
        await scanner.start()
        ...
        await scanner.stop()
    """

    def __init__(self, config, credentials: CredentialsConfig | None = None):
        self._config = config
        self._credentials = credentials

        # HTTP session (lazy init)
        self._session = None
        self._client = None  # ClobClient for live mode
        self._initialized = False

        # State
        self._running = False
        self._scan_task: asyncio.Task | None = None
        self._markets: list[WeatherMarket] = []
        self._trades: list[WeatherTrade] = []
        self._forecast_cache: dict[str, ForecastDistribution] = {}  # "city:date" → forecast

        # Stats
        self._total_scans = 0
        self._markets_found = 0
        self._opportunities_found = 0
        self._trades_executed = 0
        self._trades_won = 0
        self._trades_lost = 0
        self._total_pnl = 0.0
        self._started_at: float = 0.0

    @property
    def is_paper(self) -> bool:
        return self._config.mode == "paper"

    @property
    def should_simulate(self) -> bool:
        return self._config.mode in ("paper", "dry_run")

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self):
        if self._running:
            return

        import aiohttp
        self._session = aiohttp.ClientSession()

        if not self.should_simulate:
            await self._init_clob_client()

        self._running = True
        self._started_at = time.time()
        self._scan_task = asyncio.create_task(self._scan_loop())

        logger.info(
            "weather_scanner_started",
            mode=self._config.mode,
            scan_interval=self._config.scan_interval,
            min_edge=self._config.min_edge,
        )

    async def stop(self):
        self._running = False
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
            self._session = None
        logger.info("weather_scanner_stopped")

    # ── ClobClient init ──────────────────────────────────────────────────

    async def _init_clob_client(self):
        if not self._credentials:
            logger.error("no_credentials_for_weather_live")
            return
        try:
            import os
            from py_clob_client_v2 import ApiCreds, ClobClient

            private_key = self._credentials.get_private_key()
            sig_type = self._credentials.signature_type
            proxy_address = self._credentials.get_proxy_address()

            funder = proxy_address
            if not funder:
                from eth_account import Account
                account = Account.from_key(private_key)
                funder = account.address

            try:
                api_key = self._credentials.get_api_key()
                api_secret = self._credentials.get_api_secret()
                passphrase = self._credentials.get_passphrase()
            except EnvironmentError:
                logger.info("deriving_api_creds_weather")
                tmp = ClobClient(
                    host=CLOB_URL, key=private_key,
                    chain_id=137, signature_type=sig_type, funder=funder,
                )
                creds = tmp.create_or_derive_api_key()
                api_key = creds.api_key
                api_secret = creds.api_secret
                passphrase = creds.api_passphrase

            self._client = ClobClient(
                host=CLOB_URL, key=private_key, chain_id=137,
                signature_type=sig_type, funder=funder,
                creds=ApiCreds(
                    api_key=api_key,
                    api_secret=api_secret,
                    api_passphrase=passphrase,
                ),
            )
            self._initialized = True
            logger.info("weather_clob_initialized", sig_type=sig_type)
        except Exception as e:
            logger.error("weather_clob_init_failed", error=str(e))

    # ── Scan loop ────────────────────────────────────────────────────────

    async def _scan_loop(self):
        """Main loop: discover markets → fetch forecasts → find edges → trade."""
        while self._running:
            try:
                await self._run_scan_cycle()
            except Exception as e:
                logger.error("weather_scan_error", error=str(e))
            await asyncio.sleep(self._config.scan_interval)

    async def _run_scan_cycle(self):
        """Single scan cycle."""
        self._total_scans += 1

        # Step 1: Discover temperature markets
        markets = await self._discover_markets()
        if not markets:
            if self._total_scans % 10 == 1:
                logger.info("weather_no_markets_found")
            return

        self._markets = markets
        self._markets_found = len(markets)

        # Step 2: For each market, get forecast and evaluate
        opportunities: list[WeatherOpportunity] = []
        for market in markets:
            try:
                forecast = await self._get_forecast(market)
                if not forecast:
                    continue

                opp = self._evaluate_market(market, forecast)
                if opp:
                    opportunities.append(opp)
            except Exception as e:
                logger.warning(
                    "weather_market_eval_error",
                    city=market.city_slug,
                    error=str(e),
                )

        # Step 3: Execute best opportunities
        if opportunities:
            # Sort by edge (highest first)
            opportunities.sort(key=lambda o: o.edge, reverse=True)
            for opp in opportunities[:self._config.max_bets_per_cycle]:
                self._opportunities_found += 1
                await self._execute_trade(opp)

        # Diagnostic log
        if self._total_scans % 5 == 1:
            logger.info(
                "weather_scan_diag",
                markets=len(markets),
                forecast_cache_size=len(self._forecast_cache),
                opportunities=len(opportunities),
                best_edge=round(opportunities[0].edge, 3) if opportunities else 0,
                total_trades=self._trades_executed,
                total_pnl=round(self._total_pnl, 2),
            )

    # ── Market Discovery ─────────────────────────────────────────────────

    async def _discover_markets(self) -> list[WeatherMarket]:
        """Fetch active temperature markets from Gamma API."""
        if not self._session:
            return []

        markets: list[WeatherMarket] = []

        # Try multiple search queries to find temperature markets
        queries = [
            "highest temperature",
            "high temperature",
        ]

        seen_conditions: set[str] = set()

        for query in queries:
            try:
                url = f"{GAMMA_API}/events"
                params = {
                    "active": "true",
                    "closed": "false",
                    "limit": "50",
                    "title": query,
                }
                async with self._session.get(url, params=params) as resp:
                    if resp.status != 200:
                        continue
                    events = await resp.json()

                for event in events:
                    event_markets = event.get("markets", [])
                    for mkt in event_markets:
                        cid = mkt.get("conditionId", "")
                        if cid in seen_conditions:
                            continue
                        if not mkt.get("active") or mkt.get("closed"):
                            continue

                        parsed = self._parse_temperature_market(mkt, event)
                        if parsed:
                            seen_conditions.add(cid)
                            markets.append(parsed)
            except Exception as e:
                logger.warning("weather_discovery_error", query=query, error=str(e))

        return markets

    def _parse_temperature_market(self, mkt: dict, event: dict) -> WeatherMarket | None:
        """Parse a Gamma API market response into WeatherMarket.

        Expected slug format: highest-temperature-in-{city}-on-{month}-{day}-{year}
        Expected question: "Highest temperature in {City} on {Month} {Day}?"
        """
        slug = mkt.get("slug", "") or event.get("slug", "")
        question = mkt.get("question", "") or mkt.get("groupItemTitle", "")

        if not slug and not question:
            return None

        # Extract city and date from slug
        # Pattern: highest-temperature-in-{city}-on-{month}-{day}-{year}
        slug_match = re.match(
            r"highest-temperature-in-(.+?)-on-(\w+)-(\d+)(?:-(\d{4}))?",
            slug,
        )
        if not slug_match:
            # Try from question: "Highest temperature in {City} on {Month} {Day}?"
            q_match = re.match(
                r"(?:Highest|High) temperature in (.+?) on (\w+) (\d+)",
                question, re.IGNORECASE,
            )
            if not q_match:
                return None
            city_raw = q_match.group(1).lower().replace(" ", "-")
            month_str = q_match.group(2)
            day_str = q_match.group(3)
            year_str = None
        else:
            city_raw = slug_match.group(1)
            month_str = slug_match.group(2)
            day_str = slug_match.group(3)
            year_str = slug_match.group(4)

        # Resolve city
        city_slug = self._normalize_city(city_raw)
        if city_slug not in CITY_COORDS:
            logger.debug("weather_unknown_city", city=city_raw)
            return None

        # Parse date
        target_date = self._parse_market_date(month_str, day_str, year_str)
        if not target_date:
            return None

        # Skip markets already resolved (date in the past)
        today = date.today()
        if target_date < today:
            return None

        # Skip markets too far in future (forecast unreliable beyond 7 days)
        if (target_date - today).days > self._config.max_forecast_days:
            return None

        # Parse outcomes and prices
        outcomes = mkt.get("outcomes", [])
        outcome_prices_raw = mkt.get("outcomePrices", [])
        token_ids = mkt.get("clobTokenIds", [])

        if not outcomes or not outcome_prices_raw or not token_ids:
            return None

        # Multi-outcome markets have outcomes like ["17°C or below", "18°C", ...]
        # Binary markets have ["Yes", "No"] — skip those for now
        if len(outcomes) < 3:
            return None

        try:
            outcome_prices = [float(p) for p in outcome_prices_raw]
        except (ValueError, TypeError):
            return None

        return WeatherMarket(
            event_slug=slug,
            condition_id=mkt.get("conditionId", ""),
            question=question or mkt.get("question", ""),
            city_slug=city_slug,
            target_date=target_date,
            outcomes=outcomes,
            outcome_prices=outcome_prices,
            token_ids=token_ids,
            volume=float(mkt.get("volume", 0) or 0),
            liquidity=float(mkt.get("liquidity", 0) or 0),
            end_date=mkt.get("endDate", ""),
            fee_rate=self._extract_fee_rate(mkt),
        )

    def _normalize_city(self, city_raw: str) -> str:
        """Normalize city slug to match CITY_COORDS keys."""
        # Handle common aliases
        aliases = {
            "new-york-city": "new-york",
            "la": "los-angeles",
            "sao-paulo": "sao-paulo",
            "são-paulo": "sao-paulo",
        }
        normalized = city_raw.lower().strip()
        return aliases.get(normalized, normalized)

    def _parse_market_date(self, month_str: str, day_str: str, year_str: str | None) -> date | None:
        """Parse month name + day + optional year into a date."""
        months = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
            "jan": 1, "feb": 2, "mar": 3, "apr": 4,
            "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        month = months.get(month_str.lower())
        if not month:
            return None

        try:
            day = int(day_str)
            year = int(year_str) if year_str else date.today().year
            return date(year, month, day)
        except (ValueError, TypeError):
            return None

    def _extract_fee_rate(self, mkt: dict) -> float:
        """Extract fee rate from market data."""
        fee_schedule = mkt.get("feeSchedule") or {}
        rate = fee_schedule.get("rate")
        if rate is not None:
            return float(rate)
        # Fallback: weather_fees = 0.05
        return 0.05

    # ── Forecast ─────────────────────────────────────────────────────────

    async def _get_forecast(self, market: WeatherMarket) -> ForecastDistribution | None:
        """Get ensemble forecast for a market, using cache if fresh."""
        cache_key = f"{market.city_slug}:{market.target_date.isoformat()}"

        # Check cache
        cached = self._forecast_cache.get(cache_key)
        if cached and (time.time() - cached.fetched_at) < self._config.forecast_cache_ttl:
            return cached

        # Fetch from Open-Meteo
        coords = CITY_COORDS.get(market.city_slug)
        if not coords:
            return None

        lat, lon, tz = coords
        forecast = await self._fetch_ensemble_forecast(lat, lon, tz, market.target_date, market.outcomes)
        if forecast:
            forecast.city_slug = market.city_slug
            forecast.target_date = market.target_date
            self._forecast_cache[cache_key] = forecast

        return forecast

    async def _fetch_ensemble_forecast(
        self, lat: float, lon: float, tz: str, target_date: date, outcomes: list[str]
    ) -> ForecastDistribution | None:
        """Fetch ECMWF IFS ensemble forecast from Open-Meteo."""
        if not self._session:
            return None

        # Calculate forecast_days needed
        today = date.today()
        days_ahead = (target_date - today).days + 1  # +1 to include the target day
        if days_ahead < 1:
            return None

        params = {
            "latitude": str(lat),
            "longitude": str(lon),
            "hourly": "temperature_2m",
            "models": "ecmwf_ifs025",
            "forecast_days": str(min(days_ahead + 1, 15)),
            "timezone": tz,
        }

        try:
            async with self._session.get(ENSEMBLE_API, params=params) as resp:
                if resp.status != 200:
                    logger.warning("ensemble_api_error", status=resp.status)
                    return None
                data = await resp.json()
        except Exception as e:
            logger.warning("ensemble_fetch_error", error=str(e))
            return None

        # Extract hourly data
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        if not times:
            return None

        # Find indices for the target date (local time)
        target_str = target_date.isoformat()  # "2026-05-05"
        target_indices = [
            i for i, t in enumerate(times)
            if t.startswith(target_str)
        ]
        if not target_indices:
            return None

        # Get max temperature for each ensemble member on the target date
        ensemble_max_temps: list[float] = []

        # Mean/control member
        mean_temps = hourly.get("temperature_2m", [])

        # Extract all ensemble members (member01..member50)
        member_keys = [
            k for k in hourly.keys()
            if k.startswith("temperature_2m_member")
        ]

        if not member_keys:
            # Fallback: use mean only (less useful but still works)
            if mean_temps:
                max_temp = max(mean_temps[i] for i in target_indices if i < len(mean_temps))
                ensemble_max_temps = [max_temp]
            else:
                return None
        else:
            for key in member_keys:
                member_temps = hourly.get(key, [])
                if member_temps:
                    member_day_temps = [
                        member_temps[i] for i in target_indices
                        if i < len(member_temps) and member_temps[i] is not None
                    ]
                    if member_day_temps:
                        ensemble_max_temps.append(max(member_day_temps))

        if not ensemble_max_temps:
            return None

        # Build probability distribution over outcome buckets
        buckets = self._build_distribution(ensemble_max_temps, outcomes)

        # Calculate agreement (how concentrated the distribution is)
        probs = list(buckets.values())
        max_prob = max(probs) if probs else 0
        agreement = max_prob  # 1.0 = all members agree on same bucket

        return ForecastDistribution(
            city_slug="",  # Set by caller
            target_date=target_date,
            buckets=buckets,
            ensemble_max_temps=ensemble_max_temps,
            agreement=agreement,
            fetched_at=time.time(),
        )

    def _build_distribution(
        self, max_temps: list[float], outcomes: list[str]
    ) -> dict[str, float]:
        """Convert ensemble max temps into probability per outcome bucket.

        Outcome format examples:
          - "17°C or below" → all temps ≤ 17
          - "18°C"          → temps in [17.5, 18.5)
          - "27°C or higher" → all temps ≥ 27 (actually ≥ 26.5)
        """
        n_members = len(max_temps)
        if n_members == 0:
            return {}

        # Parse outcome buckets: extract temperature thresholds
        parsed_buckets: list[tuple[str, float, float]] = []  # (label, low, high)

        for i, outcome in enumerate(outcomes):
            temp = self._parse_outcome_temp(outcome)
            if temp is None:
                continue

            if "or below" in outcome.lower() or "or less" in outcome.lower():
                parsed_buckets.append((outcome, -999, temp + 0.5))
            elif "or higher" in outcome.lower() or "or more" in outcome.lower() or "+" in outcome:
                parsed_buckets.append((outcome, temp - 0.5, 999))
            else:
                # Single degree: e.g. "18°C" means [17.5, 18.5)
                parsed_buckets.append((outcome, temp - 0.5, temp + 0.5))

        if not parsed_buckets:
            return {}

        # Count members falling into each bucket
        counts: dict[str, int] = {label: 0 for label, _, _ in parsed_buckets}
        for temp in max_temps:
            for label, low, high in parsed_buckets:
                if low <= temp < high:
                    counts[label] += 1
                    break
            else:
                # Temp outside all buckets — assign to nearest
                if temp < parsed_buckets[0][1]:
                    counts[parsed_buckets[0][0]] += 1
                else:
                    counts[parsed_buckets[-1][0]] += 1

        # Convert to probabilities
        return {label: count / n_members for label, count in counts.items()}

    def _parse_outcome_temp(self, outcome: str) -> float | None:
        """Extract temperature value from outcome label.

        Examples: "18°C" → 18, "72°F" → 22.2, "17°C or below" → 17
        """
        # Match patterns like "18°C", "72°F", "18 °C"
        match = re.search(r"(-?\d+)\s*°\s*([CF])", outcome)
        if not match:
            # Try plain number
            match = re.search(r"(-?\d+)", outcome)
            if not match:
                return None
            return float(match.group(1))

        temp = float(match.group(1))
        unit = match.group(2)

        if unit == "F":
            # Convert to Celsius (Open-Meteo returns °C)
            temp = (temp - 32) * 5 / 9

        return temp

    # ── Evaluation ───────────────────────────────────────────────────────

    def _evaluate_market(
        self, market: WeatherMarket, forecast: ForecastDistribution
    ) -> WeatherOpportunity | None:
        """Compare forecast distribution against market prices to find edge."""
        best_edge = 0.0
        best_idx = -1
        best_label = ""
        best_forecast_prob = 0.0
        best_market_price = 0.0

        for i, outcome in enumerate(market.outcomes):
            if i >= len(market.outcome_prices):
                break

            market_price = market.outcome_prices[i]
            forecast_prob = forecast.buckets.get(outcome, 0.0)

            # Skip if no forecast data for this outcome
            if forecast_prob == 0.0 and market_price < 0.05:
                continue

            # Edge = what we think prob is - what market says
            edge = forecast_prob - market_price

            # Only consider positive edge (underpriced by market)
            if edge > best_edge:
                best_edge = edge
                best_idx = i
                best_label = outcome
                best_forecast_prob = forecast_prob
                best_market_price = market_price

        # Minimum thresholds
        if best_edge < self._config.min_edge:
            return None

        # Don't bet on very low probability events (noisy)
        if best_forecast_prob < self._config.min_forecast_prob:
            return None

        # Don't buy overpriced outcomes (even with edge, risk/reward bad)
        if best_market_price > self._config.max_price:
            return None

        # Agreement filter: if models disagree strongly, skip
        if forecast.agreement < self._config.min_agreement:
            return None

        # Expected value: prob × $1.00 - cost = prob - price
        ev_per_share = best_forecast_prob - best_market_price

        # Fee adjustment
        fee_per_share = market.fee_rate * best_market_price * (1 - best_market_price)
        ev_per_share -= fee_per_share

        if ev_per_share <= 0:
            return None

        # Kelly criterion for bet sizing
        # f* = (bp - q) / b where b=odds, p=prob, q=1-p
        b = (1.0 / best_market_price) - 1  # Odds (e.g. price 0.40 → odds 1.5:1)
        p = best_forecast_prob
        q = 1 - p
        kelly = (b * p - q) / b if b > 0 else 0
        kelly = max(0, min(kelly, 0.25))  # Cap at 25% Kelly

        return WeatherOpportunity(
            market=market,
            forecast=forecast,
            best_outcome_idx=best_idx,
            best_outcome_label=best_label,
            forecast_prob=best_forecast_prob,
            market_price=best_market_price,
            edge=best_edge,
            expected_value=ev_per_share,
            kelly_fraction=kelly,
            detected_at=time.time(),
        )

    # ── Execution ────────────────────────────────────────────────────────

    async def _execute_trade(self, opp: WeatherOpportunity):
        """Execute a weather trade based on detected opportunity."""
        market = opp.market

        # Calculate bet size
        bet_size = self._calculate_bet_size(opp)
        if bet_size < 1.0:
            return

        shares = bet_size / opp.market_price
        cost = bet_size

        trade = WeatherTrade(
            trade_id=f"wx_{uuid.uuid4().hex[:12]}",
            condition_id=market.condition_id,
            question=market.question,
            city=market.city_slug,
            outcome=opp.best_outcome_label,
            shares=round(shares, 2),
            price=opp.market_price,
            cost=round(cost, 4),
            forecast_prob=opp.forecast_prob,
            edge=opp.edge,
            created_at=time.time(),
            mode="paper" if self.should_simulate else "live",
        )

        if self.should_simulate:
            await self._paper_execute(trade, opp)
        else:
            await self._live_execute(trade, opp)

    def _calculate_bet_size(self, opp: WeatherOpportunity) -> float:
        """Calculate bet size using fractional Kelly."""
        max_bet = self._config.max_bet_per_trade
        kelly_bet = self._config.bankroll * opp.kelly_fraction * self._config.kelly_multiplier

        # Use fraction of Kelly (conservative)
        bet = min(kelly_bet, max_bet)
        bet = max(bet, 0)

        return round(bet, 2)

    async def _paper_execute(self, trade: WeatherTrade, opp: WeatherOpportunity):
        """Simulate the trade in paper mode."""
        trade.status = "pending"
        self._trades.append(trade)
        self._trades_executed += 1

        logger.info(
            "weather_paper_trade",
            trade_id=trade.trade_id,
            city=trade.city,
            outcome=trade.outcome,
            price=f"${trade.price:.3f}",
            shares=trade.shares,
            cost=f"${trade.cost:.2f}",
            edge=f"{opp.edge:.1%}",
            forecast_prob=f"{opp.forecast_prob:.1%}",
            agreement=f"{opp.forecast.agreement:.1%}",
        )

    async def _live_execute(self, trade: WeatherTrade, opp: WeatherOpportunity):
        """Execute real trade via CLOB."""
        if not self._client:
            logger.error("no_clob_client_for_weather")
            trade.status = "failed"
            self._trades.append(trade)
            return

        try:
            from py_clob_client_v2 import OrderArgs
            from py_clob_client_v2.order_builder.constants import BUY

            token_id = opp.market.token_ids[opp.best_outcome_idx]
            order_args = OrderArgs(
                price=opp.market_price,
                size=round(trade.shares, 2),
                side=BUY,
                token_id=token_id,
            )
            signed = self._client.create_order(order_args)
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: self._client.post_order(signed)
            )

            order_id = result.get("orderID", "") if isinstance(result, dict) else ""
            if order_id:
                trade.status = "confirmed"
                logger.info(
                    "weather_live_trade",
                    trade_id=trade.trade_id,
                    order_id=order_id[:12],
                    city=trade.city,
                    outcome=trade.outcome,
                    price=f"${trade.price:.3f}",
                    edge=f"{opp.edge:.1%}",
                )
            else:
                trade.status = "failed"
                logger.error("weather_order_no_id", response=str(result)[:200])

        except Exception as e:
            trade.status = "failed"
            logger.error("weather_live_error", error=str(e))

        self._trades.append(trade)
        self._trades_executed += 1

    # ── Resolution ───────────────────────────────────────────────────────

    async def check_resolutions(self):
        """Check if any pending trades have resolved.

        Called periodically by the strategy. Uses actual temperature data
        from Open-Meteo historical endpoint to determine winners.
        """
        now = time.time()
        pending = [t for t in self._trades if t.status == "pending"]

        for trade in pending:
            # Parse the trade's target date from the question
            # Only check if the market date has passed
            market = next(
                (m for m in self._markets if m.condition_id == trade.condition_id),
                None,
            )
            if not market:
                # If market date info not available, check by age (>48h = expired)
                if now - trade.created_at > 172800:
                    trade.status = "expired"
                continue

            if market.target_date >= date.today():
                continue  # Market not yet resolved

            # Fetch actual temperature to determine winner
            actual_temp = await self._fetch_actual_temperature(market)
            if actual_temp is None:
                continue

            # Determine which outcome won
            winning_outcome = self._determine_winner(actual_temp, market.outcomes)
            if winning_outcome is None:
                continue

            if trade.outcome == winning_outcome:
                # We won: payout = $1.00 * shares - cost
                trade.pnl = (1.0 * trade.shares) - trade.cost
                trade.status = "won"
                self._trades_won += 1
            else:
                # We lost: lost our cost
                trade.pnl = -trade.cost
                trade.status = "lost"
                self._trades_lost += 1

            trade.resolved_at = now
            self._total_pnl += trade.pnl

            logger.info(
                "weather_trade_resolved",
                trade_id=trade.trade_id,
                city=trade.city,
                outcome=trade.outcome,
                actual_temp=f"{actual_temp:.1f}°C",
                winning=winning_outcome,
                status=trade.status,
                pnl=f"${trade.pnl:.2f}",
            )

    async def _fetch_actual_temperature(self, market: WeatherMarket) -> float | None:
        """Fetch actual recorded temperature for a resolved market."""
        if not self._session:
            return None

        coords = CITY_COORDS.get(market.city_slug)
        if not coords:
            return None

        lat, lon, tz = coords
        target_str = market.target_date.isoformat()

        # Use Open-Meteo historical weather API
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": str(lat),
            "longitude": str(lon),
            "daily": "temperature_2m_max",
            "timezone": tz,
            "start_date": target_str,
            "end_date": target_str,
        }

        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            daily = data.get("daily", {})
            temps = daily.get("temperature_2m_max", [])
            if temps and temps[0] is not None:
                return float(temps[0])
        except Exception as e:
            logger.warning("weather_actual_fetch_error", error=str(e))

        return None

    def _determine_winner(self, actual_temp: float, outcomes: list[str]) -> str | None:
        """Determine which outcome won given the actual temperature.

        Uses same bucket logic as _build_distribution:
          - "X°C or below": (-inf, X+0.5)
          - "X°C": [X-0.5, X+0.5)
          - "X°C or higher": [X-0.5, +inf)
        Iterates in order; first match wins.
        """
        for outcome in outcomes:
            temp = self._parse_outcome_temp(outcome)
            if temp is None:
                continue

            if "or below" in outcome.lower() or "or less" in outcome.lower():
                if actual_temp < temp + 0.5:
                    return outcome
            elif "or higher" in outcome.lower() or "or more" in outcome.lower() or "+" in outcome:
                if actual_temp >= temp - 0.5:
                    return outcome
            else:
                # Single degree bucket: [temp-0.5, temp+0.5)
                if temp - 0.5 <= actual_temp < temp + 0.5:
                    return outcome

        return None

    # ── Stats ────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Return scanner statistics."""
        recent_trades = self._trades[-20:]

        # Current forecast summary
        active_forecasts = []
        for key, forecast in self._forecast_cache.items():
            if time.time() - forecast.fetched_at < self._config.forecast_cache_ttl:
                top_bucket = max(forecast.buckets.items(), key=lambda x: x[1]) if forecast.buckets else ("?", 0)
                active_forecasts.append({
                    "city_date": key,
                    "top_prediction": top_bucket[0],
                    "top_prob": round(top_bucket[1], 2),
                    "agreement": round(forecast.agreement, 2),
                    "members": len(forecast.ensemble_max_temps),
                })

        return {
            "running": self._running,
            "mode": self._config.mode,
            "total_scans": self._total_scans,
            "markets_found": self._markets_found,
            "opportunities_found": self._opportunities_found,
            "trades_executed": self._trades_executed,
            "trades_won": self._trades_won,
            "trades_lost": self._trades_lost,
            "win_rate": round(
                self._trades_won / max(1, self._trades_won + self._trades_lost), 3
            ),
            "total_pnl": round(self._total_pnl, 2),
            "uptime_s": round(time.time() - self._started_at, 1) if self._started_at else 0,
            "forecast_cache": active_forecasts[:10],
            "recent_trades": [
                {
                    "trade_id": t.trade_id,
                    "city": t.city,
                    "outcome": t.outcome,
                    "price": round(t.price, 3),
                    "edge": round(t.edge, 3),
                    "cost": round(t.cost, 2),
                    "pnl": round(t.pnl, 2),
                    "status": t.status,
                    "mode": t.mode,
                }
                for t in recent_trades
            ],
        }
