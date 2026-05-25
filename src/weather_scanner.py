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
import json
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

import os
from pathlib import Path

CLOB_URL = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
ENSEMBLE_API = "https://ensemble-api.open-meteo.com/v1/ensemble"
TRADES_FILE = Path(os.environ.get("WEATHER_TRADES_PATH", "data/weather_trades.json"))

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
    "berlin": (52.52, 13.41, "Europe/Berlin"),
    "rome": (41.90, 12.50, "Europe/Rome"),
    "dublin": (53.35, -6.26, "Europe/Dublin"),
    "lisbon": (38.72, -9.14, "Europe/Lisbon"),
    "athens": (37.97, 23.73, "Europe/Athens"),
    "vienna": (48.21, 16.37, "Europe/Vienna"),
    "prague": (50.08, 14.44, "Europe/Prague"),
    "copenhagen": (55.68, 12.57, "Europe/Copenhagen"),
    "stockholm": (59.33, 18.07, "Europe/Stockholm"),
    "zurich": (47.38, 8.54, "Europe/Zurich"),
    "brussels": (50.85, 4.35, "Europe/Brussels"),
    "oslo": (59.91, 10.75, "Europe/Oslo"),
    "bucharest": (44.43, 26.10, "Europe/Bucharest"),
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
    "atlanta": (33.75, -84.39, "America/New_York"),
    "san-francisco": (37.77, -122.42, "America/Los_Angeles"),
    "phoenix": (33.45, -112.07, "America/Phoenix"),
    "washington": (38.91, -77.04, "America/New_York"),
    "washington-dc": (38.91, -77.04, "America/New_York"),
    "boston": (42.36, -71.06, "America/New_York"),
    "philadelphia": (39.95, -75.17, "America/New_York"),
    "san-diego": (32.72, -117.16, "America/Los_Angeles"),
    "detroit": (42.33, -83.05, "America/Detroit"),
    "minneapolis": (44.98, -93.27, "America/Chicago"),
    "portland": (45.52, -122.68, "America/Los_Angeles"),
    "las-vegas": (36.17, -115.14, "America/Los_Angeles"),
    "vancouver": (49.28, -123.12, "America/Vancouver"),
    "montreal": (45.50, -73.57, "America/Toronto"),
    "charlotte": (35.23, -80.84, "America/New_York"),
    "nashville": (36.16, -86.78, "America/Chicago"),
    "salt-lake-city": (40.76, -111.89, "America/Denver"),
    "honolulu": (21.31, -157.86, "Pacific/Honolulu"),
    "anchorage": (61.22, -149.90, "America/Anchorage"),
    # Africa/Middle East
    "lagos": (6.52, 3.38, "Africa/Lagos"),
    "cape-town": (-33.93, 18.42, "Africa/Johannesburg"),
    "jeddah": (21.49, 39.19, "Asia/Riyadh"),
    "dubai": (25.20, 55.27, "Asia/Dubai"),
    "riyadh": (24.69, 46.72, "Asia/Riyadh"),
    "cairo": (30.04, 31.24, "Africa/Cairo"),
    "nairobi": (-1.29, 36.82, "Africa/Nairobi"),
    "mumbai": (19.08, 72.88, "Asia/Kolkata"),
    "delhi": (28.61, 77.21, "Asia/Kolkata"),
    "new-delhi": (28.61, 77.21, "Asia/Kolkata"),
    "sydney": (-33.87, 151.21, "Australia/Sydney"),
    "melbourne": (-37.81, 144.96, "Australia/Melbourne"),
    "osaka": (34.69, 135.50, "Asia/Tokyo"),
    "ho-chi-minh-city": (10.82, 106.63, "Asia/Ho_Chi_Minh"),
    "hanoi": (21.03, 105.85, "Asia/Ho_Chi_Minh"),
    "wuhan": (30.59, 114.31, "Asia/Shanghai"),
    "karachi": (24.86, 67.01, "Asia/Karachi"),
    "lucknow": (26.85, 80.95, "Asia/Kolkata"),
    "guangzhou": (23.13, 113.26, "Asia/Shanghai"),
    "shenzhen": (22.54, 114.06, "Asia/Shanghai"),
    "wellington": (-41.29, 174.78, "Pacific/Auckland"),
    "tel-aviv": (32.08, 34.78, "Asia/Jerusalem"),
}


# ── Data classes ─────────────────────────────────────────────────────────

