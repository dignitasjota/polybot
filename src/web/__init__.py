"""Web application factory with Jinja2 + lightweight session + auth middleware."""
from __future__ import annotations

from pathlib import Path

import aiohttp_jinja2
import jinja2
from aiohttp import web

from src.db import init_db
from src.web.auth import routes as auth_routes
from src.web.middleware import auth_middleware
from src.web.routes_api import routes as api_routes
from src.web.routes_dashboard import routes as dashboard_routes
from src.web.routes_panel import routes as panel_routes
from src.web.session import init_session_secret


async def _csrf_context(request: web.Request) -> dict:
    """Expose the session's CSRF token to every template (forms + hx-headers)."""
    session = request.get("session", {})
    return {"csrf_token": session.get("csrf", "")}


def create_app(bot) -> web.Application:
    # auth_middleware runs first (outermost): it sets request["session"] before
    # context_processors_middleware reads it for _csrf_context.
    app = web.Application(
        middlewares=[auth_middleware, aiohttp_jinja2.context_processors_middleware]
    )
    app["bot"] = bot

    init_session_secret()

    # Jinja2 templates
    templates_dir = Path(__file__).parent.parent / "templates"
    # autoescape=True (M14): escape every {{ }} by default so any future
    # {{ user_or_api_field }} can't inject HTML. The two pre-built HTML blobs
    # (markets_html, opps_html) are already rendered with |safe; the htmx
    # fragments in routes_panel are built in Python and never pass through here.
    aiohttp_jinja2.setup(
        app,
        loader=jinja2.FileSystemLoader(str(templates_dir)),
        context_processors=[_csrf_context],
        autoescape=True,
    )

    # Static files
    static_dir = Path(__file__).parent.parent / "static"
    app.router.add_static("/static", str(static_dir), name="static")

    # Register routes
    app.router.add_routes(auth_routes)
    app.router.add_routes(dashboard_routes)
    app.router.add_routes(panel_routes)
    app.router.add_routes(api_routes)

    # Init DB on startup
    app.on_startup.append(_on_startup)

    return app


async def _on_startup(app: web.Application):
    await init_db()
