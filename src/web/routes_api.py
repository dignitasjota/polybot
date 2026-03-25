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
    """Diagnostic: test order signing with different configs."""
    import os
    import traceback
    import importlib.metadata
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
    from py_clob_client.clob_types import OrderArgs
    from py_clob_client.order_builder.constants import BUY

    pk = os.environ.get("COPY_PRIVATE_KEY", "") or os.environ.get("PRIVATE_KEY", "")
    if not pk:
        return web.json_response({"error": "no private key configured"}, status=400)

    try:
        version = importlib.metadata.version("py-clob-client")
    except Exception:
        version = "unknown"

    # Get a real token_id from the bot's copy_trader data
    bot = request.app["bot"]
    test_token_id = ""
    for acc in bot.accounts:
        opps = acc.export_opportunities()
        for o in opps:
            if o.get("token_id"):
                test_token_id = o["token_id"]
                break
        if test_token_id:
            break

    results = {"py_clob_client_version": version, "test_token_id": test_token_id[:20] + "..." if test_token_id else "none"}

    if not test_token_id:
        results["error"] = "No token_id found from bot data to test with"
        return web.json_response(results)

    # Test sig_type=1 (the one with balance) with and without funder
    sig_type = 1
    try:
        c = ClobClient("https://clob.polymarket.com", key=pk, chain_id=137, signature_type=sig_type)
        addr = c.get_address()
        creds = c.derive_api_key()
        results["address"] = addr

        for test_name, funder_val in [("without_funder", None), ("with_funder", addr)]:
            try:
                kwargs = {"host": "https://clob.polymarket.com", "key": pk, "chain_id": 137,
                          "signature_type": sig_type,
                          "creds": ApiCreds(api_key=creds.api_key, api_secret=creds.api_secret,
                                           api_passphrase=creds.api_passphrase)}
                if funder_val:
                    kwargs["funder"] = funder_val
                client = ClobClient(**kwargs)

                # Step 1: create_order (signing)
                order_args = OrderArgs(price=0.10, size=1.0, side=BUY, token_id=test_token_id)
                try:
                    signed = client.create_order(order_args)
                    results[test_name] = {"create_order": "OK", "signed_keys": list(signed.keys()) if isinstance(signed, dict) else str(type(signed))}
                except Exception as e:
                    results[test_name] = {"create_order": f"FAILED: {e}", "traceback": traceback.format_exc()[-500:]}
                    continue

                # Step 2: post_order
                try:
                    resp = client.post_order(signed)
                    results[test_name]["post_order"] = f"OK: {resp}"
                except Exception as e:
                    err = str(e)
                    results[test_name]["post_order"] = err
                    results[test_name]["is_sig_error"] = "invalid signature" in err.lower()

            except Exception as e:
                results[test_name] = {"error": str(e), "traceback": traceback.format_exc()[-500:]}

    except Exception as e:
        results["init_error"] = str(e)

    return web.json_response(results)
