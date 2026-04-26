#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

TOKENS=(
  kgw_demo_doc_ingestor
  kgw_demo_principal_busy
  kgw_demo_low_user
  kgw_demo_external
)

for token in "${TOKENS[@]}"; do
  echo
  echo "=== token: $token ==="
  set +e
  KGW_TOKEN="$token" ./scripts/test_chat.sh
  rc=$?
  set -e
  echo "exit_code=$rc"
  case "$token" in
    kgw_demo_doc_ingestor) echo "expected: 200 allowed" ;;
    kgw_demo_principal_busy) echo "expected: 429 principal_capacity_exceeded" ;;
    kgw_demo_low_user) echo "expected: 429 user_quota_exceeded" ;;
    kgw_demo_external) echo "expected: 403 permission_denied" ;;
  esac
done
