"""Auth middleware — redirects unauthenticated requests to /login."""
from aiohttp import web

from src.web.session import read_session_cookie, SESSION_COOKIE


@web.middleware
async def auth_middleware(request: web.Request, handler):
    path = request.path
    if path == "/login" or path.startswith("/static/") or path == "/api/health":
        return await handler(request)

    cookie = request.cookies.get(SESSION_COOKIE, "")
    session = read_session_cookie(cookie) if cookie else None

    if not session or not session.get("user"):
        raise web.HTTPFound("/login")

    request["session"] = session
    return await handler(request)
