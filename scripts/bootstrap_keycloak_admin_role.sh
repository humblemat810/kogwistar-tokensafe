#!/usr/bin/env bash
set -euo pipefail

# Assign the realm admin role to the bundled Keycloak service-account client.
# This is idempotent and safe to rerun. It is used by local compose and by the
# production rehearsal runner so the admin bearer-token path is actually usable.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  if [[ -x "${ROOT}/.venv/bin/python" ]]; then
    PYTHON_BIN="${ROOT}/.venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8080}"
KEYCLOAK_REALM="${KEYCLOAK_REALM:-modelguard}"
BOOTSTRAP_USER="${MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME:-admin}"
BOOTSTRAP_PASSWORD="${MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD:-admin}"
ADMIN_CLIENT_ID="${MODELKEYGUARD_OIDC_ADMIN_CLIENT_ID:-modelguard-admin}"
REQUIRED_ROLE="${MODELKEYGUARD_ADMIN_REQUIRED_ROLE:-model.admin}"
export KEYCLOAK_URL KEYCLOAK_REALM BOOTSTRAP_USER BOOTSTRAP_PASSWORD ADMIN_CLIENT_ID REQUIRED_ROLE

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

wait_http "${KEYCLOAK_URL}/realms/master/.well-known/openid-configuration" "Keycloak"

"${PYTHON_BIN}" - <<'PY'
import json
import os
import urllib.error
import urllib.parse
import urllib.request

base = os.environ["KEYCLOAK_URL"].rstrip("/")
realm = os.environ["KEYCLOAK_REALM"]
bootstrap_user = os.environ["BOOTSTRAP_USER"]
bootstrap_password = os.environ["BOOTSTRAP_PASSWORD"]
admin_client_id = os.environ["ADMIN_CLIENT_ID"]
required_role = os.environ["REQUIRED_ROLE"]


def request_json(method: str, url: str, token: str | None = None, data: object | None = None):
    headers = {}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None


def request_form(url: str, payload: dict[str, str]):
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None


token = request_form(
    f"{base}/realms/master/protocol/openid-connect/token",
    {
        "grant_type": "password",
        "client_id": "admin-cli",
        "username": bootstrap_user,
        "password": bootstrap_password,
    },
).get("access_token")

if not token:
    raise SystemExit("failed to obtain bootstrap admin token from Keycloak")

clients = request_json("GET", f"{base}/admin/realms/{realm}/clients?clientId={urllib.parse.quote(admin_client_id, safe='')}", token=token)
if not clients:
    raise SystemExit(f"Keycloak client not found: {admin_client_id}")

client_uuid = clients[0]["id"]
service_user = request_json("GET", f"{base}/admin/realms/{realm}/clients/{client_uuid}/service-account-user", token=token)
if not service_user or not service_user.get("id"):
    raise SystemExit(f"service account user not found for client: {admin_client_id}")

user_id = service_user["id"]
role = request_json("GET", f"{base}/admin/realms/{realm}/roles/{urllib.parse.quote(required_role, safe='')}", token=token)
if not role:
    raise SystemExit(f"required realm role not found: {required_role}")

current = request_json("GET", f"{base}/admin/realms/{realm}/users/{user_id}/role-mappings/realm", token=token) or []
if any(str(item.get("name")) == required_role for item in current):
    print(f"Keycloak service account already has realm role: {required_role}")
    raise SystemExit(0)

request_json("POST", f"{base}/admin/realms/{realm}/users/{user_id}/role-mappings/realm", token=token, data=[role])
print(f"Assigned realm role {required_role} to service account client {admin_client_id}")
PY
