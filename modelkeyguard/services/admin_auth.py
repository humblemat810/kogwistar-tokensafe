from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


ADMIN_COOKIE_NAME = "kgw_admin_session"
ADMIN_OIDC_COOKIE_NAME = "kgw_admin_oidc_session"
ADMIN_HEADER_NAME = "x-modelkeyguard-admin-secret"


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64ud(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def issue_admin_session(
    secret: str,
    ttl_seconds: int,
    *,
    source: str = "secret",
    extra: dict[str, Any] | None = None,
) -> tuple[str, int]:
    exp = int(time.time()) + max(1, int(ttl_seconds))
    payload: dict[str, Any] = {"exp": exp, "source": source}
    if extra:
        payload.update(extra)
    payload_b64 = _b64u(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_derive_admin_key(secret), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64u(sig)}", exp


def decode_admin_session(secret: str, token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    parts = token.split(".", 1)
    if len(parts) != 2:
        return None
    payload_b64, sig_b64 = parts
    try:
        expected = hmac.new(_derive_admin_key(secret), payload_b64.encode("utf-8"), hashlib.sha256).digest()
        provided = _b64ud(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected, provided):
        return None
    try:
        payload = json.loads(_b64ud(payload_b64).decode("utf-8"))
    except Exception:
        return None
    exp = int(payload.get("exp") or 0)
    if exp < int(time.time()):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def verify_admin_session(secret: str, token: str | None) -> bool:
    return decode_admin_session(secret, token) is not None


def is_admin_authenticated(request: Any, secret: str, *, allowed_sources: set[str] | None = None) -> bool:
    if _matches_admin_header(request, secret):
        return True
    allowed_sources = allowed_sources or {"secret", "oidc"}
    payload = decode_admin_session(secret, request.cookies.get(ADMIN_COOKIE_NAME))
    if not payload:
        return False
    source = str(payload.get("source") or "secret")
    return source in allowed_sources


def admin_html_login_response(next_path: str = "/admin/usage", oidc_login_url: str | None = None):
    from fastapi.responses import HTMLResponse

    safe_next = next_path if next_path.startswith("/admin/") else "/admin/usage"
    oidc_button = ""
    if oidc_login_url:
        oidc_button = (
            f"<p><a href='{oidc_login_url}'>Sign in with Keycloak</a></p>"
            "<p>Use this when your deployment is set up for browser OIDC login.</p>"
        )
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
  {oidc_button}
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
