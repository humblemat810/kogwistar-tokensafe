#!/usr/bin/env bash
set -euo pipefail

# Start the local Keycloak container and wait for the realm discovery endpoint.
# This is the auth service used by the OIDC and Keycloak tutorial paths.
cd "$(dirname "$0")/.."
docker compose up -d keycloak
printf 'Waiting for Keycloak'
for i in {1..60}; do
  if curl -fsS http://localhost:8080/realms/modelguard/.well-known/openid-configuration >/dev/null 2>&1; then
    KEYCLOAK_URL="http://localhost:8080" \
    KEYCLOAK_REALM="modelguard" \
    MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME="${MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME:-admin}" \
    MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD="${MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD:-admin}" \
    MODELKEYGUARD_OIDC_ADMIN_CLIENT_ID="${MODELKEYGUARD_OIDC_ADMIN_CLIENT_ID:-modelguard-admin}" \
    MODELKEYGUARD_ADMIN_REQUIRED_ROLE="${MODELKEYGUARD_ADMIN_REQUIRED_ROLE:-model.admin}" \
      ./scripts/bootstrap_keycloak_admin_role.sh
    echo; echo 'Keycloak ready: http://localhost:8080/admin (admin/admin), realm=modelguard'
    exit 0
  fi
  printf '.'; sleep 2
done
echo ' Keycloak did not become ready in time' >&2
exit 1
