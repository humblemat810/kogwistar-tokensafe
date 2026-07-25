from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..reviewer_agent import advance_review_checkpoint, compute_review_status


def create_router() -> APIRouter:
    router = APIRouter(tags=["admin-review"])

    @router.get("/admin/review/status.json")
    def admin_review_status(request: Request):
        status = compute_review_status(request.app.state.guard.graph_state, request.app.state.policy)
        return JSONResponse(status_code=200, content=status)

    @router.post("/admin/review/checkpoint")
    async def admin_review_checkpoint(request: Request):
        payload = await _parse_payload(request)
        reviewed_by = str(payload.get("reviewed_by") or "reviewer-agent")
        review_summary = str(payload.get("review_summary") or "")
        checkpoint = advance_review_checkpoint(
            request.app.state.guard.graph_state,
            request.app.state.policy,
            reviewed_by=reviewed_by,
            review_summary=review_summary,
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
