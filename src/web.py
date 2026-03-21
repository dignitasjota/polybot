from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from aiohttp import web

import structlog

logger = structlog.get_logger("polymarket.web")


def create_app(bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/api/opportunities", handle_api)
    app.router.add_get("/api/report", handle_report)
    app.router.add_get("/api/report/{account}", handle_account_report)
    return app


async def handle_api(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    data = {
        "exported_at": time.time(),
        "accounts": [],
        "markets_active": len(bot.tracker.all_markets) if bot.tracker else 0,
        "ws_connected": bot.ws_client.is_connected if bot.ws_client else False,
    }
    for acc in bot.accounts:
        data["accounts"].append({
            "name": acc.name,
            "strategy": acc.strategy_type,
            "stats": acc.get_stats(),
            "opportunities": acc.export_opportunities(),
        })
    return web.json_response(data)


async def handle_report(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    reports = {}
    for acc in bot.accounts:
        report = acc.export_full_report()
        if report:
            reports[acc.name] = report
    return web.json_response(reports)


async def handle_account_report(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    account_name = request.match_info["account"]
    for acc in bot.accounts:
        if acc.name == account_name:
            report = acc.export_full_report()
            if report:
                return web.json_response(report)
            return web.json_response({"error": "no data"}, status=404)
    return web.json_response({"error": "account not found"}, status=404)


async def handle_dashboard(request: web.Request) -> web.Response:
    bot = request.app["bot"]

    tz_display = timezone(timedelta(hours=1))  # GMT+1 (CET)
    now = datetime.now(tz_display)

    # Build per-account sections
    accounts_html = ""
    for acc in bot.accounts:
        accounts_html += _render_account_section(acc, tz_display)

    # Markets table (shared by directional accounts)
    markets_html = ""
    markets = bot.tracker.all_markets if bot.tracker else []
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

        markets_html += f"""
        <tr class="{stale_class}">
            <td class="question">{_esc(m.question[:70])}</td>
            <td>{time_left}</td>
            <td>${m.best_ask_yes:.4f}</td>
            <td>${m.best_ask_no:.4f}</td>
            <td><strong>${best_price:.4f}</strong> ({best_side})</td>
            <td>{resolved_badge}</td>
        </tr>"""

    ws_connected = bot.ws_client.is_connected if bot.ws_client else False

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Multi-Strategy Bot</title>
<meta http-equiv="refresh" content="10">
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: 'Courier New', monospace; background: #0a0a0a; color: #e0e0e0; padding: 20px; }}
    h1 {{ color: #00ff88; margin-bottom: 5px; font-size: 1.4em; }}
    h2 {{ color: #00aaff; margin: 20px 0 10px; font-size: 1.1em; }}
    h3 {{ color: #ffaa00; margin: 15px 0 8px; font-size: 1em; }}
    .subtitle {{ color: #666; font-size: 0.85em; margin-bottom: 20px; }}
    .account-section {{
        border: 1px solid #333; border-radius: 8px; padding: 15px;
        margin-bottom: 20px; background: #0d0d1a;
    }}
    .account-header {{
        display: flex; align-items: center; gap: 12px; margin-bottom: 12px;
    }}
    .account-name {{ color: #00ff88; font-size: 1.2em; font-weight: bold; }}
    .account-badge {{
        padding: 3px 8px; border-radius: 4px; font-size: 0.75em; font-weight: bold;
    }}
    .badge-directional {{ background: #00aaff; color: #000; }}
    .badge-copy {{ background: #ff6600; color: #000; }}
    .stats {{
        display: flex; gap: 15px; flex-wrap: wrap; margin-bottom: 15px;
    }}
    .stat {{
        background: #1a1a2e; border: 1px solid #333; border-radius: 6px;
        padding: 10px 14px; min-width: 120px;
    }}
    .stat .label {{ color: #888; font-size: 0.7em; text-transform: uppercase; }}
    .stat .value {{ color: #00ff88; font-size: 1.3em; font-weight: bold; }}
    .stat .value.warn {{ color: #ffaa00; }}
    .stat .value.off {{ color: #ff4444; }}
    table {{
        width: 100%; border-collapse: collapse; font-size: 0.8em;
        margin-bottom: 15px;
    }}
    th {{
        background: #1a1a2e; color: #00aaff; text-align: left;
        padding: 6px 8px; border-bottom: 2px solid #333;
        position: sticky; top: 0;
    }}
    td {{ padding: 5px 8px; border-bottom: 1px solid #1a1a2e; }}
    tr:hover {{ background: #1a1a2e; }}
    .question {{ max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .stale td {{ color: #555; }}
    .resolved-row td {{ color: #00ff88; }}
    .badge {{ padding: 2px 6px; border-radius: 3px; font-size: 0.75em; font-weight: bold; }}
    .badge.resolved {{ background: #00ff88; color: #000; }}
    .pct {{ color: #888; font-size: 0.85em; }}
    .profit {{ color: #00ff88; font-weight: bold; }}
    .badge.win {{ background: #00ff88; color: #000; }}
    .badge.loss {{ background: #ff4444; color: #fff; }}
    .badge.pending {{ background: #555; color: #ccc; }}
    .pnl-win {{ color: #00ff88; font-weight: bold; }}
    .pnl-loss {{ color: #ff4444; font-weight: bold; }}
    .bet-row {{ border-left: 3px solid #00aaff; }}
    .copy-row {{ border-left: 3px solid #ff6600; }}
    .toggle-btn {{
        cursor: pointer; color: #00aaff; font-size: 0.75em;
        background: #1a1a2e; border: 1px solid #333; border-radius: 3px;
        padding: 1px 5px; margin-left: 6px; user-select: none;
    }}
    .toggle-btn:hover {{ background: #2a2a4e; }}
    .update-row {{ display: none; }}
    .update-row.visible {{ display: table-row; }}
    .update-row td {{ color: #666; font-size: 0.8em; border-left: 3px solid #333; }}
    .update-indent {{ padding-left: 20px !important; }}
    .update-text {{ color: #555 !important; }}
    .footer {{ color: #444; font-size: 0.75em; margin-top: 20px; }}
    .ws-status {{ font-size: 0.8em; }}
    @media (max-width: 768px) {{
        table {{ font-size: 0.65em; }}
        .stats {{ gap: 8px; }}
        .stat {{ min-width: 90px; padding: 6px 10px; }}
    }}
</style>
</head>
<body>
    <h1>POLYMARKET MULTI-STRATEGY BOT</h1>
    <div class="subtitle">
        Paper Trading | Auto-refresh 10s | {now.strftime('%Y-%m-%d %H:%M:%S')} CET |
        <span class="ws-status">WS: {'🟢 LIVE' if ws_connected else '🔴 DOWN'}</span> |
        Markets: {len(markets)}
    </div>

    {accounts_html}

    <h2>MONITORED MARKETS ({len(markets)})</h2>
    <table>
        <thead>
            <tr>
                <th>Market</th>
                <th>Time Left</th>
                <th>YES Ask</th>
                <th>NO Ask</th>
                <th>Best</th>
                <th>Status</th>
            </tr>
        </thead>
        <tbody>
            {markets_html}
        </tbody>
    </table>

    <div class="footer">
        API: <a href="/api/opportunities" style="color:#00aaff">/api/opportunities</a> |
        <a href="/api/report" style="color:#00aaff">/api/report</a> |
        Per-account: {' | '.join(f'<a href="/api/report/{acc.name}" style="color:#00aaff">/api/report/{acc.name}</a>' for acc in bot.accounts)}
    </div>

    <script>
    function toggleUpdates(groupIdx) {{
        const rows = document.querySelectorAll('.update-group-' + groupIdx);
        const btn = event.currentTarget;
        const isVisible = rows.length > 0 && rows[0].classList.contains('visible');
        rows.forEach(r => r.classList.toggle('visible'));
        btn.textContent = isVisible
            ? '+ ' + rows.length
            : '− ' + rows.length;
    }}
    </script>
</body>
</html>"""

    return web.Response(text=html, content_type="text/html")


def _render_account_section(acc, tz_display) -> str:
    """Render a single account's stats + opportunities."""
    stats = acc.get_stats()
    is_copy = acc.strategy_type == "copy_trade"
    badge_class = "badge-copy" if is_copy else "badge-directional"
    badge_text = "COPY-TRADE" if is_copy else "DIRECTIONAL"
    row_class = "copy-row" if is_copy else "bet-row"

    # Extract the right stats
    if is_copy and "copy_trader" in stats:
        s = stats["copy_trader"]
    elif not is_copy and "detector" in stats:
        s = stats["detector"]
    else:
        s = {}

    balance = s.get("current_balance", s.get("starting_balance", 0))
    starting = s.get("starting_balance", 0)
    pnl = s.get("simulated_pnl", 0)
    roi = s.get("roi_pct", 0)
    wins = s.get("settled_wins", 0)
    losses = s.get("settled_losses", 0)
    copied = s.get("trades_copied", 0)

    # Build stats bar
    stats_html = f"""
    <div class="stats">
        <div class="stat">
            <div class="label">Balance</div>
            <div class="value{' off' if balance < starting else ''}">${balance:.2f}</div>
        </div>
        <div class="stat">
            <div class="label">P&L</div>
            <div class="value{' off' if pnl < 0 else ''}">${pnl:+.2f}</div>
        </div>
        <div class="stat">
            <div class="label">ROI</div>
            <div class="value{' off' if roi < 0 else ''}">{roi:+.2f}%</div>
        </div>
        <div class="stat">
            <div class="label">Wins / Losses</div>
            <div class="value">{wins} / {losses}</div>
        </div>"""

    if is_copy:
        stats_html += f"""
        <div class="stat">
            <div class="label">Trades Copied</div>
            <div class="value">{copied}</div>
        </div>
        <div class="stat">
            <div class="label">Polls</div>
            <div class="value">{s.get('polls', 0)}</div>
        </div>"""
    else:
        stats_html += f"""
        <div class="stat">
            <div class="label">Opportunities</div>
            <div class="value">{s.get('opportunities_found', 0)}</div>
        </div>
        <div class="stat">
            <div class="label">Scans</div>
            <div class="value">{s.get('total_scans', 0)}</div>
        </div>"""

    stats_html += "</div>"

    # Build opportunities table
    opportunities = acc.export_opportunities()
    opps_html = _render_opportunities_table(opportunities, tz_display, row_class, is_copy)

    return f"""
    <div class="account-section">
        <div class="account-header">
            <span class="account-name">{_esc(acc.name.upper())}</span>
            <span class="account-badge {badge_class}">{badge_text}</span>
            <span style="color:#666;font-size:0.8em;">({acc.exec_mode.value})</span>
        </div>
        {stats_html}
        <h3>Bets (last 30)</h3>
        {opps_html}
    </div>"""


def _render_opportunities_table(opportunities: list[dict], tz_display, row_class: str, is_copy: bool) -> str:
    """Render the opportunities/bets table for an account."""
    if is_copy:
        # Copy trades: simple table, one row per bet
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
            wallet = b.get("wallet_source", "")

            rows += f"""
            <tr class="{row_class}">
                <td>{ts}</td>
                <td class="question">{_esc(b['question'][:55])}</td>
                <td>{b['token_side']}</td>
                <td>${b['token_price']:.4f}</td>
                <td>${b.get('suggested_bet', 0):.2f}</td>
                <td>${b.get('potential_profit', 0):.2f}</td>
                <td>{dur_str}</td>
                <td>{badge}</td>
                <td class="{pnl_class}">{f'${pnl:+.2f}' if outcome != 'pending' else '-'}</td>
                <td style="color:#888;font-size:0.75em;">{wallet}</td>
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

    # Directional: grouped table with price updates (existing logic)
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
        <tr class="{row_class}">
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


def _esc(text: str) -> str:
    """Escape HTML special characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
