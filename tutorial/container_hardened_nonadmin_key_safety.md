# Hardened Container Deployment (Real Azure Key, Non-Admin Cannot Recover)

This tutorial is a container-first deployment pattern for:

- real Azure upstream calls,
- real bearer token authentication,
- PostgreSQL-backed graph state,
- admin-only key management path,
- non-admin users limited to model-serving endpoints.

It uses:

- `docker-compose.yml` (base stack),
- `docker-compose.container-secure.yml` (hardening override),
- `deploy/nginx/modelkeyguard_public_only.conf` (public ingress policy).

## Security target and boundary

Target:

- non-admin callers can use model endpoints only,
- non-admin callers cannot reach `/admin/*`, `/docs`, `/openapi.json`,
- admin callers must present `x-modelkeyguard-admin-secret` or a valid admin session cookie on `/admin/*`,
- provider key plaintext is never returned by gateway APIs/UI.

Boundary:

- host/root administrators still have privileged access to host/container runtime and secrets.
- this guide protects against non-admin application users, not hostile host administrators.

## 0) Prerequisites

- Docker Engine + Docker Compose available.
- Real Azure API key (kept only in local secret file).
- Real bearer token source (for example Keycloak token from this stack or your external IdP).

Run from repo root:

```bash
cd "$(git rev-parse --show-toplevel)"
```

## 1) Create secret files (never commit these)

```bash
mkdir -p secrets
chmod 700 secrets

# 32+ chars graph key for sealed payload encryption.
python3 - <<'PY'
import secrets, pathlib
path = pathlib.Path("secrets/modelkeyguard_graph_key")
if not path.exists():
    path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
PY

# Keycloak client secret used by gateway introspection.
printf '%s' '<REAL_KEYCLOAK_INTROSPECTION_CLIENT_SECRET>' > secrets/keycloak_client_secret

# Compose base stack expects this file; value is not used for Azure-native key registration path.
printf '%s' 'placeholder-not-used-for-azure-native-flow' > secrets/openai_provider_key

chmod 600 secrets/modelkeyguard_graph_key secrets/keycloak_client_secret secrets/openai_provider_key
```

## 2) Start hardened container stack from scratch

Optional clean reset:

```bash
docker compose -f docker-compose.yml -f docker-compose.container-secure.yml down -v
```

Start:

```bash
docker compose -f docker-compose.yml -f docker-compose.container-secure.yml up --build -d
docker compose -f docker-compose.yml -f docker-compose.container-secure.yml ps
```

What this hardening does:

- gateway admin port is loopback-only: `127.0.0.1:8789`,
- postgres/keycloak ports are loopback-only,
- public ingress on `:8788` blocks `/admin/*`, `/docs`, `/openapi.json`,
- gateway runs in real-upstream mode (`MODELKEYGUARD_DRY_RUN=0`) with Postgres store.

## 3) Verify public non-admin surface

Public health:

```bash
curl -sS http://127.0.0.1:8788/healthz | python -m json.tool
```

Public admin should be denied:

```bash
curl -i http://127.0.0.1:8788/admin/keys
curl -i http://127.0.0.1:8788/docs
curl -i http://127.0.0.1:8788/openapi.json
```

Expected: `403` for those admin/schema paths.

## 4) Admin-only key registration path

Admin key management is intentionally on loopback-only gateway endpoint:

- `http://127.0.0.1:8789/admin/keys`

Admin auth:

```bash
export MODELKEYGUARD_ADMIN_API_SECRET='<ADMIN_SHARED_SECRET>'
```

If you are administering remotely, use SSH tunnel:

```bash
ssh -L 18789:127.0.0.1:8789 <ADMIN_SSH_HOST_ALIAS>
```

Then open:

- `http://127.0.0.1:18789/admin/keys`

Create key (UI form):

- `key_id`: `key:azure:chat:prod`
- `provider`: `azure_openai`
- `models`: your deployment names, comma-separated (example `gpt-5-mini,gpt4o`)
- `display_name`: descriptive label
- `intended_use`: paragraph contract
- `provider_secret`: paste real Azure key

Verify from admin-only API:

```bash
curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" http://127.0.0.1:8789/admin/keys.json | python -m json.tool
```

You should see:

- `active_secret_ref`,
- no raw `provider_secret`.

## 5) Get a real bearer token

If using the bundled Keycloak realm:

```bash
KGW_SAFE_TOKEN="$(./scripts/get_agent_token.sh langchain-agent agent-secret)"
echo "token length: ${#KGW_SAFE_TOKEN}"
```

If using external IdP, set your real token value:

```bash
KGW_SAFE_TOKEN='<REAL_BEARER_TOKEN>'
```

## 6) Non-admin real short request through public ingress

```bash
export KGW_BASE_URL='http://127.0.0.1:8788'
export KGW_SAFE_TOKEN
export KGW_AZURE_DEPLOYMENT='gpt-5-mini'
export KGW_AZURE_API_VERSION='2024-10-21'
export KGW_SYSTEM_PROMPT='You are a policy-compliant assistant.'
export KGW_USER_PROMPT='Reply in one short sentence.'

./scripts/smoke_azure_real_completion.sh
```

Expected:

- request succeeds via public ingress,
- real upstream response (not dry-run),
- no access to admin routes on same public endpoint.

## 7) LangChain request through public ingress (real token)

Prepare client env:

```bash
bash scripts/setup_langchain_smoke_env.sh
source .venv-langchain-smoke/bin/activate
```

Run a short structured request:

```bash
python scripts/smoke_langchain_azure_structured_real.py \
  --base-url 'http://127.0.0.1:8788' \
  --safe-token "$KGW_SAFE_TOKEN" \
  --deployment 'gpt-5-mini' \
  --api-version '2024-10-21'
```

Expected:

- structured JSON output (`summary`, `risk_level`, `action`).

## 8) Evidence that non-admin cannot recover key

1. Public ingress blocks admin routes (`403` on `/admin/*`).
2. Admin key listing returns only `secret_ref` handles, not raw secrets.
3. Audit records do not include provider secret fields.
4. Sealed payloads are encrypted under graph key; plaintext provider secret is not rendered back by gateway APIs.

Quick checks:

```bash
curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" http://127.0.0.1:8789/admin/keys.json | grep -i provider_secret || echo "ok: no provider_secret field in admin keys json"
grep -R --line-number "provider_secret" out/ || echo "ok: no provider_secret in out/"
```

## 9) Operational note

Because `/admin/*` is intentionally separated from model bearer-token auth, keep admin path private by network design:

- loopback-only gateway admin port (`127.0.0.1:8789`),
- separate public ingress with explicit path deny rules,
- admin actions through controlled SSH/bastion access.
