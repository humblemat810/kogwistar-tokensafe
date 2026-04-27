# Final Dev Guard Setup (Azure Real Token + PostgreSQL, CLI + GUI Parity)

This guide is for final development guard validation before UAT/production using:

- real Azure upstream access,
- **PostgreSQL-backed graph state** (authoritative store),
- CLI and GUI admin parity checks.

Security constraints in this document:

- no real tokens are shown,
- no real usernames are shown,
- placeholders and local secret files only.

## 1. Scope and smoke targets

This run validates:

1. real Azure chat completion through gateway,
2. real LangChain structured output through gateway.

Azure deployment names (from your list):

- `gpt-5-chat`
- `gpt-5-mini`
- `gpt-5-nano`
- `gpt-5.3-chat`
- `gpt4o`
- `gpt4omini`
- `text-embedding-3-large`
- `text-embedding-ada-002`

Note:

- Azure native compatibility now includes both chat-completions and responses routes.
- Newer client/model combinations may use `/openai/responses` automatically.
- Embedding deployments are still registered in key inventory for management readiness.

## 2. Prepare local environment

From repo root:

```bash
./scripts/reset_local_e2e_state.sh
```

Then prepare Python:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Docker preflight:

```bash
docker info >/dev/null && echo "docker ok"
```

If this shell reports Docker socket permission denied:

```bash
newgrp docker
docker info >/dev/null && echo "docker ok"
```

## 3. Start PostgreSQL (required)

Start Postgres from the repo compose stack:

```bash
./scripts/start_postgres.sh
docker compose ps postgres || docker-compose ps postgres
```

Optional readiness check:

```bash
docker compose exec -T postgres pg_isready -U modelguard -d modelguard
```

## 4. Configure gateway to use PostgreSQL

Set runtime state (Postgres-backed, real upstream mode):

```bash
export MODELKEYGUARD_STORE='postgres'
export MODELKEYGUARD_POSTGRES_DSN='postgresql://modelguard:modelguard@localhost:5432/modelguard'
export MODELKEYGUARD_AUDIT_PATH='out/finaldev_audit.jsonl'
export MODELKEYGUARD_REVIEW_OUT='out/finaldev_review.jsonl'
export MODELKEYGUARD_GRAPH_KEY='<32+ char random value>'
export MODELKEYGUARD_DRY_RUN=0
export MODELKEYGUARD_ADMIN_API_SECRET='<ADMIN_SHARED_SECRET>'
```

Initialize policy graph into Postgres once:

```bash
./scripts/init_graph.sh
```

Verify graph rows exist in Postgres:

```bash
docker compose exec -T postgres psql -U modelguard -d modelguard -c "select count(*) as graph_nodes from graph_nodes;"
docker compose exec -T postgres psql -U modelguard -d modelguard -c "select count(*) as graph_edges from graph_edges;"
```

## 5. Prepare secrets safely (no plaintext in history)

Create local secret files:

```bash
mkdir -p .secrets
chmod 700 .secrets
printf '%s' '<REAL_AZURE_API_KEY>' > .secrets/azure_api_key.txt
printf '%s' '<SHARED_SECRET_FOR_SECURITY_EVENTS>' > .secrets/security_events_secret.txt
chmod 600 .secrets/*.txt
```

Load security-event secret via file/env:

```bash
export SECURITY_EVENT_SHARED_SECRET_FILE='.secrets/security_events_secret.txt'
```

## 6. Start gateway

```bash
./scripts/start_gateway.sh
```

Health check:

```bash
curl -sS http://127.0.0.1:8789/healthz | python -m json.tool
```

## 7. Register Azure keys and deployment mappings

Recommendation for final-dev/UAT: separate logical keys by function to reduce revoke/rotate blast radius.

### CLI pathway

All admin API calls below require:

```bash
-H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}"
```

Chat deployment key:

```bash
curl -sS -X POST http://127.0.0.1:8789/admin/keys \
  -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  -H 'content-type: application/x-www-form-urlencoded' \
  --data-urlencode 'key_id=key:azure:chat:prod' \
  --data-urlencode 'provider=azure_openai' \
  --data-urlencode 'models=gpt-5-chat,gpt-5-mini,gpt-5-nano,gpt-5.3-chat,gpt4o,gpt4omini' \
  --data-urlencode 'display_name=Azure Chat Production' \
  --data-urlencode "provider_secret=$(cat .secrets/azure_api_key.txt)" \
  | python -m json.tool
```

Embedding inventory key:

```bash
curl -sS -X POST http://127.0.0.1:8789/admin/keys \
  -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  -H 'content-type: application/x-www-form-urlencoded' \
  --data-urlencode 'key_id=key:azure:embedding:prod' \
  --data-urlencode 'provider=azure_openai' \
  --data-urlencode 'models=text-embedding-3-large,text-embedding-ada-002' \
  --data-urlencode 'display_name=Azure Embedding Production' \
  --data-urlencode "provider_secret=$(cat .secrets/azure_api_key.txt)" \
  | python -m json.tool
```

### GUI pathway

Open:

