# Kogwistar ModelKeyGuard — graph-native Keycloak gateway

A small standalone application that turns provider model keys into graph-governed capabilities.

The client never receives the real OpenAI/Azure/Anthropic key. It receives a short-lived Keycloak token or local `kgw_*` token, calls this gateway with an OpenAI-compatible API, and the gateway verifies identity, checks Kogwistar-style ACL, checks named projections, decrypts/looks up the provider key, forwards the request, and appends audit/usage graph events.

Repository-wide invariants are recorded in [`REPO_INVARIANTS.md`](REPO_INVARIANTS.md).
For a plain-English explanation of the runnable shell entrypoints, see
[`scripts/README.md`](scripts/README.md).

## 60-second quickstart
### step 0 (for restart only, skip if fresh run)
turn off existing running resources occupying required resources
```bash
./scripts/reset_local_e2e_state.sh
```
### step 1
```bash
./scripts/quickstart.sh
```


The quickstart prints each step as it runs:

```text
[1/8] Prepare isolated local quickstart state
[2/8] Initialize graph-native policy state
[3/8] Start FastAPI ModelKeyGuard gateway in the background
[4/8] Run OpenAI-compatible client call using a Kogwistar-safe key
[5/8] Show quota/error semantics
[6/8] Inspect graph state
[7/8] Run alert and LLM-style usage review
[8/8] Summary and next commands
```

It runs in dry-run mode by default, so no real OpenAI key is required.


## Run in 1 minute

```bash
export MODELKEYGUARD_GRAPH_KEY='dev-local-graph-encryption-key'
./scripts/init_graph.sh
./scripts/start_gateway.sh
```

In another terminal:
Let client use the same key and call cli to inspect graph.
```bash
export MODELKEYGUARD_GRAPH_KEY='dev-local-graph-encryption-key'
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

## Full tutorial ladder

For the fastest guide, start with the glanceable tutorial index:

[`tutorial/README.md`](tutorial/README.md)

See [`docs_quickstart_and_tutorial.md`](docs_quickstart_and_tutorial.md) for:

1. the one-command quickstart,
2. OpenAI/LangChain-compatible client usage,
3. principal-vs-user quota behavior,
4. graph state inspection,
5. alert and LLM-review walkthrough,
6. production deployment notes.

See [`tutorial/slow_quickstart_cli_gui_parity.md`](tutorial/slow_quickstart_cli_gui_parity.md) for a slower, retry-safe CLI and GUI parity walkthrough.
See [`tutorial/kogwistar_managed_postgres_setup.md`](tutorial/kogwistar_managed_postgres_setup.md) for the copy-paste installed-Kogwistar managed Postgres setup, including no-JSONL graph artifact verification.
See [`tutorial/final_dev_guard_azure_real_setup.md`](tutorial/final_dev_guard_azure_real_setup.md) for final-dev guard setup with real Azure token pathway, PostgreSQL-backed state, and real smoke tests (completion + LangChain structured output).

See [`docs_langchain_provider_native_smoke.md`](docs_langchain_provider_native_smoke.md) for separate-environment LangChain smoke tests covering provider-native endpoints (OpenAI, Azure OpenAI, Ollama, Gemini) and `/v1` universal fallback mode, each with streaming and non-streaming examples, including retry-safe gateway startup with the correct quickstart graph key/path.

See [`docs_schema_semantics.md`](docs_schema_semantics.md) for the current entity relationship and storage/projection schema (including Mermaid diagrams and Kogwistar-compatibility mapping).
See [`docs_postgres_schema.md`](docs_postgres_schema.md) for the strict Postgres table-level view (PK/index/logical FK mapping + ERD source).


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
# Core concepts
## What is graph-native here?

The app has one authoritative graph with three logical lanes. Hot serving state
is kept separately as rebuildable named projections.

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

Named projections
  rebuildable named-projection serving cache keyed by lane/subject/period/bucket.
  Quota counters are materialized from successful usage events; they are not
  graph nodes/edges.
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

The bundle now includes a regression and behavior suite with **300+ pytest test cases**. The current suite collects 319 tests. The tests pin down:

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
installed-Kogwistar Postgres backend semantics, no-JSONL graph artifacts, and backend facade behavior
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

The model is still graph-native: authority is in graph facts/events, while
serving counters and current lookup views are named projections.

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

Named projections
  principal/user/key quota counters for 10s/hour/day/week/month windows
  usage-lane tail pointers and latest quota-policy views
```

