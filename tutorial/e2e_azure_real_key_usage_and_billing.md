# E2E Azure Real Key Flow: Admin GUI Key Setup + Client Call + Usage/Billing Checks

This flow is for a real paid Azure OpenAI path:

1. bootstrap from a policy file,
2. add Azure key in admin GUI,
3. run a real client request through gateway,
4. verify usage and billing numbers.

Run commands from repo root:

```bash
cd "$(git rev-parse --show-toplevel)"
```

## 0) Mandatory clean preflight (default workflow)

Run this first. It closes old gateway processes, tears down local containers/volumes used by this repo, and prints port holders for `5432`/`8789`:

```bash
./scripts/reset_local_e2e_state.sh
```

If `5432` is still occupied after that, stop host postgres:

```bash
sudo systemctl stop postgresql
```

## 1) Create a dedicated runtime policy file

Create a minimal policy with one user, one principal, one safe token, and one model price entry:

```bash
mkdir -p out
cat > out/azure_real_billing_policy.json <<'JSON'
{
  "issuer": "keycloak:modelguard",
  "users": {
    "user:billing-demo": {
      "display_name": "Billing Demo User",
      "quotas": {
        "hour": {"period": "hour", "max_usd": 20.0, "max_tokens": 500000, "max_requests": 5000}
      }
    }
  },
  "principal_quotas": {
    "app:azure-billing-demo": {
      "10s": {"period": "10s", "max_usd": 2.0, "max_tokens": 50000, "max_requests": 100},
      "hour": {"period": "hour", "max_usd": 50.0, "max_tokens": 1000000, "max_requests": 10000}
    }
  },
  "local_tokens": {
    "kgw_azure_billing_demo": {
      "principal_id": "app:azure-billing-demo",
      "kind": "app",
      "groups": ["app-dev"],
      "namespace": "tenant:kogwistar",
      "on_behalf_of_user_id": "user:billing-demo",
      "scopes": ["model.invoke"],
      "jti": "kgw-azure-billing-demo"
    }
  },
  "model_keys": [],
  "model_price_per_1k_tokens_usd": {
    "gpt-5-mini": 0.010
  },
  "usage_profiles": {
    "app:azure-billing-demo": {
      "description": "Azure billing verification profile",
      "models": ["gpt-5-mini"]
    }
  }
}
JSON
```

## 2) Start gateway in real mode

Set runtime:

```bash
export MODELKEYGUARD_STORE='kogwistar_postgres'
export MODELKEYGUARD_POSTGRES_DSN='postgresql://modelguard:modelguard@localhost:5432/modelguard'
export MODELKEYGUARD_POLICY_PATH='out/azure_real_billing_policy.json'
export MODELKEYGUARD_GRAPH_PATH='out/azure_real_billing_graph.jsonl'
export MODELKEYGUARD_AUDIT_PATH='out/azure_real_billing_audit.jsonl'
export MODELKEYGUARD_GRAPH_KEY='<GRAPH_KEY_32+_CHARS>'
export MODELKEYGUARD_ADMIN_API_SECRET='<ADMIN_SECRET>'
export MODELKEYGUARD_KOGWISTAR_ENFORCE_INSTALLED_ONLY=1
export MODELKEYGUARD_USE_INSTALLED_KOGWISTAR=1
export MODELKEYGUARD_DRY_RUN=0
export AZURE_OPENAI_UPSTREAM_URL='<AZURE_OPENAI_BASE_URL>'
```

Use real values in exports. Do not leave placeholders like `<GRAPH_KEY_32+_CHARS>`.
Keep one consistent `MODELKEYGUARD_GRAPH_KEY` for this run; changing it later will make existing sealed state unreadable.

Start services:

```bash
./scripts/start_postgres.sh
./scripts/init_graph.sh
find "$PWD" -maxdepth 2 -name 'azure_real_billing_graph.jsonl' -print -quit | grep -q . && exit 1 || true
./scripts/start_gateway.sh
```

Health check:

```bash
curl -sS http://127.0.0.1:8789/healthz | python -m json.tool
```

## 3) Add Azure key using admin GUI

Open:

- `http://127.0.0.1:8789/admin/keys`

Sign in with `MODELKEYGUARD_ADMIN_API_SECRET`, then create:

- `key_id`: `key:azure:billing:prod`
- `provider`: `azure_openai`
- `models`: `gpt-5-mini` (or your real deployment name)
- `display_name`: `Azure Billing Production Key`
- `upstream_url`: your Azure resource base URL (for multi-resource routing, set per key)
- `intended_use`: billing + production usage paragraph
- `provider_secret`: your real Azure key

Verify from API:

```bash
curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  http://127.0.0.1:8789/admin/keys.json | python -m json.tool
```

## 4) Run real client request through gateway

```bash
export KGW_BASE_URL='http://127.0.0.1:8789'
export KGW_SAFE_TOKEN='kgw_azure_billing_demo'
export KGW_AZURE_DEPLOYMENT='gpt-5-mini'
export KGW_AZURE_API_VERSION='2025-04-01-preview'
export KGW_SYSTEM_PROMPT='You are a concise assistant.'
export KGW_USER_PROMPT='Return one short line for billing verification run 1.'
./scripts/smoke_azure_real_completion.sh
```

## 5) Check gateway usage totals (tokens + estimated cost)

```bash
curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  'http://127.0.0.1:8789/admin/usage.json?subject_type=principal&subject_id=app:azure-billing-demo&time_range=24h&bucket=5m' \
  | python -m json.tool
```

Look at:

- `overview.tokens`
- `overview.usd`

## 6) Check provider response token details (input/output/cached/total)

Get latest request id:

```bash
RID=$(curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  'http://127.0.0.1:8789/admin/history.json?subject_type=principal&subject_id=app:azure-billing-demo&time_range=24h&page_size=1' \
  | python -c 'import sys,json; d=json.load(sys.stdin); print((d.get("data") or [{}])[0].get("request_id",""))')
echo "${RID}"
```

Fetch detailed request/response body capture:

```bash
curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  "http://127.0.0.1:8789/admin/history/${RID}.json" > out/azure_real_history_detail.json
```

Extract provider usage tokens from response body:

```bash
python - <<'PY'
import json
from pathlib import Path

d = json.loads(Path("out/azure_real_history_detail.json").read_text())
resp = json.loads(d["response_body_text"])
usage = resp.get("usage", {})

input_tokens = usage.get("prompt_tokens")
output_tokens = usage.get("completion_tokens")
total_tokens = usage.get("total_tokens")

cached_tokens = None
details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
if isinstance(details, dict):
    cached_tokens = details.get("cached_tokens")

print(json.dumps({
    "input_tokens": input_tokens,
    "cached_tokens": cached_tokens,
    "output_tokens": output_tokens,
    "total_tokens": total_tokens
}, indent=2))
PY
```

Notes:

- Gateway usage monitor currently uses gateway-estimated token/cost fields from audit events.
- Provider-native token breakdown (including cached tokens if present) is available from captured response bodies in history detail.
