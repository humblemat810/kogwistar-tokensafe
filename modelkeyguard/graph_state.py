from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

from .sealed_payload import (
    GRAPH_KEY_SENTINEL_NODE_ID,
    open_json,
    seal_json,
)
from .settings import is_dev_mode, read_env_or_file

DEFAULT_GRAPH_PATH = Path(os.getenv("MODELKEYGUARD_GRAPH_PATH", "out/modelkeyguard_graph.jsonl"))
DEFAULT_APP_KEY = "dev-modelkeyguard-change-me"
QUOTA_POLICY_PROJECTION_PREFIX = "quota_policy_projection"
SUPPORTED_STORE_BACKENDS = {"jsonl", "postgres", "kogwistar_postgres"}
SUPPORTED_QUOTA_PERIODS = {"10s", "hour", "day", "week", "month", "infinite", "lifetime"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def period_bucket(ts: datetime, period: str) -> str:
    period = normalize_quota_period(period)
    if period == "10s":
        second = (ts.second // 10) * 10
        return ts.replace(second=second, microsecond=0).isoformat()
    if period == "hour":
        return ts.replace(minute=0, second=0, microsecond=0).isoformat()
    if period == "day":
        return ts.replace(hour=0, minute=0, second=0, microsecond=0).date().isoformat()
    if period == "week":
        start = ts - timedelta(days=ts.weekday())
        return start.replace(hour=0, minute=0, second=0, microsecond=0).date().isoformat()
    if period == "month":
        return f"{ts.year:04d}-{ts.month:02d}"
    if period == "infinite":
        return "lifetime"
    raise ValueError(f"unsupported period: {period}")


def normalize_quota_period(period: str) -> str:
    normalized = str(period or "").strip().lower()
    if normalized == "lifetime":
        normalized = "infinite"
    if normalized not in SUPPORTED_QUOTA_PERIODS:
        raise ValueError(f"unsupported_quota_period:{period}")
    return normalized


def resolve_store_backend(value: str | None = None) -> str:
    store = (value if value is not None else os.getenv("MODELKEYGUARD_STORE", "kogwistar_postgres")).strip().lower() or "kogwistar_postgres"
    if store not in SUPPORTED_STORE_BACKENDS:
        raise ValueError(f"unsupported_store_backend:{store}")
    if store == "jsonl" and not is_dev_mode():
        raise ValueError(
            "jsonl_toy_backend_requires_dev_mode: set MODELKEYGUARD_ENV=local (or another non-production dev mode) "
            "before using the jsonl store."
        )
    return store


def resolve_graph_app_key(app_key: str | None = None) -> str:
    if app_key:
        return app_key
    configured = read_env_or_file("MODELKEYGUARD_GRAPH_KEY")
    if configured:
        return configured
    if os.getenv("MODELKEYGUARD_ALLOW_DEV_GRAPH_KEY", "").strip().lower() in {"1", "true", "yes", "on"}:
        print(
            "WARNING: using dev fallback MODELKEYGUARD_GRAPH_KEY. "
            "Set MODELKEYGUARD_GRAPH_KEY_FILE or MODELKEYGUARD_GRAPH_KEY for real state.",
            file=sys.stderr,
        )
        return DEFAULT_APP_KEY
    raise ValueError(
        "graph_key_required: set MODELKEYGUARD_GRAPH_KEY_FILE or MODELKEYGUARD_GRAPH_KEY. "
        "For toy JSONL demos only, set MODELKEYGUARD_ALLOW_DEV_GRAPH_KEY=1 to use the dev fallback key."
    )


@dataclass(frozen=True)
class GraphNode:
    id: str
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class GraphEdge:
    id: str
    kind: str
    source: str
    target: str
    payload: dict[str, Any]


class GraphStateStore:
    """Graph-native append-only state for ModelKeyGuard.

    The local JSONL file mirrors Kogwistar style primitives so the standalone app
    remains useful without a live Kogwistar server:

    * policy graph: principal/token/user/key/quota/policy-version nodes and grant edges
    * access conversation graph: every request outcome, including denied auth
    * usage ledger graph: only successful quota-consuming usage, strict linked list lanes
    * quota projection: rebuildable O(1) counters per token/user/principal/key and period

    Every node/edge/event payload is sealed at rest. The app opens it with
    MODELKEYGUARD_GRAPH_KEY. This is intentionally not a provider-key vault; raw
    provider keys still belong in env/Vault/Key Vault and are referenced by secret_ref.
    """

    def __init__(self, path: str | Path | None = None, app_key: str | None = None) -> None:
        self.path = Path(path or os.getenv("MODELKEYGUARD_GRAPH_PATH", str(DEFAULT_GRAPH_PATH)))
        self.app_key = resolve_graph_app_key(app_key)
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[str, GraphEdge] = {}
        self.events: list[dict[str, Any]] = []
        self.projections: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            self.load()

    def load(self) -> None:
        self.nodes.clear(); self.edges.clear(); self.events.clear(); self.projections.clear()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            payload = open_json(rec["payload_sealed"], self.app_key) if rec.get("payload_sealed") else {}
            typ = rec["record_type"]
            if typ == "node":
                self.nodes[rec["id"]] = GraphNode(rec["id"], rec["kind"], payload)
                if rec["id"] == GRAPH_KEY_SENTINEL_NODE_ID:
                    from .graph_key_contract import seed_graph_key_sentinel_from_storage_payload

                    seed_graph_key_sentinel_from_storage_payload(payload, self.app_key)
            elif typ == "edge":
                self.edges[rec["id"]] = GraphEdge(rec["id"], rec["kind"], rec["source"], rec["target"], payload)
            elif typ == "event":
                e = dict(rec); e["payload"] = payload; e.pop("payload_sealed", None); self.events.append(e)
            elif typ == "projection":
                self.projections[rec["id"]] = payload

    def _append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")

    def put_node(self, node_id: str, kind: str, payload: dict[str, Any]) -> None:
        existing = self.nodes.get(node_id)
        if node_id == GRAPH_KEY_SENTINEL_NODE_ID and existing is not None:
            if existing.kind != kind or existing.payload != payload:
                raise ValueError("graph key sentinel node cannot be overwritten")
            return
        self.nodes[node_id] = GraphNode(node_id, kind, payload)
        self._append({"record_type": "node", "id": node_id, "kind": kind, "payload_sealed": seal_json(payload, self.app_key)})

    def put_edge(self, edge_id: str, kind: str, source: str, target: str, payload: dict[str, Any]) -> None:
        self.edges[edge_id] = GraphEdge(edge_id, kind, source, target, payload)
        self._append({"record_type": "edge", "id": edge_id, "kind": kind, "source": source, "target": target, "payload_sealed": seal_json(payload, self.app_key)})

    def append_event(self, event_type: str, subject_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        event_id = f"event:{event_type}:{len(self.events)+1:08d}"
        rec = {"record_type": "event", "id": event_id, "kind": event_type, "subject": subject_id, "ts": iso_now(), "payload_sealed": seal_json(payload, self.app_key)}
        self._append(rec)
        out = dict(rec); out["payload"] = payload; out.pop("payload_sealed", None); self.events.append(out)
        return out

    def put_projection(self, projection_id: str, payload: dict[str, Any]) -> None:
        self.projections[projection_id] = payload
        self._append({"record_type": "projection", "id": projection_id, "kind": "quota_total", "payload_sealed": seal_json(payload, self.app_key)})

    def edges_from(self, source: str, kind: str | None = None) -> Iterable[GraphEdge]:
        for e in self.edges.values():
            if e.source == source and (kind is None or e.kind == kind):
                yield e

    def edges_to(self, target: str, kind: str | None = None) -> Iterable[GraphEdge]:
        for e in self.edges.values():
            if e.target == target and (kind is None or e.kind == kind):
                yield e

    def get_token_principal_id(self, token_node_id: str) -> str | None:
        return next((e.target for e in self.edges_from(token_node_id, "AUTHENTICATES_AS")), None)

    def quota_projection_id(self, lane: str, subject_id: str, period: str, bucket: str) -> str:
        return f"quota_projection:{lane}:{subject_id}:{period}:{bucket}"

    def quota_policy_projection_id(self, lane: str, subject_id: str) -> str:
        return f"{QUOTA_POLICY_PROJECTION_PREFIX}:{lane}:{subject_id}"

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

    def get_quota_policy_projection(self, lane: str, subject_id: str) -> dict[str, Any] | None:
        return self.projections.get(self.quota_policy_projection_id(lane, subject_id))

    def replace_quota_policy_projection(self, lane: str, subject_id: str, payload: dict[str, Any]) -> None:
        self.put_projection(self.quota_policy_projection_id(lane, subject_id), payload)

    def rebuild_quota_policy_projection(self, lane: str, subject_id: str) -> dict[str, Any]:
        latest_by_name: dict[str, tuple[int, str, dict[str, Any]]] = {}
        for e in self.edges_from(subject_id, "HAS_QUOTA_POLICY"):
            n = self.nodes.get(e.target)
            if not n or n.kind != "quota_policy" or n.payload.get("lane") != lane:
                continue
            name = self._quota_policy_name(n.id, n.payload, lane, subject_id)
            rev = self._quota_policy_revision(n.id, n.payload)
            cur = latest_by_name.get(name)
            if cur is None or rev > cur[0] or (rev == cur[0] and n.id > cur[1]):
                latest_by_name[name] = (rev, n.id, n.payload)
        items = [dict(payload) for _name, (_rev, _id, payload) in sorted(latest_by_name.items()) if not bool(payload.get("revoked"))]
        projection = {
            "lane": lane,
            "subject_id": subject_id,
            "items": items,
            "updated_at_ms": int(utc_now().timestamp() * 1000),
            "projection_schema_version": 1,
        }
        self.replace_quota_policy_projection(lane, subject_id, projection)
        return projection

    def get_quota_used(self, lane: str, subject_id: str, period: str, when: datetime | None = None) -> dict[str, float]:
        bucket = period_bucket(when or utc_now(), period)
        p = self.projections.get(self.quota_projection_id(lane, subject_id, period, bucket), {})
        return {"usd": float(p.get("usd", 0.0)), "tokens": float(p.get("tokens", 0.0)), "requests": float(p.get("requests", 0.0))}

    def add_quota_usage(self, lane: str, subject_id: str, period: str, usd: float, tokens: int, when: datetime | None = None) -> None:
        when = when or utc_now()
        bucket = period_bucket(when, period)
        pid = self.quota_projection_id(lane, subject_id, period, bucket)
        prev = self.projections.get(pid, {"lane": lane, "subject_id": subject_id, "period": period, "bucket": bucket, "usd": 0.0, "tokens": 0, "requests": 0})
        nxt = dict(prev)
        nxt["usd"] = round(float(prev.get("usd", 0.0)) + float(usd), 8)
        nxt["tokens"] = int(prev.get("tokens", 0)) + int(tokens)
        nxt["requests"] = int(prev.get("requests", 0)) + 1
        self.put_projection(pid, nxt)

    def append_access_conversation_event(self, request_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        node_id = f"access:{request_id}:{len(self.events)+1:08d}"
        self.put_node(node_id, "access_conversation_event", {"request_id": request_id, "event_type": event_type, **payload})
        self.append_event(event_type, node_id, {"request_id": request_id, **payload})
        return {"node_id": node_id, "event_type": event_type, **payload}

    def append_usage_ledger_event(self, lane_subject_id: str, payload: dict[str, Any]) -> str:
        """Append a strict sequential usage node to a per-user lane.

        The lane is a linked list: usage_head:<subject> -> usage:N, usage:N -> usage:N+1.
        Branching is intentionally disallowed by always replacing the head pointer.
        """
        safe_subject = lane_subject_id.replace(":", "_")
        head_id = f"usage_head:{lane_subject_id}"
        if head_id not in self.nodes:
            self.put_node(head_id, "usage_lane_head", {"subject_id": lane_subject_id, "tail": None})
        old_tail = self.nodes[head_id].payload.get("tail")
        seq = int(self.nodes[head_id].payload.get("seq", 0)) + 1
        usage_id = f"usage:{safe_subject}:{seq:08d}"
        self.put_node(usage_id, "usage_ledger_event", {"seq": seq, "lane_subject_id": lane_subject_id, **payload})
        if old_tail:
            self.put_edge(f"edge:{old_tail}:NEXT_USAGE:{usage_id}", "NEXT_USAGE", old_tail, usage_id, {})
        else:
            self.put_edge(f"edge:{head_id}:FIRST_USAGE:{usage_id}", "FIRST_USAGE", head_id, usage_id, {})
        self.put_node(head_id, "usage_lane_head", {"subject_id": lane_subject_id, "tail": usage_id, "seq": seq})
        return usage_id

    def secret_payload_plaintext_is_not_stored(self, forbidden: str) -> bool:
        if not self.path.exists():
            return True
        return forbidden not in self.path.read_text(encoding="utf-8")

    @classmethod
    def from_policy(cls, policy: dict[str, Any], path: str | Path | None = None, app_key: str | None = None) -> "GraphStateStore":
        store = resolve_store_backend()
        if cls is GraphStateStore and store == "postgres":
            from .postgres_state import PostgresGraphStateStore

            return PostgresGraphStateStore.from_policy(policy, app_key=app_key)  # type: ignore[return-value]
        if cls is GraphStateStore and store == "kogwistar_postgres":
            from .kogwistar_postgres_state import KogwistarPostgresGraphStateStore

            return KogwistarPostgresGraphStateStore.from_policy(policy, app_key=app_key)  # type: ignore[return-value]
        store = cls(path, app_key)
        existing_nodes = [node_id for node_id in store.nodes if node_id != GRAPH_KEY_SENTINEL_NODE_ID]
        if existing_nodes:
            sentinel = store.nodes.get(GRAPH_KEY_SENTINEL_NODE_ID)
            if sentinel is None:
                raise ValueError("graph key sentinel node missing from existing graph state")
            from .graph_key_contract import ensure_graph_key_sentinel_node

            ensure_graph_key_sentinel_node(store, store.app_key)
            return store
        from .graph_key_contract import ensure_graph_key_sentinel_node

        ensure_graph_key_sentinel_node(store, store.app_key)
        store.put_node("policy:version:0001", "policy_version", {"version": 1, "source": "config/gateway_policy.json"})
        store.put_node("issuer:keycloak:modelguard", "issuer", {"name": policy.get("issuer", "keycloak:modelguard")})
        for user_id, user in policy.get("users", {}).items():
            store.put_node(user_id, "end_user", user)
            for quota_name, quota in user.get("quotas", {}).items():
                qid = f"quota:user:{user_id}:{quota_name}"
                store.put_node(
                    qid,
                    "quota_policy",
                    {
                        "lane": "user",
                        "subject_id": user_id,
                        "quota_name": quota_name,
                        "registered_at_epoch": 0,
                        "revision_ms": 0,
                        "revoked": False,
                        **quota,
                    },
                )
                store.put_edge(f"edge:{user_id}:HAS_QUOTA_POLICY:{qid}", "HAS_QUOTA_POLICY", user_id, qid, {})
        for token, entry in policy.get("local_tokens", {}).items():
            principal_id = entry["principal_id"]
            store.put_node(principal_id, "principal", {"kind": entry.get("kind", "agent"), "groups": entry.get("groups", []), "description": entry.get("description")})
            token_id = f"token:{entry.get('jti', token[-12:])}"
            store.put_node(token_id, "auth_token", {"token": token, "jti": entry.get("jti", token[-12:]), "expires_at_epoch": entry.get("expires_at_epoch"), "scopes": entry.get("scopes", ["model.invoke"]), "namespace": entry.get("namespace", "tenant:kogwistar"), "on_behalf_of_user_id": entry.get("on_behalf_of_user_id")})
            store.put_edge(f"edge:{token_id}:AUTHENTICATES_AS:{principal_id}", "AUTHENTICATES_AS", token_id, principal_id, {})
            if entry.get("on_behalf_of_user_id"):
                store.put_edge(f"edge:{token_id}:ON_BEHALF_OF:{entry['on_behalf_of_user_id']}", "ON_BEHALF_OF", token_id, entry["on_behalf_of_user_id"], {})
            ns = entry.get("namespace", "tenant:kogwistar")
            store.put_node(ns, "namespace", {})
            store.put_edge(f"edge:{principal_id}:MEMBER_OF_NAMESPACE:{ns}", "MEMBER_OF_NAMESPACE", principal_id, ns, {})
            for quota_name, quota in entry.get("quotas", policy.get("principal_quotas", {}).get(principal_id, {})).items():
                qid = f"quota:principal:{principal_id}:{quota_name}"
                store.put_node(
                    qid,
                    "quota_policy",
                    {
                        "lane": "principal",
                        "subject_id": principal_id,
                        "quota_name": quota_name,
                        "registered_at_epoch": 0,
                        "revision_ms": 0,
                        "revoked": False,
                        **quota,
                    },
                )
                store.put_edge(f"edge:{principal_id}:HAS_QUOTA_POLICY:{qid}", "HAS_QUOTA_POLICY", principal_id, qid, {})
        for client_id, entry in policy.get("keycloak_clients", {}).items():
            principal_id = entry["principal_id"]
            store.put_node(principal_id, "principal", {"kind": entry.get("kind", "service"), "groups": entry.get("groups", [])})
            client_node = f"keycloak_client:{client_id}"
            store.put_node(client_node, "keycloak_client", {"client_id": client_id, "scopes": entry.get("scopes", ["model.invoke"]), "namespace": entry.get("namespace", "tenant:kogwistar"), "on_behalf_of_user_id": entry.get("on_behalf_of_user_id")})
            store.put_edge(f"edge:{client_node}:AUTHENTICATES_AS:{principal_id}", "AUTHENTICATES_AS", client_node, principal_id, {})
        for key in policy.get("model_keys", []):
            key_id = key["id"]
            store.put_node(
                key_id,
                "model_key",
                {
                    "provider": key["provider"],
                    "models": key["models"],
                    "display_name": key.get("display_name", key_id),
                    "upstream_url": key.get("upstream_url", ""),
                    "intended_use": key.get("intended_use", ""),
                    "sealed_secret_payload": key.get("sealed_secret_payload"),
                    "secret_ref": key.get("secret_ref"),
                },
            )
            for quota_name, quota in key.get("quotas", {}).items():
                qid = f"quota:key:{key_id}:{quota_name}"
                store.put_node(
                    qid,
                    "quota_policy",
                    {
                        "lane": "key",
                        "subject_id": key_id,
                        "quota_name": quota_name,
                        "registered_at_epoch": 0,
                        "revision_ms": 0,
                        "revoked": False,
                        **quota,
                    },
                )
                store.put_edge(f"edge:{key_id}:HAS_QUOTA_POLICY:{qid}", "HAS_QUOTA_POLICY", key_id, qid, {})
            acl = key.get("acl", {})
            ns = acl.get("namespace", "tenant:kogwistar")
            store.put_node(ns, "namespace", {})
            store.put_edge(f"edge:{key_id}:AVAILABLE_IN:{ns}", "AVAILABLE_IN", key_id, ns, {"acl_mode": acl.get("mode", "scope")})
        for principal_id, profile in policy.get("usage_profiles", {}).items():
            profile_id = f"usage_profile:{principal_id}"
            store.put_node(profile_id, "usage_profile", profile)
            store.put_edge(f"edge:{principal_id}:HAS_USAGE_PROFILE:{profile_id}", "HAS_USAGE_PROFILE", principal_id, profile_id, {})
        store.append_event("POLICY_GRAPH_INITIALIZED", "policy:version:0001", {"source": "config/gateway_policy.json"})
        return store
