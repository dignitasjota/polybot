"""DirectionalStrategy: Strategy ABC adapter over ClosingArbitrageDetector.

Composes the existing detector instance and exposes it through the
Strategy ABC interface. The legacy detector module (src/detector.py) is
left untouched so the running bot keeps working until Fase 8 wires this
adapter into the AccountRunner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from src.detector import ClosingArbitrageDetector, Opportunity
from src.strategies.base import AccountContext, Strategy, StrategyConfig
from src.strategies.registry import register_strategy

logger = structlog.get_logger("polymarket.strategies.directional")


@dataclass
class DirectionalConfig(StrategyConfig):
    """Configuration for the directional (closing arb + up/down) strategy.

    Inherits mode/priority/max_concurrent_bets/max_bet_per_trade/paper_daily_trade_cap
    from StrategyConfig and adds the directional-specific fields.

    Note: this is the *new* per-strategy config used in the multi-strategy
    architecture. The legacy ``src.config.StrategyConfig`` will be translated
    into this in Fase 8 (AccountRunner) by `from_legacy()`.
    """
    max_price: float = 0.70
    min_buffer_pct: float = 0.0001
    min_margin_net: float = 0.05
    tag: str = "crypto"
    crypto_configs: dict[str, Any] = field(default_factory=dict)
    probability_tiers: list[Any] = field(default_factory=list)

    @classmethod
    def from_legacy(cls, legacy: Any, mode: str = "disabled") -> DirectionalConfig:
        """Build a DirectionalConfig from the legacy src.config.StrategyConfig."""
        return cls(
            mode=mode,
            max_concurrent_bets=getattr(legacy, "max_concurrent_bets", 3),
            max_price=getattr(legacy, "max_price", 0.70),
            min_buffer_pct=getattr(legacy, "min_buffer_pct", 0.0001),
            min_margin_net=getattr(legacy, "min_margin_net", 0.05),
            tag=getattr(legacy, "tag", "crypto"),
            crypto_configs=getattr(legacy, "crypto_configs", {}) or {},
            probability_tiers=getattr(legacy, "probability_tiers", []) or [],
        )


class DirectionalStrategy(Strategy):
    """Strategy ABC wrapper around an existing ClosingArbitrageDetector.

    Composition (not inheritance): the detector is created externally and
    injected, so the existing detector module stays unchanged. This adapter
    only forwards lifecycle and reporting calls.
    """

    def __init__(
        self,
        config: DirectionalConfig,
        context: AccountContext,
        detector: ClosingArbitrageDetector,
    ):
        super().__init__(config, context)
        self.config: DirectionalConfig = config
        self._detector = detector

    @property
    def detector(self) -> ClosingArbitrageDetector:
        """Underlying ClosingArbitrageDetector (for callback wiring)."""
        return self._detector

    async def start(self) -> None:
        """Start the price checker (the WS feed itself is owned by the runner)."""
        try:
            await self._detector._price_checker.start()
        except Exception as e:
            logger.warning("directional_start_error", error=str(e))

    async def stop(self) -> None:
        try:
            await self._detector.close()
        except Exception as e:
            logger.warning("directional_stop_error", error=str(e))

    def get_stats(self) -> dict[str, Any]:
        """Detection stats only — no balance/P&L (those belong to the executor)."""
        stats = self._detector.get_stats() if self._detector else {}
        # Strip balance/P&L fields if present so this returns only detection metrics.
        for k in ("balance", "starting_balance", "total_pnl", "daily_pnl",
                  "wins", "losses", "win_rate"):
            stats.pop(k, None)
        return stats

    def get_config(self) -> dict[str, Any]:
        return {
            "mode": self.config.mode,
            "priority": self.config.priority,
            "max_concurrent_bets": self.config.max_concurrent_bets,
            "max_bet_per_trade": self.config.max_bet_per_trade,
            "paper_daily_trade_cap": self.config.paper_daily_trade_cap,
            "max_price": self.config.max_price,
            "min_buffer_pct": self.config.min_buffer_pct,
            "min_margin_net": self.config.min_margin_net,
            "tag": self.config.tag,
        }

    async def restore_open_positions(self, positions: list[Any]) -> None:
        """Restore in-memory dedupe state from persisted trades after redeploy.

        Filters by source_strategy='directional' and rebuilds the
        ``_bet_placed`` map so the detector won't double-bet markets that
        already have an open position.
        """
        if not self._detector:
            return
        restored = 0
        for trade in positions:
            if getattr(trade, "source_strategy", None) != "directional":
                continue
            condition_id = getattr(trade, "condition_id", None)
            token_side = getattr(trade, "token_side", None)
            if not condition_id or not token_side:
                continue
            key = f"{condition_id}:{token_side}"
            # Build a minimal Opportunity placeholder so dedupe checks fire.
            opp = Opportunity(
                timestamp=getattr(trade, "created_at", 0.0),
                condition_id=condition_id,
                question=getattr(trade, "question", ""),
                token_side=token_side,
                token_id=getattr(trade, "token_id", "") or "",
                token_price=getattr(trade, "price", 0.0) or 0.0,
                implied_probability=0.0,
                margin_gross=0.0,
                fee_estimated=0.0,
                margin_net=0.0,
                depth_at_price=0.0,
                resolved=False,
                winning_token_id="",
                source_strategy="directional",
                mode=self.config.mode,
            )
            self._detector._bet_placed[key] = opp
            restored += 1
        if restored:
            logger.info("directional_restored_positions", count=restored)


# Registrar en el registry global
register_strategy("directional", DirectionalStrategy, DirectionalConfig)
