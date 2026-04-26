#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${KGW_BASE_URL:-http://127.0.0.1:8789}"
SAFE_TOKEN="${KGW_SAFE_TOKEN:-}"
DEPLOYMENT="${KGW_AZURE_DEPLOYMENT:-}"
API_VERSION="${KGW_AZURE_API_VERSION:-2024-10-21}"
SYSTEM_PROMPT="${KGW_SYSTEM_PROMPT:-You are a policy-compliant enterprise assistant.}"
USER_PROMPT="${KGW_USER_PROMPT:-Return one sentence proving this request went through ModelKeyGuard.}"
MAX_TOKENS="${KGW_MAX_TOKENS:-128}"

if [[ -z "$SAFE_TOKEN" ]]; then
  echo "KGW_SAFE_TOKEN is required" >&2
  exit 2
fi
if [[ -z "$DEPLOYMENT" ]]; then
  echo "KGW_AZURE_DEPLOYMENT is required" >&2
  exit 2
fi

curl -fsS -X POST \
  "${BASE_URL%/}/openai/deployments/${DEPLOYMENT}/chat/completions?api-version=${API_VERSION}" \
  -H "authorization: Bearer ${SAFE_TOKEN}" \
  -H "content-type: application/json" \
  -d @- <<JSON | "${PYTHON:-python3}" -m json.tool
{
  "messages": [
    {"role": "system", "content": "${SYSTEM_PROMPT}"},
    {"role": "user", "content": "${USER_PROMPT}"}
  ],
  "max_tokens": ${MAX_TOKENS}
}
JSON