- `http://127.0.0.1:8789/admin/keys`
- sign in via `/admin/session` using `MODELKEYGUARD_ADMIN_API_SECRET`.

Create same entries with fields:

- `key_id`
- `provider=azure_openai`
- `models` comma-separated
- `display_name`
- `provider_secret`

### Verify CLI/GUI parity

CLI:

```bash
curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" http://127.0.0.1:8789/admin/keys.json | python -m json.tool
curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" 'http://127.0.0.1:8789/admin/history.json?time_range=24h&page_size=20' | python -m json.tool
```

GUI:

- confirm `/admin/keys` table rows match JSON.

## 8. Smoke test A: real normal completion

Set runtime values:

```bash
export KGW_BASE_URL='http://127.0.0.1:8789'
export KGW_SAFE_TOKEN='<MODELKEYGUARD_SAFE_TOKEN_OR_KEYCLOAK_ACCESS_TOKEN>'
export KGW_AZURE_DEPLOYMENT='gpt-5-mini'
export KGW_AZURE_API_VERSION='2024-10-21'
```

Run smoke:

```bash
./scripts/smoke_azure_real_completion.sh
```

Expected:

- HTTP 200,
- standard chat response payload,
- response generated by Azure upstream (not dry-run mock).

## 9. Smoke test B: LangChain structured outputs (real)

Prepare separate smoke environment:

```bash
bash scripts/setup_langchain_smoke_env.sh
source .venv-langchain-smoke/bin/activate
```

Run structured-output smoke:

```bash
python scripts/smoke_langchain_azure_structured_real.py \
  --base-url 'http://127.0.0.1:8789' \
  --safe-token '<MODELKEYGUARD_SAFE_TOKEN_OR_KEYCLOAK_ACCESS_TOKEN>' \
  --deployment 'gpt-5-mini' \
  --api-version '2024-10-21'
```

Expected:

- JSON object printed with:
  - `summary`
  - `risk_level`
  - `action`

## 10. Confirm data is in PostgreSQL (not JSONL graph)

Graph tables should advance after requests:

```bash
docker compose exec -T postgres psql -U modelguard -d modelguard -c "select count(*) as graph_events from graph_events;"
docker compose exec -T postgres psql -U modelguard -d modelguard -c "select count(*) as named_projections from named_projections;"
```

Audit file still exists for review worker input:

```bash
ls -l out/finaldev_audit.jsonl
tail -n 5 out/finaldev_audit.jsonl
```

## 11. Operations parity: rotate/revoke/list and usage visibility

Rotate key:

- CLI:

```bash
curl -sS -X POST http://127.0.0.1:8789/admin/keys/rotate \
  -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  -H 'content-type: application/x-www-form-urlencoded' \
  --data-urlencode 'key_id=key:azure:chat:prod' \
  --data-urlencode "provider_secret=$(cat .secrets/azure_api_key.txt)" \
  | python -m json.tool
```

- GUI:
  - `http://127.0.0.1:8789/admin/keys`
  - use **Rotate key** form.

Revoke key:

- CLI:

```bash
curl -sS -X POST http://127.0.0.1:8789/admin/keys/key:azure:chat:prod/revoke \
  -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  -H 'content-type: application/x-www-form-urlencoded' \
  --data-urlencode 'reason=final-dev-test-revoke' \
  | python -m json.tool
```

- GUI:
  - same row in `/admin/keys`, use revoke action.

Usage monitor:

- GUI page: `http://127.0.0.1:8789/admin/usage`
- CLI data API:

```bash
curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" 'http://127.0.0.1:8789/admin/usage.json?time_range=24h&bucket=hour' | python -m json.tool
curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" 'http://127.0.0.1:8789/admin/history.json?time_range=24h&page_size=20' | python -m json.tool
```

Manual review trigger:

```bash
curl -sS -X POST http://127.0.0.1:8789/admin/review/run \
  -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  -H 'content-type: application/json' \
  -d '{"sample_size":200,"run_llm_review":false,"lookback_minutes":120,"out_path":"out/finaldev_review.jsonl","checkpoint_path":"out/finaldev_review_checkpoint.json"}' \
  | python -m json.tool
```

## 12. Security-events intake test (optional, recommended)

```bash
export SECURITY_EVENT_SHARED_SECRET='<same value in secret file>'
curl -sS -X POST http://127.0.0.1:8789/admin/security-events \
  -H "x-modelkeyguard-security-secret: ${SECURITY_EVENT_SHARED_SECRET}" \
  -H 'content-type: application/json' \
  -d '{"event_type":"admin_ssh_login","username":"<ADMIN_ALIAS>","host":"<HOST_ALIAS>","source_ip":"203.0.113.10","auth_method":"publickey"}' \
  | python -m json.tool
```

## 13. Exit criteria before UAT

1. Both smoke tests pass in `MODELKEYGUARD_DRY_RUN=0`.
2. `/admin/keys` and `/admin/keys.json` show expected mappings.
3. `/admin/usage` shows smoke requests.
4. `/admin/review/run` completes and writes output.
5. Postgres `graph_events` and `named_projections` are updating.
6. Key rotate/revoke tested once in dev.
7. No secrets committed to repo or shell history.
