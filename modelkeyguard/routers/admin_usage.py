from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..review_worker import review_once
from ..reviewer_agent import compute_review_status
from ..services.ui_pages import render_admin_usage_page
from ..services.usage_ops import build_usage_monitor_dataset, load_usage_events


def create_router() -> APIRouter:
    router = APIRouter(tags=["admin-usage"])

    @router.get("/admin/usage")
    def admin_usage_page():
        return HTMLResponse(render_admin_usage_page())

    @router.get("/admin/usage.json")
    def admin_usage_data(
        request: Request,
        subject_type: str | None = None,
        subject_id: str | None = None,
        time_range: str = "24h",
        bucket: str = "hour",
    ):
        audit_path = request.app.state.settings.audit_path
        events = load_usage_events(audit_path)
        result = build_usage_monitor_dataset(
            events,
            request.app.state.policy,
            subject_type=subject_type,
            subject_id=subject_id,
            time_range=time_range,
            bucket=bucket,
        )
        security_nodes = []
        if request.app.state.guard.graph_state:
            security_nodes = [
                {"id": n.id, **n.payload}
                for n in request.app.state.guard.graph_state.nodes.values()
                if n.kind == "admin_security_event"
            ]
        result["security_events"] = security_nodes[-50:]
        return result

    @router.post("/admin/review/run")
    async def admin_run_review(request: Request):
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}

        if request.app.state.settings.review_pipeline == "graph":
            status = compute_review_status(request.app.state.guard.graph_state, request.app.state.policy)
            return JSONResponse(status_code=200, content={"pipeline": "graph", "status": status, "automatic_retry": False})

        sample_size = int(payload.get("sample_size") or os.getenv("MODELKEYGUARD_REVIEW_SAMPLE_SIZE", "200"))
        run_llm_review = bool(
            payload.get("run_llm_review")
            if "run_llm_review" in payload
            else os.getenv("MODELKEYGUARD_REVIEW_RUN_LLM", "1") != "0"
        )
        lookback_minutes = payload.get("lookback_minutes")
        if lookback_minutes is None:
            lookback_minutes = os.getenv("MODELKEYGUARD_REVIEW_LOOKBACK_MINUTES")
        checkpoint = payload.get("checkpoint_path") or os.getenv("MODELKEYGUARD_REVIEW_CHECKPOINT_PATH")

        out_path = Path(payload.get("out_path") or os.getenv("MODELKEYGUARD_REVIEW_OUT", "out/review_results.jsonl"))
        result = review_once(
            Path(request.app.state.settings.audit_path),
            Path(request.app.state.settings.policy_path),
            out_path,
            sample_size=sample_size,
            run_llm_review=run_llm_review,
            lookback_minutes=int(lookback_minutes) if lookback_minutes else None,
            checkpoint_path=Path(checkpoint) if checkpoint else None,
        )
        return JSONResponse(status_code=200, content=result)

    return router
