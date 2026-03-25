"""JSON API routes — migrated from src/web.py."""
from __future__ import annotations

import time

from aiohttp import web

routes = web.RouteTableDef()


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


@routes.get("/api/diag/wallet")
async def handle_wallet_diag(request: web.Request) -> web.Response:
    """Diagnostic: test all signature types to find the correct one."""
    import os
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

    pk = os.environ.get("COPY_PRIVATE_KEY", "") or os.environ.get("PRIVATE_KEY", "")
    if not pk:
        return web.json_response({"error": "no private key configured"}, status=400)

    results = {}
    for sig_type in [0, 1, 2]:
        label = {0: "EOA (MetaMask)", 1: "POLY_PROXY (Magic Link)", 2: "POLY_GNOSIS_SAFE"}[sig_type]
        try:
            c = ClobClient("https://clob.polymarket.com", key=pk, chain_id=137, signature_type=sig_type)
            creds = c.derive_api_key()
            c2 = ClobClient(
                "https://clob.polymarket.com", key=pk, chain_id=137, signature_type=sig_type,
                creds=ApiCreds(api_key=creds.api_key, api_secret=creds.api_secret, api_passphrase=creds.api_passphrase),
            )
            resp = c2.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            raw = float(resp.get("balance", 0))
            balance = raw / 1e6 if raw > 1000 else raw
            results[f"sig_{sig_type}"] = {
                "label": label,
                "balance_usd": round(balance, 2),
                "balance_raw": raw,
                "address": c.get_address(),
                "status": "OK",
            }
        except Exception as e:
            results[f"sig_{sig_type}"] = {"label": label, "status": "ERROR", "error": str(e)}

    return web.json_response(results)
