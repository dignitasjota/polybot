"""Dashboard route — migrated from src/web.py to Jinja2 template."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiohttp import web
import aiohttp_jinja2

from src.db import get_wallet_overrides

routes = web.RouteTableDef()


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


@routes.get("/")
async def handle_dashboard(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    session = request.get("session", {})

    tz_display = timezone(timedelta(hours=1))
    now = datetime.now(tz_display)

    # Load wallet aliases for copy trade source display
    overrides = await get_wallet_overrides()
    wallet_aliases = {addr: ov.get("alias", "") for addr, ov in overrides.items() if ov.get("alias")}

    # Build per-account data for template
    accounts_data = []
    for acc in bot.accounts:
        accounts_data.append(_build_account_data(acc, tz_display, wallet_aliases))

    # Markets
    markets = bot.tracker.all_markets if bot.tracker else []
    markets_html = _render_markets(markets)
    ws_connected = bot.ws_client.is_connected if bot.ws_client else False

    context = {
        "active_tab": "dashboard",
        "session_user": session.get("user", ""),
        "bot_accounts": [a.name for a in bot.accounts],
        "now_str": now.strftime("%Y-%m-%d %H:%M:%S"),
        "ws_connected": ws_connected,
        "markets_count": len(markets),
        "accounts_data": accounts_data,
        "markets_html": markets_html,
    }
    return aiohttp_jinja2.render_template("dashboard.html", request, context)


def _build_account_data(acc, tz_display, wallet_aliases: dict | None = None) -> dict:
    stats = acc.get_stats()
    is_copy = acc.strategy_type == "copy_trade"

    if is_copy and "copy_trader" in stats:
        s = stats["copy_trader"]
    elif not is_copy and "detector" in stats:
        s = stats["detector"]
    else:
        s = {}

    # In live mode, show real USDC balance from executor
    executor_stats = stats.get("executor", {})
    live_balance = executor_stats.get("live_balance")
    balance = live_balance if live_balance is not None else s.get("current_balance", s.get("starting_balance", 0))
    starting = s.get("starting_balance", 0)

    opps = acc.export_opportunities()
    opps_html = _render_copy_table(opps, tz_display, wallet_aliases or {}) if is_copy else _render_directional_table(opps, tz_display)

    return {
        "name": acc.name,
        "is_copy": is_copy,
        "badge_class": "badge-copy" if is_copy else "badge-directional",
        "badge_text": "COPY-TRADE" if is_copy else "DIRECTIONAL",
        "exec_mode": acc.exec_mode.value,
        "balance": balance,
        "starting": starting,
        "pnl": s.get("simulated_pnl", 0),
        "roi": s.get("roi_pct", 0),
        "wins": s.get("settled_wins", 0),
        "losses": s.get("settled_losses", 0),
        "copied": s.get("trades_copied", 0),
        "polls": s.get("polls", 0),
        "opportunities_found": s.get("opportunities_found", 0),
        "total_scans": s.get("total_scans", 0),
        "opps_html": opps_html,
    }


def _render_markets(markets) -> str:
    html = ""
    for m in sorted(markets, key=lambda x: x.hours_to_resolution or 999):
        hours = m.hours_to_resolution
        if hours is None:
            time_left = "?"
        elif hours < 1:
            time_left = f"{hours * 60:.0f}min"
        else:
            time_left = f"{hours:.1f}h"

        stale_class = "stale" if m.is_stale else ""
        resolved_badge = '<span class="badge resolved">RESOLVED</span>' if m.resolved else ""
        best_price = max(m.best_ask_yes, m.best_ask_no)
        best_side = "YES" if m.best_ask_yes >= m.best_ask_no else "NO"

        html += f"""
        <tr class="{stale_class}">
            <td class="question">{_esc(m.question[:70])}</td>
            <td>{time_left}</td>
            <td>${m.best_ask_yes:.4f}</td>
            <td>${m.best_ask_no:.4f}</td>
            <td><strong>${best_price:.4f}</strong> ({best_side})</td>
            <td>{resolved_badge}</td>
        </tr>"""
    return html


def _render_copy_table(opportunities: list[dict], tz_display, wallet_aliases: dict) -> str:
    bets = [o for o in opportunities if o.get("suggested_bet", 0) > 0]
    bets.sort(key=lambda x: x["timestamp"], reverse=True)
    bets = bets[:30]

    if not bets:
        return '<div style="color:#555;padding:10px;">No copy trades yet...</div>'

    rows = ""
    for b in bets:
        ts = datetime.fromtimestamp(b["timestamp"], tz=tz_display).strftime("%H:%M:%S")
        outcome = b.get("outcome", "pending")
        pnl = b.get("actual_pnl", 0)

        if outcome == "win":
            badge = '<span class="badge win">WIN</span>'
            pnl_class = "pnl-win"
        elif outcome == "loss":
            badge = '<span class="badge loss">LOSS</span>'
            pnl_class = "pnl-loss"
        else:
            badge = '<span class="badge pending">PENDING</span>'
            pnl_class = ""

        dur = b.get("duration_seconds", 0)
        dur_str = f"{dur:.0f}s" if dur > 0 else "-"
        wallet_src = b.get("wallet_source", "")
        # Resolve alias: wallet_source has first 10 chars, match against full addresses
        wallet_display = wallet_src
        for addr, alias in wallet_aliases.items():
            if addr.startswith(wallet_src):
                wallet_display = alias
                break

        rows += f"""
        <tr class="copy-row">
            <td>{ts}</td>
            <td class="question">{_esc(b['question'][:55])}</td>
            <td>{b['token_side']}</td>
            <td>${b['token_price']:.4f}</td>
            <td>${b.get('suggested_bet', 0):.2f}</td>
            <td>${b.get('potential_profit', 0):.2f}</td>
            <td>{dur_str}</td>
            <td>{badge}</td>
            <td class="{pnl_class}">{f'${pnl:+.2f}' if outcome != 'pending' else '-'}</td>
            <td style="color:#888;font-size:0.75em;">{_esc(wallet_display)}</td>
        </tr>"""

    return f"""
    <table>
        <thead><tr>
            <th>Time</th><th>Market</th><th>Side</th><th>Price</th>
            <th>Bet</th><th>Profit Est</th><th>Duration</th>
            <th>Result</th><th>P&L</th><th>Source</th>
        </tr></thead>
        <tbody>{rows}</tbody>
    </table>"""


def _render_directional_table(opportunities: list[dict], tz_display) -> str:
    grouped: dict[str, dict] = {}
    for o in opportunities:
        key = f"{o['condition_id']}:{o['token_side']}"
        if key not in grouped:
            grouped[key] = {"bet": None, "updates": []}
        if o.get("suggested_bet", 0) > 0:
            grouped[key]["bet"] = o
        else:
            grouped[key]["updates"].append(o)

    sorted_groups = sorted(
        grouped.values(),
        key=lambda g: g["bet"]["timestamp"] if g["bet"] else 0,
        reverse=True,
    )[:30]

    rows = ""
    group_idx = 0
    for group in sorted_groups:
        bet = group["bet"]
        updates = group["updates"]
        if bet is None:
            continue

        ts = datetime.fromtimestamp(bet["timestamp"], tz=tz_display).strftime("%H:%M:%S")
        hours = bet.get("hours_remaining", 0)
        time_left = f"{hours * 60:.0f}min" if hours < 1 else f"{hours:.1f}h"
        margin_pct = bet["margin_net"] / bet["token_price"] * 100 if bet["token_price"] > 0 else 0
        outcome = bet.get("outcome", "pending")
        pnl = bet.get("actual_pnl", 0)

        if outcome == "win":
            badge = '<span class="badge win">WIN</span>'
            pnl_class = "pnl-win"
        elif outcome == "loss":
            badge = '<span class="badge loss">LOSS</span>'
            pnl_class = "pnl-loss"
        else:
            badge = '<span class="badge pending">PENDING</span>'
            pnl_class = ""

        dur = bet.get("duration_seconds", 0)
        dur_str = f"{dur:.0f}s" if dur > 0 else "-"

        toggle = ""
        if updates:
            toggle = f'<span class="toggle-btn" onclick="toggleUpdates({group_idx})">+ {len(updates)}</span>'

        rows += f"""
        <tr class="bet-row">
            <td>{ts} {toggle}</td>
            <td class="question">{_esc(bet['question'][:55])}</td>
            <td>{bet['token_side']}</td>
            <td>${bet['token_price']:.4f}</td>
            <td>${bet['margin_net']:.4f} <span class="pct">({margin_pct:.1f}%)</span></td>
            <td>{time_left}</td>
            <td>{bet.get('depth_at_price', 0):.0f}</td>
            <td>${bet.get('suggested_bet', 0):.2f}</td>
            <td>${bet.get('potential_profit', 0):.2f}</td>
            <td>{dur_str}</td>
            <td>{badge}</td>
            <td class="{pnl_class}">{f'${pnl:+.2f}' if outcome != 'pending' else '-'}</td>
        </tr>"""

        for u in updates:
            u_ts = datetime.fromtimestamp(u["timestamp"], tz=tz_display).strftime("%H:%M:%S")
            u_hours = u.get("hours_remaining", 0)
            u_time_left = f"{u_hours * 60:.0f}min" if u_hours < 1 else f"{u_hours:.1f}h"
            u_margin_pct = u["margin_net"] / u["token_price"] * 100 if u["token_price"] > 0 else 0

            rows += f"""
        <tr class="update-row update-group-{group_idx}">
            <td class="update-indent">{u_ts}</td>
            <td class="question update-text">{_esc(u['question'][:55])}</td>
            <td>{u['token_side']}</td>
            <td>${u['token_price']:.4f}</td>
            <td>${u['margin_net']:.4f} <span class="pct">({u_margin_pct:.1f}%)</span></td>
            <td>{u_time_left}</td>
            <td>{u.get('depth_at_price', 0):.0f}</td>
            <td>-</td><td>-</td><td>-</td><td></td><td></td>
        </tr>"""
        group_idx += 1

    if not rows:
        return '<div style="color:#555;padding:10px;">No opportunities detected yet...</div>'

    return f"""
    <table>
        <thead><tr>
            <th>Time</th><th>Market</th><th>Side</th><th>Price</th>
            <th>Margin Net</th><th>Time Left</th><th>Depth</th>
            <th>Bet</th><th>Profit</th><th>Duration</th>
            <th>Result</th><th>P&L</th>
        </tr></thead>
        <tbody>{rows}</tbody>
    </table>"""
