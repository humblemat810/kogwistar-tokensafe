from __future__ import annotations

import asyncio
import importlib
import json
import os
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from kogwistar.runtime import (
    AsyncMappingStepResolver,
    AsyncWorkflowRuntime,
    MappingStepResolver,
    WorkflowRuntime,
)
from kogwistar.runtime.models import RunSuccess

from .graph_state import GraphStateStore, resolve_graph_app_key

USAGE_SCANNER_CHECKPOINT_NAMESPACE = "modelkeyguard.usage.scanner.checkpoint"
USAGE_SCANNER_CHECKPOINT_KEY = "runtime"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip() or default


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(value: Any) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class _WorkflowNodeRecord:
    id: str
    op: str
    metadata: dict[str, Any]
    terminal: bool = False
    fanout: bool = False

    def safe_get_id(self) -> str:
        return self.id


@dataclass
class _WorkflowEdgeRecord:
    id: str
    source_ids: list[str]
    target_ids: list[str]
    metadata: dict[str, Any]

    def safe_get_id(self) -> str:
        return self.id


class _CodeWorkflowEngine:
    def __init__(self, nodes: list[_WorkflowNodeRecord], edges: list[_WorkflowEdgeRecord]) -> None:
        self._nodes = list(nodes)
        self._edges = list(edges)
        self.read = self

    @staticmethod
    def _matches_where(metadata: dict[str, Any], where: dict[str, Any] | None) -> bool:
        if not where:
            return True
        clauses = where.get("$and") if isinstance(where, dict) else None
        if not isinstance(clauses, list):
            return True
        for clause in clauses:
            if not isinstance(clause, dict):
                continue
            for key, expected in clause.items():
                if metadata.get(key) != expected:
                    return False
        return True

    def get_nodes(self, **kwargs: Any) -> list[_WorkflowNodeRecord]:
        where = kwargs.get("where")
        limit = kwargs.get("limit")
        rows = [node for node in self._nodes if self._matches_where(node.metadata, where)]
        if isinstance(limit, int):
            return rows[:limit]
        return rows

    def get_edges(self, **kwargs: Any) -> list[_WorkflowEdgeRecord]:
        where = kwargs.get("where")
        limit = kwargs.get("limit")
        rows = [edge for edge in self._edges if self._matches_where(edge.metadata, where)]
        if isinstance(limit, int):
            return rows[:limit]
        return rows


class _ConversationMetaSQLite:
    def current_user_seq(self, _conversation_id: str) -> int:
        return 0


class _InMemoryConversationEngine:
    def __init__(self) -> None:
        self.nodes: list[Any] = []
        self.edges: list[Any] = []
        self.read = self
        self.write = self
        self.meta_sqlite = _ConversationMetaSQLite()
        self.backend = None

    @staticmethod
    def _matches_where(metadata: dict[str, Any], where: dict[str, Any] | None) -> bool:
        if not where:
            return True
        clauses = where.get("$and") if isinstance(where, dict) else None
        if not isinstance(clauses, list):
            return True
        for clause in clauses:
            if not isinstance(clause, dict):
                continue
            for key, expected in clause.items():
                if metadata.get(key) != expected:
                    return False
        return True

    def add_node(self, node: Any, doc_id: str | None = None) -> None:
        _ = doc_id
        self.nodes.append(node)

    def add_edge(self, edge: Any, doc_id: str | None = None) -> None:
        _ = doc_id
        self.edges.append(edge)

    def get_nodes(
        self,
        ids: list[str] | None = None,
        node_type: type | None = None,
        include: list[str] | None = None,
        where: dict[str, Any] | None = None,
        limit: int | None = 200,
        resolve_mode: str = "active_only",
    ) -> list[Any]:
        _ = include, resolve_mode
        rows = self.nodes
        if ids:
            wanted = {str(i) for i in ids}
            rows = [node for node in rows if str(getattr(node, "id", "")) in wanted]
        if node_type is not None:
            rows = [node for node in rows if isinstance(node, node_type)]
        if where:
            rows = [node for node in rows if self._matches_where(dict(getattr(node, "metadata", {}) or {}), where)]
        if isinstance(limit, int):
            return rows[:limit]
        return rows

    def get_edges(
        self,
        ids: list[str] | None = None,
        edge_type: type | None = None,
        where: dict[str, Any] | None = None,
        limit: int | None = 400,
        include: list[str] | None = None,
        resolve_mode: str = "active_only",
    ) -> list[Any]:
        _ = include, resolve_mode
        rows = self.edges
        if ids:
            wanted = {str(i) for i in ids}
            rows = [edge for edge in rows if str(getattr(edge, "id", "")) in wanted]
        if edge_type is not None:
            rows = [edge for edge in rows if isinstance(edge, edge_type)]
        if where:
            rows = [edge for edge in rows if self._matches_where(dict(getattr(edge, "metadata", {}) or {}), where)]
        if isinstance(limit, int):
            return rows[:limit]
        return rows

    def uow(self):
        return nullcontext()


