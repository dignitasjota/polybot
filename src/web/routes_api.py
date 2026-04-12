"""JSON API routes — migrated from src/web.py."""
from __future__ import annotations

import time

from aiohttp import web

routes = web.RouteTableDef()


@routes.get("/api/health")
async def handle_health(request: web.Request) -> web.Response:
    """Diagnostic endpoint (no auth) for container-level debugging."""
    bot = request.app["bot"]
    data = {"ts": time.time(), "accounts": []}
    for acc in bot.accounts:
        stats = acc.get_stats()
        data["accounts"].append({
            "name": acc.name,
            "detector": stats.get("detector", {}),
            "executor": stats.get("executor", {}),
        })
    return web.json_response(data)


@routes.get("/api/opportunities")
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


@routes.get("/api/report")
async def handle_report(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    reports = {}
    for acc in bot.accounts:
        report = acc.export_full_report()
        if report:
            reports[acc.name] = report
    return web.json_response(reports)


@routes.get("/api/report/{account}")
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


@routes.get("/api/scanner/top-traders")
async def handle_top_traders(request: web.Request) -> web.Response:
    """Get top profitable traders for copy-trading."""
    bot = request.app["bot"]

    # Find a copy_trade account with scanner
    copy_account = None
    for acc in bot.accounts:
        if acc.strategy_type == "copy_trade" and acc.wallet_scanner:
            copy_account = acc
            break

    if not copy_account:
        return web.json_response({"error": "no copy_trade account", "traders": []})

    # Get min_trades and min_wr from query params
    min_trades = int(request.query.get("min_trades", "10"))
    min_wr = float(request.query.get("min_wr", "0.50"))

    try:
        traders = await copy_account.wallet_scanner.get_top_traders(
            min_trades=min_trades,
            min_wr=min_wr,
        )

        # Format for JSON
        formatted = []
        for t in traders:
            formatted.append({
                "wallet": t["wallet"],
                "trades": t["total_trades"],
                "wins": t["total_wins"],
                "losses": t["total_losses"],
                "win_rate": round(t["win_rate"] * 100, 1),
                "avg_price": round(t["avg_price"], 2),
                "volume": round(t["total_volume"], 2),
                "coins": t["coins_traded"] or "",
            })

        return web.json_response({
            "exported_at": time.time(),
            "criteria": {
                "min_trades": min_trades,
                "min_win_rate": f"{min_wr*100:.0f}%",
                "market_type": "crypto_5min",
                "price_range": "$0.40-$0.75",
            },
            "traders": formatted,
            "total_found": len(formatted),
        })
    except Exception as e:
        return web.json_response({
            "error": str(e),
            "traders": [],
        }, status=500)


@routes.get("/api/scanner/stats")
async def handle_scanner_stats(request: web.Request) -> web.Response:
    """Get scanner statistics and report."""
    bot = request.app["bot"]

    # Find a copy_trade account with scanner
    copy_account = None
    for acc in bot.accounts:
        if acc.strategy_type == "copy_trade" and acc.wallet_scanner:
            copy_account = acc
            break

    if not copy_account:
        return web.json_response({"error": "no copy_trade account"}, status=404)

    try:
        report = await copy_account.wallet_scanner.export_report()
        report["exported_at"] = time.time()
        return web.json_response(report)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
