from __future__ import annotations

import asyncio
import json as json_lib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import aiohttp
import structlog

GAMMA_API_URL = "https://gamma-api.polymarket.com"

logger = structlog.get_logger("polymarket.gamma")


@dataclass
class Market:
    """Represents a Polymarket market relevant for closing arbitrage."""

    condition_id: str
    question: str
    slug: str
    yes_token_id: str
    no_token_id: str
    end_date: datetime | None
    active: bool
    closed: bool
    enable_order_book: bool
    yes_price: float
    no_price: float
    resolved: bool = False
    winning_token_id: str = ""
    tags: list[str] = field(default_factory=list)
    fee_rate: float = -1.0       # From feeSchedule.rate (-1 = not available)
    fee_exponent: int = 1        # From feeSchedule.exponent (always 1 so far)

    @property
    def best_token_price(self) -> float:
        return max(self.yes_price, self.no_price)

    @property
    def best_token_side(self) -> str:
        return "YES" if self.yes_price >= self.no_price else "NO"

    @property
    def best_token_id(self) -> str:
        return self.yes_token_id if self.yes_price >= self.no_price else self.no_token_id

    @property
    def time_to_resolution(self) -> timedelta | None:
        if self.end_date is None:
            return None
        return self.end_date - datetime.now(timezone.utc)


