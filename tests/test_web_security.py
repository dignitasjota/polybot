"""Tests de C6: hardening del panel (CSRF, rate-limit, cookie flags, password).

Monta una app mínima con el auth_middleware real + el login real (DB temporal)
y una ruta POST protegida, para ejercer el flujo login → CSRF de punta a punta.
"""

import os
import tempfile
import importlib

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import aiohttp_jinja2
import jinja2
from pathlib import Path


@pytest_asyncio.fixture
async def client():
    # DB temporal aislada por test
    tmp = tempfile.mktemp(suffix=".db")
    os.environ["PANEL_DB_PATH"] = tmp
    os.environ["PANEL_PASSWORD"] = "testpass123"
    os.environ.pop("PANEL_HTTPS", None)

    # Recargar módulos que capturan env/DB_PATH en tiempo de import
    import src.db as db
    importlib.reload(db)
    import src.web.auth as auth
    importlib.reload(auth)
    import src.web.middleware as mw
    importlib.reload(mw)
    from src.web import _csrf_context
    from src.web.session import init_session_secret, read_session_cookie, SESSION_COOKIE

    app = web.Application(
        middlewares=[mw.auth_middleware, aiohttp_jinja2.context_processors_middleware]
    )
    init_session_secret()
    templates_dir = Path(__file__).parent.parent / "src" / "templates"
    aiohttp_jinja2.setup(
        app, loader=jinja2.FileSystemLoader(str(templates_dir)),
        context_processors=[_csrf_context],
    )
    app.add_routes(auth.routes)

    async def protected(request):
        return web.json_response({"ok": True})
    app.router.add_post("/test-protected", protected)
    app.router.add_get("/api/health", lambda r: web.json_response({"health": "ok"}))

    await db.init_db()

    cl = TestClient(TestServer(app))
    await cl.start_server()
    cl._session_reader = read_session_cookie
    cl._cookie_name = SESSION_COOKIE
    yield cl
    await cl.close()
    if os.path.exists(tmp):
        os.remove(tmp)


async def _login(client) -> str:
    """Log in and return the session CSRF token decoded from the cookie."""
    resp = await client.post(
        "/login", data={"username": "admin", "password": "testpass123"},
        allow_redirects=False,
    )
    assert resp.status == 302
    # Cookie de sesión seteada
    cookie = client.session.cookie_jar.filter_cookies(client.make_url("/"))
    raw = cookie.get(client._cookie_name)
    assert raw is not None
    session = client._session_reader(raw.value)
    assert session and session.get("user") == "admin"
    return session["csrf"]


# ── Auth ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unauthenticated_redirects_to_login(client):
    resp = await client.post("/test-protected", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/login"


@pytest.mark.asyncio
async def test_health_is_public(client):
    resp = await client.get("/api/health")
    assert resp.status == 200


@pytest.mark.asyncio
async def test_login_sets_samesite_strict_cookie(client):
    resp = await client.post(
        "/login", data={"username": "admin", "password": "testpass123"},
        allow_redirects=False,
    )
    assert resp.status == 302
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "SameSite=Strict" in set_cookie
    assert "HttpOnly" in set_cookie


# ── CSRF ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_post_without_csrf_is_forbidden(client):
    await _login(client)
    resp = await client.post("/test-protected")   # sin token
    assert resp.status == 403


@pytest.mark.asyncio
async def test_post_with_valid_csrf_header_passes(client):
    csrf = await _login(client)
    resp = await client.post("/test-protected", headers={"X-CSRF-Token": csrf})
    assert resp.status == 200


@pytest.mark.asyncio
async def test_post_with_valid_csrf_form_field_passes(client):
    csrf = await _login(client)
    resp = await client.post("/test-protected", data={"csrf": csrf})
    assert resp.status == 200


@pytest.mark.asyncio
async def test_post_with_wrong_csrf_is_forbidden(client):
    await _login(client)
    resp = await client.post("/test-protected", headers={"X-CSRF-Token": "wrong"})
    assert resp.status == 403


# ── Rate limiting ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_login_rate_limited_after_failures(client):
    # 5 intentos fallidos → el 6º devuelve 429
    for _ in range(5):
        r = await client.post(
            "/login", data={"username": "admin", "password": "bad"},
            allow_redirects=False,
        )
        assert r.status == 401
    r = await client.post(
        "/login", data={"username": "admin", "password": "testpass123"},
        allow_redirects=False,
    )
    assert r.status == 429   # bloqueado pese a credenciales correctas


# ── Password default ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_default_password_is_not_admin():
    """Sin PANEL_PASSWORD, el admin se crea con password aleatoria, no 'admin'."""
    tmp = tempfile.mktemp(suffix=".db")
    os.environ["PANEL_DB_PATH"] = tmp
    os.environ.pop("PANEL_PASSWORD", None)
    import src.db as db
    importlib.reload(db)
    await db.init_db()
    assert await db.verify_password("admin", "admin") is False
    if os.path.exists(tmp):
        os.remove(tmp)
