#!/usr/bin/env bash
set -euo pipefail

# End-to-end smoke for the OIDC-only deployment path.
# It boots local services, starts the gateway, fetches Keycloak tokens, and
# verifies that model and admin endpoints require OIDC bearer auth.

usage() {
  cat <<'TXT'
Usage:
  ./scripts/oidc_protect_everything_smoke.sh
  ./scripts/oidc_protect_everything_smoke.sh --print-required-env

Purpose:
  Start the local pgvector Postgres + Keycloak stack, initialize the
  kogwistar_postgres graph, start the gateway in OIDC-only mode, and smoke-test
  that model and admin endpoints require Keycloak bearer tokens.

Override these when deploying to a non-local environment:
  MODELKEYGUARD_POSTGRES_DSN
  MODELKEYGUARD_GRAPH_KEY or MODELKEYGUARD_GRAPH_KEY_FILE
  KEYCLOAK_URL
  KEYCLOAK_REALM
  KEYCLOAK_INTROSPECTION_CLIENT_ID
  KEYCLOAK_INTROSPECTION_CLIENT_SECRET or KEYCLOAK_INTROSPECTION_CLIENT_SECRET_FILE
  MODELKEYGUARD_OIDC_USER_CLIENT_ID
  MODELKEYGUARD_OIDC_USER_CLIENT_SECRET
  MODELKEYGUARD_OIDC_ADMIN_CLIENT_ID
  MODELKEYGUARD_OIDC_ADMIN_CLIENT_SECRET
TXT
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ "${1:-}" == "--print-required-env" ]]; then
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

HOST="${MODELKEYGUARD_HOST:-127.0.0.1}"
PORT="${MODELKEYGUARD_PORT:-8789}"
BASE_URL="http://${HOST}:${PORT}"
GATEWAY_LOG="${MODELKEYGUARD_OIDC_SMOKE_GATEWAY_LOG:-${ROOT}/out/oidc_gateway.log}"

export MODELKEYGUARD_STORE="${MODELKEYGUARD_STORE:-kogwistar_postgres}"
export MODELKEYGUARD_POSTGRES_DSN="${MODELKEYGUARD_POSTGRES_DSN:-postgresql://modelguard:modelguard@localhost:5432/modelguard}"
export MODELKEYGUARD_GRAPH_KEY="${MODELKEYGUARD_GRAPH_KEY:-kogwistar-managed-postgres-dev-key-32-bytes-minimum}"
export MODELKEYGUARD_INIT_RESET_EXISTING="${MODELKEYGUARD_INIT_RESET_EXISTING:-1}"
export MODELKEYGUARD_KOGWISTAR_EMBED_DIM="${MODELKEYGUARD_KOGWISTAR_EMBED_DIM:-2}"
export MODELKEYGUARD_KOGWISTAR_ENFORCE_INSTALLED_ONLY="${MODELKEYGUARD_KOGWISTAR_ENFORCE_INSTALLED_ONLY:-1}"
export MODELKEYGUARD_USE_INSTALLED_KOGWISTAR="${MODELKEYGUARD_USE_INSTALLED_KOGWISTAR:-1}"
export MODELKEYGUARD_DRY_RUN="${MODELKEYGUARD_DRY_RUN:-1}"

export MODELKEYGUARD_AUTH_MODE="${MODELKEYGUARD_AUTH_MODE:-keycloak}"
export MODELKEYGUARD_REQUIRE_KEYCLOAK="${MODELKEYGUARD_REQUIRE_KEYCLOAK:-1}"
export MODELKEYGUARD_ADMIN_AUTH_MODE="${MODELKEYGUARD_ADMIN_AUTH_MODE:-keycloak}"
export MODELKEYGUARD_ADMIN_REQUIRED_ROLE="${MODELKEYGUARD_ADMIN_REQUIRED_ROLE:-model.admin}"
export MODELKEYGUARD_REQUIRE_MODEL_LIST_AUTH="${MODELKEYGUARD_REQUIRE_MODEL_LIST_AUTH:-1}"
export KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8080}"
export KEYCLOAK_REALM="${KEYCLOAK_REALM:-modelguard}"
export KEYCLOAK_INTROSPECTION_CLIENT_ID="${KEYCLOAK_INTROSPECTION_CLIENT_ID:-modelguard-gateway}"
export KEYCLOAK_INTROSPECTION_CLIENT_SECRET="${KEYCLOAK_INTROSPECTION_CLIENT_SECRET:-gateway-secret}"

