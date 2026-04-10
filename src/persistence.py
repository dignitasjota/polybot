"""Persistence layer for trading bot with SQLite WAL + async writer-task.

Provides non-blocking writes to SQLite via a background writer-task that
consumes an asyncio.Queue. Tables: opportunities, trades, stats_snapshots.

Coexists with src/db.py (which manages users, wallet_overrides, audit_log,
and wallet activity tables). Both use the same SQLite file (data/panel.db).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import aiosqlite
import structlog

logger = structlog.get_logger("polymarket.persistence")

# PRAGMAs para optimización WAL
PRAGMAS = {
    "journal_mode": "WAL",
    "synchronous": "NORMAL",
    "cache_size": "-64000",  # 64 MB
    "temp_store": "MEMORY",
    "mmap_size": "268435456",  # 256 MB
    "foreign_keys": "ON",
    "wal_autocheckpoint": "1000",
}


# ─── Operaciones encoladas ────────────────────────────────────────────────

@dataclass
class PersistOp:
    """Operación asíncrona de persistencia."""

    async def execute(self, db: aiosqlite.Connection) -> None:
        raise NotImplementedError


@dataclass
class InsertOpportunity(PersistOp):
    account_name: str
    source_strategy: str
    mode: str
    timestamp: float
    condition_id: str
    question: str
    token_side: str
    token_id: str
    token_price: float
    margin_net: float
    suggested_bet: float
    decision: str
    decision_reason: str
    extra_json: str | None = None

    async def execute(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            """INSERT INTO opportunities
               (account_name, source_strategy, mode, timestamp, condition_id, question,
                token_side, token_id, token_price, margin_net, suggested_bet, decision,
                decision_reason, extra_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self.account_name, self.source_strategy, self.mode, self.timestamp,
             self.condition_id, self.question, self.token_side, self.token_id,
             self.token_price, self.margin_net, self.suggested_bet, self.decision,
             self.decision_reason, self.extra_json),
        )


@dataclass
class InsertTrade(PersistOp):
    account_name: str
    source_strategy: str
    mode: str
    order_id: str
    condition_id: str
    question: str
    token_side: str
    token_id: str
    price: float
    size: float
    cost_usd: float
    status: str
    created_at: float
    extra_json: str | None = None

    async def execute(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            """INSERT INTO trades
               (account_name, source_strategy, mode, order_id, condition_id, question,
                token_side, token_id, price, size, cost_usd, status, created_at, extra_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self.account_name, self.source_strategy, self.mode, self.order_id,
             self.condition_id, self.question, self.token_side, self.token_id,
             self.price, self.size, self.cost_usd, self.status, self.created_at,
             self.extra_json),
        )


@dataclass
class UpdateTradeStatus(PersistOp):
    account_name: str
    order_id: str
    status: str
    matched_at: float | None = None
    settled_at: float | None = None
    settled_pnl: float | None = None
    error: str | None = None

    async def execute(self, db: aiosqlite.Connection) -> None:
        updates = ["status = ?"]
        values: list[Any] = [self.status]
        if self.matched_at is not None:
            updates.append("matched_at = ?")
            values.append(self.matched_at)
        if self.settled_at is not None:
            updates.append("settled_at = ?")
            values.append(self.settled_at)
        if self.settled_pnl is not None:
            updates.append("settled_pnl = ?")
            values.append(self.settled_pnl)
        if self.error is not None:
            updates.append("error = ?")
            values.append(self.error)
        values.append(self.order_id)
        values.append(self.account_name)
        query = (
            f"UPDATE trades SET {', '.join(updates)} "
            f"WHERE order_id = ? AND account_name = ?"
        )
        await db.execute(query, values)


@dataclass
class InsertSnapshot(PersistOp):
    account_name: str
    source_strategy: str | None
    mode: str
    timestamp: float
    balance: float
    daily_pnl: float
    total_pnl: float
    trades_count: int
    wins: int
    losses: int
    pending: int
    open_positions: int
    opportunities_detected: int
    opportunities_placed: int

    async def execute(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            """INSERT INTO stats_snapshots
               (account_name, source_strategy, mode, timestamp, balance, daily_pnl, total_pnl,
                trades_count, wins, losses, pending, open_positions,
                opportunities_detected, opportunities_placed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self.account_name, self.source_strategy, self.mode, self.timestamp,
             self.balance, self.daily_pnl, self.total_pnl, self.trades_count,
             self.wins, self.losses, self.pending, self.open_positions,
             self.opportunities_detected, self.opportunities_placed),
        )


# ─── PersistenceLayer ─────────────────────────────────────────────────────

