"""Auth middleware — redirects unauthenticated requests to /login."""
from aiohttp import web
from aiohttp_session import get_session

# Paths that don't require authentication
PUBLIC_PATHS = {"/login", "/static"}


@web.middleware
async def auth_middleware(request: web.Request, handler):
    path = request.path
    # Allow public paths
    if path == "/login" or path.startswith("/static/"):
        return await handler(request)

    session = await get_session(request)
    if not session.get("user"):
        raise web.HTTPFound("/login")

    return await handler(request)
