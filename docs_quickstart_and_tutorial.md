# ModelKeyGuard quickstart and tutorial ladder

This project is a Kogwistar-style model-key safety gateway. Clients use a safe token or a Keycloak token. The gateway infers the principal/user/application context, checks the graph-native policy and usage projections, decrypts or resolves the real provider key internally, forwards the request, and records access/usage/audit events.

For a slower side-by-side experience (CLI and GUI parity), see `tutorial/slow_quickstart_cli_gui_parity.md`.

## 0. The 60-second quickstart

Run one command:

```bash
./scripts/quickstart.sh
```

The script prints each step and generated artifact clearly. It shows:

1. where the encrypted graph and audit files will be written,
2. graph policy initialization,
3. FastAPI gateway startup,
4. an OpenAI-compatible client request using `kgw_demo_doc_ingestor`,
5. principal-limit, user-limit, and permission-denied cases,
6. graph inspection with named projections,
7. alert and LLM-style review output.

No real provider key is needed because the quickstart uses:

```bash
MODELKEYGUARD_DRY_RUN=1
```

To run the gateway again after quickstart (retry-safe, same encrypted graph):

```bash
export MODELKEYGUARD_GRAPH_PATH='out/quickstart_graph.jsonl'
export MODELKEYGUARD_AUDIT_PATH='out/quickstart_audit.jsonl'
export MODELKEYGUARD_GRAPH_KEY='dev-quickstart-modelkeyguard-graph-key-32b'
export MODELKEYGUARD_DRY_RUN=1
./scripts/start_gateway.sh
```

If you want a clean restart from scratch, rerun quickstart:

```bash
./scripts/quickstart.sh
```

Then in another terminal:

```bash
export KGW_TOKEN=kgw_demo_doc_ingestor
./scripts/test_chat.sh
./scripts/inspect_graph.sh
```

When inspecting quickstart artifacts:

- `out/quickstart_graph.jsonl` is intentionally readable at record level (`record_type`, `id`, `kind`).
- Sensitive payload content is not plaintext there; it is stored as sealed `payload_sealed`.
- For real production, switch to `MODELKEYGUARD_STORE=kogwistar_postgres` and `MODELKEYGUARD_POSTGRES_DSN=...` instead of relying on tutorial JSONL graph artifacts.

## 1. Mental model

```mermaid
flowchart LR
    Client["Client / LangChain / OpenAI SDK"] -->|Bearer kgw token| Gateway["ModelKeyGuard FastAPI Gateway"]

    subgraph Graph["Single Kogwistar-style authoritative graph"]
        Policy["Policy lane\nprincipals, users, namespaces, model keys, quotas"]
        Access["Access conversation lane\nrequest, allow, deny, approval, errors"]
        Usage["Usage ledger lane\nstrict sequential usage nodes"]
        Projection["Named projections\nquota counters, usage heads, hot reads"]
    end

    Gateway --> Policy
    Gateway --> Projection
    Gateway --> Access
    Gateway --> Usage
    Gateway -->|real provider key only inside gateway| Provider["OpenAI / Azure / Anthropic"]
```

There is **one graph**, not separate policy and usage engines. Policy, access, and usage are logical lanes/views inside the same authoritative graph. Hot reads use named projections; the graph remains replayable authority.

## 2. Basic wrapper: replace provider key with ModelKeyGuard safe key

The client keeps the normal OpenAI-compatible shape. Only `base_url` and `api_key` change.

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8789/v1",
    api_key="kgw_demo_doc_ingestor",  # safe key/token, not the provider key
)

resp = client.chat.completions.create(
    model="gpt-5.3-mini",
    messages=[
        {
            "role": "system",
            "content": "You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets.",
        },
        {"role": "user", "content": "Summarize this note."},
    ],
    max_tokens=64,
)
print(resp)
```

The bundled dependency-light script uses the same protocol through `urllib`:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8789/v1
export OPENAI_API_KEY=kgw_demo_doc_ingestor
python scripts/langchain_user_openai_compatible.py
```

The gateway resolves:

```text
token -> principal -> on_behalf_of_user -> namespace -> allowed model key
```

