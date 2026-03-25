"""SQLite database for panel state: users, wallet overrides, audit log."""
from __future__ import annotations

import os
import time

import aiosqlite
import bcrypt

DB_PATH = os.environ.get("PANEL_DB_PATH", "data/panel.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT DEFAULT 'admin',
    created_at REAL DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS wallet_overrides (
    address TEXT PRIMARY KEY,
    alias TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    role TEXT DEFAULT 'primary',
    confirms_wallet TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT,
    timestamp REAL DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS wallet_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    coin TEXT,
    side TEXT,
    price REAL,
    size REAL,
    timestamp REAL,
    market_resolved INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS wallet_resolution (
    condition_id TEXT PRIMARY KEY,
    winning_side TEXT,
    resolved_at REAL
);

CREATE TABLE IF NOT EXISTS wallet_stats (
    wallet TEXT PRIMARY KEY,
    total_trades INTEGER DEFAULT 0,
    total_wins INTEGER DEFAULT 0,
    total_losses INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0,
    avg_price REAL DEFAULT 0,
    total_volume REAL DEFAULT 0,
    coins_traded TEXT,
    last_updated REAL
);

CREATE INDEX IF NOT EXISTS idx_wallet_activity_wallet ON wallet_activity(wallet);
CREATE INDEX IF NOT EXISTS idx_wallet_activity_cid ON wallet_activity(condition_id);
CREATE INDEX IF NOT EXISTS idx_wallet_stats_wr ON wallet_stats(win_rate DESC);
"""


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    return db


async def init_db():
    """Create tables and default admin user if needed."""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = await get_db()
    try:
        await db.executescript(SCHEMA)

        # Migrate: add role + confirms_wallet columns if missing
        cursor = await db.execute("PRAGMA table_info(wallet_overrides)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "role" not in cols:
            await db.execute("ALTER TABLE wallet_overrides ADD COLUMN role TEXT DEFAULT 'primary'")
        if "confirms_wallet" not in cols:
            await db.execute("ALTER TABLE wallet_overrides ADD COLUMN confirms_wallet TEXT DEFAULT ''")
        if "role" not in cols or "confirms_wallet" not in cols:
            await db.commit()

        # Create default admin if no users exist
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        row = await cursor.fetchone()
        if row[0] == 0:
            password = os.environ.get("PANEL_PASSWORD", "admin")
            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            await db.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                ("admin", hashed, "admin"),
            )
            await db.commit()
    finally:
        await db.close()


async def verify_password(username: str, password: str) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT password_hash FROM users WHERE username = ?", (username,)
        )
        row = await cursor.fetchone()
        if not row:
            return False
        return bcrypt.checkpw(password.encode(), row["password_hash"].encode())
    finally:
        await db.close()


async def change_password(username: str, new_password: str):
    db = await get_db()
    try:
        hashed = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
        await db.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (hashed, username),
        )
        await db.commit()
    finally:
        await db.close()


async def add_audit(username: str, action: str, details: str = ""):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO audit_log (username, action, details) VALUES (?, ?, ?)",
            (username, action, details),
        )
        await db.commit()
    finally:
        await db.close()


async def get_audit_log(limit: int = 50) -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT username, action, details, timestamp FROM audit_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_wallet_overrides() -> dict[str, dict]:
    """Return {address: {alias, enabled, role, confirms_wallet}} for all overrides."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT address, alias, enabled, role, confirms_wallet FROM wallet_overrides"
        )
        rows = await cursor.fetchall()
        return {
            r["address"]: {
                "alias": r["alias"],
                "enabled": bool(r["enabled"]),
                "role": r["role"] or "primary",
                "confirms_wallet": r["confirms_wallet"] or "",
            }
            for r in rows
        }
    finally:
        await db.close()


async def set_wallet_override(
    address: str, alias: str = "", enabled: bool = True,
    role: str = "primary", confirms_wallet: str = "",
):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO wallet_overrides (address, alias, enabled, role, confirms_wallet) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(address) DO UPDATE SET "
            "alias=excluded.alias, enabled=excluded.enabled, "
            "role=excluded.role, confirms_wallet=excluded.confirms_wallet",
            (address.lower(), alias, int(enabled), role, confirms_wallet.lower()),
        )
        await db.commit()
    finally:
        await db.close()


