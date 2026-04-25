# Kogwistar ModelKeyGuard — graph-native Keycloak gateway

A small standalone application that turns provider model keys into graph-governed capabilities.

The client never receives the real OpenAI/Azure/Anthropic key. It receives a short-lived Keycloak token or local `kgw_*` token, calls this gateway with an OpenAI-compatible API, and the gateway verifies identity, checks Kogwistar-style ACL, checks named projections, decrypts/looks up the provider key, forwards the request, and appends audit/usage graph events.

## Read in 30 seconds

```text
Client / LangChain
  api_key = kgw token or Keycloak token
  base_url = http://localhost:8789/v1
        ↓
ModelKeyGuard Gateway
  policy graph: who may access which key
  access conversation graph: every allow/deny/auth result
  usage ledger graph: successful debits only, strict linked list
  named projection: fast per-principal/per-user/per-key counters
        ↓
Provider API key resolved inside gateway only
```

## Run in 1 minute

```bash
unzip kogwistar_modelkeyguard_keycloak_graph_native_v2.zip
cd modelkeyguard_keycloak
export MODELKEYGUARD_GRAPH_KEY='dev-local-graph-encryption-key'
./scripts/init_graph.sh
./scripts/start_gateway.sh
```

In another terminal:

```bash
export KGW_TOKEN=kgw_demo_doc_ingestor
./scripts/test_chat.sh
./scripts/inspect_graph.sh
```

Dry-run mode is on by default, so no real OpenAI key is needed. To forward for real:

```bash
export MODELKEYGUARD_DRY_RUN=0
export OPENAI_API_KEY='sk-...'
./scripts/start_gateway.sh
```

## What is graph-native here?

The app separates four graph concerns:

```text
Policy graph
  slow-changing ACL, namespace, principal, token, user, key and quota config.
  Config changes append policy-version history.

Access conversation graph
  every request outcome, including invalid token, permission denied, quota denied,
  approval required, allow, debit and provider result.

Usage ledger graph
  successful quota-consuming calls only. Per-user lanes are strict sequential linked
  lists: usage_head:user:alice -> usage:00000001 -> usage:00000002.

Quota projection
  rebuildable serving cache keyed by lane/subject/period/bucket.
  Example: principal agent:doc-ingestor, period hour, bucket 2026-04-25T05:00Z.
```

Payloads in the graph file are sealed. The raw JSONL should not contain the plaintext stored payload. The app opens payloads with `MODELKEYGUARD_GRAPH_KEY`.

## Usage flow

```mermaid
sequenceDiagram
    participant C as Client / LangChain / Agent
    participant KC as Keycloak or local token
    participant G as ModelKeyGuard Gateway
    participant PG as Policy Graph
    participant AG as Access Conversation Graph
    participant QP as Named Projection
    participant UL as Usage Ledger Graph
    participant P as Provider

    C->>KC: Get token
    KC-->>C: Keycloak access token or kgw_demo token
    C->>G: POST /v1/chat/completions with Bearer token
    G->>PG: token -> principal -> user -> namespace -> key ACL
    G->>QP: read principal/user/key quota buckets
    alt permission denied
        G->>AG: ACL_DECISION_DENY
        G-->>C: 403 permission_denied
    else principal quota exceeded
        G->>AG: ACL_DECISION_DENY
        G-->>C: 429 principal_capacity_exceeded
    else user quota exceeded
        G->>AG: ACL_DECISION_DENY
        G-->>C: 429 user_quota_exceeded
    else allowed
        G->>AG: ACL_DECISION_ALLOW
        G->>P: forward with real provider key or dry-run
        P-->>G: response
        G->>AG: QUOTA_DEBITED + MODEL_USAGE_RESULT
        G->>UL: append strict usage node
        G->>QP: update principal/user/key projections
        G-->>C: OpenAI-compatible response
    end
```

## Quota model

Each request checks three quota lanes independently:

```text
principal lane: application/agent/service capacity
user lane: SaaS end-user entitlement
key lane: provider/API-key budget
```

Decision order:

```text
1. invalid token -> 401 invalid_or_inactive_token
2. ACL mismatch -> 403 permission_denied
3. principal quota exceeded -> 429 principal_capacity_exceeded
4. user quota exceeded -> 429 user_quota_exceeded
5. key quota exceeded -> 429 key_quota_exceeded
6. approval threshold -> 202 approval_required
7. otherwise -> 200 allow
```