@dataclass
class WeatherMarket:
    """A Polymarket temperature event with multiple binary outcome markets.

    Polymarket structures temperature markets as an EVENT containing N binary
    markets (each Yes/No). E.g. "Highest temperature in LA on May 6?" has:
      - "Will it be 57°F or below?" → Yes/No (conditionId A, tokenIds A)
      - "Will it be 58-59°F?"       → Yes/No (conditionId B, tokenIds B)
      - ...
    """
    event_slug: str
    event_title: str
    city_slug: str
    target_date: date
    outcomes: list[str]           # ["57°F or below", "58-59°F", ..., "76°F or higher"]
    outcome_prices: list[float]   # YES price for each outcome binary market
    condition_ids: list[str]      # Per-outcome conditionId (each is a binary market)
    token_ids: list[str]          # Per-outcome YES token ID (clobTokenIds[0])
    volume: float = 0.0
    liquidity: float = 0.0
    end_date: str = ""
    fee_rate: float = 0.05        # weather_fees default
    unit: str = "C"               # "C" or "F" — detected from outcome labels


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
    target_date: date | None = None  # Market resolution date (for orphan resolution)
    outcomes: list[str] = field(default_factory=list)  # All outcomes (for resolution without _markets)
    unit: str = "C"               # "C" or "F" — needed for resolution
    status: str = "pending"       # pending, confirmed, won, lost, expired
    created_at: float = 0.0
    resolved_at: float = 0.0
    pnl: float = 0.0
    mode: str = "paper"
    lead_days: int = 0            # Days between bet placement and target_date (forecast horizon)


# ── Scanner ──────────────────────────────────────────────────────────────

