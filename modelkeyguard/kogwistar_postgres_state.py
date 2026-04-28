from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Iterable

from .graph_state import GraphEdge, GraphNode, DEFAULT_APP_KEY, iso_now, period_bucket, utc_now
from .kogwistar_import_guard import enforce_installed_kogwistar_only
from .sealed_payload import open_json, seal_json

DEFAULT_DSN = os.getenv("MODELKEYGUARD_POSTGRES_DSN", "postgresql://modelguard:modelguard@localhost:5432/modelguard")
DEFAULT_PERSIST_DIR = os.getenv("MODELKEYGUARD_KOGWISTAR_PERSIST_DIRECTORY", "out/kogwistar_runtime")
DEFAULT_EMBED_DIM = 2
MIN_EMBED_DIM = 1
MAX_EMBED_DIM = 8

QUOTA_PROJECTION_NAMESPACE = "modelkeyguard.quota_usage"
USAGE_LANE_PROJECTION_NAMESPACE = "modelkeyguard.usage_lane_head"
QUOTA_POLICY_PROJECTION_NAMESPACE = "modelkeyguard.quota_policy"
GENERIC_PROJECTION_NAMESPACE = "modelkeyguard.generic"
PROJECTION_SCHEMA_VERSION = 1

MODELKEYGUARD_DOC_ID = "modelkeyguard.graph"
RECORD_NODE = "node"
RECORD_EDGE = "edge"
RECORD_EVENT = "event"

ALL_PROJECTION_NAMESPACES = (
    GENERIC_PROJECTION_NAMESPACE,
    QUOTA_PROJECTION_NAMESPACE,
    USAGE_LANE_PROJECTION_NAMESPACE,
    QUOTA_POLICY_PROJECTION_NAMESPACE,
    "modelkeyguard.history.meta",
    "modelkeyguard.history.blob",
    "modelkeyguard.history.index",
    "modelkeyguard.history.config",
)


def resolve_kogwistar_embed_dim(value: str | None = None) -> int:
    raw = (value if value is not None else os.getenv("MODELKEYGUARD_KOGWISTAR_EMBED_DIM", str(DEFAULT_EMBED_DIM))).strip()
    try:
        dim = int(raw)
    except Exception as exc:
        raise ValueError("MODELKEYGUARD_KOGWISTAR_EMBED_DIM must be an integer") from exc
    if dim < MIN_EMBED_DIM or dim > MAX_EMBED_DIM:
        raise ValueError(f"MODELKEYGUARD_KOGWISTAR_EMBED_DIM must be between {MIN_EMBED_DIM} and {MAX_EMBED_DIM}")
    return dim


def _stable_embedding(text: str, *, dim: int, space: str) -> list[float]:
    digest = hashlib.sha256(f"{space}:{text}".encode("utf-8")).digest()
    out: list[float] = []
    for i in range(dim):
        b = digest[i % len(digest)]
        out.append(round((b / 255.0) * 2.0 - 1.0, 6))
    return out


def _space_for_node_kind(kind: str) -> str:
    if kind in {"access_conversation_event", "usage_ledger_event", "request_response_history"}:
        return "event"
    if kind.startswith("history:") or kind.startswith("event:"):
        return "event"
    return "policy"


@dataclass(frozen=True)
class _KogwistarRuntime:
    engine: Any
    meta: Any


