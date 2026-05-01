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
  ./scripts/production_compose.sh stop
  ./scripts/production_compose.sh start
  ./scripts/production_compose.sh logs
  ./scripts/production_compose.sh down
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

Pause/resume:
  stop pauses existing containers without removing them.
  start resumes containers stopped by stop.
  down removes containers/networks, but preserves mapped volumes. Use
  fresh-up or a dedicated reset path when you want a clean wipe.
TXT
}

case "${1:-}" in
  up|fresh-up|stop|start|logs|down|build|config|preflight)
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

compose_files=(-f docker-compose.yml -f docker-compose.container-secure.yml)
runtime_state_dir="${MODELKEYGUARD_RUNTIME_STATE_DIR:-./out}"
runtime_compose_data_file="${MODELKEYGUARD_RUNTIME_COMPOSE_DATA_FILE:-${runtime_state_dir%/}/.runtime-compose-data-dir}"
fresh_up_started=0
keycloak_bootstrap_admin_username="${MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME:-admin}"
keycloak_bootstrap_admin_password="${MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD:-admin}"

require_dockerignore_entry() {
  local pattern="$1"
  if [[ ! -f .dockerignore ]] || ! grep -Fxq "$pattern" .dockerignore; then
    echo ".dockerignore must exclude '${pattern}' before building production images." >&2
    exit 1
  fi
}

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

load_runtime_compose_data_dir() {
  if [[ ! -f "$runtime_compose_data_file" ]]; then
    return 0
  fi
  local fresh_root
  fresh_root="$(tr -d '\r\n' < "$runtime_compose_data_file")"
  if [[ -z "$fresh_root" ]]; then
    return 0
  fi
  if [[ -d "${fresh_root%/}/postgres" && -d "${fresh_root%/}/keycloak" ]]; then
    export MODELKEYGUARD_POSTGRES_DATA_DIR="${fresh_root%/}/postgres"
    export MODELKEYGUARD_KEYCLOAK_DATA_DIR="${fresh_root%/}/keycloak"
  fi
}

bootstrap_keycloak_admin_role() {
  KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8080}" \
  KEYCLOAK_REALM="${KEYCLOAK_REALM:-modelguard}" \
  MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME="$keycloak_bootstrap_admin_username" \
  MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD="$keycloak_bootstrap_admin_password" \
  MODELKEYGUARD_OIDC_ADMIN_CLIENT_ID="${MODELKEYGUARD_OIDC_ADMIN_CLIENT_ID:-modelguard-admin}" \
  MODELKEYGUARD_ADMIN_REQUIRED_ROLE="${MODELKEYGUARD_ADMIN_REQUIRED_ROLE:-model.admin}" \
    ./scripts/bootstrap_keycloak_admin_role.sh
}

bootstrap_keycloak_usage_role() {
  KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8080}" \
  KEYCLOAK_REALM="${KEYCLOAK_REALM:-modelguard}" \
  MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME="$keycloak_bootstrap_admin_username" \
  MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD="$keycloak_bootstrap_admin_password" \
  MODELKEYGUARD_KEYCLOAK_ROLE_GRANT_CLIENT_ID="${MODELKEYGUARD_OIDC_USAGE_CLIENT_ID:-modelguard-usage-agent}" \
  MODELKEYGUARD_KEYCLOAK_ROLE_GRANT_ROLE="${MODELKEYGUARD_USAGE_REQUIRED_ROLE:-model.usage.read}" \
    ./scripts/bootstrap_keycloak_admin_role.sh
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
  local keycloak_realm_import_file="${MODELKEYGUARD_KEYCLOAK_REALM_IMPORT_FILE:-./keycloak/modelguard-realm.json}"
  if [[ ! -f "${keycloak_realm_import_file}" ]]; then
    echo "missing Keycloak realm import file: ${keycloak_realm_import_file}" >&2
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
  fresh_up_started=1
  fresh_root="${MODELKEYGUARD_FRESH_ROOT:-./out/production_compose_fresh/$(date +%Y%m%d%H%M%S)-$$}"
  export MODELKEYGUARD_POSTGRES_DATA_DIR="${fresh_root%/}/postgres"
  export MODELKEYGUARD_KEYCLOAK_DATA_DIR="${fresh_root%/}/keycloak"
  mkdir -p "$MODELKEYGUARD_POSTGRES_DATA_DIR" "$MODELKEYGUARD_KEYCLOAK_DATA_DIR"
  mkdir -p "$(dirname "$runtime_compose_data_file")"
  printf '%s\n' "$fresh_root" > "$runtime_compose_data_file"

  echo "stopping production compose stack, including secure override services"
  "${compose_cmd[@]}" "${compose_files[@]}" down --remove-orphans -v >/dev/null 2>&1 || true

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

