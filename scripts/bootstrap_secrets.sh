#!/usr/bin/env bash
set -euo pipefail

# Create secret files needed by the compose stack and gateway.
# Default mode is local/dev convenience. Use --production to refuse unsafe
# placeholders while generating only runtime/admin/IdP material by default.
MODE="local"
if [[ "${1:-}" == "--production" || "${1:-}" == "--prod" ]]; then
  MODE="production"
elif [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'TXT'
Usage:
  ./scripts/bootstrap_secrets.sh
  ./scripts/bootstrap_secrets.sh --production

Local mode:
  Creates missing local demo secrets, including placeholder provider material.

Production mode:
  Generates missing graph/admin/Keycloak secret files with strong random values.
  Does not create placeholder provider keys. Provider keys should normally be
  registered later through /admin/keys. If MODELKEYGUARD_PROVIDER_KEY_OPENAI or
  OPENAI_API_KEY is set, writes secrets/openai_provider_key as an optional
  runtime fallback.

Existing secret files are never overwritten.
TXT
  exit 0
elif [[ -n "${1:-}" ]]; then
  echo "unknown argument: ${1}" >&2
  exit 2
fi

random_secret() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
}

write_secret_if_missing() {
  local path="$1"
  local value="$2"
  if [[ ! -f "${path}" ]]; then
    printf '%s\n' "${value}" > "${path}"
  fi
}

mkdir -p secrets

write_secret_if_missing secrets/modelkeyguard_graph_key "${MODELKEYGUARD_GRAPH_KEY:-$(random_secret)}"

if [[ "${MODE}" == "production" ]]; then
  write_secret_if_missing secrets/modelkeyguard_admin_api_secret "${MODELKEYGUARD_ADMIN_API_SECRET:-$(random_secret)}"
  write_secret_if_missing secrets/keycloak_client_secret "${KEYCLOAK_INTROSPECTION_CLIENT_SECRET:-$(random_secret)}"

  if [[ ! -f secrets/openai_provider_key ]]; then
    provider_key="${MODELKEYGUARD_PROVIDER_KEY_OPENAI:-${OPENAI_API_KEY:-}}"
    if [[ -n "${provider_key}" ]]; then
      printf '%s\n' "${provider_key}" > secrets/openai_provider_key
    fi
  fi
else
  write_secret_if_missing secrets/modelkeyguard_admin_api_secret "${MODELKEYGUARD_ADMIN_API_SECRET:-$(random_secret)}"
  write_secret_if_missing secrets/keycloak_client_secret "${KEYCLOAK_INTROSPECTION_CLIENT_SECRET:-gateway-secret}"
  write_secret_if_missing secrets/openai_provider_key "${MODELKEYGUARD_PROVIDER_KEY_OPENAI:-${OPENAI_API_KEY:-dry-run-placeholder-provider-key}}"
fi

chmod 600 secrets/*
echo "secrets bootstrapped under ./secrets (${MODE})"
echo "admin secret file: ./secrets/modelkeyguard_admin_api_secret"

if [[ "${MODE}" == "production" ]]; then
  cat <<'TXT'
Production note:
  The bundled local Keycloak realm uses the demo introspection secret
  "gateway-secret". If you generated a different keycloak_client_secret, update
  the real Keycloak client secret to match before using OIDC introspection.
  Provider keys are normally registered after deploy through /admin/keys; this
  script only writes secrets/openai_provider_key when you explicitly provide
  MODELKEYGUARD_PROVIDER_KEY_OPENAI or OPENAI_API_KEY.
TXT
fi
