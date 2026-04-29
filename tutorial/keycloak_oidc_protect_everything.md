# Keycloak OIDC Protect Everything

This path makes Keycloak/OIDC the required authentication layer for both model
calls and admin operations. Local `kgw_*` tokens are rejected.

## 1. One-command local deployment smoke

Use this first. It starts local pgvector Postgres + Keycloak, initializes
Kogwistar-managed Postgres state, starts the gateway in OIDC-only mode, obtains
Keycloak model/admin tokens, and verifies the protected endpoints.

```bash
./scripts/oidc_protect_everything_smoke.sh
```

If another gateway is already using `8789`, either stop it or run the smoke on
a different port:

```bash
MODELKEYGUARD_PORT=8791 ./scripts/oidc_protect_everything_smoke.sh
```

To see the variables you need to override for another machine or CI runner:

```bash
./scripts/oidc_protect_everything_smoke.sh --print-required-env
```

For a non-local deployment, set these before running an equivalent deploy job:

```bash
export MODELKEYGUARD_POSTGRES_DSN='postgresql://modelguard:<password>@postgres-b.example:5432/modelguard'
export MODELKEYGUARD_GRAPH_KEY_FILE='/run/secrets/modelkeyguard_graph_key'
export KEYCLOAK_URL='https://keycloak-c.example'
export KEYCLOAK_REALM='modelguard'
export KEYCLOAK_INTROSPECTION_CLIENT_ID='modelguard-gateway'
export KEYCLOAK_INTROSPECTION_CLIENT_SECRET_FILE='/run/secrets/keycloak_client_secret'
export MODELKEYGUARD_OIDC_USER_CLIENT_ID='langchain-agent'
export MODELKEYGUARD_OIDC_USER_CLIENT_SECRET='<client-secret>'
export MODELKEYGUARD_OIDC_ADMIN_CLIENT_ID='modelguard-admin'
export MODELKEYGUARD_OIDC_ADMIN_CLIENT_SECRET='<admin-client-secret>'
```

The local smoke defaults to dry-run provider mode, so it does not need a real
OpenAI/Azure key.

## 2. Manual setup, same behavior

Use this section only when you want to run the script's steps by hand.

### 2.1 Start the local stack

```bash
./scripts/bootstrap_secrets.sh
./scripts/start_stack.sh
```

Initialize the graph with the bundled policy:

```bash
export MODELKEYGUARD_STORE='kogwistar_postgres'
export MODELKEYGUARD_POSTGRES_DSN='postgresql://modelguard:modelguard@localhost:5432/modelguard'
export MODELKEYGUARD_GRAPH_KEY='kogwistar-managed-postgres-dev-key-32-bytes-minimum'
export MODELKEYGUARD_INIT_RESET_EXISTING=1
export MODELKEYGUARD_KOGWISTAR_ENFORCE_INSTALLED_ONLY=1
export MODELKEYGUARD_USE_INSTALLED_KOGWISTAR=1
./scripts/init_graph.sh
```

### 2.2 Require OIDC for the gateway

```bash
export MODELKEYGUARD_AUTH_MODE='keycloak'
export MODELKEYGUARD_REQUIRE_KEYCLOAK=1
export MODELKEYGUARD_ADMIN_AUTH_MODE='keycloak'
export MODELKEYGUARD_ADMIN_REQUIRED_ROLE='model.admin'
export MODELKEYGUARD_REQUIRE_MODEL_LIST_AUTH=1
export KEYCLOAK_URL='http://localhost:8080'
export KEYCLOAK_REALM='modelguard'
export KEYCLOAK_INTROSPECTION_CLIENT_ID='modelguard-gateway'
export KEYCLOAK_INTROSPECTION_CLIENT_SECRET='gateway-secret'
./scripts/start_gateway.sh
```

In this mode:

