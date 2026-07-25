from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Iterable

from .graph_state import GraphEdge, GraphNode, iso_now, period_bucket, resolve_graph_app_key, utc_now
from .sealed_payload import GRAPH_KEY_SENTINEL_NODE_ID, open_json, seal_json

DEFAULT_DSN = os.getenv("MODELKEYGUARD_POSTGRES_DSN", "postgresql://modelguard:modelguard@localhost:5432/modelguard")
QUOTA_PROJECTION_NAMESPACE = "modelkeyguard.quota_usage"
USAGE_LANE_PROJECTION_NAMESPACE = "modelkeyguard.usage_lane_head"
QUOTA_POLICY_PROJECTION_NAMESPACE = "modelkeyguard.quota_policy"
PROJECTION_SCHEMA_VERSION = 1


class PostgresGraphStateStore:
    """Postgres-backed graph-native state store.

    The durable authority is the append-only graph/event record set. Hot-read state
    is materialized through Kogwistar-style named projections, not through
    feature-specific SQL tables. This mirrors the core primitive exposed by
    Kogwistar meta stores: get_named_projection / replace_named_projection /
    list_named_projections / clear_named_projection.
    """

    def __init__(self, dsn: str | None = None, app_key: str | None = None) -> None:
        self.dsn = dsn or os.getenv("MODELKEYGUARD_POSTGRES_DSN", DEFAULT_DSN)
        self.app_key = resolve_graph_app_key(app_key)
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[str, GraphEdge] = {}
        self.events: list[dict[str, Any]] = []
        self.projections: dict[str, dict[str, Any]] = {}
        self._ensure_schema()
        self.load()

    def _connect(self):
        try:
            import psycopg  # type: ignore
        except Exception as e:  # pragma: no cover - environment dependent
            raise RuntimeError("Postgres backend requires `pip install psycopg[binary]`") from e
        return psycopg.connect(self.dsn)

    def _ensure_schema(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                create table if not exists graph_records (
                  record_seq bigserial primary key,
                  record_type text not null,
                  id text not null,
                  kind text,
                  source text,
                  target text,
                  subject text,
                  ts timestamptz default now(),
                  payload_sealed jsonb not null
                );
                create table if not exists graph_nodes (
                  id text primary key,
                  kind text not null,
                  payload_sealed jsonb not null,
                  updated_at timestamptz default now()
                );
                create table if not exists graph_edges (
                  id text primary key,
                  kind text not null,
                  source text not null,
                  target text not null,
                  payload_sealed jsonb not null,
                  updated_at timestamptz default now()
                );
                create index if not exists idx_graph_edges_source_kind on graph_edges(source, kind);
                create index if not exists idx_graph_edges_target_kind on graph_edges(target, kind);
                create table if not exists graph_events (
                  id text primary key,
                  kind text not null,
                  subject text not null,
                  ts timestamptz default now(),
                  payload_sealed jsonb not null
                );
                create table if not exists named_projections (
                  namespace text not null,
                  key text not null,
                  payload_sealed jsonb not null,
                  last_authoritative_seq bigint not null default 0,
                  last_materialized_seq bigint not null default 0,
                  projection_schema_version integer not null default 1,
                  materialization_status text not null default 'ready',
                  updated_at_ms bigint not null default (extract(epoch from clock_timestamp()) * 1000)::bigint,
                  primary key(namespace, key)
                );
                create index if not exists idx_named_projections_namespace on named_projections(namespace, updated_at_ms);
                """
            )

    # ------------------------------------------------------------------
    # Kogwistar-style named projection primitive.
    # ------------------------------------------------------------------
    def get_named_projection(self, namespace: str, key: str) -> dict[str, Any] | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """select namespace, key, payload_sealed, last_authoritative_seq,
                          last_materialized_seq, projection_schema_version,
                          materialization_status, updated_at_ms
                   from named_projections where namespace=%s and key=%s""",
                (namespace, key),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "namespace": row[0],
            "key": row[1],
            "payload": open_json(dict(row[2]), self.app_key),
            "last_authoritative_seq": int(row[3]),
            "last_materialized_seq": int(row[4]),
            "projection_schema_version": int(row[5]),
            "materialization_status": str(row[6]),
            "updated_at_ms": int(row[7]),
        }

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
        sealed = seal_json(payload, self.app_key)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """insert into named_projections(
                     namespace,key,payload_sealed,last_authoritative_seq,
                     last_materialized_seq,projection_schema_version,
                     materialization_status,updated_at_ms)
                   values (%s,%s,%s::jsonb,%s,%s,%s,%s,(extract(epoch from clock_timestamp()) * 1000)::bigint)
                   on conflict(namespace,key) do update set
                     payload_sealed=excluded.payload_sealed,
                     last_authoritative_seq=excluded.last_authoritative_seq,
                     last_materialized_seq=excluded.last_materialized_seq,
                     projection_schema_version=excluded.projection_schema_version,
                     materialization_status=excluded.materialization_status,
                     updated_at_ms=excluded.updated_at_ms""",
                (
                    namespace,
                    key,
                    json.dumps(sealed),
                    int(last_authoritative_seq),
                    int(last_materialized_seq),
                    int(projection_schema_version),
                    materialization_status,
                ),
            )
            cur.execute(
                "insert into graph_records(record_type,id,kind,payload_sealed) values ('projection',%s,%s,%s::jsonb)",
                (f"{namespace}:{key}", namespace, json.dumps(sealed)),
            )
        self.projections[f"{namespace}:{key}"] = payload

    def compare_and_swap_named_projections(self, updates: list[dict[str, Any]]) -> bool:
        """Atomically compare and replace several projections in one transaction.

        Rows lock in deterministic namespace/key order.  Every compare runs before
        any write, so one stale lane aborts the complete admission update.
        """
        if not updates:
            return True
        rows = sorted(updates, key=lambda item: (str(item["namespace"]), str(item["key"])))
        if len({(str(item["namespace"]), str(item["key"])) for item in rows}) != len(rows):
            raise ValueError("duplicate named projection key")
        staged: list[tuple[dict[str, Any], tuple[Any, ...] | None]] = []
        with self._connect() as conn, conn.cursor() as cur:
            for item in rows:
                namespace, key = str(item["namespace"]), str(item["key"])
                cur.execute(
                    """select payload_sealed, last_authoritative_seq,
                              last_materialized_seq
                       from named_projections
                       where namespace=%s and key=%s
                       for update""",
                    (namespace, key),
                )
                current = cur.fetchone()
                expected_hash = item.get("expected_payload_hash")
                expected_a = item.get("expected_last_authoritative_seq")
                expected_m = item.get("expected_last_materialized_seq")
                if expected_hash is not None:
                    if current is None or open_json(dict(current[0]), self.app_key) != expected_hash:
                        return False
                elif expected_a is None and expected_m is None:
                    if current is not None:
                        return False
                elif current is None or int(current[1]) != int(expected_a) or int(current[2]) != int(expected_m):
                    return False
                staged.append((item, current))

            for item, _current in staged:
                namespace, key = str(item["namespace"]), str(item["key"])
                payload = dict(item["payload"])
                sealed = seal_json(payload, self.app_key)
                authoritative = int(item.get("last_authoritative_seq", 0))
                materialized = int(item.get("last_materialized_seq", 0))
                schema_version = int(item.get("projection_schema_version", PROJECTION_SCHEMA_VERSION))
                status = str(item.get("materialization_status", "ready"))
                cur.execute(
                    """insert into named_projections(
                             namespace,key,payload_sealed,last_authoritative_seq,
                             last_materialized_seq,projection_schema_version,
                             materialization_status,updated_at_ms)
                       values (%s,%s,%s::jsonb,%s,%s,%s,%s,
                               (extract(epoch from clock_timestamp()) * 1000)::bigint)
                       on conflict(namespace,key) do update set
                         payload_sealed=excluded.payload_sealed,
                         last_authoritative_seq=excluded.last_authoritative_seq,
                         last_materialized_seq=excluded.last_materialized_seq,
                         projection_schema_version=excluded.projection_schema_version,
                         materialization_status=excluded.materialization_status,
                         updated_at_ms=excluded.updated_at_ms""",
                    (namespace, key, json.dumps(sealed), authoritative, materialized, schema_version, status),
                )
                cur.execute(
                    "insert into graph_records(record_type,id,kind,payload_sealed) values ('projection',%s,%s,%s::jsonb)",
                    (f"{namespace}:{key}", namespace, json.dumps(sealed)),
                )
        for item, _current in staged:
            self.projections[f"{item['namespace']}:{item['key']}"] = dict(item["payload"])
        return True

    def list_named_projections(self, namespace: str) -> list[dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """select key, payload_sealed, last_authoritative_seq,
                          last_materialized_seq, projection_schema_version,
                          materialization_status, updated_at_ms
                   from named_projections where namespace=%s order by updated_at_ms, key""",
                (namespace,),
            )
            rows = cur.fetchall()
        return [
            {
                "namespace": namespace,
                "key": row[0],
                "payload": open_json(dict(row[1]), self.app_key),
                "last_authoritative_seq": int(row[2]),
                "last_materialized_seq": int(row[3]),
                "projection_schema_version": int(row[4]),
                "materialization_status": str(row[5]),
                "updated_at_ms": int(row[6]),
            }
            for row in rows
        ]

    def clear_named_projection(self, namespace: str, key: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("delete from named_projections where namespace=%s and key=%s", (namespace, key))
        self.projections.pop(f"{namespace}:{key}", None)

    def clear_projection_namespace(self, namespace: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("delete from named_projections where namespace=%s", (namespace,))
        for key in list(self.projections):
            if key.startswith(f"{namespace}:"):
                self.projections.pop(key, None)

    # ------------------------------------------------------------------
    # Graph state API used by ModelKeyGuard.
    # ------------------------------------------------------------------
    def load(self) -> None:
        self.nodes.clear(); self.edges.clear(); self.events.clear(); self.projections.clear()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("select id, kind, payload_sealed from graph_nodes")
            for node_id, kind, sealed in cur.fetchall():
                payload = open_json(dict(sealed), self.app_key)
                self.nodes[node_id] = GraphNode(node_id, kind, payload)
                if node_id == GRAPH_KEY_SENTINEL_NODE_ID:
                    from .graph_key_contract import seed_graph_key_sentinel_from_storage_payload

                    seed_graph_key_sentinel_from_storage_payload(payload, self.app_key)
            cur.execute("select id, kind, source, target, payload_sealed from graph_edges")
            for edge_id, kind, source, target, sealed in cur.fetchall():
                self.edges[edge_id] = GraphEdge(edge_id, kind, source, target, open_json(dict(sealed), self.app_key))
            cur.execute("select id, kind, subject, ts, payload_sealed from graph_events order by ts, id")
            for event_id, kind, subject, ts, sealed in cur.fetchall():
                self.events.append({"record_type": "event", "id": event_id, "kind": kind, "subject": subject, "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts), "payload": open_json(dict(sealed), self.app_key)})
            cur.execute("select namespace,key,payload_sealed from named_projections")
            for namespace, key, sealed in cur.fetchall():
                self.projections[f"{namespace}:{key}"] = open_json(dict(sealed), self.app_key)

    def put_node(self, node_id: str, kind: str, payload: dict[str, Any]) -> None:
        existing = self.nodes.get(node_id)
        if node_id == GRAPH_KEY_SENTINEL_NODE_ID and existing is not None:
            if existing.kind != kind or existing.payload != payload:
                raise ValueError("graph key sentinel node cannot be overwritten")
            return
        sealed = seal_json(payload, self.app_key)
        self.nodes[node_id] = GraphNode(node_id, kind, payload)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """insert into graph_nodes(id,kind,payload_sealed) values (%s,%s,%s::jsonb)
                   on conflict(id) do update set kind=excluded.kind, payload_sealed=excluded.payload_sealed, updated_at=now()""",
                (node_id, kind, json.dumps(sealed)),
            )
            cur.execute("insert into graph_records(record_type,id,kind,payload_sealed) values ('node',%s,%s,%s::jsonb)", (node_id, kind, json.dumps(sealed)))

    def put_edge(self, edge_id: str, kind: str, source: str, target: str, payload: dict[str, Any]) -> None:
        sealed = seal_json(payload, self.app_key)
        self.edges[edge_id] = GraphEdge(edge_id, kind, source, target, payload)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """insert into graph_edges(id,kind,source,target,payload_sealed) values (%s,%s,%s,%s,%s::jsonb)
                   on conflict(id) do update set kind=excluded.kind, source=excluded.source, target=excluded.target, payload_sealed=excluded.payload_sealed, updated_at=now()""",
                (edge_id, kind, source, target, json.dumps(sealed)),
            )
            cur.execute("insert into graph_records(record_type,id,kind,source,target,payload_sealed) values ('edge',%s,%s,%s,%s,%s::jsonb)", (edge_id, kind, source, target, json.dumps(sealed)))

    def append_event(self, event_type: str, subject_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        event_id = f"event:{event_type}:{len(self.events)+1:08d}"
        sealed = seal_json(payload, self.app_key)
        ts = iso_now()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("insert into graph_events(id,kind,subject,ts,payload_sealed) values (%s,%s,%s,%s,%s::jsonb)", (event_id, event_type, subject_id, ts, json.dumps(sealed)))
            cur.execute("insert into graph_records(record_type,id,kind,subject,ts,payload_sealed) values ('event',%s,%s,%s,%s,%s::jsonb)", (event_id, event_type, subject_id, ts, json.dumps(sealed)))
        rec = {"record_type": "event", "id": event_id, "kind": event_type, "subject": subject_id, "ts": ts, "payload": payload}
        self.events.append(rec)
        return rec

    def put_projection(self, projection_id: str, payload: dict[str, Any]) -> None:
        # Compatibility shim for the JSONL store API: treat the caller-supplied id
        # as a generic named projection key. No feature-specific SQL table is used.
        self.replace_named_projection("modelkeyguard.generic", projection_id, payload)

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
        lock_key = abs(hash((QUOTA_PROJECTION_NAMESPACE, key))) % (2**31)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("select pg_advisory_xact_lock(%s)", (lock_key,))
            cur.execute(
                "select payload_sealed from named_projections where namespace=%s and key=%s for update",
                (QUOTA_PROJECTION_NAMESPACE, key),
            )
            row = cur.fetchone()
            prev = open_json(dict(row[0]), self.app_key) if row else {"lane": lane, "subject_id": subject_id, "period": period, "bucket": bucket, "usd": 0.0, "tokens": 0, "requests": 0}
            nxt = dict(prev)
            nxt["usd"] = round(float(prev.get("usd", 0.0)) + float(usd), 8)
            nxt["tokens"] = int(prev.get("tokens", 0)) + int(tokens)
            nxt["requests"] = int(prev.get("requests", 0)) + 1
            sealed = seal_json(nxt, self.app_key)
            cur.execute(
                """insert into named_projections(namespace,key,payload_sealed,projection_schema_version,materialization_status,updated_at_ms)
                   values (%s,%s,%s::jsonb,%s,'ready',(extract(epoch from clock_timestamp()) * 1000)::bigint)
                   on conflict(namespace,key) do update set
                     payload_sealed=excluded.payload_sealed,
                     projection_schema_version=excluded.projection_schema_version,
                     materialization_status='ready',
                     updated_at_ms=excluded.updated_at_ms""",
                (QUOTA_PROJECTION_NAMESPACE, key, json.dumps(sealed), PROJECTION_SCHEMA_VERSION),
            )
            cur.execute(
                "insert into graph_records(record_type,id,kind,payload_sealed) values ('projection',%s,%s,%s::jsonb)",
                (f"{QUOTA_PROJECTION_NAMESPACE}:{key}", QUOTA_PROJECTION_NAMESPACE, json.dumps(sealed)),
            )
        self.projections[f"{QUOTA_PROJECTION_NAMESPACE}:{key}"] = nxt

    def append_access_conversation_event(self, request_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        node_id = f"access:{request_id}:{len(self.events)+1:08d}"
        self.put_node(node_id, "access_conversation_event", {"request_id": request_id, "event_type": event_type, **payload})
        self.append_event(event_type, node_id, {"request_id": request_id, **payload})
        return {"node_id": node_id, "event_type": event_type, **payload}

    def append_usage_ledger_event(self, lane_subject_id: str, payload: dict[str, Any]) -> str:
        safe_subject = lane_subject_id.replace(":", "_")
        head_id = f"usage_head:{lane_subject_id}"
        lock_key = abs(hash((USAGE_LANE_PROJECTION_NAMESPACE, lane_subject_id))) % (2**31)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("select pg_advisory_xact_lock(%s)", (lock_key,))
            cur.execute(
                "select payload_sealed from named_projections where namespace=%s and key=%s for update",
                (USAGE_LANE_PROJECTION_NAMESPACE, lane_subject_id),
            )
            row = cur.fetchone()
            head = open_json(dict(row[0]), self.app_key) if row else {"subject_id": lane_subject_id, "tail": None, "seq": 0}
            old_tail = head.get("tail")
            seq = int(head.get("seq", 0)) + 1
            usage_id = f"usage:{safe_subject}:{seq:08d}"

            sealed_node = seal_json({"seq": seq, "lane_subject_id": lane_subject_id, **payload}, self.app_key)
            cur.execute("insert into graph_nodes(id,kind,payload_sealed) values (%s,'usage_ledger_event',%s::jsonb) on conflict(id) do update set payload_sealed=excluded.payload_sealed, updated_at=now()", (usage_id, json.dumps(sealed_node)))
            cur.execute("insert into graph_records(record_type,id,kind,payload_sealed) values ('node',%s,'usage_ledger_event',%s::jsonb)", (usage_id, json.dumps(sealed_node)))
            if old_tail:
                edge_id = f"edge:{old_tail}:NEXT_USAGE:{usage_id}"; kind = "NEXT_USAGE"; src = old_tail
            else:
                # Also materialize a graph node for the lane head; the hot tail pointer is the named projection.
                sealed_initial_head = seal_json({"subject_id": lane_subject_id, "tail": None, "seq": 0}, self.app_key)
                cur.execute("insert into graph_nodes(id,kind,payload_sealed) values (%s,'usage_lane_head',%s::jsonb) on conflict(id) do nothing", (head_id, json.dumps(sealed_initial_head)))
                edge_id = f"edge:{head_id}:FIRST_USAGE:{usage_id}"; kind = "FIRST_USAGE"; src = head_id
            sealed_edge = seal_json({}, self.app_key)
            cur.execute("insert into graph_edges(id,kind,source,target,payload_sealed) values (%s,%s,%s,%s,%s::jsonb) on conflict(id) do nothing", (edge_id, kind, src, usage_id, json.dumps(sealed_edge)))
            cur.execute("insert into graph_records(record_type,id,kind,source,target,payload_sealed) values ('edge',%s,%s,%s,%s,%s::jsonb)", (edge_id, kind, src, usage_id, json.dumps(sealed_edge)))

            new_head = {"subject_id": lane_subject_id, "tail": usage_id, "seq": seq}
            sealed_head = seal_json(new_head, self.app_key)
            cur.execute("insert into graph_nodes(id,kind,payload_sealed) values (%s,'usage_lane_head',%s::jsonb) on conflict(id) do update set payload_sealed=excluded.payload_sealed, updated_at=now()", (head_id, json.dumps(sealed_head)))
            cur.execute(
                """insert into named_projections(namespace,key,payload_sealed,projection_schema_version,materialization_status,updated_at_ms)
                   values (%s,%s,%s::jsonb,%s,'ready',(extract(epoch from clock_timestamp()) * 1000)::bigint)
                   on conflict(namespace,key) do update set payload_sealed=excluded.payload_sealed, updated_at_ms=excluded.updated_at_ms""",
                (USAGE_LANE_PROJECTION_NAMESPACE, lane_subject_id, json.dumps(sealed_head), PROJECTION_SCHEMA_VERSION),
            )
            cur.execute("insert into graph_records(record_type,id,kind,payload_sealed) values ('projection',%s,%s,%s::jsonb)", (f"{USAGE_LANE_PROJECTION_NAMESPACE}:{lane_subject_id}", USAGE_LANE_PROJECTION_NAMESPACE, json.dumps(sealed_head)))
        self.load()
        return usage_id

    def secret_payload_plaintext_is_not_stored(self, forbidden: str) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("select count(*) from graph_records where payload_sealed::text like %s", (f"%{forbidden}%",))
            records_count = int(cur.fetchone()[0])
            cur.execute("select count(*) from named_projections where payload_sealed::text like %s", (f"%{forbidden}%",))
            projections_count = int(cur.fetchone()[0])
        return records_count == 0 and projections_count == 0

    @classmethod
    def from_policy(cls, policy: dict[str, Any], dsn: str | None = None, app_key: str | None = None) -> "PostgresGraphStateStore":
        store = cls(dsn, app_key)
        existing_nodes = [node_id for node_id in store.nodes if node_id != GRAPH_KEY_SENTINEL_NODE_ID]
        if existing_nodes:
            sentinel = store.nodes.get(GRAPH_KEY_SENTINEL_NODE_ID)
            if sentinel is None:
                raise ValueError("graph key sentinel node missing from existing graph state")
            from .graph_key_contract import seed_graph_key_sentinel_from_storage_payload

            seed_graph_key_sentinel_from_storage_payload(sentinel.payload, store.app_key)
            return store
        if GRAPH_KEY_SENTINEL_NODE_ID in store.nodes:
            from .graph_key_contract import seed_graph_key_sentinel_from_storage_payload

            seed_graph_key_sentinel_from_storage_payload(store.nodes[GRAPH_KEY_SENTINEL_NODE_ID].payload, store.app_key)
        else:
            from .graph_key_contract import ensure_graph_key_sentinel_node

            ensure_graph_key_sentinel_node(store, store.app_key)
        # same graph seed shape as the JSONL store
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
        store.append_event("POLICY_GRAPH_INITIALIZED", "policy:version:0001", {"source": "config/gateway_policy.json", "store": "postgres", "projection_primitive": "named_projections"})
        store.load()
        return store