def _build_linear_workflow(workflow_id: str, ops: list[str]) -> _CodeWorkflowEngine:
    nodes: list[_WorkflowNodeRecord] = []
    edges: list[_WorkflowEdgeRecord] = []
    for idx, op in enumerate(ops):
        node_id = f"{workflow_id}:node:{op}"
        nodes.append(
            _WorkflowNodeRecord(
                id=node_id,
                op=op,
                metadata={
                    "entity_type": "workflow_node",
                    "workflow_id": workflow_id,
                    "wf_op": op,
                    "wf_start": idx == 0,
                    "wf_terminal": idx == len(ops) - 1,
                },
                terminal=idx == len(ops) - 1,
            )
        )
        if idx > 0:
            prev_node_id = nodes[idx - 1].id
            edges.append(
                _WorkflowEdgeRecord(
                    id=f"{workflow_id}:edge:{idx}",
                    source_ids=[prev_node_id],
                    target_ids=[node_id],
                    metadata={
                        "entity_type": "workflow_edge",
                        "workflow_id": workflow_id,
                        "wf_priority": 100,
                    },
                )
            )
    return _CodeWorkflowEngine(nodes=nodes, edges=edges)


def _runtime_mode(value: str) -> str:
    mode = (value or "sync").strip().lower()
    if mode not in {"sync", "async"}:
        raise ValueError(f"unsupported runtime mode: {value}")
    return mode


