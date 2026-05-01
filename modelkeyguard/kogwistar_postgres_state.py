from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Iterable

from .graph_state import GraphEdge, GraphNode, iso_now, period_bucket, resolve_graph_app_key, utc_now
from .kogwistar_import_guard import enforce_installed_kogwistar_only
from .sealed_payload import open_json, seal_json

DEFAULT_DSN = os.getenv("MODELKEYGUARD_POSTGRES_DSN", "postgresql://modelguard:modelguard@localhost:5432/modelguard")
DEFAULT_EMBED_DIM = 2
MIN_EMBED_DIM = 1
MAX_EMBED_DIM = 8

QUOTA_PROJECTION_NAMESPACE = "modelkeyguard.quota_usage"
USAGE_LANE_PROJECTION_NAMESPACE = "modelkeyguard.usage_lane_head"
QUOTA_POLICY_PROJECTION_NAMESPACE = "modelkeyguard.quota_policy"
GENERIC_PROJECTION_NAMESPACE = "modelkeyguard.generic"
CURRENT_NODE_PROJECTION_NAMESPACE = "modelkeyguard.current_node"
CURRENT_EDGE_PROJECTION_NAMESPACE = "modelkeyguard.current_edge"
PROJECTION_SCHEMA_VERSION = 1

MODELKEYGUARD_DOC_ID = "modelkeyguard.graph"
RECORD_NODE = "node"
RECORD_EDGE = "edge"
RECORD_EVENT = "event"
RECORD_NODE_REVISION = "node_revision"
RECORD_EDGE_REVISION = "edge_revision"

ALL_PROJECTION_NAMESPACES = (
    GENERIC_PROJECTION_NAMESPACE,
    CURRENT_NODE_PROJECTION_NAMESPACE,
    CURRENT_EDGE_PROJECTION_NAMESPACE,
    QUOTA_PROJECTION_NAMESPACE,
    USAGE_LANE_PROJECTION_NAMESPACE,
    QUOTA_POLICY_PROJECTION_NAMESPACE,
    "modelkeyguard.history.meta",
    "modelkeyguard.history.blob",
    "modelkeyguard.history.index",
    "modelkeyguard.history.config",
    "modelkeyguard.review.checkpoint",
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


def _kogwistar_stable_json(value: Any) -> str:
    try:
        from kogwistar.runtime.serialize import stable_json_dumps

        return stable_json_dumps(value)
    except Exception:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_kogwistar_stable_json(value).encode("utf-8")).hexdigest()


def canonical_node_hash(kind: str, payload: dict[str, Any]) -> str:
    return _canonical_hash({"kind": kind, "payload": payload})


def canonical_edge_hash(kind: str, source: str, target: str, payload: dict[str, Any]) -> str:
    return _canonical_hash({"kind": kind, "source": source, "target": target, "payload": payload})


def _safe_record_id_part(value: str) -> str:
    return value.replace(":", "_").replace("/", "_")


@dataclass(frozen=True)
class _KogwistarRuntime:
    engine: Any
    meta: Any


