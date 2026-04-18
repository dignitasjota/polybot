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

    # Multi-strategy tagging (Fase 3)
    source_strategy: str = ""   # "directional", "copy_trade", etc — set on execute()
    mode: str = "paper"         # "paper" or "live" — set on execute() from strategy mode


class ExecutionMode(Enum):
    PAPER = "paper"      # Current mode — no real orders
    DRY_RUN = "dry_run"  # Validates everything but doesn't submit
    LIVE = "live"        # Real execution


class LedgerView:
    """Per-mode read/write view over the Executor's underlying state.

    Introduced in Fase 5 of the multi-strategy refactor. Filters
    `executor._trades` and `executor._pending_orders` by `trade.mode`
    so each mode (paper/live) has an isolated view, and exposes its own
    `balance` and `daily_pnl`.

    The underlying storage stays in the Executor (single source of truth),
    so this class is non-breaking — existing code that touches
    `executor._trades` directly keeps working.
    """

    __slots__ = ("_executor", "_mode")

    def __init__(self, executor: "Executor", mode: str):
        if mode not in ("paper", "live"):
            raise ValueError(f"invalid ledger mode: {mode}")
        self._executor = executor
        self._mode = mode

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def balance(self) -> float | None:
        if self._mode == "live":
            return self._executor._live_balance
        return self._executor._paper_balance

    @balance.setter
    def balance(self, value: float | None) -> None:
        if self._mode == "live":
            self._executor._live_balance = value
        else:
            self._executor._paper_balance = value

    @property
    def trades(self) -> list["TradeRecord"]:
        """Filtered view: only trades tagged with this ledger's mode.

        Trades created before Fase 3 default to ``mode='paper'``, so during
        the migration window the paper ledger sees them and the live ledger
        is empty until new live trades are created with ``mode='live'``.
        """
        return [
            t for t in self._executor._trades
            if getattr(t, "mode", "paper") == self._mode
        ]

    @property
    def pending_orders(self) -> dict[str, "TradeRecord"]:
        return {
            oid: t for oid, t in self._executor._pending_orders.items()
            if getattr(t, "mode", "paper") == self._mode
        }

    @property
    def daily_pnl(self) -> float:
        return self._executor._daily_pnl_by_mode.get(self._mode, 0.0)

    @daily_pnl.setter
    def daily_pnl(self, value: float) -> None:
        self._executor._daily_pnl_by_mode[self._mode] = value


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
        account_name: str = "default",
    ):
        self.risk = risk
        self.mode = mode
        self._account_name = account_name  # Fase 6: tag persisted rows
        self._credentials = credentials or CredentialsConfig()
        self._client = None  # py_clob_client.ClobClient
        self._trades: list[TradeRecord] = []
        self._daily_pnl: float = 0.0
        self._daily_reset_time: float = 0.0
        self._initialized = False

        # Live balance tracking
        self._live_balance: float | None = None  # USDC balance from Polymarket
        self._last_balance_fetch: float = 0.0

        # Paper balance tracking (Fase 5: dual-ledger). Default to the
        # configured simulated balance. Detector/copy_trader keep their own
        # balance for now; Fase 6+ will route updates here too.
        self._paper_balance: float | None = risk.simulated_balance

        # Per-mode daily P&L (Fase 5). Legacy `_daily_pnl` above stays as
        # the active-mode counter for backward compat with the existing
        # risk checks until Fase 8 fully splits routing.
        self._daily_pnl_by_mode: dict[str, float] = {"paper": 0.0, "live": 0.0}

        # Dual ledger views (Fase 5). Read/write proxies that filter
        # _trades/_pending_orders by trade.mode. Used by AccountContext
        # (Fase 2) and by the multi-strategy AccountRunner (Fase 8).
        self._ledger_live = LedgerView(self, "live")
        self._ledger_paper = LedgerView(self, "paper")

        # Order monitoring
        self._pending_orders: dict[str, TradeRecord] = {}  # order_id -> TradeRecord
        # Fase 6: track which trades have already been persisted (idempotency
        # guard for restored trades and rapid status updates).
        self._persisted_orders: set[str] = set()
        self._monitor_task: asyncio.Task | None = None
        self._on_balance_update = None  # callback(balance: float)
        self._on_order_confirmed = None  # callback(order_id, condition_id, token_id, side, price, size, cost_usd)
        self._on_order_cancelled = None  # callback(condition_id, token_side)
        self._on_position_redeemed = None  # callback(condition_id, amount_usd)

        # Builder Relayer (auto-redeem)
        self._relayer = None
        self._redeem_available = False
        self._proxy_address: str | None = None  # Proxy address for wallet (Magic Link or Gnosis Safe)
        self._funder: str | None = None  # Funder address (EOA or proxy)

        # Redeem retry: track failed redeems for periodic retry
        self._pending_redeems: set[str] = set()  # condition_ids awaiting redeem
        self._redeem_retry_task: asyncio.Task | None = None

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
            self._proxy_address = proxy_address  # Store for later use in redeem

            # Derive EOA address from private key (for fallback if no proxy)
            if not proxy_address:
                from eth_account import Account
                account = Account.from_key(private_key)
                self._funder = account.address
            else:
                self._funder = proxy_address

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

            # Start redeem retry loop
            if self._redeem_retry_task is None or self._redeem_retry_task.done():
                self._redeem_retry_task = asyncio.create_task(self._redeem_retry_loop())

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
        """Initialize Builder Relayer for gasless auto-redeem of winning positions.

        Builder credentials (BUILDER_API_KEY/SECRET/PASSPHRASE) are required
        for ALL signature types except Magic Link (sig_type=1). Without them,
        the relayer rejects requests with "invalid builder creds configured!".

        - Magic Link (sig_type=1): only needs proxy address; relayer trusts the
          built-in Polymarket builder identity.
        - Gnosis Safe (sig_type=2): needs proxy address AND builder creds.
        - MetaMask EOA (sig_type=0): needs builder creds (no proxy).
        """
        sig_type = self._credentials.signature_type
        private_key = self._credentials.get_private_key()

        # For proxy-based wallets (Magic Link or Gnosis Safe): proxy address required
        if sig_type in (1, 2):
            proxy_address = self._credentials.get_proxy_address()
            wallet_label = "gnosis_safe" if sig_type == 2 else "magic_link"
            if not proxy_address:
                logger.info("relayer_not_configured",
                           wallet_type=wallet_label,
                           msg="POLYMARKET_PROXY_ADDRESS not set, auto-redeem disabled")
                return
            logger.info("relayer_initializing", wallet_type=wallet_label, proxy=proxy_address[:10] + "...")

        # Builder creds: required for everything except Magic Link
        builder_creds = None
        if sig_type != 1:
            builder_creds = self._credentials.get_builder_creds()
            if not builder_creds:
                wt = "gnosis_safe" if sig_type == 2 else "metamask"
                logger.info("relayer_not_configured",
                           wallet_type=wt,
                           msg="BUILDER_API_KEY/SECRET/PASSPHRASE not set, auto-redeem disabled")
                return
            logger.info("relayer_builder_creds_loaded", wallet_type=("gnosis_safe" if sig_type == 2 else "metamask"))

        try:
            from py_builder_relayer_client.client import RelayClient
            from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds

            if sig_type == 1:
                # Magic Link: relayer trusts the built-in Polymarket builder identity
                builder_config = BuilderConfig()
            else:
                # Gnosis Safe or MetaMask EOA: pass explicit builder credentials
                api_key, api_secret, api_passphrase = builder_creds
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
            wallet_labels = {0: "MetaMask (via Builder)", 1: "Magic Link (via proxy)", 2: "Gnosis Safe (via proxy)"}
            wallet_label = wallet_labels.get(sig_type, f"unknown ({sig_type})")
            logger.info("relayer_initialized",
                       wallet_type=wallet_label,
                       msg="Builder Relayer ready for auto-redeem")
        except ImportError:
            logger.warning("relayer_import_error", msg="py-builder-relayer-client not installed")
        except Exception as e:
            wt = {0: "metamask", 1: "magic_link", 2: "gnosis_safe"}.get(sig_type, "unknown")
            logger.warning("relayer_init_error", wallet_type=wt, error=str(e))

    async def redeem_position(self, condition_id: str) -> bool:
        """Redeem a winning position.

        Strategy depends on wallet type:
        - Magic Link (POLY_PROXY): MUST use Builder Relayer (positions are under proxy)
        - MetaMask (EOA): Can use Builder Relayer or fallback to direct CLOB redeem

        Returns True if redeem was submitted. Failed redeems are queued for retry.
        """
        sig_type = self._credentials.signature_type

        wallet_labels = {0: "metamask", 1: "magic_link", 2: "gnosis_safe"}
        logger.info(
            "redeem_callback_invoked",
            condition_id=condition_id[:20] + "...",
            mode=self.mode.value,
            wallet_type=wallet_labels.get(sig_type, f"unknown({sig_type})"),
            relayer_available=self._redeem_available,
        )

        if self.mode != ExecutionMode.LIVE:
            logger.info(
                "redeem_skipped_not_live",
                condition_id=condition_id[:20] + "...",
                mode=self.mode.value,
            )
            return False

        success = False

        # Proxy-based (Magic Link / Gnosis Safe): MUST use Builder Relayer (positions are under proxy)
        if sig_type in (1, 2):
            if self._redeem_available and self._relayer:
                success = await self._redeem_via_builder(condition_id)
            else:
                logger.error(
                    "redeem_impossible",
                    condition_id=condition_id[:20] + "...",
                    reason="Proxy wallet requires Builder Relayer, but it's not configured",
                )

        # MetaMask EOA: try Builder Relayer first, fallback to direct CLOB
        elif sig_type == 0:
            if self._redeem_available and self._relayer:
                success = await self._redeem_via_builder(condition_id)

            # Fallback: direct redeem via CLOB (pays gas)
            if not success and self._client:
                success = await self._redeem_via_clob(condition_id)

        # Track for retry if failed
        if success:
            self._pending_redeems.discard(condition_id)
        else:
            self._pending_redeems.add(condition_id)
            logger.warning(
                "redeem_queued_for_retry",
                condition_id=condition_id[:20] + "...",
                pending_count=len(self._pending_redeems),
            )

        return success

    async def _redeem_via_builder(self, condition_id: str) -> bool:
        """Try gasless redeem via Builder Relayer."""
        try:
            from web3 import Web3
            from py_builder_relayer_client.models import SafeTransaction, OperationType

            # Need a real HTTP provider so build_transaction() can call
            # eth_chainId. Without it, web3.py raises
            # "Could not discover provider while making request: method:eth_chainId"
            w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com/"))
            ctf = w3.eth.contract(
                address=Web3.to_checksum_address(CONDITIONAL_TOKENS),
                abi=REDEEM_ABI,
            )

            # Ensure condition_id is proper bytes32 hex
            cid_hex = condition_id if condition_id.startswith("0x") else f"0x{condition_id}"

            # Encode the function call to get calldata. We only need the
            # calldata bytes — we don't actually broadcast this tx ourselves;
            # the Builder Relayer wraps and submits it from the proxy.
            # Pass chainId explicitly to avoid an extra RPC roundtrip.
            func = ctf.functions.redeemPositions(
                Web3.to_checksum_address(USDC_ADDRESS),
                bytes.fromhex(ZERO_BYTES32[2:]),
                bytes.fromhex(cid_hex[2:]),
                [1, 2],
            )
            tx_dict = func.build_transaction({
                'from': Web3.to_checksum_address(self._proxy_address or self._funder),
                'nonce': 0,
                'gasPrice': 0,
                'gas': 0,
                'chainId': 137,
            })
            calldata = tx_dict['data']

            # OperationType.CALL → regular contract call. DELEGATECALL would
            # execute the CTF code in the Safe's context, which is wrong for
            # redeemPositions.
            tx = SafeTransaction(
                to=Web3.to_checksum_address(CONDITIONAL_TOKENS),
                operation=OperationType.Call,
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

            # Fallback: also refresh balance after 30 seconds in case response.wait() hangs
            asyncio.create_task(self._refresh_balance_delayed(delay_seconds=30))
            return True

        except Exception as e:
            error_str = str(e)
            # Detect rate limit — don't spam logs on every retry
            if "429" in error_str or "quota exceeded" in error_str:
                logger.warning(
                    "redeem_rate_limited",
                    condition_id=condition_id[:20] + "...",
                )
            else:
                logger.warning(
                    "redeem_via_builder_failed",
                    condition_id=condition_id[:20] + "...",
                    error=error_str,
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

            w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com/"))

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
            logger.info("redeem_waiting_for_confirmation", condition_id=condition_id[:20] + "...")
            result = await asyncio.to_thread(response.wait)
            if result:
                logger.info(
                    "redeem_confirmed",
                    condition_id=condition_id[:20] + "...",
                    msg="Position redeemed successfully, refreshing balance",
                )
                await self._refresh_balance()
                # Fase 6: persist redeem on all matching trades for this market
                now = time.time()
                for trade in self._trades:
                    if (
                        trade.condition_id == condition_id
                        and trade.mode == "live"
                        and trade.status in (OrderStatus.MATCHED, OrderStatus.CONFIRMED)
                    ):
                        await self._persist_status_update(
                            trade, "redeemed", settled_at=now,
                        )
                # Notify detector callback if registered
                if self._on_balance_update and self._live_balance is not None:
                    self._on_balance_update(self._live_balance)
                # Notify detector that position was redeemed (for live balance sync)
                if self._on_position_redeemed:
                    try:
                        await self._on_position_redeemed(condition_id, self._live_balance or 0)
                    except Exception as e:
                        logger.warning("position_redeemed_callback_error", error=str(e))
            else:
                logger.warning(
                    "redeem_wait_failed",
                    condition_id=condition_id[:20] + "...",
                    msg="Redeem tx did not reach confirmed state",
                )
        except Exception as e:
            logger.warning(
                "redeem_wait_error",
                condition_id=condition_id[:20] + "...",
                error=str(e),
                msg="Failed to wait for redeem confirmation",
            )

    async def scan_and_redeem_orphan_positions(self) -> int:
        """Query Polymarket Data API for the proxy's open positions and redeem
        any that are settled (redeemable=True) but not yet claimed.

        This handles "orphan" positions: bets that were placed in a previous
        run of the bot (or are otherwise not tracked in memory) and have since
        been resolved as winners. Without this scan, those positions would
        stay locked on Polymarket forever because the in-memory _bet_placed
        dict is empty after a restart.

        Safe to call repeatedly: redeem_position handles duplicates via
        _pending_redeems and the on-chain CTF will simply no-op if already
        redeemed.

        Returns the number of redemption attempts triggered.
        """
        if self.mode != ExecutionMode.LIVE:
            return 0
        if not self._funder:
            logger.warning("orphan_scan_no_funder",
                           msg="Cannot scan positions, funder address unknown")
            return 0

        try:
            import aiohttp
        except ImportError:
            logger.error("orphan_scan_no_aiohttp")
            return 0

        url = f"https://data-api.polymarket.com/positions?user={self._funder}&sizeThreshold=0.01&limit=500"
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "orphan_scan_http_error",
                            status=resp.status,
                            funder=self._funder[:10] + "...",
                        )
                        return 0
                    positions = await resp.json()
        except Exception as e:
            logger.warning("orphan_scan_fetch_error", error=str(e))
            return 0

        if not isinstance(positions, list):
            logger.warning("orphan_scan_unexpected_payload", payload_type=type(positions).__name__)
            return 0

        redeemable = []
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            # Data API marks settled winning positions as redeemable=True
            if not pos.get("redeemable"):
                continue
            cid = pos.get("conditionId") or pos.get("condition_id")
            if not cid:
                continue
            # Skip already-pending redeems to avoid log spam
            if cid in self._pending_redeems:
                continue
            redeemable.append((cid, pos))

        logger.info(
            "orphan_scan_complete",
            funder=self._funder[:10] + "...",
            total_positions=len(positions),
            redeemable_found=len(redeemable),
        )

        triggered = 0
        for cid, pos in redeemable:
            size = pos.get("size", 0)
            current_value = pos.get("currentValue", 0)
            logger.info(
                "orphan_redeem_triggering",
                condition_id=cid[:20] + "...",
                size=size,
                current_value=current_value,
                title=(pos.get("title") or "")[:60],
            )
            try:
                await self.redeem_position(cid)
                triggered += 1
            except Exception as e:
                logger.warning(
                    "orphan_redeem_error",
                    condition_id=cid[:20] + "...",
                    error=str(e),
                )
            # Small spacing to avoid hammering the relayer
            await asyncio.sleep(1)

        if triggered > 0:
            # Refresh balance after redeems so the new floor logic sees the
            # recovered USDC immediately instead of waiting up to an hour.
            try:
                await self._refresh_balance()
            except Exception as e:
                logger.debug("orphan_scan_balance_refresh_failed", error=str(e))

        return triggered

    async def _redeem_retry_loop(self):
        """Periodically retry failed redeems with exponential backoff.

        Starts at 60s, doubles on each consecutive failure up to 1 hour.
        Resets to 60s after a successful redeem.
        """
        BASE_INTERVAL = 60
        MAX_INTERVAL = 3600  # 1 hour max between retries
        MAX_RETRIES_PER_CONDITION = 20  # Give up after 20 attempts
        interval = BASE_INTERVAL
        retry_counts: dict[str, int] = {}  # condition_id -> attempt count
        logger.info("redeem_retry_loop_started")
        while True:
            try:
                await asyncio.sleep(interval)
                if not self._pending_redeems:
                    interval = BASE_INTERVAL  # Reset when queue is empty
                    retry_counts.clear()
                    continue

                to_retry = list(self._pending_redeems)
                logger.info("redeem_retry_batch", count=len(to_retry), interval=interval)
                any_success = False
                for cid in to_retry:
                    # Check retry count
                    attempts = retry_counts.get(cid, 0)
                    if attempts >= MAX_RETRIES_PER_CONDITION:
                        logger.warning(
                            "redeem_giving_up",
                            condition_id=cid[:20] + "...",
                            attempts=attempts,
                        )
                        self._pending_redeems.discard(cid)
                        continue

                    retry_counts[cid] = attempts + 1
                    success = await self.redeem_position(cid)
                    if success:
                        logger.info("redeem_retry_success", condition_id=cid[:20] + "...")
                        retry_counts.pop(cid, None)
                        any_success = True
                    await asyncio.sleep(5)

                # Exponential backoff on failure, reset on success
                if any_success:
                    interval = BASE_INTERVAL
                else:
                    interval = min(interval * 2, MAX_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("redeem_retry_error", error=str(e))
                interval = min(interval * 2, MAX_INTERVAL)

    async def close(self):
        """Stop order monitor and redeem retry loop."""
        for task in (self._monitor_task, self._redeem_retry_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
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

    def on_order_confirmed(self, callback):
        """Register callback when order is confirmed (LIVE).

        Signature: callback(order_id, condition_id, token_id, side, price, size, cost_usd)
        """
        self._on_order_confirmed = callback

    def on_order_cancelled(self, callback):
        """Register callback when order is cancelled (timeout or user).

        Signature: callback(condition_id, token_side)
        """
        self._on_order_cancelled = callback

    def on_position_redeemed(self, callback):
        """Register callback when a position is redeemed (winning).

        Signature: callback(condition_id, amount_usd)
        """
        self._on_position_redeemed = callback

    # ── Execution ─────────────────────────────────────────────────

    async def execute(self, opp: Opportunity) -> TradeRecord | None:
        """Execute a trade for the given opportunity.

        Routing priority:
        1. ``opp.mode`` (set by AccountRunner in Fase 8 multi-strategy)
        2. ``self.mode`` (legacy single-mode executor)

        In multi-strategy mode an account can have one strategy in paper and
        another in live — each opportunity carries its own mode tag.
        """
        if not self._initialized:
            logger.error("executor_not_initialized")
            return None

        # Determine effective mode per-opportunity (Fase 8) or fallback
        opp_mode = getattr(opp, "mode", "") or ""
        if opp_mode == "live":
            effective = ExecutionMode.LIVE
        elif opp_mode == "dry_run":
            effective = ExecutionMode.DRY_RUN
        elif opp_mode == "paper":
            effective = ExecutionMode.PAPER
        else:
            effective = self.mode  # Legacy fallback

        # In live mode, refresh balance before every trade
        if effective == ExecutionMode.LIVE:
            await self._refresh_balance()

        # Risk checks (live only — paper is unconstrained except daily cap)
        if effective in (ExecutionMode.LIVE, ExecutionMode.DRY_RUN):
            if not self._check_risk(opp):
                return None

        if effective == ExecutionMode.PAPER:
            return self._paper_trade(opp)

        if effective == ExecutionMode.DRY_RUN:
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

    # ── Fase 6: persistence helpers ─────────────────────────────────

    def _current_mode_str(self) -> str:
        """String form of the current execution mode for persistence."""
        return "live" if self.mode == ExecutionMode.LIVE else "paper"

    def _opp_strategy(self, opp: Opportunity) -> str:
        """Best-effort source_strategy tag for an opportunity.

        Until Fase 8 wires `opp.source_strategy` from the AccountRunner,
        fall back to the legacy `strategy_type` or to the executor's
        single-strategy default.
        """
        s = getattr(opp, "source_strategy", "") or ""
        if s:
            return s
        legacy = getattr(opp, "strategy_type", "") or ""
        if "copy" in legacy:
            return "copy_trade"
        return "directional"

    async def _persist_new_trade(self, trade: TradeRecord, opp: Opportunity, status_str: str) -> None:
        """Queue a record_trade call. Swallows errors and de-dupes by order_id."""
        if not trade.order_id or trade.order_id in self._persisted_orders:
            return
        try:
            from src.persistence import get_persistence
            persistence = get_persistence()
        except (ImportError, RuntimeError):
            return  # Persistence not initialized (tests, paper-only sandbox)
        try:
            mode_str = trade.mode or self._current_mode_str()
            await persistence.record_trade(
                account_name=self._account_name,
                source_strategy=self._opp_strategy(opp),
                mode=mode_str,
                order_id=trade.order_id,
                condition_id=opp.condition_id,
                question=getattr(opp, "question", "") or "",
                token_side=opp.token_side,
                token_id=trade.token_id,
                price=trade.price,
                size=trade.size,
                cost_usd=trade.cost_usd,
                status=status_str,
            )
            self._persisted_orders.add(trade.order_id)
        except Exception as e:
            logger.warning("persist_trade_failed", error=str(e), order_id=trade.order_id)

    async def _persist_status_update(
        self,
        trade: TradeRecord,
        status_str: str,
        settled_pnl: float | None = None,
        matched_at: float | None = None,
        settled_at: float | None = None,
        error: str | None = None,
    ) -> None:
        """Queue a status update for a persisted trade."""
        if not trade.order_id:
            return
        try:
            from src.persistence import get_persistence
            persistence = get_persistence()
        except (ImportError, RuntimeError):
            return
        try:
            await persistence.update_trade_status(
                account_name=self._account_name,
                order_id=trade.order_id,
                status=status_str,
                matched_at=matched_at,
                settled_at=settled_at,
                settled_pnl=settled_pnl,
                error=error,
            )
        except Exception as e:
            logger.warning("persist_status_failed", error=str(e), order_id=trade.order_id)

    async def load_persisted_state(self) -> int:
        """Reload open trades from SQLite into the in-memory ledgers.

        Called by AccountRunner after `initialize()` so that trades placed
        in a previous bot run keep being tracked. Returns the number of
        trades restored.
        """
        try:
            from src.persistence import get_persistence
            persistence = get_persistence()
        except (ImportError, RuntimeError):
            return 0

        restored = 0
        for mode_str in ("live", "paper"):
            try:
                rows = await persistence.query_open_trades(
                    self._account_name, mode=mode_str
                )
            except Exception as e:
                logger.warning("persist_query_failed", mode=mode_str, error=str(e))
                continue
            for row in rows:
                # Skip already-tracked orders (e.g. re-entry after redeploy)
                if any(t.order_id == row.get("order_id") for t in self._trades):
                    continue
                try:
                    status_raw = (row.get("status") or "pending").lower()
                    try:
                        status_enum = OrderStatus(status_raw)
                    except ValueError:
                        status_enum = OrderStatus.PENDING
                    trade = TradeRecord(
                        order_id=row.get("order_id") or "",
                        condition_id=row.get("condition_id") or "",
                        question=row.get("question") or "",
                        token_id=row.get("token_id") or "",
                        token_side=row.get("token_side") or "",
                        price=row.get("price") or 0.0,
                        size=row.get("size") or 0.0,
                        cost_usd=row.get("cost_usd") or 0.0,
                        status=status_enum,
                        created_at=row.get("created_at") or 0.0,
                        matched_at=row.get("matched_at") or 0.0,
                        size_matched=row.get("size") or 0.0,
                        error=row.get("error") or "",
                        source_strategy=row.get("source_strategy") or "",
                        mode=mode_str,
                    )
                    self._trades.append(trade)
                    if trade.order_id:
                        self._persisted_orders.add(trade.order_id)
                    if mode_str == "live" and status_enum in (
                        OrderStatus.PENDING, OrderStatus.LIVE,
                    ):
                        self._pending_orders[trade.order_id] = trade
                    restored += 1
                except Exception as e:
                    logger.warning("persist_restore_row_failed", error=str(e))
        if restored:
            logger.info(
                "persisted_state_restored",
                account=self._account_name,
                restored=restored,
            )
        return restored

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
            source_strategy=self._opp_strategy(opp),
            mode="paper",
        )
        self._trades.append(trade)
        # Fase 6: persist (fire-and-forget via asyncio.create_task because
        # _paper_trade is sync). The persistence layer queues internally,
        # so this just kicks off a coroutine that returns immediately.
        try:
            asyncio.create_task(self._persist_new_trade(trade, opp, "confirmed"))
        except RuntimeError:
            pass  # No running loop (tests)
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
            source_strategy=self._opp_strategy(opp),
            mode="live",
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
                # Deduct optimistically now, real refresh follows async
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
                # Notify detector that order was confirmed (for balance sync in live mode)
                if self._on_order_confirmed:
                    try:
                        await self._on_order_confirmed(
                            trade.order_id,
                            opp.condition_id,
                            resolved_token_id,
                            opp.token_side,
                            opp.token_price,
                            shares,
                            opp.suggested_bet,
                        )
                    except Exception as e:
                        logger.warning("order_confirmed_callback_error", error=str(e))
                # Refresh real balance async after order settles on-chain
                asyncio.create_task(self._refresh_balance_delayed(delay_seconds=5))
                # Fase 6: persist with status='pending' (live not yet matched)
                await self._persist_new_trade(trade, opp, "pending")
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
                    # Refresh real balance after fill
                    await self._refresh_balance()
                    # Fase 6: persist transition
                    await self._persist_status_update(
                        trade, "confirmed", matched_at=now,
                    )
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
                    # Refresh real balance instead of optimistic refund
                    await self._refresh_balance()
                    # Fase 6: persist transition
                    await self._persist_status_update(
                        trade, "cancelled", error=trade.error,
                    )
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
            # Refresh real balance instead of optimistic refund
            await self._refresh_balance()
            # Fase 6: persist transition
            await self._persist_status_update(trade, "cancelled", error=trade.error)
            logger.info(
                "order_cancelled_timeout",
                order_id=order_id,
                question=trade.question[:60],
                age=f"{time.time() - trade.created_at:.0f}s",
            )
            # Notify detector to mark bet as cancelled
            if self._on_order_cancelled:
                try:
                    await self._on_order_cancelled(trade.condition_id, trade.token_side)
                except Exception as e:
                    logger.warning(
                        "order_cancelled_callback_error",
                        error=str(e),
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
        if self._pending_redeems:
            stats["pending_redeems"] = len(self._pending_redeems)
        stats["redeem_available"] = self._redeem_available
        return stats
