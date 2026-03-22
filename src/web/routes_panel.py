"""Panel routes — copy-trade, directional, settings."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiohttp import web
from aiohttp_session import get_session
import aiohttp_jinja2

from src.db import (
    add_audit, change_password, get_audit_log, get_wallet_overrides,
    set_wallet_override, remove_wallet_override, verify_password,
)
from src.web.config_manager import ConfigManager

routes = web.RouteTableDef()


def _get_cm(request: web.Request) -> ConfigManager:
    bot = request.app["bot"]
    if "config_manager" not in request.app:
        request.app["config_manager"] = ConfigManager(bot)
    return request.app["config_manager"]


async def _base_context(request: web.Request) -> dict:
    session = await get_session(request)
    bot = request.app["bot"]
    return {
        "session_user": session.get("user", ""),
        "bot_accounts": [a.name for a in bot.accounts],
    }


# ── Copy Trade ─────────────────────────────────────────────────────

@routes.get("/panel/copy-trade")
async def panel_copy_trade(request: web.Request) -> web.Response:
    cm = _get_cm(request)
    ctx = await _base_context(request)

    params = cm.get_copy_trade_params()
    wallets_in_config = cm.get_copy_wallets()
    overrides = await get_wallet_overrides()

    # Merge: config wallets + overrides
    wallets = []
    for addr in wallets_in_config:
        ov = overrides.get(addr, {})
        wallets.append({
            "address": addr,
            "alias": ov.get("alias", ""),
            "enabled": ov.get("enabled", True),
        })
    # Add wallets only in overrides (added via panel, not yet in TOML)
    for addr, ov in overrides.items():
        if addr not in wallets_in_config:
            wallets.append({
                "address": addr,
                "alias": ov.get("alias", ""),
                "enabled": ov.get("enabled", True),
            })

    ctx.update({
        "active_tab": "copy-trade",
        "wallets": wallets,
        "params": params,
    })
    return aiohttp_jinja2.render_template("panel/copy_trade.html", request, ctx)


@routes.post("/panel/copy-trade/wallets")
async def panel_copy_wallets(request: web.Request) -> web.Response:
    cm = _get_cm(request)
    session = await get_session(request)
    user = session.get("user", "unknown")
    data = await request.post()
    action = data.get("action", "")
    address = data.get("address", "").strip().lower()

    if action == "add" and address:
        alias = data.get("alias", "").strip()
        cm.add_copy_wallet(address)
        await set_wallet_override(address, alias=alias, enabled=True)
        await add_audit(user, "wallet_add", f"{address} alias={alias}")
        msg = f'<div class="flash flash-success">Wallet added: {address[:10]}...</div>'

    elif action == "remove" and address:
        cm.remove_copy_wallet(address)
        await remove_wallet_override(address)
        await add_audit(user, "wallet_remove", address)
        msg = f'<div class="flash flash-success">Wallet removed: {address[:10]}...</div>'

    elif action == "toggle" and address:
        enabled = data.get("enabled", "true").lower() == "true"
        await set_wallet_override(address, enabled=enabled)
        await add_audit(user, "wallet_toggle", f"{address} enabled={enabled}")
        state = "enabled" if enabled else "disabled"
        msg = f'<div class="flash flash-info">Wallet {state}: {address[:10]}...</div>'

    else:
        msg = '<div class="flash flash-error">Invalid action</div>'

    # If htmx request, return partial
    if request.headers.get("HX-Request"):
        return web.Response(text=msg, content_type="text/html")
    raise web.HTTPFound("/panel/copy-trade")


@routes.post("/panel/copy-trade/params")
async def panel_copy_params(request: web.Request) -> web.Response:
    cm = _get_cm(request)
    session = await get_session(request)
    user = session.get("user", "unknown")
    data = await request.post()

    params = {}
    for key in ("fixed_bet_size", "poll_interval_ms", "min_price", "max_concurrent_bets",
                "max_bet_per_trade", "max_daily_loss"):
        if key in data and data[key]:
            params[key] = data[key]

    cm.set_copy_trade_params(params)
    await add_audit(user, "copy_trade_params", str(params))

    raise web.HTTPFound("/panel/copy-trade")


# ── Directional ────────────────────────────────────────────────────

@routes.get("/panel/directional")
async def panel_directional(request: web.Request) -> web.Response:
    cm = _get_cm(request)
    ctx = await _base_context(request)
    ctx.update({
        "active_tab": "directional",
        "params": cm.get_directional_params(),
    })
    return aiohttp_jinja2.render_template("panel/directional.html", request, ctx)


@routes.post("/panel/directional/params")
async def panel_directional_params(request: web.Request) -> web.Response:
    cm = _get_cm(request)
    session = await get_session(request)
    user = session.get("user", "unknown")
    data = await request.post()

    params = {}
    for key in ("kill_switch", "min_margin_net", "max_price", "min_buffer_pct",
                "max_concurrent_bets", "max_bet_per_trade", "max_daily_loss"):
        if key in data and data[key]:
            params[key] = data[key]

    cm.set_directional_params(params)
    await add_audit(user, "directional_params", str(params))

    raise web.HTTPFound("/panel/directional")


# ── Settings ───────────────────────────────────────────────────────

@routes.get("/panel/settings")
async def panel_settings(request: web.Request) -> web.Response:
    ctx = await _base_context(request)
    tz_display = timezone(timedelta(hours=1))

    log_entries = await get_audit_log(50)
    for entry in log_entries:
        ts = entry.get("timestamp", 0)
        entry["time_str"] = datetime.fromtimestamp(ts, tz=tz_display).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"

    ctx.update({
        "active_tab": "settings",
        "audit_log": log_entries,
        "flash_msg": None,
        "flash_type": None,
    })
    return aiohttp_jinja2.render_template("panel/settings.html", request, ctx)


@routes.post("/panel/settings/password")
async def panel_change_password(request: web.Request) -> web.Response:
    session = await get_session(request)
    user = session.get("user", "unknown")
    data = await request.post()

    current = data.get("current_password", "")
    new_pw = data.get("new_password", "")
    confirm = data.get("confirm_password", "")

    if new_pw != confirm:
        return await _settings_with_flash(request, "Passwords don't match", "error")

    if not await verify_password(user, current):
        return await _settings_with_flash(request, "Current password is incorrect", "error")

    if len(new_pw) < 4:
        return await _settings_with_flash(request, "Password must be at least 4 characters", "error")

    await change_password(user, new_pw)
    await add_audit(user, "password_change", "")

    return await _settings_with_flash(request, "Password changed successfully", "success")


async def _settings_with_flash(request, msg, flash_type):
    ctx = await _base_context(request)
    tz_display = timezone(timedelta(hours=1))
    log_entries = await get_audit_log(50)
    for entry in log_entries:
        ts = entry.get("timestamp", 0)
        entry["time_str"] = datetime.fromtimestamp(ts, tz=tz_display).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"

    ctx.update({
        "active_tab": "settings",
        "audit_log": log_entries,
        "flash_msg": msg,
        "flash_type": flash_type,
    })
    return aiohttp_jinja2.render_template("panel/settings.html", request, ctx)
