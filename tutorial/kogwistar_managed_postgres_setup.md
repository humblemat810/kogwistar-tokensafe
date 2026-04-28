# Kogwistar Managed Postgres Setup

This is the copy-paste path for the serious backend:

- runtime Kogwistar is pip-installed, never imported from `./kogwistar_reference_only`;
- graph authority is stored through Kogwistar Postgres primitives;
- current serving views and hot reads use Kogwistar named projections;
- `MODELKEYGUARD_GRAPH_PATH` is not created as a JSONL graph artifact.

The commands are retry-idempotent for local development because they reset the local Postgres graph state before initialization.

## 1. Install runtime dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,postgres]"
```

## 2. Start Postgres

```bash
./scripts/start_postgres.sh
docker compose ps postgres || docker-compose ps postgres
```

The local compose stack uses `pgvector/pgvector:pg16` because installed Kogwistar creates the PostgreSQL `vector` extension during backend initialization.

## 3. Configure managed Kogwistar Postgres

```bash
export MODELKEYGUARD_STORE='kogwistar_postgres'
export MODELKEYGUARD_POSTGRES_DSN='postgresql://modelguard:modelguard@localhost:5432/modelguard'
export MODELKEYGUARD_GRAPH_KEY='kogwistar-managed-postgres-dev-key-32-bytes-minimum'
export MODELKEYGUARD_GRAPH_PATH="$PWD/out/should_not_exist_graph.jsonl"
export MODELKEYGUARD_AUDIT_PATH="$PWD/out/kogwistar_managed_audit.jsonl"
export MODELKEYGUARD_DRY_RUN=1
export MODELKEYGUARD_INIT_RESET_EXISTING=1
export MODELKEYGUARD_KOGWISTAR_EMBED_DIM=2
export MODELKEYGUARD_KOGWISTAR_ENFORCE_INSTALLED_ONLY=1
export MODELKEYGUARD_USE_INSTALLED_KOGWISTAR=1
```

## 4. Initialize and verify

```bash
python scripts/kogwistar_postgres_no_jsonl_smoke.py
./scripts/init_graph.sh
test ! -e "$MODELKEYGUARD_GRAPH_PATH"
```

Expected result:

```text
initialized encrypted graph: postgresql://modelguard:modelguard@localhost:5432/modelguard
...
"ok": true
```

The smoke script resets the configured local Postgres DSN with its own smoke key, creates its own fresh temporary working directory, and fails if any `*.jsonl` file appears there. Run `./scripts/init_graph.sh` after the smoke before starting the gateway so the database is sealed with your configured `MODELKEYGUARD_GRAPH_KEY`.
It also fails if current node/edge serving state leaks into Kogwistar graph rows instead of named projections.

## 5. Run the gateway

```bash
./scripts/start_gateway.sh
```

In another terminal:

```bash
source .venv/bin/activate
export OPENAI_BASE_URL='http://127.0.0.1:8789/v1'
export OPENAI_API_KEY='kgw_demo_doc_ingestor'
python scripts/langchain_user_openai_compatible.py
```

## Optional: Cache real upstream calls while testing

For a realistic paid-provider smoke that is still retry-friendly, enable the
joblib-backed LLM call cache before starting the gateway:

```bash
export MODELKEYGUARD_DRY_RUN=0
export MODELKEYGUARD_LLM_CALL_CACHE='joblib'
export MODELKEYGUARD_LLM_CALL_CACHE_DIR="$PWD/out/llm_call_cache"
```

The first matching request still calls the real provider. Later identical
requests with the same provider, upstream URL, content type, safe-token route,
request body, and provider-secret hash replay the cached upstream response.
Provider secrets are hashed for the cache key and are not written into cache
filenames.

Use this only for developer smoke runs. Disable it for production:

```bash
unset MODELKEYGUARD_LLM_CALL_CACHE
```

Clear the cached upstream responses whenever you want a fresh paid-provider call:

```bash
rm -rf "${MODELKEYGUARD_LLM_CALL_CACHE_DIR:-$PWD/out/llm_call_cache}"
```

The repo reset script also removes this directory:

```bash
./scripts/reset_local_e2e_state.sh
```

## Production Shape

Use the same backend flags, but replace local literals with secret files:

```bash
export MODELKEYGUARD_STORE='kogwistar_postgres'
export MODELKEYGUARD_POSTGRES_DSN='postgresql://modelguard:modelguard@postgres:5432/modelguard'
export MODELKEYGUARD_GRAPH_KEY_FILE='/run/secrets/modelkeyguard_graph_key'
export MODELKEYGUARD_ADMIN_API_SECRET_FILE='/run/secrets/modelkeyguard_admin_api_secret'
export MODELKEYGUARD_KOGWISTAR_ENFORCE_INSTALLED_ONLY=1
export MODELKEYGUARD_USE_INSTALLED_KOGWISTAR=1
```

Keep `MODELKEYGUARD_INIT_RESET_EXISTING=1` for local/dev retry loops only. Do not enable reset-by-default against production data.
