from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone, timedelta

import aiohttp
import structlog

logger = structlog.get_logger("polymarket.price_checker")

BINANCE_API = "https://api.binance.com/api/v3"

# Map crypto names from Polymarket questions to Binance symbols
CRYPTO_SYMBOLS = {
    "bitcoin": "BTCUSDT",
    "ethereum": "ETHUSDT",
    "solana": "SOLUSDT",
    "bnb": "BNBUSDT",
    "dogecoin": "DOGEUSDT",
    "xrp": "XRPUSDT",
    "cardano": "ADAUSDT",
    "avalanche": "AVAXUSDT",
    "polkadot": "DOTUSDT",
    "polygon": "MATICUSDT",
    "chainlink": "LINKUSDT",
    "litecoin": "LTCUSDT",
    "sui": "SUIUSDT",
    "pepe": "PEPEUSDT",
    # Note: Hyperliquid (HYPE) is NOT on Binance — these markets
    # will fall back to order-book-only logic (no price verification)
}

# Per-crypto min buffer % for directional confirmation.
# These are FALLBACK values — actual values come from config.toml [strategy.crypto_configs]
# Less volatile cryptos need higher buffer to avoid noise.
# More volatile cryptos can use lower buffer to capture more trades.
CRYPTO_BUFFER_PCT = {
    "bitcoin": 0.0005,     # 0.05% — baja volatilidad en 5min
    "ethereum": 0.0008,    # 0.08%
    "bnb": 0.0008,         # 0.08%
    "litecoin": 0.001,     # 0.1%
    "cardano": 0.001,      # 0.1%
    "polkadot": 0.001,     # 0.1%
    "chainlink": 0.001,    # 0.1%
    "polygon": 0.001,      # 0.1%
    "avalanche": 0.001,    # 0.1%
    "xrp": 0.001,          # 0.1%
    "solana": 0.001,       # 0.1%
    "sui": 0.0012,         # 0.12% — alta volatilidad
    "dogecoin": 0.0015,    # 0.15% — meme coin
    "pepe": 0.0015,        # 0.15% — meme coin
}

# Regex to parse "Crypto Up or Down - March 21, 6:05AM-6:10AM ET"
_QUESTION_RE = re.compile(
    r"^(\w+)\s+Up or Down\s*-\s*(\w+ \d+),?\s*(\d{1,2}(?::\d{2})?(?:AM|PM))\s*-\s*(\d{1,2}(?::\d{2})?(?:AM|PM))\s*ET$",
    re.IGNORECASE,
)

# Regex for hourly format: "Crypto Up or Down - March 21, 6AM ET"
_QUESTION_HOURLY_RE = re.compile(
    r"^(\w+)\s+Up or Down\s*-\s*(\w+ \d+),?\s*(\d{1,2}(?::\d{2})?(?:AM|PM))\s*ET$",
    re.IGNORECASE,
)

# ET timezone offset (Eastern Time)
ET_OFFSET_STANDARD = timedelta(hours=-5)  # EST
ET_OFFSET_DST = timedelta(hours=-4)       # EDT (March-November)


def _et_to_utc(dt: datetime) -> datetime:
    """Convert naive ET datetime to UTC. Assumes EDT (March-November)."""
    return dt.replace(tzinfo=timezone(ET_OFFSET_DST)).astimezone(timezone.utc)


def _parse_time_str(date_str: str, year: int, time_str: str) -> datetime | None:
    """Parse a time string that may or may not have minutes (e.g., '6AM' or '6:05AM')."""
    for fmt in ("%B %d %Y %I:%M%p", "%B %d %Y %I%p"):
        try:
            return datetime.strptime(f"{date_str} {year} {time_str}", fmt)
        except ValueError:
            continue
    return None


