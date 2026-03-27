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

# Order monitoring
ORDER_CHECK_INTERVAL = 5       # seconds between order status polls
ORDER_TIMEOUT = 30             # seconds before cancelling unfilled order
BALANCE_REFRESH_INTERVAL = 3600  # seconds between balance refreshes (1 hour)

# Builder Relayer (gasless redeem via Safe/proxy)
RELAYER_URL = "https://relayer-v2.polymarket.com"
ZERO_BYTES32 = "0x" + "00" * 32

# Minimal ABI for CTF redeemPositions
REDEEM_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "type": "function",
    }
]


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
    size_matched: float = 0.0  # shares actually filled
    error: str = ""


class ExecutionMode(Enum):
    PAPER = "paper"      # Current mode — no real orders
    DRY_RUN = "dry_run"  # Validates everything but doesn't submit
    LIVE = "live"        # Real execution


class Executor:
    """Handles order execution against Polymarket CLOB.

    Supports three modes:
    - PAPER: No interaction with CLOB (current behavior)
    - DRY_RUN: Initializes CLOB client, validates orders, but doesn't submit
    - LIVE: Places real orders with monitoring (status polling + auto-cancel)

    In LIVE mode:
    - Queries real USDC balance from Polymarket for bet sizing
    - Monitors placed orders and cancels after ORDER_TIMEOUT if unfilled
    - Tracks filled amounts for partial fills
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

        # Live balance tracking
        self._live_balance: float | None = None  # USDC balance from Polymarket
        self._last_balance_fetch: float = 0.0

        # Order monitoring
        self._pending_orders: dict[str, TradeRecord] = {}  # order_id -> TradeRecord
        self._monitor_task: asyncio.Task | None = None
        self._on_balance_update = None  # callback(balance: float)

        # Builder Relayer (auto-redeem)
        self._relayer = None
        self._redeem_available = False

    async def initialize(self):
        """Initialize the CLOB client with API credentials."""
        if self.mode == ExecutionMode.PAPER:
            self._initialized = True
            logger.info("executor_initialized", mode="paper")
            return

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            private_key = self._credentials.get_private_key()
            sig_type = self._credentials.signature_type  # 0=EOA, 1=POLY_PROXY
            proxy_address = self._credentials.get_proxy_address()

            # Try to get API credentials from env vars; if missing, derive from private key
            try:
                api_key = self._credentials.get_api_key()
                api_secret = self._credentials.get_api_secret()
                passphrase = self._credentials.get_passphrase()
                logger.info("using_provided_api_creds", signature_type=sig_type)
            except EnvironmentError:
                # Auto-derive API credentials from private key
                logger.info("deriving_api_creds",
                            msg="API env vars not set, deriving from private key...",
                            signature_type=sig_type,
                            funder=proxy_address or "none")
                client_tmp = ClobClient(
                    host="https://clob.polymarket.com",
                    key=private_key,
                    chain_id=137,
                    signature_type=sig_type,
                    funder=proxy_address,
                )
                creds = client_tmp.derive_api_key()
                api_key = creds.api_key
                api_secret = creds.api_secret
                passphrase = creds.api_passphrase
                logger.info("api_creds_derived", api_key=api_key[:8] + "...")

            self._client = ClobClient(
                host="https://clob.polymarket.com",
                key=private_key,
                chain_id=137,  # Polygon mainnet
                signature_type=sig_type,
                funder=proxy_address,
                creds=ApiCreds(
                    api_key=api_key,
                    api_secret=api_secret,
                    api_passphrase=passphrase,
                ),
            )

            self._initialized = True

            # Verify USDC allowance for CTF Exchange
            await self._check_allowance()

            # Fetch initial balance
            await self._refresh_balance()

            # Start order monitor loop
            if self._monitor_task is None or self._monitor_task.done():
                self._monitor_task = asyncio.create_task(self._order_monitor_loop())

            # Initialize Builder Relayer for auto-redeem
            self._init_relayer()

            redeem_status = "gasless (Builder Relayer)" if self._redeem_available else "fallback (direct web3, pays gas)"
            logger.info(
                "executor_initialized",
                mode=self.mode.value,
                balance=f"${self._live_balance:.2f}" if self._live_balance else "unknown",
                auto_redeem=redeem_status,
            )

        except ImportError:
            logger.error("executor_init_failed", error="py-clob-client not installed")
            raise
        except EnvironmentError as e:
            logger.error("executor_init_failed", error=str(e))
            raise

    def _init_relayer(self):
        """Initialize Builder Relayer for gasless auto-redeem of winning positions."""
        builder_creds = self._credentials.get_builder_creds()
        if not builder_creds:
            logger.info("relayer_not_configured", msg="Builder API credentials not set, auto-redeem disabled")
            return

        try:
            from py_builder_relayer_client.client import RelayClient
            from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds

            api_key, api_secret, api_passphrase = builder_creds
            private_key = self._credentials.get_private_key()

            builder_config = BuilderConfig(
                local_builder_creds=BuilderApiKeyCreds(
                    key=api_key,
                    secret=api_secret,
                    passphrase=api_passphrase,
                )
            )
            self._relayer = RelayClient(
                relayer_url=RELAYER_URL,
                chain_id=137,
                private_key=private_key,
                builder_config=builder_config,
            )
            self._redeem_available = True
            logger.info("relayer_initialized", msg="Builder Relayer ready for auto-redeem")
        except ImportError:
            logger.warning("relayer_import_error", msg="py-builder-relayer-client not installed")
        except Exception as e:
            logger.warning("relayer_init_error", error=str(e))

    async def redeem_position(self, condition_id: str) -> bool:
        """Redeem a winning position.

        Tries two strategies:
        1. Builder Relayer (gasless) if credentials available
        2. Direct CLOB redeem (pays gas) as fallback

        Returns True if redeem was submitted (whether gasless or paid).
        """
        if self.mode != ExecutionMode.LIVE:
            return False

        # Try gasless redeem via Builder Relayer first
        if self._redeem_available and self._relayer:
            if await self._redeem_via_builder(condition_id):
                return True

        # Fallback: direct redeem via CLOB (pays gas)
        if self._client:
            return await self._redeem_via_clob(condition_id)

        return False

    async def _redeem_via_builder(self, condition_id: str) -> bool:
        """Try gasless redeem via Builder Relayer."""
        try:
            from web3 import Web3
            from py_builder_relayer_client.models import SafeTransaction

            w3 = Web3()
            ctf = w3.eth.contract(
                address=Web3.to_checksum_address(CONDITIONAL_TOKENS),
                abi=REDEEM_ABI,
            )

            # Ensure condition_id is proper bytes32 hex
            cid_hex = condition_id if condition_id.startswith("0x") else f"0x{condition_id}"

            calldata = ctf.encodeABI(
                fn_name="redeemPositions",
                args=[
                    Web3.to_checksum_address(USDC_ADDRESS),
                    bytes.fromhex(ZERO_BYTES32[2:]),
                    bytes.fromhex(cid_hex[2:]),
                    [1, 2],
                ],
            )

            tx = SafeTransaction(
                to=Web3.to_checksum_address(CONDITIONAL_TOKENS),
                data=calldata,
                value="0",
            )

            response = await asyncio.to_thread(
                self._relayer.execute, [tx], "auto-redeem"
            )

            tx_id = getattr(response, "transaction_id", None) or str(response)

            logger.info(
                "redeem_submitted_gasless",
                condition_id=condition_id[:20] + "...",
                tx_id=str(tx_id)[:20],
            )

            # Poll for confirmation in background
            asyncio.create_task(self._wait_redeem(response, condition_id))
            return True

        except Exception as e:
            logger.warning(
                "redeem_via_builder_failed",
                condition_id=condition_id[:20] + "...",
                error=str(e),
            )
            return False

    async def _redeem_via_clob(self, condition_id: str) -> bool:
        """Redeem directly via CLOB client (pays gas, no relayer needed).

        Note: This requires calling the CTF contract directly via web3.
        py_clob_client doesn't expose a direct redeem method, so we use web3 to encode
        and submit the redeemPositions call directly.
        """
        try:
            from web3 import Web3
            from eth_account import Account

            w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))

            private_key = self._credentials.get_private_key()
            account = Account.from_key(private_key)

            ctf = w3.eth.contract(
                address=Web3.to_checksum_address(CONDITIONAL_TOKENS),
                abi=REDEEM_ABI,
            )

            # Ensure condition_id is proper bytes32 hex
            cid_hex = condition_id if condition_id.startswith("0x") else f"0x{condition_id}"

            # Build the transaction
            tx_data = ctf.functions.redeemPositions(
                Web3.to_checksum_address(USDC_ADDRESS),
                bytes.fromhex(ZERO_BYTES32[2:]),
                bytes.fromhex(cid_hex[2:]),
                [1, 2],
            ).build_transaction({
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gasPrice": w3.eth.gas_price,
            })

            # Sign and send
            signed_tx = w3.eth.account.sign_transaction(tx_data, private_key)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)

            logger.info(
                "redeem_submitted_clob",
                condition_id=condition_id[:20] + "...",
                tx_hash=tx_hash.hex()[:20],
            )

            # Refresh balance after ~30s (tx confirmation time)
            asyncio.create_task(self._refresh_balance_delayed(delay_seconds=30))
            return True

        except Exception as e:
            logger.warning(
                "redeem_via_clob_failed",
                condition_id=condition_id[:20] + "...",
                error=str(e),
            )
            return False

    async def _refresh_balance_delayed(self, delay_seconds: int = 30):
        """Refresh balance after a redeem tx is likely confirmed."""
        await asyncio.sleep(delay_seconds)
        await self._refresh_balance()

    async def _wait_redeem(self, response, condition_id: str):
        """Background: poll relayer until redeem tx is mined, then refresh balance."""
        try:
            # response.wait() polls until STATE_MINED/CONFIRMED (blocking)
            result = await asyncio.to_thread(response.wait)
            if result:
                logger.info(
                    "redeem_confirmed",
                    condition_id=condition_id[:20] + "...",
                )
                await self._refresh_balance()
            else:
                logger.warning(
                    "redeem_wait_failed",
                    condition_id=condition_id[:20] + "...",
                )
        except Exception as e:
            logger.warning("redeem_wait_error", condition_id=condition_id[:20] + "...", error=str(e))

    async def close(self):
        """Stop order monitor."""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

    # ── Allowance ──────────────────────────────────────────────────

    async def _check_allowance(self):
        """Verify USDC is approved for the CTF Exchange.

        If allowance is zero or too low, log an error and block live trading.
        The user must approve USDC spending via Polymarket UI or manually.
        """
        if not self._client:
            return
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            resp = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            if not resp:
                logger.warning("allowance_check_empty", response=str(resp))
                return

            allowance_raw = float(resp.get("allowance", 0) or 0)
            # Auto-detect format (same as balance)
            allowance = allowance_raw / 1e6 if allowance_raw > 1000 else allowance_raw

            if allowance < 1.0:
                logger.error(
                    "usdc_not_approved",
                    allowance=f"${allowance:.6f}",
                    raw_value=allowance_raw,
                    msg="USDC allowance too low. Deposit funds via Polymarket UI to auto-approve.",
                )
            else:
                logger.info("allowance_ok", allowance=f"${allowance:.2f}", raw_value=allowance_raw)

        except Exception as e:
            logger.warning("allowance_check_error", error=str(e))

    # ── Balance ───────────────────────────────────────────────────

    async def _refresh_balance(self):
        """Fetch real USDC balance from Polymarket."""
        if not self._client:
            return
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            resp = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            if resp and "balance" in resp:
                raw = float(resp["balance"])
                # Auto-detect format: if raw > 1000, it's in smallest units (6 decimals)
                # e.g. $1.00 = 1000000 raw. If raw < 1000, it's already in USDC.
                self._live_balance = raw / 1e6 if raw > 1000 else raw
                self._last_balance_fetch = time.time()
                logger.info("balance_refreshed",
                            balance=f"${self._live_balance:.2f}",
                            raw_value=raw)
                if self._on_balance_update:
                    self._on_balance_update(self._live_balance)
            else:
                logger.warning("balance_fetch_empty", response=str(resp))
        except Exception as e:
            logger.warning("balance_fetch_error", error=str(e))

    def get_balance(self) -> float | None:
        """Return live USDC balance (None if not yet fetched or in paper mode)."""
        return self._live_balance

    def on_balance_update(self, callback):
        """Register callback to be called when balance changes."""
        self._on_balance_update = callback

    # ── Execution ─────────────────────────────────────────────────

    async def execute(self, opp: Opportunity) -> TradeRecord | None:
        """Execute a trade for the given opportunity."""
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

        # In live mode, check against real balance
        if self.mode == ExecutionMode.LIVE and self._live_balance is not None:
            if opp.suggested_bet > self._live_balance:
                logger.warning(
                    "insufficient_balance",
                    bet=f"${opp.suggested_bet:.2f}",
                    balance=f"${self._live_balance:.2f}",
                    question=opp.question[:60],
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
            token_id=self._resolve_token_id(opp),
            token_side=opp.token_side,
            price=opp.token_price,
            size=shares,
            cost_usd=opp.suggested_bet,
            status=OrderStatus.CONFIRMED,
            created_at=time.time(),
            matched_at=time.time(),
            size_matched=shares,
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
        resolved_token_id = self._resolve_token_id(opp)

        if not resolved_token_id:
            logger.error("no_token_id", question=opp.question[:60], side=opp.token_side)
            return None

        trade = TradeRecord(
            order_id="",
            condition_id=opp.condition_id,
            question=opp.question,
            token_id=resolved_token_id,
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
                token_id=resolved_token_id,
            )

            # Create and sign the order
            signed_order = self._client.create_order(order_args)

            # Submit to CLOB
            response = self._client.post_order(signed_order)

            if response and response.get("orderID"):
                trade.order_id = response["orderID"]
                trade.status = OrderStatus.LIVE
                # Track for monitoring
                self._pending_orders[trade.order_id] = trade
                # Deduct from live balance optimistically
                if self._live_balance is not None:
                    self._live_balance -= opp.suggested_bet
                logger.info(
                    "order_placed",
                    order_id=trade.order_id,
                    question=opp.question[:60],
                    side=opp.token_side,
                    price=f"${opp.token_price:.4f}",
                    cost=f"${opp.suggested_bet:.2f}",
                    balance=f"${self._live_balance:.2f}" if self._live_balance is not None else "?",
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

    # ── Order Monitoring ──────────────────────────────────────────

    async def _order_monitor_loop(self):
        """Background loop: check pending orders, cancel if timed out."""
        logger.info("order_monitor_started")
        while True:
            try:
                await asyncio.sleep(ORDER_CHECK_INTERVAL)
                await self._check_pending_orders()

                # Periodically refresh balance
                if time.time() - self._last_balance_fetch > BALANCE_REFRESH_INTERVAL:
                    await self._refresh_balance()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("order_monitor_error", error=str(e))

    async def _check_pending_orders(self):
        """Poll status of all pending orders."""
        if not self._client or not self._pending_orders:
            return

        now = time.time()
        to_remove = []

        for order_id, trade in list(self._pending_orders.items()):
            try:
                order_data = self._client.get_order(order_id)
                if not order_data:
                    continue

                status = order_data.get("status", "").upper()
                size_matched = float(order_data.get("size_matched", 0) or 0)

                if status == "MATCHED" or (status == "CLOSED" and size_matched > 0):
                    # Fully or partially filled
                    trade.status = OrderStatus.MATCHED
                    trade.matched_at = now
                    trade.size_matched = size_matched
                    # Recalculate actual cost based on fill
                    trade.cost_usd = round(size_matched * trade.price, 2)
                    to_remove.append(order_id)
                    logger.info(
                        "order_matched",
                        order_id=order_id,
                        question=trade.question[:60],
                        size_matched=f"{size_matched:.2f}",
                        cost=f"${trade.cost_usd:.2f}",
                    )

                elif status in ("CANCELLED", "EXPIRED"):
                    trade.status = OrderStatus.CANCELLED
                    trade.error = f"Order {status.lower()} by exchange"
                    to_remove.append(order_id)
                    # Refund balance
                    if self._live_balance is not None:
                        self._live_balance += trade.cost_usd
                    logger.info(
                        "order_cancelled_external",
                        order_id=order_id,
                        question=trade.question[:60],
                    )

                elif now - trade.created_at > ORDER_TIMEOUT:
                    # Timed out — cancel it
                    await self._cancel_order(order_id, trade)
                    to_remove.append(order_id)

            except Exception as e:
                logger.warning(
                    "order_check_error",
                    order_id=order_id,
                    error=str(e),
                )

        for oid in to_remove:
            self._pending_orders.pop(oid, None)

    async def _cancel_order(self, order_id: str, trade: TradeRecord):
        """Cancel a single order and update trade record."""
        try:
            self._client.cancel(order_id)
            trade.status = OrderStatus.CANCELLED
            trade.error = f"Timed out after {ORDER_TIMEOUT}s"
            # Refund balance
            if self._live_balance is not None:
                self._live_balance += trade.cost_usd
            logger.info(
                "order_cancelled_timeout",
                order_id=order_id,
                question=trade.question[:60],
                age=f"{time.time() - trade.created_at:.0f}s",
            )
        except Exception as e:
            logger.warning(
                "order_cancel_failed",
                order_id=order_id,
                error=str(e),
            )

    # ── Helpers ───────────────────────────────────────────────────

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
        """Resolve the token ID for the opportunity."""
        # Try explicit override first (set by CopyTrader)
        if hasattr(opp, '_token_id') and opp._token_id:
            return opp._token_id
        # Use the token_id from the Opportunity itself (set by Detector)
        if opp.token_id:
            return opp.token_id
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

    async def set_mode(self, mode: ExecutionMode):
        """Change execution mode at runtime (hot-reload).

        When switching to LIVE/DRY_RUN, initializes the CLOB client if needed.
        When switching to PAPER, keeps the client but routes to paper handler.
        """
        old_mode = self.mode
        self.mode = mode

        if mode in (ExecutionMode.LIVE, ExecutionMode.DRY_RUN):
            if self._client is None:
                # Need to initialize CLOB client
                self._initialized = False
                try:
                    await self.initialize()
                except Exception as e:
                    # Revert to old mode if initialization fails
                    self.mode = old_mode
                    self._initialized = (old_mode == ExecutionMode.PAPER)
                    logger.error(
                        "mode_switch_failed",
                        target_mode=mode.value,
                        reverted_to=old_mode.value,
                        error=str(e),
                    )
                    raise
            else:
                # Client exists, force balance refresh
                await self._refresh_balance()
        elif mode == ExecutionMode.PAPER:
            self._initialized = True

        logger.info("executor_mode_changed", old=old_mode.value, new=mode.value)

    def reset_trades(self):
        """Clear all trade history (e.g. when switching from paper to live)."""
        self._trades.clear()
        self._pending_orders.clear()
        self._daily_pnl = 0.0
        logger.info("executor_trades_reset")

    @property
    def trades(self) -> list[TradeRecord]:
        return self._trades

    def get_stats(self) -> dict:
        confirmed = [t for t in self._trades if t.status in (
            OrderStatus.CONFIRMED, OrderStatus.MATCHED,
        )]
        failed = [t for t in self._trades if t.status == OrderStatus.FAILED]
        cancelled = [t for t in self._trades if t.status == OrderStatus.CANCELLED]
        pending = [t for t in self._trades if t.status in (
            OrderStatus.LIVE, OrderStatus.PENDING,
        )]
        stats = {
            "mode": self.mode.value,
            "total_trades": len(self._trades),
            "confirmed": len(confirmed),
            "failed": len(failed),
            "cancelled": len(cancelled),
            "pending_orders": len(pending),
            "total_cost_usd": sum(t.cost_usd for t in confirmed),
            "daily_pnl": round(self._daily_pnl, 2),
        }
        if self._live_balance is not None:
            stats["live_balance"] = round(self._live_balance, 2)
        return stats
