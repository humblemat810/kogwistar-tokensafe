#!/usr/bin/env bash
set -euo pipefail

# Start the local Postgres + Keycloak compose stack used by the tutorials.
# This is the local infrastructure launcher, not the gateway itself.

if docker compose version >/dev/null 2>&1; then
  compose_cmd=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose_cmd=(docker-compose)
else
  echo "Neither 'docker compose' nor 'docker-compose' is available." >&2
  exit 1
fi

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

TOKENSAFE_HOME="${MODELKEYGUARD_HOME:-${TOKENSAFE_HOME:-$HOME/.tokensafe}}"
export MODELKEYGUARD_POSTGRES_DATA_DIR="${MODELKEYGUARD_POSTGRES_DATA_DIR:-${TOKENSAFE_HOME%/}/postgres}"
export MODELKEYGUARD_KEYCLOAK_DATA_DIR="${MODELKEYGUARD_KEYCLOAK_DATA_DIR:-${TOKENSAFE_HOME%/}/keycloak}"
mkdir -p "${MODELKEYGUARD_POSTGRES_DATA_DIR}" "${MODELKEYGUARD_KEYCLOAK_DATA_DIR}"

"${compose_cmd[@]}" up -d postgres keycloak
wait_http "http://localhost:8080/realms/master/.well-known/openid-configuration" "Keycloak"
KEYCLOAK_URL="http://localhost:8080" \
KEYCLOAK_REALM="modelguard" \
MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME="${MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME:-admin}" \
MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD="${MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD:-admin}" \
MODELKEYGUARD_OIDC_ADMIN_CLIENT_ID="${MODELKEYGUARD_OIDC_ADMIN_CLIENT_ID:-modelguard-admin}" \
MODELKEYGUARD_ADMIN_REQUIRED_ROLE="${MODELKEYGUARD_ADMIN_REQUIRED_ROLE:-model.admin}" \
  ./scripts/bootstrap_keycloak_admin_role.sh
cat <<'TXT'
Stack started.
Keycloak: http://localhost:8080
Postgres: postgresql://modelguard:modelguard@localhost:5432/modelguard
Gateway:
  export MODELKEYGUARD_STORE=postgres
  export MODELKEYGUARD_POSTGRES_DSN=postgresql://modelguard:modelguard@localhost:5432/modelguard
  ./scripts/start_gateway.sh
TXT
