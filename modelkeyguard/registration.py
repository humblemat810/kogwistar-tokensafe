from __future__ import annotations

import argparse
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import hashlib

from .graph_state import GraphStateStore, resolve_store_backend


def safe_token_hash(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def make_safe_token(prefix: str = "kgw_sk") -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


@dataclass(frozen=True)
class IssuedToken:
    token: str
    token_node_id: str
    principal_id: str
    on_behalf_of_user_id: str | None
    application_id: str | None
    namespace: str


class RegistrationError(ValueError):
    pass


class RegistrationService:
    """Graph-native registration utilities for SaaS-style model-key usage.

    These helpers intentionally write the same graph primitives used by the
    gateway runtime:

    * end_user nodes with HAS_QUOTA_POLICY edges
    * principal/application nodes with principal quota policies
    * auth_token nodes with AUTHENTICATES_AS and ON_BEHALF_OF edges
    * optional APPLICATION_PRINCIPAL edges for app-level reporting

    Safe tokens are only returned once. The graph stores a SHA-256 hash, never
    the raw token value. Existing demo tokens with plaintext `token` payloads
    are still supported by TokenVerifier for local development compatibility.
    """

    def __init__(self, store: GraphStateStore) -> None:
        self.store = store

    def register_user(self, user_id: str, display_name: str = "", metadata: dict[str, Any] | None = None) -> None:
        self._require_prefix(user_id, "user:")
        payload = {"display_name": display_name or user_id, "registered_at_epoch": int(time.time())}
        if metadata:
            payload["metadata"] = metadata
        self.store.put_node(user_id, "end_user", payload)
        self.store.append_event("USER_REGISTERED", user_id, {"user_id": user_id, "display_name": payload["display_name"]})

    def register_application(self, application_id: str, display_name: str = "", metadata: dict[str, Any] | None = None) -> None:
        self._require_prefix(application_id, "app:")
        payload = {"display_name": display_name or application_id, "registered_at_epoch": int(time.time())}
        if metadata:
            payload["metadata"] = metadata
        self.store.put_node(application_id, "application", payload)
        self.store.append_event("APPLICATION_REGISTERED", application_id, {"application_id": application_id, "display_name": payload["display_name"]})

    def register_principal(
        self,
        principal_id: str,
        *,
        kind: str = "agent",
        groups: list[str] | None = None,
        namespace: str = "tenant:kogwistar",
        application_id: str | None = None,
        description: str = "",
    ) -> None:
        if not (principal_id.startswith("agent:") or principal_id.startswith("service:") or principal_id.startswith("human:")):
            raise RegistrationError("principal_id_must_start_with_agent_service_or_human")
        payload = {"kind": kind, "groups": groups or [], "description": description, "registered_at_epoch": int(time.time())}
        if application_id:
            payload["application_id"] = application_id
        self.store.put_node(principal_id, "principal", payload)
        self.store.put_node(namespace, "namespace", {})
        self.store.put_edge(f"edge:{principal_id}:MEMBER_OF_NAMESPACE:{namespace}", "MEMBER_OF_NAMESPACE", principal_id, namespace, {})
        if application_id:
            if application_id not in self.store.nodes:
                self.register_application(application_id)
            self.store.put_edge(f"edge:{application_id}:HAS_PRINCIPAL:{principal_id}", "HAS_PRINCIPAL", application_id, principal_id, {})
        self.store.append_event("PRINCIPAL_REGISTERED", principal_id, {"principal_id": principal_id, "kind": kind, "namespace": namespace, "application_id": application_id})

    def set_quota(
        self,
        lane: str,
        subject_id: str,
        quota_name: str,
        *,
        period: str,
        max_usd: float | None = None,
        max_tokens: int | None = None,
        max_requests: int | None = None,
    ) -> str:
        if lane not in {"principal", "user", "key"}:
            raise RegistrationError("quota_lane_must_be_principal_user_or_key")
        if period not in {"10s", "hour", "day", "week", "month"}:
            raise RegistrationError("unsupported_quota_period")
        if max_usd is None and max_tokens is None and max_requests is None:
            raise RegistrationError("at_least_one_quota_limit_required")
        qid = f"quota:{lane}:{subject_id}:{quota_name}"
        payload: dict[str, Any] = {
            "lane": lane,
            "subject_id": subject_id,
            "quota_name": quota_name,
            "period": period,
            "registered_at_epoch": int(time.time()),
            "revision_ms": int(time.time() * 1000),
            "revoked": False,
        }
        if max_usd is not None:
            payload["max_usd"] = float(max_usd)
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        if max_requests is not None:
            payload["max_requests"] = int(max_requests)
        self.store.put_node(qid, "quota_policy", payload)
        self.store.put_edge(f"edge:{subject_id}:HAS_QUOTA_POLICY:{qid}", "HAS_QUOTA_POLICY", subject_id, qid, {})
        self.store.append_event("QUOTA_POLICY_REGISTERED", qid, payload)
        self._refresh_quota_projection(lane, subject_id)
        return qid

    def append_quota_revision(
        self,
        lane: str,
        subject_id: str,
        quota_name: str,
        *,
        period: str | None = None,
        max_usd: float | None = None,
        max_tokens: int | None = None,
        max_requests: int | None = None,
        revoked: bool = False,
        reason: str = "",
    ) -> str:
        if lane not in {"principal", "user", "key"}:
            raise RegistrationError("quota_lane_must_be_principal_user_or_key")
        if period is not None and period not in {"10s", "hour", "day", "week", "month"}:
            raise RegistrationError("unsupported_quota_period")
        if not revoked and max_usd is None and max_tokens is None and max_requests is None:
            raise RegistrationError("at_least_one_quota_limit_required")

        now_ms = int(time.time() * 1000)
        now_s = int(time.time())
        qid = f"quota:{lane}:{subject_id}:{quota_name}:rev:{now_ms}"
        while qid in self.store.nodes:
            now_ms += 1
            qid = f"quota:{lane}:{subject_id}:{quota_name}:rev:{now_ms}"
        payload: dict[str, Any] = {
            "lane": lane,
            "subject_id": subject_id,
            "quota_name": quota_name,
            "registered_at_epoch": now_s,
            "revision_ms": now_ms,
            "revoked": bool(revoked),
        }
        if period:
            payload["period"] = period
        if max_usd is not None:
            payload["max_usd"] = float(max_usd)
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        if max_requests is not None:
            payload["max_requests"] = int(max_requests)
        if reason:
            payload["reason"] = reason

        self.store.put_node(qid, "quota_policy", payload)
        self.store.put_edge(f"edge:{subject_id}:HAS_QUOTA_POLICY:{qid}", "HAS_QUOTA_POLICY", subject_id, qid, {})
        self.store.append_event("QUOTA_POLICY_REVOKED" if revoked else "QUOTA_POLICY_REGISTERED", qid, payload)
        self._refresh_quota_projection(lane, subject_id)
        return qid

    def revoke_quota(self, lane: str, subject_id: str, quota_name: str, *, reason: str = "") -> str:
        return self.append_quota_revision(
            lane,
            subject_id,
            quota_name,
            revoked=True,
            reason=reason,
        )

    def _refresh_quota_projection(self, lane: str, subject_id: str) -> None:
        rebuild = getattr(self.store, "rebuild_quota_policy_projection", None)
        if callable(rebuild):
            rebuild(lane, subject_id)

    def issue_safe_token(
        self,
        *,
        principal_id: str,
        namespace: str,
        on_behalf_of_user_id: str | None = None,
        application_id: str | None = None,
        scopes: list[str] | None = None,
        expires_at_epoch: int | None = None,
        token: str | None = None,
        token_id: str | None = None,
    ) -> IssuedToken:
        if principal_id not in self.store.nodes:
            raise RegistrationError("principal_not_registered")
        if on_behalf_of_user_id and on_behalf_of_user_id not in self.store.nodes:
            raise RegistrationError("user_not_registered")
        if application_id and application_id not in self.store.nodes:
            raise RegistrationError("application_not_registered")
        raw_token = token or make_safe_token()
        jti = token_id or hashlib.sha256(raw_token.encode("utf-8")).hexdigest()[:16]
        node_id = f"token:{jti}"
        payload: dict[str, Any] = {
            "safe_token_hash": safe_token_hash(raw_token),
            "jti": jti,
            "expires_at_epoch": expires_at_epoch,
            "scopes": scopes or ["model.invoke"],
            "namespace": namespace,
            "on_behalf_of_user_id": on_behalf_of_user_id,
            "application_id": application_id,
            "issued_at_epoch": int(time.time()),
        }
        self.store.put_node(node_id, "auth_token", payload)
        self.store.put_edge(f"edge:{node_id}:AUTHENTICATES_AS:{principal_id}", "AUTHENTICATES_AS", node_id, principal_id, {})
        if on_behalf_of_user_id:
            self.store.put_edge(f"edge:{node_id}:ON_BEHALF_OF:{on_behalf_of_user_id}", "ON_BEHALF_OF", node_id, on_behalf_of_user_id, {})
        if application_id:
            self.store.put_edge(f"edge:{node_id}:ISSUED_FOR_APPLICATION:{application_id}", "ISSUED_FOR_APPLICATION", node_id, application_id, {})
        self.store.append_event("SAFE_MODEL_TOKEN_ISSUED", node_id, {"token_id": jti, "principal_id": principal_id, "on_behalf_of_user_id": on_behalf_of_user_id, "application_id": application_id, "namespace": namespace, "scopes": scopes or ["model.invoke"]})
        return IssuedToken(raw_token, node_id, principal_id, on_behalf_of_user_id, application_id, namespace)

    def write_usage_summary(self, path: str | Path) -> None:
        data = {
            "users": sorted(n.id for n in self.store.nodes.values() if n.kind == "end_user"),
            "principals": sorted(n.id for n in self.store.nodes.values() if n.kind == "principal"),
            "applications": sorted(n.id for n in self.store.nodes.values() if n.kind == "application"),
            "quota_policies": sorted(n.id for n in self.store.nodes.values() if n.kind == "quota_policy"),
            "tokens": sorted(n.id for n in self.store.nodes.values() if n.kind == "auth_token"),
            "quota_projections": self.store.projections,
            "access_events": [e for e in self.store.events if e.get("kind", "").startswith(("ACL_", "MODEL_", "QUOTA_", "AUTH_"))],
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    @staticmethod
    def _require_prefix(value: str, prefix: str) -> None:
        if not value.startswith(prefix):
            raise RegistrationError(f"id_must_start_with_{prefix}")


def open_registration_store():
    """Open the registration store using the configured backend.

    Invariant: never silently fall back to JSONL when a serious backend mode
    was explicitly requested.
    """
    try:
        store_kind = resolve_store_backend()
    except ValueError as exc:
        raise RegistrationError(str(exc)) from exc
    if store_kind == "jsonl":
        return GraphStateStore()
    if store_kind == "postgres":
        from .postgres_state import PostgresGraphStateStore

        return PostgresGraphStateStore()
    if store_kind == "kogwistar_postgres":
        from .kogwistar_postgres_state import KogwistarPostgresGraphStateStore

        return KogwistarPostgresGraphStateStore()
    raise RegistrationError(f"unsupported_store_backend:{store_kind}")


def register_usage_demo(graph_path: str | Path = "out/registration_demo_graph.jsonl", app_key: str = "dev-registration-demo-key-change-me") -> IssuedToken:
    # Rebuild the demo graph from the default policy and then append registration records.
    path = Path(graph_path)
    if path.exists():
        path.unlink()
    import os
    old_path = os.environ.get("MODELKEYGUARD_GRAPH_PATH")
    old_key = os.environ.get("MODELKEYGUARD_GRAPH_KEY")
    os.environ["MODELKEYGUARD_GRAPH_PATH"] = str(path)
    os.environ["MODELKEYGUARD_GRAPH_KEY"] = app_key
    try:
        policy_path = Path("config/gateway_policy.json")
        policy = json.loads(policy_path.read_text()) if policy_path.exists() else {}
        store = GraphStateStore.from_policy(policy, path=path, app_key=app_key)
        reg = RegistrationService(store)
        reg.register_user("user:demo-saas-alice", "Demo SaaS Alice")
        reg.register_application("app:demo-saas", "Demo SaaS Application")
        reg.register_principal(
            "agent:demo-saas-agent",
            kind="agent",
            groups=["agent-dev"],
            namespace="tenant:kogwistar",
            application_id="app:demo-saas",
            description="OpenAI-compatible client principal for registration tutorial",
        )
        reg.set_quota("principal", "agent:demo-saas-agent", "10s", period="10s", max_usd=0.5, max_tokens=8000, max_requests=20)
        reg.set_quota("principal", "agent:demo-saas-agent", "hour", period="hour", max_usd=2.0, max_tokens=50000, max_requests=500)
        reg.set_quota("user", "user:demo-saas-alice", "hour", period="hour", max_usd=1.0, max_tokens=20000, max_requests=100)
        issued = reg.issue_safe_token(
            principal_id="agent:demo-saas-agent",
            namespace="tenant:kogwistar",
            on_behalf_of_user_id="user:demo-saas-alice",
            application_id="app:demo-saas",
            scopes=["model.invoke"],
        )
        Path("out").mkdir(exist_ok=True)
        Path("out/registration_demo_token.txt").write_text(issued.token, encoding="utf-8")
        reg.write_usage_summary("out/registration_demo_summary.json")
        return issued
    finally:
        if old_path is None:
            os.environ.pop("MODELKEYGUARD_GRAPH_PATH", None)
        else:
            os.environ["MODELKEYGUARD_GRAPH_PATH"] = old_path
        if old_key is None:
            os.environ.pop("MODELKEYGUARD_GRAPH_KEY", None)
        else:
            os.environ["MODELKEYGUARD_GRAPH_KEY"] = old_key


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="modelkeyguard registration")
    sub = p.add_subparsers(dest="cmd", required=True)
    demo = sub.add_parser("demo", help="create a SaaS user/principal/quota/safe-token registration graph")
    demo.add_argument("--graph-path", default="out/registration_demo_graph.jsonl")
    demo.add_argument("--graph-key", default="dev-registration-demo-key-change-me")
    user = sub.add_parser("register-user")
    user.add_argument("--user-id", required=True)
    user.add_argument("--display-name", default="")
    principal = sub.add_parser("register-principal")
    principal.add_argument("--principal-id", required=True)
    principal.add_argument("--kind", default="agent")
    principal.add_argument("--groups", default="")
    principal.add_argument("--namespace", default="tenant:kogwistar")
    principal.add_argument("--application-id")
    quota = sub.add_parser("set-quota")
    quota.add_argument("--lane", required=True, choices=["principal", "user", "key"])
    quota.add_argument("--subject-id", required=True)
    quota.add_argument("--quota-name", required=True)
    quota.add_argument("--period", required=True, choices=["10s", "hour", "day", "week", "month"])
    quota.add_argument("--max-usd", type=float)
    quota.add_argument("--max-tokens", type=int)
    quota.add_argument("--max-requests", type=int)
    token = sub.add_parser("issue-token")
    token.add_argument("--principal-id", required=True)
    token.add_argument("--namespace", default="tenant:kogwistar")
    token.add_argument("--on-behalf-of-user-id")
    token.add_argument("--application-id")
    token.add_argument("--scopes", default="model.invoke")
    args = p.parse_args(argv)

    if args.cmd == "demo":
        issued = register_usage_demo(args.graph_path, args.graph_key)
        print(issued.token)
        print(f"graph={args.graph_path}")
        print("token_file=out/registration_demo_token.txt")
        return 0

    try:
        store = open_registration_store()
    except RegistrationError as exc:
        print(str(exc))
        return 2
    reg = RegistrationService(store)
    if args.cmd == "register-user":
        reg.register_user(args.user_id, args.display_name)
        print(args.user_id)
    elif args.cmd == "register-principal":
        reg.register_principal(args.principal_id, kind=args.kind, groups=[g.strip() for g in args.groups.split(",") if g.strip()], namespace=args.namespace, application_id=args.application_id)
        print(args.principal_id)
    elif args.cmd == "set-quota":
        print(reg.set_quota(args.lane, args.subject_id, args.quota_name, period=args.period, max_usd=args.max_usd, max_tokens=args.max_tokens, max_requests=args.max_requests))
    elif args.cmd == "issue-token":
        issued = reg.issue_safe_token(principal_id=args.principal_id, namespace=args.namespace, on_behalf_of_user_id=args.on_behalf_of_user_id, application_id=args.application_id, scopes=[s.strip() for s in args.scopes.split(",") if s.strip()])
        print(issued.token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
