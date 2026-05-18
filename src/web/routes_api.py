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
    # Price checker snapshot: last check per crypto
    for acc in bot.accounts:
        det = acc.detector
        if det and hasattr(det, "_price_checker"):
            pc = det._price_checker
            prices = {}
            for sym, price in pc._current_prices.items():
                prices[sym] = round(price, 2)
            open_prices = {}
            for key, price in list(pc._open_prices.items())[-10:]:
                open_prices[key] = round(price, 2)
            data["price_checker"] = {
                "current_prices": prices,
                "open_prices_recent": open_prices,
                "active_symbols": list(pc._active_symbols),
                "pending_open_requests": len(pc._pending_open_requests),
            }
            break
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


@routes.get("/api/weather/debug/discovery")
async def handle_weather_debug_discovery(request: web.Request) -> web.Response:
    """Raw Gamma API query that weather discovery uses. Diagnoses why no markets are found."""
    import aiohttp
    base = "https://gamma-api.polymarket.com"
    results = {}
    # Test both deprecated /events and new /events/keyset to diagnose discovery
    queries = {
        "legacy_events_103040": (f"{base}/events", {
            "tag_id": "103040", "active": "true", "closed": "false", "limit": "20",
            "order": "startDate", "ascending": "false",
        }),
        "keyset_events_103040": (f"{base}/events/keyset", {
            "tag_id": "103040", "active": "true", "closed": "false", "limit": "20",
            "order": "startDate", "ascending": "false",
        }),
        "keyset_events_no_filters": (f"{base}/events/keyset", {
            "tag_id": "103040", "limit": "20",
        }),
    }
    async with aiohttp.ClientSession() as session:
        for name, (url, params) in queries.items():
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    status = resp.status
                    raw = await resp.json() if status == 200 else None
                    # New keyset returns {"events": [...], "next_cursor": "..."}; legacy returns plain list
                    if isinstance(raw, dict):
                        items = raw.get("events") or raw.get("data") or []
                        meta = {"next_cursor": raw.get("next_cursor")}
                    elif isinstance(raw, list):
                        items = raw
                        meta = {"response_type": "legacy_list"}
                    else:
                        items = []
                        meta = {"raw": str(raw)[:200]}
                    sample = []
                    for e in items[:5]:
                        sample.append({
                            "slug": e.get("slug"),
                            "title": (e.get("title") or "")[:80],
                            "active": e.get("active"),
                            "closed": e.get("closed"),
                            "start_date": e.get("startDate"),
                            "end_date": e.get("endDate"),
                            "tags": [t.get("slug") for t in (e.get("tags") or [])][:8],
                        })
                    results[name] = {"status": status, "count": len(items), "meta": meta, "sample": sample}
            except Exception as e:
                results[name] = {"error": str(e)}
    return web.json_response(results, dumps=lambda x: __import__('json').dumps(x, indent=2))


@routes.get("/api/weather/by_lead")
async def handle_weather_by_lead(request: web.Request) -> web.Response:
    """Breakdown of resolved weather trades grouped by forecast horizon (lead_days)."""
    bot = request.app["bot"]
    for acc in bot.accounts:
        if "weather" not in acc.strategies:
            continue
        scanner = acc.strategies["weather"].scanner
        by_lead: dict[int, dict] = {}
        for t in scanner._trades:
            if t.status not in ("won", "lost"):
                continue
            ld = t.lead_days
            bucket = by_lead.setdefault(ld, {"n": 0, "wins": 0, "pnl": 0.0, "cost": 0.0})
            bucket["n"] += 1
            bucket["wins"] += 1 if t.status == "won" else 0
            bucket["pnl"] += t.pnl
            bucket["cost"] += t.cost
        result = []
        for ld in sorted(by_lead.keys()):
            b = by_lead[ld]
            result.append({
                "lead_days": ld,
                "n": b["n"],
                "wins": b["wins"],
                "win_rate": round(b["wins"] / b["n"], 3) if b["n"] else 0,
                "pnl": round(b["pnl"], 2),
                "pnl_per_trade": round(b["pnl"] / b["n"], 2) if b["n"] else 0,
                "roi": round(b["pnl"] / b["cost"], 3) if b["cost"] > 0 else 0,
            })
        return web.json_response({"by_lead": result, "total_resolved": sum(b["n"] for b in by_lead.values())})
    return web.json_response({"error": "no weather account"}, status=404)


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


@routes.get("/api/rewards/markets")
async def handle_reward_markets(request: web.Request) -> web.Response:
    """Get reward markets ranked by profitability."""
    bot = request.app["bot"]
    scanner = None

    # Find scanner from liquidity strategy
    for acc in bot.accounts:
        for strat_name, strat in acc.strategies.items():
            if strat_name == "liquidity" and hasattr(strat, "scanner"):
                scanner = strat.scanner
                break
        if scanner:
            break

    # Fallback: standalone scanner
    if not scanner:
        scanner = request.app.get("reward_scanner")

    if not scanner:
        return web.json_response({"error": "no reward scanner active", "markets": []})

    limit = int(request.query.get("limit", "50"))
    report = scanner.export_report()
    report["exported_at"] = time.time()
    report["markets"] = report["markets"][:limit]
    return web.json_response(report)


@routes.get("/api/rewards/metrics")
async def handle_reward_metrics(request: web.Request) -> web.Response:
    """Get liquidity provider daily metrics and summary."""
    bot = request.app["bot"]

    for acc in bot.accounts:
        for strat_name, strat in acc.strategies.items():
            if strat_name == "liquidity" and hasattr(strat, "metrics"):
                days = int(request.query.get("days", "7"))
                return web.json_response({
                    "today": strat.metrics.get_today(),
                    "history": strat.metrics.get_history(days),
                    "summary": strat.metrics.get_summary(days),
                })

    return web.json_response({"error": "no liquidity strategy active"})
