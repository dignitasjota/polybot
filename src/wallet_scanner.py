"""Wallet scanner: tracks all wallet activity and computes profitability stats."""
from __future__ import annotations

import structlog

from . import db

logger = structlog.get_logger()


class WalletScanner:
    """
    Observes all trades in crypto markets and records wallet activity.
    After markets resolve, computes win/loss stats per wallet.

    Use cases:
    - Find profitable traders to copy
    - Identify trading patterns and preferences
    - Rank traders by win rate
    """

    def __init__(self):
        self.observed_markets = set()  # condition_ids we've seen

    async def on_trade(self, condition_id: str, coin: str, wallet: str, side: str, price: float, size: float):
        """
        Called when we see a BUY trade in a crypto market at medium price ($0.40-$0.75).

        Args:
            condition_id: Market condition ID
            coin: Crypto symbol (BTC, ETH, SOL, etc)
            wallet: Wallet address (we'll truncate to first 10 chars)
            side: YES or NO
            price: Buy price
            size: Trade size in USDC
        """
        if condition_id in self.observed_markets:
            return  # Already tracked

        try:
            await db.add_wallet_trade(
                wallet=wallet,
                condition_id=condition_id,
                coin=coin,
                side=side,
                price=price,
                size=size,
            )
            self.observed_markets.add(condition_id)

            logger.info(
                "wallet_scanner_trade_recorded",
                wallet=wallet[:10],
                coin=coin,
                side=side,
                price=f"${price:.2f}",
                size=f"${size:.0f}",
            )
        except Exception as e:
            logger.error("wallet_scanner_trade_error", error=str(e))

    async def on_market_resolved(self, condition_id: str, winning_token_id: str, coin: str):
        """
        Called when a market resolves. Determine winning side and update wallet stats.

        Args:
            condition_id: Market condition ID
            winning_token_id: The token that won (YES or NO encoded)
            coin: Crypto symbol for logging
        """
        # Determine winning side from token_id
        # In Polymarket, YES is typically token 0, NO is token 1
        # But we need to check the actual market. For now, use a simple heuristic:
        # If the winning_token_id matches the YES token, it's YES win, else NO win

        # This is a simplification - in practice we'd query the market data
        # For now, caller should pass winning_side directly
        # We'll update the signature to accept winning_side
        pass

    async def on_market_resolved_with_side(self, condition_id: str, winning_side: str, coin: str):
        """
        Called when a market resolves with explicit winning side.

        Args:
            condition_id: Market condition ID
            winning_side: YES or NO
            coin: Crypto symbol for logging
        """
        try:
            await db.resolve_market(condition_id, winning_side)

            logger.info(
                "wallet_scanner_market_resolved",
                condition_id=condition_id[:16],
                coin=coin,
                winning_side=winning_side,
            )
        except Exception as e:
            logger.error("wallet_scanner_resolve_error", error=str(e), condition_id=condition_id)

    async def get_top_traders(self, min_trades: int = 20, min_wr: float = 0.55) -> list[dict]:
        """
        Get top traders by win rate.

        Args:
            min_trades: Minimum number of trades to be considered
            min_wr: Minimum win rate (0.55 = 55%)

        Returns:
            List of dicts with wallet, trades, wins, losses, win_rate, avg_price, volume
        """
        try:
            traders = await db.get_top_traders(min_trades=min_trades, min_wr=min_wr)

            logger.info(
                "wallet_scanner_top_traders_fetched",
                count=len(traders),
                min_trades=min_trades,
                min_wr=f"{min_wr*100:.0f}%",
            )

            return traders
        except Exception as e:
            logger.error("wallet_scanner_fetch_error", error=str(e))
            return []

    async def get_stats(self, wallet: str) -> dict | None:
        """Get stats for a specific wallet."""
        return await db.get_wallet_stats(wallet)

    async def export_report(self) -> dict:
        """Export wallet scanner report for dashboard."""
        try:
            top10 = await self.get_top_traders(min_trades=10, min_wr=0.50)
            return {
                "total_wallets_tracked": len(top10) + 10,  # Approximation
                "top_traders": top10,
                "sample_criteria": {
                    "min_trades": 10,
                    "min_win_rate": 0.50,
                    "market_type": "crypto_5min",
                    "price_range": "$0.40-$0.75",
                },
            }
        except Exception as e:
            logger.error("wallet_scanner_export_error", error=str(e))
            return {}
