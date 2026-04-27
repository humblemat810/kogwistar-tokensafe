from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..services.history_ops import (
    get_history_config,
    get_history_detail,
    list_history,
    update_history_config,
)
from ..services.ui_pages import render_admin_history_page


def create_router() -> APIRouter:
    router = APIRouter(tags=["admin-history"])

    @router.get("/admin/history")
    def admin_history_page():
        return HTMLResponse(render_admin_history_page())

    @router.get("/admin/history.json")
    def admin_history_json(
        request: Request,
        subject_type: str | None = None,
        subject_id: str | None = None,
        principal_id: str | None = None,
        user_id: str | None = None,
        key_id: str | None = None,
        token_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        route_family: str | None = None,
        decision: str | None = None,
        http_status: int | None = None,
        request_id: str | None = None,
        time_range: str = "24h",
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
    ):
        filters = {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "principal_id": principal_id,
            "user_id": user_id,
            "key_id": key_id,
            "token_id": token_id,
            "provider": provider,
            "model": model,
            "route_family": route_family,
            "decision": decision,
            "http_status": http_status,
            "request_id": request_id,
            "time_range": time_range,
        }
        result = list_history(
            request.app.state.guard.graph_state,
            request.app.state.settings,
            filters=filters,
            page=page,
            page_size=page_size,
        )
        return JSONResponse(status_code=200, content=result)

    @router.get("/admin/history/{request_id}.json")
    def admin_history_detail(request_id: str, request: Request):
        detail = get_history_detail(request.app.state.guard.graph_state, request.app.state.settings, request_id)
        if not detail:
            return JSONResponse(status_code=404, content={"error": {"message": "history_record_not_found"}})
        return JSONResponse(status_code=200, content=detail)

    @router.get("/admin/history/config")
    def admin_history_config(request: Request):
        cfg = get_history_config(request.app.state.guard.graph_state, request.app.state.settings)
        return JSONResponse(status_code=200, content=cfg)

    @router.post("/admin/history/config")
    async def admin_history_config_update(request: Request):
        payload = await _parse_payload(request)
        cfg = update_history_config(request.app.state.guard.graph_state, request.app.state.settings, payload)
        return JSONResponse(status_code=200, content=cfg)

    return router


async def _parse_payload(request: Request) -> dict[str, Any]:
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            payload = await request.json()
            if isinstance(payload, dict):
                return payload
        except Exception:
            return {}
        return {}
    try:
        form = await request.form()
    except Exception:
        return {}
    payload = {k: v for k, v in form.items()}
    if "enabled" in payload:
        payload["enabled"] = str(payload["enabled"]).lower() in {"1", "true", "yes", "on"}
    return payload
