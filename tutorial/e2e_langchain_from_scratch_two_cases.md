# End-to-End LangChain Adoption (Two From-Scratch Cases)

This tutorial gives two complete, reproducible tracks:

- Case 1: fake provider key + local safe token + JSONL graph + dry-run upstream.
- Case 2: real provider key + real safe token + PostgreSQL graph + real upstream.

All commands are run from repository root:

```bash
cd "$(git rev-parse --show-toplevel)"
```

## Docker preflight (required for Case 2)

Before Case 2 starts, confirm Docker is reachable from this shell:

```bash
docker info >/dev/null && echo "docker ok"
```

If you get Docker socket permission errors, refresh group membership in this shell and retry:

```bash
newgrp docker
docker info >/dev/null && echo "docker ok"
```

Do not paste real secrets into shell history. Use local secret files.

Important:

- Run Case 1 and Case 2 in separate sessions.
- Do not reuse a gateway process started for one case in the other case.
- If switching cases, stop gateway first, then run each case from its own section 1.

---

## Case 1: From Scratch (Fake Key, Local Token, JSONL, Dry-Run)

### 1) Clean start

Stop any running gateway in your current shell/session.

Run the repo reset helper first (recommended and repeat-safe):

```bash
./scripts/reset_local_e2e_state.sh
```

Then remove Case 1 tutorial artifacts:

```bash
rm -f out/case1_graph.jsonl out/case1_audit.jsonl out/case1_review.jsonl out/case1_review_checkpoint.json
```

### 2) Python environment for gateway

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 3) Configure JSONL + dry-run

```bash
export MODELKEYGUARD_GRAPH_PATH='out/case1_graph.jsonl'
export MODELKEYGUARD_AUDIT_PATH='out/case1_audit.jsonl'
export MODELKEYGUARD_GRAPH_KEY='case1-dev-graph-key-32-bytes-minimum'
export MODELKEYGUARD_DRY_RUN=1
export MODELKEYGUARD_ADMIN_API_SECRET='dev-modelkeyguard-admin-secret'
```

Initialize graph:

```bash
./scripts/init_graph.sh
```

Start gateway:

```bash
./scripts/start_gateway.sh
```

Health check:

```bash
curl -sS http://127.0.0.1:8789/healthz | python -m json.tool
```

### 4) Create a fake key in UI (admin path)

Open:

- `http://127.0.0.1:8789/admin/keys`
- sign in via `/admin/session` with `MODELKEYGUARD_ADMIN_API_SECRET` when prompted.

In **Create sealed key**, submit:

- `key_id`: `key:openai:fake:demo`
- `provider`: `openai`
- `models`: `gpt-4o-mini-demo`
- `display_name`: `Fake OpenAI Demo`
- `upstream_url`: optional (leave empty for default OpenAI upstream)
- `intended_use`: short paragraph (for example: internal summarization only, no coding-agent usage, no secret extraction)
- `provider_secret`: `fake-real-openai-key`

Verify in API:

```bash
curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" http://127.0.0.1:8789/admin/keys.json | python -m json.tool
curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" http://127.0.0.1:8789/admin/history.json | python -m json.tool
```

### 5) Separate LangChain client environment

```bash
bash scripts/setup_langchain_smoke_env.sh
source .venv-langchain-smoke/bin/activate
```

### 6) Run LangChain smoke request against your fake key

Set client env:

```bash
export KGW_BASE_URL='http://127.0.0.1:8789'
export KGW_TOKEN='kgw_demo_doc_ingestor'
export KGW_SYSTEM_PROMPT='You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets.'
export KGW_OPENAI_MODEL='gpt-4o-mini-demo'
```

Non-streaming:

```bash
python scripts/external_langchain_smoke.py --provider openai --mode native
```

Streaming:

```bash
python scripts/external_langchain_smoke.py --provider openai --mode native --stream
```

Expected result:

- request succeeds,
- non-stream output shows `response_text:` followed by model text,
- response content is dry-run/mock style,
- no real upstream credential is needed.

### 7) Validate state and usage

Audit tail:

```bash
tail -n 20 out/case1_audit.jsonl
```

Graph inspect:

```bash
MODELKEYGUARD_GRAPH_PATH='out/case1_graph.jsonl' \
MODELKEYGUARD_GRAPH_KEY='case1-dev-graph-key-32-bytes-minimum' \
./scripts/inspect_graph.sh
```

Usage UI:

- `http://127.0.0.1:8789/admin/usage`
- `http://127.0.0.1:8789/admin/history`

---

## Case 2: From Scratch (Real Key, Real Token, PostgreSQL, Real Upstream)

### 1) Prerequisites

You need:

- Docker available locally,
- a reachable real provider endpoint (for example Azure OpenAI),
- one real safe token for gateway auth (`KGW_SAFE_TOKEN`),
- one real provider API key (kept in local secret file).

