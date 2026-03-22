"""Lightweight HMAC-signed cookie session — no cryptography dependency."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

SESSION_COOKIE = "s"
SESSION_MAX_AGE = 86400 * 7  # 7 days

_secret: bytes = b""


def init_session_secret():
    global _secret
    env = os.environ.get("SESSION_SECRET", "")
    _secret = env.encode() if env else os.urandom(32)


def create_session_cookie(data: dict) -> str:
    """Create an HMAC-signed cookie value."""
    data["_t"] = int(time.time())
    payload = base64.urlsafe_b64encode(json.dumps(data).encode())
    sig = hmac.new(_secret, payload, hashlib.sha256).hexdigest()
    return payload.decode() + "." + sig


def read_session_cookie(cookie_value: str) -> dict | None:
    """Verify and decode an HMAC-signed cookie. Returns None if invalid."""
    try:
        payload_str, sig = cookie_value.rsplit(".", 1)
        expected = hmac.new(_secret, payload_str.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload_str))
        # Check expiry
        if time.time() - data.get("_t", 0) > SESSION_MAX_AGE:
            return None
        return data
    except Exception:
        return None
