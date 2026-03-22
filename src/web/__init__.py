"""Web application factory with Jinja2 + session + auth middleware."""
from __future__ import annotations

import os
from pathlib import Path

import aiohttp_jinja2
import aiohttp_session
import jinja2
from aiohttp import web
from aiohttp_session.cookie_storage import EncryptedCookieStorage
from cryptography.fernet import Fernet

from src.db import init_db
from src.web.auth import routes as auth_routes
from src.web.middleware import auth_middleware
from src.web.routes_api import routes as api_routes
from src.web.routes_dashboard import routes as dashboard_routes
from src.web.routes_panel import routes as panel_routes


def create_app(bot) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["bot"] = bot

    # Session setup — key from env or generate ephemeral
    secret_key = os.environ.get("SESSION_SECRET")
    if secret_key:
        key = secret_key.encode()[:32].ljust(32, b"\0")
    else:
        key = Fernet.generate_key()[:32]
    aiohttp_session.setup(app, EncryptedCookieStorage(key))

    # Jinja2 templates
    templates_dir = Path(__file__).parent.parent / "templates"
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(str(templates_dir)))

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