def parse_up_down_question(question: str, year: int | None = None) -> dict | None:
    """Parse an Up/Down market question to extract crypto, start time, end time.

    Supports formats:
    - "Bitcoin Up or Down - March 21, 6:05AM-6:10AM ET" (range)
    - "Bitcoin Up or Down - March 21, 6AM ET" (hourly — start=6AM, end=7AM)

    Returns dict with keys: crypto, symbol, start_utc, end_utc, or None if not parseable.
    """
    if year is None:
        year = datetime.now(timezone.utc).year

    # Try range format first: "6:05AM-6:10AM ET"
    m = _QUESTION_RE.match(question)
    if m:
        crypto_name = m.group(1).lower()
        symbol = CRYPTO_SYMBOLS.get(crypto_name)
        if not symbol:
            return None

        date_str = m.group(2)
        start_dt = _parse_time_str(date_str, year, m.group(3))
        end_dt = _parse_time_str(date_str, year, m.group(4))
        if not start_dt or not end_dt:
            return None

        return {
            "crypto": crypto_name,
            "symbol": symbol,
            "start_utc": _et_to_utc(start_dt),
            "end_utc": _et_to_utc(end_dt),
        }

    # Try hourly format: "6AM ET" (implies 1-hour window)
    m = _QUESTION_HOURLY_RE.match(question)
    if m:
        crypto_name = m.group(1).lower()
        symbol = CRYPTO_SYMBOLS.get(crypto_name)
        if not symbol:
            return None

        date_str = m.group(2)
        start_dt = _parse_time_str(date_str, year, m.group(3))
        if not start_dt:
            return None
        end_dt = start_dt + timedelta(hours=1)

        return {
            "crypto": crypto_name,
            "symbol": symbol,
            "start_utc": _et_to_utc(start_dt),
            "end_utc": _et_to_utc(end_dt),
        }

    return None


