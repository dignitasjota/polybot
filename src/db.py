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
    enabled INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT,
    timestamp REAL DEFAULT (strftime('%s', 'now'))
);
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
    """Return {address: {alias, enabled}} for all overrides."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT address, alias, enabled FROM wallet_overrides")
        rows = await cursor.fetchall()
        return {r["address"]: {"alias": r["alias"], "enabled": bool(r["enabled"])} for r in rows}
    finally:
        await db.close()


async def set_wallet_override(address: str, alias: str = "", enabled: bool = True):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO wallet_overrides (address, alias, enabled) VALUES (?, ?, ?) "
            "ON CONFLICT(address) DO UPDATE SET alias=excluded.alias, enabled=excluded.enabled",
            (address.lower(), alias, int(enabled)),
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
