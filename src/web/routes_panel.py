"""Panel routes — copy-trade, directional, settings."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiohttp import web
import aiohttp_jinja2

from src.db import (
    add_audit, change_password, get_audit_log, get_wallet_overrides,
    set_wallet_override, remove_wallet_override, verify_password,
)
from src.web.config_manager import ConfigManager

routes = web.RouteTableDef()


async def _sync_wallet_overrides_to_copy_trader(request: web.Request):
    """Push updated wallet roles/enabled to live CopyTrader instances."""
    overrides = await get_wallet_overrides()
    bot = request.app["bot"]
    for runner in bot.accounts:
        if runner.strategy_type == "copy_trade" and runner.copy_trader:
            runner.copy_trader.set_wallet_overrides(overrides)


def _get_cm(request: web.Request) -> ConfigManager:
    bot = request.app["bot"]
    if "config_manager" not in request.app:
        request.app["config_manager"] = ConfigManager(bot)
    return request.app["config_manager"]


async def _base_context(request: web.Request) -> dict:
    session = request.get("session", {})
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
            "role": ov.get("role", "primary"),
            "confirms_wallet": ov.get("confirms_wallet", ""),
        })
    # Add wallets only in overrides (added via panel, not yet in TOML)
    for addr, ov in overrides.items():
        if addr not in wallets_in_config:
            wallets.append({
                "address": addr,
                "alias": ov.get("alias", ""),
                "enabled": ov.get("enabled", True),
                "role": ov.get("role", "primary"),
                "confirms_wallet": ov.get("confirms_wallet", ""),
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
    session = request.get("session", {})
    user = session.get("user", "unknown")
    data = await request.post()
    action = data.get("action", "")
    address = data.get("address", "").strip().lower()

    if action == "add" and address:
        alias = data.get("alias", "").strip()
        role = data.get("role", "primary")
        cm.add_copy_wallet(address)
        await set_wallet_override(address, alias=alias, enabled=True, role=role)
        await _sync_wallet_overrides_to_copy_trader(request)
        await add_audit(user, "wallet_add", f"{address} alias={alias} role={role}")
        msg = f'<div class="flash flash-success">Wallet added: {address[:10]}...</div>'

    elif action == "remove" and address:
        cm.remove_copy_wallet(address)
        await remove_wallet_override(address)
        await _sync_wallet_overrides_to_copy_trader(request)
        await add_audit(user, "wallet_remove", address)
        msg = f'<div class="flash flash-success">Wallet removed: {address[:10]}...</div>'

    elif action == "toggle" and address:
        enabled = data.get("enabled", "true").lower() == "true"
        # Preserve existing fields
        overrides = await get_wallet_overrides()
        existing = overrides.get(address, {})
        await set_wallet_override(
            address,
            alias=existing.get("alias", ""),
            enabled=enabled,
            role=existing.get("role", "primary"),
            confirms_wallet=existing.get("confirms_wallet", ""),
        )
        await _sync_wallet_overrides_to_copy_trader(request)
        await add_audit(user, "wallet_toggle", f"{address} enabled={enabled}")
        state = "enabled" if enabled else "disabled"
        msg = f'<div class="flash flash-info">Wallet {state}: {address[:10]}...</div>'

    elif action == "set_role" and address:
        role = data.get("role", "primary")
        if role not in ("primary", "confirmation"):
            role = "primary"
        confirms_wallet = data.get("confirms_wallet", "").strip().lower()
        overrides = await get_wallet_overrides()
        existing = overrides.get(address, {})
        await set_wallet_override(
            address,
            alias=existing.get("alias", ""),
            enabled=existing.get("enabled", True),
            role=role,
            confirms_wallet=confirms_wallet if role == "confirmation" else "",
        )
        await _sync_wallet_overrides_to_copy_trader(request)
        await add_audit(user, "wallet_role", f"{address} role={role} confirms={confirms_wallet}")
        msg = f'<div class="flash flash-info">Wallet role updated: {address[:10]}...</div>'

    else:
        msg = '<div class="flash flash-error">Invalid action</div>'

    # If htmx request, return partial
    if request.headers.get("HX-Request"):
        return web.Response(text=msg, content_type="text/html")
    raise web.HTTPFound("/panel/copy-trade")


@routes.post("/panel/copy-trade/params")
async def panel_copy_params(request: web.Request) -> web.Response:
    cm = _get_cm(request)
    session = request.get("session", {})
    user = session.get("user", "unknown")
    data = await request.post()

    params = {}
    for key in ("fixed_bet_size", "poll_interval_ms", "min_price", "max_concurrent_bets",
                "spread_arb_multiplier", "max_bet_per_trade", "max_daily_loss", "simulated_balance"):
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
    crypto_configs = cm.get_crypto_configs()
    # Sort: enabled first, then alphabetical
    crypto_list = [
        {"name": name, **cfg}
        for name, cfg in sorted(crypto_configs.items(), key=lambda x: (not x[1]["enabled"], x[0]))
    ]
    ctx.update({
        "active_tab": "directional",
        "params": cm.get_directional_params(),
        "crypto_configs": crypto_list,
    })
    return aiohttp_jinja2.render_template("panel/directional.html", request, ctx)


@routes.post("/panel/directional/params")
async def panel_directional_params(request: web.Request) -> web.Response:
    cm = _get_cm(request)
    session = request.get("session", {})
    user = session.get("user", "unknown")
    data = await request.post()

    params = {}
    for key in ("kill_switch", "min_margin_net", "max_price", "min_buffer_pct",
                "max_concurrent_bets", "max_bet_per_trade", "max_daily_loss",
                "simulated_balance", "max_markets_monitored"):
        if key in data and data[key]:
            params[key] = data[key]

    # Checkbox: if "crypto_only" field exists in the form but unchecked, it won't be in data
    if "crypto_only" in data:
        params["crypto_only"] = "true"
    elif any(k in data for k in ("max_markets_monitored",)):
        # Market filter form was submitted but crypto_only unchecked
        params["crypto_only"] = "false"

    cm.set_directional_params(params)
    await add_audit(user, "directional_params", str(params))

    raise web.HTTPFound("/panel/directional")


@routes.post("/panel/directional/crypto")
async def panel_directional_crypto(request: web.Request) -> web.Response:
    cm = _get_cm(request)
    session = request.get("session", {})
    user = session.get("user", "unknown")
    data = await request.post()

    action = data.get("action", "")
    crypto = data.get("crypto", "").strip().lower()

    if action == "toggle" and crypto:
        enabled = data.get("enabled", "true").lower() == "true"
        cm.set_crypto_config(crypto, enabled=enabled)
        await add_audit(user, "crypto_toggle", f"{crypto} enabled={enabled}")
        state = "enabled" if enabled else "disabled"
        msg = f'<div class="flash flash-info">{crypto.title()} {state}</div>'

    elif action == "set_buffer" and crypto:
        try:
            buffer = float(data.get("buffer_pct", "0.03"))
            buffer = max(0.005, min(0.20, buffer))  # Clamp to sane range
        except ValueError:
            buffer = 0.03
        cm.set_crypto_config(crypto, buffer_pct=buffer)
        await add_audit(user, "crypto_buffer", f"{crypto} buffer_pct={buffer}")
        msg = f'<div class="flash flash-info">{crypto.title()} buffer: {buffer*100:.1f}%</div>'

    else:
        msg = '<div class="flash flash-error">Invalid action</div>'

    if request.headers.get("HX-Request"):
        return web.Response(text=msg, content_type="text/html")
    raise web.HTTPFound("/panel/directional")


# ── Settings ───────────────────────────────────────────────────────

@routes.get("/panel/settings")
async def panel_settings(request: web.Request) -> web.Response:
    cm = _get_cm(request)
    ctx = await _base_context(request)
    tz_display = timezone(timedelta(hours=1))

    log_entries = await get_audit_log(50)
    for entry in log_entries:
        ts = entry.get("timestamp", 0)
        entry["time_str"] = datetime.fromtimestamp(ts, tz=tz_display).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"

    ctx.update({
        "active_tab": "settings",
        "audit_log": log_entries,
        "account_modes": cm.get_account_modes(),
        "strategy_modes": cm.get_strategy_modes(),
        "flash_msg": None,
        "flash_type": None,
    })
    return aiohttp_jinja2.render_template("panel/settings.html", request, ctx)


@routes.post("/panel/settings/password")
async def panel_change_password(request: web.Request) -> web.Response:
    session = request.get("session", {})
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


@routes.post("/panel/settings/execution-mode")
async def panel_execution_mode(request: web.Request) -> web.Response:
    cm = _get_cm(request)
    session = request.get("session", {})
    user = session.get("user", "unknown")
    data = await request.post()

    account_name = data.get("account", "")
    mode = data.get("mode", "")

    if mode not in ("paper", "dry_run", "live"):
        msg = '<div class="flash flash-error">Invalid mode</div>'
    else:
        ok = await cm.set_account_mode(account_name, mode)
        if ok:
            await add_audit(user, "execution_mode", f"{account_name} → {mode}")
            colors = {"live": "#ff4444", "dry_run": "#ffaa00", "paper": "#00ff88"}
            color = colors.get(mode, "#00ff88")
            msg = f'<div class="flash flash-info">{account_name}: <span style="color:{color};font-weight:bold;">{mode.upper()}</span></div>'
        else:
            msg = f'<div class="flash flash-error">Account not found: {account_name}</div>'

    if request.headers.get("HX-Request"):
        return web.Response(text=msg, content_type="text/html")
    raise web.HTTPFound("/panel/settings")


@routes.post("/panel/settings/strategy-mode")
async def panel_strategy_mode(request: web.Request) -> web.Response:
    """Change mode for a specific strategy (disabled/paper/live)."""
    cm = _get_cm(request)
    session = request.get("session", {})
    user = session.get("user", "unknown")
    data = await request.post()

    account_name = data.get("account", "")
    strategy_name = data.get("strategy", "")
    mode = data.get("mode", "")

    if mode not in ("disabled", "paper", "dry_run", "live"):
        msg = '<div class="flash flash-error">Modo invalido</div>'
    else:
        ok = await cm.set_strategy_mode(account_name, strategy_name, mode)
        if ok:
            await add_audit(user, "strategy_mode", f"{account_name}/{strategy_name} → {mode}")
            colors = {"live": "#ff4444", "dry_run": "#ffaa00", "paper": "#00ff88", "disabled": "#888"}
            color = colors.get(mode, "#888")
            msg = f'<div class="flash flash-info">{account_name}/{strategy_name}: <span style="color:{color};font-weight:bold;">{mode.upper()}</span></div>'
        else:
            msg = f'<div class="flash flash-error">No encontrado: {account_name}/{strategy_name}</div>'

    if request.headers.get("HX-Request"):
        return web.Response(text=msg, content_type="text/html")
    raise web.HTTPFound("/panel/settings")


# ── Liquidity ──────────────────────────────────────────────────────

def _get_liquidity_scanner(request: web.Request):
    """Find the first liquidity strategy's scanner, or a standalone one."""
    bot = request.app["bot"]
    for runner in bot.accounts:
        for strat_name, strat in runner.strategies.items():
            if strat_name == "liquidity" and hasattr(strat, "scanner"):
                return strat.scanner
    return request.app.get("reward_scanner")