class _PostgresKogwistarSearchIndexService:
    """Postgres-backed replacement for Kogwistar's SQLite FTS search index."""

    def __init__(self, engine: Any, index_db_path: str) -> None:
        self._e = engine
        self.index_db_path = ""
        self.schema = str(getattr(getattr(engine, "backend", None), "schema", "public") or "public")
        if not self.schema.replace("_", "").isalnum():
            raise ValueError(f"invalid postgres search-index schema: {self.schema!r}")
        self.ensure_initialized()

    @property
    def _sa_engine(self) -> Any:
        meta = getattr(self._e, "meta_sqlite", None)
        engine = getattr(meta, "engine", None) or getattr(getattr(self._e, "backend", None), "engine", None)
        if engine is None:
            raise RuntimeError("Postgres search index requires a Kogwistar Postgres engine")
        return engine

    def _execute(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        import sqlalchemy as sa  # type: ignore

        with self._sa_engine.begin() as conn:
            result = conn.execute(sa.text(sql), params or {})
            if result.returns_rows:
                return list(result.mappings())
            return []

    def ensure_initialized(self) -> None:
        schema = self.schema
        for statement in (
            f"CREATE SCHEMA IF NOT EXISTS {schema}",
            f"""
            CREATE TABLE IF NOT EXISTS {schema}.semantic_index (
                id BIGSERIAL PRIMARY KEY,
                index_key TEXT NOT NULL UNIQUE,
                node_id TEXT NOT NULL,
                canonical_title TEXT NOT NULL,
                keywords TEXT NOT NULL DEFAULT '',
                aliases TEXT NOT NULL DEFAULT '',
                provision TEXT NOT NULL,
                document_id TEXT NULL,
                search_text TSVECTOR GENERATED ALWAYS AS (
                    to_tsvector(
                        'simple',
                        coalesce(canonical_title, '') || ' ' ||
                        coalesce(keywords, '') || ' ' ||
                        coalesce(aliases, '') || ' ' ||
                        coalesce(provision, '') || ' ' ||
                        coalesce(document_id, '')
                    )
                ) STORED,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            f"""
            CREATE INDEX IF NOT EXISTS idx_semantic_index_search_text
                ON {schema}.semantic_index USING GIN(search_text)
            """,
            f"""
            CREATE INDEX IF NOT EXISTS idx_semantic_index_node_id
                ON {schema}.semantic_index(node_id)
            """,
        ):
            self._execute(statement)

    def upsert_entries(self, items: list[Any]) -> None:
        from kogwistar.cdc.change_event import EntityRefModel
        from kogwistar.engine_core.search_index.models import build_embedding_text, make_index_key_for_item

        for item in items:
            index_key = make_index_key_for_item(item)
            keywords = " ".join(item.keywords or [])
            aliases = " ".join(item.aliases or [])
            self._execute(
                f"""
                INSERT INTO {self.schema}.semantic_index
                    (index_key, node_id, canonical_title, keywords, aliases, provision, document_id)
                VALUES
                    (:index_key, :node_id, :canonical_title, :keywords, :aliases, :provision, :document_id)
                ON CONFLICT(index_key) DO UPDATE SET
                    node_id = EXCLUDED.node_id,
                    canonical_title = EXCLUDED.canonical_title,
                    keywords = EXCLUDED.keywords,
                    aliases = EXCLUDED.aliases,
                    provision = EXCLUDED.provision,
                    document_id = EXCLUDED.document_id,
                    updated_at = NOW()
                """,
                {
                    "index_key": index_key,
                    "node_id": item.node_id,
                    "canonical_title": item.canonical_title,
                    "keywords": keywords,
                    "aliases": aliases,
                    "provision": item.provision,
                    "document_id": item.doc_id,
                },
            )

            node_index_upsert = getattr(getattr(self._e, "backend", None), "node_index_upsert", None)
            if callable(node_index_upsert):
                node_index_upsert(
                    ids=[f"idx:{index_key}"],
                    metadatas=[
                        {
                            "index_key": index_key,
                            "target_node_id": item.node_id,
                            "canonical_title": item.canonical_title,
                            "provision": item.provision,
                            "keywords": json.dumps(item.keywords),
                            "aliases": json.dumps(item.aliases),
                            "doc_id": item.doc_id,
                        }
                    ],
                    documents=[build_embedding_text(item)],
                )

            payload = {
                "node_id": item.node_id,
                "canonical_title": item.canonical_title,
                "keywords": item.keywords,
                "aliases": item.aliases,
                "provision": item.provision,
                "doc_id": item.doc_id,
            }
            self._e._append_event_for_entity(
                namespace=self._e.namespace,
                entity_kind="search_index",
                entity_id=index_key,
                op="search_index.upsert",
                payload=payload,
            )
            self._e._emit_change(
                op="search_index.upsert",
                entity=EntityRefModel(
                    kind="search_index",
                    id=index_key,
                    kg_graph_type=self._e.kg_graph_type,
                    url=None,
                ),
                payload=payload,
            )

    def search_hybrid(self, q: str, limit: int = 10, resolve_node: bool = False) -> dict[str, Any]:
        from kogwistar.engine_core.search_index.models import make_index_key

        rows = self._execute(
            f"""
            SELECT
                index_key,
                node_id,
                canonical_title,
                provision,
                document_id,
                ts_rank_cd(search_text, plainto_tsquery('simple', :query)) AS fts_score
            FROM {self.schema}.semantic_index
            WHERE search_text @@ plainto_tsquery('simple', :query)
            ORDER BY fts_score DESC, updated_at DESC
            LIMIT :limit
            """,
            {"query": q, "limit": int(limit)},
        )
        fts_norm = self._normalize_rank_rows(rows)
        combined: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row["index_key"])
            combined[key] = {
                "index_key": key,
                "node_id": str(row["node_id"]),
                "canonical_title": str(row["canonical_title"]),
                "provision": str(row["provision"]),
                "document_id": row["document_id"],
                "fts_score": fts_norm.get(key, 0.0),
                "vec_score": 0.0,
            }

        vector_results = self._query_vector_index(q, limit)
        vec_norm = self._normalize_vector_results(vector_results)
        vec_ids = vector_results.get("ids") or [[]]
        vec_metas = vector_results.get("metadatas") or [[]]
        if vec_ids and vec_ids[0] and vec_metas and vec_metas[0]:
            for idx, _ in enumerate(vec_ids[0]):
                meta = vec_metas[0][idx] or {}
                key = str(meta.get("index_key") or "")
                if not key:
                    node_id = str(meta.get("target_node_id") or "")
                    canonical_title = str(meta.get("canonical_title") or "")
                    provision = str(meta.get("provision") or "")
                    if not node_id or not canonical_title:
                        continue
                    key = make_index_key(node_id, canonical_title, provision)
                if key not in combined:
                    combined[key] = {
                        "index_key": key,
                        "node_id": str(meta.get("target_node_id") or ""),
                        "canonical_title": str(meta.get("canonical_title") or ""),
                        "provision": str(meta.get("provision") or ""),
                        "document_id": meta.get("doc_id"),
                        "fts_score": 0.0,
                        "vec_score": vec_norm.get(key, 0.0),
                    }
                else:
                    combined[key]["vec_score"] = vec_norm.get(key, 0.0)

        for row in combined.values():
            row["hybrid_score"] = 0.6 * row["fts_score"] + 0.4 * row["vec_score"]
        ranked = sorted(combined.values(), key=lambda x: x["hybrid_score"], reverse=True)
        if resolve_node:
            return self._resolve_nodes(ranked[:limit], q)
        return {"query": q, "results": ranked[:limit]}

    def _query_vector_index(self, q: str, limit: int) -> dict[str, Any]:
        node_index_query = getattr(getattr(self._e, "backend", None), "node_index_query", None)
        if not callable(node_index_query):
            return {}
        return node_index_query(query_texts=[q], n_results=limit) or {}

    @staticmethod
    def _normalize_rank_rows(rows: list[Any]) -> dict[str, float]:
        if not rows:
            return {}
        raw = [float(r["fts_score"] or 0.0) for r in rows]
        min_s = min(raw)
        max_s = max(raw)
        out: dict[str, float] = {}
        for row, score in zip(rows, raw):
            key = str(row["index_key"])
            if max_s == min_s:
                out[key] = 1.0 if score > 0 else 0.0
            else:
                out[key] = max(0.0, min(1.0, (score - min_s) / (max_s - min_s)))
        return out

    @staticmethod
    def _normalize_vector_results(vr: dict[str, Any]) -> dict[str, float]:
        ids = vr.get("ids") or [[]]
        metas = vr.get("metadatas") or [[]]
        distances = vr.get("distances") or [[]]
        if not ids or not ids[0] or not metas or not metas[0] or not distances or not distances[0]:
            return {}
        raw = [float(d) for d in distances[0]]
        min_d = min(raw)
        max_d = max(raw)
        out: dict[str, float] = {}
        for idx, _ in enumerate(ids[0]):
            meta = metas[0][idx] or {}
            key = str(meta.get("index_key") or "")
            if not key:
                continue
            dist = float(distances[0][idx])
            out[key] = 1.0 if max_d == min_d else max(0.0, min(1.0, (max_d - dist) / (max_d - min_d)))
        return out

    def _resolve_nodes(self, ranked_rows: list[dict[str, Any]], q: str) -> dict[str, Any]:
        unique_node_ids: list[str] = []
        seen: set[str] = set()
        for row in ranked_rows:
            node_id = row["node_id"]
            if node_id and node_id not in seen:
                seen.add(node_id)
                unique_node_ids.append(node_id)
        res = self._e.backend.node_get(ids=unique_node_ids, include=["documents", "metadatas"]) or {}
        rows_by_node_id: dict[str, dict[str, Any]] = {}
        for i, node_id in enumerate(res.get("ids") or []):
            rows_by_node_id[str(node_id)] = {
                "documents": (res.get("documents") or [None])[i] if i < len(res.get("documents") or []) else None,
                "metadatas": (res.get("metadatas") or [None])[i] if i < len(res.get("metadatas") or []) else None,
            }
        output = []
        for row in ranked_rows:
            enriched = dict(row)
            enriched.update(rows_by_node_id.get(row["node_id"], {}))
            output.append(enriched)
        return {"query": q, "results": output}


class KogwistarPostgresGraphStateStore:
    """Delegated ModelKeyGuard store backed by installed Kogwistar Postgres primitives.

    Authority remains graph-native (nodes/edges/events), and named projections are
    served from Kogwistar meta-store projection primitives.
    """

    def __init__(self, dsn: str | None = None, app_key: str | None = None) -> None:
        self.dsn = dsn or os.getenv("MODELKEYGUARD_POSTGRES_DSN", DEFAULT_DSN)
        self.app_key = resolve_graph_app_key(app_key)
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
            import kogwistar.engine_core.engine as engine_module
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

        original_search_index = getattr(engine_module, "SearchIndexService", None)
        engine_module.SearchIndexService = _PostgresKogwistarSearchIndexService
        try:
            engine = engine_module.GraphKnowledgeEngine(
                persist_directory=None,
                embedding_function=_default_embed,
                backend=backend,
            )
        finally:
            if original_search_index is not None:
                engine_module.SearchIndexService = original_search_index
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
        return self._counter("event_seq")

    def _counter(self, key: str) -> int:
        rows = self._rt.meta.list_named_projections("modelkeyguard.counters")
        for row in rows:
            if str(row.get("key") or "") != key:
                continue
            payload = row.get("payload")
            if isinstance(payload, dict):
                return int(payload.get("value", 0) or 0)
        return 0

    def _set_event_seq(self, value: int) -> None:
        self._set_counter("event_seq", value)

    def _set_counter(self, key: str, value: int) -> None:
        self._rt.meta.replace_named_projection(
            "modelkeyguard.counters",
            key,
            {"value": int(value), "updated_at_ms": int(datetime.now().timestamp() * 1000)},
            last_authoritative_seq=int(value),
            last_materialized_seq=int(value),
            projection_schema_version=PROJECTION_SCHEMA_VERSION,
            materialization_status="ready",
        )

    def _next_revision_seq(self) -> int:
        seq = self._counter("graph_revision_seq") + 1
        self._set_counter("graph_revision_seq", seq)
        return seq

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
        embedding_space: str | None = None,
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
        space = embedding_space or ("event" if record_type == RECORD_EVENT else _space_for_node_kind(kind))
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

    def _append_graph_revision(
        self,
        *,
        record_type: str,
        revision_id: str,
        revision_kind: str,
        subject_id: str,
        payload: dict[str, Any],
    ) -> None:
        self._add_or_update_node_record(
            revision_id,
            revision_kind,
            payload,
            record_type=record_type,
            ts=iso_now(),
            subject=subject_id,
            extra_meta={
                "mk_revision_kind": revision_kind,
                "mk_current": False,
                "mk_tombstone": bool(payload.get("tombstone")),
                "mk_entity_id": subject_id,
            },
            embedding_space="event",
        )

    def _revision_id(self, prefix: str, entity_id: str, seq: int, content_hash: str) -> str:
        digest = content_hash.split(":", 1)[-1][:16]
        return f"revision:{prefix}:{_safe_record_id_part(entity_id)}:{seq:08d}:{digest}"

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

    def load(self) -> None:
        self.nodes.clear()
        self.edges.clear()
        self.events.clear()
        self.projections.clear()

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
                    if namespace == CURRENT_NODE_PROJECTION_NAMESPACE:
                        node_id = str(payload.get("id") or key)
                        node_payload = payload.get("payload")
                        if isinstance(node_payload, dict):
                            self.nodes[node_id] = GraphNode(node_id, str(payload.get("kind") or ""), node_payload)
                    elif namespace == CURRENT_EDGE_PROJECTION_NAMESPACE:
                        edge_id = str(payload.get("id") or key)
                        edge_payload = payload.get("payload")
                        if isinstance(edge_payload, dict):
                            self.edges[edge_id] = GraphEdge(
                                edge_id,
                                str(payload.get("kind") or ""),
                                str(payload.get("source") or ""),
                                str(payload.get("target") or ""),
                                edge_payload,
                            )

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

    def _replace_current_node_projection(
        self,
        node_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        content_hash: str,
        revision_id: str,
    ) -> None:
        projection = {
            "id": node_id,
            "kind": kind,
            "payload": payload,
            "content_hash": content_hash,
            "current_revision_id": revision_id,
            "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        }
        self.replace_named_projection(CURRENT_NODE_PROJECTION_NAMESPACE, node_id, projection)

    def _replace_current_edge_projection(
        self,
        edge_id: str,
        kind: str,
        source: str,
        target: str,
        payload: dict[str, Any],
        *,
        content_hash: str,
        revision_id: str,
    ) -> None:
        projection = {
            "id": edge_id,
            "kind": kind,
            "source": source,
            "target": target,
            "payload": payload,
            "content_hash": content_hash,
            "current_revision_id": revision_id,
            "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        }
        self.replace_named_projection(CURRENT_EDGE_PROJECTION_NAMESPACE, edge_id, projection)

    # ------------------------------------------------------------------
    # Graph state API used by ModelKeyGuard.
    # ------------------------------------------------------------------
    def append_node_if_updated(self, node_id: str, kind: str, payload: dict[str, Any]) -> bool:
        content_hash = canonical_node_hash(kind, payload)
        existing = self.nodes.get(node_id)
        existing_hash = None
        existing_revision_id = None
        if existing is not None:
            existing_hash = canonical_node_hash(existing.kind, existing.payload)
            existing_revision_id = self._current_node_revision_id(node_id)
        if existing is not None and existing.kind == kind and existing_hash == content_hash:
            return False

        seq = self._next_revision_seq()
        revision_id = self._revision_id("node", node_id, seq, content_hash)
        revision_kind = "NODE_ASSERTED" if existing is None else "NODE_REVISED"
        revision_payload = {
            "entity_id": node_id,
            "kind": kind,
            "payload": payload,
            "content_hash": content_hash,
            "supersedes_revision_id": existing_revision_id,
            "revision_seq": seq,
        }
        self._append_graph_revision(
            record_type=RECORD_NODE_REVISION,
            revision_id=revision_id,
            revision_kind=revision_kind,
            subject_id=node_id,
            payload=revision_payload,
        )
        if existing_revision_id:
            tombstone_id = self._revision_id("node_tombstone", node_id, self._next_revision_seq(), content_hash)
            self._append_graph_revision(
                record_type=RECORD_NODE_REVISION,
                revision_id=tombstone_id,
                revision_kind="NODE_REVISION_TOMBSTONED",
                subject_id=node_id,
                payload={
                    "entity_id": node_id,
                    "tombstone": True,
                    "tombstoned_revision_id": existing_revision_id,
                    "redirects_to_revision_id": revision_id,
                },
            )

        self.nodes[node_id] = GraphNode(node_id, kind, payload)
        self._replace_current_node_projection(
            node_id,
            kind,
            payload,
            content_hash=content_hash,
            revision_id=revision_id,
        )
        return True

    def append_edge_if_updated(self, edge_id: str, kind: str, source: str, target: str, payload: dict[str, Any]) -> bool:
        content_hash = canonical_edge_hash(kind, source, target, payload)
        existing = self.edges.get(edge_id)
        existing_hash = None
        existing_revision_id = None
        if existing is not None:
            existing_hash = canonical_edge_hash(existing.kind, existing.source, existing.target, existing.payload)
            existing_revision_id = self._current_edge_revision_id(edge_id)
        if (
            existing is not None
            and existing.kind == kind
            and existing.source == source
            and existing.target == target
            and existing_hash == content_hash
        ):
            return False

        seq = self._next_revision_seq()
        revision_id = self._revision_id("edge", edge_id, seq, content_hash)
        revision_kind = "EDGE_ASSERTED" if existing is None else "EDGE_REVISED"
        revision_payload = {
            "entity_id": edge_id,
            "kind": kind,
            "source": source,
            "target": target,
            "payload": payload,
            "content_hash": content_hash,
            "supersedes_revision_id": existing_revision_id,
            "revision_seq": seq,
        }
        self._append_graph_revision(
            record_type=RECORD_EDGE_REVISION,
            revision_id=revision_id,
            revision_kind=revision_kind,
            subject_id=edge_id,
            payload=revision_payload,
        )
        if existing_revision_id:
            tombstone_id = self._revision_id("edge_tombstone", edge_id, self._next_revision_seq(), content_hash)
            self._append_graph_revision(
                record_type=RECORD_EDGE_REVISION,
                revision_id=tombstone_id,
                revision_kind="EDGE_REVISION_TOMBSTONED",
                subject_id=edge_id,
                payload={
                    "entity_id": edge_id,
                    "tombstone": True,
                    "tombstoned_revision_id": existing_revision_id,
                    "redirects_to_revision_id": revision_id,
                },
            )

        self.edges[edge_id] = GraphEdge(edge_id, kind, source, target, payload)
        self._replace_current_edge_projection(
            edge_id,
            kind,
            source,
            target,
            payload,
            content_hash=content_hash,
            revision_id=revision_id,
        )
        return True

    def _current_node_revision_id(self, node_id: str) -> str | None:
        row = self.get_named_projection(CURRENT_NODE_PROJECTION_NAMESPACE, node_id)
        if row and isinstance(row.get("payload"), dict):
            value = row["payload"].get("current_revision_id")
            return str(value) if value else None
        return None

    def _current_edge_revision_id(self, edge_id: str) -> str | None:
        row = self.get_named_projection(CURRENT_EDGE_PROJECTION_NAMESPACE, edge_id)
        if row and isinstance(row.get("payload"), dict):
            value = row["payload"].get("current_revision_id")
            return str(value) if value else None
        return None

    def put_node(self, node_id: str, kind: str, payload: dict[str, Any]) -> None:
        self.append_node_if_updated(node_id, kind, payload)

    def put_edge(self, edge_id: str, kind: str, source: str, target: str, payload: dict[str, Any]) -> None:
        self.append_edge_if_updated(edge_id, kind, source, target, payload)

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