class KogwistarPostgresGraphStateStore:
    """Delegated ModelKeyGuard store backed by installed Kogwistar Postgres primitives.

    Authority remains graph-native (nodes/edges/events), and named projections are
    served from Kogwistar meta-store projection primitives.
    """

    def __init__(self, dsn: str | None = None, app_key: str | None = None) -> None:
        self.dsn = dsn or os.getenv("MODELKEYGUARD_POSTGRES_DSN", DEFAULT_DSN)
        self.app_key = app_key or os.getenv("MODELKEYGUARD_GRAPH_KEY", DEFAULT_APP_KEY)
        self.embed_dim = resolve_kogwistar_embed_dim()
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[str, GraphEdge] = {}
        self.events: list[dict[str, Any]] = []
        self.projections: dict[str, dict[str, Any]] = {}
        self._rt = self._build_runtime()
        self.load()

    def _build_runtime(self) -> _KogwistarRuntime:
        enforce_installed_kogwistar_only()
        try:
            from kogwistar.engine_core.engine import GraphKnowledgeEngine
            from kogwistar.engine_core.engine_postgres import EnginePostgresConfig, build_postgres_backend
        except Exception as exc:
            raise RuntimeError(
                "kogwistar_postgres backend requires installed optional dependencies for Kogwistar Postgres backend "
                "(sqlalchemy + pgvector extras)."
            ) from exc

        cfg = EnginePostgresConfig(dsn=self.dsn, embedding_dim=self.embed_dim)
        backend, uow = build_postgres_backend(cfg)

        def _default_embed(texts: list[str]) -> list[list[float]]:
            return [_stable_embedding(t, dim=self.embed_dim, space="policy") for t in texts]

        engine = GraphKnowledgeEngine(
            persist_directory=DEFAULT_PERSIST_DIR,
            embedding_function=_default_embed,
            backend=backend,
        )
        # Explicitly share the backend transaction boundary used in Kogwistar pg tests.
        engine._backend_uow = uow
        return _KogwistarRuntime(engine=engine, meta=engine.meta_sqlite)

    @staticmethod
    def _meta_to_dict(meta: Any) -> dict[str, Any]:
        if isinstance(meta, dict):
            return meta
        return {}

    def _decode_payload_from_meta(self, meta: dict[str, Any]) -> dict[str, Any]:
        sealed_json = meta.get("mk_payload_sealed_json")
        if not isinstance(sealed_json, str) or not sealed_json:
            return {}
        sealed = json.loads(sealed_json)
        if not isinstance(sealed, dict):
            return {}
        return open_json(sealed, self.app_key)

    def _encode_payload_meta(self, payload: dict[str, Any]) -> str:
        sealed = seal_json(payload, self.app_key)
        return json.dumps(sealed, sort_keys=True)

    def _event_seq(self) -> int:
        rows = self._rt.meta.list_named_projections("modelkeyguard.counters")
        for row in rows:
            if str(row.get("key") or "") != "event_seq":
                continue
            payload = row.get("payload")
            if isinstance(payload, dict):
                return int(payload.get("value", 0) or 0)
        return 0

    def _set_event_seq(self, value: int) -> None:
        self._rt.meta.replace_named_projection(
            "modelkeyguard.counters",
            "event_seq",
            {"value": int(value), "updated_at_ms": int(datetime.now().timestamp() * 1000)},
            last_authoritative_seq=int(value),
            last_materialized_seq=int(value),
            projection_schema_version=PROJECTION_SCHEMA_VERSION,
            materialization_status="ready",
        )

    def _add_or_update_node_record(
        self,
        record_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        record_type: str,
        ts: str = "",
        subject: str = "",
        extra_meta: dict[str, Any] | None = None,
    ) -> None:
        try:
            from kogwistar.engine_core.models import PureChromaNode
        except Exception as exc:
            raise RuntimeError("installed kogwistar models are unavailable for kogwistar_postgres backend") from exc
        meta = {
            "mk_record_type": record_type,
            "mk_kind": kind,
            "mk_subject": subject,
            "mk_ts": ts,
            "mk_payload_sealed_json": self._encode_payload_meta(payload),
        }
        if extra_meta:
            meta.update(extra_meta)
        space = "event" if record_type == RECORD_EVENT else _space_for_node_kind(kind)
        node = PureChromaNode(
            id=record_id,
            label=kind,
            type="entity",
            summary=f"{kind}:{record_id}",
            doc_id=MODELKEYGUARD_DOC_ID,
            metadata=meta,
            embedding=_stable_embedding(f"{record_id}:{kind}", dim=self.embed_dim, space=space),
        )
        self._rt.engine.add_pure_node(node)

    def _add_or_update_edge_record(self, edge_id: str, kind: str, source: str, target: str, payload: dict[str, Any]) -> None:
        try:
            from kogwistar.engine_core.models import PureChromaEdge
        except Exception as exc:
            raise RuntimeError("installed kogwistar models are unavailable for kogwistar_postgres backend") from exc
        meta = {
            "mk_record_type": RECORD_EDGE,
            "mk_kind": kind,
            "mk_source": source,
            "mk_target": target,
            "mk_payload_sealed_json": self._encode_payload_meta(payload),
        }
        edge = PureChromaEdge(
            id=edge_id,
            label=kind,
            type="relationship",
            summary=f"{source}->{target}:{kind}",
            source_ids=[source],
            target_ids=[target],
            relation=kind,
            source_edge_ids=None,
            target_edge_ids=None,
            doc_id=MODELKEYGUARD_DOC_ID,
            metadata=meta,
            embedding=_stable_embedding(f"{edge_id}:{kind}:{source}:{target}", dim=self.embed_dim, space="policy"),
        )
        self._rt.engine.add_pure_edge(edge)

    def _list_nodes(self, record_type: str) -> list[Any]:
        rows = self._rt.engine.backend.node_get(
            where={"mk_record_type": record_type},
            include=["documents", "metadatas"],
            limit=10000,
        )
        ids = rows.get("ids") or []
        docs = rows.get("documents") or []
        metadatas = rows.get("metadatas") or []
        out: list[Any] = []
        for idx, node_id in enumerate(ids):
            meta = metadatas[idx] if idx < len(metadatas) and isinstance(metadatas[idx], dict) else {}
            if str(meta.get("mk_record_type") or "") != record_type:
                continue
            doc: dict[str, Any] = {}
            if idx < len(docs) and isinstance(docs[idx], str):
                try:
                    parsed = json.loads(docs[idx])
                    if isinstance(parsed, dict):
                        doc = parsed
                except Exception:
                    doc = {}
            if doc.get("doc_id") != MODELKEYGUARD_DOC_ID:
                continue
            out.append(SimpleNamespace(id=str(node_id), metadata=meta))
        return out

    def _list_edges(self) -> list[Any]:
        rows = self._rt.engine.backend.edge_get(
            where={"doc_id": MODELKEYGUARD_DOC_ID},
            include=["documents", "metadatas"],
            limit=10000,
        )
        ids = rows.get("ids") or []
        docs = rows.get("documents") or []
        out: list[Any] = []
        for idx, edge_id in enumerate(ids):
            doc: dict[str, Any] = {}
            if idx < len(docs) and isinstance(docs[idx], str):
                try:
                    parsed = json.loads(docs[idx])
                    if isinstance(parsed, dict):
                        doc = parsed
                except Exception:
                    doc = {}
            meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
            if str(meta.get("mk_record_type") or "") != RECORD_EDGE:
                continue
            out.append(
                SimpleNamespace(
                    id=str(edge_id),
                    metadata=meta,
                    source_ids=doc.get("source_ids") or [],
                    target_ids=doc.get("target_ids") or [],
                )
            )
        return out

    def load(self) -> None:
        self.nodes.clear()
        self.edges.clear()
        self.events.clear()
        self.projections.clear()

        for node in self._list_nodes(RECORD_NODE):
            meta = self._meta_to_dict(getattr(node, "metadata", None))
            payload = self._decode_payload_from_meta(meta)
            node_id = str(getattr(node, "id"))
            kind = str(meta.get("mk_kind") or "")
            self.nodes[node_id] = GraphNode(node_id, kind, payload)

        for edge in self._list_edges():
            meta = self._meta_to_dict(getattr(edge, "metadata", None))
            payload = self._decode_payload_from_meta(meta)
            edge_id = str(getattr(edge, "id"))
            kind = str(meta.get("mk_kind") or "")
            source = str(meta.get("mk_source") or (getattr(edge, "source_ids", [""])[0] if getattr(edge, "source_ids", None) else ""))
            target = str(meta.get("mk_target") or (getattr(edge, "target_ids", [""])[0] if getattr(edge, "target_ids", None) else ""))
            self.edges[edge_id] = GraphEdge(edge_id, kind, source, target, payload)

        events_with_seq: list[tuple[int, dict[str, Any]]] = []
        for node in self._list_nodes(RECORD_EVENT):
            meta = self._meta_to_dict(getattr(node, "metadata", None))
            payload = self._decode_payload_from_meta(meta)
            event_id = str(getattr(node, "id"))
            kind = str(meta.get("mk_kind") or "")
            subject = str(meta.get("mk_subject") or "")
            ts = str(meta.get("mk_ts") or "")
            seq = int(meta.get("mk_seq") or 0)
            events_with_seq.append(
                (
                    seq,
                    {
                        "record_type": "event",
                        "id": event_id,
                        "kind": kind,
                        "subject": subject,
                        "ts": ts,
                        "payload": payload,
                    },
                )
            )
        events_with_seq.sort(key=lambda x: (x[0], x[1]["ts"], x[1]["id"]))
        self.events.extend([e for _seq, e in events_with_seq])

        for namespace in ALL_PROJECTION_NAMESPACES:
            for row in self._rt.meta.list_named_projections(namespace):
                key = str(row.get("key") or "")
                payload = row.get("payload")
                if key and isinstance(payload, dict):
                    self.projections[f"{namespace}:{key}"] = payload

    # ------------------------------------------------------------------
    # Kogwistar-style named projection primitive.
    # ------------------------------------------------------------------
    def get_named_projection(self, namespace: str, key: str) -> dict[str, Any] | None:
        return self._rt.meta.get_named_projection(namespace, key)

    def replace_named_projection(
        self,
        namespace: str,
        key: str,
        payload: dict[str, Any],
        *,
        last_authoritative_seq: int = 0,
        last_materialized_seq: int = 0,
        projection_schema_version: int = PROJECTION_SCHEMA_VERSION,
        materialization_status: str = "ready",
    ) -> None:
        self._rt.meta.replace_named_projection(
            namespace,
            key,
            payload,
            last_authoritative_seq=last_authoritative_seq,
            last_materialized_seq=last_materialized_seq,
            projection_schema_version=projection_schema_version,
            materialization_status=materialization_status,
        )
        self.projections[f"{namespace}:{key}"] = payload

    def list_named_projections(self, namespace: str) -> list[dict[str, Any]]:
        return self._rt.meta.list_named_projections(namespace)

    def clear_named_projection(self, namespace: str, key: str) -> None:
        self._rt.meta.clear_named_projection(namespace, key)
        self.projections.pop(f"{namespace}:{key}", None)

    def clear_projection_namespace(self, namespace: str) -> None:
        rows = self._rt.meta.list_named_projections(namespace)
        for row in rows:
            key = str(row.get("key") or "")
            if key:
                self._rt.meta.clear_named_projection(namespace, key)
                self.projections.pop(f"{namespace}:{key}", None)

    # ------------------------------------------------------------------
    # Graph state API used by ModelKeyGuard.
    # ------------------------------------------------------------------
    def put_node(self, node_id: str, kind: str, payload: dict[str, Any]) -> None:
        self.nodes[node_id] = GraphNode(node_id, kind, payload)
        self._add_or_update_node_record(node_id, kind, payload, record_type=RECORD_NODE)

    def put_edge(self, edge_id: str, kind: str, source: str, target: str, payload: dict[str, Any]) -> None:
        self.edges[edge_id] = GraphEdge(edge_id, kind, source, target, payload)
        self._add_or_update_edge_record(edge_id, kind, source, target, payload)

    def append_event(self, event_type: str, subject_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        seq = self._event_seq() + 1
        event_id = f"event:{event_type}:{seq:08d}"
        ts = iso_now()
        self._add_or_update_node_record(
            event_id,
            event_type,
            payload,
            record_type=RECORD_EVENT,
            ts=ts,
            subject=subject_id,
            extra_meta={"mk_seq": seq},
        )
        self._set_event_seq(seq)
        rec = {"record_type": "event", "id": event_id, "kind": event_type, "subject": subject_id, "ts": ts, "payload": payload}
        self.events.append(rec)
        return rec

    def put_projection(self, projection_id: str, payload: dict[str, Any]) -> None:
        self.replace_named_projection(GENERIC_PROJECTION_NAMESPACE, projection_id, payload)

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
        return f"{lane}:{subject_id}:{period}:{bucket}"

    def quota_policy_projection_key(self, lane: str, subject_id: str) -> str:
        return f"{lane}:{subject_id}"

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
        projection = self.get_named_projection(QUOTA_POLICY_PROJECTION_NAMESPACE, self.quota_policy_projection_key(lane, subject_id))
        if not projection:
            return None
        payload = dict(projection.get("payload") or {})
        payload.setdefault("lane", lane)
        payload.setdefault("subject_id", subject_id)
        return payload

    def replace_quota_policy_projection(self, lane: str, subject_id: str, payload: dict[str, Any]) -> None:
        self.replace_named_projection(
            QUOTA_POLICY_PROJECTION_NAMESPACE,
            self.quota_policy_projection_key(lane, subject_id),
            payload,
        )

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
            "updated_at_ms": int(datetime.now().timestamp() * 1000),
            "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        }
        self.replace_quota_policy_projection(lane, subject_id, projection)
        return projection

    def get_quota_used(self, lane: str, subject_id: str, period: str, when: datetime | None = None) -> dict[str, float]:
        bucket = period_bucket(when or utc_now(), period)
        key = self.quota_projection_id(lane, subject_id, period, bucket)
        projection = self.get_named_projection(QUOTA_PROJECTION_NAMESPACE, key)
        payload = dict((projection or {}).get("payload") or {})
        return {"usd": float(payload.get("usd", 0.0)), "tokens": float(payload.get("tokens", 0.0)), "requests": float(payload.get("requests", 0.0))}

    def add_quota_usage(self, lane: str, subject_id: str, period: str, usd: float, tokens: int, when: datetime | None = None) -> None:
        when = when or utc_now()
        bucket = period_bucket(when, period)
        key = self.quota_projection_id(lane, subject_id, period, bucket)
        projection = self.get_named_projection(QUOTA_PROJECTION_NAMESPACE, key)
        prev = dict((projection or {}).get("payload") or {}) or {
            "lane": lane,
            "subject_id": subject_id,
            "period": period,
            "bucket": bucket,
            "usd": 0.0,
            "tokens": 0,
            "requests": 0,
        }
        nxt = dict(prev)
        nxt["usd"] = round(float(prev.get("usd", 0.0)) + float(usd), 8)
        nxt["tokens"] = int(prev.get("tokens", 0)) + int(tokens)
        nxt["requests"] = int(prev.get("requests", 0)) + 1
        self.replace_named_projection(
            QUOTA_PROJECTION_NAMESPACE,
            key,
            nxt,
            projection_schema_version=PROJECTION_SCHEMA_VERSION,
            materialization_status="ready",
        )

    def append_access_conversation_event(self, request_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        node_id = f"access:{request_id}:{len(self.events)+1:08d}"
        self.put_node(node_id, "access_conversation_event", {"request_id": request_id, "event_type": event_type, **payload})
        self.append_event(event_type, node_id, {"request_id": request_id, **payload})
        return {"node_id": node_id, "event_type": event_type, **payload}

    def append_usage_ledger_event(self, lane_subject_id: str, payload: dict[str, Any]) -> str:
        safe_subject = lane_subject_id.replace(":", "_")
        head_id = f"usage_head:{lane_subject_id}"
        head_projection = self.get_named_projection(USAGE_LANE_PROJECTION_NAMESPACE, lane_subject_id)
        head = dict((head_projection or {}).get("payload") or {}) or {"subject_id": lane_subject_id, "tail": None, "seq": 0}
        old_tail = head.get("tail")
        seq = int(head.get("seq", 0)) + 1
        usage_id = f"usage:{safe_subject}:{seq:08d}"

        self.put_node(usage_id, "usage_ledger_event", {"seq": seq, "lane_subject_id": lane_subject_id, **payload})
        if old_tail:
            self.put_edge(f"edge:{old_tail}:NEXT_USAGE:{usage_id}", "NEXT_USAGE", old_tail, usage_id, {})
        else:
            self.put_node(head_id, "usage_lane_head", {"subject_id": lane_subject_id, "tail": None, "seq": 0})
            self.put_edge(f"edge:{head_id}:FIRST_USAGE:{usage_id}", "FIRST_USAGE", head_id, usage_id, {})

        new_head = {"subject_id": lane_subject_id, "tail": usage_id, "seq": seq}
        self.put_node(head_id, "usage_lane_head", new_head)
        self.replace_named_projection(
            USAGE_LANE_PROJECTION_NAMESPACE,
            lane_subject_id,
            new_head,
            projection_schema_version=PROJECTION_SCHEMA_VERSION,
            materialization_status="ready",
        )
        return usage_id

    def secret_payload_plaintext_is_not_stored(self, forbidden: str) -> bool:
        needle = str(forbidden)
        for n in self._list_nodes(RECORD_NODE) + self._list_nodes(RECORD_EVENT):
            meta = self._meta_to_dict(getattr(n, "metadata", None))
            if needle in json.dumps(meta, sort_keys=True):
                return False
        for namespace in ALL_PROJECTION_NAMESPACES:
            for row in self._rt.meta.list_named_projections(namespace):
                if needle in json.dumps(row, sort_keys=True):
                    return False
        return True

    @classmethod
    def from_policy(cls, policy: dict[str, Any], dsn: str | None = None, app_key: str | None = None) -> "KogwistarPostgresGraphStateStore":
        store = cls(dsn, app_key)
        if store.nodes:
            return store
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
        store.append_event(
            "POLICY_GRAPH_INITIALIZED",
            "policy:version:0001",
            {"source": "config/gateway_policy.json", "store": "kogwistar_postgres", "projection_primitive": "named_projections"},
        )
        store.load()
        return store
