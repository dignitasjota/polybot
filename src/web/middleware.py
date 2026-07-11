"""Auth + CSRF middleware.

- Redirects unauthenticated requests to /login.
- Rejects mutating requests (POST/PUT/PATCH/DELETE) whose CSRF token doesn't
  match the one stored in the signed session cookie (synchronizer-token
  pattern). Combined with the cookie's SameSite=Strict, this closes the CSRF
  hole that let a malicious page flip the bot to live, raise bet sizes, or
  disable the kill switch on behalf of a logged-in operator.
"""
import hmac

from aiohttp import web

from src.web.session import read_session_cookie, SESSION_COOKIE

_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Paths exempt from auth (and, where mutating, from CSRF): the login form has
# no prior session to carry a token, and health is a public liveness probe.
_PUBLIC = ("/login", "/api/health")


def _is_public(path: str) -> bool:
    return path in _PUBLIC or path.startswith("/static/")


async def _csrf_ok(request: web.Request, session: dict) -> bool:
    expected = session.get("csrf", "")
    if not expected:
        return False
    # htmx sends it as a header (inherited hx-headers); plain forms as a field.
    sent = request.headers.get("X-CSRF-Token", "")
    if not sent:
        try:
            data = await request.post()
            sent = data.get("csrf", "")
        except Exception:
            sent = ""
    return bool(sent) and hmac.compare_digest(str(sent), str(expected))


@web.middleware
async def auth_middleware(request: web.Request, handler):
    path = request.path
    if _is_public(path):
        return await handler(request)

    cookie = request.cookies.get(SESSION_COOKIE, "")
    session = read_session_cookie(cookie) if cookie else None

    if not session or not session.get("user"):
        raise web.HTTPFound("/login")

    request["session"] = session

    # CSRF check on state-changing requests.
    if request.method in _MUTATING and not await _csrf_ok(request, session):
        raise web.HTTPForbidden(reason="CSRF token missing or invalid")

    return await handler(request)
