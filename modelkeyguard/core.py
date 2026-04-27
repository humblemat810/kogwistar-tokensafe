from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
import uuid

from .kogwistar_acl_adapter import AdapterInfo, load_acl_graph
from .graph_state import GraphStateStore, utc_now

Action = Literal["model.invoke", "model.invoke.high_cost", "model.read_metadata", "model.admin"]


@dataclass(frozen=True)
class Principal:
    id: str
    kind: Literal["human", "agent", "service"]
    groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelKey:
    id: str
    provider: str
    models: tuple[str, ...]
    secret_ref: str | None
    display_name: str
    monthly_budget_usd: float = 0.0
    approval_threshold_usd: float = 999999.0
    intended_use: str = ""


@dataclass(frozen=True)
class Request:
    principal: Principal
    key_id: str
    model: str
    namespace: str
    action: Action = "model.invoke"
    estimated_cost_usd: float = 0.0
    estimated_tokens: int = 0
    reason: str = ""
    request_id: str = ""
    token_id: str = ""
    on_behalf_of_user_id: str | None = None


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    requires_approval: bool
    http_status: int
    reason: str
    acl_reason: str
    key_id: str
    principal_id: str
    namespace: str
    request_id: str
    token_id: str
    on_behalf_of_user_id: str | None
    remaining: dict[str, dict[str, float]]
    secret_ref: str | None = None


@dataclass(frozen=True)
class AuditEvent:
    ts: str
    type: str
    principal_id: str
    key_id: str
    namespace: str
    allowed: bool
    requires_approval: bool
    reason: str
    acl_reason: str
    estimated_cost_usd: float
    actual_cost_usd: float | None = None


