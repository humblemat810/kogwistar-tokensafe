from __future__ import annotations

import argparse
import json
import os
import random
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .graph_state import GraphStateStore


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def review_once(audit_path: Path, policy_path: Path, out_path: Path, sample_size: int = 20) -> dict[str, Any]:
    policy = json.loads(policy_path.read_text())
    events = load_jsonl(audit_path)
    sample = random.sample(events, min(sample_size, len(events))) if events else []
    findings = []
    by_principal = defaultdict(list)
    for e in events:
        by_principal[e.get("principal_id")].append(e)
    for principal, rows in by_principal.items():
        profile = policy.get("usage_profiles", {}).get(principal, {})
        expected_models = set(profile.get("models", []))
        expected_hashes = set(profile.get("system_prompt_hashes", []))
        reasons = Counter(r.get("reason") for r in rows)
        bad_models = sorted({r.get("model") for r in rows if expected_models and r.get("model") not in expected_models})
        bad_hashes = sorted({r.get("system_prompt_hash") for r in rows if expected_hashes and r.get("system_prompt_hash") not in expected_hashes})
        blocked = sum(1 for r in rows if r.get("decision") == "BLOCKED")
        if bad_models or bad_hashes or blocked:
            findings.append({
                "principal_id": principal,
                "severity": "high" if bad_hashes else "medium",
                "bad_models": bad_models,
                "bad_system_prompt_hashes": bad_hashes,
                "blocked_count": blocked,
                "top_reasons": reasons.most_common(5),
            })
    result = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event_type": "MODEL_CALL_REVIEWED",
        "events_seen": len(events),
        "events_sampled": len(sample),
        "findings": findings,
        "llm_review_note": "Set REVIEW_LLM_BASE_URL and REVIEW_LLM_API_KEY to add an external LLM reviewer; hard-rule review ran locally.",
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, sort_keys=True) + "\n")
    graph = GraphStateStore.from_policy(policy)
    review_id = f"review:{int(time.time())}"
    graph.put_node(review_id, "review_result", result)
    graph.append_event("MODEL_CALL_REVIEWED", review_id, result)
    return result


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--audit", default=os.getenv("MODELKEYGUARD_AUDIT_PATH", "out/audit.jsonl"))
    p.add_argument("--policy", default="config/gateway_policy.json")
    p.add_argument("--out", default="out/review_results.jsonl")
    p.add_argument("--loop", action="store_true", help="run once per hour")
    args = p.parse_args()
    while True:
        result = review_once(Path(args.audit), Path(args.policy), Path(args.out))
        print(json.dumps(result, indent=2))
        if not args.loop:
            return 0
        time.sleep(3600)

if __name__ == "__main__":
    raise SystemExit(main())