def _get_liquidity_provider(request: web.Request):
    """Find the first liquidity strategy's provider."""
    bot = request.app["bot"]
    for runner in bot.accounts:
        for strat_name, strat in runner.strategies.items():
            if strat_name == "liquidity" and hasattr(strat, "provider"):
                return strat.provider
    return None


@routes.get("/panel/liquidity")
async def panel_liquidity(request: web.Request) -> web.Response:
    ctx = await _base_context(request)
    scanner = _get_liquidity_scanner(request)

    if scanner:
        stats = scanner.get_stats()
        all_markets = scanner.get_all_markets()
        markets = [m.to_dict() for m in all_markets]
    else:
        stats = {
            "total_reward_markets": 0,
            "last_scan_ago": None,
            "scan_count": 0,
            "scan_errors": 0,
            "total_daily_rewards_available": 0,
        }
        markets = []

    # Get config params
    params = {
        "scan_interval": 300,
        "min_daily_rate": 1.0,
        "min_reward_per_dollar": 0.0005,
        "capital_per_market": 50.0,
        "max_markets": 5,
    }
    if scanner:
        params.update({
            "scan_interval": scanner._scan_interval,
            "min_daily_rate": scanner._min_daily_rate,
            "min_reward_per_dollar": scanner._min_reward_per_dollar,
            "capital_per_market": scanner._capital_per_market,
        })
    # Check if there's a LiquidityConfig
    bot = request.app["bot"]
    for runner in bot.accounts:
        for strat_name, strat in runner.strategies.items():
            if strat_name == "liquidity" and hasattr(strat, "config"):
                params["max_markets"] = strat.config.max_markets
                break

    # Provider stats
    provider = _get_liquidity_provider(request)
    provider_stats = provider.get_stats() if provider else {"running": False, "positions": []}

    # Metrics
    metrics_today = {}
    metrics_summary = {}
    bot = request.app["bot"]
    for runner in bot.accounts:
        for strat_name, strat in runner.strategies.items():
            if strat_name == "liquidity" and hasattr(strat, "metrics"):
                metrics_today = strat.metrics.get_today()
                metrics_summary = strat.metrics.get_summary(days=7)
                break

    ctx.update({
        "active_tab": "liquidity",
        "stats": stats,
        "markets": markets,
        "params": params,
        "provider": provider_stats,
        "metrics_today": metrics_today,
        "metrics_summary": metrics_summary,
    })
    return aiohttp_jinja2.render_template("panel/liquidity.html", request, ctx)


