from __future__ import annotations

import json
import time
from collections.abc import Callable
from math import ceil
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..registration import RegistrationError, RegistrationService


def _json_request_body(schema: dict[str, Any], required: bool = True) -> dict[str, Any]:
    return {
        "required": required,
        "content": {
            "application/json": {
                "schema": schema,
            }
        },
    }


USER_BODY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "user_id": {"type": "string", "example": "user:alice"},
        "display_name": {"type": "string", "example": "Alice"},
    },
    "required": ["user_id"],
}

APPLICATION_BODY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "application_id": {"type": "string", "example": "app:crm-assistant"},
        "display_name": {"type": "string", "example": "CRM Assistant"},
    },
    "required": ["application_id"],
}

PRINCIPAL_BODY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "principal_id": {"type": "string", "example": "agent:azure-manual-demo"},
        "kind": {"type": "string", "example": "agent"},
        "groups": {
            "oneOf": [
                {"type": "string", "example": "app-dev,finance"},
                {"type": "array", "items": {"type": "string"}, "example": ["app-dev", "finance"]},
            ]
        },
        "namespace": {"type": "string", "example": "tenant:kogwistar"},
        "application_id": {"type": "string", "example": "app:crm-assistant"},
        "description": {"type": "string", "example": "Azure app principal"},
    },
    "required": ["principal_id"],
}

QUOTA_UPSERT_BODY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lane": {"type": "string", "enum": ["principal", "user", "key"], "example": "principal"},
        "subject_id": {"type": "string", "example": "agent:azure-manual-demo"},
        "quota_name": {"type": "string", "example": "hour"},
        "period": {"type": "string", "enum": ["10s", "hour", "day", "week", "month"], "example": "hour"},
        "max_usd": {"type": "number", "example": 20.0},
        "max_tokens": {"type": "integer", "example": 200000},
        "max_requests": {"type": "integer", "example": 1000},
    },
    "required": ["lane", "subject_id", "quota_name", "period"],
}

QUOTA_REVOKE_BODY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lane": {"type": "string", "enum": ["principal", "user", "key"], "example": "principal"},
        "subject_id": {"type": "string", "example": "agent:azure-manual-demo"},
        "quota_name": {"type": "string", "example": "hour"},
        "reason": {"type": "string", "example": "temporary freeze"},
    },
    "required": ["lane", "subject_id", "quota_name"],
}

TOKEN_ISSUE_BODY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "principal_id": {"type": "string", "example": "agent:azure-manual-demo"},
        "namespace": {"type": "string", "example": "tenant:kogwistar"},
        "on_behalf_of_user_id": {"type": "string", "example": "user:alice"},
        "application_id": {"type": "string", "example": "app:crm-assistant"},
        "scopes": {
            "oneOf": [
                {"type": "string", "example": "model.invoke"},
                {"type": "array", "items": {"type": "string"}, "example": ["model.invoke"]},
            ]
        },
    },
    "required": ["principal_id"],
}