async def remove_wallet_override(address: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM wallet_overrides WHERE address = ?", (address.lower(),))
        await db.commit()
    finally:
        await db.close()


# Wallet scanner methods
async def add_wallet_trade(
    wallet: str, condition_id: str, coin: str, side: str, price: float, size: float
):
    """Register a trade we observed."""
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO wallet_activity
               (wallet, condition_id, coin, side, price, size, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (wallet[:10], condition_id, coin, side, price, size, time.time()),
        )
        await db.commit()
    finally:
        await db.close()


async def resolve_market(condition_id: str, winning_side: str):
    """Register market resolution and update wallet stats."""
    db = await get_db()
    try:
        # Insert resolution
        await db.execute(
            "INSERT OR REPLACE INTO wallet_resolution (condition_id, winning_side, resolved_at) VALUES (?, ?, ?)",
            (condition_id, winning_side, time.time()),
        )

        # Mark all trades in this market as resolved
        await db.execute(
            "UPDATE wallet_activity SET market_resolved = 1 WHERE condition_id = ?",
            (condition_id,),
        )

        # Update stats for all wallets that traded in this market
        cursor = await db.execute(
            """SELECT DISTINCT wallet FROM wallet_activity WHERE condition_id = ?""",
            (condition_id,),
        )
        wallets = await cursor.fetchall()

        for (wallet,) in wallets:
            # Count wins and losses
            win_cursor = await db.execute(
                """SELECT COUNT(*) FROM wallet_activity
                   WHERE wallet = ? AND condition_id = ? AND side = ?""",
                (wallet, condition_id, winning_side),
            )
            win_row = await win_cursor.fetchone()
            wins = win_row[0] if win_row else 0

            loss_cursor = await db.execute(
                """SELECT COUNT(*) FROM wallet_activity
                   WHERE wallet = ? AND condition_id = ? AND side != ?""",
                (wallet, condition_id, winning_side),
            )
            loss_row = await loss_cursor.fetchone()
            losses = loss_row[0] if loss_row else 0

            # Aggregate stats
            stats_cursor = await db.execute(
                """SELECT total_trades, total_wins, total_losses, total_volume
                   FROM wallet_stats WHERE wallet = ?""",
                (wallet,),
            )
            stats_row = await stats_cursor.fetchone()

            if stats_row:
                old_trades, old_wins, old_losses, old_vol = stats_row
            else:
                old_trades, old_wins, old_losses, old_vol = 0, 0, 0, 0.0

            new_trades = old_trades + wins + losses
            new_wins = old_wins + wins
            new_losses = old_losses + losses
            wr = new_wins / new_trades if new_trades > 0 else 0

            # Get avg price and total volume for this wallet
            vol_cursor = await db.execute(
                """SELECT AVG(price), SUM(size) FROM wallet_activity WHERE wallet = ?""",
                (wallet,),
            )
            vol_row = await vol_cursor.fetchone()
            avg_price = vol_row[0] or 0
            total_vol = vol_row[1] or 0

            # Get coins traded
            coins_cursor = await db.execute(
                """SELECT DISTINCT coin FROM wallet_activity WHERE wallet = ? AND coin IS NOT NULL""",
                (wallet,),
            )
            coins = [c[0] for c in await coins_cursor.fetchall()]

            # Upsert stats
            await db.execute(
                """INSERT INTO wallet_stats
                   (wallet, total_trades, total_wins, total_losses, win_rate, avg_price, total_volume, coins_traded, last_updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(wallet) DO UPDATE SET
                   total_trades=excluded.total_trades,
                   total_wins=excluded.total_wins,
                   total_losses=excluded.total_losses,
                   win_rate=excluded.win_rate,
                   avg_price=excluded.avg_price,
                   total_volume=excluded.total_volume,
                   coins_traded=excluded.coins_traded,
                   last_updated=excluded.last_updated""",
                (wallet, new_trades, new_wins, new_losses, wr, avg_price, total_vol, ",".join(coins), time.time()),
            )

        await db.commit()
    finally:
        await db.close()


async def get_top_traders(min_trades: int = 20, min_wr: float = 0.55) -> list[dict]:
    """Get top traders by win rate."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT wallet, total_trades, total_wins, total_losses, win_rate, avg_price, total_volume, coins_traded
               FROM wallet_stats
               WHERE total_trades >= ? AND win_rate >= ?
               ORDER BY win_rate DESC, total_trades DESC
               LIMIT 10""",
            (min_trades, min_wr),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_wallet_stats(wallet: str) -> dict | None:
    """Get stats for a specific wallet."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT wallet, total_trades, total_wins, total_losses, win_rate, avg_price, total_volume, coins_traded
               FROM wallet_stats WHERE wallet = ?""",
            (wallet[:10],),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()
