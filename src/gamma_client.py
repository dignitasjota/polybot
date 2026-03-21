from __future__ import annotations

import asyncio
import json as json_lib
from dataclasses import dataclass
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

    async def fetch_active_markets(
        self,
        max_time_to_resolution: timedelta = timedelta(hours=24),
        max_results: int = 50,
    ) -> list[Market]:
        """Fetch active markets closing within max_time_to_resolution.

        Uses end_date_min + ascending order to get soonest-closing markets first,
        avoiding pagination through thousands of irrelevant markets.
        Filters: enableOrderBook=true, has liquidity (price > 0).
        """
        session = await self._get_session()
        markets: list[Market] = []
        skipped_no_book = 0
        skipped_no_liquidity = 0
        offset = 0
        page_size = 100
        now = datetime.now(timezone.utc)
        end_max = now + max_time_to_resolution

        while len(markets) < max_results:
            params = {
                "active": "true",
                "closed": "false",
                "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_date_max": end_max.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "order": "endDate",
                "ascending": "true",
                "limit": str(page_size),
                "offset": str(offset),
            }

            try:
                async with session.get(
                    f"{GAMMA_API_URL}/markets", params=params
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "gamma_api_error",
                            status=resp.status,
                            body=await resp.text(),
                        )
                        break

                    data = await resp.json()

                    if not data:
                        break

                    for item in data:
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
                        markets.append(market)

                    if len(data) < page_size:
                        break
                    offset += page_size

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
            # Standard order is [Yes, No] but let's be safe
            yes_idx = None
            no_idx = None
            for i, outcome in enumerate(outcomes):
                if outcome.lower() == "yes":
                    yes_idx = i
                elif outcome.lower() == "no":
                    no_idx = i

            if yes_idx is None or no_idx is None:
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
            )
        except (KeyError, ValueError, TypeError, json_lib.JSONDecodeError) as e:
            logger.debug("gamma_parse_error", error=str(e), data_keys=list(data.keys()))
            return None
