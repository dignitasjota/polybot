"""Login/logout handlers."""
from aiohttp import web
from aiohttp_session import get_session
import aiohttp_jinja2

from src.db import verify_password, add_audit

routes = web.RouteTableDef()


@routes.get("/login")
@aiohttp_jinja2.template("login.html")
async def login_page(request: web.Request):
    session = await get_session(request)
    if session.get("user"):
        raise web.HTTPFound("/")
    return {"error": None}


@routes.post("/login")
async def login_submit(request: web.Request):
    data = await request.post()
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if await verify_password(username, password):
        session = await get_session(request)
        session["user"] = username
        await add_audit(username, "login", "Login successful")
        raise web.HTTPFound("/")

    context = {"error": "Invalid credentials"}
    return aiohttp_jinja2.render_template("login.html", request, context)


@routes.get("/logout")
async def logout(request: web.Request):
    session = await get_session(request)
    user = session.get("user", "unknown")
    await add_audit(user, "logout", "")
    session.invalidate()
    raise web.HTTPFound("/login")
