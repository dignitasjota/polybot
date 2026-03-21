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
    return app


async def handle_api(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    data = {
        "exported_at": time.time(),
        "stats": bot.detector.get_stats(),
        "markets_active": len(bot.tracker.all_markets),
        "ws_connected": bot.ws_client.is_connected,
        "opportunities": bot.detector.export_opportunities(),
    }
    return web.json_response(data)


async def handle_report(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    report = bot.detector.export_full_report()
    return web.json_response(report)


async def handle_dashboard(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    stats = bot.detector.get_stats()
    opportunities = bot.detector.export_opportunities()
    markets = bot.tracker.all_markets

    tz_display = timezone(timedelta(hours=1))  # GMT+1 (CET)
    now = datetime.now(tz_display)

    # Build markets summary
    markets_html = ""
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

    # Group opportunities: bets (suggested_bet > 0) with their price updates
    grouped: dict[str, dict] = {}  # "condition_id:side" -> {"bet": opp, "updates": [opp, ...]}
    for o in opportunities:
        key = f"{o['condition_id']}:{o['token_side']}"
        if key not in grouped:
            grouped[key] = {"bet": None, "updates": []}
        if o.get("suggested_bet", 0) > 0:
            grouped[key]["bet"] = o
        else:
            grouped[key]["updates"].append(o)

    # Sort groups by bet timestamp descending (newest first), limit to last 50
    sorted_groups = sorted(
        grouped.values(),
        key=lambda g: g["bet"]["timestamp"] if g["bet"] else 0,
        reverse=True,
    )[:50]

    opps_html = ""
    group_idx = 0
    for group in sorted_groups:
        bet = group["bet"]
        updates = group["updates"]

        if bet is None:
            continue

        # Render the main bet row
        ts = datetime.fromtimestamp(bet["timestamp"], tz=tz_display).strftime("%H:%M:%S")
        hours = bet.get("hours_remaining", 0)
        time_left = f"{hours * 60:.0f}min" if hours < 1 else f"{hours:.1f}h"
        margin_pct = bet["margin_net"] / bet["token_price"] * 100 if bet["token_price"] > 0 else 0
        resolved_class = "resolved-row" if bet["resolved"] else ""
        min_prob = bet.get("min_probability_required", 0)
        outcome = bet.get("outcome", "pending")
        actual_pnl = bet.get("actual_pnl", 0)

        if outcome == "win":
            outcome_badge = '<span class="badge win">WIN</span>'
            pnl_class = "pnl-win"
        elif outcome == "loss":
            outcome_badge = '<span class="badge loss">LOSS</span>'
            pnl_class = "pnl-loss"
        else:
            outcome_badge = '<span class="badge pending">PENDING</span>'
            pnl_class = ""

        dur = bet.get("duration_seconds", 0)
        dur_str = f"{dur:.0f}s" if dur > 0 else "-"

        # Toggle button if there are price updates
        toggle = ""
        if updates:
            toggle = f'<span class="toggle-btn" onclick="toggleUpdates({group_idx})">+ {len(updates)}</span>'

        opps_html += f"""
        <tr class="{resolved_class} bet-row">
            <td>{ts} {toggle}</td>
            <td class="question">{_esc(bet['question'][:60])}</td>
            <td>{bet['token_side']}</td>
            <td>${bet['token_price']:.4f}</td>
            <td>${bet['margin_net']:.4f} <span class="pct">({margin_pct:.2f}%)</span></td>
            <td>{time_left}</td>
            <td>{min_prob:.2f}</td>
            <td>{bet['depth_at_price']:.0f}</td>
            <td>${bet['suggested_bet']:.2f}</td>
            <td class="profit">${bet['potential_profit']:.2f}</td>
            <td>{dur_str}</td>
            <td>{"RESOLVED" if bet['resolved'] else "PRE"}</td>
            <td>{outcome_badge}</td>
            <td class="{pnl_class}">{f'${actual_pnl:+.2f}' if outcome != 'pending' else '-'}</td>
        </tr>"""

        # Render hidden price update rows
        for u in updates:
            u_ts = datetime.fromtimestamp(u["timestamp"], tz=tz_display).strftime("%H:%M:%S")
            u_hours = u.get("hours_remaining", 0)
            u_time_left = f"{u_hours * 60:.0f}min" if u_hours < 1 else f"{u_hours:.1f}h"
            u_margin_pct = u["margin_net"] / u["token_price"] * 100 if u["token_price"] > 0 else 0
            u_dur = u.get("duration_seconds", 0)
            u_dur_str = f"{u_dur:.0f}s" if u_dur > 0 else "-"

            opps_html += f"""
        <tr class="update-row update-group-{group_idx}">
            <td class="update-indent">{u_ts}</td>
            <td class="question update-text">{_esc(u['question'][:60])}</td>
            <td>{u['token_side']}</td>
            <td>${u['token_price']:.4f}</td>
            <td>${u['margin_net']:.4f} <span class="pct">({u_margin_pct:.2f}%)</span></td>
            <td>{u_time_left}</td>
            <td>{u.get('min_probability_required', 0):.2f}</td>
            <td>{u['depth_at_price']:.0f}</td>
            <td>-</td>
            <td>-</td>
            <td>{u_dur_str}</td>
            <td>{"RESOLVED" if u['resolved'] else "PRE"}</td>
            <td></td>
            <td></td>
        </tr>"""

        group_idx += 1

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Closing Arbitrage</title>
<meta http-equiv="refresh" content="10">
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: 'Courier New', monospace; background: #0a0a0a; color: #e0e0e0; padding: 20px; }}
    h1 {{ color: #00ff88; margin-bottom: 5px; font-size: 1.4em; }}
    h2 {{ color: #00aaff; margin: 20px 0 10px; font-size: 1.1em; }}
    .subtitle {{ color: #666; font-size: 0.85em; margin-bottom: 20px; }}
    .stats {{
        display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 20px;
    }}
    .stat {{
        background: #1a1a2e; border: 1px solid #333; border-radius: 6px;
        padding: 12px 18px; min-width: 140px;
    }}
    .stat .label {{ color: #888; font-size: 0.75em; text-transform: uppercase; }}
    .stat .value {{ color: #00ff88; font-size: 1.5em; font-weight: bold; }}
    .stat .value.warn {{ color: #ffaa00; }}
    .stat .value.off {{ color: #ff4444; }}
    table {{
        width: 100%; border-collapse: collapse; font-size: 0.85em;
        margin-bottom: 30px;
    }}
    th {{
        background: #1a1a2e; color: #00aaff; text-align: left;
        padding: 8px 10px; border-bottom: 2px solid #333;
        position: sticky; top: 0;
    }}
    td {{ padding: 6px 10px; border-bottom: 1px solid #1a1a2e; }}
    tr:hover {{ background: #1a1a2e; }}
    .question {{ max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
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
    @media (max-width: 768px) {{
        table {{ font-size: 0.7em; }}
        .stats {{ gap: 10px; }}
        .stat {{ min-width: 100px; padding: 8px 12px; }}
    }}
</style>
</head>
<body>
    <h1>POLYMARKET CLOSING ARBITRAGE</h1>
    <div class="subtitle">Phase 1.1 Observer | Auto-refresh 10s | {now.strftime('%Y-%m-%d %H:%M:%S')} CET</div>

    <div class="stats">
        <div class="stat">
            <div class="label">WS Status</div>
            <div class="value{'' if bot.ws_client.is_connected else ' warn' if bot.ws_client._fallback_active else ' off'}">{'LIVE' if bot.ws_client.is_connected else 'REST' if bot.ws_client._fallback_active else 'DOWN'}</div>
        </div>
        <div class="stat">
            <div class="label">Markets</div>
            <div class="value">{len(markets)}</div>
        </div>
        <div class="stat">
            <div class="label">Opportunities</div>
            <div class="value">{stats['opportunities_found']}</div>
        </div>
        <div class="stat">
            <div class="label">Resolved</div>
            <div class="value">{stats['resolved_opportunities']}</div>
        </div>
        <div class="stat">
            <div class="label">Scans</div>
            <div class="value">{stats['total_scans']}</div>
        </div>
        <div class="stat">
            <div class="label">Wins / Losses</div>
            <div class="value">{stats.get('settled_wins', 0)} / {stats.get('settled_losses', 0)}</div>
        </div>
    </div>

    <h2>PAPER TRADING</h2>
    <div class="stats">
        <div class="stat">
            <div class="label">Capital Inicial</div>
            <div class="value">${stats.get('starting_balance', 500):.2f}</div>
        </div>
        <div class="stat">
            <div class="label">Balance Actual</div>
            <div class="value{' off' if stats.get('current_balance', 0) < stats.get('starting_balance', 500) else ''}">${stats.get('current_balance', 500):.2f}</div>
        </div>
        <div class="stat">
            <div class="label">P&L Total</div>
            <div class="value{' off' if stats.get('simulated_pnl', 0) < 0 else ''}">${stats.get('simulated_pnl', 0):+.2f}</div>
        </div>
        <div class="stat">
            <div class="label">ROI</div>
            <div class="value{' off' if stats.get('roi_pct', 0) < 0 else ''}">{stats.get('roi_pct', 0):+.2f}%</div>
        </div>
        <div class="stat">
            <div class="label">Bet Size ({bot.detector.risk.max_bet_pct}%)</div>
            <div class="value">${stats.get('current_balance', 500) * bot.detector.risk.max_bet_pct / 100:.2f}</div>
        </div>
    </div>

    <h2>OPPORTUNITIES (last 50)</h2>
    <table>
        <thead>
            <tr>
                <th>Time</th>
                <th>Market</th>
                <th>Side</th>
                <th>Price</th>
                <th>Margin Net</th>
                <th>Time Left</th>
                <th>Min Prob</th>
                <th>Depth</th>
                <th>Bet</th>
                <th>Profit</th>
                <th>Duration</th>
                <th>Type</th>
                <th>Result</th>
                <th>P&L</th>
            </tr>
        </thead>
        <tbody>
            {opps_html if opps_html else '<tr><td colspan="14" style="color:#555;text-align:center;padding:20px;">No opportunities detected yet...</td></tr>'}
        </tbody>
    </table>

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
        <a href="/api/report" style="color:#00aaff">/api/report</a> (full report for analysis)
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


def _esc(text: str) -> str:
    """Escape HTML special characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
