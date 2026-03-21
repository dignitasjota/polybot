from __future__ import annotations

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
}

# Regex to parse "Crypto Up or Down - March 21, 6:05AM-6:10AM ET"
_QUESTION_RE = re.compile(
    r"^(\w+)\s+Up or Down\s*-\s*(\w+ \d+),?\s*(\d{1,2}:\d{2}(?:AM|PM))\s*-\s*(\d{1,2}:\d{2}(?:AM|PM))\s*ET$",
    re.IGNORECASE,
)

# ET timezone offset (Eastern Time)
ET_OFFSET_STANDARD = timedelta(hours=-5)  # EST
ET_OFFSET_DST = timedelta(hours=-4)       # EDT (March-November)


def _et_to_utc(dt: datetime) -> datetime:
    """Convert naive ET datetime to UTC. Assumes EDT (March-November)."""
    return dt.replace(tzinfo=timezone(ET_OFFSET_DST)).astimezone(timezone.utc)


def parse_up_down_question(question: str, year: int | None = None) -> dict | None:
    """Parse an Up/Down market question to extract crypto, start time, end time.

    Returns dict with keys: crypto, symbol, start_utc, end_utc, or None if not parseable.
    """
    m = _QUESTION_RE.match(question)
    if not m:
        return None

    crypto_name = m.group(1).lower()
    symbol = CRYPTO_SYMBOLS.get(crypto_name)
    if not symbol:
        return None

    date_str = m.group(2)  # "March 21"
    start_time_str = m.group(3)  # "6:05AM"
    end_time_str = m.group(4)    # "6:10AM"

    if year is None:
        year = datetime.now(timezone.utc).year

    try:
        start_dt = datetime.strptime(f"{date_str} {year} {start_time_str}", "%B %d %Y %I:%M%p")
        end_dt = datetime.strptime(f"{date_str} {year} {end_time_str}", "%B %d %Y %I:%M%p")
    except ValueError:
        return None

    return {
        "crypto": crypto_name,
        "symbol": symbol,
        "start_utc": _et_to_utc(start_dt),
        "end_utc": _et_to_utc(end_dt),
    }


async def get_binance_price(symbol: str, session: aiohttp.ClientSession | None = None) -> float | None:
    """Get current price from Binance."""
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        url = f"{BINANCE_API}/ticker/price"
        async with session.get(url, params={"symbol": symbol}, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return float(data["price"])
    except Exception as e:
        logger.warning("binance_price_error", symbol=symbol, error=str(e))
        return None
    finally:
        if own_session:
            await session.close()


async def get_binance_open_price(
    symbol: str,
    start_utc: datetime,
    session: aiohttp.ClientSession | None = None,
) -> float | None:
    """Get the opening price at a specific time from Binance klines.

    Uses 1-minute klines to get the open price of the candle that contains start_utc.
    """
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
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
        logger.warning("binance_kline_error", symbol=symbol, error=str(e))
        return None
    finally:
        if own_session:
            await session.close()


class PriceChecker:
    """Verifies Up/Down market direction using Binance prices as proxy for Chainlink.

    Polymarket Up/Down markets resolve using Chainlink Data Streams (e.g. BTC/USD).
    Chainlink Data Streams require paid enterprise access, so we use Binance spot
    prices as a proxy (correlation >99.99%). The min_buffer_pct accounts for any
    small discrepancy between Binance and Chainlink.
    """

    def __init__(self, min_buffer_pct: float = 0.10):
        """
        Args:
            min_buffer_pct: Minimum price difference (%) from open to confirm direction.
                           Set to 0.10% to cover Binance-Chainlink price discrepancy.
                           Lower = more trades but higher risk of wrong direction.
                           Higher = fewer trades but more confidence.
        """
        self.min_buffer_pct = min_buffer_pct
        self._session: aiohttp.ClientSession | None = None
        # Cache open prices permanently (they never change)
        self._open_price_cache: dict[str, float] = {}  # "BTCUSDT:1234567890" -> price
        # Cache current prices with TTL to avoid hammering Binance API
        self._current_price_cache: dict[str, tuple[float, float]] = {}  # symbol -> (price, timestamp)
        self._current_price_ttl = 2.0  # seconds

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def check_direction(self, question: str) -> dict | None:
        """Check the actual crypto direction for an Up/Down market.

        Returns dict with:
            - confirmed_side: "YES" (Up) or "NO" (Down) or None if uncertain
            - current_price: float
            - open_price: float
            - change_pct: float (percentage change from open)
            - symbol: str

        Returns None if the question is not an Up/Down market or prices unavailable.
        """
        parsed = parse_up_down_question(question)
        if not parsed:
            return None

        session = await self._get_session()
        symbol = parsed["symbol"]
        start_utc = parsed["start_utc"]

        # Get open price (cached)
        cache_key = f"{symbol}:{int(start_utc.timestamp())}"
        open_price = self._open_price_cache.get(cache_key)
        if open_price is None:
            open_price = await get_binance_open_price(symbol, start_utc, session)
            if open_price is None:
                return None
            self._open_price_cache[cache_key] = open_price

        # Get current price (cached for 2s to avoid rate limits)
        cached = self._current_price_cache.get(symbol)
        now = time.time()
        if cached and (now - cached[1]) < self._current_price_ttl:
            current_price = cached[0]
        else:
            current_price = await get_binance_price(symbol, session)
            if current_price is None:
                return None
            self._current_price_cache[symbol] = (current_price, now)

        # Calculate direction
        change_pct = ((current_price - open_price) / open_price) * 100

        if abs(change_pct) < self.min_buffer_pct:
            confirmed_side = None  # Too close to call
        elif change_pct > 0:
            confirmed_side = "YES"  # Up
        else:
            confirmed_side = "NO"  # Down

        result = {
            "confirmed_side": confirmed_side,
            "current_price": current_price,
            "open_price": open_price,
            "change_pct": round(change_pct, 4),
            "symbol": symbol,
        }

        logger.debug(
            "price_check",
            symbol=symbol,
            open_price=open_price,
            current_price=current_price,
            change_pct=f"{change_pct:.4f}%",
            confirmed_side=confirmed_side,
        )

        return result