class GammaClient:
    """Client for the Polymarket Gamma API (market discovery)."""

    def __init__(self, session: aiohttp.ClientSession | None = None):
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close(self):
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _passes_fee_filter(market: Market, max_fee_rate: float | None) -> bool:
        """True if the market's fee rate qualifies under max_fee_rate.

        Unknown fee rates (-1) pass — the consumer's own fee gate makes the
        final call with its category fallback.
        """
        if max_fee_rate is None:
            return True
        if market.fee_rate < 0:
            return True
        return market.fee_rate <= max_fee_rate

    async def fetch_active_markets(
        self,
        max_time_to_resolution: timedelta = timedelta(hours=24),
        max_results: int = 50,
        tag: str = "",
        max_fee_rate: float | None = None,
        max_pages: int = 50,
    ) -> list[Market]:
        """Fetch active markets closing within max_time_to_resolution.

        Uses keyset pagination (GET /markets/keyset) with cursor-based paging.
        Orders by endDate ascending to get soonest-closing markets first.
        Filters: enableOrderBook=true, has liquidity (price > 0).

        max_fee_rate: if set, markets with a KNOWN fee rate above it are
        skipped DURING pagination (they don't consume max_results slots).
        This matters because endDate-ascending ordering front-loads 5-min
        crypto markets — without the filter they exhaust the quota before
        low-fee categories (geopolitics/politics, days away) ever appear.
        max_pages bounds the extra paging this can trigger.
        """
        session = await self._get_session()
        markets: list[Market] = []
        skipped_no_book = 0
        skipped_no_liquidity = 0
        skipped_high_fee = 0
        pages = 0
        page_size = min(100, max_results)
        now = datetime.now(timezone.utc)
        end_max = now + max_time_to_resolution
        after_cursor: str | None = None

        while len(markets) < max_results and pages < max_pages:
            pages += 1
            params: dict[str, str] = {
                "active": "true",
                "closed": "false",
                "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_date_max": end_max.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "order": "endDate",
                "ascending": "true",
                "limit": str(page_size),
            }
            if tag:
                params["tag"] = tag
            if after_cursor:
                params["after_cursor"] = after_cursor

            try:
                async with session.get(
                    f"{GAMMA_API_URL}/markets/keyset", params=params
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "gamma_api_error",
                            status=resp.status,
                            body=await resp.text(),
                        )
                        break

                    data = await resp.json()

                    items = data.get("markets", []) if isinstance(data, dict) else data
                    if not items:
                        break

                    for item in items:
                        market = self._parse_market(item)
                        if market is None:
                            continue
                        # Skip markets without order book
                        if not market.enable_order_book:
                            skipped_no_book += 1
                            continue
                        # Skip markets with no liquidity (both sides at 0)
                        if market.yes_price <= 0 and market.no_price <= 0:
                            skipped_no_liquidity += 1
                            continue
                        # Skip high-fee markets so they don't consume slots
                        if not self._passes_fee_filter(market, max_fee_rate):
                            skipped_high_fee += 1
                            continue
                        markets.append(market)

                    # Keyset pagination: next_cursor absent = last page
                    next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
                    if not next_cursor:
                        break
                    after_cursor = next_cursor

            except aiohttp.ClientError as e:
                logger.error("gamma_api_connection_error", error=str(e))
                break

        # Trim to max_results
        markets = markets[:max_results]

        logger.info(
            "gamma_markets_fetched",
            total_found=len(markets),
            skipped_no_book=skipped_no_book,
            skipped_no_liquidity=skipped_no_liquidity,
            skipped_high_fee=skipped_high_fee,
            pages=pages,
            max_time_to_resolution=str(max_time_to_resolution),
        )
        return markets

    async def check_resolution(self, condition_ids: list[str]) -> dict[str, str]:
        """Check if markets have resolved via CLOB REST API.

        Uses GET https://clob.polymarket.com/markets/{condition_id} which returns
        authoritative resolution data with a `tokens` array where each token has
        a `winner` boolean field.

        Returns dict of condition_id -> winning_token_id for resolved markets.
        """
        if not condition_ids:
            return {}

        session = await self._get_session()
        resolved: dict[str, str] = {}

        for condition_id in condition_ids:
            try:
                # Use CLOB REST API — authoritative source for resolution
                url = f"https://clob.polymarket.com/markets/{condition_id}"
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        logger.debug(
                            "clob_resolution_check_status",
                            condition_id=condition_id[:16],
                            status=resp.status,
                        )
                        continue
                    data = await resp.json()
                    if not data:
                        continue

                    logger.debug(
                        "clob_resolution_response",
                        condition_id=condition_id[:16],
                        closed=data.get("closed"),
                        active=data.get("active"),
                        has_tokens=bool(data.get("tokens")),
                    )

                    # CLOB API: check if market is closed/inactive
                    is_closed = data.get("closed", False)
                    is_active = data.get("active", True)

                    if not is_closed and is_active:
                        continue

                    # Look for winner in the tokens array
                    # Each token has: token_id, outcome ("Yes"/"No"), winner (bool)
                    tokens = data.get("tokens", [])
                    winning_token_id = ""

                    for token in tokens:
                        if token.get("winner") is True:
                            winning_token_id = token.get("token_id", "")
                            break

                    if winning_token_id:
                        resolved[condition_id] = winning_token_id
                        logger.info(
                            "clob_resolution_detected",
                            condition_id=condition_id[:16],
                            winning_token=winning_token_id[:16],
                            question=data.get("question", "")[:60],
                        )
                    elif is_closed:
                        # Market closed but no winner found yet — log for debugging
                        logger.debug(
                            "clob_market_closed_no_winner",
                            condition_id=condition_id[:16],
                            tokens_count=len(tokens),
                            token_details=[
                                {"outcome": t.get("outcome"), "winner": t.get("winner")}
                                for t in tokens
                            ],
                        )

            except (aiohttp.ClientError, asyncio.TimeoutError, json_lib.JSONDecodeError) as e:
                logger.debug("clob_resolution_check_error", condition_id=condition_id[:16], error=str(e))
                continue

        return resolved

    def _parse_market(self, data: dict) -> Market | None:
        """Parse a market from the Gamma API response.

        The Gamma API returns:
        - clobTokenIds: JSON string array ["yes_token_id", "no_token_id"]
        - outcomePrices: JSON string array ["yes_price", "no_price"]
        - outcomes: JSON string array ["Yes", "No"]
        """
        try:
            # Parse JSON string fields
            clob_token_ids_raw = data.get("clobTokenIds", "[]")
            if isinstance(clob_token_ids_raw, str):
                clob_token_ids = json_lib.loads(clob_token_ids_raw)
            else:
                clob_token_ids = clob_token_ids_raw or []

            if len(clob_token_ids) < 2:
                return None

            outcome_prices_raw = data.get("outcomePrices", "[]")
            if isinstance(outcome_prices_raw, str):
                outcome_prices = json_lib.loads(outcome_prices_raw)
            else:
                outcome_prices = outcome_prices_raw or []

            outcomes_raw = data.get("outcomes", "[]")
            if isinstance(outcomes_raw, str):
                outcomes = json_lib.loads(outcomes_raw)
            else:
                outcomes = outcomes_raw or []

            # Map outcomes to token IDs and prices
            # Standard: [Yes, No] — but 5-min crypto markets use [Up, Down]
            yes_idx = None
            no_idx = None
            for i, outcome in enumerate(outcomes):
                low = outcome.lower()
                if low in ("yes", "up"):
                    yes_idx = i
                elif low in ("no", "down"):
                    no_idx = i

            if yes_idx is None or no_idx is None:
                # Fallback: if exactly 2 outcomes, use positional [0]=yes, [1]=no
                if len(outcomes) == 2:
                    yes_idx = 0
                    no_idx = 1
                else:
                    return None

            yes_token_id = clob_token_ids[yes_idx]
            no_token_id = clob_token_ids[no_idx]

            yes_price = float(outcome_prices[yes_idx]) if len(outcome_prices) > yes_idx else 0.0
            no_price = float(outcome_prices[no_idx]) if len(outcome_prices) > no_idx else 0.0

            # Parse end date
            end_date_str = data.get("endDate") or data.get("end_date_iso")
            end_date = None
            if end_date_str:
                end_date_str = end_date_str.replace("Z", "+00:00")
                end_date = datetime.fromisoformat(end_date_str)

            enable_order_book = data.get("enableOrderBook", False)
            if isinstance(enable_order_book, str):
                enable_order_book = enable_order_book.lower() == "true"

            # Parse tags (Gamma returns "tags" as list of dicts with "label" or as strings)
            raw_tags = data.get("tags", [])
            tags = []
            if isinstance(raw_tags, list):
                for t in raw_tags:
                    if isinstance(t, str):
                        tags.append(t.lower())
                    elif isinstance(t, dict):
                        tags.append(t.get("label", t.get("slug", "")).lower())

            # Parse fee info: prefer feeSchedule (full market endpoint),
            # fallback to feeType+feesEnabled (keyset endpoint)
            fee_schedule = data.get("feeSchedule") or {}
            fee_rate = float(fee_schedule.get("rate", -1))
            fee_exponent = int(fee_schedule.get("exponent", 1))

            if fee_rate < 0:
                # Keyset endpoint doesn't return feeSchedule but has feeType
                from src.fees import fee_rate_from_fee_type
                fee_type = data.get("feeType")
                fees_enabled = data.get("feesEnabled", True)
                if isinstance(fees_enabled, str):
                    fees_enabled = fees_enabled.lower() == "true"
                inferred = fee_rate_from_fee_type(fee_type, fees_enabled)
                if inferred >= 0:
                    fee_rate = inferred

            return Market(
                condition_id=data.get("conditionId", ""),
                question=data.get("question", ""),
                slug=data.get("slug", ""),
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                end_date=end_date,
                active=data.get("active", False),
                closed=data.get("closed", False),
                enable_order_book=enable_order_book,
                yes_price=yes_price,
                no_price=no_price,
                tags=tags,
                fee_rate=fee_rate,
                fee_exponent=fee_exponent,
            )
        except (KeyError, ValueError, TypeError, json_lib.JSONDecodeError) as e:
            logger.debug("gamma_parse_error", error=str(e), data_keys=list(data.keys()))
            return None
