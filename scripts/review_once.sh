#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-.}"
export PYTHONDONTWRITEBYTECODE=1
export MODELKEYGUARD_GRAPH_KEY="${MODELKEYGUARD_GRAPH_KEY:-dev-only-change-this-32-bytes-minimum}"
${PYTHON:-python3} -c "from modelkeyguard.review_worker import review_once; from pathlib import Path; import json; print(json.dumps(review_once(Path('out/audit.jsonl'), Path('config/gateway_policy.json'), Path('out/review_results.jsonl')), indent=2))"