def run_usage_analysis_runtime(
    *,
    client: Any,
    base_url: str,
    time_range: str,
    bucket: str,
    user_subjects: list[str],
    principal_subjects: list[str],
    key_subjects: list[str],
    runtime_mode: str = "sync",
) -> dict[str, Any]:
    workflow_id = "wf:modelkeyguard:usage_analysis:v1"
    workflow_engine = _build_linear_workflow(workflow_id, ["start", "collect", "done"])
    conversation_engine = _InMemoryConversationEngine()
    mode = _runtime_mode(runtime_mode)

    def _collect() -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for subject_type, subjects in (
            ("user", user_subjects),
            ("principal", principal_subjects),
            ("key", key_subjects),
        ):
            lane_results: dict[str, Any] = {}
            for subject_id in subjects:
                lane_results[subject_id] = client.analyze(
                    subject_type=subject_type,
                    subject_id=subject_id,
                    time_range=time_range,
                    bucket=bucket,
                )
            results[subject_type] = lane_results
        return results

    if mode == "sync":
        resolver = MappingStepResolver()

        @resolver.register("start")
        def _start(_ctx: Any):
            return RunSuccess(conversation_node_id=None, state_update=[("u", {"base_url": base_url})], route_next=["collect"])

        @resolver.register("collect")
        def _collect_step(_ctx: Any):
            payload = {
                "base_url": base_url,
                "time_range": time_range,
                "bucket": bucket,
                "results": _collect(),
            }
            return RunSuccess(conversation_node_id=None, state_update=[("u", payload)], route_next=["done"])

        @resolver.register("done")
        def _done(_ctx: Any):
            return RunSuccess(conversation_node_id=None, state_update=[])

        runtime = WorkflowRuntime(
            workflow_engine=workflow_engine,
            conversation_engine=conversation_engine,
            step_resolver=resolver,
            predicate_registry={},
            trace=False,
        )
        out = runtime.run(
            workflow_id=workflow_id,
            conversation_id=f"usage-analysis:{int(time.time() * 1000)}",
            turn_node_id="usage-analysis-turn",
            initial_state={},
        )
        return {
            "base_url": str(out.final_state.get("base_url") or base_url),
            "time_range": str(out.final_state.get("time_range") or time_range),
            "bucket": str(out.final_state.get("bucket") or bucket),
            "results": dict(out.final_state.get("results") or {}),
        }

    resolver_async = AsyncMappingStepResolver()

    @resolver_async.register("start")
    async def _start_async(_ctx: Any):
        return RunSuccess(conversation_node_id=None, state_update=[("u", {"base_url": base_url})], route_next=["collect"])

    @resolver_async.register("collect")
    async def _collect_async(_ctx: Any):
        payload = {
            "base_url": base_url,
            "time_range": time_range,
            "bucket": bucket,
            "results": _collect(),
        }
        return RunSuccess(conversation_node_id=None, state_update=[("u", payload)], route_next=["done"])

    @resolver_async.register("done")
    async def _done_async(_ctx: Any):
        return RunSuccess(conversation_node_id=None, state_update=[])

    runtime_async = AsyncWorkflowRuntime(
        workflow_engine=workflow_engine,
        conversation_engine=conversation_engine,
        step_resolver=resolver_async,
        predicate_registry={},
        trace=False,
    )

    async def _run_async():
        return await runtime_async.run(
            workflow_id=workflow_id,
            conversation_id=f"usage-analysis:{int(time.time() * 1000)}",
            turn_node_id="usage-analysis-turn",
            initial_state={},
        )

    out_async = asyncio.run(_run_async())
    return {
        "base_url": str(out_async.final_state.get("base_url") or base_url),
        "time_range": str(out_async.final_state.get("time_range") or time_range),
        "bucket": str(out_async.final_state.get("bucket") or bucket),
        "results": dict(out_async.final_state.get("results") or {}),
    }


def run_usage_reviewer_runtime(
    *,
    status: dict[str, Any],
    base_url: str,
    safe_token: str,
    model: str,
    system_prompt: str,
    key_id: str = "",
    runtime_mode: str = "sync",
) -> dict[str, Any]:
    from .reviewer_agent import run_langchain_reviewer

    workflow_id = "wf:modelkeyguard:usage_reviewer:v1"
    workflow_engine = _build_linear_workflow(workflow_id, ["start", "review", "done"])
    conversation_engine = _InMemoryConversationEngine()
    mode = _runtime_mode(runtime_mode)

    if mode == "sync":
        resolver = MappingStepResolver()

        @resolver.register("start")
        def _start(_ctx: Any):
            return RunSuccess(conversation_node_id=None, state_update=[], route_next=["review"])

        @resolver.register("review")
        def _review(_ctx: Any):
            result = run_langchain_reviewer(
                status=status,
                base_url=base_url,
                safe_token=safe_token,
                model=model,
                system_prompt=system_prompt,
                key_id=key_id,
            )
            return RunSuccess(conversation_node_id=None, state_update=[("u", {"review_result": result})], route_next=["done"])

        @resolver.register("done")
        def _done(_ctx: Any):
            return RunSuccess(conversation_node_id=None, state_update=[])

        runtime = WorkflowRuntime(
            workflow_engine=workflow_engine,
            conversation_engine=conversation_engine,
            step_resolver=resolver,
            predicate_registry={},
            trace=False,
        )
        out = runtime.run(
            workflow_id=workflow_id,
            conversation_id=f"usage-reviewer:{int(time.time() * 1000)}",
            turn_node_id="usage-reviewer-turn",
            initial_state={},
        )
        return dict(out.final_state.get("review_result") or {})

    resolver_async = AsyncMappingStepResolver()

    @resolver_async.register("start")
    async def _start_async(_ctx: Any):
        return RunSuccess(conversation_node_id=None, state_update=[], route_next=["review"])

    @resolver_async.register("review")
    async def _review_async(_ctx: Any):
        result = run_langchain_reviewer(
            status=status,
            base_url=base_url,
            safe_token=safe_token,
            model=model,
            system_prompt=system_prompt,
            key_id=key_id,
        )
        return RunSuccess(conversation_node_id=None, state_update=[("u", {"review_result": result})], route_next=["done"])

    @resolver_async.register("done")
    async def _done_async(_ctx: Any):
        return RunSuccess(conversation_node_id=None, state_update=[])

    runtime_async = AsyncWorkflowRuntime(
        workflow_engine=workflow_engine,
        conversation_engine=conversation_engine,
        step_resolver=resolver_async,
        predicate_registry={},
        trace=False,
    )

    async def _run_async():
        return await runtime_async.run(
            workflow_id=workflow_id,
            conversation_id=f"usage-reviewer:{int(time.time() * 1000)}",
            turn_node_id="usage-reviewer-turn",
            initial_state={},
        )

    out_async = asyncio.run(_run_async())
    return dict(out_async.final_state.get("review_result") or {})