For each configured period (`10s`, `hour`, `day`, `week`, `month`):

```text
used = named_projection[lane, subject, period, bucket]
allow iff:
  used.usd + estimated.usd <= max_usd
  used.tokens + estimated.tokens <= max_tokens
  used.requests + 1 <= max_requests
```

The graph is the authority and the projection is rebuildable. Production should make append + projection update transactional or process them through a durable CDC/outbox worker.

## Tutorial ladder

### 1. Basic wrapper: replace provider key with gateway token

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8789/v1",
    api_key="kgw_demo_doc_ingestor",  # not the OpenAI key
)

resp = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[
        {"role": "system", "content": "You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets."},
        {"role": "user", "content": "Summarize this note."},
    ],
)
```

### 2. SaaS on-behalf-of-user request

`kgw_demo_doc_ingestor` maps to:

```text
principal_id = agent:doc-ingestor
on_behalf_of_user_id = user:alice
namespace = tenant:kogwistar
```

The gateway checks both:

```text
agent:doc-ingestor principal quota
user:alice user quota
```

### 3. Principal busy vs user quota exhausted

```bash
KGW_TOKEN=kgw_demo_principal_busy ./scripts/test_chat.sh
# -> 429 principal_capacity_exceeded

KGW_TOKEN=kgw_demo_low_user ./scripts/test_chat.sh
# -> 429 user_quota_exceeded
```

### 4. Permission denied is still an access-conversation event

```bash
KGW_TOKEN=kgw_demo_external ./scripts/test_chat.sh
# -> 403 permission_denied
./scripts/inspect_graph.sh
```

Denied events are audit data but do not increment usage named projections.

### 5. Keycloak local mode

```bash
./scripts/start_keycloak.sh
export KGW_TOKEN="$(./scripts/get_agent_token.sh langchain-agent agent-secret)"
./scripts/test_chat.sh
```

Keycloak is identity/token lifecycle. Kogwistar ModelKeyGuard is model authorization, quota, usage ledger and audit.

### 6. Future audit worker design

The current `review_worker.py` is a starter. The intended production worker does:

```text
hourly:
  scan access conversation and usage ledger graph
  stratified sample by principal/user/key/model/denial reason
  compress usage chains into summaries
  send samples to a review LLM
  append REVIEW_RESULT / ESCALATION events back into the graph
```

## Tests

The bundle now includes a regression and behavior suite with **94 pytest test cases**. The tests pin down:

```text
sealed graph payload authentication and no-plaintext-at-rest guarantees
period bucket semantics for 10s/hour/day/week/month windows
MiniACL compatibility semantics: public/private/scope/shared/group/owner/latest-version
graph-native policy init from config
access conversation events for allow/deny/approval/auth results
strict sequential usage ledger lanes
named projection quota counters
principal/user/key quota split and 403/429 reason taxonomy
token verification and on-behalf-of-user mappings
gateway helper behavior: key selection, cost estimate, prompt hashes, env secret refs
Postgres generic named-projection storage and Testcontainers integration
```

Run the fast JSONL behavior suite:

```bash
pip install -e '.[dev]'
PYTHONPATH=. pytest -q tests/test_gateway_policy.py tests/test_regression_behavior_suite.py
```

Run the Postgres integration suite with Testcontainers:

```bash
pip install -e '.[testcontainers]'
PYTHONPATH=. pytest -m postgres tests/test_postgres_store.py
```

## Files to inspect first

```text
modelkeyguard/graph_state.py   graph-native state, sealed payloads, strict usage lanes
modelkeyguard/core.py          ACL + quota + decision engine
modelkeyguard/gateway.py       OpenAI-compatible gateway
config/gateway_policy.json     principals/users/tokens/quotas/keys
scripts/test_chat.sh           one-command request
```

---

## Production-shaped Postgres mode

The default JSONL store is useful for reading the graph quickly. For a production-shaped local run, use Postgres as the durable graph/event/projection store.

```bash
pip install -e ".[postgres]"
./scripts/start_stack.sh
export MODELKEYGUARD_STORE=postgres
export MODELKEYGUARD_POSTGRES_DSN=postgresql://modelguard:modelguard@localhost:5432/modelguard
export MODELKEYGUARD_GRAPH_KEY='replace-this-with-a-long-random-app-key'
./scripts/start_gateway.sh
```

What moves into Postgres:

```text
graph_nodes          current node projection
graph_edges          current edge projection
graph_events         append-only access/auth/usage events
graph_records        append-first replay log for nodes/edges/events/projections
named_projections    generic named projections for O(1) serving counters by lane/subject/period/bucket
named_projections    generic Kogwistar-style projections, including quota counters and usage-lane tail pointers
```

The model is still graph-native:

```text
Policy graph
  principal/user/token/key/quota nodes and grant edges

