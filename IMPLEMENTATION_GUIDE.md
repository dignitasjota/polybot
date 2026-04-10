# Guía de Implementación: Multi-estrategia con Persistencia

**Estado**: Plan v7 - Listo para ejecutar
**Fecha**: 2026-04-09
**Objetivo**: Refactorizar el bot de single-strategy a multi-strategy por cuenta, con persistencia en SQLite y support para future strategies (market making, etc).

---

## Tabla de Contenidos

1. [Resumen Ejecutivo](#resumen-ejecutivo)
2. [Cambios de Datos](#cambios-de-datos)
3. [14 Pasos de Implementación](#14-pasos-de-implementación)
4. [Checklist de Completitud](#checklist-de-completitud)
5. [Rollback y Planes de Contingencia](#rollback-y-planes-de-contingencia)

---

## Resumen Ejecutivo

### Antes (Actual)
- 1 cuenta = 1 estrategia (strategy_type: "directional" | "copy_trade")
- Estado en memoria, se pierde con redeploy
- Directional y copy_trade comparten wallet pero no comparten balance/stats
- Panel hardcodeado a estos dos tipos

### Después (Propuesto)
- 1 cuenta = N estrategias activas en paralelo
- Todas comparten wallet, balance USDC, limite de risk (max_concurrent_positions, max_daily_loss)
- Cada estrategia genera sus propias oportunidades pero compiten por ejecutarse via un executor único
- Estadísticas separadas por estrategia pero agregadas por cuenta
- Persistencia completa en SQLite: sobrevive redeploys
- Panel genérico, extensible a nuevas estrategias sin cambios de código

### Impacto en Datos
```
Antes:
  user_main cuenta:
    - executor: balance, trades (solo live)
    - directional: detector.stats, detector._bet_placed (en memoria)
    - copy_trade: copy_trader.stats, copy_trader._bets (en memoria)
  (Pérdida total si crash)

Después:
  user_main cuenta:
    - executor.ledger_live: balance real, trades persistidos
    - executor.ledger_paper: balance simulado, trades persistidos
    - directional.stats: en memoria + agregado en stats_snapshots
    - copy_trade.stats: en memoria + agregado en stats_snapshots
  (Recuperable de SQLite tras redeploy)
```

---

## Cambios de Datos

### 1. Nuevas Tablas en SQLite (`data/panel.db`)

Añadir a la función `init_db()` en `src/db.py`:

#### `opportunities`
```sql
CREATE TABLE opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name TEXT NOT NULL,
    source_strategy TEXT NOT NULL,           -- "directional", "copy_trade", etc
    mode TEXT NOT NULL,                       -- "paper", "live"
    timestamp REAL NOT NULL,                  -- time.time()
    condition_id TEXT NOT NULL,
    question TEXT,
    token_side TEXT,                          -- "YES", "NO"
    token_id TEXT,
    token_price REAL,
    margin_net REAL,
    suggested_bet REAL,
    decision TEXT NOT NULL,                   -- "placed", "rejected_risk", "rejected_opposite_side_exists", "rejected_dedupe_same_strategy", "rejected_strategy_limit"
    decision_reason TEXT,
    extra_json TEXT,                          -- JSON con campos específicos por estrategia (ej: {"buffer_used": 0.0001, "change_pct": 0.0005})
    created_at REAL DEFAULT (unixepoch())
);
CREATE INDEX idx_opp_account_strategy_time ON opportunities(account_name, source_strategy, timestamp DESC);
CREATE INDEX idx_opp_condition ON opportunities(condition_id);
CREATE INDEX idx_opp_decision ON opportunities(decision);
```

#### `trades`
```sql
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name TEXT NOT NULL,
    source_strategy TEXT NOT NULL,           -- qué estrategia colocó este trade
    mode TEXT NOT NULL,                       -- "paper" o "live"
    order_id TEXT,                            -- vacío en paper, UUID en live
    condition_id TEXT NOT NULL,
    question TEXT,
    token_side TEXT NOT NULL,                 -- "YES", "NO"
    token_id TEXT,
    price REAL,
    size REAL,                                -- shares
    cost_usd REAL,                            -- cantidad invertida
    status TEXT NOT NULL DEFAULT 'pending',   -- "pending", "confirmed", "cancelled", "failed", "redeemed"
    created_at REAL NOT NULL,                 -- timestamp de creación
    matched_at REAL,                          -- timestamp de match/confirm
    settled_at REAL,                          -- timestamp de resolución
    settled_pnl REAL,                         -- P&L una vez resuelto (NULL hasta resolución)
    error TEXT,                               -- mensaje de error si status='failed'
    extra_json TEXT                           -- JSON con datos específicos
);
CREATE INDEX idx_trades_account_strategy_time ON trades(account_name, source_strategy, created_at DESC);
CREATE INDEX idx_trades_account_mode ON trades(account_name, mode);
CREATE INDEX idx_trades_condition_status ON trades(condition_id, status);
CREATE INDEX idx_trades_status ON trades(status);
```

#### `stats_snapshots`
```sql
CREATE TABLE stats_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name TEXT NOT NULL,
    source_strategy TEXT,                     -- NULL = stats agregadas de la cuenta
    mode TEXT NOT NULL,                       -- "paper" o "live"
    timestamp REAL NOT NULL,                  -- snapshot timestamp
    balance REAL,                             -- saldo en ese momento
    daily_pnl REAL,                          -- P&L del día
    total_pnl REAL,                          -- P&L acumulado (modo)
    trades_count INTEGER,                     -- total trades
    wins INTEGER,                             -- trades ganadores
    losses INTEGER,                           -- trades perdedores
    pending INTEGER,                          -- trades sin resolver
    open_positions INTEGER,                   -- posiciones abiertas
    opportunities_detected INTEGER,           -- oportunidades vistas
    opportunities_placed INTEGER              -- oportunidades ejecutadas
);
CREATE INDEX idx_snap_account_strategy_time ON stats_snapshots(account_name, source_strategy, timestamp DESC);
```

### 2. Cambios en Tabla Existente `users`

```sql
ALTER TABLE users ADD COLUMN account_name TEXT DEFAULT NULL;
-- NULL = ve todas las cuentas (admin actual)
-- Futuro: restricción a cuenta específica si no NULL
```

### 3. Cambios en Tablas Existentes `audit_log`

Añadir acciones nuevas (sin cambiar schema):
- `strategy_mode_change`: cuando se cambia modo de una estrategia
- `strategy_config_update`: cuando se actualizan parámetros
- `opportunity_decision`: cuando hay un rechazo por risk importante

---

## 14 Pasos de Implementación

### Paso 1: Schema SQLite + `persistence.py` (Aislado)

**Objetivo**: Crear la capa de persistencia completa sin tocar el bot.

**Archivos a crear**:
- `src/persistence.py` — módulo de persistencia con writer-task

**Archivos a modificar**:
- `src/db.py` — añadir las nuevas tablas en `init_db()`

**Código de `src/persistence.py`**:

```python
"""Persistence layer for trading bot with SQLite WAL + async writer-task."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import aiosqlite

logger = logging.getLogger("polymarket.persistence")

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

# Operaciones que se encolan para el writer-task
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
    decision: str  # "placed", "rejected_*"
    decision_reason: str
    extra_json: str | None = None

    async def execute(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            """INSERT INTO opportunities
               (account_name, source_strategy, mode, timestamp, condition_id, question,
                token_side, token_id, token_price, margin_net, suggested_bet, decision, decision_reason, extra_json)
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
        values = [self.status]
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
        query = f"UPDATE trades SET {', '.join(updates)} WHERE order_id = ?"
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
                trades_count, wins, losses, pending, open_positions, opportunities_detected, opportunities_placed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self.account_name, self.source_strategy, self.mode, self.timestamp,
             self.balance, self.daily_pnl, self.total_pnl, self.trades_count,
             self.wins, self.losses, self.pending, self.open_positions,
             self.opportunities_detected, self.opportunities_placed),
        )


class PersistenceLayer:
    """SQLite persistence with async writer-task for non-blocking writes."""

    def __init__(self, db_path: str = "data/panel.db"):
        self._db_path = db_path
        self._write_queue: asyncio.Queue[PersistOp | None] = asyncio.Queue()
        self._writer_task: asyncio.Task[None] | None = None
        self._shutdown = False

    async def start(self) -> None:
        """Initialize DB and start writer task."""
        # Crear schema si no existe
        await self._init_schema()
        # Arrancar writer-task
        self._writer_task = asyncio.create_task(self._writer_loop())
        logger.info("persistence_started", db_path=self._db_path)

    async def stop(self) -> None:
        """Graceful shutdown: drain queue, stop writer task."""
        self._shutdown = True
        if self._writer_task:
            await self._write_queue.put(None)  # Señal de terminación
            try:
                await asyncio.wait_for(self._writer_task, timeout=10)
            except asyncio.TimeoutError:
                logger.warning("persistence_shutdown_timeout")
                self._writer_task.cancel()
        logger.info("persistence_stopped")

    async def _init_schema(self) -> None:
        """Crear tablas si no existen."""
        async with aiosqlite.connect(self._db_path) as db:
            # Aplicar PRAGMAs
            for key, value in PRAGMAS.items():
                await db.execute(f"PRAGMA {key}={value}")
            # Crear tablas
            await db.executescript(self._get_schema_sql())
            await db.commit()

    @staticmethod
    def _get_schema_sql() -> str:
        """SQL para crear las tablas nuevas."""
        return """
            -- Tabla de oportunidades detectadas
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

            -- Tabla de trades ejecutados
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

            -- Snapshots de stats agregadas
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
                # Aplicar PRAGMAs
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
                        logger.error("persistence_write_error", error=str(e), op_type=type(op).__name__)
                        await db.rollback()
        except Exception as e:
            logger.error("persistence_writer_loop_error", error=str(e))

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
        decision_reason: str,
        extra_json: dict[str, Any] | None = None,
    ) -> None:
        """Queue a new opportunity record."""
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
        """Queue a new trade record."""
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
        """Queue a trade status update."""
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
        """Queue a stats snapshot."""
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

    async def query_open_trades(
        self, account_name: str, mode: str | None = None, strategy: str | None = None
    ) -> list[dict[str, Any]]:
        """Query open trades (pending o confirmed, no redeemed/failed)."""
        query = "SELECT * FROM trades WHERE account_name = ? AND status IN ('pending', 'confirmed')"
        params = [account_name]
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
        """Query stats snapshots."""
        query = "SELECT * FROM stats_snapshots WHERE account_name = ? AND mode = ?"
        params = [account_name, mode]
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

    async def cleanup_old_data(self) -> None:
        """Delete data older than retention policy (run monthly)."""
        now = time.time()
        one_year = 365 * 86400
        ninety_days = 90 * 86400
        thirty_days = 30 * 86400

        async with aiosqlite.connect(self._db_path) as db:
            # Aplicar PRAGMAs
            for key, value in PRAGMAS.items():
                await db.execute(f"PRAGMA {key}={value}")

            # Delete live data > 1 year
            await db.execute(
                "DELETE FROM trades WHERE mode='live' AND created_at < ?",
                (now - one_year,)
            )
            await db.execute(
                "DELETE FROM opportunities WHERE mode='live' AND timestamp < ?",
                (now - one_year,)
            )

            # Delete paper data > 90 days
            await db.execute(
                "DELETE FROM trades WHERE mode='paper' AND created_at < ?",
                (now - ninety_days,)
            )
            await db.execute(
                "DELETE FROM opportunities WHERE mode='paper' AND timestamp < ?",
                (now - thirty_days,)
            )

            # Compact snapshots: keep all <90d, 1 per hour 90-365d, 1 per day >365d
            # (Implementation simplified for now, can be refined)

            await db.commit()
            logger.info("persistence_cleanup_completed")

    async def vacuum(self) -> None:
        """Compact database (run monthly after cleanup)."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("VACUUM")
        logger.info("persistence_vacuum_completed")

# Global singleton instance
_persistence: PersistenceLayer | None = None

async def init_persistence(db_path: str = "data/panel.db") -> PersistenceLayer:
    """Initialize the global persistence layer."""
    global _persistence
    _persistence = PersistenceLayer(db_path)
    await _persistence.start()
    return _persistence

def get_persistence() -> PersistenceLayer:
    """Get the global persistence instance."""
    if _persistence is None:
        raise RuntimeError("Persistence layer not initialized")
    return _persistence

async def close_persistence() -> None:
    """Graceful shutdown of persistence layer."""
    if _persistence:
        await _persistence.stop()
```

**Archivos a modificar en `src/db.py`**:

Actualizar `init_db()` para llamar a la init del schema de persistence (o integrar directly):

```python
async def init_db():
    """Ensure DB schema is up to date."""
    from src.persistence import init_persistence
    await init_persistence()
    # ... resto de existentes (users, audit_log, wallet_overrides)
```

**Tests (crear `tests/test_persistence.py`)**:

```python
import asyncio
import pytest
from src.persistence import PersistenceLayer, init_persistence

@pytest.mark.asyncio
async def test_persistence_lifecycle():
    """Test basic persistence operations."""
    pl = PersistenceLayer(":memory:")  # In-memory para tests
    await pl.start()

    # Record an opportunity
    await pl.record_opportunity(
        account_name="test_account",
        source_strategy="directional",
        mode="paper",
        condition_id="cond123",
        question="Bitcoin up?",
        token_side="YES",
        token_id="tok456",
        token_price=0.65,
        margin_net=0.32,
        suggested_bet=10.0,
        decision="placed",
        decision_reason="",
    )

    # Record a trade
    await pl.record_trade(
        account_name="test_account",
        source_strategy="directional",
        mode="paper",
        order_id="order789",
        condition_id="cond123",
        question="Bitcoin up?",
        token_side="YES",
        token_id="tok456",
        price=0.65,
        size=15.4,
        cost_usd=10.0,
        status="confirmed",
    )

    # Query open trades
    trades = await pl.query_open_trades("test_account", mode="paper")
    assert len(trades) == 1
    assert trades[0]["source_strategy"] == "directional"

    await pl.stop()

if __name__ == "__main__":
    asyncio.run(test_persistence_lifecycle())
```

**Checklist para Paso 1**:
- [ ] `src/persistence.py` creado con `PersistenceLayer` + operaciones
- [ ] Schema SQL en `_get_schema_sql()`
- [ ] `src/db.py` actualizado para llamar a `init_persistence()`
- [ ] Tablas visibles en SQLite: `sqlite3 data/panel.db ".tables"`
- [ ] Test manual ejecutado sin errores
- [ ] No hay cambios en el comportamiento del bot (solo adición)

---

### Paso 2: `Strategy` ABC + `StrategyConfig` base + registry

**Objetivo**: Definir la interfaz abstracta que todas las estrategias implementarán.

**Archivos a crear**:
- `src/strategies/__init__.py`
- `src/strategies/base.py` — `Strategy` ABC + `StrategyConfig` base
- `src/strategies/registry.py` — mapping nombre → clase

**Archivos a modificar**:
- (ninguno aún)

**Código de `src/strategies/base.py`**:

```python
"""Base Strategy abstraction and configuration."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

import structlog

logger = structlog.get_logger("polymarket.strategies")

# Hard limit
PAPER_DAILY_TRADE_CAP = 500

@dataclass
class StrategyConfig(ABC):
    """Base configuration for all strategies."""
    mode: Literal["disabled", "paper", "live"] = "disabled"
    priority: int = 1  # Higher = more priority in conflicts
    max_concurrent_bets: int = 3  # Soft limit per strategy
    max_bet_per_trade: float = 50.0  # Soft limit per strategy
    paper_daily_trade_cap: int = PAPER_DAILY_TRADE_CAP  # Hard system limit (read-only)

@dataclass
class DirectionalConfig(StrategyConfig):
    """Directional (closing arb + up/down) strategy config."""
    max_price: float = 0.70
    min_buffer_pct: float = 0.0001
    # ... resto de params

@dataclass
class CopyTradeConfig(StrategyConfig):
    """Copy-trading strategy config."""
    fixed_bet_size: float = 5.0
    target_wallets: list[str] = field(default_factory=list)
    poll_interval_ms: int = 500
    max_latency_ms: int = 120000
    min_price: float = 0.50
    # ... resto de params

class AccountContext:
    """Read-only view of account state for strategies to consult."""

    def __init__(self, account_name: str, executor):
        self.account_name = account_name
        self._executor = executor

    def get_balance(self, mode: str) -> float:
        """Get current balance (live or paper)."""
        if mode == "live":
            return self._executor._ledger_live.balance or 0.0
        else:
            return self._executor._ledger_paper.balance or 0.0

    def get_open_positions(
        self, mode: str, strategy: str | None = None
    ) -> list:
        """Get open positions from executor."""
        ledger = self._executor._ledger_live if mode == "live" else self._executor._ledger_paper
        trades = ledger.trades
        if strategy:
            trades = [t for t in trades if getattr(t, "source_strategy", None) == strategy]
        return [t for t in trades if getattr(t, "status", None) in ("pending", "confirmed")]

    def has_position(self, condition_id: str, side: str, mode: str) -> bool:
        """Check if there's an open position for this market+side."""
        positions = self.get_open_positions(mode)
        return any(
            p.condition_id == condition_id and p.token_side == side
            for p in positions
        )

    def has_opposite_position(self, condition_id: str, side: str, mode: str) -> tuple[bool, str | None]:
        """Check if there's an open position on the opposite side."""
        opposite_side = "NO" if side == "YES" else "YES"
        positions = self.get_open_positions(mode)
        for p in positions:
            if p.condition_id == condition_id and p.token_side == opposite_side:
                return True, getattr(p, "source_strategy", None)
        return False, None

    def count_active_positions(self, mode: str, strategy: str | None = None) -> int:
        """Count open positions."""
        return len(self.get_open_positions(mode, strategy))

    def count_paper_trades_today(self, strategy: str) -> int:
        """Count paper trades started today (UTC)."""
        import time
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        start_of_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        start_of_day_ts = start_of_day.timestamp()

        positions = self.get_open_positions("paper", strategy)
        return sum(1 for p in positions if getattr(p, "created_at", 0) >= start_of_day_ts)

class Strategy(ABC):
    """Base class for all trading strategies."""

    def __init__(self, config: StrategyConfig, context: AccountContext):
        self.config = config
        self.context = context
        self.name = self.__class__.__name__
        self._on_opportunity_cb: Callable | None = None
        self._on_redeem_cb: Callable | None = None

    @property
    def is_active(self) -> bool:
        """Strategy is active if mode != disabled."""
        return self.config.mode != "disabled"

    @abstractmethod
    async def start(self) -> None:
        """Initialize and start the strategy."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Cleanup and stop the strategy."""
        pass

    async def set_mode(self, new_mode: str) -> None:
        """Change execution mode (paper/live/disabled) at runtime."""
        old_mode = self.config.mode
        self.config.mode = new_mode

        if old_mode == "disabled" and new_mode != "disabled":
            # Transitioning from disabled → active
            await self.start()
        elif old_mode != "disabled" and new_mode == "disabled":
            # Transitioning from active → disabled
            await self.stop()
        elif old_mode != new_mode and new_mode != "disabled":
            # Changing between paper/live
            # Don't stop/start, just let ongoing positions settle and new ones switch mode
            pass

        logger.info(
            "strategy_mode_changed",
            strategy=self.name,
            old=old_mode,
            new=new_mode,
        )

    def update_config(self, new_cfg: dict[str, Any]) -> None:
        """Update configuration fields (hot-reload)."""
        for key, value in new_cfg.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
        logger.debug("strategy_config_updated", strategy=self.name, updates=new_cfg)

    def on_opportunity(self, callback: Callable) -> None:
        """Register callback when opportunity is detected."""
        self._on_opportunity_cb = callback

    def on_redeem(self, callback: Callable) -> None:
        """Register callback for position redemption."""
        self._on_redeem_cb = callback

    @abstractmethod
    def get_stats(self) -> dict[str, Any]:
        """Return strategy-specific stats (no balance/P&L, only detections)."""
        pass

    @abstractmethod
    def get_config(self) -> dict[str, Any]:
        """Return current config as dict."""
        pass

    async def restore_open_positions(self, positions: list) -> None:
        """Restore positions from DB after redeploy."""
        # Default no-op, subclasses override if needed
        pass

    def cleanup_market(self, condition_id: str) -> bool:
        """Cleanup market data. Return False if strategy still has open positions in it."""
        return True  # Default: OK to cleanup
```

**Código de `src/strategies/registry.py`**:

```python
"""Strategy registry mapping names to classes."""

from typing import Type, Tuple

from src.strategies.base import Strategy, StrategyConfig

# Will be populated after DirectionalStrategy and CopyTradeStrategy are defined
STRATEGIES: dict[str, Tuple[Type[Strategy], Type[StrategyConfig]]] = {}

def register_strategy(name: str, strategy_cls: Type[Strategy], config_cls: Type[StrategyConfig]) -> None:
    """Register a strategy and its config class."""
    STRATEGIES[name] = (strategy_cls, config_cls)

def get_strategy_class(name: str) -> Type[Strategy] | None:
    """Get strategy class by name."""
    return STRATEGIES.get(name, (None, None))[0]

def get_config_class(name: str) -> Type[StrategyConfig] | None:
    """Get config class by name."""
    return STRATEGIES.get(name, (None, None))[1]
```

**Checklist para Paso 2**:
- [ ] `src/strategies/` directorio creado
- [ ] `src/strategies/base.py` con `Strategy` ABC, `StrategyConfig`, `AccountContext`
- [ ] `src/strategies/registry.py` con funciones de registro
- [ ] No hay cambios en el bot aún (solo nuevas clases)
- [ ] Imports validados

---

### Paso 3: Añadir `source_strategy` y `mode` a dataclasses

**Objetivo**: Marcar oportunidades y trades con su origen y modo.

**Archivos a modificar**:
- `src/types.py` (o dónde vivan `Opportunity` y `TradeRecord`)

**Cambios en `Opportunity`**:

```python
@dataclass
class Opportunity:
    # ... campos existentes ...
    source_strategy: str = ""  # "directional", "copy_trade", etc
    mode: str = "paper"        # "paper" o "live"
```

**Cambios en `TradeRecord`**:

```python
@dataclass
class TradeRecord:
    # ... campos existentes ...
    source_strategy: str = ""  # "directional", "copy_trade", etc
    mode: str = "paper"        # "paper" o "live"
```

**Checklist para Paso 3**:
- [ ] `Opportunity` tiene `source_strategy` y `mode` con defaults
- [ ] `TradeRecord` tiene `source_strategy` y `mode` con defaults
- [ ] El bot sigue funcionando con valores por defecto

---

### Paso 4: Refactor `Detector` → `DirectionalStrategy` y `CopyTrader` → `CopyTradeStrategy`

**Objetivo**: Renombrar y hacer heredar de `Strategy`.

**Archivos a crear**:
- `src/strategies/directional.py` (contenido de Detector renombrado)
- `src/strategies/copy_trade.py` (contenido de CopyTrader renombrado)

**Archivos a modificar**:
- `src/detector.py` → renombrar a `src/strategies/directional.py` (move)
- `src/copy_trader.py` → renombrar a `src/strategies/copy_trade.py` (move)
- `src/strategies/__init__.py` → importar y registrar

**Cambios en `src/strategies/directional.py`** (antes `detector.py`):

```python
from src.strategies.base import Strategy, DirectionalConfig, AccountContext, register_strategy
import structlog

class DirectionalStrategy(Strategy):
    """Closing arbitrage + up/down directional strategy."""

    def __init__(self, config: DirectionalConfig, context: AccountContext, ...):
        super().__init__(config, context)
        self.config: DirectionalConfig = config
        # ... resto de __init__ original

    async def start(self) -> None:
        # ... original de detector._start o similar
        pass

    async def stop(self) -> None:
        # ... original cleanup
        pass

    def get_stats(self) -> dict:
        # ... stats pero SIN balance (eso vuelve del executor)
        return {
            "total_scans": self._stats.get("total_scans", 0),
            "opportunities_found": self._stats.get("opportunities_found", 0),
            # ... etc
        }

    def get_config(self) -> dict:
        # Exportar config como dict para el panel
        return {
            "mode": self.config.mode,
            "priority": self.config.priority,
            "max_concurrent_bets": self.config.max_concurrent_bets,
            "max_bet_per_trade": self.config.max_bet_per_trade,
            "max_price": self.config.max_price,
            "min_buffer_pct": self.config.min_buffer_pct,
            # ... resto
        }

    async def restore_open_positions(self, positions: list) -> None:
        """Restore positions from DB."""
        # Tomar los trades persistidos y ponerlos en _bet_placed
        for trade in positions:
            if getattr(trade, "source_strategy", None) == "directional":
                key = f"{trade.condition_id}:{trade.token_side}"
                # Crear un Opportunity simulado desde el trade
                opp = Opportunity(...)
                self._bet_placed[key] = opp

# Registrar
register_strategy("directional", DirectionalStrategy, DirectionalConfig)
```

**Lo mismo para `src/strategies/copy_trade.py`** (antes `copy_trader.py`):

```python
class CopyTradeStrategy(Strategy):
    # ... análogo

async def restore_open_positions(self, positions: list) -> None:
    for trade in positions:
        if getattr(trade, "source_strategy", None) == "copy_trade":
            # Restaurar en _bets interno
            ...

register_strategy("copy_trade", CopyTradeStrategy, CopyTradeConfig)
```

**Actualizar imports en `src/strategies/__init__.py`**:

```python
from src.strategies.directional import DirectionalStrategy, DirectionalConfig
from src.strategies.copy_trade import CopyTradeStrategy, CopyTradeConfig
from src.strategies.base import Strategy, StrategyConfig, AccountContext

__all__ = [
    "Strategy",
    "StrategyConfig",
    "AccountContext",
    "DirectionalStrategy",
    "DirectionalConfig",
    "CopyTradeStrategy",
    "CopyTradeConfig",
]
```

**Checklist para Paso 4**:
- [ ] `src/strategies/directional.py` existe y contiene `DirectionalStrategy`
- [ ] `src/strategies/copy_trade.py` existe y contiene `CopyTradeStrategy`
- [ ] Ambas heredan de `Strategy`
- [ ] Registradas en registry
- [ ] Imports actualizados en archivos que usaban `Detector` y `CopyTrader`
- [ ] El bot sigue funcionando sin cambios en comportamiento

---

### Paso 5: Executor dual-ledger

**Objetivo**: Separar ledgers live y paper en el executor.

**Archivos a modificar**:
- `src/executor.py`

**Cambios**:

```python
@dataclass
class LedgerState:
    """State of trades for a single execution mode (paper or live)."""
    balance: float | None
    trades: list[TradeRecord] = field(default_factory=list)
    pending_orders: dict[str, TradeRecord] = field(default_factory=dict)
    daily_pnl: float = 0.0
    daily_loss_reset_at: float = 0.0

class Executor:
    def __init__(self, risk: RiskConfig, mode: ExecutionMode = ExecutionMode.PAPER):
        # Dual ledger
        self._ledger_live = LedgerState(balance=None)  # Real USDC, refresca de API
        self._ledger_paper = LedgerState(balance=risk.simulated_balance)  # Simulado

        # Legacy: mantener compatibilidad
        self.risk = risk
        self.mode = mode
        # ... resto

    async def execute(self, opp: Opportunity) -> TradeRecord | None:
        """Route to live or paper based on opp.mode."""
        ledger = self._ledger_live if opp.mode == "live" else self._ledger_paper

        if not self._initialized:
            logger.error("executor_not_initialized")
            return None

        # Risk checks SOLO en live
        if opp.mode == "live":
            if not self._check_risk(opp, ledger):
                return None

        if opp.mode == "live":
            return await self._live_trade(opp, ledger)
        else:
            return self._paper_trade(opp, ledger)

    def _check_risk(self, opp: Opportunity, ledger: LedgerState) -> bool:
        """Pre-trade risk checks (only for live)."""
        # ... (igual que ahora, usa ledger)

    def _paper_trade(self, opp: Opportunity, ledger: LedgerState) -> TradeRecord:
        """Record a paper trade."""
        # ... crea el TradeRecord y lo añade a ledger.trades

    async def _live_trade(self, opp: Opportunity, ledger: LedgerState) -> TradeRecord | None:
        """Place a real order."""
        # ... crea, coloca, retorna TradeRecord

    def get_stats(self) -> dict:
        """Stats of both ledgers."""
        return {
            "live": self._ledger_stats(self._ledger_live),
            "paper": self._ledger_stats(self._ledger_paper),
            "total": { /* agregado */ }
        }

    def _ledger_stats(self, ledger: LedgerState) -> dict:
        """Stats for a single ledger."""
        total_trades = len(ledger.trades)
        settled = [t for t in ledger.trades if t.status == "redeemed"]
        wins = sum(1 for t in settled if t.settled_pnl and t.settled_pnl > 0)
        losses = sum(1 for t in settled if t.settled_pnl and t.settled_pnl <= 0)
        return {
            "trades": total_trades,
            "confirmed": sum(1 for t in ledger.trades if t.status in ("confirmed", "live")),
            "wins": wins,
            "losses": losses,
            "pending": sum(1 for t in ledger.trades if t.status == "pending"),
            "balance": ledger.balance,
            "daily_pnl": ledger.daily_pnl,
        }
```

**Checklist para Paso 5**:
- [ ] `Executor` tiene `_ledger_live` y `_ledger_paper`
- [ ] `execute()` enruta según `opp.mode`
- [ ] Risk checks solo en live
- [ ] El bot sigue funcionando (ledgers are internal)

---

### Paso 6: Integración Executor ↔ Persistence

**Objetivo**: Cada trade se persiste, se recupera al arrancar.

**Cambios en `src/executor.py`**:

```python
from src.persistence import get_persistence

class Executor:
    async def _paper_trade(self, opp: Opportunity, ledger: LedgerState) -> TradeRecord:
        # ... crear trade ...
        trade = TradeRecord(...)
        ledger.trades.append(trade)

        # Persist
        await get_persistence().record_trade(
            account_name=opp.account_name,  # Asumir que Opportunity tiene esto
            source_strategy=opp.source_strategy,
            mode="paper",
            order_id=trade.order_id,
            condition_id=opp.condition_id,
            question=opp.question,
            token_side=opp.token_side,
            token_id=opp.token_id,
            price=opp.token_price,
            size=trade.size,
            cost_usd=trade.cost_usd,
            status="confirmed",
        )
        return trade

    async def _live_trade(self, opp: Opportunity, ledger: LedgerState) -> TradeRecord | None:
        # ... crear, firmar, colocar orden ...
        trade = TradeRecord(...)

        if response and response.get("orderID"):
            trade.order_id = response["orderID"]
            trade.status = OrderStatus.LIVE
            ledger.trades.append(trade)
            ledger.pending_orders[trade.order_id] = trade

            # Persist
            await get_persistence().record_trade(
                account_name=opp.account_name,
                source_strategy=opp.source_strategy,
                mode="live",
                order_id=trade.order_id,
                condition_id=opp.condition_id,
                question=opp.question,
                token_side=opp.token_side,
                token_id=opp.token_id,
                price=opp.token_price,
                size=trade.size,
                cost_usd=trade.cost_usd,
                status="pending",
            )

    async def _update_trade_status(self, trade_id: str, status: str, ...):
        """Update trade status both in memory and persistence."""
        trade = self._find_trade(trade_id)
        if trade:
            trade.status = status
            if settled_pnl is not None:
                trade.settled_pnl = settled_pnl

            # Persist update
            await get_persistence().update_trade_status(
                account_name=self._account_name,  # Requiere guardar esto en init
                order_id=trade_id,
                status=status,
                settled_pnl=settled_pnl,
                error=error,
            )

    async def initialize(self):
        """Load open positions from DB and initialize."""
        # Cargar positions open live de la DB
        persistence = get_persistence()
        open_trades = await persistence.query_open_trades(
            self._account_name, mode="live"
        )
        for trade_dict in open_trades:
            trade = TradeRecord(**trade_dict)
            self._ledger_live.trades.append(trade)
            if trade.status in ("pending", "live"):
                self._ledger_live.pending_orders[trade.order_id] = trade

        # Cargar positions open paper de la DB
        open_trades_paper = await persistence.query_open_trades(
            self._account_name, mode="paper"
        )
        for trade_dict in open_trades_paper:
            trade = TradeRecord(**trade_dict)
            self._ledger_paper.trades.append(trade)
```

**Checklist para Paso 6**:
- [ ] `Executor` llama a `persistence.record_trade()` para cada trade
- [ ] `Executor` llama a `persistence.update_trade_status()` para updates
- [ ] `initialize()` carga trades open de DB
- [ ] Los trades se persisten en SQLite
- [ ] Query en SQLite muestra los trades registrados

---

### Paso 7: `restore_open_positions` en cada strategy + bootstrap

**Objetivo**: Al arrancar, las estrategias retoman sus posiciones persistidas.

**Cambios en `src/account_runner.py`**:

```python
async def start(self):
    # ... init existing code ...

    # Bootstrap: load open positions from DB per strategy
    if self.strategy_type == "directional" and self.detector:
        open_trades = await persistence.query_open_trades(
            self.account.name, mode="live", strategy="directional"
        )
        open_trades.extend(await persistence.query_open_trades(
            self.account.name, mode="paper", strategy="directional"
        ))
        await self.detector.restore_open_positions(open_trades)

    elif self.strategy_type == "copy_trade" and self.copy_trader:
        open_trades = await persistence.query_open_trades(
            self.account.name, mode="live", strategy="copy_trade"
        )
        open_trades.extend(await persistence.query_open_trades(
            self.account.name, mode="paper", strategy="copy_trade"
        ))
        await self.copy_trader.restore_open_positions(open_trades)
```

**Implementación en `DirectionalStrategy`**:

```python
async def restore_open_positions(self, positions: list) -> None:
    """Restore positions from DB after redeploy."""
    for pos in positions:
        # pos es un TradeRecord loaded from DB
        key = f"{pos.condition_id}:{pos.token_side}"

        # Create an Opportunity-like object for internal tracking
        opp = Opportunity(
            timestamp=pos.created_at,
            condition_id=pos.condition_id,
            question=pos.question,
            token_side=pos.token_side,
            token_id=pos.token_id,
            token_price=pos.price,
            # ... resto de fields
        )
        self._bet_placed[key] = opp

        logger.info("position_restored", condition_id=pos.condition_id[:20], side=pos.token_side)
```

**Checklist para Paso 7**:
- [ ] `restore_open_positions()` implementado en ambas estrategias
- [ ] `AccountRunner` carga positions al bootstrap
- [ ] Tras redeploy, las posiciones se retoman automáticamente
- [ ] Settlement continúa normalmente

---

### Paso 8: Reescribir `AccountRunner` multi-strategy

**Objetivo**: De single-strategy a N estrategias con orquestación central.

**Cambios en `src/account_runner.py`**:

Este es un cambio importante. Resumen:

```python
from src.strategies import Strategy, get_strategy_class

class AccountRunner:
    def __init__(self, account: AccountConfig, ...):
        self.account = account
        self.executor = Executor(account.risk, mode=self.exec_mode)
        self.strategies: dict[str, Strategy] = {}
        self.context = AccountContext(account.name, self.executor)

    async def start(self):
        # Inicializar executor
        await self.executor.initialize()

        # Crear estrategias según config
        for strategy_name, strategy_cfg in self.account.strategies.items():
            StrategyClass = get_strategy_class(strategy_name)
            if not StrategyClass:
                logger.warning(f"Unknown strategy: {strategy_name}")
                continue

            strategy = StrategyClass(strategy_cfg, self.context)
            strategy.on_opportunity(lambda opp, sname=strategy_name: self._handle_opportunity(opp, sname))
            strategy.on_redeem(self.executor.redeem_position)

            self.strategies[strategy_name] = strategy

            # Restore positions
            open_trades = await persistence.query_open_trades(self.account.name, strategy=strategy_name)
            await strategy.restore_open_positions(open_trades)

            # Start if active
            if strategy.is_active:
                await strategy.start()

    async def _handle_opportunity(self, opp: Opportunity, source_strategy: str):
        """Central opportunity handler with conflict resolution."""
        opp.source_strategy = source_strategy
        opp.mode = self.strategies[source_strategy].config.mode

        # 1. Sanity
        if opp.suggested_bet <= 0:
            await persistence.record_opportunity(..., decision="rejected_zero_bet")
            return

        # 2. Risk de cuenta (solo live)
        if opp.mode == "live":
            if not self.executor._check_risk(opp, self.executor._ledger_live):
                await persistence.record_opportunity(..., decision="rejected_risk", ...)
                return

        # 3. Opposite side
        has_opposite, opposite_strategy = self.context.has_opposite_position(
            opp.condition_id, opp.token_side, opp.mode
        )
        if has_opposite:
            await persistence.record_opportunity(..., decision="rejected_opposite_side_exists", ...)
            return

        # 4. Dedupe same strategy
        key = f"{opp.condition_id}:{opp.token_side}"
        executor_ledger = self.executor._ledger_live if opp.mode == "live" else self.executor._ledger_paper
        if any(t.condition_id == opp.condition_id and t.token_side == opp.token_side
               and getattr(t, "source_strategy", None) == source_strategy
               for t in executor_ledger.trades):
            await persistence.record_opportunity(..., decision="rejected_dedupe_same_strategy", ...)
            return

        # All passed → execute
        trade = await self.executor.execute(opp)
        if trade:
            decision = "placed"
        else:
            decision = "rejected_execution_failed"

        await persistence.record_opportunity(..., decision=decision, ...)

    async def set_strategy_mode(self, strategy_name: str, new_mode: str):
        """Enable/disable/change mode of a strategy at runtime."""
        if strategy_name not in self.strategies:
            logger.warning(f"Unknown strategy: {strategy_name}")
            return

        strategy = self.strategies[strategy_name]
        await strategy.set_mode(new_mode)

        # Update config TOML
        self.account.strategies[strategy_name].mode = new_mode
        await self._save_config()

        # Audit log
        # ... record mode change in audit_log

    def get_stats(self) -> dict:
        """Aggregate stats from all strategies + executor."""
        stats = {
            "account": self.account.name,
            "executor": self.executor.get_stats(),
            "strategies": {},
        }
        for name, strategy in self.strategies.items():
            stats["strategies"][name] = strategy.get_stats()
        return stats
```

**Checklist para Paso 8**:
- [ ] `AccountRunner` initializa N estrategias desde config
- [ ] `_handle_opportunity` aplica lógica central de conflictos
- [ ] Ambas estrategias pueden generar opportunities en paralelo
- [ ] Stats agregadas correctamente
- [ ] Panel necesita update, pero backend funciona

---

### Paso 9: Cambio de schema TOML + script migración

**Objetivo**: Hard-cut del formato viejo al nuevo.

**Script `scripts/migrate_config.py`**:

```python
#!/usr/bin/env python3
"""Migrate config from old single-strategy format to new multi-strategy format."""

import sys
import tomli
import tomli_w
from pathlib import Path
from datetime import datetime

def migrate():
    config_path = Path("config/config.toml")
    if not config_path.exists():
        print(f"Error: {config_path} not found")
        sys.exit(1)

    # Backup
    backup_path = config_path.with_suffix(f".toml.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    config_path.rename(backup_path)
    print(f"Backup created: {backup_path}")

    # Read old format
    with open(backup_path, "rb") as f:
        old = tomli.load(f)

    # Migrate
    new = {
        "strategy": old.get("strategy", {}),  # Global strategy config
        "risk": {},
        "data": old.get("data", {}),
        "websocket": old.get("websocket", {}),
        "logging": old.get("logging", {}),
        "accounts": [],
    }

    # Merge accounts
    accounts_to_merge = []
    for acc in old.get("accounts", []):
        if not acc.get("enabled", True):
            continue
        accounts_to_merge.append(acc)

    if len(accounts_to_merge) > 1:
        print(f"\nFound {len(accounts_to_merge)} accounts:")
        for i, acc in enumerate(accounts_to_merge):
            print(f"  {i}: {acc.get('name')} ({acc.get('strategy_type')})")

        response = input("\nMerge all into one account? [Y/n]: ").strip().lower()
        do_merge = response != "n"
    else:
        do_merge = False

    if do_merge:
        # Merge all strategies into one account
        merged_account = {
            "name": "main",
            "enabled": True,
            "priority": 1,
            "credentials": {},
            "risk": {},
            "strategies": {},
        }

        # Use first account's credentials
        merged_account["credentials"] = accounts_to_merge[0].get("credentials", {})
        merged_account["risk"] = accounts_to_merge[0].get("risk", {})

        # Collect all strategies
        for acc in accounts_to_merge:
            strategy_type = acc.get("strategy_type", "directional")
            strategy_cfg = {}

            if strategy_type == "directional":
                # Copy from [strategy] + account-level overrides
                strategy_cfg = old.get("strategy", {}).copy()
            elif strategy_type == "copy_trade":
                strategy_cfg = acc.get("copy_trade", {}).copy()

            strategy_cfg["enabled"] = acc.get("enabled", True)
            strategy_cfg["priority"] = 10 if strategy_type == "directional" else 5

            merged_account["strategies"][strategy_type] = strategy_cfg

        new["accounts"] = [merged_account]
    else:
        # Keep separate
        for acc in accounts_to_merge:
            new_acc = {
                "name": acc.get("name"),
                "enabled": acc.get("enabled", True),
                "credentials": acc.get("credentials", {}),
                "risk": acc.get("risk", {}),
                "strategies": {},
            }

            strategy_type = acc.get("strategy_type", "directional")
            if strategy_type == "directional":
                strategy_cfg = old.get("strategy", {}).copy()
                strategy_cfg["enabled"] = True
                strategy_cfg["priority"] = 10
                new_acc["strategies"]["directional"] = strategy_cfg
            elif strategy_type == "copy_trade":
                strategy_cfg = acc.get("copy_trade", {}).copy()
                strategy_cfg["enabled"] = True
                strategy_cfg["priority"] = 5
                new_acc["strategies"]["copy_trade"] = strategy_cfg

            new["accounts"].append(new_acc)

    # Write new format
    with open(config_path, "wb") as f:
        tomli_w.dump(new, f)

    print(f"\nMigration complete: {config_path}")
    print("\nNext steps:")
    print("  1. Review the new config file")
    print("  2. docker compose down && docker compose up -d")

if __name__ == "__main__":
    migrate()
```

**Checklist para Paso 9**:
- [ ] Script `migrate_config.py` creado y funcional
- [ ] Config viejo respaldado
- [ ] Config nuevo generado correctamente
- [ ] Bot arranca con nuevo formato

---

### Paso 10: Snapshots periódicos + cleanup

**Objetivo**: Grabar snapshots cada 5 min y limpiar datos old.

**Cambios en `src/main.py`**:

```python
class Bot:
    async def start(self):
        # ... existing code ...

        tasks = [
            self._run_stats_reporter(),
            self._run_snapshot_loop(),
            self._run_cleanup_loop(),
            # ... rest
        ]
        await asyncio.gather(*tasks)

    async def _run_snapshot_loop(self):
        """Record stats snapshots every 5 minutes."""
        while self._running:
            await asyncio.sleep(300)  # 5 min
            try:
                for acc in self.accounts:
                    stats = acc.get_stats()
                    persistence = get_persistence()

                    # Snapshot per strategy
                    for strat_name, strat_stats in stats.get("strategies", {}).items():
                        mode = self.accounts[0].strategies[strat_name].config.mode
                        if mode != "disabled":
                            await persistence.snapshot_stats(
                                account_name=acc.name,
                                source_strategy=strat_name,
                                mode=mode,
                                balance=stats["executor"]["live"]["balance"] if mode == "live" else stats["executor"]["paper"]["balance"],
                                daily_pnl=0.0,  # TODO
                                total_pnl=0.0,  # TODO
                                trades_count=len(strat_stats.get("trades", [])),
                                # ... rest
                            )

                    # Snapshot aggregate
                    await persistence.snapshot_stats(
                        account_name=acc.name,
                        source_strategy=None,
                        mode="live",
                        # ... aggregated
                    )
            except Exception as e:
                logger.error("snapshot_error", error=str(e))

    async def _run_cleanup_loop(self):
        """Cleanup old data monthly + VACUUM."""
        await asyncio.sleep(3600)  # Wait 1h before first run
        while self._running:
            try:
                # First Tuesday of the month at 1 AM
                # (Simplified: just run every 30 days)
                await asyncio.sleep(30 * 86400)
                persistence = get_persistence()
                await persistence.cleanup_old_data()
                await persistence.vacuum()
                logger.info("maintenance_completed")
            except Exception as e:
                logger.error("maintenance_error", error=str(e))
```

**Checklist para Paso 10**:
- [ ] `_run_snapshot_loop` grabando snapshots cada 5 min
- [ ] `_run_cleanup_loop` corriendo (test manual)
- [ ] Datos viejos siendo borrados según retención
- [ ] VACUUM compactando DB

---

### Paso 11: Panel rediseñado

**Objetivo**: Mostrar N estrategias con tabs live/paper, dropdown de modo.

**Cambios**:
- Subtabs por estrategia
- Dropdown modo (disabled/paper/live)
- Tabs Live | Paper | Total en los stats
- Tabla de posiciones con columna "Strategy | Mode"
- Vista histórico paginada desde SQLite

(Omitido por brevedad — es cambio significativo pero straightforward de UX)

**Checklist para Paso 11**:
- [ ] Panel muestra todas las estrategias
- [ ] Dropdown de modo funciona
- [ ] Tabs live/paper visible
- [ ] Tabla histórico paginada

---

### Paso 12: `users.account_name` preparación multi-tenant

```sql
ALTER TABLE users ADD COLUMN account_name TEXT DEFAULT NULL;
```

**Checklist para Paso 12**:
- [ ] Campo añadido a tabla
- [ ] No hay lógica de filtrado activada todavía

---

### Paso 13: Backup automático + VACUUM mensual

**Cambios en `src/main.py`**:

```python
async def _run_backup_loop(self):
    """Backup DB every 6 hours, retain 14 days."""
    import shutil
    from datetime import datetime, timedelta

    backup_dir = Path("data/backups")
    backup_dir.mkdir(exist_ok=True)

    while self._running:
        try:
            await asyncio.sleep(6 * 3600)  # 6 hours

            now = datetime.now()
            backup_path = backup_dir / f"panel_{now.strftime('%Y%m%d_%H%M%S')}.db"

            # Safe copy using sqlite3.backup()
            import sqlite3
            src = sqlite3.connect("data/panel.db")
            dst = sqlite3.connect(str(backup_path))
            with dst:
                src.backup(dst)
            src.close()
            dst.close()

            logger.info("backup_completed", path=str(backup_path))

            # Cleanup old backups (>14 days)
            cutoff = now - timedelta(days=14)
            for old_backup in backup_dir.glob("panel_*.db"):
                if datetime.fromtimestamp(old_backup.stat().st_mtime) < cutoff:
                    old_backup.unlink()
                    logger.info("backup_deleted", path=str(old_backup))

        except Exception as e:
            logger.error("backup_error", error=str(e))
```

**Checklist para Paso 13**:
- [ ] Backups se crean cada 6h
- [ ] Archivos antiguos se borran
- [ ] `data/backups/` tiene archivos recientes

---

### Paso 14: Validación end-to-end

**Objetivo**: Verificar que todo funciona junto sin romper el deployment actual.

**Pasos manuales**:

1. Backup del DB actual y config
2. Ejecutar script de migración
3. Redeploy del bot
4. Verificar que ambas estrategias corren
5. Generar algunas trades paper/live
6. Redeploy nuevamente
7. Verificar que trades se recuperan de DB
8. Revisar stats agregadas
9. Test cambio de modo en caliente
10. Validar panel muestra todo correctamente

**Checklist para Paso 14**:
- [ ] Migración exitosa
- [ ] Ambas estrategias activas y generando oportunidades
- [ ] Trades se persisten
- [ ] Redeploy recupera estado
- [ ] Cambio de modo funciona
- [ ] Stats correctas
- [ ] No hay errores en logs

---

## Checklist de Completitud

- [ ] **Paso 1**: Persistence layer aislada (SQLite WAL + writer-task)
- [ ] **Paso 2**: Strategy ABC + registry
- [ ] **Paso 3**: `source_strategy` y `mode` en dataclasses
- [ ] **Paso 4**: Refactor a `DirectionalStrategy` y `CopyTradeStrategy`
- [ ] **Paso 5**: Executor dual-ledger
- [ ] **Paso 6**: Integración persistence ↔ executor
- [ ] **Paso 7**: `restore_open_positions` en estrategias
- [ ] **Paso 8**: `AccountRunner` multi-strategy
- [ ] **Paso 9**: Migración TOML hard-cut
- [ ] **Paso 10**: Snapshots + cleanup
- [ ] **Paso 11**: Panel rediseñado
- [ ] **Paso 12**: `users.account_name`
- [ ] **Paso 13**: Backup automático
- [ ] **Paso 14**: Validación end-to-end

---

## Rollback y Planes de Contingencia

### Si falla algún paso:

1. **Paso 1-7**: No afectan el bot en vivo. Rollback: `git revert`, no hay estado persistido aún.
2. **Paso 8** (AccountRunner multi): PUNTO DE NO RETORNO. Si falla aquí, revert es difícil. Mitigar: test exhaustivo en paper antes.
3. **Paso 9** (TOML migration): Mantener `config.toml.backup_*`. Script reversible.
4. **Paso 10+**: Snapshots/cleanup/panel son aditivos. Safe to rollback.

### Rollback rápido:

```bash
git revert <commit_id>
docker compose down && docker compose up -d
# Acceder al container
docker exec polymarket-bot python scripts/migrate_config.py --reverse
```

---

**Documento completado. Listo para comenzar paso 1.**
