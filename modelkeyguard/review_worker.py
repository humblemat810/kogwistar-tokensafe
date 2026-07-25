from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .alert_rules import AlertEngine, LLMUsageReviewer, load_jsonl
from .graph_state import GraphStateStore
from .reviewer_agent import advance_review_checkpoint


def review_once(
    audit_path: Path,
    policy_path: Path,
    out_path: Path,
    sample_size: int = 200,
    run_llm_review: bool = True,
    lookback_minutes: int | None = None,
    checkpoint_path: Path | None = None,
) -> dict[str, Any]:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    events = load_jsonl(audit_path)
    checkpoint = _load_checkpoint(checkpoint_path) if checkpoint_path else None
    if lookback_minutes:
        since = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
        events = [e for e in events if _parse_ts(e.get("ts")) >= since]
    if checkpoint:
        checkpoint_ts, checkpoint_request_id = checkpoint
        events = [
            e for e in events
            if (_parse_ts(e.get("ts")), str(e.get("request_id") or ""))
            > (checkpoint_ts, checkpoint_request_id)
        ]
    if sample_size and len(events) > sample_size:
        events = events[-sample_size:]
    graph = GraphStateStore.from_policy(policy)
    alerts = AlertEngine(graph).evaluate(events, policy)
    reviews = LLMUsageReviewer(graph).review(events, policy) if run_llm_review else []
    result = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event_type": "MODEL_USAGE_REVIEW_BATCH_COMPLETED",
        "pipeline": "legacy-audit-adapter",
        "events_seen": len(events),
        "alerts": alerts,
        "reviews": reviews,
        "llm_review_note": "Default reviewer is deterministic. Inject LLMUsageReviewer(review_callback=...) or wire REVIEW_LLM_* to call an external LLM.",
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, sort_keys=True) + "\n")
    graph.put_node(f"review_batch:{int(time.time())}", "review_batch", result)
    graph.append_event("MODEL_USAGE_REVIEW_BATCH_COMPLETED", "review_worker", result)
    # Keep legacy audit scheduling on the same graph cursor as the reviewer
    # agent.  This prevents the two entrypoints from reviewing the same window
    # twice while preserving the legacy output contract.
    if events:
        latest_ts, latest_request_id = max((_parse_ts(e.get("ts")), str(e.get("request_id") or "")) for e in events)
        advance_review_checkpoint(
            graph,
            policy,
            reviewed_by="review_worker",
            review_summary="legacy audit adapter batch",
            status={
                "checkpoint": {},
                "window": {"latest_ts": latest_ts.isoformat().replace("+00:00", "Z"), "latest_request_id": latest_request_id},
            },
        )
    if checkpoint_path and events:
        latest = max((_parse_ts(e.get("ts")), str(e.get("request_id") or "")) for e in events)
        _save_checkpoint(checkpoint_path, *latest)
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--audit", default=os.getenv("MODELKEYGUARD_AUDIT_PATH", "out/audit.jsonl"))
    p.add_argument("--policy", default=os.getenv("MODELKEYGUARD_POLICY_PATH", "config/gateway_policy.json"))
    p.add_argument("--out", default=os.getenv("MODELKEYGUARD_REVIEW_OUT", "out/review_results.jsonl"))
    p.add_argument("--sample-size", type=int, default=int(os.getenv("MODELKEYGUARD_REVIEW_SAMPLE_SIZE", "200")))
    p.add_argument("--no-llm-review", action="store_true")
    p.add_argument("--lookback-minutes", type=int, default=int(os.getenv("MODELKEYGUARD_REVIEW_LOOKBACK_MINUTES", "0")) or None)
    p.add_argument("--checkpoint", default=os.getenv("MODELKEYGUARD_REVIEW_CHECKPOINT_PATH"), help="optional checkpoint JSON file path for incremental review")
    p.add_argument("--loop", action="store_true", help="run repeatedly with interval")
    p.add_argument("--interval-seconds", type=int, default=int(os.getenv("MODELKEYGUARD_REVIEW_INTERVAL_SECONDS", "3600")))
    args = p.parse_args(argv)
    while True:
        result = review_once(
            Path(args.audit),
            Path(args.policy),
            Path(args.out),
            args.sample_size,
            not args.no_llm_review,
            lookback_minutes=args.lookback_minutes,
            checkpoint_path=Path(args.checkpoint) if args.checkpoint else None,
        )
        print(json.dumps(result, indent=2))
        if not args.loop:
            return 0
        time.sleep(max(1, args.interval_seconds))


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


def _load_checkpoint(path: Path) -> tuple[datetime, str] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    value = payload.get("last_ts")
    if not value:
        return None
    return _parse_ts(value), str(payload.get("last_request_id") or "")


def _save_checkpoint(path: Path, ts: datetime, request_id: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_ts": ts.isoformat().replace("+00:00", "Z"), "last_request_id": request_id}
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}" )
    temp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)

if __name__ == "__main__":
    raise SystemExit(main())