@dataclass
class ModelKeyGuard:
    acl_graph: object = field(default_factory=lambda: load_acl_graph()[0])
    adapter_info: AdapterInfo = field(default_factory=lambda: load_acl_graph()[1])
    keys: dict[str, ModelKey] = field(default_factory=dict)
    audit: list[AuditEvent] = field(default_factory=list)
    _versions: dict[str, int] = field(default_factory=dict)
    graph_state: GraphStateStore | None = None

    @classmethod
    def create(cls) -> "ModelKeyGuard":
        graph, info = load_acl_graph()
        return cls(acl_graph=graph, adapter_info=info)

    def register_key(self, key: ModelKey) -> None:
        self.keys[key.id] = key

    def grant(self, key_id: str, mode: str, created_by: str, owner_id: str | None = None, namespace: str | None = None, shared_with_principals: tuple[str, ...] = (), shared_with_groups: tuple[str, ...] = ()) -> None:
        """Register a Kogwistar ACL record for a model-key resource."""
        if not hasattr(self.acl_graph, "add_record"):
            return
        version = self._versions.get(key_id, 0) + 1
        self._versions[key_id] = version
        try:
            from kogwistar.acl.graph import ACLRecord, ACLTarget  # type: ignore
            try:
                target = ACLTarget(truth_graph="model_keys", kind="node", id=key_id)
            except TypeError:
                target = ACLTarget(truth_graph="model_keys", grain="node", entity_id=key_id)
            record = ACLRecord(
                target=target,
                version=version,
                mode=mode,
                created_by=created_by,
                owner_id=owner_id,
                security_scope=namespace,
                shared_with_principals=frozenset(shared_with_principals),
                shared_with_groups=frozenset(shared_with_groups),
            )
            self.acl_graph.add_record(record)
        except Exception:
            self.acl_graph.add_record(
                truth_graph="model_keys",
                entity_id=key_id,
                grain="node",
                version=version,
                mode=mode,
                created_by=created_by,
                owner_id=owner_id,
                security_scope=namespace,
                shared_with_principals=shared_with_principals,
                shared_with_groups=shared_with_groups,
            )

    def check(self, request: Request) -> AccessDecision:
        rid = request.request_id or str(uuid.uuid4())
        key = self.keys.get(request.key_id)
        remaining: dict[str, dict[str, float]] = {}
        self._access_event("MODEL_ACCESS_REQUESTED", request, rid, {"model": request.model})
        if not key:
            return self._deny(request, rid, 403, "model_key_not_registered", "no key", remaining)
        if request.model not in key.models:
            return self._deny(request, rid, 403, "model_not_allowed_for_key", "model not on key", remaining)
        acl_allowed, acl_reason = self._acl_decide(request)
        if not acl_allowed:
            return self._deny(request, rid, 403, "permission_denied", acl_reason, remaining)
        for lane, subject_id in self._quota_subjects(request).items():
            exceeded, reason, rem = self._check_quota_lane(lane, subject_id, request)
            remaining[f"{lane}:{subject_id}"] = rem
            if exceeded:
                status_reason = {
                    "principal": "principal_capacity_exceeded",
                    "user": "user_quota_exceeded",
                    "key": "key_quota_exceeded",
                }.get(lane, reason)
                return self._deny(request, rid, 429, status_reason, acl_reason, remaining)
        if key.approval_threshold_usd and request.estimated_cost_usd >= key.approval_threshold_usd:
            d = self._decision(request, rid, False, True, 202, "approval_required", acl_reason, remaining, None)
            self._access_event("ACL_DECISION_APPROVAL_REQUIRED", request, rid, {"reason": d.reason, "remaining": remaining})
            return d
        d = self._decision(request, rid, True, False, 200, "allow", acl_reason, remaining, key.secret_ref)
        self._access_event("ACL_DECISION_ALLOW", request, rid, {"reason": d.reason, "remaining": remaining})
        return d

    def record_usage(self, decision: AccessDecision, estimated_cost_usd: float, actual_cost_usd: float, actual_tokens: int = 0) -> None:
        if not decision.allowed:
            return
        if self.graph_state:
            payload = {
                "request_id": decision.request_id,
                "principal_id": decision.principal_id,
                "on_behalf_of_user_id": decision.on_behalf_of_user_id,
                "key_id": decision.key_id,
                "namespace": decision.namespace,
                "estimated_cost_usd": estimated_cost_usd,
                "actual_cost_usd": actual_cost_usd,
                "actual_tokens": actual_tokens,
                "token_id": decision.token_id,
            }
            self.graph_state.append_access_conversation_event(decision.request_id, "QUOTA_DEBITED", payload)
            self.graph_state.append_usage_ledger_event(decision.on_behalf_of_user_id or decision.principal_id, payload)
            for lane, subject_id in {
                "principal": decision.principal_id,
                **({"user": decision.on_behalf_of_user_id} if decision.on_behalf_of_user_id else {}),
                "key": decision.key_id,
            }.items():
                for q in self._quota_policies(lane, subject_id):
                    self.graph_state.add_quota_usage(lane, subject_id, q["period"], actual_cost_usd, actual_tokens)
            self.graph_state.append_access_conversation_event(decision.request_id, "MODEL_USAGE_RESULT", payload)
        self.audit.append(AuditEvent(datetime.now(timezone.utc).isoformat(), "MODEL_KEY_CHECK", decision.principal_id, decision.key_id, decision.namespace, decision.allowed, decision.requires_approval, decision.reason, decision.acl_reason, estimated_cost_usd, actual_cost_usd))

    def _acl_decide(self, request: Request) -> tuple[bool, str]:
        try:
            try:
                result = self.acl_graph.decide(
                    truth_graph="model_keys",
                    kind="node",
                    id=request.key_id,
                    principal_id=request.principal.id,
                    principal_groups=frozenset(request.principal.groups),
                    security_scope=request.namespace,
                )
            except TypeError:
                result = self.acl_graph.decide(
                    truth_graph="model_keys",
                    entity_id=request.key_id,
                    grain="node",
                    principal_id=request.principal.id,
                    principal_groups=frozenset(request.principal.groups),
                    security_scope=request.namespace,
                )
            allowed = bool(getattr(result, "allowed", getattr(result, "visible", result[0] if isinstance(result, tuple) else result)))
            reason = str(getattr(result, "reason", "kogwistar_acl_decision"))
            return allowed, reason
        except Exception as e:
            return False, f"acl_error:{e.__class__.__name__}"

    def _quota_subjects(self, request: Request) -> dict[str, str]:
        out = {"principal": request.principal.id, "key": request.key_id}
        if request.on_behalf_of_user_id:
            out["user"] = request.on_behalf_of_user_id
        return out

    def _quota_policies(self, lane: str, subject_id: str) -> list[dict[str, Any]]:
        if not self.graph_state:
            return []
        policies: list[dict[str, Any]] = []
        for e in self.graph_state.edges_from(subject_id, "HAS_QUOTA_POLICY"):
            n = self.graph_state.nodes.get(e.target)
            if n and n.kind == "quota_policy" and n.payload.get("lane") == lane:
                policies.append(n.payload)
        return policies

    def _check_quota_lane(self, lane: str, subject_id: str, request: Request) -> tuple[bool, str, dict[str, float]]:
        if not self.graph_state:
            return False, "no_graph_quota", {}
        rem_out: dict[str, float] = {}
        for q in self._quota_policies(lane, subject_id):
            period = q.get("period", "hour")
            used = self.graph_state.get_quota_used(lane, subject_id, period, utc_now())
            max_usd = q.get("max_usd")
            max_tokens = q.get("max_tokens")
            max_requests = q.get("max_requests")
            if max_usd is not None:
                left = float(max_usd) - used["usd"] - request.estimated_cost_usd
                rem_out[f"usd_left_per_{period}"] = round(left, 8)
                if left < 0:
                    return True, f"{lane}_usd_quota_exceeded", rem_out
            if max_tokens is not None:
                left_t = float(max_tokens) - used["tokens"] - request.estimated_tokens
                rem_out[f"token_left_per_{period}"] = round(left_t, 3)
                if left_t < 0:
                    return True, f"{lane}_token_quota_exceeded", rem_out
            if max_requests is not None:
                left_r = float(max_requests) - used["requests"] - 1
                rem_out[f"requests_left_per_{period}"] = round(left_r, 3)
                if left_r < 0:
                    return True, f"{lane}_request_quota_exceeded", rem_out
        return False, "quota_available", rem_out

    def _access_event(self, event_type: str, request: Request, request_id: str, payload: dict[str, Any]) -> None:
        if self.graph_state:
            self.graph_state.append_access_conversation_event(request_id, event_type, {
                "principal_id": request.principal.id,
                "on_behalf_of_user_id": request.on_behalf_of_user_id,
                "key_id": request.key_id,
                "namespace": request.namespace,
                "token_id": request.token_id,
                "estimated_cost_usd": request.estimated_cost_usd,
                "estimated_tokens": request.estimated_tokens,
                **payload,
            })

    def _deny(self, request: Request, rid: str, http_status: int, reason: str, acl_reason: str, remaining: dict[str, dict[str, float]]) -> AccessDecision:
        d = self._decision(request, rid, False, False, http_status, reason, acl_reason, remaining, None)
        self._access_event("ACL_DECISION_DENY", request, rid, {"reason": reason, "http_status": http_status, "remaining": remaining})
        return d

    def _decision(self, request: Request, rid: str, allowed: bool, requires_approval: bool, http_status: int, reason: str, acl_reason: str, remaining: dict[str, dict[str, float]], secret_ref: str | None) -> AccessDecision:
        return AccessDecision(allowed, requires_approval, http_status, reason, acl_reason, request.key_id, request.principal.id, request.namespace, rid, request.token_id, request.on_behalf_of_user_id, remaining, secret_ref)
