from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..services.admin_oidc import admin_oidc_callback_response, admin_oidc_login_response


def create_router() -> APIRouter:
    router = APIRouter(tags=["admin-oidc"])

    @router.get("/admin/oidc/login")
    def admin_oidc_login(request: Request, next: str = "/admin/usage"):
        settings = request.app.state.settings
        return admin_oidc_login_response(
            request=request,
            secret=settings.admin_api_secret,
            next_path=next,
            keycloak_url=settings.keycloak_url,
            keycloak_public_url=settings.keycloak_public_url,
            keycloak_local_url=settings.keycloak_local_url,
            realm=settings.keycloak_realm,
            client_id=settings.browser_oidc_client_id,
            gateway_public_url=settings.gateway_public_url,
            ttl_seconds=int(settings.admin_session_ttl_seconds),
        )

    @router.get("/admin/oidc/callback")
    def admin_oidc_callback(request: Request, code: str = "", state: str = "", next: str = "/admin/usage"):
        settings = request.app.state.settings
        response = admin_oidc_callback_response(
            request=request,
            secret=settings.admin_api_secret,
            next_path=next,
            code=code,
            state=state,
            keycloak_url=settings.keycloak_url,
            realm=settings.keycloak_realm,
            client_id=settings.browser_oidc_client_id,
            required_role=settings.admin_required_role,
            ttl_seconds=int(settings.admin_session_ttl_seconds),
        )
        if isinstance(response, JSONResponse) and response.status_code >= 400:
            return response
        return response

    return router
