from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

logger = structlog.get_logger("polymarket.tracker")


@dataclass
class PriceLevel:
    price: float
    size: float


@dataclass
class MarketState:
    """Tracks the current state of a market's order book and prices."""

    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    end_date: datetime | None = None

    # Best prices (updated via WebSocket)
    best_bid_yes: float = 0.0
    best_ask_yes: float = 0.0
    best_bid_no: float = 0.0
    best_ask_no: float = 0.0

    # Last trade prices
    last_trade_yes: float = 0.0
    last_trade_no: float = 0.0

    # Order book depth (top N levels)
    asks_yes: list[PriceLevel] = field(default_factory=list)
    bids_yes: list[PriceLevel] = field(default_factory=list)
    asks_no: list[PriceLevel] = field(default_factory=list)
    bids_no: list[PriceLevel] = field(default_factory=list)

    # Timestamps
    last_update: float = 0.0
    last_book_snapshot: float = 0.0

    # Resolution
    resolved: bool = False
    winning_token_id: str = ""

    # Metadata
    tags: list[str] = field(default_factory=list)

    @property
    def hours_to_resolution(self) -> float | None:
        """Hours remaining until market resolution."""
        if self.end_date is None:
            return None
        delta = self.end_date - datetime.now(timezone.utc)
        return max(0.0, delta.total_seconds() / 3600)

    @property
    def is_stale(self) -> bool:
        if self.last_update == 0:
            return True
        return (time.time() - self.last_update) > 5.0

    @property
    def best_token_price(self) -> float:
        """Price of the highest-probability token (best ask)."""
        return max(self.best_ask_yes, self.best_ask_no)

    @property
    def best_token_side(self) -> str:
        return "YES" if self.best_ask_yes >= self.best_ask_no else "NO"

    @property
    def spread_sum(self) -> float:
        """Sum of best asks. If < 1.0, there's a bonding arbitrage opportunity."""
        if self.best_ask_yes == 0 or self.best_ask_no == 0:
            return 0.0
        return self.best_ask_yes + self.best_ask_no


class MarketTracker:
    """Manages the state of all monitored markets."""

    def __init__(self):
        self._markets: dict[str, MarketState] = {}  # token_id -> MarketState
        self._by_condition: dict[str, MarketState] = {}  # condition_id -> MarketState

    def add_market(
        self,
        condition_id: str,
        question: str,
        yes_token_id: str,
        no_token_id: str,
        end_date: datetime | None = None,
        tags: list[str] | None = None,
    ) -> MarketState:
        state = MarketState(
            condition_id=condition_id,
            question=question,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            end_date=end_date,
            tags=tags or [],
        )
        self._markets[yes_token_id] = state
        self._markets[no_token_id] = state
        self._by_condition[condition_id] = state
        return state

    def remove_market(self, condition_id: str):
        state = self._by_condition.pop(condition_id, None)
        if state:
            self._markets.pop(state.yes_token_id, None)
            self._markets.pop(state.no_token_id, None)

    def get_by_token(self, token_id: str) -> MarketState | None:
        return self._markets.get(token_id)

    def get_by_condition(self, condition_id: str) -> MarketState | None:
        return self._by_condition.get(condition_id)

    @property
    def all_markets(self) -> list[MarketState]:
        return list(self._by_condition.values())

    @property
    def all_token_ids(self) -> list[str]:
        return list(self._markets.keys())

    def update_book(self, token_id: str, bids: list[dict], asks: list[dict]):
        """Update order book from a 'book' snapshot event.

        Only keeps top 10 levels (liquidity provider needs deeper book).
        """
        state = self._markets.get(token_id)
        if not state:
            return

        # Parse top 10 levels — liquidity provider uses deeper book for quoting
        parsed_bids = [PriceLevel(float(b["price"]), float(b["size"])) for b in bids[:10]]
        parsed_asks = [PriceLevel(float(a["price"]), float(a["size"])) for a in asks[:10]]

        now = time.time()
        is_yes = token_id == state.yes_token_id
        if is_yes:
            state.bids_yes = parsed_bids
            state.asks_yes = parsed_asks
            if parsed_bids:
                state.best_bid_yes = parsed_bids[0].price
            if parsed_asks:
                state.best_ask_yes = parsed_asks[0].price
        else:
            state.bids_no = parsed_bids
            state.asks_no = parsed_asks
            if parsed_bids:
                state.best_bid_no = parsed_bids[0].price
            if parsed_asks:
                state.best_ask_no = parsed_asks[0].price

        state.last_update = now
        state.last_book_snapshot = now

    def update_best_bid_ask(self, token_id: str, best_bid: float, best_ask: float):
        """Update best bid/ask from a 'best_bid_ask' event."""
        state = self._markets.get(token_id)
        if not state:
            return

        if token_id == state.yes_token_id:
            state.best_bid_yes = best_bid
            state.best_ask_yes = best_ask
        else:
            state.best_bid_no = best_bid
            state.best_ask_no = best_ask

        state.last_update = time.time()

    def get_midpoint(self, token_id: str) -> float | None:
        """Get midpoint price for a token (best_bid + best_ask) / 2."""
        state = self._markets.get(token_id)
        if not state:
            return None
        if token_id == state.yes_token_id:
            bid, ask = state.best_bid_yes, state.best_ask_yes
        else:
            bid, ask = state.best_bid_no, state.best_ask_no
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        return None

    def update_last_trade(self, token_id: str, price: float):
        """Update last trade price."""
        state = self._markets.get(token_id)
        if not state:
            return

        if token_id == state.yes_token_id:
            state.last_trade_yes = price
        else:
            state.last_trade_no = price

        state.last_update = time.time()

    def mark_resolved(self, condition_id: str, winning_token_id: str):
        """Mark a market as resolved."""
        state = self._by_condition.get(condition_id)
        if not state:
            return

        state.resolved = True
        state.winning_token_id = winning_token_id
        state.last_update = time.time()

        logger.info(
            "market_resolved",
            condition_id=condition_id,
            question=state.question,
            winning_token_id=winning_token_id,
            winning_side="YES" if winning_token_id == state.yes_token_id else "NO",
        )