USER_CLIENT_ID="${MODELKEYGUARD_OIDC_USER_CLIENT_ID:-langchain-agent}"
USER_CLIENT_SECRET="${MODELKEYGUARD_OIDC_USER_CLIENT_SECRET:-agent-secret}"
ADMIN_CLIENT_ID="${MODELKEYGUARD_OIDC_ADMIN_CLIENT_ID:-modelguard-admin}"
ADMIN_CLIENT_SECRET="${MODELKEYGUARD_OIDC_ADMIN_CLIENT_SECRET:-admin-agent-secret}"

mkdir -p "${ROOT}/out"

wait_http() {
  local url="$1"
  local label="$2"
  local attempts="${3:-60}"
  for _ in $(seq 1 "${attempts}"); do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "${label} did not become ready: ${url}" >&2
  return 1
}

if curl -fsS "${BASE_URL}/healthz" >/dev/null 2>&1; then
  cat >&2 <<TXT
Refusing to run OIDC smoke because a gateway is already responding at ${BASE_URL}.
Stop the existing gateway first, or use another port:
  MODELKEYGUARD_PORT=8791 ./scripts/oidc_protect_everything_smoke.sh
TXT
  exit 2
fi

cleanup_gateway() {
  if [[ -n "${GATEWAY_PID:-}" ]] && kill -0 "${GATEWAY_PID}" >/dev/null 2>&1; then
    kill "${GATEWAY_PID}" >/dev/null 2>&1 || true
    wait "${GATEWAY_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup_gateway EXIT

cd "${ROOT}"

echo "[1/8] Bootstrap local secrets and start Postgres + Keycloak"
./scripts/bootstrap_secrets.sh
./scripts/start_stack.sh
wait_http "${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/.well-known/openid-configuration" "Keycloak"

echo "[2/8] Initialize kogwistar_postgres graph"
./scripts/init_graph.sh

echo "[3/8] Start gateway in OIDC-only mode"
("${PYTHON_BIN}" -m modelkeyguard gateway --host "${HOST}" --port "${PORT}" --policy config/gateway_policy.json >"${GATEWAY_LOG}" 2>&1) &
GATEWAY_PID="$!"
sleep 1
if ! kill -0 "${GATEWAY_PID}" >/dev/null 2>&1; then
  echo "Gateway exited before becoming ready. Log follows:" >&2
  cat "${GATEWAY_LOG}" >&2 || true
  exit 1
fi
wait_http "${BASE_URL}/healthz" "ModelKeyGuard gateway"

echo "[4/8] Get Keycloak model and admin tokens"
USER_TOKEN="$(./scripts/get_agent_token.sh "${USER_CLIENT_ID}" "${USER_CLIENT_SECRET}")"
ADMIN_TOKEN="$(./scripts/get_agent_token.sh "${ADMIN_CLIENT_ID}" "${ADMIN_CLIENT_SECRET}")"

echo "[5/8] Pin unauthenticated /v1/models rejection"
models_status="$(curl -sS -o /tmp/modelkeyguard_oidc_models_noauth.json -w '%{http_code}' "${BASE_URL}/v1/models")"
test "${models_status}" = "401"

echo "[6/8] Pin authenticated /v1/models success"
curl -fsS -H "Authorization: Bearer ${USER_TOKEN}" "${BASE_URL}/v1/models" >/tmp/modelkeyguard_oidc_models_auth.json

echo "[7/8] Pin admin role boundary"
admin_secret_status="$(curl -sS -o /tmp/modelkeyguard_oidc_admin_secret.json -w '%{http_code}' -H 'x-modelkeyguard-admin-secret: dev-modelkeyguard-admin-secret' "${BASE_URL}/admin/usage.json")"
test "${admin_secret_status}" = "401"
admin_user_status="$(curl -sS -o /tmp/modelkeyguard_oidc_admin_user.json -w '%{http_code}' -H "Authorization: Bearer ${USER_TOKEN}" "${BASE_URL}/admin/usage.json")"
test "${admin_user_status}" = "403"
curl -fsS -H "Authorization: Bearer ${ADMIN_TOKEN}" "${BASE_URL}/admin/usage.json?time_range=24h&bucket=hour" >/tmp/modelkeyguard_oidc_admin_ok.json

echo "[8/8] Run OpenAI-compatible client through Keycloak token"
OPENAI_BASE_URL="${BASE_URL}/v1" OPENAI_API_KEY="${USER_TOKEN}" OPENAI_MODEL="${OPENAI_MODEL:-gpt-4o-mini}" "${PYTHON_BIN}" scripts/langchain_user_openai_compatible.py >/tmp/modelkeyguard_oidc_client.json

cat <<TXT
OIDC protect-everything smoke passed.
Gateway log: ${GATEWAY_LOG}
Model token client: ${USER_CLIENT_ID}
Admin token client: ${ADMIN_CLIENT_ID}
TXT
