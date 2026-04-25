#!/usr/bin/env bash
set -euo pipefail
python -m modelkeyguard review-once --audit "${MODELKEYGUARD_AUDIT_PATH:-out/audit.jsonl}" --policy "${MODELKEYGUARD_POLICY_PATH:-config/gateway_policy.json}" --out "${MODELKEYGUARD_REVIEW_OUT:-out/review_results.jsonl}"
