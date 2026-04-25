#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose up -d keycloak
printf 'Waiting for Keycloak'
for i in {1..60}; do
  if curl -fsS http://localhost:8080/realms/modelguard/.well-known/openid-configuration >/dev/null 2>&1; then
    echo; echo 'Keycloak ready: http://localhost:8080/admin (admin/admin), realm=modelguard'
    exit 0
  fi
  printf '.'; sleep 2
done
echo ' Keycloak did not become ready in time' >&2
exit 1
