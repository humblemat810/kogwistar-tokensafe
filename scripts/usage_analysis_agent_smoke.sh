#!/usr/bin/env bash
set -euo pipefail

# Smoke test for the reusable usage-analysis agent against a fresh-up stack.
# Run ./scripts/production_compose.sh fresh-up first, then run this script.

usage() {
  cat <<'TXT'
Usage:
  ./scripts/usage_analysis_agent_smoke.sh
  ./scripts/usage_analysis_agent_smoke.sh --help

Purpose:
  Verify that the reusable usage-analysis agent can mint a Keycloak
  service-account token and read /admin/usage.json for user, principal, and key
  analytics against the current fresh-up compose stack.

Expected stack:
  ./scripts/production_compose.sh fresh-up

Optional overrides:
  MODELKEYGUARD_GATEWAY_PUBLIC_URL
  KEYCLOAK_URL
  KEYCLOAK_REALM
  MODELKEYGUARD_OIDC_USAGE_CLIENT_ID
  MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET
  MODELKEYGUARD_ANALYTICS_SMOKE_SUBJECT_USER
  MODELKEYGUARD_ANALYTICS_SMOKE_SUBJECT_PRINCIPAL
  MODELKEYGUARD_ANALYTICS_SMOKE_SUBJECT_KEY
TXT
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  if [[ -x "${ROOT}/.venv/bin/python" ]]; then
    PYTHON_BIN="${ROOT}/.venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

BASE_URL="${MODELKEYGUARD_GATEWAY_PUBLIC_URL:-http://127.0.0.1:8789}"
KEYCLOAK_URL="${KEYCLOAK_URL:-http://127.0.0.1:8080}"
KEYCLOAK_REALM="${KEYCLOAK_REALM:-modelguard}"
CLIENT_ID="${MODELKEYGUARD_OIDC_USAGE_CLIENT_ID:-modelguard-usage-agent}"
CLIENT_SECRET="${MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET:-usage-agent-secret}"
USER_SUBJECT="${MODELKEYGUARD_ANALYTICS_SMOKE_SUBJECT_USER:-user:alice}"
PRINCIPAL_SUBJECT="${MODELKEYGUARD_ANALYTICS_SMOKE_SUBJECT_PRINCIPAL:-agent:doc-ingestor}"
KEY_SUBJECT="${MODELKEYGUARD_ANALYTICS_SMOKE_SUBJECT_KEY:-key:openai:prod}"

wait_http() {
  local url="$1"
  local label="$2"
  for _ in $(seq 1 60); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "${label} did not become ready: ${url}" >&2
  return 1
}

wait_http "${BASE_URL}/healthz" "Gateway"
wait_http "${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/.well-known/openid-configuration" "Keycloak"

export MODELKEYGUARD_GATEWAY_PUBLIC_URL="${BASE_URL}"
export MODELKEYGUARD_BEARER_TOKEN=""
export MODELKEYGUARD_OIDC_USAGE_CLIENT_ID="${CLIENT_ID}"
export MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET="${CLIENT_SECRET}"
export KEYCLOAK_URL
export KEYCLOAK_REALM

TOKEN="$(./scripts/get_agent_token.sh "${CLIENT_ID}" "${CLIENT_SECRET}")"

output="$("${PYTHON_BIN}" "${ROOT}/scripts/usage_analysis_agent.py" \
  --bearer-token "${TOKEN}" \
  --base-url "${BASE_URL}" \
  --user "${USER_SUBJECT}" \
  --principal "${PRINCIPAL_SUBJECT}" \
  --key "${KEY_SUBJECT}")"

printf '%s\n' "${output}" | "${PYTHON_BIN}" -c '
import json
import sys

payload = json.load(sys.stdin)
results = payload.get("results", {})
for lane in ("user", "principal", "key"):
    lane_results = results.get(lane, {})
    if not lane_results:
        raise SystemExit(f"missing results for lane: {lane}")
print("usage-analysis agent smoke passed.")
'

cat <<TXT
Gateway: ${BASE_URL}
Keycloak: ${KEYCLOAK_URL}
Client: ${CLIENT_ID}
Subjects:
  user=${USER_SUBJECT}
  principal=${PRINCIPAL_SUBJECT}
  key=${KEY_SUBJECT}
TXT
