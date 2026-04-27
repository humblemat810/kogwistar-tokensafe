from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


ADMIN_COOKIE_NAME = "kgw_admin_session"
ADMIN_HEADER_NAME = "x-modelkeyguard-admin-secret"


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64ud(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def issue_admin_session(secret: str, ttl_seconds: int) -> tuple[str, int]:
    exp = int(time.time()) + max(1, int(ttl_seconds))
    payload = {"exp": exp}
    payload_b64 = _b64u(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_derive_admin_key(secret), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64u(sig)}", exp


def verify_admin_session(secret: str, token: str | None) -> bool:
    if not token:
        return False
    parts = token.split(".", 1)
    if len(parts) != 2:
        return False
    payload_b64, sig_b64 = parts
    try:
        expected = hmac.new(_derive_admin_key(secret), payload_b64.encode("utf-8"), hashlib.sha256).digest()
        provided = _b64ud(sig_b64)
    except Exception:
        return False
    if not hmac.compare_digest(expected, provided):
        return False
    try:
        payload = json.loads(_b64ud(payload_b64).decode("utf-8"))
    except Exception:
        return False
    exp = int(payload.get("exp") or 0)
    return exp >= int(time.time())


def is_admin_authenticated(request: Any, secret: str) -> bool:
    if _matches_admin_header(request, secret):
        return True
    return verify_admin_session(secret, request.cookies.get(ADMIN_COOKIE_NAME))


def admin_html_login_response(next_path: str = "/admin/usage"):
    from fastapi.responses import HTMLResponse

    safe_next = next_path if next_path.startswith("/admin/") else "/admin/usage"
    html = f"""<!doctype html>
<html><head><meta charset='utf-8'><title>ModelKeyGuard Admin Login</title></head>
<body style='font-family:system-ui,sans-serif;margin:2rem;'>
  <h1>Admin Login Required</h1>
  <p>Provide admin secret to open protected admin pages.</p>
  <form method='post' action='/admin/session'>
    <input type='password' name='secret' placeholder='admin secret' autocomplete='off' required>
    <input type='hidden' name='next' value='{safe_next}'>
    <button>Sign in</button>
  </form>
</body></html>"""
    return HTMLResponse(content=html, status_code=401)


def _matches_admin_header(request: Any, secret: str) -> bool:
    header_val = request.headers.get(ADMIN_HEADER_NAME, "")
    if not header_val:
        return False
    return hmac.compare_digest(header_val, secret)


def _derive_admin_key(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()


def extract_admin_secret_from_payload(payload: dict[str, Any]) -> str:
    return str(payload.get("secret") or "").strip()