def try_load_policy(path: str | None = None) -> dict[str, Any]:
    policy_path = path or _env("MODELKEYGUARD_POLICY_PATH", "config/gateway_policy.json")
    try:
        return json.loads(Path(policy_path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _topic_keywords_plugin(payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("recent_text") or "").lower()
    keywords = [str(x).strip().lower() for x in (payload.get("topic_keywords") or []) if str(x).strip()]
    matched = [kw for kw in keywords if kw and kw in text]
    return {
        "plugin": "topic_keywords",
        "triggered": bool(matched),
        "reason": "topic keywords detected in recent history" if matched else "no topic keywords detected",
        "matched": matched,
    }


def _dangerous_keywords_plugin(payload: dict[str, Any]) -> dict[str, Any]:
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    summary = status.get("summary") if isinstance(status.get("summary"), dict) else {}
    hits = int(summary.get("dangerous_keyword_hits") or 0)
    return {
        "plugin": "dangerous_keywords",
        "triggered": hits > 0,
        "reason": f"dangerous_keyword_hits={hits}",
        "hits": hits,
    }


def _plugin_registry() -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    return {
        "topic_keywords": _topic_keywords_plugin,
        "dangerous_keywords": _dangerous_keywords_plugin,
    }


def _load_configured_plugin(ref: str) -> Callable[[dict[str, Any]], dict[str, Any]] | None:
    ref = (ref or "").strip()
    if not ref or ":" not in ref:
        return None
    module_name, fn_name = ref.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        fn = getattr(module, fn_name)
    except Exception:
        return None
    if not callable(fn):
        return None
    return fn


def evaluate_scanner_plugins(
    *,
    policy: dict[str, Any],
    workflow_name: str,
    status: dict[str, Any],
    recent_text: str = "",
) -> list[dict[str, Any]]:
    scanner_cfg = policy.get("scanner_plugins") if isinstance(policy.get("scanner_plugins"), dict) else {}
    entries = scanner_cfg.get(workflow_name) if isinstance(scanner_cfg, dict) else None
    configured: list[str] = [str(item).strip() for item in entries] if isinstance(entries, list) else []
    payload = {
        "status": status,
        "recent_text": recent_text,
        "topic_keywords": _topic_keywords(policy),
    }
    registry = _plugin_registry()
    results: list[dict[str, Any]] = []

    for ref in configured:
        fn = registry.get(ref) or _load_configured_plugin(ref)
        if fn is None:
            continue
        try:
            out = fn(payload)
        except Exception as exc:
            results.append({"plugin": ref, "triggered": False, "reason": f"plugin_error:{exc}"})
            continue
        if isinstance(out, dict):
            results.append(out)

    if not results:
        for name in ("dangerous_keywords", "topic_keywords"):
            out = registry[name](payload)
            if isinstance(out, dict):
                results.append(out)
    return results


def scanner_should_run(
    *,
    status: dict[str, Any],
    force: bool,
    plugin_results: list[dict[str, Any]],
    usage_checkpoint: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    if force:
        return True, {"reason": "forced"}
    should_review = bool(status.get("should_review"))
    plugin_triggered = any(bool(item.get("triggered")) for item in plugin_results)
    latest_request_id = str((status.get("window") or {}).get("latest_request_id") or "")
    if usage_checkpoint:
        last_seen = str(usage_checkpoint.get("last_seen_request_id") or "")
        if latest_request_id and latest_request_id == last_seen:
            return False, {"reason": "no_new_history", "latest_request_id": latest_request_id}
    if should_review or plugin_triggered:
        return True, {"reason": "triggered", "should_review": should_review, "plugin_triggered": plugin_triggered}
    return False, {"reason": "thresholds_not_met"}


def try_open_graph_state() -> GraphStateStore | None:
    graph_path = Path(_env("MODELKEYGUARD_REVIEW_GRAPH_PATH", _env("MODELKEYGUARD_GRAPH_PATH", "out/modelkeyguard_graph.jsonl")))
    try:
        return GraphStateStore(graph_path, app_key=resolve_graph_app_key())
    except Exception:
        return None


def _get_named_projection(graph_state: GraphStateStore, namespace: str, key: str) -> dict[str, Any] | None:
    getter = getattr(graph_state, "get_named_projection", None)
    if callable(getter):
        try:
            payload = getter(namespace, key)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
    composite = f"{namespace}:{key}"
    payload = getattr(graph_state, "projections", {}).get(composite)
    return payload if isinstance(payload, dict) else None


def _replace_named_projection(graph_state: GraphStateStore, namespace: str, key: str, payload: dict[str, Any]) -> None:
    replacer = getattr(graph_state, "replace_named_projection", None)
    if callable(replacer):
        replacer(namespace, key, payload)
        return
    graph_state.put_projection(f"{namespace}:{key}", payload)


def load_usage_scanner_checkpoint(graph_state: GraphStateStore | None) -> dict[str, Any]:
    if graph_state is None:
        return {
            "last_run_ts": "",
            "last_seen_request_id": "",
        }
    payload = _get_named_projection(graph_state, USAGE_SCANNER_CHECKPOINT_NAMESPACE, USAGE_SCANNER_CHECKPOINT_KEY) or {}
    return {
        "last_run_ts": str(payload.get("last_run_ts") or ""),
        "last_seen_request_id": str(payload.get("last_seen_request_id") or ""),
    }


def save_usage_scanner_checkpoint(
    graph_state: GraphStateStore | None,
    *,
    latest_request_id: str,
) -> dict[str, Any]:
    payload = {
        "last_run_ts": _iso_now(),
        "last_seen_request_id": str(latest_request_id or ""),
        "projection_schema_version": 1,
    }
    if graph_state is not None:
        _replace_named_projection(graph_state, USAGE_SCANNER_CHECKPOINT_NAMESPACE, USAGE_SCANNER_CHECKPOINT_KEY, payload)
    return payload


def recent_history_text(graph_state: GraphStateStore | None, limit: int = 20) -> str:
    if graph_state is None:
        return ""
    rows: list[tuple[datetime, str]] = []
    for node in getattr(graph_state, "nodes", {}).values():
        if getattr(node, "kind", "") != "request_response_history":
            continue
        payload = getattr(node, "payload", {}) or {}
        ts = _parse_ts(payload.get("ts"))
        text = f"{payload.get('request_body_text', '')}\n{payload.get('response_body_text', '')}".strip()
        if text:
            rows.append((ts, text))
    rows.sort(key=lambda item: item[0], reverse=True)
    merged = "\n".join(item[1] for item in rows[: max(1, int(limit))])
    return merged[:20000]


def _topic_keywords(policy: dict[str, Any]) -> list[str]:
    scanner_cfg = policy.get("scanner_plugins") if isinstance(policy.get("scanner_plugins"), dict) else {}
    topic_cfg = scanner_cfg.get("topic_keywords") if isinstance(scanner_cfg, dict) else None
    if isinstance(topic_cfg, list):
        kws = [str(item).strip() for item in topic_cfg if str(item).strip()]
        if kws:
            return kws
    default_raw = _env(
        "MODELKEYGUARD_SCANNER_TOPIC_KEYWORDS",
        "terrorist attacks,terrorism,bomb making,extremist attack,violent attack",
    )
    return [item.strip() for item in default_raw.split(",") if item.strip()]