Denied auth and permission events are stored in the access conversation graph. They do **not** update named projections. Successful allowed calls append usage ledger nodes and update principal/user/key named projections.

### Delegated Kogwistar Postgres mode

For installed-Kogwistar delegated persistence (pgvector backend + Kogwistar meta-store projections), use this retry-safe developer setup.

Use this block for a clean local rerun:

```bash
pip install -e ".[postgres]"
./scripts/reset_local_e2e_state.sh
./scripts/start_postgres.sh
export MODELKEYGUARD_STORE=kogwistar_postgres
export MODELKEYGUARD_POSTGRES_DSN=postgresql://modelguard:modelguard@localhost:5432/modelguard
export MODELKEYGUARD_GRAPH_KEY='kogwistar-managed-postgres-dev-key-32-bytes-minimum'
export MODELKEYGUARD_INIT_RESET_EXISTING=1
export MODELKEYGUARD_KOGWISTAR_EMBED_DIM=2
export MODELKEYGUARD_KOGWISTAR_ENFORCE_INSTALLED_ONLY=1
export MODELKEYGUARD_USE_INSTALLED_KOGWISTAR=1
```

Optional destructive preflight smoke:

```bash
python scripts/kogwistar_postgres_no_jsonl_smoke.py
```

Then initialize with your configured graph key and start the gateway:

```bash
./scripts/init_graph.sh
./scripts/start_gateway.sh
```

After the gateway prints `ModelKeyGuard FastAPI gateway listening on
http://127.0.0.1:8789`, stay in this same local setup and test it with the
same demo gateway token below. Do **not** switch to the Compose section unless
you are intentionally changing deployment shape.

In another terminal, try a dry-run OpenAI-compatible call through the
Kogwistar-backed gateway:

```bash
source .venv/bin/activate
export OPENAI_BASE_URL='http://127.0.0.1:8789/v1'
export OPENAI_API_KEY='kgw_demo_doc_ingestor'
export OPENAI_MODEL='gpt-4o-mini'
python scripts/langchain_user_openai_compatible.py
```

Optional admin checks:

```bash
curl -sS http://127.0.0.1:8789/healthz | python -m json.tool
curl -sS -H "x-modelkeyguard-admin-secret: dev-modelkeyguard-admin-secret" \
  'http://127.0.0.1:8789/admin/usage.json?time_range=24h&bucket=hour' \
  | python -m json.tool
```

Next-step links:

