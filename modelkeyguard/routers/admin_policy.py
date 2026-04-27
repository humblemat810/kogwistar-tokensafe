from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..registration import RegistrationError, RegistrationService


def create_router() -> APIRouter:
    router = APIRouter(tags=["admin-policy"])

    def _registration_service(request: Request) -> RegistrationService | None:
        graph_state = request.app.state.guard.graph_state
        if not graph_state:
            return None
        return RegistrationService(graph_state)

    async def _payload(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
            if isinstance(body, dict):
                return body
        except Exception:
            pass
        try:
            form = await request.form()
            return {k: v for k, v in form.items()}
        except Exception:
            return {}

    @router.post("/admin/policy/users")
    async def admin_register_user(request: Request):
        reg = _registration_service(request)
        if not reg:
            return JSONResponse(status_code=503, content={"error": {"message": "graph_state_unavailable"}})
        body = await _payload(request)
        user_id = str(body.get("user_id", "")).strip()
        if not user_id:
            return JSONResponse(status_code=400, content={"error": {"message": "user_id_required"}})
        try:
            reg.register_user(user_id, str(body.get("display_name", "")).strip())
            return {"ok": True, "user_id": user_id}
        except RegistrationError as exc:
            return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})

    @router.post("/admin/policy/applications")
    async def admin_register_application(request: Request):
        reg = _registration_service(request)
        if not reg:
            return JSONResponse(status_code=503, content={"error": {"message": "graph_state_unavailable"}})
        body = await _payload(request)
        application_id = str(body.get("application_id", "")).strip()
        if not application_id:
            return JSONResponse(status_code=400, content={"error": {"message": "application_id_required"}})
        try:
            reg.register_application(application_id, str(body.get("display_name", "")).strip())
            return {"ok": True, "application_id": application_id}
        except RegistrationError as exc:
            return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})

    @router.post("/admin/policy/principals")
    async def admin_register_principal(request: Request):
        reg = _registration_service(request)
        if not reg:
            return JSONResponse(status_code=503, content={"error": {"message": "graph_state_unavailable"}})
        body = await _payload(request)
        principal_id = str(body.get("principal_id", "")).strip()
        if not principal_id:
            return JSONResponse(status_code=400, content={"error": {"message": "principal_id_required"}})
        raw_groups = body.get("groups", [])
        if isinstance(raw_groups, str):
            groups = [g.strip() for g in raw_groups.split(",") if g.strip()]
        elif isinstance(raw_groups, list):
            groups = [str(g).strip() for g in raw_groups if str(g).strip()]
        else:
            groups = []
        try:
            reg.register_principal(
                principal_id,
                kind=str(body.get("kind", "agent")).strip() or "agent",
                groups=groups,
                namespace=str(body.get("namespace", "tenant:kogwistar")).strip() or "tenant:kogwistar",
                application_id=(str(body.get("application_id", "")).strip() or None),
                description=str(body.get("description", "")).strip(),
            )
            return {"ok": True, "principal_id": principal_id}
        except RegistrationError as exc:
            return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})

    @router.post("/admin/policy/quotas/upsert")
    async def admin_upsert_quota(request: Request):
        reg = _registration_service(request)
        if not reg:
            return JSONResponse(status_code=503, content={"error": {"message": "graph_state_unavailable"}})
        body = await _payload(request)
        lane = str(body.get("lane", "")).strip()
        subject_id = str(body.get("subject_id", "")).strip()
        quota_name = str(body.get("quota_name", "")).strip()
        period = str(body.get("period", "")).strip()
        if not lane or not subject_id or not quota_name:
            return JSONResponse(status_code=400, content={"error": {"message": "lane_subject_id_quota_name_required"}})

        graph_state = request.app.state.guard.graph_state
        if lane in {"principal", "user"} and subject_id not in graph_state.nodes:
            return JSONResponse(status_code=400, content={"error": {"message": "subject_not_registered"}})
        if lane == "key" and subject_id not in graph_state.nodes and subject_id not in request.app.state.guard.keys:
            return JSONResponse(status_code=400, content={"error": {"message": "subject_not_registered"}})

        def _num(name: str, cast):
            v = body.get(name)
            if v in (None, ""):
                return None
            return cast(v)

        max_usd = _num("max_usd", float)
        max_tokens = _num("max_tokens", int)
        max_requests = _num("max_requests", int)
        if not period:
            return JSONResponse(status_code=400, content={"error": {"message": "period_required"}})
        if max_usd is None and max_tokens is None and max_requests is None:
            return JSONResponse(status_code=400, content={"error": {"message": "at_least_one_limit_required"}})

        try:
            qid = reg.append_quota_revision(
                lane,
                subject_id,
                quota_name,
                period=period or None,
                max_usd=max_usd,
                max_tokens=max_tokens,
                max_requests=max_requests,
                revoked=False,
            )
            return {"ok": True, "quota_policy_id": qid}
        except (RegistrationError, ValueError) as exc:
            return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})

    @router.post("/admin/policy/quotas/revoke")
    async def admin_revoke_quota(request: Request):
        reg = _registration_service(request)
        if not reg:
            return JSONResponse(status_code=503, content={"error": {"message": "graph_state_unavailable"}})
        body = await _payload(request)
        lane = str(body.get("lane", "")).strip()
        subject_id = str(body.get("subject_id", "")).strip()
        quota_name = str(body.get("quota_name", "")).strip()
        if not lane or not subject_id or not quota_name:
            return JSONResponse(status_code=400, content={"error": {"message": "lane_subject_id_quota_name_required"}})
        try:
            qid = reg.revoke_quota(lane, subject_id, quota_name, reason=str(body.get("reason", "")).strip())
            return {"ok": True, "quota_policy_id": qid, "revoked": True}
        except RegistrationError as exc:
            return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})

    @router.get("/admin/policy/quotas.json")
    def admin_list_quotas(request: Request, lane: str | None = None, subject_id: str | None = None):
        graph_state = request.app.state.guard.graph_state
        if not graph_state:
            return JSONResponse(status_code=503, content={"error": {"message": "graph_state_unavailable"}})
        rows: list[dict[str, Any]] = []
        for node in graph_state.nodes.values():
            if node.kind != "quota_policy":
                continue
            if lane and node.payload.get("lane") != lane:
                continue
            if subject_id and node.payload.get("subject_id") != subject_id:
                continue
            rows.append(
                {
                    "id": node.id,
                    "lane": node.payload.get("lane"),
                    "subject_id": node.payload.get("subject_id"),
                    "quota_name": node.payload.get("quota_name"),
                    "period": node.payload.get("period"),
                    "max_usd": node.payload.get("max_usd"),
                    "max_tokens": node.payload.get("max_tokens"),
                    "max_requests": node.payload.get("max_requests"),
                    "revoked": bool(node.payload.get("revoked")),
                    "revision_ms": node.payload.get("revision_ms", 0),
                }
            )
        rows.sort(key=lambda r: (r.get("revision_ms") or 0, r["id"]))
        return {"data": rows}

    return router
