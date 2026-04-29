from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..services.admin_auth import (
    ADMIN_COOKIE_NAME,
    admin_html_login_response,
    extract_admin_secret_from_payload,
    issue_admin_session,
)


def create_router() -> APIRouter:
    router = APIRouter(tags=["admin-session"])

    @router.post("/admin/session")
    async def admin_login(request: Request):
        payload = await _payload_from_request(request)
        secret = extract_admin_secret_from_payload(payload)
        next_path = str(payload.get("next") or "/admin/usage")
        expected = request.app.state.settings.admin_api_secret

        if secret != expected:
            if _expects_html(request):
                return admin_html_login_response(next_path)
            return JSONResponse(status_code=401, content={"error": {"message": "invalid_admin_secret"}})

        ttl = int(request.app.state.settings.admin_session_ttl_seconds)
        token, exp = issue_admin_session(expected, ttl, source="secret")

        if _wants_redirect(request):
            response: Response = RedirectResponse(url=next_path if next_path.startswith("/admin/") else "/admin/usage", status_code=303)
        else:
            response = JSONResponse(status_code=200, content={"ok": True, "exp": exp})

        response.set_cookie(
            ADMIN_COOKIE_NAME,
            token,
            max_age=ttl,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @router.delete("/admin/session")
    def admin_logout():
        response = JSONResponse(status_code=200, content={"ok": True})
        response.delete_cookie(ADMIN_COOKIE_NAME, path="/")
        response.delete_cookie("kgw_admin_oidc_state", path="/")
        return response

    @router.get("/admin/session")
    def admin_login_page(next: str = "/admin/usage"):
        oidc_login_url = f"/admin/oidc/login?next={next}"
        return admin_html_login_response(next, oidc_login_url=oidc_login_url)

    return router


async def _payload_from_request(request: Request) -> dict[str, Any]:
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            body = await request.json()
            if isinstance(body, dict):
                return body
        except Exception:
            return {}
        return {}

    try:
        form = await request.form()
    except Exception:
        return {}
    return {k: v for k, v in form.items()}


def _expects_html(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    return "text/html" in accept


def _wants_redirect(request: Request) -> bool:
    content_type = (request.headers.get("content-type") or "").lower()
    return "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type
