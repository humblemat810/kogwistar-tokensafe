from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
import urllib.request
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .admin_auth import ADMIN_COOKIE_NAME, ADMIN_OIDC_COOKIE_NAME, admin_html_login_response, issue_admin_session


def build_oidc_login_url(*, keycloak_url: str, realm: str, client_id: str, redirect_uri: str, state: str, code_challenge: str, next_path: str) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": "openid",
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "kc_idp_hint": "",
    }
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v})
    base = f"{keycloak_url.rstrip('/')}/realms/{realm}/protocol/openid-connect/auth"
    if next_path:
        return f"{base}?{query}"
    return f"{base}?{query}"


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = _b64u(hashlib.sha256(verifier.encode("utf-8")).digest())
    return verifier, challenge


def issue_login_state(secret: str, *, ttl_seconds: int, next_path: str, redirect_uri: str, code_verifier: str) -> tuple[str, str]:
    state = secrets.token_urlsafe(24)
    payload = {
        "exp": int(time.time()) + max(1, int(ttl_seconds)),
        "next": next_path if next_path.startswith("/admin/") else "/admin/usage",
        "redirect_uri": redirect_uri,
        "state": state,
        "code_verifier": code_verifier,
    }
    return _sign_payload(secret, payload), state


def parse_login_state(secret: str, token: str | None) -> dict[str, Any] | None:
    payload = _verify_payload(secret, token)
    if not payload:
        return None
    return payload


def exchange_authorization_code(*, keycloak_url: str, realm: str, client_id: str, code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
    discovery = _fetch_json(f"{keycloak_url.rstrip('/')}/realms/{realm}/.well-known/openid-configuration")
    token_url = str(discovery["token_endpoint"])
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        token_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data


def decode_access_token_claims(access_token: str) -> dict[str, Any]:
    parts = access_token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


def admin_oidc_login_response(
    *,
    request: Request,
    secret: str,
    next_path: str,
    keycloak_url: str,
    realm: str,
    client_id: str,
    gateway_public_url: str,
    ttl_seconds: int,
) -> Response:
    redirect_uri = f"{gateway_public_url.rstrip('/')}/admin/oidc/callback"
    code_verifier, code_challenge = pkce_pair()
    state_cookie, state = issue_login_state(secret, ttl_seconds=ttl_seconds, next_path=next_path, redirect_uri=redirect_uri, code_verifier=code_verifier)
    login_url = build_oidc_login_url(
        keycloak_url=keycloak_url,
        realm=realm,
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=code_challenge,
        next_path=next_path,
    )
    response = RedirectResponse(url=login_url, status_code=303)
    response.set_cookie(
        ADMIN_OIDC_COOKIE_NAME,
        state_cookie,
        max_age=ttl_seconds,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


def admin_oidc_callback_response(
    *,
    request: Request,
    secret: str,
    next_path: str,
    code: str,
    state: str,
    keycloak_url: str,
    realm: str,
    client_id: str,
    ttl_seconds: int,
) -> Response:
    state_cookie = request.cookies.get(ADMIN_OIDC_COOKIE_NAME)
    state_payload = parse_login_state(secret, state_cookie)
    if not state_payload:
        return JSONResponse(status_code=401, content={"error": {"message": "invalid_oidc_login_state"}})
    if state_payload.get("state") != state:
        return JSONResponse(status_code=401, content={"error": {"message": "oidc_state_mismatch"}})
    redirect_uri = str(state_payload.get("redirect_uri") or "")
    code_verifier = str(state_payload.get("code_verifier") or "")
    if not redirect_uri or not code_verifier:
        return JSONResponse(status_code=400, content={"error": {"message": "invalid_oidc_login_state"}})
    payload = exchange_authorization_code(
        keycloak_url=keycloak_url,
        realm=realm,
        client_id=client_id,
        code=code,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
    )
    access_token = str(payload.get("access_token") or "")
    claims = decode_access_token_claims(access_token)
    if not _has_admin_role(claims):
        return JSONResponse(status_code=403, content={"error": {"message": "admin_role_required"}})
    session_cookie, _ = issue_admin_session(
        secret,
        ttl_seconds,
        source="oidc",
        extra={
            "sub": claims.get("sub", ""),
            "preferred_username": claims.get("preferred_username", ""),
            "iss": claims.get("iss", ""),
            "aud": claims.get("aud", ""),
        },
    )
    response: Response = RedirectResponse(url=next_path if next_path.startswith("/admin/") else "/admin/usage", status_code=303)
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        session_cookie,
        max_age=ttl_seconds,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(ADMIN_OIDC_COOKIE_NAME, path="/")
    return response


def render_admin_login_page(next_path: str, *, oidc_login_url: str | None = None) -> HTMLResponse:
    return admin_html_login_response(next_path, oidc_login_url=oidc_login_url)


def _fetch_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _has_admin_role(claims: dict[str, Any]) -> bool:
    realm_roles = claims.get("realm_access", {}).get("roles", [])
    if isinstance(realm_roles, list) and "model.admin" in [str(r) for r in realm_roles]:
        return True
    resource_access = claims.get("resource_access", {})
    if isinstance(resource_access, dict):
        for value in resource_access.values():
            roles = value.get("roles", []) if isinstance(value, dict) else []
            if "model.admin" in [str(r) for r in roles]:
                return True
    return False


def _sign_payload(secret: str, payload: dict[str, Any]) -> str:
    payload_b64 = _b64u(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_derive_key(secret), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64u(sig)}"


def _verify_payload(secret: str, token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    parts = token.split(".", 1)
    if len(parts) != 2:
        return None
    payload_b64, sig_b64 = parts
    try:
        expected = hmac.new(_derive_key(secret), payload_b64.encode("utf-8"), hashlib.sha256).digest()
        provided = _b64ud(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected, provided):
        return None
    try:
        payload = json.loads(_b64ud(payload_b64).decode("utf-8"))
    except Exception:
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    return payload if isinstance(payload, dict) else None


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64ud(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _derive_key(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()