if [[ "$command_name" == "up" || "$command_name" == "start" || "$command_name" == "config" ]]; then
  load_runtime_compose_data_dir
fi

case "$command_name" in
  up|build|config)
    needs_bootstrap=1
    ;;
  *)
    needs_bootstrap=0
  ;;
esac

if [[ "$needs_bootstrap" == "1" ]]; then
  KEYCLOAK_INTROSPECTION_CLIENT_SECRET="${KEYCLOAK_INTROSPECTION_CLIENT_SECRET:-gateway-secret}" \
    ./scripts/bootstrap_secrets.sh --production
fi

case "$command_name" in
  up|build|config|preflight)
    preflight
    ;;
esac

case "$command_name" in
  preflight)
    ;;
  config)
    "${compose_cmd[@]}" "${compose_files[@]}" config
    ;;
  build)
    "${compose_cmd[@]}" "${compose_files[@]}" build gateway
    ;;
  down)
    "${compose_cmd[@]}" "${compose_files[@]}" down --remove-orphans
    ;;
  stop)
    "${compose_cmd[@]}" "${compose_files[@]}" stop
    cat <<'TXT'
production compose stack stopped without removing containers, networks, or data.
Resume it with:
  ./scripts/production_compose.sh start
TXT
    ;;
  start)
    "${compose_cmd[@]}" "${compose_files[@]}" start
    cat <<'TXT'
production compose stack resumed from stopped containers.
Follow logs with:
  ./scripts/production_compose.sh logs
TXT
    ;;
  logs)
    "${compose_cmd[@]}" "${compose_files[@]}" logs -f
    ;;
  up)
    "${compose_cmd[@]}" "${compose_files[@]}" down --remove-orphans >/dev/null 2>&1 || true
    "${compose_cmd[@]}" "${compose_files[@]}" up -d --build
    keycloak_bind="${MODELKEYGUARD_KEYCLOAK_BIND:-127.0.0.1:8080}"
    keycloak_port="${keycloak_bind##*:}"
    gateway_bind="${MODELKEYGUARD_GATEWAY_BIND:-127.0.0.1:8789}"
    gateway_port="${gateway_bind##*:}"
    wait_http "http://127.0.0.1:${keycloak_port}/realms/master/.well-known/openid-configuration" "Keycloak"
    bootstrap_keycloak_admin_role
    bootstrap_keycloak_usage_role
    if [[ "$fresh_up_started" -eq 1 ]]; then
      cat <<TXT
Keycloak bootstrap admin for this deployment:
  username: ${keycloak_bootstrap_admin_username}
  password: ${keycloak_bootstrap_admin_password}
Use this pair only for a fresh Keycloak data directory. If the realm already
exists, keep using the older admin that already works for that realm.
TXT
    fi
    wait_http "http://127.0.0.1:${gateway_port}/healthz" "Gateway"
    cat <<'TXT'
production compose stack started in the background.
Follow logs with:
  ./scripts/production_compose.sh logs
Stop it with:
  ./scripts/production_compose.sh stop
Remove containers/networks with:
  ./scripts/production_compose.sh down
TXT
    ;;
esac
