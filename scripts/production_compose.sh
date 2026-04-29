#!/usr/bin/env bash
set -euo pipefail

# Production-style single-host Compose runner.
# This is for local production rehearsal or a small single-host deployment:
# it bootstraps runtime secrets, verifies Docker build-context guardrails, and
# runs compose with the hardened override. For split-host production, use the
# same environment contract in your orchestrator.

usage() {
  cat <<'TXT'
Usage:
  ./scripts/production_compose.sh up
  ./scripts/production_compose.sh fresh-up
  ./scripts/production_compose.sh build
  ./scripts/production_compose.sh config
  ./scripts/production_compose.sh preflight

What it does:
  - runs ./scripts/bootstrap_secrets.sh --production when needed
  - verifies .dockerignore excludes runtime data, secrets, and reference clones
  - verifies required runtime secret files exist
  - runs docker compose with docker-compose.container-secure.yml
  - uses the bundled local Keycloak realm, whose gateway client secret is
    gateway-secret; split-host production should configure its own IdP secret
    and deployment wrapper.

Split target note:
  Docker Compose here is a single-host runner. For gateway on machine A,
  Postgres on machine B, and Keycloak/OIDC on machine C, use the templates in:
    deploy/gateway.env.example
    deploy/postgres.env.example
    deploy/keycloak.env.example
    deploy/docker-compose.gateway-only.yml

Provider keys:
  Register provider keys after deploy through /admin/keys. This script does not
  require OPENAI_API_KEY or any provider secret at bootstrap time.

Fresh local rehearsal:
  fresh-up stops the local compose stack, then starts with a new local data
  directory so it cannot reuse stale encrypted Postgres state. Use it only for
  local rehearsal, never against production data you need to keep.
TXT
}

case "${1:-}" in
  up|fresh-up|build|config|preflight)
    command_name="$1"
    ;;
  -h|--help|"")
    usage
    exit 0
    ;;
  *)
    echo "unknown command: ${1}" >&2
    usage >&2
    exit 2
    ;;
esac

if docker compose version >/dev/null 2>&1; then
  compose_cmd=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose_cmd=(docker-compose)
else
  echo "Neither 'docker compose' nor 'docker-compose' is available." >&2
  exit 1
fi

require_dockerignore_entry() {
  local pattern="$1"
  if [[ ! -f .dockerignore ]] || ! grep -Fxq "$pattern" .dockerignore; then
    echo ".dockerignore must exclude '${pattern}' before building production images." >&2
    exit 1
  fi
}

preflight() {
  require_dockerignore_entry "data"
  require_dockerignore_entry "out"
  require_dockerignore_entry "secrets"
  require_dockerignore_entry "kogwistar_reference_only"

  if [[ ! -f secrets/modelkeyguard_graph_key ]]; then
    echo "missing secrets/modelkeyguard_graph_key" >&2
    exit 1
  fi
  if [[ ! -f secrets/modelkeyguard_admin_api_secret ]]; then
    echo "missing secrets/modelkeyguard_admin_api_secret" >&2
    exit 1
  fi
  if [[ ! -f secrets/keycloak_client_secret ]]; then
    echo "missing secrets/keycloak_client_secret" >&2
    exit 1
  fi
  local keycloak_secret
  keycloak_secret="$(tr -d '\r\n' < secrets/keycloak_client_secret)"
  if [[ "${keycloak_secret}" != "gateway-secret" ]]; then
    cat >&2 <<'TXT'
secrets/keycloak_client_secret does not match the bundled local Keycloak realm.
This single-host Compose runner imports keycloak/modelguard-realm.json, where
the modelguard-gateway client secret is "gateway-secret".

For this local Compose runner, rotate secrets/keycloak_client_secret to
"gateway-secret" before starting. For real split-host production, configure your
IdP client secret and deploy the gateway with matching *_FILE env vars in your
orchestrator instead of this bundled local Keycloak runner.
TXT
    exit 1
  fi

  echo "production compose preflight passed"
}

if [[ "$command_name" == "fresh-up" ]]; then
  fresh_root="${MODELKEYGUARD_FRESH_ROOT:-./out/production_compose_fresh/$(date +%Y%m%d%H%M%S)-$$}"
  export MODELKEYGUARD_POSTGRES_DATA_DIR="${fresh_root%/}/postgres"
  export MODELKEYGUARD_KEYCLOAK_DATA_DIR="${fresh_root%/}/keycloak"
  mkdir -p "$MODELKEYGUARD_POSTGRES_DATA_DIR" "$MODELKEYGUARD_KEYCLOAK_DATA_DIR"

  MODELKEYGUARD_RESET_DATA_DIR="$MODELKEYGUARD_POSTGRES_DATA_DIR" \
    MODELKEYGUARD_RESET_KEYCLOAK_DATA_DIR="$MODELKEYGUARD_KEYCLOAK_DATA_DIR" \
    MODELKEYGUARD_RESET_OUT_DIR="${MODELKEYGUARD_RESET_OUT_DIR:-./out}" \
    ./scripts/reset_local_e2e_state.sh

  mkdir -p "$MODELKEYGUARD_POSTGRES_DATA_DIR" "$MODELKEYGUARD_KEYCLOAK_DATA_DIR"
  cat <<TXT
fresh local rehearsal data directory:
  ${fresh_root}

Note: old local data directories are left in place if the current user cannot
delete container-owned files. This run will not reuse them.
TXT
  command_name="up"
fi

if [[ "$command_name" != "preflight" ]]; then
  KEYCLOAK_INTROSPECTION_CLIENT_SECRET="${KEYCLOAK_INTROSPECTION_CLIENT_SECRET:-gateway-secret}" \
    ./scripts/bootstrap_secrets.sh --production
fi

preflight

case "$command_name" in
  preflight)
    ;;
  config)
    "${compose_cmd[@]}" -f docker-compose.yml -f docker-compose.container-secure.yml config
    ;;
  build)
    "${compose_cmd[@]}" -f docker-compose.yml -f docker-compose.container-secure.yml build gateway
    ;;
  up)
    "${compose_cmd[@]}" -f docker-compose.yml -f docker-compose.container-secure.yml up --build
    ;;
esac
