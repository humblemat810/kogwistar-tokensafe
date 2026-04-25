#!/usr/bin/env bash
set -euo pipefail
docker compose up -d postgres keycloak
cat <<'TXT'
Stack started.
Keycloak: http://localhost:8080
Postgres: postgresql://modelguard:modelguard@localhost:5432/modelguard
Gateway:
  export MODELKEYGUARD_STORE=postgres
  export MODELKEYGUARD_POSTGRES_DSN=postgresql://modelguard:modelguard@localhost:5432/modelguard
  ./scripts/start_gateway.sh
TXT