- Continue the same local Kogwistar Postgres walkthrough:
  [`tutorial/kogwistar_managed_postgres_setup.md#5-run-the-gateway`](tutorial/kogwistar_managed_postgres_setup.md#5-run-the-gateway)
- Turn on retry-friendly cached real upstream calls, using the same gateway
  token and backend environment:
  [`tutorial/kogwistar_managed_postgres_setup.md#optional-cache-real-upstream-calls-while-testing`](tutorial/kogwistar_managed_postgres_setup.md#optional-cache-real-upstream-calls-while-testing)
- Pick a different tutorial path from the index:
  [`tutorial/README.md`](tutorial/README.md)

Notes:

- In delegated mode, runtime imports must resolve from installed `kogwistar` package (not repo-local clones).
- Serious backend modes (`postgres`, `kogwistar_postgres`) do not rely on JSONL graph files for graph state.
- The smoke script is a local preflight that resets the configured Postgres DSN with its own smoke key, then verifies no `*.jsonl` graph artifact is created. Run `./scripts/init_graph.sh` after the smoke before starting the gateway.
- The client token in this section is `kgw_demo_doc_ingestor`. It is a gateway/demo token, not the provider API key. The real provider key, if enabled, stays server-side.
- For the full copy-paste walkthrough, including real-provider and joblib-cache notes, see [`tutorial/kogwistar_managed_postgres_setup.md`](tutorial/kogwistar_managed_postgres_setup.md).

The next section is an alternate deployment shape for Linux Compose. It is not
the next step after the local Kogwistar Postgres developer setup above.

## Linux production-ish Compose

`docker-compose.yml` now includes:

```text
postgres  pgvector-enabled Postgres image with persistent data directory
keycloak  imports keycloak/modelguard-realm.json and uses the same Postgres service
gateway   builds the token-safe app image locally and connects to those services
```

The checked-in compose file is an all-in-one local deployment shape. It is good
for one machine where Postgres, Keycloak, and the gateway share the compose
network:

```text
same Docker host
  gateway -> postgres:5432
  gateway -> keycloak:8080
  keycloak -> postgres:5432
```

It is **not** currently a CI runner recipe that builds and pushes the gateway
image to another registry or host, and it is **not** a complete split-machine
deployment file. For a split topology, use the same built image, but replace the
service-local hostnames with reachable remote endpoints:

```text
machine A: token-safe gateway container
machine B: pgvector Postgres, or A if colocated
machine C: Keycloak, or A/B if colocated

MODELKEYGUARD_POSTGRES_DSN=postgresql://modelguard:...@postgres-b.example:5432/modelguard
KEYCLOAK_URL=https://keycloak-c.example
```

In that shape, Postgres and Keycloak need their own backups, TLS/network policy,
secret injection, and health checks outside this repo's default compose file.
See [`docs_production.md#distributed-deployment-shape`](docs_production.md#distributed-deployment-shape)
for the supported topology and what still needs site-specific deployment glue.

Authentication support today:

- OpenAI-compatible model endpoints accept `Authorization: Bearer ...` and can
  validate real Keycloak access tokens by introspection when
  `MODELKEYGUARD_AUTH_MODE=keycloak` or `local_or_keycloak` is configured.
- Local `kgw_*` demo/safe tokens remain available for developer flows unless
  `MODELKEYGUARD_REQUIRE_KEYCLOAK=1` is set.
- Admin pages and admin JSON APIs use `x-modelkeyguard-admin-secret` or the
  admin session cookie by default. Set `MODELKEYGUARD_ADMIN_AUTH_MODE=secret_or_keycloak`
  to keep the browser admin-session path and Keycloak/OIDC bearer tokens with
  `model.admin` working together. Use `keycloak` only when you want to remove
  the browser secret-session path entirely.
- Browser OIDC login is available at `/admin/oidc/login` and lands on the
  standard Keycloak authorization-code flow before returning to the admin UI.
  The browser redirect uses `MODELKEYGUARD_KEYCLOAK_PUBLIC_URL` so the browser
  lands on a Keycloak URL it can actually reach, while the gateway still uses
  `KEYCLOAK_URL` for server-side token exchange.
  The browser login client defaults to `MODELKEYGUARD_OIDC_BROWSER_CLIENT_ID`
  (`modelguard-admin-web` in the bundled realm).
- Machine usage-analysis agents can use `MODELKEYGUARD_OIDC_USAGE_CLIENT_ID`
  (`modelguard-usage-agent` in the bundled realm) with the `model.usage.read`
  role to read `/admin/usage` and `/admin/usage.json` without policy mutation.
  The reusable Python client lives in `modelkeyguard.analytics`.
- For the OIDC-only deployment path, including authenticated `/v1/models`, see
  [`tutorial/keycloak_oidc_protect_everything.md`](tutorial/keycloak_oidc_protect_everything.md).
  For a small reusable usage-analysis agent example, see
  [`tutorial/usage_analysis_agent.md`](tutorial/usage_analysis_agent.md).
  The local executable smoke is:
  `./scripts/oidc_protect_everything_smoke.sh`.

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
modelkeyguard.quota_policy
```

So quota counters, strict lane tail pointers, and latest active quota-policy sets are rebuildable named projections over the single authoritative graph/event stream.

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

---

## Production-ready secure key management update

This bundle now includes a FastAPI gateway with a secure key-management surface:

- `_FILE` secret loading for Docker secrets and remote deployments
- sealed graph payloads for provider keys
- create / rotate / revoke key lifecycle events
- `/admin/keys` management page that never renders raw provider keys
- `/admin/keys.json` safe metadata API
- shared-secret admin auth gate + short-lived admin session cookie for all `/admin/*` routes
- encrypted request/response history capture with filtered admin retrieval APIs/UI
- OpenAI-compatible `/v1/chat/completions` endpoint
- one-minute LangChain/OpenAI-compatible client demo
- production-oriented Docker Compose with Keycloak + Postgres + gateway

See [`docs_production.md`](docs_production.md).

### One-minute E2E client demo

Terminal 1:

```bash
./scripts/bootstrap_secrets.sh
MODELKEYGUARD_DRY_RUN=1 ./scripts/start_gateway.sh
```

`bootstrap_secrets.sh` defaults to local/dev convenience. For a hardened
single-host production-style Compose run, use the production runner:

```bash
./scripts/production_compose.sh up
```

To push the same workflow to another user account on the same machine or to a
different host over SSH, use the remote deploy wrapper and then run the shared
deployment smoke:

```bash
./scripts/deploy_remote_stack.sh up --ssh user@host --shape compose
./scripts/deploy_remote_stack.sh smoke --ssh user@host --shape compose
```

That wrapper builds the gateway image locally, loads it on the remote Docker
host, and keeps secrets only in a runtime tmpfs staging area on the target
while the deployment is active.

For the remote compose path, it also generates a non-default Keycloak
bootstrap admin username/password pair unless you override them locally. It
prints that pair during deploy so you can reach the Keycloak admin console
without using `admin` / `admin`.

Warning: copy that pair when the deploy finishes. The wrapper does not keep a
recoverable copy for you. Losing that bootstrap console login does not erase
the persisted gateway data, but it can leave you without a Keycloak admin
console until you provision another admin path.

To add a new Keycloak user, log into the Keycloak admin console with that
bootstrap admin pair, create the user under `Users`, set a password, and assign
realm roles like `model.admin` or `model.usage.read`.

The smoke checks the browser OIDC redirect, CLI token auth, and the reusable
usage-analysis agent against the deployed surface:

```bash
./scripts/deployment_smoke.sh
```

Next step after the stack is up:

- [Register And Use](docs_production.md#register-and-use)
- [Secure key management pages](docs_production.md#secure-key-management-pages)

Provider keys are registered after deploy through `/admin/keys`; the production
bootstrap step does not need your OpenAI, Azure OpenAI, Gemini, or Ollama
secret.

Terminal 2:

```bash
./scripts/one_minute_e2e_demo.sh
```

This second script behaves like a normal OpenAI/LangChain client:

```bash
OPENAI_BASE_URL=http://127.0.0.1:8789/v1
OPENAI_API_KEY=kgw_demo_doc_ingestor
```

The gateway receives the Kogwistar token, checks graph ACL/quota, decrypts only inside the server when needed, and returns an OpenAI-compatible response.

### Manage keys securely

All admin routes now require either:

- header: `x-modelkeyguard-admin-secret: <secret>`, or
- admin session cookie created by `POST /admin/session`.

Example CLI export:

```bash
export MODELKEYGUARD_ADMIN_API_SECRET='<admin-shared-secret>'
```

Open:

```text
http://127.0.0.1:8789/admin/keys
```

Raw keys are accepted only through password fields. They are sealed into graph payloads and are never shown back in HTML, JSON, audit logs, or graph plaintext.

`/admin/keys` supports an optional per-key `upstream_url` override. This lets one gateway route different keys of the same provider (for example multiple Azure OpenAI resources) to different upstream base URLs without pinning all traffic to one global env var.

### Usage Ops and security monitoring

New admin routes are grouped by domain routers (`provider_*`, `admin_*`) and keep one shared governance flow internally.

- `POST /admin/session` login (set admin session cookie)
- `DELETE /admin/session` logout (clear admin session cookie)
- `GET /admin/usage` interactive usage monitor page
- `GET /admin/usage.json` filtered usage dataset API
- `GET /admin/history` interactive request/response history page
- `GET /admin/history.json` filtered/paginated history metadata API
- `GET /admin/history/{request_id}.json` exact request/response body detail
- `GET /admin/history/config` history runtime config
- `POST /admin/history/config` update retention/cap/runtime history config
- `GET /admin/policy` interactive policy operations page (forms + queryable quota revision view with capped lazy pagination; user/principal rows drill into revision history)
- `POST /admin/policy/users` append-only user registration
- `POST /admin/policy/applications` append-only application registration
- `POST /admin/policy/principals` append-only principal registration
- `POST /admin/policy/tokens` issue safe token (one-time reveal response, hash-only persistence)
- `POST /admin/policy/quotas/upsert` append-only quota policy revision (latest revision is active)
- `POST /admin/policy/quotas/revoke` append-only quota revoke revision (`revoked=true`, no hard delete)
- `GET /admin/policy/quotas.json` quota policy revision list with filters/paging (`lane`, `subject_id`, `quota_name`, `revoked`, `page`, `page_size`)
- `POST /admin/review/run` manual/scheduled review trigger (`sample_size`, `lookback_minutes`, `checkpoint_path`)
- `POST /admin/security-events` host security event intake (shared-secret protected)

History config env defaults (overrideable at runtime by admin config API/UI):

- `MODELKEYGUARD_HISTORY_ENABLED=1`
- `MODELKEYGUARD_HISTORY_RETENTION_DAYS=30`
- `MODELKEYGUARD_HISTORY_MAX_ACTIVE_RECORDS=10000`
- `MODELKEYGUARD_HISTORY_MAX_ACTIVE_BYTES=52428800`
- `MODELKEYGUARD_ADMIN_SESSION_TTL_SECONDS=3600`

Example security-event intake settings:

```bash
export SECURITY_EVENT_SHARED_SECRET=change-me
export ADMIN_WATCH_USERS=azureuser,opsadmin
export ADMIN_SECURITY_EVENT_LOG_PATH=out/admin_security_events.jsonl
```

Host-side watcher (run on the machine host, outside Docker, to capture SSH/sudo):

```bash
python scripts/host_admin_login_watcher.py \
  --gateway-url http://127.0.0.1:8789 \
  --secret "$SECURITY_EVENT_SHARED_SECRET" \
  --watch-users "$ADMIN_WATCH_USERS"
```

### Tests

The bundle now has more than 170 tests, including the production-readiness/key-management tests plus an extended alert and LLM-review regression suite.

```bash
pip install -e ".[dev]"
pytest
```

## Alert and LLM-review quick start

Run a one-off review over the gateway audit log:

```bash
./scripts/alert_review_once.sh
```

The review worker emits graph-native alert/review state:

```text
ALERT_RAISED
LLM_USAGE_REVIEWED
MODEL_USAGE_REVIEW_BATCH_COMPLETED
```

Built-in rules detect high denial rate, deny spikes, system-prompt signature mismatch, quota exhaustion, usage-profile model violations, token-exfiltration patterns, intent drift, and sudden model-distribution shifts. Rules are installable as `condition(ctx, events, policy)` + `action(alert, graph_state)` callbacks, so production can later plug in Slack, PagerDuty, email, ticketing, or Kogwistar approval callbacks.

For the full tutorial ladder, see `docs_production.md`.

## SaaS usage registration quick start

This bundle includes graph-native registration utilities for the common SaaS path:

```text
create user -> create application/principal -> attach quota -> issue safe token -> call OpenAI-compatible gateway -> inspect quota/audit graph
```

Run the registration example:

```bash
./scripts/register_usage_example.sh
```

Start the gateway against that registered graph:

```bash
MODELKEYGUARD_GRAPH_PATH=out/registration_demo_graph.jsonl \
MODELKEYGUARD_GRAPH_KEY=dev-registration-demo-key-change-me \
MODELKEYGUARD_DRY_RUN=1 \
./scripts/start_gateway.sh
```

In another terminal, call it like an OpenAI-compatible client:

```bash
KGW_TOKEN=$(cat out/registration_demo_token.txt) ./scripts/test_chat.sh
```

Or run the combined demo:

```bash
./scripts/register_and_run_usage_demo.sh
```

See `docs_usage_registration.md` for the tutorial and individual registration commands.
