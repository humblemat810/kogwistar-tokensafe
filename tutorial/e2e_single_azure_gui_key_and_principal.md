# Single E2E: Register Principal + Create Azure Key in GUI + Call Azure Endpoint

This is one deterministic tutorial for exactly this flow:

1. register an application principal for a user,
2. create an Azure OpenAI key via admin GUI,
3. call Azure native endpoint with that principal token,
4. verify usage/history,
5. repeat safely from scratch.

Run all commands from repository root:

```bash
cd "$(git rev-parse --show-toplevel)"
```

## Docker preflight (required for real mode)

Before section 3, confirm this shell can talk to Docker:

```bash
docker info >/dev/null && echo "docker ok"
```

If you get `permission denied while trying to connect to the docker API`, refresh group membership in this shell, then retry:

```bash
newgrp docker
docker info >/dev/null && echo "docker ok"
```

Use real values in exports. Do not keep angle-bracket placeholders like `<GRAPH_KEY_32+_CHARS>`.

## Mode lock (do this first, do not mix paths)

Choose exactly one mode for one full run:

- `demo` mode: fake provider key + `MODELKEYGUARD_DRY_RUN=1`
- `real` mode: real provider key + `MODELKEYGUARD_DRY_RUN=0`

Set one mode flag and keep it unchanged until the tutorial is complete:

```bash
export KGW_E2E_MODE='demo'   # or 'real'
echo "KGW_E2E_MODE=${KGW_E2E_MODE}"
```

Rules:

- Do not switch `KGW_E2E_MODE` mid-run.
- Do not reuse old gateway process from another mode.
- If you need to switch mode, restart from section 0.

## 0) Clean start (repeat-safe)

Stop previous local gateway process first.

```bash
./scripts/reset_local_e2e_state.sh
rm -f out/single_e2e_graph.jsonl out/single_e2e_audit.jsonl out/single_e2e_policy.json
```

If you want Postgres clean too:

```bash
docker compose down -v || docker-compose down -v
```

If your host has no compose command at all, use plain Docker cleanup:

```bash
docker rm -f modelkeyguard-postgres 2>/dev/null || true
rm -rf data/postgres
```

## 1) Python environment

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 2) Create a dedicated policy copy and register app principal + user token

This creates a local policy file only for this tutorial (`out/single_e2e_policy.json`), without changing your base config file.

```bash
python - <<'PY'
import json
from pathlib import Path

src = Path("config/gateway_policy.json")
dst = Path("out/single_e2e_policy.json")
policy = json.loads(src.read_text(encoding="utf-8"))

# Register an application principal quota lane
policy.setdefault("principal_quotas", {})["app:azure-demo"] = {
    "10s": {"period": "10s", "max_usd": 1.0, "max_tokens": 12000, "max_requests": 30},
    "hour": {"period": "hour", "max_usd": 20.0, "max_tokens": 300000, "max_requests": 3000},
}

# Register a local safe token mapped to that principal and user
policy.setdefault("local_tokens", {})["kgw_single_azure_demo"] = {
    "principal_id": "app:azure-demo",
    "kind": "app",
    "groups": ["app-dev"],
    "namespace": "tenant:kogwistar",
    "on_behalf_of_user_id": "user:alice",
    "scopes": ["model.invoke"],
    "jti": "single-azure-demo",
}

# Optional profile to declare intended model set for this principal
policy.setdefault("usage_profiles", {})["app:azure-demo"] = {
    "description": "Single-tutorial Azure app principal",
    "models": ["gpt-5-mini"],
}

dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(json.dumps(policy, indent=2), encoding="utf-8")
print(f"wrote {dst}")
PY
```

## 3) Start Postgres + configure gateway runtime

```bash
./scripts/start_postgres.sh
```

Set runtime vars:

```bash
export MODELKEYGUARD_STORE='postgres'
export MODELKEYGUARD_POSTGRES_DSN='postgresql://modelguard:modelguard@localhost:5432/modelguard'
export MODELKEYGUARD_POLICY_PATH='out/single_e2e_policy.json'
export MODELKEYGUARD_GRAPH_PATH='out/single_e2e_graph.jsonl'
export MODELKEYGUARD_AUDIT_PATH='out/single_e2e_audit.jsonl'
export MODELKEYGUARD_GRAPH_KEY='single-e2e-graph-key-32-bytes-minimum'

# Admin auth for /admin/*
export MODELKEYGUARD_ADMIN_API_SECRET='single-e2e-admin-secret'

# Lock dry-run based on selected mode
if [[ "${KGW_E2E_MODE}" == "demo" ]]; then
  export MODELKEYGUARD_DRY_RUN=1
else
  export MODELKEYGUARD_DRY_RUN=0
fi
echo "MODELKEYGUARD_DRY_RUN=${MODELKEYGUARD_DRY_RUN}"
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

## 4) Create Azure key in GUI (admin input)

Open:

- `http://127.0.0.1:8789/admin/keys`

Sign in when prompted:

- secret: `single-e2e-admin-secret`

In **Create sealed key**, submit:

- `key_id`: `key:azure:demo`
- `provider`: `azure_openai`
- `models`: `gpt-5-mini`
- `display_name`: `Azure demo key`
- `intended_use`: paragraph text (for example: "Only short enterprise Q&A requests for app:azure-demo.")
- `provider_secret`: use fake key for dry-run (`fake-real-azure-key`) or real Azure key for real mode

Mode-to-secret mapping (strict):

- if `KGW_E2E_MODE=demo`: use `fake-real-azure-key`
- if `KGW_E2E_MODE=real`: use your actual Azure key

CLI verify key exists:

```bash
curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  http://127.0.0.1:8789/admin/keys.json | python -m json.tool
```