Then it checks quota and policy before it resolves the real provider secret.

## 3. Principal quota vs user quota

The default policy includes three useful tokens:

```text
kgw_demo_doc_ingestor       -> allowed; agent:doc-ingestor on behalf of user:alice
kgw_demo_principal_busy     -> 429 principal_capacity_exceeded
kgw_demo_low_user           -> 429 user_quota_exceeded
kgw_demo_external           -> 403 permission_denied
```

Run the cases:

```bash
KGW_TOKEN=kgw_demo_doc_ingestor ./scripts/test_chat.sh
KGW_TOKEN=kgw_demo_principal_busy ./scripts/test_chat.sh
KGW_TOKEN=kgw_demo_low_user ./scripts/test_chat.sh
KGW_TOKEN=kgw_demo_external ./scripts/test_chat.sh
```

Decision order:

```text
1. invalid token -> 401 invalid_or_inactive_token
2. ACL/namespace mismatch -> 403 permission_denied
3. principal quota exceeded -> 429 principal_capacity_exceeded
4. user quota exceeded -> 429 user_quota_exceeded
5. key quota exceeded -> 429 key_quota_exceeded
6. approval threshold -> 202 approval_required
7. otherwise -> 200 allowed
```

This distinction matters in SaaS:

- principal limit means the application/agent/service capacity is exhausted;
- user limit means the end user's entitlement is exhausted;
- key limit means the provider/model key budget is exhausted.

## 4. Inspect graph state and quota projections

After a request:

```bash
./scripts/inspect_graph.sh
```

You will see:

```text
node_kinds.access_conversation_event
node_kinds.usage_ledger_event
node_kinds.usage_lane_head
named_projections.quota_projection:principal:...
named_projections.quota_projection:user:...
named_projections.quota_projection:key:...
```

Denied/auth-failed requests are written to the access conversation lane. They do **not** debit quota. Successful allowed requests append usage events and update named projections.

## 5. Alert and LLM-style review walkthrough

The alert system is installable by rule:

```text
condition callback + action callback
```

Built-in rules cover:

- high denial rate,
- system prompt signature mismatch,
- quota exhausted,
- usage profile model violation.

Run once:

```bash
./scripts/review_once.sh
```

or after the quickstart:

```bash
python -m modelkeyguard review-once \
  --audit out/quickstart_audit.jsonl \
  --policy config/gateway_policy.json \
  --out out/quickstart_review_results.jsonl
```

The default reviewer is deterministic and local. Production can inject a callback that calls an LLM:

```python
from modelkeyguard.alert_rules import LLMUsageReviewer

reviewer = LLMUsageReviewer(graph_state, review_callback=my_llm_review_callback)
reviews = reviewer.review(events, policy)
```

The callback receives a redacted payload grouped by principal, user, and application. It should return structured findings such as risk, reasons, and recommended action. Review results are appended as graph-native `LLM_USAGE_REVIEWED` events.

## 6. Production deployment pointer

For production-like local deployment:

```bash
./scripts/bootstrap_secrets.sh
./scripts/start_stack.sh
```

Then start the gateway with installed-Kogwistar managed Postgres:

```bash
pip install -e ".[postgres]"
export MODELKEYGUARD_STORE=kogwistar_postgres
export MODELKEYGUARD_POSTGRES_DSN=postgresql://modelguard:modelguard@localhost:5432/modelguard
export MODELKEYGUARD_GRAPH_KEY_FILE=./secrets/modelkeyguard_graph_key
export MODELKEYGUARD_INIT_RESET_EXISTING=1
export MODELKEYGUARD_KOGWISTAR_ENFORCE_INSTALLED_ONLY=1
export MODELKEYGUARD_USE_INSTALLED_KOGWISTAR=1
./scripts/init_graph.sh
python scripts/kogwistar_postgres_no_jsonl_smoke.py
export MODELKEYGUARD_DRY_RUN=1
./scripts/start_gateway.sh
```

Use `_FILE` variables for secrets in Compose and Linux deployments. Do not put real provider keys directly into `.env` in production.
