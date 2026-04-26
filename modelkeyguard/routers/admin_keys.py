from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..core import ModelKey
from ..key_manager import KeyLifecycleError


def create_router(render_admin_html: Callable[[list[Any]], str]) -> APIRouter:
    router = APIRouter(tags=["admin-keys"])

    @router.get("/admin/keys")
    def admin_keys(request: Request):
        views = request.app.state.key_manager.list_key_views() if request.app.state.key_manager else []
        return HTMLResponse(render_admin_html(views))

    @router.get("/admin/keys.json")
    def admin_keys_json(request: Request):
        views = request.app.state.key_manager.list_key_views() if request.app.state.key_manager else []
        return {"data": [v.__dict__ for v in views]}

    @router.post("/admin/keys")
    async def admin_create_key(request: Request):
        form = await request.form()
        try:
            view = request.app.state.key_manager.create_key(
                key_id=str(form.get("key_id", "")),
                provider=str(form.get("provider", "")),
                models=[m.strip() for m in str(form.get("models", "")).split(",") if m.strip()],
                display_name=str(form.get("display_name", "")),
                provider_secret=str(form.get("provider_secret", "")),
                created_by="admin:web",
                expires_at_epoch=int(form["expires_at_epoch"]) if form.get("expires_at_epoch") else None,
            )
            request.app.state.guard.register_key(
                ModelKey(
                    id=view.key_id,
                    provider=view.provider,
                    models=view.models,
                    secret_ref=view.active_secret_ref,
                    display_name=view.display_name,
                )
            )
            request.app.state.guard.grant(
                key_id=view.key_id,
                mode="scope",
                created_by="admin:web",
                owner_id="admin:web",
                namespace="tenant:kogwistar",
            )
            return {"ok": True, "key_id": view.key_id, "secret_ref": view.active_secret_ref, "secret_value": None}
        except KeyLifecycleError as e:
            return JSONResponse(status_code=400, content={"error": {"message": str(e)}})

    async def _rotate(request: Request, key_id: str):
        form = await request.form()
        try:
            view = request.app.state.key_manager.rotate_key(
                key_id=key_id,
                provider_secret=str(form.get("provider_secret", "")),
                rotated_by="admin:web",
                expires_at_epoch=int(form["expires_at_epoch"]) if form.get("expires_at_epoch") else None,
            )
            if key_id in request.app.state.guard.keys:
                old = request.app.state.guard.keys[key_id]
                request.app.state.guard.register_key(
                    ModelKey(
                        id=old.id,
                        provider=old.provider,
                        models=old.models,
                        secret_ref=view.active_secret_ref,
                        display_name=old.display_name,
                    )
                )
            return {"ok": True, "key_id": view.key_id, "secret_ref": view.active_secret_ref, "secret_value": None}
        except KeyLifecycleError as e:
            return JSONResponse(status_code=400, content={"error": {"message": str(e)}})

    @router.post("/admin/keys/rotate")
    async def admin_rotate_key_form(request: Request):
        form = await request.form()
        return await _rotate(request, str(form.get("key_id", "")))

    @router.post("/admin/keys/{key_id:path}/rotate")
    async def admin_rotate_key(key_id: str, request: Request):
        return await _rotate(request, key_id)

    @router.post("/admin/keys/{key_id:path}/revoke")
    async def admin_revoke_key(key_id: str, request: Request):
        form = await request.form()
        try:
            view = request.app.state.key_manager.revoke_key(key_id=key_id, revoked_by="admin:web", reason=str(form.get("reason", "")))
            request.app.state.guard.keys.pop(key_id, None)
            return {"ok": True, "key_id": view.key_id, "status": view.status}
        except KeyLifecycleError as e:
            return JSONResponse(status_code=400, content={"error": {"message": str(e)}})

    return router