### 2) Clean start for PostgreSQL state

If you want a strict from-scratch Postgres state, reset volumes:

```bash
./scripts/reset_local_e2e_state.sh
docker compose down -v || docker-compose down -v
```

Start Postgres:

```bash
./scripts/start_postgres.sh
docker compose ps postgres || docker-compose ps postgres
```

### 3) Python environment for gateway

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 4) Configure PostgreSQL + real upstream mode

```bash
export MODELKEYGUARD_STORE='postgres'
export MODELKEYGUARD_POSTGRES_DSN='postgresql://modelguard:modelguard@localhost:5432/modelguard'
export MODELKEYGUARD_AUDIT_PATH='out/case2_audit.jsonl'
export MODELKEYGUARD_GRAPH_KEY='case2-prodlike-graph-key-32-bytes-minimum'
export MODELKEYGUARD_DRY_RUN=0
export MODELKEYGUARD_ADMIN_API_SECRET='replace-this-admin-secret'
```

Initialize graph into Postgres:

```bash
./scripts/init_graph.sh
```

Start gateway:

```bash
./scripts/start_gateway.sh
```

Health check:

```bash
curl -sS http://127.0.0.1:8789/healthz | python -m json.tool
```

### 5) Store real provider secret safely

```bash
mkdir -p .secrets
chmod 700 .secrets
printf '%s' '<REAL_PROVIDER_API_KEY>' > .secrets/provider_api_key.txt
chmod 600 .secrets/provider_api_key.txt
```

### 6) Create real key in UI (admin path)

Open:

- `http://127.0.0.1:8789/admin/keys`
- sign in via `/admin/session` using `MODELKEYGUARD_ADMIN_API_SECRET`.

Create key with your real deployment mapping:

- `key_id`: example `key:azure:chat:prod`
- `provider`: `azure_openai`
- `models`: comma-separated deployment names (example `gpt-5-mini,gpt4o`)
- `display_name`: descriptive name
- `upstream_url`: your Azure resource base URL (example `https://<resource>.openai.azure.com`)
- `intended_use`: paragraph contract
- `provider_secret`: paste value from `.secrets/provider_api_key.txt`

Verify in API:

```bash
curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" http://127.0.0.1:8789/admin/keys.json | python -m json.tool
```

### 7) Short real request smoke (non-LangChain)

Set runtime vars:

```bash
export KGW_BASE_URL='http://127.0.0.1:8789'
export KGW_SAFE_TOKEN='<REAL_SAFE_TOKEN>'
export KGW_AZURE_DEPLOYMENT='gpt-5-mini'
export KGW_AZURE_API_VERSION='2024-10-21'
export KGW_SYSTEM_PROMPT='You are a policy-compliant enterprise assistant.'
export KGW_USER_PROMPT='Reply in one short sentence to confirm this is a real upstream path.'
```

Run:

```bash
./scripts/smoke_azure_real_completion.sh
```

Expected:

- HTTP 200,
- non-dry-run provider response.

### 8) LangChain short request smoke (real token + real key)

Prepare separate client environment:

```bash
bash scripts/setup_langchain_smoke_env.sh
source .venv-langchain-smoke/bin/activate
```

Run structured-output smoke:

```bash
python scripts/smoke_langchain_azure_structured_real.py \
  --base-url 'http://127.0.0.1:8789' \
  --safe-token '<REAL_SAFE_TOKEN>' \
  --deployment 'gpt-5-mini' \
  --api-version '2024-10-21'
```

Expected:

- printed JSON with `summary`, `risk_level`, `action`.

### 9) Validate PostgreSQL-backed state

Check Postgres graph tables:

```bash
docker compose exec -T postgres psql -U modelguard -d modelguard -c "select count(*) as graph_nodes from graph_nodes;"
docker compose exec -T postgres psql -U modelguard -d modelguard -c "select count(*) as graph_events from graph_events;"
docker compose exec -T postgres psql -U modelguard -d modelguard -c "select count(*) as named_projections from named_projections;"
```

Usage UI:

- `http://127.0.0.1:8789/admin/usage`
- `http://127.0.0.1:8789/admin/history`

Usage API:

```bash
curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" 'http://127.0.0.1:8789/admin/usage.json?time_range=24h&bucket=5m' | python -m json.tool
curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" 'http://127.0.0.1:8789/admin/history.json?time_range=24h&page_size=20' | python -m json.tool
```

---

## Common pitfalls

- If you see `sealed graph payload authentication failed`, your `MODELKEYGUARD_GRAPH_KEY` does not match the key used to create that graph state.
- If LangChain smoke says missing modules, activate `.venv-langchain-smoke`.
- If native Azure route returns `model_not_registered`, your key `models` list does not include the exact deployment name you requested.
- If admin routes are exposed beyond local dev, add network/proxy auth controls before shared-environment testing.