- `/v1/chat/completions` requires a valid Keycloak bearer token.
- `/v1/models` also requires a valid bearer token.
- `/admin/*` requires a valid Keycloak bearer token mapped to `model.admin`.
- `x-modelkeyguard-admin-secret` is ignored for admin routes.
- Local `kgw_*` tokens are rejected.

This does not remove host/operator break-glass power. A Linux, Docker, cloud, or
Kubernetes administrator on the machine that hosts the gateway can still stop
containers, change environment variables, mount/read secrets, connect to
Postgres with database credentials, or deploy a different image. Treat that as
infrastructure administration and control it with SSH/IAM, audit logs, change
approval, backups, and secret rotation. OIDC protects normal HTTP access to the
gateway.

### 2.3 Get a real user/client token

The bundled realm includes a model client and an admin client:

```bash
USER_TOKEN="$(./scripts/get_agent_token.sh langchain-agent agent-secret)"
ADMIN_TOKEN="$(./scripts/get_agent_token.sh modelguard-admin admin-agent-secret)"
```

`langchain-agent` maps to `agent:doc-ingestor` with `model.invoke`.
`modelguard-admin` maps to `human:platform-admin` with `model.admin`.

### 2.4 Test model endpoint auth

No token should fail:

```bash
curl -sS -i http://127.0.0.1:8789/v1/models | head
```

User/client token should work:

```bash
curl -sS -H "Authorization: Bearer ${USER_TOKEN}" \
  http://127.0.0.1:8789/v1/models \
  | python -m json.tool
```

Model call:

```bash
OPENAI_BASE_URL='http://127.0.0.1:8789/v1' \
OPENAI_API_KEY="${USER_TOKEN}" \
OPENAI_MODEL='gpt-4o-mini' \
python scripts/langchain_user_openai_compatible.py
```

### 2.5 Test admin endpoint auth

Admin secret should fail in Keycloak-only mode:

```bash
curl -sS -i -H 'x-modelkeyguard-admin-secret: dev-modelkeyguard-admin-secret' \
  http://127.0.0.1:8789/admin/usage.json \
  | head
```

Non-admin model token should fail admin:

```bash
curl -sS -i -H "Authorization: Bearer ${USER_TOKEN}" \
  http://127.0.0.1:8789/admin/usage.json \
  | head
```

Admin token should work:

```bash
curl -sS -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  'http://127.0.0.1:8789/admin/usage.json?time_range=24h&bucket=hour' \
  | python -m json.tool
```

## CI/CD runner shape

The repo now has a local deployment smoke script. A GitHub/GitLab runner should
use the same contract:

1. build and push the gateway image,
2. deploy or point at pgvector Postgres,
3. deploy or point at Keycloak,
4. inject the variables printed by `--print-required-env`,
5. run the same smoke checks against the deployed gateway.

Minimum image commands:

```bash
docker build -t registry.example/token-safe/modelkeyguard:${CI_COMMIT_SHA:-local} .
docker push registry.example/token-safe/modelkeyguard:${CI_COMMIT_SHA:-local}
```

Do not run with `MODELKEYGUARD_INIT_RESET_EXISTING=1` against production data.
That flag is for repeatable local/dev smoke runs.

## Production Notes

Use HTTPS Keycloak URLs in real deployments:

```bash
export KEYCLOAK_URL='https://keycloak.example.com'
export KEYCLOAK_INTROSPECTION_CLIENT_SECRET_FILE='/run/secrets/keycloak_client_secret'
```

Keep `MODELKEYGUARD_ADMIN_AUTH_MODE=keycloak` and
`MODELKEYGUARD_REQUIRE_KEYCLOAK=1` when OIDC is the only allowed gateway auth
layer. Use `MODELKEYGUARD_ADMIN_AUTH_MODE=secret_or_keycloak` only during
migration windows.

For emergency fixes by trusted operators, prefer a documented break-glass runbook:

1. log in through audited host/cloud access,
2. pause or drain the gateway,
3. apply the database/config/image fix,
4. rotate any exposed secrets,
5. restart the gateway,
6. record the change in your incident/change system.
