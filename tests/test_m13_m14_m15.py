"""Tests de M13 (TOML atómico), M14 (autoescape), M15 (parseo de balance)."""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ── M15: parseo determinista del balance ─────────────────────────────

from src.executor import _usdc_from_raw


def test_usdc_from_raw_smallest_units():
    assert _usdc_from_raw(1_000_000) == pytest.approx(1.0)      # $1.00
    assert _usdc_from_raw(500_000_000) == pytest.approx(500.0)  # $500
    assert _usdc_from_raw(0) == pytest.approx(0.0)


def test_usdc_from_raw_large_balance_not_misread():
    """Un balance de $1500 (1.5e9 raw) → $1500, no ~$0.0015 (el bug M15)."""
    assert _usdc_from_raw(1_500_000_000) == pytest.approx(1500.0)


def test_usdc_from_raw_sub_dollar():
    # $0.50 = 500_000 raw. La vieja heurística (>1000) lo dejaba en 500_000.
    assert _usdc_from_raw(500_000) == pytest.approx(0.5)


# ── M14: autoescape en el Environment de Jinja2 ──────────────────────

@pytest.mark.asyncio
async def test_jinja_autoescape_enabled():
    import aiohttp_jinja2
    tmp = tempfile.mktemp(suffix=".db")
    os.environ["PANEL_DB_PATH"] = tmp
    from src.web import create_app
    app = create_app(bot=object())
    env = aiohttp_jinja2.get_env(app)
    assert env.autoescape is True


def test_template_escapes_html():
    """Con autoescape, {{ x }} escapa HTML; |safe lo deja crudo."""
    import jinja2
    env = jinja2.Environment(autoescape=True)
    assert env.from_string("{{ x }}").render(x="<script>") == "&lt;script&gt;"
    assert env.from_string("{{ x|safe }}").render(x="<b>ok</b>") == "<b>ok</b>"


# ── M13: escritura atómica del TOML ──────────────────────────────────

@pytest.fixture
def config_manager():
    """ConfigManager sobre una copia temporal del config.toml real."""
    from src.config import Config
    from src.web.config_manager import ConfigManager

    src_toml = Path("config/config.toml")
    tmpdir = tempfile.mkdtemp()
    tmp_toml = Path(tmpdir) / "config.toml"
    shutil.copy(src_toml, tmp_toml)
    cfg = Config.load(tmp_toml)
    bot = type("Bot", (), {"config": cfg, "accounts": []})()
    cm = ConfigManager(bot)
    yield cm, tmp_toml
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_persist_writes_valid_toml(config_manager):
    import sys
    tomllib = sys.modules.get("tomllib") or __import__("tomllib")
    cm, path = config_manager
    cm._persist()
    # El archivo sigue siendo TOML válido tras la escritura atómica.
    with open(path, "rb") as f:
        data = tomllib.load(f)
    assert "accounts" in data


def test_persist_failure_does_not_corrupt(config_manager):
    """Si la serialización falla a mitad, el config.toml original queda intacto."""
    cm, path = config_manager
    original = path.read_bytes()

    with patch("src.web.config_manager.tomli_w.dump", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            cm._persist()

    # El original no fue truncado ni modificado (se escribía a un temp).
    assert path.read_bytes() == original
    # No quedan temp files huérfanos en el directorio.
    leftovers = [p for p in path.parent.iterdir() if p.name.startswith(".config-")]
    assert leftovers == []