@routes.post("/panel/liquidity/scan")
async def panel_liquidity_scan(request: web.Request) -> web.Response:
    """Trigger an immediate scan."""
    scanner = _get_liquidity_scanner(request)
    if not scanner:
        # Create standalone scanner on first use
        from src.reward_scanner import RewardScanner
        scanner = RewardScanner()
        request.app["reward_scanner"] = scanner

    await scanner.scan()

    raise web.HTTPFound("/panel/liquidity")


@routes.post("/panel/liquidity/params")
async def panel_liquidity_params(request: web.Request) -> web.Response:
    """Update scanner config via ConfigManager (hot-reload + persist)."""
    cm = _get_cm(request)
    data = await request.post()
    cm.set_liquidity_params(dict(data))
    raise web.HTTPFound("/panel/liquidity")


@routes.post("/panel/liquidity/cancel-all")
async def panel_liquidity_cancel_all(request: web.Request) -> web.Response:
    """Emergency: cancel all outstanding liquidity orders."""
    provider = _get_liquidity_provider(request)
    if provider:
        await provider._cancel_all_orders()
    raise web.HTTPFound("/panel/liquidity")


async def _settings_with_flash(request, msg, flash_type):
    cm = _get_cm(request)
    ctx = await _base_context(request)
    tz_display = timezone(timedelta(hours=1))
    log_entries = await get_audit_log(50)
    for entry in log_entries:
        ts = entry.get("timestamp", 0)
        entry["time_str"] = datetime.fromtimestamp(ts, tz=tz_display).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"

    ctx.update({
        "active_tab": "settings",
        "audit_log": log_entries,
        "account_modes": cm.get_account_modes(),
        "strategy_modes": cm.get_strategy_modes(),
        "flash_msg": msg,
        "flash_type": flash_type,
    })
    return aiohttp_jinja2.render_template("panel/settings.html", request, ctx)