class PersistenceLayer:
    """SQLite persistence with async writer-task for non-blocking writes."""

    def __init__(self, db_path: str = "data/panel.db"):
        self._db_path = db_path
        self._write_queue: asyncio.Queue[PersistOp | None] = asyncio.Queue()
        self._writer_task: asyncio.Task[None] | None = None
        self._shutdown = False

    async def start(self) -> None:
        """Initialize DB schema and start background writer-task."""
        await self._init_schema()
        self._writer_task = asyncio.create_task(self._writer_loop())
        logger.info("persistence_started", db_path=self._db_path)

    async def stop(self) -> None:
        """Graceful shutdown: drain queue and stop writer task."""
        self._shutdown = True
        if self._writer_task and not self._writer_task.done():
            await self._write_queue.put(None)  # Señal de terminación
            try:
                await asyncio.wait_for(self._writer_task, timeout=10)
            except asyncio.TimeoutError:
                logger.warning("persistence_shutdown_timeout")
                self._writer_task.cancel()
        logger.info("persistence_stopped")

    async def _init_schema(self) -> None:
        """Crear tablas si no existen y aplicar PRAGMAs."""
        async with aiosqlite.connect(self._db_path) as db:
            for key, value in PRAGMAS.items():
                await db.execute(f"PRAGMA {key}={value}")
            await db.executescript(self._get_schema_sql())
            await db.commit()

    @staticmethod
    def _get_schema_sql() -> str:
        return """
            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL,
                source_strategy TEXT NOT NULL,
                mode TEXT NOT NULL,
                timestamp REAL NOT NULL,
                condition_id TEXT NOT NULL,
                question TEXT,
                token_side TEXT,
                token_id TEXT,
                token_price REAL,
                margin_net REAL,
                suggested_bet REAL,
                decision TEXT NOT NULL,
                decision_reason TEXT,
                extra_json TEXT,
                created_at REAL DEFAULT (unixepoch())
            );
            CREATE INDEX IF NOT EXISTS idx_opp_account_strategy_time
                ON opportunities(account_name, source_strategy, timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_opp_condition ON opportunities(condition_id);
            CREATE INDEX IF NOT EXISTS idx_opp_decision ON opportunities(decision);

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL,
                source_strategy TEXT NOT NULL,
                mode TEXT NOT NULL,
                order_id TEXT,
                condition_id TEXT NOT NULL,
                question TEXT,
                token_side TEXT NOT NULL,
                token_id TEXT,
                price REAL,
                size REAL,
                cost_usd REAL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL,
                matched_at REAL,
                settled_at REAL,
                settled_pnl REAL,
                error TEXT,
                extra_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_trades_account_strategy_time
                ON trades(account_name, source_strategy, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_trades_account_mode
                ON trades(account_name, mode);
            CREATE INDEX IF NOT EXISTS idx_trades_condition_status
                ON trades(condition_id, status);
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_order_id ON trades(order_id);

            CREATE TABLE IF NOT EXISTS stats_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL,
                source_strategy TEXT,
                mode TEXT NOT NULL,
                timestamp REAL NOT NULL,
                balance REAL,
                daily_pnl REAL,
                total_pnl REAL,
                trades_count INTEGER,
                wins INTEGER,
                losses INTEGER,
                pending INTEGER,
                open_positions INTEGER,
                opportunities_detected INTEGER,
                opportunities_placed INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_snap_account_strategy_time
                ON stats_snapshots(account_name, source_strategy, timestamp DESC);
        """

    async def _writer_loop(self) -> None:
        """Background writer task: consume queue and persist to SQLite."""
        try:
            async with aiosqlite.connect(self._db_path) as db:
                for key, value in PRAGMAS.items():
                    await db.execute(f"PRAGMA {key}={value}")

                while True:
                    op = await self._write_queue.get()
                    if op is None:  # Shutdown signal
                        break
                    try:
                        await op.execute(db)
                        await db.commit()
                    except Exception as e:
                        logger.error(
                            "persistence_write_error",
                            error=str(e),
                            op_type=type(op).__name__,
                        )
                        try:
                            await db.rollback()
                        except Exception:
                            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("persistence_writer_loop_error", error=str(e))

    # ─── API pública (encolan ops) ────────────────────────────────────────

    async def record_opportunity(
        self,
        account_name: str,
        source_strategy: str,
        mode: str,
        condition_id: str,
        question: str,
        token_side: str,
        token_id: str,
        token_price: float,
        margin_net: float,
        suggested_bet: float,
        decision: str,
        decision_reason: str = "",
        extra_json: dict[str, Any] | None = None,
    ) -> None:
        op = InsertOpportunity(
            account_name=account_name,
            source_strategy=source_strategy,
            mode=mode,
            timestamp=time.time(),
            condition_id=condition_id,
            question=question,
            token_side=token_side,
            token_id=token_id,
            token_price=token_price,
            margin_net=margin_net,
            suggested_bet=suggested_bet,
            decision=decision,
            decision_reason=decision_reason,
            extra_json=json.dumps(extra_json) if extra_json else None,
        )
        await self._write_queue.put(op)

    async def record_trade(
        self,
        account_name: str,
        source_strategy: str,
        mode: str,
        order_id: str,
        condition_id: str,
        question: str,
        token_side: str,
        token_id: str,
        price: float,
        size: float,
        cost_usd: float,
        status: str = "pending",
        extra_json: dict[str, Any] | None = None,
    ) -> None:
        op = InsertTrade(
            account_name=account_name,
            source_strategy=source_strategy,
            mode=mode,
            order_id=order_id,
            condition_id=condition_id,
            question=question,
            token_side=token_side,
            token_id=token_id,
            price=price,
            size=size,
            cost_usd=cost_usd,
            status=status,
            created_at=time.time(),
            extra_json=json.dumps(extra_json) if extra_json else None,
        )
        await self._write_queue.put(op)

    async def update_trade_status(
        self,
        account_name: str,
        order_id: str,
        status: str,
        matched_at: float | None = None,
        settled_at: float | None = None,
        settled_pnl: float | None = None,
        error: str | None = None,
    ) -> None:
        op = UpdateTradeStatus(
            account_name=account_name,
            order_id=order_id,
            status=status,
            matched_at=matched_at,
            settled_at=settled_at,
            settled_pnl=settled_pnl,
            error=error,
        )
        await self._write_queue.put(op)

    async def snapshot_stats(
        self,
        account_name: str,
        source_strategy: str | None,
        mode: str,
        balance: float,
        daily_pnl: float,
        total_pnl: float,
        trades_count: int,
        wins: int,
        losses: int,
        pending: int,
        open_positions: int,
        opportunities_detected: int,
        opportunities_placed: int,
    ) -> None:
        op = InsertSnapshot(
            account_name=account_name,
            source_strategy=source_strategy,
            mode=mode,
            timestamp=time.time(),
            balance=balance,
            daily_pnl=daily_pnl,
            total_pnl=total_pnl,
            trades_count=trades_count,
            wins=wins,
            losses=losses,
            pending=pending,
            open_positions=open_positions,
            opportunities_detected=opportunities_detected,
            opportunities_placed=opportunities_placed,
        )
        await self._write_queue.put(op)

    # ─── API de consulta (lectura directa) ───────────────────────────────

    async def query_open_trades(
        self,
        account_name: str,
        mode: str | None = None,
        strategy: str | None = None,
    ) -> list[dict[str, Any]]:
        """Trades en estado pending o confirmed (no redeemed/failed)."""
        query = (
            "SELECT * FROM trades WHERE account_name = ? "
            "AND status IN ('pending', 'confirmed')"
        )
        params: list[Any] = [account_name]
        if mode:
            query += " AND mode = ?"
            params.append(mode)
        if strategy:
            query += " AND source_strategy = ?"
            params.append(strategy)
        query += " ORDER BY created_at DESC"

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                return [dict(row) async for row in cursor]

    async def query_stats(
        self,
        account_name: str,
        mode: str,
        strategy: str | None = None,
        since: float | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM stats_snapshots WHERE account_name = ? AND mode = ?"
        params: list[Any] = [account_name, mode]
        if strategy:
            query += " AND source_strategy = ?"
            params.append(strategy)
        else:
            query += " AND source_strategy IS NULL"
        if since:
            query += " AND timestamp >= ?"
            params.append(since)
        query += " ORDER BY timestamp DESC"

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                return [dict(row) async for row in cursor]

    # ─── Mantenimiento ────────────────────────────────────────────────────

    async def cleanup_old_data(self) -> None:
        """Delete data beyond retention policy. Run monthly."""
        now = time.time()
        one_year = 365 * 86400
        ninety_days = 90 * 86400
        thirty_days = 30 * 86400

        async with aiosqlite.connect(self._db_path) as db:
            for key, value in PRAGMAS.items():
                await db.execute(f"PRAGMA {key}={value}")

            # Live: 1 year retention
            await db.execute(
                "DELETE FROM trades WHERE mode='live' AND created_at < ?",
                (now - one_year,),
            )
            await db.execute(
                "DELETE FROM opportunities WHERE mode='live' AND timestamp < ?",
                (now - one_year,),
            )
            # Paper: 90 days for trades, 30 days for opportunities
            await db.execute(
                "DELETE FROM trades WHERE mode='paper' AND created_at < ?",
                (now - ninety_days,),
            )
            await db.execute(
                "DELETE FROM opportunities WHERE mode='paper' AND timestamp < ?",
                (now - thirty_days,),
            )
            await db.commit()
            logger.info("persistence_cleanup_completed")

    async def vacuum(self) -> None:
        """Compact database. Run monthly after cleanup."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("VACUUM")
        logger.info("persistence_vacuum_completed")


# ─── Singleton global ─────────────────────────────────────────────────────

_persistence: PersistenceLayer | None = None


async def init_persistence(db_path: str = "data/panel.db") -> PersistenceLayer:
    """Initialize the global persistence layer."""
    global _persistence
    if _persistence is not None:
        return _persistence
    _persistence = PersistenceLayer(db_path)
    await _persistence.start()
    return _persistence


def get_persistence() -> PersistenceLayer:
    """Get the global persistence instance."""
    if _persistence is None:
        raise RuntimeError("Persistence layer not initialized — call init_persistence() first")
    return _persistence


async def close_persistence() -> None:
    """Graceful shutdown of persistence layer."""
    global _persistence
    if _persistence:
        await _persistence.stop()
        _persistence = None
