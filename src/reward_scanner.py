"""Reward Scanner — Phase 1 of Liquidity Strategy.

Scans Polymarket CLOB API for markets with active liquidity rewards,
ranks them by reward_per_dollar / risk, and provides a sorted list of
the most profitable markets to quote.

Endpoints used (no auth required):
  GET /rewards/markets/current  — all active reward configs
  GET /rewards/markets/multi    — markets with filtering/sorting
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp
import structlog

CLOB_BASE_URL = "https://clob.polymarket.com"

logger = structlog.get_logger("polymarket.reward_scanner")


@dataclass
class RewardMarket:
    """A market with active liquidity rewards, scored for profitability."""

    condition_id: str
    question: str
    market_slug: str
    event_slug: str = ""

    # Tokens
    tokens: list[dict] = field(default_factory=list)
    yes_price: float = 0.0
    no_price: float = 0.0

    # Reward params
    daily_rate: float = 0.0          # Total $/day in rewards
    max_spread: float = 0.0          # Max spread in centavos for scoring
    min_size: float = 0.0            # Min order size for scoring
    reward_end_date: str = ""

    # Market metrics
    competitiveness: float = 0.0     # 0-1, how competitive (higher = more competition)
    volume_24h: float = 0.0
    spread: float = 0.0              # Current spread

    # Computed
    reward_per_dollar: float = 0.0   # daily_rate / competitiveness_dollars
    score: float = 0.0               # Final ranking score
    midpoint: float = 0.5

    def to_dict(self) -> dict:
        return {
            "condition_id": self.condition_id,
            "question": self.question,
            "market_slug": self.market_slug,
            "daily_rate": round(self.daily_rate, 2),
            "max_spread": self.max_spread,
            "min_size": self.min_size,
            "competitiveness": round(self.competitiveness, 4),
            "volume_24h": round(self.volume_24h, 2),
            "spread": round(self.spread, 4),
            "yes_price": round(self.yes_price, 3),
            "no_price": round(self.no_price, 3),
            "midpoint": round(self.midpoint, 3),
            "reward_per_dollar": round(self.reward_per_dollar, 6),
            "score": round(self.score, 4),
            "reward_end_date": self.reward_end_date,
        }


class RewardScanner:
    """Scans and ranks Polymarket markets by liquidity reward profitability.

    Usage:
        scanner = RewardScanner()
        await scanner.start()
        top = scanner.get_top_markets(n=10)
        await scanner.close()
    """

    def __init__(
        self,
        session: aiohttp.ClientSession | None = None,
        scan_interval: float = 300.0,
        min_daily_rate: float = 1.0,
        min_reward_per_dollar: float = 0.5,
        capital_per_market: float = 50.0,
        max_min_size: float = 0.0,
    ):
        self._session = session
        self._owns_session = session is None
        self._scan_interval = scan_interval
        self._min_daily_rate = min_daily_rate
        self._min_reward_per_dollar = min_reward_per_dollar
        self._capital_per_market = capital_per_market
        self._max_min_size = max_min_size

        self._markets: list[RewardMarket] = []
        self._last_scan: float = 0.0
        self._scan_count: int = 0
        self._scan_errors: int = 0
        self._running: bool = False
        self._scan_task: asyncio.Task | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def start(self):
        """Start background scanning loop."""
        if self._running:
            return
        self._running = True
        # Initial scan immediately
        await self.scan()
        # Start periodic scan
        self._scan_task = asyncio.create_task(self._scan_loop())
        logger.info("reward_scanner_started", interval=self._scan_interval)

    async def stop(self):
        """Stop scanning."""
        self._running = False
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
        self._scan_task = None

    async def close(self):
        """Stop and close session."""
        await self.stop()
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def _scan_loop(self):
        while self._running:
            await asyncio.sleep(self._scan_interval)
            if not self._running:
                break
            try:
                await self.scan()
            except Exception as e:
                logger.error("scan_loop_error", error=str(e))
                self._scan_errors += 1

    async def scan(self) -> list[RewardMarket]:
        """Fetch reward markets from CLOB API, rank them, update internal state."""
        try:
            raw_markets = await self._fetch_reward_markets()
            scored = self._rank_markets(raw_markets)
            self._markets = scored
            self._last_scan = time.time()
            self._scan_count += 1
            logger.info(
                "scan_complete",
                markets_found=len(raw_markets),
                markets_scored=len(scored),
                top_score=round(scored[0].score, 4) if scored else 0,
            )
            return scored
        except Exception as e:
            logger.error("scan_failed", error=str(e))
            self._scan_errors += 1
            return self._markets

    async def _fetch_reward_markets(self) -> list[RewardMarket]:
        """Fetch all active reward markets from /rewards/markets/multi.

        Uses /multi as primary source (has all fields: question, tokens,
        volume, spread, competitiveness). Paginates through all results.
        """
        session = await self._get_session()
        markets: list[RewardMarket] = []
        cursor = ""

        while True:
            params: dict = {
                "page_size": 500,
                "order_by": "rate_per_day",
                "position": "DESC",
            }
            if cursor:
                params["next_cursor"] = cursor

            url = f"{CLOB_BASE_URL}/rewards/markets/multi"
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        logger.warning("rewards_api_error", status=resp.status, url=url)
                        break
                    data = await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning("rewards_api_request_failed", error=str(e))
                break

            for item in data.get("data", []):
                market = self._parse_multi_market(item)
                if market and market.daily_rate >= self._min_daily_rate:
                    markets.append(market)

            cursor = data.get("next_cursor", "")
            if not cursor or cursor == "LTE=":
                break

        return markets

    def _parse_multi_market(self, item: dict) -> RewardMarket | None:
        """Parse a single market from /rewards/markets/multi response."""
        condition_id = item.get("condition_id", "")
        if not condition_id:
            return None

        max_spread = float(item.get("rewards_max_spread", 0) or 0)
        min_size = float(item.get("rewards_min_size", 0) or 0)

        if max_spread <= 0:
            return None

        # Aggregate daily rate from reward configs
        daily_rate = 0.0
        reward_end = ""
        for rc in item.get("rewards_config", []):
            daily_rate += float(rc.get("rate_per_day", 0) or 0)
            reward_end = rc.get("end_date", reward_end)

        # Parse tokens and prices
        yes_price = 0.0
        no_price = 0.0
        tokens = item.get("tokens", [])
        for tok in tokens:
            outcome = tok.get("outcome", "").lower()
            price = float(tok.get("price", 0) or 0)
            if outcome == "yes":
                yes_price = price
            elif outcome == "no":
                no_price = price

        # Fallback: non-yes/no outcomes → use first two tokens
        if not yes_price and not no_price and len(tokens) >= 2:
            yes_price = float(tokens[0].get("price", 0) or 0)
            no_price = float(tokens[1].get("price", 0) or 0)
        # If only one side found, derive the other (binary market: yes + no = 1)
        elif yes_price and not no_price:
            no_price = 1.0 - yes_price
        elif no_price and not yes_price:
            yes_price = 1.0 - no_price

        # Midpoint = YES price (NOT average of yes+no, which is always 0.5)
        # The provider quotes around this price: bid slightly below, ask slightly above
        midpoint = yes_price if yes_price > 0 else 0.5

        return RewardMarket(
            condition_id=condition_id,
            question=item.get("question", condition_id[:16]),
            market_slug=item.get("market_slug", ""),
            event_slug=item.get("event_slug", ""),
            tokens=tokens,
            yes_price=yes_price,
            no_price=no_price,
            daily_rate=daily_rate,
            max_spread=max_spread,
            min_size=min_size,
            reward_end_date=reward_end,
            competitiveness=float(item.get("market_competitiveness", 0) or 0),
            volume_24h=float(item.get("volume_24hr", 0) or 0),
            spread=float(item.get("spread", 0) or 0),
            midpoint=midpoint,
        )

    def _rank_markets(self, markets: list[RewardMarket]) -> list[RewardMarket]:
        """Score and sort markets by reward capture potential WITH fill safety.

        Key insight: we want rewards WITHOUT fills. This means:
        - Moderate competition = GOOD (others absorb flow, we hide behind them)
        - Zero competition = BAD (we ARE the book, all fills come to us)
        - Wide natural spread = BAD (our orders are top-of-book → certain fills)
        - Tight natural spread = GOOD (others quote tighter, we're protected)

        Score formula:
            reward_per_dollar = daily_rate / max(competitiveness, 1)
            comp_factor = bonus for moderate competition (shields from fills)
            spread_penalty = penalty for wide natural spread (top-of-book risk)
            risk_factor = penalty for extreme midpoints
            score = reward_per_dollar * comp_factor / (risk_factor * spread_penalty)
        """
        for m in markets:
            # competitiveness is in USD — 0 means nobody is quoting
            m.reward_per_dollar = m.daily_rate / max(m.competitiveness, 1.0)

            # Competition factor: moderate competition = SHIELDS from fills
            # With 0 competition, ALL adverse flow hits our orders
            # With moderate competition, others absorb flow before us
            if m.competitiveness <= 0:
                comp_factor = 0.3  # NOBODY else → we ARE the book → fills certain
            elif m.competitiveness < 1.0:
                comp_factor = 0.5  # Almost no shield
            elif m.competitiveness < 5.0:
                comp_factor = 1.0  # Good: others absorb some flow
            elif m.competitiveness < 20.0:
                comp_factor = 1.3  # Better: more shields, still decent reward share
            elif m.competitiveness < 100.0:
                comp_factor = 1.0  # Good shields but lower reward share
            else:
                comp_factor = 0.5  # Too much competition → negligible rewards

            # Spread penalty: wide natural spread → our orders are top-of-book
            # If market spread > our quoting distance, we're the best price → fills
            # Our distance from mid ≈ max_spread * 0.85 (in centavos)
            our_distance = (m.max_spread / 100.0) * 0.85  # Convert centavos to price
            if m.spread > 0 and m.spread > our_distance * 2:
                # Natural spread MUCH wider than our quotes → certain fills
                spread_penalty = 5.0
            elif m.spread > 0 and m.spread > our_distance:
                # Wider than us → likely fills
                spread_penalty = 2.5
            elif m.spread > 0.05:
                # 5¢+ spread → somewhat risky
                spread_penalty = 1.5
            else:
                # Tight spread → others quote tighter, we're safe
                spread_penalty = 1.0

            # Risk factor: extreme midpoints (near 0 or 1)
            mid_distance = abs(m.midpoint - 0.5)
            if mid_distance > 0.35:
                risk_factor = 5.0
            elif mid_distance > 0.25:
                risk_factor = 2.5
            else:
                risk_factor = 1.0 + mid_distance

            # Volume factor: HIGH volume = more flow = more fills risk
            # LOW volume = less flow = safer (opposite of before)
            volume_factor = 1.0
            if m.volume_24h > 50000:
                volume_factor = 0.5  # Very high volume → lots of aggressive orders
            elif m.volume_24h > 10000:
                volume_factor = 0.7  # High volume → risky
            elif m.volume_24h < 500:
                volume_factor = 1.2  # Low volume → less fill risk

            m.score = (m.reward_per_dollar * comp_factor * volume_factor) / (risk_factor * spread_penalty)

        # Filter by minimum reward_per_dollar, competitiveness, volume, and max_min_size
        # MIN COMPETITIVENESS = $1.0: avoid being sole provider (generates fills)
        # MAX VOLUME = 8000: avoid ultra high-flow markets (WTI 12k, David Bailey 4-12k)
        #               but allow moderate-flow markets (3-7k vol)
        scored = [m for m in markets if m.reward_per_dollar >= self._min_reward_per_dollar]
        scored = [m for m in scored if m.competitiveness >= 1.0]  # Hard filter: need rivals
        scored = [m for m in scored if m.volume_24h < 8000]  # Hard filter: avoid ultra high-flow
        if self._max_min_size > 0:
            scored = [m for m in scored if m.min_size <= self._max_min_size]

        # Sort by score descending
        scored.sort(key=lambda m: m.score, reverse=True)

        return scored

    def get_top_markets(self, n: int = 10) -> list[RewardMarket]:
        """Get top N markets by score."""
        return self._markets[:n]

    def get_all_markets(self) -> list[RewardMarket]:
        """Get all scored markets."""
        return list(self._markets)

    def get_stats(self) -> dict:
        """Return scanner stats for dashboard/API."""
        top5 = self._markets[:5]
        return {
            "total_reward_markets": len(self._markets),
            "last_scan": self._last_scan,
            "last_scan_ago": round(time.time() - self._last_scan, 1) if self._last_scan else None,
            "scan_count": self._scan_count,
            "scan_errors": self._scan_errors,
            "scan_interval": self._scan_interval,
            "top_markets": [m.to_dict() for m in top5],
            "total_daily_rewards_available": round(
                sum(m.daily_rate for m in self._markets), 2
            ),
        }

    def export_report(self) -> dict:
        """Full report for API endpoint."""
        return {
            "scan_count": self._scan_count,
            "scan_errors": self._scan_errors,
            "last_scan": self._last_scan,
            "total_markets": len(self._markets),
            "config": {
                "scan_interval": self._scan_interval,
                "min_daily_rate": self._min_daily_rate,
                "min_reward_per_dollar": self._min_reward_per_dollar,
                "capital_per_market": self._capital_per_market,
                "max_min_size": self._max_min_size,
            },
            "markets": [m.to_dict() for m in self._markets],
        }
