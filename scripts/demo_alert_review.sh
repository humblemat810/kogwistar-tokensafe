#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export MODELKEYGUARD_GRAPH_KEY="${MODELKEYGUARD_GRAPH_KEY:-dev-only-change-this-32-bytes-minimum}"
export MODELKEYGUARD_AUDIT_PATH="${MODELKEYGUARD_AUDIT_PATH:-out/audit.jsonl}"
export MODELKEYGUARD_REVIEW_OUT="${MODELKEYGUARD_REVIEW_OUT:-out/review_results.jsonl}"

echo "Running alert + LLM-style usage review"
echo "  audit:  $MODELKEYGUARD_AUDIT_PATH"
echo "  output: $MODELKEYGUARD_REVIEW_OUT"
python -m modelkeyguard review-once \
  --audit "$MODELKEYGUARD_AUDIT_PATH" \
  --policy "${MODELKEYGUARD_POLICY_PATH:-config/gateway_policy.json}" \
  --out "$MODELKEYGUARD_REVIEW_OUT"