## 4b) Optional: add a brand-new runtime principal and issue a new safe token

If you want to create a new principal after startup (instead of using `kgw_single_azure_demo` from step 2), you now have a dedicated admin UI:

- open `http://127.0.0.1:8789/admin/policy`
- use forms for principal registration, quota upsert/revoke, and one-time token issuance.

CLI/API equivalent:

```bash
curl -sS -X POST http://127.0.0.1:8789/admin/policy/principals \
  -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  -H 'content-type: application/json' \
  -d '{"principal_id":"agent:azure-manual-demo","kind":"agent","groups":"app-dev","namespace":"tenant:kogwistar","description":"manual runtime principal"}' \
  | python -m json.tool

curl -sS -X POST http://127.0.0.1:8789/admin/policy/quotas/upsert \
  -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  -H 'content-type: application/json' \
  -d '{"lane":"principal","subject_id":"agent:azure-manual-demo","quota_name":"hour","period":"hour","max_requests":1000,"max_tokens":200000,"max_usd":20.0}' \
  | python -m json.tool
```

Then issue a safe token:

```bash
export KGW_SAFE_TOKEN="$(curl -sS -X POST http://127.0.0.1:8789/admin/policy/tokens \
  -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  -H 'content-type: application/json' \
  -d '{"principal_id":"agent:azure-manual-demo","namespace":"tenant:kogwistar","scopes":["model.invoke"]}' \
  | python -c 'import sys,json; print(json.load(sys.stdin)["safe_token"])')"
echo "${KGW_SAFE_TOKEN}"
```

Use `KGW_SAFE_TOKEN` in step 5 and `KGW_TOKEN` in step 6.

## 5) Call Azure native endpoint with the registered principal token

Use the registered local safe token from step 2:

```bash
export KGW_BASE_URL='http://127.0.0.1:8789'
export KGW_SAFE_TOKEN='kgw_single_azure_demo'
export KGW_AZURE_DEPLOYMENT='gpt-5-mini'
export KGW_AZURE_API_VERSION='2024-10-21'
./scripts/smoke_azure_real_completion.sh
```

If you ran step 4b, replace `kgw_single_azure_demo` with `${KGW_SAFE_TOKEN}`.

Note:

- Native Azure clients may call either `/openai/deployments/{deployment}/chat/completions` or `/openai/responses` depending on client/version/model.
- This gateway supports both paths; keep `models=` registration aligned with the exact deployment/model string.

Expected results:

- demo mode (`MODELKEYGUARD_DRY_RUN=1`): HTTP 200 with synthetic gateway completion.
- real mode (`MODELKEYGUARD_DRY_RUN=0`): HTTP 200 with real upstream completion.

## 6) LangChain smoke using same principal token

```bash
bash scripts/setup_langchain_smoke_env.sh
source .venv-langchain-smoke/bin/activate

export KGW_BASE_URL='http://127.0.0.1:8789'
export KGW_TOKEN='kgw_single_azure_demo'
export KGW_AZURE_DEPLOYMENT='gpt-5-mini'
export KGW_AZURE_API_VERSION='2024-10-21'
export KGW_SYSTEM_PROMPT='You are a policy-compliant enterprise assistant.'

python scripts/external_langchain_smoke.py --provider azure_openai --mode native
python scripts/external_langchain_smoke.py --provider azure_openai --mode native --stream
```

If you ran step 4b, set:

```bash
export KGW_TOKEN="${KGW_SAFE_TOKEN}"
```

## 7) Verify usage + history for this principal

Usage monitor UI:

- `http://127.0.0.1:8789/admin/usage`

History UI:

- `http://127.0.0.1:8789/admin/history`

CLI check (filtered by principal):

```bash
curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  'http://127.0.0.1:8789/admin/history.json?subject_type=principal&subject_id=app:azure-demo&time_range=24h&page_size=50' \
  | python -m json.tool
```

## 7b) Optional runtime policy updates (append-only)

You can update user/principal quota policy while gateway is running (admin-auth required). These writes are append-only revisions and the latest active revision is served from rebuildable quota-policy projections:

```bash
curl -sS -X POST http://127.0.0.1:8789/admin/policy/quotas/upsert \
  -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  -H 'content-type: application/json' \
  -d '{"lane":"principal","subject_id":"app:azure-demo","quota_name":"hour","period":"hour","max_requests":3000}' \
  | python -m json.tool

curl -sS -X POST http://127.0.0.1:8789/admin/policy/quotas/revoke \
  -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  -H 'content-type: application/json' \
  -d '{"lane":"principal","subject_id":"app:azure-demo","quota_name":"hour","reason":"temporary rollback"}' \
  | python -m json.tool
```

## 8) Switch from fake to real Azure upstream (optional)

If you intentionally switch from demo to real:

1. stop gateway,
2. set `export KGW_E2E_MODE='real'` and `export MODELKEYGUARD_DRY_RUN=0`,
3. in GUI rotate `key:azure:demo` to real Azure provider secret,
4. restart gateway,
5. rerun step 5 and step 6.

## 9) Repeat process from scratch

Use this exact reset sequence:

```bash
# stop gateway first
rm -f out/single_e2e_graph.jsonl out/single_e2e_audit.jsonl out/single_e2e_policy.json

docker compose down -v || docker-compose down -v
./scripts/start_postgres.sh

# then rerun from step 1
```

If you get `sealed graph payload authentication failed`, your `MODELKEYGUARD_GRAPH_KEY` changed between runs.

If you get `principal_not_registered` while issuing a safe token:

1. You likely did not successfully create the principal in `/admin/policy/principals`, or
2. you are issuing token against a different running gateway instance than the one where the principal was created.
