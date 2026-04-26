from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from ..services.security_notify import configured_admin_users, notify_security_event


def create_router() -> APIRouter:
    router = APIRouter(tags=["admin-security"])

    @router.post("/admin/security-events")
    async def admin_security_events(
        request: Request,
        x_modelkeyguard_security_secret: str | None = Header(default=None, alias="x-modelkeyguard-security-secret"),
    ):
        required = os.getenv("SECURITY_EVENT_SHARED_SECRET", "").strip()
        if not required:
            return JSONResponse(status_code=503, content={"error": {"message": "security_event_secret_not_configured"}})
        if x_modelkeyguard_security_secret != required:
            return JSONResponse(status_code=401, content={"error": {"message": "invalid_security_event_secret"}})

        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                return JSONResponse(status_code=400, content={"error": {"message": "invalid_payload"}})
        except Exception:
            return JSONResponse(status_code=400, content={"error": {"message": "invalid_json"}})

        event = _normalize_event(payload)
        admins = configured_admin_users()
        if admins and event["username"] not in admins:
            return {"ok": True, "ignored": True, "reason": "user_not_in_allowlist", "username": event["username"]}

        if request.app.state.guard.graph_state:
            node_id = f"security_event:{event['event_type']}:{event['username']}:{int(time.time() * 1000)}"
            request.app.state.guard.graph_state.put_node(node_id, "admin_security_event", event)
            request.app.state.guard.graph_state.append_event("ADMIN_LOGIN_EVENT", node_id, event)

        notify_status = notify_security_event(event)
        return {"ok": True, "event": event, "notify_status": notify_status}

    return router


def _normalize_event(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": str(payload.get("ts") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        "event_type": str(payload.get("event_type") or "admin_login"),
        "username": str(payload.get("username") or "unknown"),
        "host": str(payload.get("host") or ""),
        "source_ip": str(payload.get("source_ip") or ""),
        "auth_method": str(payload.get("auth_method") or ""),
        "raw": str(payload.get("raw") or ""),
    }
