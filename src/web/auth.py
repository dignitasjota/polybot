"""Login/logout handlers."""
from aiohttp import web
import aiohttp_jinja2

from src.db import verify_password, add_audit
from src.web.session import create_session_cookie, SESSION_COOKIE

routes = web.RouteTableDef()


@routes.get("/login")
@aiohttp_jinja2.template("login.html")
async def login_page(request: web.Request):
    return {"error": None}


@routes.post("/login")
async def login_submit(request: web.Request):
    data = await request.post()
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if await verify_password(username, password):
        await add_audit(username, "login", "Login successful")
        cookie = create_session_cookie({"user": username})
        resp = web.HTTPFound("/")
        resp.set_cookie(SESSION_COOKIE, cookie, httponly=True, max_age=86400 * 7)
        raise resp

    context = {"error": "Invalid credentials"}
    return aiohttp_jinja2.render_template("login.html", request, context)


@routes.get("/logout")
async def logout(request: web.Request):
    session = request.get("session", {})
    user = session.get("user", "unknown")
    await add_audit(user, "logout", "")
    resp = web.HTTPFound("/login")
    resp.del_cookie(SESSION_COOKIE)
    raise resp