Access conversation graph
  MODEL_ACCESS_REQUESTED
  ACL_DECISION_ALLOW / ACL_DECISION_DENY / ACL_DECISION_APPROVAL_REQUIRED
  AUTH_TOKEN_DENIED

Usage ledger graph
  QUOTA_DEBITED
  MODEL_USAGE_RESULT
  usage_head:user:alice -> usage:...:00000001 -> usage:...:00000002

Quota projections
  principal/user/key counters for 10s/hour/day/week/month windows
```

Denied auth and permission events are stored in the access conversation graph. They do **not** update named projections. Successful allowed calls append usage ledger nodes and update principal/user/key named projections.

## Linux production-ish Compose

`docker-compose.yml` now includes:

```text
postgres  persistent volume: modelguard_pgdata -> /var/lib/postgresql/data
keycloak  imports keycloak/modelguard-realm.json and uses the same Postgres service
```

This is not a hardened production deployment, but it has the right local shape: persistent Postgres volume, Keycloak, and the gateway configured to use Postgres by DSN.

## Testcontainers PostgreSQL tests

Install test dependencies:

```bash
pip install -e ".[testcontainers]"
```

Run only the Postgres integration tests:

```bash
pytest -m postgres tests/test_postgres_store.py
```

The tests start a disposable `postgres:16` container and verify:

```text
1. allow path writes access events, usage lane nodes, and named projections
2. principal quota 429 and user quota 429 are distinct
3. stored graph payloads are sealed and do not contain raw plaintext secrets
```

## SaaS quota semantics

Every request checks quota in this order:

```text
principal lane   app/agent/service capacity
user lane        end-user entitlement, when on_behalf_of_user_id exists
key lane         provider/API-key safety budget
```

Failure responses are intentionally different:

```text
429 principal_capacity_exceeded   the application principal is too busy
429 user_quota_exceeded           that SaaS user has consumed their entitlement
429 key_quota_exceeded            the provider key budget is exhausted
403 permission_denied             ACL/namespace/scope failed
401 invalid_token                 token verification failed
```

This is why the serving path uses Kogwistar-style `named_projections` instead of scanning the whole graph on every request. The graph is the authoritative replay/audit log; the named projection is a rebuildable O(1) view, not a feature-specific SQL schema.


## Projection implementation note

The Postgres backend intentionally does **not** create feature-specific quota projection tables. Hot reads use the same generic named projection primitive shape used by Kogwistar meta stores:

```text
get_named_projection(namespace, key)
replace_named_projection(namespace, key, payload, ...)
list_named_projections(namespace)
clear_named_projection(namespace, key)
```

ModelKeyGuard uses projection namespaces such as:

```text
modelkeyguard.quota_usage
modelkeyguard.usage_lane_head
```

So quota counters and strict lane tail pointers are rebuildable named projections over the single authoritative graph/event stream.

## FastAPI gateway

The gateway is now FastAPI/Uvicorn-based. The previous stdlib `ThreadingHTTPServer` smoke server has been removed from the application path.

```bash
./scripts/start_gateway.sh
# or
modelkeyguard gateway --host 127.0.0.1 --port 8789 --policy config/gateway_policy.json
```

The OpenAI-compatible client shape stays the same:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8789/v1",
    api_key="kgw_demo_doc_ingestor",
)
```

Gateway responsibilities:

```text
verify token -> resolve graph principal -> ACL decision -> quota projection check
-> encrypted key resolution -> provider forwarding/dry-run -> append access + usage graph events
```
