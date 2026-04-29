#!/usr/bin/env bash
set -euo pipefail

# Smoke the deployed surface against a running gateway/Keycloak pair.
# This checks the browser OIDC login redirect, a CLI/service-account token path,
# and the usage-analysis agent scaffold that operators and AI agents extend.

usage() {
  cat <<'TXT'
Usage:
  ./scripts/deployment_smoke.sh
  ./scripts/deployment_smoke.sh --help

Environment:
  MODELKEYGUARD_GATEWAY_PUBLIC_URL   Gateway URL to probe.
  KEYCLOAK_URL                       Keycloak URL to probe and mint tokens from.
  KEYCLOAK_REALM                     Keycloak realm name.
  MODELKEYGUARD_OIDC_USER_CLIENT_ID  Client id for the human/app token path.
  MODELKEYGUARD_OIDC_USER_CLIENT_SECRET
  MODELKEYGUARD_OIDC_ADMIN_CLIENT_ID Client id for the admin/token path.
  MODELKEYGUARD_OIDC_ADMIN_CLIENT_SECRET
  MODELKEYGUARD_OIDC_USAGE_CLIENT_ID Client id for the usage-analysis agent.
  MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET
  MODELKEYGUARD_KEYCLOAK_PUBLIC_URL  Optional browser-visible Keycloak URL.
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
KEYCLOAK_PUBLIC_URL="${MODELKEYGUARD_KEYCLOAK_PUBLIC_URL:-${KEYCLOAK_URL}}"
KEYCLOAK_REALM="${KEYCLOAK_REALM:-modelguard}"
USER_CLIENT_ID="${MODELKEYGUARD_OIDC_USER_CLIENT_ID:-langchain-agent}"
USER_CLIENT_SECRET="${MODELKEYGUARD_OIDC_USER_CLIENT_SECRET:-agent-secret}"
ADMIN_CLIENT_ID="${MODELKEYGUARD_OIDC_ADMIN_CLIENT_ID:-modelguard-admin}"
ADMIN_CLIENT_SECRET="${MODELKEYGUARD_OIDC_ADMIN_CLIENT_SECRET:-admin-agent-secret}"
USAGE_CLIENT_ID="${MODELKEYGUARD_OIDC_USAGE_CLIENT_ID:-modelguard-usage-agent}"
USAGE_CLIENT_SECRET="${MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET:-usage-agent-secret}"
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

login_headers="$(mktemp)"
trap 'rm -f "${login_headers}"' EXIT
curl -fsS -D "${login_headers}" -o /dev/null "${BASE_URL}/admin/oidc/login?next=/admin/usage"
login_location="$(awk 'BEGIN{IGNORECASE=1} /^location:/ {sub(/\r$/, "", $0); sub(/^location:[[:space:]]*/, "", $0); print; exit}' "${login_headers}")"
if [[ -z "${login_location}" ]]; then
  echo "missing browser OIDC redirect location header" >&2
  exit 1
fi
if [[ "${login_location}" != "${KEYCLOAK_PUBLIC_URL%/}"* ]]; then
  cat >&2 <<TXT
browser OIDC login did not redirect to the configured Keycloak public URL.
Expected prefix: ${KEYCLOAK_PUBLIC_URL}
Actual location: ${login_location}
TXT
  exit 1
fi

USER_TOKEN="$(./scripts/get_agent_token.sh "${USER_CLIENT_ID}" "${USER_CLIENT_SECRET}")"
ADMIN_TOKEN="$(./scripts/get_agent_token.sh "${ADMIN_CLIENT_ID}" "${ADMIN_CLIENT_SECRET}")"
USAGE_TOKEN="$(./scripts/get_agent_token.sh "${USAGE_CLIENT_ID}" "${USAGE_CLIENT_SECRET}")"

curl -fsS -H "Authorization: Bearer ${USER_TOKEN}" "${BASE_URL}/v1/models" >/dev/null
curl -fsS -H "Authorization: Bearer ${ADMIN_TOKEN}" "${BASE_URL}/admin/usage.json?time_range=24h&bucket=hour" >/dev/null

output="$("${PYTHON_BIN}" "${ROOT}/scripts/usage_analysis_agent.py" \
  --bearer-token "${USAGE_TOKEN}" \
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
print("deployment smoke passed.")
'

cat <<TXT
Gateway: ${BASE_URL}
Keycloak: ${KEYCLOAK_URL}
Browser OIDC redirect: ${login_location}
Client ids:
  user=${USER_CLIENT_ID}
  admin=${ADMIN_CLIENT_ID}
  usage=${USAGE_CLIENT_ID}
TXT