def create_router(render_admin_policy_html: Callable[..., str]) -> APIRouter:
    router = APIRouter(tags=["admin-policy"])
    default_page_size = 20
    max_page_size = 100

    def _registration_service(request: Request) -> RegistrationService | None:
        graph_state = request.app.state.guard.graph_state
        if not graph_state:
            return None
        return RegistrationService(graph_state)

    def _parse_groups(raw_groups: Any) -> list[str]:
        if isinstance(raw_groups, str):
            return [g.strip() for g in raw_groups.split(",") if g.strip()]
        if isinstance(raw_groups, list):
            return [str(g).strip() for g in raw_groups if str(g).strip()]
        return []

    def _parse_scopes(raw_scopes: Any) -> list[str]:
        if isinstance(raw_scopes, str):
            scopes = [s.strip() for s in raw_scopes.split(",") if s.strip()]
            return scopes or ["model.invoke"]
        if isinstance(raw_scopes, list):
            scopes = [str(s).strip() for s in raw_scopes if str(s).strip()]
            return scopes or ["model.invoke"]
        return ["model.invoke"]

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

    def _wants_html(request: Request) -> bool:
        accept = (request.headers.get("accept") or "").lower()
        content_type = (request.headers.get("content-type") or "").lower()
        return "text/html" in accept or "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type

    def _query_text(request: Request, name: str) -> str:
        return str(request.query_params.get(name, "")).strip()

    def _query_int(request: Request, name: str, default: int, *, minimum: int = 1, maximum: int = 1000) -> int:
        raw = request.query_params.get(name)
        if raw in (None, ""):
            return default
        try:
            value = int(str(raw))
        except Exception:
            return default
        return max(minimum, min(maximum, value))

    def _policy_page_url(request: Request, *, anchor: str = "", **updates: Any) -> str:
        params = dict(request.query_params)
        for key, value in updates.items():
            if value in (None, ""):
                params.pop(key, None)
            else:
                params[key] = str(value)
        qs = urlencode(params)
        path = "/admin/policy"
        if qs:
            path = f"{path}?{qs}"
        if anchor:
            path = f"{path}#{anchor}"
        return path

    def _paginate_rows(
        request: Request,
        *,
        rows: list[dict[str, str]],
        page_param: str,
        page_size: int,
        anchor: str,
    ) -> tuple[list[dict[str, str]], dict[str, Any]]:
        total = len(rows)
        total_pages = max(1, ceil(total / page_size)) if total else 1
        page = _query_int(request, page_param, 1, minimum=1, maximum=total_pages)
        start = (page - 1) * page_size
        end = start + page_size
        sliced = rows[start:end]
        shown_start = 0 if total == 0 else start + 1
        shown_end = min(end, total)
        return sliced, {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "shown_start": shown_start,
            "shown_end": shown_end,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_url": _policy_page_url(request, anchor=anchor, **{page_param: page - 1}) if page > 1 else "",
            "next_url": _policy_page_url(request, anchor=anchor, **{page_param: page + 1}) if page < total_pages else "",
        }

    def _collect_quota_rows(graph_state: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for node in graph_state.nodes.values():
            if node.kind != "quota_policy":
                continue
            rows.append(
                {
                    "id": node.id,
                    "lane": str(node.payload.get("lane", "")),
                    "subject_id": str(node.payload.get("subject_id", "")),
                    "quota_name": str(node.payload.get("quota_name", "")),
                    "period": str(node.payload.get("period", "")),
                    "max_usd": node.payload.get("max_usd"),
                    "max_tokens": node.payload.get("max_tokens"),
                    "max_requests": node.payload.get("max_requests"),
                    "revoked": bool(node.payload.get("revoked")),
                    "revision_ms": int(node.payload.get("revision_ms", 0) or 0),
                }
            )
        rows.sort(key=lambda r: (r.get("revision_ms") or 0, r["id"]))
        return rows

    def _policy_snapshot(request: Request) -> dict[str, Any]:
        graph_state = request.app.state.guard.graph_state
        users: list[dict[str, str]] = []
        applications: list[dict[str, str]] = []
        principals: list[dict[str, str]] = []
        quotas: list[dict[str, str]] = []

        for node in graph_state.nodes.values():
            if node.kind == "end_user":
                users.append({"id": node.id, "display_name": str(node.payload.get("display_name", ""))})
            elif node.kind == "application":
                applications.append({"id": node.id, "display_name": str(node.payload.get("display_name", ""))})
            elif node.kind == "principal":
                namespace = ""
                for edge in graph_state.edges_from(node.id, "MEMBER_OF_NAMESPACE"):
                    namespace = edge.target
                    break
                principals.append(
                    {
                        "id": node.id,
                        "kind": str(node.payload.get("kind", "")),
                        "namespace": namespace,
                        "application_id": str(node.payload.get("application_id", "")),
                        "groups": ",".join(str(g) for g in node.payload.get("groups", [])),
                    }
                )
        for row in _collect_quota_rows(graph_state):
            quotas.append(
                {
                    "id": row["id"],
                    "lane": str(row.get("lane", "")),
                    "subject_id": str(row.get("subject_id", "")),
                    "quota_name": str(row.get("quota_name", "")),
                    "period": str(row.get("period", "")),
                    "max_usd": str(row.get("max_usd", "")),
                    "max_tokens": str(row.get("max_tokens", "")),
                    "max_requests": str(row.get("max_requests", "")),
                    "revoked": str(bool(row.get("revoked"))),
                    "revision_ms": str(row.get("revision_ms", 0)),
                }
            )

        users.sort(key=lambda r: r["id"])
        applications.sort(key=lambda r: r["id"])
        principals.sort(key=lambda r: r["id"])
        quotas.sort(key=lambda r: (int(r.get("revision_ms") or 0), r["id"]))

        users_q = _query_text(request, "users_q").lower()
        apps_q = _query_text(request, "apps_q").lower()
        principals_q = _query_text(request, "principals_q").lower()
        quotas_lane = _query_text(request, "quotas_lane").lower()
        quotas_subject_id = _query_text(request, "quotas_subject_id")
        quotas_name = _query_text(request, "quotas_name").lower()
        quotas_revoked = _query_text(request, "quotas_revoked").lower() or "any"
        if quotas_revoked not in {"any", "true", "false"}:
            quotas_revoked = "any"

        if users_q:
            users = [row for row in users if users_q in row["id"].lower() or users_q in row.get("display_name", "").lower()]
        if apps_q:
            applications = [
                row for row in applications if apps_q in row["id"].lower() or apps_q in row.get("display_name", "").lower()
            ]
        if principals_q:
            principals = [
                row
                for row in principals
                if principals_q in row["id"].lower()
                or principals_q in row.get("kind", "").lower()
                or principals_q in row.get("namespace", "").lower()
                or principals_q in row.get("application_id", "").lower()
                or principals_q in row.get("groups", "").lower()
            ]
        if quotas_lane:
            quotas = [row for row in quotas if row.get("lane", "").lower() == quotas_lane]
        if quotas_subject_id:
            quotas = [row for row in quotas if row.get("subject_id", "") == quotas_subject_id]
        if quotas_name:
            quotas = [row for row in quotas if quotas_name in row.get("quota_name", "").lower()]
        if quotas_revoked in {"true", "false"}:
            target = quotas_revoked == "true"
            quotas = [row for row in quotas if row.get("revoked", "").lower() == str(target).lower()]

        page_size = _query_int(request, "page_size", default_page_size, minimum=1, maximum=max_page_size)

        users, users_paging = _paginate_rows(request, rows=users, page_param="users_page", page_size=page_size, anchor="users")
        applications, apps_paging = _paginate_rows(
            request,
            rows=applications,
            page_param="apps_page",
            page_size=page_size,
            anchor="applications",
        )
        principals, principals_paging = _paginate_rows(
            request,
            rows=principals,
            page_param="principals_page",
            page_size=page_size,
            anchor="principals",
        )
        quotas, quotas_paging = _paginate_rows(
            request,
            rows=quotas,
            page_param="quotas_page",
            page_size=page_size,
            anchor="quotas",
        )

        for row in users:
            row["history_url"] = _policy_page_url(
                request,
                anchor="quotas",
                quotas_lane="user",
                quotas_subject_id=row["id"],
                quotas_page=1,
            )

        for row in principals:
            row["history_url"] = _policy_page_url(
                request,
                anchor="quotas",
                quotas_lane="principal",
                quotas_subject_id=row["id"],
                quotas_page=1,
            )

        filters = {
            "page_size": str(page_size),
            "users_q": _query_text(request, "users_q"),
            "apps_q": _query_text(request, "apps_q"),
            "principals_q": _query_text(request, "principals_q"),
            "quotas_lane": _query_text(request, "quotas_lane"),
            "quotas_subject_id": quotas_subject_id,
            "quotas_name": _query_text(request, "quotas_name"),
            "quotas_revoked": quotas_revoked,
        }

        return {
            "users": users,
            "applications": applications,
            "principals": principals,
            "quotas": quotas,
            "paging": {
                "users": users_paging,
                "applications": apps_paging,
                "principals": principals_paging,
                "quotas": quotas_paging,
            },
            "filters": filters,
        }

    def _render_page(
        request: Request,
        *,
        message: str = "",
        error: str = "",
        issued_token: str | None = None,
        issued_token_meta: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        snapshot = _policy_snapshot(request)
        html = render_admin_policy_html(
            users=snapshot["users"],
            applications=snapshot["applications"],
            principals=snapshot["principals"],
            quotas=snapshot["quotas"],
            paging=snapshot["paging"],
            filters=snapshot["filters"],
            message=message,
            error=error,
            issued_token=issued_token,
            issued_token_meta=issued_token_meta,
        )
        return HTMLResponse(content=html, status_code=status_code)

    def _append_admin_audit(request: Request, payload: dict[str, Any]) -> None:
        settings = request.app.state.settings
        path = Path(settings.audit_path)
        safe = {k: v for k, v in payload.items() if "secret" not in k.lower() and "safe_token" not in k.lower()}
        safe.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(safe, sort_keys=True) + "\n")

    def _issue_token(reg: RegistrationService, body: dict[str, Any], request: Request) -> tuple[str, dict[str, str]]:
        principal_id = str(body.get("principal_id", "")).strip()
        if not principal_id:
            raise RegistrationError("principal_id_required")
        namespace = str(body.get("namespace", "tenant:kogwistar")).strip() or "tenant:kogwistar"
        on_behalf_of_user_id = str(body.get("on_behalf_of_user_id", "")).strip() or None
        application_id = str(body.get("application_id", "")).strip() or None
        scopes = _parse_scopes(body.get("scopes"))

        issued = reg.issue_safe_token(
            principal_id=principal_id,
            namespace=namespace,
            on_behalf_of_user_id=on_behalf_of_user_id,
            application_id=application_id,
            scopes=scopes,
        )
        node = request.app.state.guard.graph_state.nodes.get(issued.token_node_id)
        safe_hash = str((node.payload if node else {}).get("safe_token_hash", ""))
        meta = {
            "token_id": issued.token_node_id,
            "principal_id": issued.principal_id,
            "namespace": issued.namespace,
            "on_behalf_of_user_id": issued.on_behalf_of_user_id or "",
            "application_id": issued.application_id or "",
            "scopes": ",".join(scopes),
            "safe_token_hash": safe_hash,
        }
        request.app.state.guard.graph_state.append_event(
            "ADMIN_SAFE_TOKEN_ISSUED",
            issued.token_node_id,
            {
                "principal_id": issued.principal_id,
                "namespace": issued.namespace,
                "on_behalf_of_user_id": issued.on_behalf_of_user_id,
                "application_id": issued.application_id,
                "scopes": scopes,
                "safe_token_hash": safe_hash,
            },
        )
        _append_admin_audit(
            request,
            {
                "event_type": "ADMIN_SAFE_TOKEN_ISSUED",
                "actor": "admin:web",
                **meta,
            },
        )
        return issued.token, meta

    @router.get("/admin/policy")
    def admin_policy_page(request: Request):
        return _render_page(request)

    @router.post("/admin/policy")
    async def admin_policy_form(request: Request):
        reg = _registration_service(request)
        if not reg:
            return JSONResponse(status_code=503, content={"error": {"message": "graph_state_unavailable"}})
        body = await _payload(request)
        action = str(body.get("action", "")).strip()
        try:
            if action == "register_user":
                user_id = str(body.get("user_id", "")).strip()
                if not user_id:
                    raise RegistrationError("user_id_required")
                reg.register_user(user_id, str(body.get("display_name", "")).strip())
                return _render_page(request, message=f"User registered: {user_id}")

            if action == "register_application":
                application_id = str(body.get("application_id", "")).strip()
                if not application_id:
                    raise RegistrationError("application_id_required")
                reg.register_application(application_id, str(body.get("display_name", "")).strip())
                return _render_page(request, message=f"Application registered: {application_id}")

            if action == "register_principal":
                principal_id = str(body.get("principal_id", "")).strip()
                if not principal_id:
                    raise RegistrationError("principal_id_required")
                reg.register_principal(
                    principal_id,
                    kind=str(body.get("kind", "agent")).strip() or "agent",
                    groups=_parse_groups(body.get("groups", "")),
                    namespace=str(body.get("namespace", "tenant:kogwistar")).strip() or "tenant:kogwistar",
                    application_id=str(body.get("application_id", "")).strip() or None,
                    description=str(body.get("description", "")).strip(),
                )
                return _render_page(request, message=f"Principal registered: {principal_id}")

            if action == "quota_upsert":
                lane = str(body.get("lane", "")).strip()
                subject_id = str(body.get("subject_id", "")).strip()
                quota_name = str(body.get("quota_name", "")).strip()
                period = str(body.get("period", "")).strip()
                if not lane or not subject_id or not quota_name or not period:
                    raise RegistrationError("lane_subject_id_quota_name_period_required")

                def _num(name: str, cast):
                    v = body.get(name)
                    if v in (None, ""):
                        return None
                    return cast(v)

                qid = reg.append_quota_revision(
                    lane,
                    subject_id,
                    quota_name,
                    period=period,
                    max_usd=_num("max_usd", float),
                    max_tokens=_num("max_tokens", int),
                    max_requests=_num("max_requests", int),
                    revoked=False,
                )
                return _render_page(request, message=f"Quota revision added: {qid}")

            if action == "quota_revoke":
                lane = str(body.get("lane", "")).strip()
                subject_id = str(body.get("subject_id", "")).strip()
                quota_name = str(body.get("quota_name", "")).strip()
                if not lane or not subject_id or not quota_name:
                    raise RegistrationError("lane_subject_id_quota_name_required")
                qid = reg.revoke_quota(lane, subject_id, quota_name, reason=str(body.get("reason", "")).strip())
                return _render_page(request, message=f"Quota revoked (append-only): {qid}")

            return _render_page(request, error="unsupported_action", status_code=400)
        except (RegistrationError, ValueError) as exc:
            return _render_page(request, error=str(exc), status_code=400)

    @router.post("/admin/policy/users", openapi_extra={"requestBody": _json_request_body(USER_BODY_SCHEMA)})
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

    @router.post("/admin/policy/applications", openapi_extra={"requestBody": _json_request_body(APPLICATION_BODY_SCHEMA)})
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

    @router.post("/admin/policy/principals", openapi_extra={"requestBody": _json_request_body(PRINCIPAL_BODY_SCHEMA)})
    async def admin_register_principal(request: Request):
        reg = _registration_service(request)
        if not reg:
            return JSONResponse(status_code=503, content={"error": {"message": "graph_state_unavailable"}})
        body = await _payload(request)
        principal_id = str(body.get("principal_id", "")).strip()
        if not principal_id:
            return JSONResponse(status_code=400, content={"error": {"message": "principal_id_required"}})
        try:
            reg.register_principal(
                principal_id,
                kind=str(body.get("kind", "agent")).strip() or "agent",
                groups=_parse_groups(body.get("groups", "")),
                namespace=str(body.get("namespace", "tenant:kogwistar")).strip() or "tenant:kogwistar",
                application_id=(str(body.get("application_id", "")).strip() or None),
                description=str(body.get("description", "")).strip(),
            )
            return {"ok": True, "principal_id": principal_id}
        except RegistrationError as exc:
            return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})

    @router.post("/admin/policy/quotas/upsert", openapi_extra={"requestBody": _json_request_body(QUOTA_UPSERT_BODY_SCHEMA)})
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

    @router.post("/admin/policy/quotas/revoke", openapi_extra={"requestBody": _json_request_body(QUOTA_REVOKE_BODY_SCHEMA)})
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

    @router.post("/admin/policy/tokens", openapi_extra={"requestBody": _json_request_body(TOKEN_ISSUE_BODY_SCHEMA)})
    async def admin_issue_safe_token(request: Request):
        reg = _registration_service(request)
        if not reg:
            return JSONResponse(status_code=503, content={"error": {"message": "graph_state_unavailable"}})
        body = await _payload(request)
        try:
            issued_token, meta = _issue_token(reg, body, request)
            # token verifier shares this graph state reference; token is immediately valid.
            if _wants_html(request):
                return _render_page(
                    request,
                    message=f"Safe token issued for {meta.get('principal_id', '')}",
                    issued_token=issued_token,
                    issued_token_meta=meta,
                )
            return {
                "ok": True,
                "one_time_reveal": True,
                "safe_token": issued_token,
                "token_id": meta.get("token_id"),
                "principal_id": meta.get("principal_id"),
                "namespace": meta.get("namespace"),
                "on_behalf_of_user_id": meta.get("on_behalf_of_user_id"),
                "application_id": meta.get("application_id"),
                "scopes": [s for s in str(meta.get("scopes", "")).split(",") if s],
                "safe_token_hash": meta.get("safe_token_hash"),
            }
        except RegistrationError as exc:
            if _wants_html(request):
                return _render_page(request, error=str(exc), status_code=400)
            return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})

    @router.get("/admin/policy/quotas.json")
    def admin_list_quotas(
        request: Request,
        lane: str | None = None,
        subject_id: str | None = None,
        quota_name: str | None = None,
        revoked: str | None = None,
        page: int = 1,
        page_size: int = 100,
    ):
        graph_state = request.app.state.guard.graph_state
        if not graph_state:
            return JSONResponse(status_code=503, content={"error": {"message": "graph_state_unavailable"}})
        rows = _collect_quota_rows(graph_state)

        lane_filter = str(lane or "").strip().lower()
        subject_filter = str(subject_id or "").strip()
        quota_name_filter = str(quota_name or "").strip().lower()
        revoked_filter = str(revoked or "").strip().lower()
        if revoked_filter not in {"", "true", "false"}:
            return JSONResponse(status_code=400, content={"error": {"message": "revoked_must_be_true_false"}})
        if lane_filter:
            rows = [row for row in rows if str(row.get("lane", "")).lower() == lane_filter]
        if subject_filter:
            rows = [row for row in rows if str(row.get("subject_id", "")) == subject_filter]
        if quota_name_filter:
            rows = [row for row in rows if quota_name_filter in str(row.get("quota_name", "")).lower()]
        if revoked_filter:
            target = revoked_filter == "true"
            rows = [row for row in rows if bool(row.get("revoked")) is target]

        safe_page_size = max(1, min(max_page_size, int(page_size)))
        total = len(rows)
        total_pages = max(1, ceil(total / safe_page_size)) if total else 1
        safe_page = max(1, min(int(page), total_pages))
        start = (safe_page - 1) * safe_page_size
        end = start + safe_page_size
        return {
            "data": rows[start:end],
            "paging": {
                "page": safe_page,
                "page_size": safe_page_size,
                "total": total,
                "total_pages": total_pages,
                "shown_start": 0 if total == 0 else start + 1,
                "shown_end": min(end, total),
            },
        }

    return router