async def _fetch_binance_price(symbol: str, session: aiohttp.ClientSession) -> float | None:
    """Get current price from Binance."""
    try:
        url = f"{BINANCE_API}/ticker/price"
        async with session.get(url, params={"symbol": symbol}, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return float(data["price"])
    except Exception as e:
        logger.warning("binance_price_error", symbol=symbol, error=str(e), error_type=type(e).__name__)
        return None


async def _fetch_binance_open_price(
    symbol: str,
    start_utc: datetime,
    session: aiohttp.ClientSession,
) -> float | None:
    """Get the opening price at a specific time from Binance klines."""
    try:
        start_ms = int(start_utc.timestamp() * 1000)
        url = f"{BINANCE_API}/klines"
        params = {
            "symbol": symbol,
            "interval": "1m",
            "startTime": start_ms,
            "limit": 1,
        }
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            if not data:
                return None
            # Kline format: [open_time, open, high, low, close, ...]
            return float(data[0][1])  # open price
    except Exception as e:
        logger.warning("binance_kline_error", symbol=symbol, error=str(e), error_type=type(e).__name__)
        return None


class PriceChecker:
    """Verifies Up/Down market direction using Binance prices as proxy for Chainlink.

    Prices are fetched in a background loop every 1 second and cached.
    check_direction() reads from cache only — zero I/O, zero latency.
    """

    def __init__(self, min_buffer_pct: float = 0.10, poll_interval: float = 2.0,
                 crypto_configs: dict | None = None):
        self.min_buffer_pct = min_buffer_pct
        self._poll_interval = poll_interval
        self._crypto_configs = crypto_configs or {}  # name -> CryptoDirectionalConfig
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._bg_task: asyncio.Task | None = None
        # Cache parsed question results to avoid regex on every WS message
        self._parse_cache: dict[str, dict | None] = {}

        # Caches (read by check_direction, written by background loop)
        self._current_prices: dict[str, float] = {}       # symbol -> price
        self._open_prices: dict[str, float] = {}           # "BTCUSDT:timestamp" -> open price
        self._active_symbols: set[str] = set()             # symbols to poll
        self._pending_open_requests: dict[str, datetime] = {}  # cache_key -> start_utc
        self._last_update: float = 0.0

    async def start(self):
        """Start the background price polling loop."""
        if self._running:
            return
        self._running = True
        self._session = aiohttp.ClientSession()
        self._bg_task = asyncio.create_task(self._poll_loop())
        logger.info("price_checker_started", poll_interval=self._poll_interval)

    async def close(self):
        """Stop background polling and close session."""
        self._running = False
        if self._bg_task:
            self._bg_task.cancel()
            try:
                await self._bg_task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()

    async def _poll_loop(self):
        """Background loop: fetch current prices for all active symbols."""
        while self._running:
            try:
                await self._update_prices()
            except Exception as e:
                logger.warning("price_poll_error", error=str(e))
            await asyncio.sleep(self._poll_interval)

    async def _update_prices(self):
        """Fetch current prices for all active symbols in parallel."""
        if not self._session or not self._active_symbols:
            return

        # Fetch all current prices in parallel
        tasks = {
            symbol: asyncio.create_task(_fetch_binance_price(symbol, self._session))
            for symbol in self._active_symbols
        }
        for symbol, task in tasks.items():
            price = await task
            if price is not None:
                self._current_prices[symbol] = price

        # Fetch open prices for markets whose start time has passed + 5s buffer
        now_utc = datetime.now(timezone.utc)
        for cache_key, start_utc in list(self._pending_open_requests.items()):
            # Wait 5s after window start for Binance to finalize the kline
            if start_utc + timedelta(seconds=5) <= now_utc:
                symbol = cache_key.split(":")[0]
                open_price = await _fetch_binance_open_price(symbol, start_utc, self._session)
                if open_price is not None:
                    self._open_prices[cache_key] = open_price
                    logger.info(
                        "binance_open_price_fetched",
                        symbol=symbol,
                        open_price=open_price,
                        cache_key=cache_key,
                    )
                    del self._pending_open_requests[cache_key]
                else:
                    # Fallback: if 2+ minutes passed and kline still unavailable, use current price
                    if start_utc + timedelta(minutes=2) <= now_utc:
                        fallback_price = self._current_prices.get(symbol)
                        if fallback_price is not None:
                            self._open_prices[cache_key] = fallback_price
                            logger.warning(
                                "binance_open_price_fallback",
                                symbol=symbol,
                                fallback_price=fallback_price,
                            )
                            del self._pending_open_requests[cache_key]

        self._last_update = time.time()

    def check_direction(self, question: str) -> dict | None:
        """Check the actual crypto direction for an Up/Down market.

        This method is SYNCHRONOUS and reads from cache only — no I/O.
        Returns None if the question is not an Up/Down market or prices not yet cached.
        """
        if question in self._parse_cache:
            parsed = self._parse_cache[question]
        else:
            parsed = parse_up_down_question(question)
            self._parse_cache[question] = parsed
            # Prevent unbounded growth
            if len(self._parse_cache) > 500:
                self._parse_cache.clear()
        if not parsed:
            return None

        symbol = parsed["symbol"]
        start_utc = parsed["start_utc"]

        # Register symbol for background polling
        self._active_symbols.add(symbol)

        # Get current price from cache first
        current_price = self._current_prices.get(symbol)
        if current_price is None:
            return None

        # Get open price from cache (real Binance kline at window start)
        cache_key = f"{symbol}:{int(start_utc.timestamp())}"
        open_price = self._open_prices.get(cache_key)
        if open_price is None:
            # Register for background fetching (will fetch once start_utc + 5s has passed)
            if cache_key not in self._pending_open_requests:
                self._pending_open_requests[cache_key] = start_utc
            # Don't confirm anything yet — wait for real open price
            return None

        # Calculate direction (as decimal, e.g. 0.05 = 5%)
        change_pct = (current_price - open_price) / open_price

        # Check if this crypto is disabled via per-crypto config
        crypto_name = parsed["crypto"]
        cc = self._crypto_configs.get(crypto_name)
        if cc is not None and not cc.enabled:
            return {"confirmed_side": None, "current_price": current_price,
                    "open_price": open_price, "change_pct": 0.0,
                    "symbol": symbol, "crypto": crypto_name,
                    "buffer_used": 0, "disabled": True}

        # Use per-crypto buffer: config > hardcoded > global fallback
        if cc is not None:
            buffer = cc.buffer_pct
        else:
            buffer = CRYPTO_BUFFER_PCT.get(crypto_name, self.min_buffer_pct)

        if abs(change_pct) < buffer:
            confirmed_side = None  # Too close to call
        elif change_pct > 0:
            confirmed_side = "YES"  # Up
        else:
            confirmed_side = "NO"  # Down

        return {
            "confirmed_side": confirmed_side,
            "current_price": current_price,
            "open_price": open_price,
            "change_pct": round(change_pct, 4),
            "symbol": symbol,
            "crypto": crypto_name,
            "buffer_used": buffer,
        }
