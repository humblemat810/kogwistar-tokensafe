from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..reviewer_agent import advance_review_checkpoint, compute_review_status, start_review_run
from ..services.ui_pages import render_admin_review_page


def create_router() -> APIRouter:
    router = APIRouter(tags=["admin-review"])

    @router.get("/admin/review")
    def admin_review_page():
        return HTMLResponse(render_admin_review_page())

    @router.get("/admin/review/status.json")
    def admin_review_status(request: Request):
        status = compute_review_status(request.app.state.guard.graph_state, request.app.state.policy)
        return JSONResponse(status_code=200, content=status)

    @router.post("/admin/review/start")
    async def admin_review_start(request: Request):
        payload = await _parse_payload(request)
        requested_by = str(payload.get("requested_by") or "reviewer-agent")
        run = start_review_run(
            request.app.state.guard.graph_state,
            request.app.state.policy,
            requested_by=requested_by,
        )
        return JSONResponse(status_code=201, content=run)

    @router.post("/admin/review/checkpoint")
    async def admin_review_checkpoint(request: Request):
        payload = await _parse_payload(request)
        reviewed_by = str(payload.get("reviewed_by") or "reviewer-agent")
        review_summary = str(payload.get("review_summary") or "")
        review_run_id = str(payload.get("review_run_id") or "").strip() or None
        # Never accept a caller-supplied status/window.  Recompute from the
        # authoritative graph so an authenticated caller cannot inject a
        # future cursor and suppress subsequent reviews.
        checkpoint = advance_review_checkpoint(
            request.app.state.guard.graph_state,
            request.app.state.policy,
            reviewed_by=reviewed_by,
            review_summary=review_summary,
            review_run_id=review_run_id,
        )
        return JSONResponse(status_code=200, content={"ok": True, "checkpoint": checkpoint})

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
    return {k: v for k, v in form.items()}
