from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
import uuid
import threading
import time

from .kogwistar_acl_adapter import AdapterInfo, load_acl_graph
from .graph_state import GraphStateStore, normalize_quota_period, utc_now

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
    upstream_url: str = ""
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
    _reservation_lock: Any = field(default_factory=threading.RLock, repr=False)

    _RESERVATION_NAMESPACE = "modelkeyguard_reservation"
    _RESERVATION_KEY = "active"

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
                    "token": "token_quota_exceeded",
                }.get(lane, reason)
                return self._deny(request, rid, 429, status_reason, acl_reason, remaining)
        if key.approval_threshold_usd and request.estimated_cost_usd >= key.approval_threshold_usd:
            d = self._decision(request, rid, False, True, 202, "approval_required", acl_reason, remaining, None)
            self._access_event("ACL_DECISION_APPROVAL_REQUIRED", request, rid, {"reason": d.reason, "remaining": remaining})
            return d
        d = self._decision(request, rid, True, False, 200, "allow", acl_reason, remaining, key.secret_ref)
        self._access_event("ACL_DECISION_ALLOW", request, rid, {"reason": d.reason, "remaining": remaining})
        return d

    def record_usage(self, decision: AccessDecision, estimated_cost_usd: float, actual_cost_usd: float, actual_tokens: int = 0, usage_authoritative: bool | None = None) -> None:
        if not decision.allowed:
            return
        # Reservation state and every quota-lane debit commit in one named-
        # projection CAS.  This prevents settled-without-debit and partial-lane
        # debit after a worker crash.
        settled = self.settle_reservation(decision, estimated_cost_usd, actual_cost_usd, actual_tokens, usage_authoritative=usage_authoritative)
        if not settled:
            self.update_reservation(decision.request_id, "uncertain", actual_cost_usd, actual_tokens)
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
            self.graph_state.append_access_conversation_event(decision.request_id, "QUOTA_DEBITED" if settled else "QUOTA_SETTLEMENT_UNCERTAIN", payload)
            if settled:
                self.graph_state.append_access_conversation_event(decision.request_id, "MODEL_USAGE_RESULT", payload)
        self.audit.append(AuditEvent(datetime.now(timezone.utc).isoformat(), "MODEL_KEY_CHECK", decision.principal_id, decision.key_id, decision.namespace, decision.allowed, decision.requires_approval, decision.reason, decision.acl_reason, estimated_cost_usd, actual_cost_usd))

    @staticmethod
    def _projection_payload(row: dict[str, Any] | None) -> dict[str, Any]:
        if isinstance(row, dict) and isinstance(row.get("payload"), dict):
            return dict(row["payload"])
        return dict(row) if isinstance(row, dict) else {}

    @staticmethod
    def _projection_update(namespace: str, key: str, payload: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any]:
        update: dict[str, Any] = {"namespace": namespace, "key": key, "payload": payload}
        if isinstance(current, dict) and "payload" in current:
            authoritative = int(current.get("last_authoritative_seq", 0))
            materialized = int(current.get("last_materialized_seq", 0))
            update.update(
                expected_last_authoritative_seq=authoritative,
                expected_last_materialized_seq=materialized,
                last_authoritative_seq=authoritative + 1,
                last_materialized_seq=materialized + 1,
            )
        else:
            update["expected_payload_hash"] = current
        return update

    def _quota_projection_address(self, lane: str, subject_id: str, period: str, when: datetime) -> tuple[str, str, dict[str, Any] | None]:
        assert self.graph_state is not None
        bucket = __import__("modelkeyguard.graph_state", fromlist=["period_bucket"]).period_bucket(when, period)
        projection_id = self.graph_state.quota_projection_id(lane, subject_id, period, bucket)
        module = self.graph_state.__class__.__module__
        if "postgres_state" in module or "kogwistar_postgres_state" in module:
            namespace, key = "modelkeyguard.quota_usage", projection_id
        else:
            prefix = "quota_projection:"
            namespace, key = "quota_projection", projection_id.removeprefix(prefix)
        row = self.graph_state.get_named_projection(namespace, key)
        return namespace, key, row

    def settle_reservation(self, decision: AccessDecision, estimated_cost_usd: float, actual_cost_usd: float, actual_tokens: int = 0, *, usage_authoritative: bool | None = None) -> bool:
        """Atomically settle reservation plus all quota projections.

        A durable settlement marker makes retry idempotent; marker and lane
        projections share the same CAS transaction.  Ledger/event enrichment
        remains after commit and can be rebuilt from the marker.
        """
        if not self.graph_state:
            return True
        now = utc_now()
        retries = max(1, int(__import__("os").getenv("MODELKEYGUARD_SETTLEMENT_CAS_RETRIES", "3")))
        for _attempt in range(retries):
            reservation_row = self.graph_state.get_named_projection(self._RESERVATION_NAMESPACE, self._RESERVATION_KEY)
            reservation_payload = self._projection_payload(reservation_row)
            reservations = dict(reservation_payload.get("reservations") or {})
            reservation = dict(reservations.get(decision.request_id) or {})
            marker_namespace, marker_key = "modelkeyguard_settlement", decision.request_id
            marker_row = self.graph_state.get_named_projection(marker_namespace, marker_key)
            marker = self._projection_payload(marker_row)
            if marker.get("state") == "settled":
                # CAS may have committed immediately before a worker crash.  On
                # retry, repair the append-only ledger exactly once from marker.
                has_ledger = any(
                    getattr(node, "kind", "") == "usage_ledger_event"
                    and str(getattr(node, "payload", {}).get("request_id") or "") == decision.request_id
                    for node in getattr(self.graph_state, "nodes", {}).values()
                )
                if not has_ledger:
                    ledger_subject = decision.on_behalf_of_user_id or decision.principal_id
                    if ledger_subject:
                        self.graph_state.append_usage_ledger_event(
                            ledger_subject,
                            {"request_id": decision.request_id, "actual_cost_usd": marker.get("actual_cost_usd", actual_cost_usd), "actual_tokens": marker.get("actual_tokens", actual_tokens)},
                        )
                return True

            lanes = reservation.get("lanes") or [
                {"lane": lane, "subject_id": subject, "period": q.get("period", "hour")}
                for lane, subject in {"principal": decision.principal_id, "key": decision.key_id, **({"user": decision.on_behalf_of_user_id} if decision.on_behalf_of_user_id else {}), **({"token": f"token:{decision.token_id}"} if decision.token_id else {})}.items()
                for q in self._quota_policies(lane, subject)
            ]
            updates: list[dict[str, Any]] = []
            seen_quota: set[tuple[str, str, str]] = set()
            for lane_row in lanes:
                if not isinstance(lane_row, dict):
                    continue
                lane = str(lane_row.get("lane") or "")
                subject = str(lane_row.get("subject_id") or "")
                period = normalize_quota_period(str(lane_row.get("period") or "hour"))
                address = (lane, subject, period)
                if address in seen_quota:
                    continue
                seen_quota.add(address)
                namespace, key, current = self._quota_projection_address(lane, subject, period, now)
                previous = self._projection_payload(current) or {
                    "lane": lane, "subject_id": subject, "period": period,
                    "bucket": __import__("modelkeyguard.graph_state", fromlist=["period_bucket"]).period_bucket(now, period),
                    "usd": 0.0, "tokens": 0, "requests": 0,
                }
                next_payload = dict(previous)
                next_payload["usd"] = round(float(previous.get("usd", 0.0)) + float(actual_cost_usd), 8)
                next_payload["tokens"] = int(previous.get("tokens", 0)) + int(actual_tokens)
                next_payload["requests"] = int(previous.get("requests", 0)) + 1
                updates.append(self._projection_update(namespace, key, next_payload, current))

            if reservation:
                reservation.update(
                    state="settled",
                    actual_cost_usd=float(actual_cost_usd),
                    actual_tokens=int(actual_tokens),
                    usage_estimated=(not usage_authoritative) if usage_authoritative is not None else (actual_cost_usd == estimated_cost_usd and actual_tokens == int(reservation.get("estimated_tokens", actual_tokens))),
                    settled_at=now.timestamp(),
                )
                reservations[decision.request_id] = reservation
                reservation_payload = {**reservation_payload, "reservations": reservations, "updated_at": now.timestamp()}
                updates.append(self._projection_update(self._RESERVATION_NAMESPACE, self._RESERVATION_KEY, reservation_payload, reservation_row))

            settlement_payload = {
                "request_id": decision.request_id,
                "state": "settled",
                "estimated_cost_usd": float(estimated_cost_usd),
                "actual_cost_usd": float(actual_cost_usd),
                "actual_tokens": int(actual_tokens),
                "usage_estimated": (not usage_authoritative) if usage_authoritative is not None else (actual_cost_usd == estimated_cost_usd and actual_tokens == int(reservation.get("estimated_tokens", actual_tokens))),
                "settled_at": now.timestamp(),
                "lanes": sorted([{"lane": lane, "subject_id": subject, "period": period} for lane, subject, period in seen_quota], key=lambda x: (x["lane"], x["subject_id"], x["period"])),
            }
            updates.append(self._projection_update(marker_namespace, marker_key, settlement_payload, marker_row))
            cas = getattr(self.graph_state, "compare_and_swap_named_projections", None)
            if callable(cas) and bool(cas(updates)):
                ledger_subject = decision.on_behalf_of_user_id or decision.principal_id
                if ledger_subject:
                    try:
                        self.graph_state.append_usage_ledger_event(ledger_subject, {"request_id": decision.request_id, "actual_cost_usd": actual_cost_usd, "actual_tokens": actual_tokens})
                    except Exception:
                        # Projection CAS is authoritative; ledger append is
                        # repairable from settlement marker on next retry.
                        pass
                return True
            if not callable(cas):
                break
        return False

    def reserve_admission(self, request: Request, decision: AccessDecision) -> bool:
        """Atomically reserve estimated quota across every lane."""
        if not decision.allowed or not self.graph_state:
            return bool(decision.allowed)
        now = time.time()
        ttl = float(__import__("os").getenv("MODELKEYGUARD_RESERVATION_TTL_SECONDS", "600"))
        retries = max(0, int(__import__("os").getenv("MODELKEYGUARD_RESERVATION_CAS_RETRIES", "3")))
        with self._reservation_lock:
            for _attempt in range(retries + 1):
                lanes: list[dict[str, Any]] = []
                current = self._reservation_payload()
                reservations = dict(current.get("reservations") or {})
                for rid, row in list(reservations.items()):
                    if row.get("state") in {"settled", "released"}:
                        reservations.pop(rid, None)
                existing = reservations.get(decision.request_id)
                if existing and existing.get("state") not in {"settled", "released"}:
                    return True
                for lane, subject_id in self._quota_subjects(request).items():
                    for q in self._quota_policies(lane, subject_id):
                        period = normalize_quota_period(q.get("period", "hour"))
                        used = self.graph_state.get_quota_used(lane, subject_id, period, utc_now())
                        reserved = self._reserved_totals(reservations, lane, subject_id, period)
                        if q.get("max_usd") is not None and float(used["usd"]) + reserved["usd"] + request.estimated_cost_usd > float(q["max_usd"]):
                            return False
                        if q.get("max_tokens") is not None and float(used["tokens"]) + reserved["tokens"] + request.estimated_tokens > float(q["max_tokens"]):
                            return False
                        if q.get("max_requests") is not None and float(used["requests"]) + reserved["requests"] + 1 > float(q["max_requests"]):
                            return False
                        lanes.append({"lane": lane, "subject_id": subject_id, "period": period})
                reservations[decision.request_id] = {
                    "state": "reserved",
                    "created_at": now,
                    "expires_at": now + ttl,
                    "estimated_cost_usd": float(request.estimated_cost_usd),
                    "estimated_tokens": int(request.estimated_tokens),
                    "lanes": lanes,
                    "principal_id": decision.principal_id,
                    "key_id": decision.key_id,
                }
                if self._cas_reservation_payload({"reservations": reservations, "updated_at": now}, current):
                    break
            else:
                return False
        self._access_event("QUOTA_RESERVATION_RESERVED", request, decision.request_id, {"lanes": lanes, "expires_at": now + ttl})
        return True

    def update_reservation(self, request_id: str, state: str, actual_cost_usd: float | None = None, actual_tokens: int | None = None) -> None:
        if not self.graph_state:
            return
        with self._reservation_lock:
            current = self._reservation_payload()
            reservations = dict(current.get("reservations") or {})
            row = dict(reservations.get(request_id) or {})
            if not row:
                return
            row["state"] = state
            row["updated_at"] = time.time()
            if actual_cost_usd is not None:
                row["actual_cost_usd"] = float(actual_cost_usd)
            if actual_tokens is not None:
                row["actual_tokens"] = int(actual_tokens)
            reservations[request_id] = row
            self._cas_reservation_payload({"reservations": reservations, "updated_at": time.time()}, current)

    def _reservation_payload(self) -> dict[str, Any]:
        get = getattr(self.graph_state, "get_named_projection", None)
        row = get(self._RESERVATION_NAMESPACE, self._RESERVATION_KEY) if callable(get) else None
        if not row:
            return {"reservations": {}}
        payload = row.get("payload") if isinstance(row, dict) and isinstance(row.get("payload"), dict) else row
        return dict(payload) if isinstance(payload, dict) else {"reservations": {}}

    def _cas_reservation_payload(self, payload: dict[str, Any], current: dict[str, Any]) -> bool:
        cas = getattr(self.graph_state, "compare_and_swap_named_projections", None)
        if callable(cas):
            update: dict[str, Any] = {"namespace": self._RESERVATION_NAMESPACE, "key": self._RESERVATION_KEY, "payload": payload}
            getter = getattr(self.graph_state, "get_named_projection", None)
            raw_current = getter(self._RESERVATION_NAMESPACE, self._RESERVATION_KEY) if callable(getter) else None
            if isinstance(raw_current, dict) and "payload" in raw_current:
                expected_a = int(raw_current.get("last_authoritative_seq", 0))
                expected_m = int(raw_current.get("last_materialized_seq", 0))
                update["expected_last_authoritative_seq"] = expected_a
                update["expected_last_materialized_seq"] = expected_m
                update["last_authoritative_seq"] = expected_a + 1
                update["last_materialized_seq"] = expected_m + 1
            else:
                update["expected_payload_hash"] = current if current.get("reservations") else None
            try:
                return bool(cas([update]))
            except TypeError:
                return bool(cas([update]))
        replace = getattr(self.graph_state, "replace_named_projection", None)
        if callable(replace):
            replace(self._RESERVATION_NAMESPACE, self._RESERVATION_KEY, payload)
            return True
        return False

    @staticmethod
    def _reserved_totals(reservations: dict[str, Any], lane: str, subject_id: str, period: str) -> dict[str, float]:
        out = {"usd": 0.0, "tokens": 0.0, "requests": 0.0}
        now = time.time()
        for row in reservations.values():
            if row.get("state") in {"settled", "released"}:
                continue
            if row.get("state") == "reserved" and float(row.get("expires_at", now + 1)) < now:
                continue
            if any(x.get("lane") == lane and x.get("subject_id") == subject_id and x.get("period") == period for x in row.get("lanes", [])):
                out["usd"] += float(row.get("estimated_cost_usd", 0.0))
                out["tokens"] += float(row.get("estimated_tokens", 0))
                out["requests"] += 1.0
        return out

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
        if request.token_id:
            out["token"] = f"token:{request.token_id}"
        return out

    def _quota_policies(self, lane: str, subject_id: str) -> list[dict[str, Any]]:
        if not self.graph_state:
            return []
        get_projection = getattr(self.graph_state, "get_quota_policy_projection", None)
        rebuild_projection = getattr(self.graph_state, "rebuild_quota_policy_projection", None)
        replace_projection = getattr(self.graph_state, "replace_quota_policy_projection", None)

        projection: dict[str, Any] | None = None
        if callable(get_projection):
            try:
                loaded = get_projection(lane, subject_id)
                if isinstance(loaded, dict):
                    projection = loaded
            except Exception:
                projection = None
        if projection is None and callable(rebuild_projection):
            try:
                rebuilt = rebuild_projection(lane, subject_id)
                if isinstance(rebuilt, dict):
                    projection = rebuilt
            except Exception:
                projection = None
        if projection is not None:
            items = projection.get("items")
            if isinstance(items, list):
                out: list[dict[str, Any]] = []
                for item in items:
                    if isinstance(item, dict) and item.get("lane") == lane and not bool(item.get("revoked")):
                        out.append(item)
                return out

        latest_by_name: dict[str, tuple[int, str, dict[str, Any]]] = {}
        for e in self.graph_state.edges_from(subject_id, "HAS_QUOTA_POLICY"):
            n = self.graph_state.nodes.get(e.target)
            if not n or n.kind != "quota_policy" or n.payload.get("lane") != lane:
                continue
            name = self._quota_policy_name(n.id, n.payload, lane, subject_id)
            rev = self._quota_policy_revision(n.id, n.payload)
            cur = latest_by_name.get(name)
            if cur is None or rev > cur[0] or (rev == cur[0] and n.id > cur[1]):
                latest_by_name[name] = (rev, n.id, n.payload)
        out = [payload for _name, (_rev, _id, payload) in sorted(latest_by_name.items()) if not bool(payload.get("revoked"))]
        if callable(replace_projection):
            try:
                replace_projection(
                    lane,
                    subject_id,
                    {
                        "lane": lane,
                        "subject_id": subject_id,
                        "items": [dict(item) for item in out],
                        "updated_at_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
                        "projection_schema_version": 1,
                    },
                )
            except Exception:
                pass
        return out

    @staticmethod
    def _quota_policy_name(node_id: str, payload: dict[str, Any], lane: str, subject_id: str) -> str:
        explicit = payload.get("quota_name")
        if isinstance(explicit, str) and explicit:
            return explicit
        prefix = f"quota:{lane}:{subject_id}:"
        if node_id.startswith(prefix):
            tail = node_id[len(prefix):]
            if ":rev:" in tail:
                return tail.split(":rev:", 1)[0] or node_id
            return tail or node_id
        return node_id

    @staticmethod
    def _quota_policy_revision(node_id: str, payload: dict[str, Any]) -> int:
        rev = payload.get("revision_ms")
        if isinstance(rev, (int, float)):
            return int(rev)
        if isinstance(rev, str) and rev.isdigit():
            return int(rev)
        if ":rev:" in node_id:
            tail = node_id.rsplit(":rev:", 1)[-1]
            if tail.isdigit():
                return int(tail)
        reg = payload.get("registered_at_epoch")
        if isinstance(reg, (int, float)):
            return int(reg) * 1000
        if isinstance(reg, str) and reg.isdigit():
            return int(reg) * 1000
        return 0

    def _check_quota_lane(self, lane: str, subject_id: str, request: Request) -> tuple[bool, str, dict[str, float]]:
        if not self.graph_state:
            return False, "no_graph_quota", {}
        rem_out: dict[str, float] = {}
        for q in self._quota_policies(lane, subject_id):
            period = normalize_quota_period(q.get("period", "hour"))
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