class WeatherScanner:
    """Scans Polymarket temperature markets for forecast-based alpha.

    Lifecycle:
        scanner = WeatherScanner(config, credentials)
        await scanner.start()
        ...
        await scanner.stop()
    """

    # Max concurrent requests to Open-Meteo (avoid 429s on free tier)
    _FORECAST_CONCURRENCY = 2
    _RATE_LIMIT_BACKOFF_S = 300  # When 429, pause all fetches for 5 minutes

    def __init__(self, config, credentials: CredentialsConfig | None = None):
        self._config = config
        self._credentials = credentials

        # HTTP session (lazy init)
        self._session = None
        self._client = None  # ClobClient for live mode
        self._initialized = False

        # Rate limiting for Open-Meteo API
        self._forecast_semaphore = asyncio.Semaphore(self._FORECAST_CONCURRENCY)
        self._rate_limited_until: float = 0.0  # Timestamp; skip fetches until this passes

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

    def reset_stats(self):
        """Reset all trades and stats. Called when switching execution mode.

        Clears pending trades, counters, and the persistence file so that
        the new mode starts with a clean slate (e.g. live only shows live data).
        """
        self._trades.clear()
        self._total_scans = 0
        self._markets_found = 0
        self._opportunities_found = 0
        self._trades_executed = 0
        self._trades_won = 0
        self._trades_lost = 0
        self._total_pnl = 0.0
        self._forecast_cache.clear()
        # Persist empty state
        self._save_pending_trades()
        logger.info("weather_stats_reset", mode=self._config.mode)

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

        # Restore pending trades from previous run
        self._load_pending_trades()

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
            pending_trades=len([t for t in self._trades if t.status in ("pending", "confirmed")]),
        )

    async def stop(self):
        self._running = False
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
        # Persist pending trades before shutdown
        self._save_pending_trades()
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

        # Step 2: Pre-filter markets where edge is impossible, then fetch forecasts
        candidates = [m for m in markets if self._has_edge_potential(m)]

        opportunities: list[WeatherOpportunity] = []
        for market in candidates:
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

        # Step 3: Execute best opportunities (deduplicated)
        if opportunities:
            # Build set of condition_ids with pending trades to avoid duplicates
            pending_cids = {
                t.condition_id for t in self._trades
                if t.status == "pending"
            }

            # Sort by edge (highest first)
            opportunities.sort(key=lambda o: o.edge, reverse=True)
            executed_this_cycle = 0
            for opp in opportunities:
                if executed_this_cycle >= self._config.max_bets_per_cycle:
                    break
                # Skip if we already have a pending trade on this condition
                cid = opp.market.condition_ids[opp.best_outcome_idx]
                if cid in pending_cids:
                    continue
                self._opportunities_found += 1
                await self._execute_trade(opp)
                pending_cids.add(cid)
                executed_this_cycle += 1

        # Prune resolved trades older than 7 days to limit memory growth
        self._prune_old_trades()

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

    def _has_edge_potential(self, market: WeatherMarket) -> bool:
        """Quick pre-filter: skip markets where no outcome can possibly have edge.

        A market has edge potential if at least one outcome has price ≤ max_price.
        If all outcomes are priced above max_price, even a 100% forecast prob
        wouldn't pass the max_price filter in _evaluate_market().
        """
        max_price = self._config.max_price
        return any(p <= max_price for p in market.outcome_prices)

    # ── Pruning ──────────────────────────────────────────────────────────

    _MAX_RESOLVED_AGE = 7 * 86400  # 7 days in seconds
    _MAX_TRADES_KEPT = 500         # Hard cap on list size

    def _prune_old_trades(self):
        """Remove resolved trades older than 7 days to prevent unbounded memory growth.

        Keeps all pending/confirmed trades (still active) and the most recent
        resolved ones up to _MAX_TRADES_KEPT total.
        """
        now = time.time()
        cutoff = now - self._MAX_RESOLVED_AGE

        self._trades = [
            t for t in self._trades
            if t.status in ("pending", "confirmed")
            or t.resolved_at > cutoff
            or (t.resolved_at == 0 and now - t.created_at < self._MAX_RESOLVED_AGE)
        ]

        # Hard cap as safety net
        if len(self._trades) > self._MAX_TRADES_KEPT:
            # Keep pending first, then most recent
            pending = [t for t in self._trades if t.status in ("pending", "confirmed")]
            resolved = [t for t in self._trades if t.status not in ("pending", "confirmed")]
            resolved.sort(key=lambda t: t.created_at, reverse=True)
            self._trades = pending + resolved[: self._MAX_TRADES_KEPT - len(pending)]

    # ── Market Discovery ─────────────────────────────────────────────────

    # Polymarket tag_id for "Daily Temperature" markets
    TEMPERATURE_TAG_ID = 103040

    async def _discover_markets(self) -> list[WeatherMarket]:
        """Fetch active temperature events from Gamma API.

        Temperature markets are structured as EVENTS, each containing N binary
        markets (one per temperature bucket). We use tag_id=103040 (Daily Temperature)
        to discover them.

        Uses keyset pagination (/events/keyset). The legacy /events endpoint was
        deprecated on Apr 10, 2026 and returns empty arrays silently.
        """
        if not self._session:
            return []

        markets: list[WeatherMarket] = []
        seen_slugs: set[str] = set()
        url = f"{GAMMA_API}/events/keyset"
        after_cursor: str | None = None
        max_pages = 5  # Safety cap: 5 × 100 = 500 events max per scan

        # Server-side filter: only events ending from now onwards.
        # `active=true closed=false` was the previous filter pair, but in
        # /events/keyset (as of May 2026) it returns 0 results because Polymarket
        # marks historical events as active=true closed=true. Filter by date
        # bounds instead, then double-check `closed` in Python below.
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        max_end = (datetime.now(timezone.utc) + timedelta(days=self._config.max_forecast_days + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")

        for _ in range(max_pages):
            params: dict[str, str] = {
                "tag_id": str(self.TEMPERATURE_TAG_ID),
                "end_date_min": now_iso,
                "end_date_max": max_end,
                "limit": "100",
                "order": "endDate",
                "ascending": "true",
            }
            if after_cursor:
                params["after_cursor"] = after_cursor

            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status != 200:
                        logger.warning("weather_discovery_http_error", status=resp.status)
                        break
                    data = await resp.json()
            except Exception as e:
                logger.warning("weather_discovery_error", error=str(e))
                break

            # Keyset response: {"events": [...], "next_cursor": "..."} OR plain list (legacy)
            if isinstance(data, dict):
                events = data.get("events", []) or data.get("data", [])
                next_cursor = data.get("next_cursor")
            else:
                events = data if isinstance(data, list) else []
                next_cursor = None

            if not events:
                break

            for event in events:
                # NOTE: do NOT filter by event.closed here. As of May 2026 Polymarket
                # marks all recurring/hide-from-new weather events as closed=true at
                # the event level even when end_date is in the future and the
                # underlying binary markets are still tradeable. Tradability is
                # validated downstream when fetching prices.
                slug = event.get("slug", "")
                if slug in seen_slugs:
                    continue
                seen_slugs.add(slug)

                parsed = self._parse_temperature_event(event)
                if parsed:
                    markets.append(parsed)

            if not next_cursor:
                break
            after_cursor = next_cursor

        return markets

    def _parse_temperature_event(self, event: dict) -> WeatherMarket | None:
        """Parse a Gamma API event response into WeatherMarket.

        Each event contains N binary markets (one per temperature bucket).
        We extract city+date from the event slug and aggregate all binary
        markets into a single WeatherMarket with N outcomes.

        Event slug format: highest-temperature-in-{city}-on-{month}-{day}-{year}
        """
        slug = event.get("slug", "")
        title = event.get("title", "")

        # Extract city and date from slug
        slug_match = re.match(
            r"highest-temperature-in-(.+?)-on-(\w+)-(\d+)(?:-(\d{4}))?",
            slug,
        )
        if not slug_match:
            return None

        city_raw = slug_match.group(1)
        month_str = slug_match.group(2)
        day_str = slug_match.group(3)
        year_str = slug_match.group(4)

        # Resolve city
        city_slug = self._normalize_city(city_raw)
        if city_slug not in CITY_COORDS:
            logger.debug("weather_unknown_city", city=city_raw, slug=slug)
            return None

        # Parse date
        target_date = self._parse_market_date(month_str, day_str, year_str)
        if not target_date:
            return None

        # Skip markets already resolved (date in the past)
        today = date.today()
        if target_date < today:
            return None

        # Skip markets too far in future (forecast unreliable)
        if (target_date - today).days > self._config.max_forecast_days:
            return None

        # Parse binary markets within this event into outcomes
        event_markets = event.get("markets", [])
        if not event_markets:
            return None

        outcomes: list[str] = []
        outcome_prices: list[float] = []
        condition_ids: list[str] = []
        token_ids: list[str] = []
        total_volume = 0.0
        total_liquidity = 0.0
        fee_rate = 0.05
        end_date = ""
        unit = "C"

        for mkt in event_markets:
            # NOTE: don't filter by active/closed flags. As of May 2026 Polymarket
            # marks recurring weather markets as active=false closed=true even when
            # they're tradeable. Real tradability signal is conditionId +
            # clobTokenIds + outcomePrices being non-empty (checked below).
            question = mkt.get("question", "")
            cid = mkt.get("conditionId", "")

            # Gamma API returns these as JSON strings, not arrays
            clob_tokens_raw = mkt.get("clobTokenIds", "[]")
            prices_str = mkt.get("outcomePrices", "[]")

            if isinstance(clob_tokens_raw, str):
                try:
                    clob_tokens = json.loads(clob_tokens_raw)
                except (json.JSONDecodeError, TypeError):
                    clob_tokens = []
            else:
                clob_tokens = clob_tokens_raw or []

            if isinstance(prices_str, str):
                try:
                    prices_raw = json.loads(prices_str)
                except (json.JSONDecodeError, TypeError):
                    prices_raw = []
            else:
                prices_raw = prices_str or []

            if not cid or not clob_tokens or not prices_raw:
                continue

            # Extract outcome label from question
            # e.g. "Will the highest temperature in LA be 57°F or below on May 6?"
            # → "57°F or below"
            label = self._extract_outcome_label(question)
            if not label:
                # Fallback: use groupItemTitle if available
                label = mkt.get("groupItemTitle")
            if not label:
                continue

            # Detect unit
            if "°F" in label:
                unit = "F"

            # YES price is first in outcomePrices
            try:
                yes_price = float(prices_raw[0])
            except (ValueError, TypeError, IndexError):
                continue

            # YES token is first in clobTokenIds
            yes_token = clob_tokens[0] if clob_tokens else ""

            outcomes.append(label)
            outcome_prices.append(yes_price)
            condition_ids.append(cid)
            token_ids.append(yes_token)
            total_volume += float(mkt.get("volume", 0) or 0)
            total_liquidity += float(mkt.get("liquidity", 0) or 0)
            fee_rate = self._extract_fee_rate(mkt)
            if not end_date:
                end_date = mkt.get("endDate", "")

        if len(outcomes) < 3:
            return None

        # Sort outcomes by temperature (ascending)
        indexed = list(zip(outcomes, outcome_prices, condition_ids, token_ids))
        indexed.sort(key=lambda x: self._parse_outcome_temp(x[0]) or 999)
        outcomes, outcome_prices, condition_ids, token_ids = [list(t) for t in zip(*indexed)]

        return WeatherMarket(
            event_slug=slug,
            event_title=title,
            city_slug=city_slug,
            target_date=target_date,
            outcomes=outcomes,
            outcome_prices=outcome_prices,
            condition_ids=condition_ids,
            token_ids=token_ids,
            volume=total_volume,
            liquidity=total_liquidity,
            end_date=end_date,
            fee_rate=fee_rate,
            unit=unit,
        )

    def _extract_outcome_label(self, question: str) -> str | None:
        """Extract temperature range from binary market question.

        Input: "Will the highest temperature in LA be between 66-67°F on May 6?"
        Output: "66-67°F"

        Input: "Will the highest temperature in LA be 57°F or below on May 6?"
        Output: "57°F or below"

        Input: "Will the highest temperature in LA be 76°F or higher on May 6?"
        Output: "76°F or higher"
        """
        # "between X-Y°F/°C"
        m = re.search(r"between\s+(\d+[-–]\d+\s*°[CF])", question, re.IGNORECASE)
        if m:
            return m.group(1).replace("–", "-")

        # "X°F/°C or below/less"
        m = re.search(r"(\d+\s*°[CF])\s+or\s+(below|less)", question, re.IGNORECASE)
        if m:
            return f"{m.group(1)} or below"

        # "X°F/°C or higher/more/above"
        m = re.search(r"(\d+\s*°[CF])\s+or\s+(higher|more|above)", question, re.IGNORECASE)
        if m:
            return f"{m.group(1)} or higher"

        # Single temp: "be X°F/°C on"
        m = re.search(r"be\s+(\d+\s*°[CF])\s+on", question, re.IGNORECASE)
        if m:
            return m.group(1)

        return None

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
        """Fetch ECMWF IFS ensemble forecast from Open-Meteo.

        Uses a semaphore to limit concurrent requests (Open-Meteo free tier).
        """
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

        # Global rate-limit short-circuit: if we hit 429 recently, skip all
        # fetches until the cooldown passes. Prevents wasted retries.
        if time.time() < self._rate_limited_until:
            return None

        try:
            async with self._forecast_semaphore:
                async with self._session.get(ENSEMBLE_API, params=params) as resp:
                    if resp.status == 429:
                        self._rate_limited_until = time.time() + self._RATE_LIMIT_BACKOFF_S
                        logger.warning(
                            "ensemble_api_rate_limited",
                            backoff_s=self._RATE_LIMIT_BACKOFF_S,
                        )
                        return None
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

        Open-Meteo returns °C. If outcomes are in °F, we convert forecast temps
        to °F before bucketing (avoids fractional °C ranges).

        Outcome format examples:
          - "57°F or below"  → all temps ≤ 57°F
          - "58-59°F"        → temps in [58, 60) °F
          - "66-67°F"        → temps in [66, 68) °F
          - "76°F or higher" → all temps ≥ 76°F
          - "17°C or below"  → all temps < 17.5°C
          - "18°C"           → temps in [17.5, 18.5)°C
        """
        n_members = len(max_temps)
        if n_members == 0:
            return {}

        # Detect unit from outcomes
        unit = "C"
        for o in outcomes:
            if "°F" in o:
                unit = "F"
                break

        # Convert forecast temps to outcome unit if needed
        if unit == "F":
            member_temps = [t * 9 / 5 + 32 for t in max_temps]  # °C → °F
        else:
            member_temps = list(max_temps)

        # Parse outcome buckets: extract temperature ranges
        parsed_buckets: list[tuple[str, float, float]] = []  # (label, low_incl, high_excl)

        for outcome in outcomes:
            lower = outcome.lower()

            if "or below" in lower or "or less" in lower or "or under" in lower or "or lower" in lower:
                temp = self._parse_outcome_temp(outcome)
                if temp is None:
                    continue
                # "57°F or below" → (-inf, 58)  (i.e. ≤57, which means <58 in integer world)
                parsed_buckets.append((outcome, -999, temp + 1 if unit == "F" else temp + 0.5))

            elif "or higher" in lower or "or more" in lower or "or above" in lower or "+" in lower:
                temp = self._parse_outcome_temp(outcome)
                if temp is None:
                    continue
                # "76°F or higher" → [76, +inf)
                parsed_buckets.append((outcome, temp, 999))

            else:
                # Range "66-67°F" or single "18°C"
                m = re.search(r"(-?\d+)\s*[-–]\s*(-?\d+)", outcome)
                if m:
                    low = float(m.group(1))
                    high = float(m.group(2))
                    # "66-67°F" → [66, 68)  (covers 66.0..67.99)
                    parsed_buckets.append((outcome, low, high + 1))
                else:
                    temp = self._parse_outcome_temp(outcome)
                    if temp is None:
                        continue
                    # Single degree: "18°C" → [17.5, 18.5)
                    parsed_buckets.append((outcome, temp - 0.5, temp + 0.5))

        if not parsed_buckets:
            return {}

        # Identify edge buckets (those with -999 or 999 bounds)
        first_is_edge = parsed_buckets[0][1] == -999  # "X or below"
        last_is_edge = parsed_buckets[-1][2] == 999   # "X or higher"

        # Count members falling into each bucket
        counts: dict[str, int] = {label: 0 for label, _, _ in parsed_buckets}
        unmatched = 0
        for temp in member_temps:
            for label, low, high in parsed_buckets:
                if low <= temp < high:
                    counts[label] += 1
                    break
            else:
                # Temp outside all buckets — only assign to edge buckets
                if temp < parsed_buckets[0][1] and first_is_edge:
                    counts[parsed_buckets[0][0]] += 1
                elif temp >= parsed_buckets[-1][2] and last_is_edge:
                    counts[parsed_buckets[-1][0]] += 1
                else:
                    # No matching bucket and no appropriate edge bucket
                    # This means the market buckets don't cover this temperature
                    unmatched += 1

        if unmatched > 0:
            logger.warning(
                "weather_unmatched_temps",
                unmatched=unmatched,
                total=n_members,
                temp_range=f"{min(member_temps):.1f}-{max(member_temps):.1f}",
                bucket_range=f"{parsed_buckets[0][1]}-{parsed_buckets[-1][2]}",
                first_is_edge=first_is_edge,
                last_is_edge=last_is_edge,
            )

        # Convert to probabilities (use total members, unmatched get 0 prob)
        return {label: count / n_members for label, count in counts.items()}

    def _parse_outcome_temp(self, outcome: str) -> float | None:
        """Extract representative temperature from outcome label.

        Returns the representative temp in the ORIGINAL unit (no conversion).
        For ranges like "66-67°F", returns the midpoint (66.5).
        For "or below"/"or higher", returns the boundary value.

        Examples:
          "18°C" → 18.0
          "72°F" → 72.0
          "66-67°F" → 66.5
          "17°C or below" → 17.0
          "-5°C" → -5.0
        """
        # Range: "66-67°F" or "20-21°C"
        m = re.search(r"(-?\d+)\s*[-–]\s*(-?\d+)\s*°\s*([CF])", outcome)
        if m:
            low = float(m.group(1))
            high = float(m.group(2))
            return (low + high) / 2

        # Single temp: "18°C", "72°F", "18 °C"
        m = re.search(r"(-?\d+)\s*°\s*([CF])", outcome)
        if m:
            return float(m.group(1))

        # Plain number fallback
        m = re.search(r"(-?\d+)", outcome)
        if m:
            return float(m.group(1))

        return None

    def _outcome_unit(self, outcome: str) -> str:
        """Detect unit from outcome label. Returns 'F' or 'C'."""
        if "°F" in outcome:
            return "F"
        return "C"

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

        # Don't buy phantom asks (price < 2¢): order book is empty or stale,
        # the bet wouldn't fill in live trading even though it "executes" in dry_run.
        if best_market_price < 0.02:
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
            condition_id=market.condition_ids[opp.best_outcome_idx],
            question=market.event_title,
            city=market.city_slug,
            outcome=opp.best_outcome_label,
            shares=round(shares, 2),
            price=opp.market_price,
            cost=round(cost, 4),
            forecast_prob=opp.forecast_prob,
            edge=opp.edge,
            target_date=market.target_date,
            outcomes=list(market.outcomes),
            unit=market.unit,
            created_at=time.time(),
            mode=self._config.mode,
            lead_days=max(0, (market.target_date - date.today()).days),
        )

        if self.should_simulate:
            await self._paper_execute(trade, opp)
        else:
            await self._live_execute(trade, opp)

    def _calculate_bet_size(self, opp: WeatherOpportunity) -> float:
        """Calculate bet size using fractional Kelly on effective bankroll.

        Effective bankroll = total bankroll - capital committed in pending trades.
        This prevents over-exposure when multiple trades are open simultaneously.
        """
        max_bet = self._config.max_bet_per_trade

        # Effective bankroll: subtract capital already committed
        committed = sum(t.cost for t in self._trades if t.status in ("pending", "confirmed"))
        effective_bankroll = max(0, self._config.bankroll - committed)

        kelly_bet = effective_bankroll * opp.kelly_fraction * self._config.kelly_multiplier

        bet = min(kelly_bet, max_bet)
        bet = max(bet, 0)

        return round(bet, 2)

    async def _paper_execute(self, trade: WeatherTrade, opp: WeatherOpportunity):
        """Simulate the trade in paper mode."""
        trade.status = "pending"
        self._trades.append(trade)
        self._trades_executed += 1
        self._save_pending_trades()

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
        """Execute real trade via CLOB.

        Re-checks the current market price before placing the order to avoid
        executing on stale data (forecast is cached 1h, prices can move).
        """
        if not self._client:
            logger.error("no_clob_client_for_weather")
            trade.status = "failed"
            self._trades.append(trade)
            return

        try:
            from py_clob_client_v2 import OrderArgs
            from py_clob_client_v2.order_builder.constants import BUY

            token_id = opp.market.token_ids[opp.best_outcome_idx]

            # Re-check current price before executing
            current_price = await self._get_current_price(token_id)
            if current_price is not None:
                actual_edge = opp.forecast_prob - current_price
                if actual_edge < self._config.min_edge:
                    logger.info(
                        "weather_edge_vanished",
                        trade_id=trade.trade_id,
                        city=trade.city,
                        original_edge=f"{opp.edge:.1%}",
                        current_edge=f"{actual_edge:.1%}",
                        original_price=f"${opp.market_price:.3f}",
                        current_price=f"${current_price:.3f}",
                    )
                    return  # Don't append to trades — edge is gone
                # Update trade with actual execution price
                trade.price = current_price
                trade.cost = round(trade.shares * current_price, 4)
                trade.edge = actual_edge

            order_args = OrderArgs(
                price=trade.price,
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
                    edge=f"{trade.edge:.1%}",
                )
            else:
                trade.status = "failed"
                logger.error("weather_order_no_id", response=str(result)[:200])

        except Exception as e:
            trade.status = "failed"
            logger.error("weather_live_error", error=str(e))

        self._trades.append(trade)
        self._trades_executed += 1
        self._save_pending_trades()

    async def _get_current_price(self, token_id: str) -> float | None:
        """Fetch current best ask price from CLOB for a token.

        The CLOB REST /book endpoint returns asks ordered worst-first (descending
        price), unlike the WebSocket which uses best-first. Compute min explicitly
        to be robust to either convention.
        """
        if not self._session:
            return None
        try:
            url = f"{CLOB_URL}/book"
            params = {"token_id": token_id}
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
            asks = data.get("asks", [])
            prices = [float(a.get("price", 0)) for a in asks if float(a.get("price", 0)) > 0]
            if prices:
                return min(prices)
        except Exception:
            pass
        return None

    # ── Resolution ───────────────────────────────────────────────────────

    async def check_resolutions(self):
        """Check if any pending trades have resolved.

        Called periodically by the strategy. Uses actual temperature data
        from Open-Meteo historical endpoint to determine winners.

        Resolution uses trade.target_date and trade.outcomes stored at execution
        time, so it works independently of self._markets (which only contains
        currently active markets and gets overwritten each scan cycle).
        """
        now = time.time()
        pending = [t for t in self._trades if t.status in ("pending", "confirmed")]
        today = date.today()

        for trade in pending:
            # Use target_date stored in the trade itself
            if trade.target_date is None:
                # Legacy trade without target_date — expire after 48h
                if now - trade.created_at > 172800:
                    trade.status = "expired"
                    trade.resolved_at = now
                continue

            if trade.target_date >= today:
                continue  # Market not yet resolved

            # Fetch actual temperature
            coords = CITY_COORDS.get(trade.city)
            if not coords:
                if now - trade.created_at > 172800:
                    trade.status = "expired"
                    trade.resolved_at = now
                continue

            actual_temp = await self._fetch_actual_temp_for_trade(
                coords[0], coords[1], coords[2], trade.target_date
            )
            if actual_temp is None:
                continue

            # Determine which outcome won using trade's stored outcomes
            winning_outcome = self._determine_winner(actual_temp, trade.outcomes)
            if winning_outcome is None:
                # If we can't determine winner and it's been >48h, expire
                if now - trade.created_at > 172800:
                    trade.status = "expired"
                    trade.resolved_at = now
                continue

            if trade.outcome == winning_outcome:
                trade.pnl = (1.0 * trade.shares) - trade.cost
                trade.status = "won"
                self._trades_won += 1
            else:
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

        # Persist: resolved trades removed from file, pending ones kept
        self._save_pending_trades()

    async def _fetch_actual_temp_for_trade(
        self, lat: float, lon: float, tz: str, target_date: date
    ) -> float | None:
        """Fetch actual recorded max temperature for a specific date and location."""
        if not self._session:
            return None

        target_str = target_date.isoformat()
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
            logger.warning("weather_actual_fetch_error", error=str(e), city_lat=lat)

        return None


    def _determine_winner(self, actual_temp_c: float, outcomes: list[str]) -> str | None:
        """Determine which outcome won given the actual temperature (°C from API).

        Converts to °F if outcomes use Fahrenheit. Uses same bucket ranges
        as _build_distribution.
        """
        # Detect unit and convert actual temp if needed
        unit = "C"
        for o in outcomes:
            if "°F" in o:
                unit = "F"
                break

        actual = actual_temp_c * 9 / 5 + 32 if unit == "F" else actual_temp_c

        for outcome in outcomes:
            lower = outcome.lower()

            if "or below" in lower or "or less" in lower or "or under" in lower or "or lower" in lower:
                temp = self._parse_outcome_temp(outcome)
                if temp is not None:
                    boundary = temp + 1 if unit == "F" else temp + 0.5
                    if actual < boundary:
                        return outcome

            elif "or higher" in lower or "or more" in lower or "or above" in lower or "+" in outcome:
                temp = self._parse_outcome_temp(outcome)
                if temp is not None and actual >= temp:
                    return outcome

            else:
                # Range "66-67°F" or single "18°C"
                m = re.search(r"(-?\d+)\s*[-–]\s*(-?\d+)", outcome)
                if m:
                    low = float(m.group(1))
                    high = float(m.group(2))
                    if low <= actual < high + 1:
                        return outcome
                else:
                    temp = self._parse_outcome_temp(outcome)
                    if temp is not None and temp - 0.5 <= actual < temp + 0.5:
                        return outcome

        return None

    # ── Stats ────────────────────────────────────────────────────────────

    # ── Trade Persistence ──────────────────────────────────────────────────

    def _save_pending_trades(self):
        """Persist trades + cumulative stats to JSON for crash recovery.

        Saves active trades (pending/confirmed) AND the most recent resolved
        trades (won/lost/expired) for empirical analysis across restarts.
        Resolved trades are capped to RESOLVED_HISTORY_CAP (most recent first)
        to avoid unbounded file growth.
        """
        TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
        active = [t for t in self._trades if t.status in ("pending", "confirmed")]
        resolved = [t for t in self._trades if t.status in ("won", "lost", "expired")]
        # Keep most recent resolved (by resolved_at, fallback to created_at)
        resolved.sort(key=lambda t: t.resolved_at or t.created_at, reverse=True)
        resolved = resolved[:500]

        records = []
        for t in active + resolved:
            records.append({
                "trade_id": t.trade_id,
                "condition_id": t.condition_id,
                "question": t.question,
                "city": t.city,
                "outcome": t.outcome,
                "shares": t.shares,
                "price": t.price,
                "cost": t.cost,
                "forecast_prob": t.forecast_prob,
                "edge": t.edge,
                "target_date": t.target_date.isoformat() if t.target_date else None,
                "outcomes": t.outcomes,
                "unit": t.unit,
                "status": t.status,
                "created_at": t.created_at,
                "resolved_at": t.resolved_at,
                "pnl": t.pnl,
                "mode": t.mode,
                "lead_days": t.lead_days,
            })

        payload = {
            "trades": records,
            "stats": {
                "total_scans": self._total_scans,
                "trades_executed": self._trades_executed,
                "trades_won": self._trades_won,
                "trades_lost": self._trades_lost,
                "total_pnl": round(self._total_pnl, 4),
                "opportunities_found": self._opportunities_found,
                "markets_found": self._markets_found,
            },
        }

        with open(TRADES_FILE, "w") as f:
            json.dump(payload, f, indent=2)

    def _load_pending_trades(self):
        """Restore pending trades and cumulative stats from disk on startup."""
        if not TRADES_FILE.exists():
            return

        try:
            with open(TRADES_FILE) as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("weather_trades_load_error", error=str(e))
            return

        # Support both old format (list) and new format (dict with trades+stats)
        if isinstance(payload, list):
            records = payload
            stats = {}
        else:
            records = payload.get("trades", [])
            stats = payload.get("stats", {})

        # Restore cumulative stats
        if stats:
            self._total_scans = stats.get("total_scans", 0)
            self._trades_executed = stats.get("trades_executed", 0)
            self._trades_won = stats.get("trades_won", 0)
            self._trades_lost = stats.get("trades_lost", 0)
            self._total_pnl = stats.get("total_pnl", 0.0)
            self._opportunities_found = stats.get("opportunities_found", 0)
            self._markets_found = stats.get("markets_found", 0)

        # Restore both pending/confirmed and resolved trades
        restored_pending = 0
        restored_resolved = 0
        for r in records:
            status = r.get("status")
            if status not in ("pending", "confirmed", "won", "lost", "expired"):
                continue
            target_date = None
            if r.get("target_date"):
                try:
                    target_date = date.fromisoformat(r["target_date"])
                except ValueError:
                    pass

            trade = WeatherTrade(
                trade_id=r["trade_id"],
                condition_id=r["condition_id"],
                question=r.get("question", ""),
                city=r["city"],
                outcome=r["outcome"],
                shares=r["shares"],
                price=r["price"],
                cost=r["cost"],
                forecast_prob=r.get("forecast_prob", 0),
                edge=r.get("edge", 0),
                target_date=target_date,
                outcomes=r.get("outcomes", []),
                unit=r.get("unit", "C"),
                status=status,
                created_at=r.get("created_at", 0),
                resolved_at=r.get("resolved_at", 0),
                pnl=r.get("pnl", 0),
                mode=r.get("mode", "paper"),
                lead_days=r.get("lead_days", 0),
            )
            self._trades.append(trade)
            if status in ("pending", "confirmed"):
                restored_pending += 1
            else:
                restored_resolved += 1

        if restored_pending > 0 or restored_resolved > 0 or stats:
            logger.info(
                "weather_state_restored",
                pending_trades=restored_pending,
                resolved_trades=restored_resolved,
                total_pnl=self._total_pnl,
                wins=self._trades_won,
                losses=self._trades_lost,
            )

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
                    "lead_days": t.lead_days,
                }
                for t in recent_trades
            ],
        }
