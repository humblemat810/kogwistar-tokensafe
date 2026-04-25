from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from .alert_rules import AlertEngine, LLMUsageReviewer, load_jsonl
from .graph_state import GraphStateStore


def review_once(audit_path: Path, policy_path: Path, out_path: Path, sample_size: int = 200, run_llm_review: bool = True) -> dict[str, Any]:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    events = load_jsonl(audit_path)
    if sample_size and len(events) > sample_size:
        events = events[-sample_size:]
    graph = GraphStateStore.from_policy(policy)
    alerts = AlertEngine(graph).evaluate(events, policy)
    reviews = LLMUsageReviewer(graph).review(events, policy) if run_llm_review else []
    result = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event_type": "MODEL_USAGE_REVIEW_BATCH_COMPLETED",
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
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--audit", default=os.getenv("MODELKEYGUARD_AUDIT_PATH", "out/audit.jsonl"))
    p.add_argument("--policy", default=os.getenv("MODELKEYGUARD_POLICY_PATH", "config/gateway_policy.json"))
    p.add_argument("--out", default="out/review_results.jsonl")
    p.add_argument("--sample-size", type=int, default=200)
    p.add_argument("--no-llm-review", action="store_true")
    p.add_argument("--loop", action="store_true", help="run once per hour")
    args = p.parse_args(argv)
    while True:
        result = review_once(Path(args.audit), Path(args.policy), Path(args.out), args.sample_size, not args.no_llm_review)
        print(json.dumps(result, indent=2))
        if not args.loop:
            return 0
        time.sleep(3600)

if __name__ == "__main__":
    raise SystemExit(main())
