from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum

import structlog

from src.config import CredentialsConfig, RiskConfig, get_secret
from src.detector import Opportunity

logger = structlog.get_logger("polymarket.executor")

# Polymarket CLOB contracts on Polygon
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


class OrderStatus(Enum):
    PENDING = "pending"
    LIVE = "live"
    MATCHED = "matched"
    MINED = "mined"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TradeRecord:
    """Records a real trade execution."""
    order_id: str
    condition_id: str
    question: str
    token_id: str
    token_side: str  # YES or NO
    price: float
    size: float  # in shares
    cost_usd: float  # price * size
    status: OrderStatus = OrderStatus.PENDING
    created_at: float = 0.0
    matched_at: float = 0.0
    error: str = ""


class ExecutionMode(Enum):
    PAPER = "paper"      # Current mode — no real orders
    DRY_RUN = "dry_run"  # Validates everything but doesn't submit
    LIVE = "live"        # Real execution


class Executor:
    """Handles real order execution against Polymarket CLOB.

    Supports three modes:
    - PAPER: No interaction with CLOB (current behavior)
    - DRY_RUN: Initializes CLOB client, validates orders, but doesn't submit
    - LIVE: Places real orders

    Usage:
        executor = Executor(risk_config, mode=ExecutionMode.LIVE)
        await executor.initialize()  # derives API credentials
        trade = await executor.execute(opportunity)
    """

    def __init__(
        self,
        risk: RiskConfig,
        mode: ExecutionMode = ExecutionMode.PAPER,
        credentials: CredentialsConfig | None = None,
    ):
        self.risk = risk
        self.mode = mode
        self._credentials = credentials or CredentialsConfig()
        self._client = None  # py_clob_client.ClobClient
        self._trades: list[TradeRecord] = []
        self._daily_pnl: float = 0.0
        self._daily_reset_time: float = 0.0
        self._initialized = False

    async def initialize(self):
        """Initialize the CLOB client with API credentials.

        Credentials are read from the CredentialsConfig (env var names).
        """
        if self.mode == ExecutionMode.PAPER:
            self._initialized = True
            logger.info("executor_initialized", mode="paper")
            return

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            private_key = self._credentials.get_private_key()
            api_key = self._credentials.get_api_key()
            api_secret = self._credentials.get_api_secret()
            passphrase = self._credentials.get_passphrase()

            self._client = ClobClient(
                host="https://clob.polymarket.com",
                key=private_key,
                chain_id=137,  # Polygon mainnet
                creds=ApiCreds(
                    api_key=api_key,
                    api_secret=api_secret,
                    api_passphrase=passphrase,
                ),
            )

            self._initialized = True
            logger.info("executor_initialized", mode=self.mode.value)

        except ImportError:
            logger.error("executor_init_failed", error="py-clob-client not installed")
            raise
        except EnvironmentError as e:
            logger.error("executor_init_failed", error=str(e))
            raise

    async def execute(self, opp: Opportunity) -> TradeRecord | None:
        """Execute a trade for the given opportunity.

        Returns a TradeRecord or None if the trade was blocked by risk checks.
        """
        if not self._initialized:
            logger.error("executor_not_initialized")
            return None

        # Risk checks
        if not self._check_risk(opp):
            return None

        if self.mode == ExecutionMode.PAPER:
            return self._paper_trade(opp)

        if self.mode == ExecutionMode.DRY_RUN:
            return await self._dry_run(opp)

        return await self._live_trade(opp)

    def _check_risk(self, opp: Opportunity) -> bool:
        """Pre-trade risk checks."""
        # Kill switch
        if self.risk.kill_switch:
            logger.warning("kill_switch_active", question=opp.question[:60])
            return False

        # Daily loss limit
        self._maybe_reset_daily()
        if self._daily_pnl <= -self.risk.max_daily_loss:
            logger.warning(
                "daily_loss_limit",
                daily_pnl=f"${self._daily_pnl:.2f}",
                limit=f"${self.risk.max_daily_loss:.2f}",
            )
            return False

        # Max concurrent positions
        active_trades = [t for t in self._trades if t.status in (
            OrderStatus.LIVE, OrderStatus.MATCHED, OrderStatus.PENDING
        )]
        if len(active_trades) >= self.risk.max_concurrent_positions:
            logger.debug("max_positions_reached", count=len(active_trades))
            return False

        # Bet size sanity
        if opp.suggested_bet <= 0:
            return False

        if opp.suggested_bet > self.risk.max_bet_per_trade:
            logger.warning(
                "bet_exceeds_max",
                bet=f"${opp.suggested_bet:.2f}",
                max=f"${self.risk.max_bet_per_trade:.2f}",
            )
            return False

        return True

    def _paper_trade(self, opp: Opportunity) -> TradeRecord:
        """Record a paper trade (no real execution)."""
        shares = opp.suggested_bet / opp.token_price if opp.token_price > 0 else 0
        trade = TradeRecord(
            order_id=f"paper_{int(time.time()*1000)}",
            condition_id=opp.condition_id,
            question=opp.question,
            token_id="",
            token_side=opp.token_side,
            price=opp.token_price,
            size=shares,
            cost_usd=opp.suggested_bet,
            status=OrderStatus.CONFIRMED,
            created_at=time.time(),
            matched_at=time.time(),
        )
        self._trades.append(trade)
        return trade

    async def _dry_run(self, opp: Opportunity) -> TradeRecord:
        """Validate the order without submitting."""
        trade = self._paper_trade(opp)
        trade.order_id = f"dryrun_{int(time.time()*1000)}"

        # Validate we can build the order
        try:
            order = self._build_order(opp)
            logger.info(
                "dry_run_order",
                question=opp.question[:60],
                side=opp.token_side,
                price=f"${opp.token_price:.4f}",
                size=f"${opp.suggested_bet:.2f}",
                order_type="limit_buy",
            )
        except Exception as e:
            trade.status = OrderStatus.FAILED
            trade.error = str(e)
            logger.error("dry_run_failed", error=str(e))

        return trade

    async def _live_trade(self, opp: Opportunity) -> TradeRecord | None:
        """Place a real order on Polymarket CLOB."""
        shares = opp.suggested_bet / opp.token_price if opp.token_price > 0 else 0
        trade = TradeRecord(
            order_id="",
            condition_id=opp.condition_id,
            question=opp.question,
            token_id="",  # Will be set from market tracker
            token_side=opp.token_side,
            price=opp.token_price,
            size=round(shares, 2),
            cost_usd=opp.suggested_bet,
            created_at=time.time(),
        )

        try:
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import BUY

            order_args = OrderArgs(
                price=opp.token_price,
                size=round(shares, 2),
                side=BUY,
                token_id=self._resolve_token_id(opp),
            )

            # Create and sign the order
            signed_order = self._client.create_order(order_args)

            # Submit to CLOB
            response = self._client.post_order(signed_order)

            if response and response.get("orderID"):
                trade.order_id = response["orderID"]
                trade.status = OrderStatus.LIVE
                logger.info(
                    "order_placed",
                    order_id=trade.order_id,
                    question=opp.question[:60],
                    side=opp.token_side,
                    price=f"${opp.token_price:.4f}",
                    cost=f"${opp.suggested_bet:.2f}",
                )
            else:
                trade.status = OrderStatus.FAILED
                trade.error = str(response)
                logger.error("order_rejected", response=str(response))

        except Exception as e:
            trade.status = OrderStatus.FAILED
            trade.error = str(e)
            logger.error("order_failed", error=str(e), question=opp.question[:60])

        self._trades.append(trade)
        return trade

    def _build_order(self, opp: Opportunity) -> dict:
        """Build order parameters (for validation in dry-run mode)."""
        shares = opp.suggested_bet / opp.token_price if opp.token_price > 0 else 0
        return {
            "token_id": self._resolve_token_id(opp),
            "side": "BUY",
            "price": opp.token_price,
            "size": round(shares, 2),
            "type": "GTC",
        }

    def _resolve_token_id(self, opp: Opportunity) -> str:
        """Resolve the token ID for the opportunity.

        Must be set externally via set_token_resolver or on the opportunity itself.
        """
        if hasattr(opp, '_token_id') and opp._token_id:
            return opp._token_id
        # Fallback: will need to be resolved from MarketTracker
        return ""

    def _maybe_reset_daily(self):
        """Reset daily P&L counter at midnight UTC."""
        now = time.time()
        # Reset every 24 hours
        if now - self._daily_reset_time > 86400:
            self._daily_pnl = 0.0
            self._daily_reset_time = now

    def update_pnl(self, pnl: float):
        """Update daily P&L (called when a trade settles)."""
        self._daily_pnl += pnl

    @property
    def trades(self) -> list[TradeRecord]:
        return self._trades

    def get_stats(self) -> dict:
        confirmed = [t for t in self._trades if t.status == OrderStatus.CONFIRMED]
        failed = [t for t in self._trades if t.status == OrderStatus.FAILED]
        return {
            "mode": self.mode.value,
            "total_trades": len(self._trades),
            "confirmed": len(confirmed),
            "failed": len(failed),
            "total_cost_usd": sum(t.cost_usd for t in confirmed),
            "daily_pnl": round(self._daily_pnl, 2),
        }
