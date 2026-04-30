#!/usr/bin/env bash
set -euo pipefail

# Render split-target deployment env files from one source of truth.
# Source values can come from the current environment or from one KEY=VALUE file.

usage() {
  cat <<'TXT'
Usage:
  ./scripts/render_deployment_env.sh --from-env deploy/rendered
  ./scripts/render_deployment_env.sh --env-file deploy/deployment-targets.env deploy/rendered
  ./scripts/render_deployment_env.sh deploy/deployment-targets.env deploy/rendered

Input:
  Either exported environment variables or one KEY=VALUE file containing the
  variables from deploy/deployment-targets.env.example.

Output:
  <output-dir>/gateway.env
  <output-dir>/postgres.env
  <output-dir>/keycloak.env
  <output-dir>/gateway-compose.env

Secrets:
  This renderer writes secret file paths and DSN placeholders. It does not write
  raw secret values. Mount real secrets separately.
TXT
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 1 ]]; then
  usage
  [[ $# -ge 1 ]] || exit 0
fi

input_label="current environment"
case "${1:-}" in
  --from-env)
    if [[ $# -ne 2 ]]; then
      usage >&2
      exit 2
    fi
    output_dir="$2"
    ;;
  --env-file)
    if [[ $# -ne 3 ]]; then
      usage >&2
      exit 2
    fi
    input_file="$2"
    output_dir="$3"
    if [[ ! -f "$input_file" ]]; then
      echo "input file not found: $input_file" >&2
      exit 1
    fi

    set -a
    # shellcheck disable=SC1090
    . "$input_file"
    set +a
    input_label="$input_file"
    ;;
  *)
    if [[ $# -ne 2 ]]; then
      usage >&2
      exit 2
    fi
    input_file="$1"
    output_dir="$2"
    if [[ ! -f "$input_file" ]]; then
      echo "input file not found: $input_file" >&2
      exit 1
    fi

    set -a
    # shellcheck disable=SC1090
    . "$input_file"
    set +a
    input_label="$input_file"
    ;;
esac

required_vars=(
  MODELKEYGUARD_GATEWAY_HOST
  MODELKEYGUARD_GATEWAY_PORT
  MODELKEYGUARD_GATEWAY_PUBLIC_URL
  MODELKEYGUARD_POSTGRES_HOST
  MODELKEYGUARD_POSTGRES_PORT
  MODELKEYGUARD_POSTGRES_DB
  MODELKEYGUARD_POSTGRES_USER
  MODELKEYGUARD_KEYCLOAK_HOST
  MODELKEYGUARD_KEYCLOAK_PORT
  MODELKEYGUARD_KEYCLOAK_PUBLIC_URL
  MODELKEYGUARD_KEYCLOAK_REALM
  MODELKEYGUARD_KEYCLOAK_INTROSPECTION_CLIENT_ID
  MODELKEYGUARD_ADMIN_REQUIRED_ROLE
)

for name in "${required_vars[@]}"; do
  value="${!name:-}"
  if [[ -z "$value" || "$value" == *"<"* || "$value" == *">"* ]]; then
    echo "set ${name} in ${input_label} before rendering deployment env files" >&2
    exit 1
  fi
done

postgres_password_placeholder="${MODELKEYGUARD_POSTGRES_PASSWORD_PLACEHOLDER:-<postgres-password>}"
postgres_dsn="postgresql://${MODELKEYGUARD_POSTGRES_USER}:${postgres_password_placeholder}@${MODELKEYGUARD_POSTGRES_HOST}:${MODELKEYGUARD_POSTGRES_PORT}/${MODELKEYGUARD_POSTGRES_DB}"

mkdir -p "$output_dir"
output_dir_abs="$(cd "$output_dir" && pwd)"

cat >"${output_dir}/gateway.env" <<EOF
# Rendered from ${input_label}. Do not edit by hand; edit the source target values.
MODELKEYGUARD_GATEWAY_HOST=${MODELKEYGUARD_GATEWAY_HOST}
MODELKEYGUARD_GATEWAY_PORT=${MODELKEYGUARD_GATEWAY_PORT}
MODELKEYGUARD_GATEWAY_PUBLIC_URL=${MODELKEYGUARD_GATEWAY_PUBLIC_URL}

MODELKEYGUARD_POSTGRES_HOST=${MODELKEYGUARD_POSTGRES_HOST}
MODELKEYGUARD_POSTGRES_PORT=${MODELKEYGUARD_POSTGRES_PORT}
MODELKEYGUARD_POSTGRES_DB=${MODELKEYGUARD_POSTGRES_DB}
MODELKEYGUARD_POSTGRES_USER=${MODELKEYGUARD_POSTGRES_USER}
MODELKEYGUARD_POSTGRES_DSN=${postgres_dsn}

MODELKEYGUARD_KEYCLOAK_HOST=${MODELKEYGUARD_KEYCLOAK_HOST}
MODELKEYGUARD_KEYCLOAK_PORT=${MODELKEYGUARD_KEYCLOAK_PORT}
MODELKEYGUARD_KEYCLOAK_PUBLIC_URL=${MODELKEYGUARD_KEYCLOAK_PUBLIC_URL}
MODELKEYGUARD_KEYCLOAK_REALM=${MODELKEYGUARD_KEYCLOAK_REALM}
MODELKEYGUARD_KEYCLOAK_INTROSPECTION_CLIENT_ID=${MODELKEYGUARD_KEYCLOAK_INTROSPECTION_CLIENT_ID}

MODELKEYGUARD_ENV=production
MODELKEYGUARD_STORE=kogwistar_postgres
MODELKEYGUARD_GRAPH_KEY_FILE=/run/secrets/modelkeyguard_graph_key
MODELKEYGUARD_ADMIN_API_SECRET_FILE=/run/secrets/modelkeyguard_admin_api_secret
KEYCLOAK_INTROSPECTION_CLIENT_SECRET_FILE=/run/secrets/keycloak_client_secret
MODELKEYGUARD_KOGWISTAR_ENFORCE_INSTALLED_ONLY=1
MODELKEYGUARD_USE_INSTALLED_KOGWISTAR=1

KEYCLOAK_URL=${MODELKEYGUARD_KEYCLOAK_PUBLIC_URL}
KEYCLOAK_REALM=${MODELKEYGUARD_KEYCLOAK_REALM}
KEYCLOAK_INTROSPECTION_CLIENT_ID=${MODELKEYGUARD_KEYCLOAK_INTROSPECTION_CLIENT_ID}

MODELKEYGUARD_AUTH_MODE=keycloak
MODELKEYGUARD_REQUIRE_KEYCLOAK=1
MODELKEYGUARD_ADMIN_AUTH_MODE=keycloak
MODELKEYGUARD_ADMIN_REQUIRED_ROLE=${MODELKEYGUARD_ADMIN_REQUIRED_ROLE}
MODELKEYGUARD_REQUIRE_MODEL_LIST_AUTH=1

MODELKEYGUARD_DRY_RUN=0
MODELKEYGUARD_HOST=0.0.0.0
MODELKEYGUARD_PORT=${MODELKEYGUARD_GATEWAY_PORT}
MODELKEYGUARD_POLICY_PATH=config/gateway_policy.json
EOF

cat >"${output_dir}/postgres.env" <<EOF
# Rendered from ${input_label}. Do not edit by hand; edit the source target values.
POSTGRES_IMAGE=pgvector/pgvector:pg16
POSTGRES_DB=${MODELKEYGUARD_POSTGRES_DB}
POSTGRES_USER=${MODELKEYGUARD_POSTGRES_USER}
POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password
POSTGRES_PORT=${MODELKEYGUARD_POSTGRES_PORT}
POSTGRES_DATA_DIR=/var/lib/modelkeyguard/postgres
MODELKEYGUARD_POSTGRES_HOST=${MODELKEYGUARD_POSTGRES_HOST}
MODELKEYGUARD_POSTGRES_DSN=${postgres_dsn}
EOF

cat >"${output_dir}/keycloak.env" <<EOF
# Rendered from ${input_label}. Do not edit by hand; edit the source target values.
MODELKEYGUARD_KEYCLOAK_HOST=${MODELKEYGUARD_KEYCLOAK_HOST}
MODELKEYGUARD_KEYCLOAK_PORT=${MODELKEYGUARD_KEYCLOAK_PORT}
MODELKEYGUARD_KEYCLOAK_PUBLIC_URL=${MODELKEYGUARD_KEYCLOAK_PUBLIC_URL}
MODELKEYGUARD_KEYCLOAK_REALM=${MODELKEYGUARD_KEYCLOAK_REALM}
MODELKEYGUARD_KEYCLOAK_INTROSPECTION_CLIENT_ID=${MODELKEYGUARD_KEYCLOAK_INTROSPECTION_CLIENT_ID}
MODELKEYGUARD_OIDC_BROWSER_CLIENT_ID=${MODELKEYGUARD_OIDC_BROWSER_CLIENT_ID:-<client-id-for-browser-admin-login>}
MODELKEYGUARD_OIDC_USAGE_CLIENT_ID=${MODELKEYGUARD_OIDC_USAGE_CLIENT_ID:-<client-id-for-usage-agent>}

KEYCLOAK_URL=${MODELKEYGUARD_KEYCLOAK_PUBLIC_URL}
KEYCLOAK_REALM=${MODELKEYGUARD_KEYCLOAK_REALM}
KEYCLOAK_INTROSPECTION_CLIENT_ID=${MODELKEYGUARD_KEYCLOAK_INTROSPECTION_CLIENT_ID}
KEYCLOAK_INTROSPECTION_CLIENT_SECRET_FILE=/run/secrets/keycloak_client_secret

MODELKEYGUARD_OIDC_USER_CLIENT_ID=${MODELKEYGUARD_OIDC_USER_CLIENT_ID:-<client-id-for-model-users>}
MODELKEYGUARD_OIDC_ADMIN_CLIENT_ID=${MODELKEYGUARD_OIDC_ADMIN_CLIENT_ID:-<client-id-for-admins>}
MODELKEYGUARD_OIDC_USAGE_CLIENT_ID=${MODELKEYGUARD_OIDC_USAGE_CLIENT_ID:-<client-id-for-usage-agent>}
MODELKEYGUARD_ADMIN_REQUIRED_ROLE=${MODELKEYGUARD_ADMIN_REQUIRED_ROLE}
MODELKEYGUARD_USAGE_REQUIRED_ROLE=${MODELKEYGUARD_USAGE_REQUIRED_ROLE:-model.usage.read}
EOF

cat >"${output_dir}/gateway-compose.env" <<EOF
# Rendered from ${input_label}. Use with deploy/docker-compose.gateway-only.yml.
MODELKEYGUARD_IMAGE=${MODELKEYGUARD_IMAGE:-token-safe-gateway:latest}
MODELKEYGUARD_GATEWAY_ENV_FILE=${output_dir_abs}/gateway.env
MODELKEYGUARD_GATEWAY_BIND=${MODELKEYGUARD_GATEWAY_BIND:-127.0.0.1:${MODELKEYGUARD_GATEWAY_PORT}}
MODELKEYGUARD_GRAPH_KEY_SOURCE=${MODELKEYGUARD_GRAPH_KEY_SOURCE:-../secrets/modelkeyguard_graph_key}
MODELKEYGUARD_ADMIN_API_SECRET_SOURCE=${MODELKEYGUARD_ADMIN_API_SECRET_SOURCE:-../secrets/modelkeyguard_admin_api_secret}
KEYCLOAK_INTROSPECTION_CLIENT_SECRET_SOURCE=${KEYCLOAK_INTROSPECTION_CLIENT_SECRET_SOURCE:-../secrets/keycloak_client_secret}
EOF

cat <<TXT
rendered deployment env files under ${output_dir}
  ${output_dir}/gateway.env
  ${output_dir}/postgres.env
  ${output_dir}/keycloak.env
  ${output_dir}/gateway-compose.env
TXT
