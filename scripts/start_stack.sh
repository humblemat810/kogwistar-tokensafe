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

TOKENSAFE_HOME="${MODELKEYGUARD_HOME:-${TOKENSAFE_HOME:-$HOME/.tokensafe}}"
export MODELKEYGUARD_POSTGRES_DATA_DIR="${MODELKEYGUARD_POSTGRES_DATA_DIR:-${TOKENSAFE_HOME%/}/postgres}"
export MODELKEYGUARD_KEYCLOAK_DATA_DIR="${MODELKEYGUARD_KEYCLOAK_DATA_DIR:-${TOKENSAFE_HOME%/}/keycloak}"
mkdir -p "${MODELKEYGUARD_POSTGRES_DATA_DIR}" "${MODELKEYGUARD_KEYCLOAK_DATA_DIR}"

"${compose_cmd[@]}" up -d postgres keycloak
cat <<'TXT'
Stack started.
Keycloak: http://localhost:8080
Postgres: postgresql://modelguard:modelguard@localhost:5432/modelguard
Gateway:
  export MODELKEYGUARD_STORE=postgres
  export MODELKEYGUARD_POSTGRES_DSN=postgresql://modelguard:modelguard@localhost:5432/modelguard
  ./scripts/start_gateway.sh
TXT
