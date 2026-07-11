"""Login/logout handlers."""
import os
import time

from aiohttp import web
import aiohttp_jinja2
import structlog

from src.db import verify_password, add_audit
from src.web.session import create_session_cookie, new_csrf_token, SESSION_COOKIE

logger = structlog.get_logger("polymarket.web.auth")

routes = web.RouteTableDef()

# ── Login rate limiting (in-memory, per client IP) ───────────────────
# Brute-force defense: after LOGIN_MAX_FAILS failed attempts within
# LOGIN_WINDOW_S, block that IP for LOGIN_BLOCK_S. Cleared on success.
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW_S = 300      # 5 min sliding window
LOGIN_BLOCK_S = 900      # 15 min lockout
_login_fails: dict[str, list[float]] = {}   # ip -> [failed attempt timestamps]
_login_blocked: dict[str, float] = {}       # ip -> blocked_until timestamp

# Set the Secure flag on the session cookie only when served over HTTPS
# (behind a TLS-terminating proxy). Defaults off so the current plain-HTTP
# deploy doesn't silently break login. SameSite=Strict is the CSRF defense
# that works without TLS.
COOKIE_SECURE = os.environ.get("PANEL_HTTPS", "").lower() in ("1", "true", "yes")


def _client_ip(request: web.Request) -> str:
    # Trust the proxy's forwarded IP if present (panel typically runs behind one).
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    peer = request.transport.get_extra_info("peername") if request.transport else None
    return peer[0] if peer else "unknown"


def _is_blocked(ip: str) -> bool:
    until = _login_blocked.get(ip, 0)
    if until and time.time() < until:
        return True
    if until:
        _login_blocked.pop(ip, None)
    return False


def _record_fail(ip: str) -> None:
    now = time.time()
    fails = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_WINDOW_S]
    fails.append(now)
    _login_fails[ip] = fails
    if len(fails) >= LOGIN_MAX_FAILS:
        _login_blocked[ip] = now + LOGIN_BLOCK_S
        _login_fails.pop(ip, None)


def _clear_fails(ip: str) -> None:
    _login_fails.pop(ip, None)
    _login_blocked.pop(ip, None)


def _set_session_cookie(resp: web.Response, username: str) -> None:
    cookie = create_session_cookie({"user": username, "csrf": new_csrf_token()})
    resp.set_cookie(
        SESSION_COOKIE, cookie,
        httponly=True, secure=COOKIE_SECURE, samesite="Strict",
        max_age=86400 * 7,
    )


@routes.get("/login")
@aiohttp_jinja2.template("login.html")
async def login_page(request: web.Request):
    return {"error": None}


@routes.post("/login")
async def login_submit(request: web.Request):
    ip = _client_ip(request)
    if _is_blocked(ip):
        logger.warning("login_rate_limited", ip=ip)
        return aiohttp_jinja2.render_template(
            "login.html", request,
            {"error": "Too many attempts. Try again later."}, status=429,
        )

    data = await request.post()
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if await verify_password(username, password):
        _clear_fails(ip)
        await add_audit(username, "login", f"Login successful from {ip}")
        resp = web.HTTPFound("/")
        _set_session_cookie(resp, username)
        raise resp

    _record_fail(ip)
    # Audit failed attempts (previously invisible → brute force left no trace).
    await add_audit(username or "?", "login_failed", f"Failed login from {ip}")
    logger.warning("login_failed", ip=ip, username=username[:32])
    return aiohttp_jinja2.render_template(
        "login.html", request, {"error": "Invalid credentials"}, status=401,
    )


@routes.get("/logout")
async def logout(request: web.Request):
    session = request.get("session", {})
    user = session.get("user", "unknown")
    await add_audit(user, "logout", "")
    resp = web.HTTPFound("/login")
    resp.del_cookie(SESSION_COOKIE)
    raise resp
