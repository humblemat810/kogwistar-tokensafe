# Governance Quickstart (Deterministic, Preseeded JSONL)

This is the fastest governance path for both human operators and coding agents.
It uses a local preseeded JSONL backend with fake/demo data so you can prove
review logic end-to-end without touching real provider keys.

If you came from Keycloak-first setup, you can also continue here for a lighter
local rehearsal:
[`keycloak_admin_first_setup.md`](keycloak_admin_first_setup.md).

Next step after this tutorial:
[`governance_quickstart_llm_migration.md`](governance_quickstart_llm_migration.md).

## 1. Start with deterministic preseeded state

```bash
export MODELKEYGUARD_ENV='local'
export MODELKEYGUARD_STORE='jsonl'
export MODELKEYGUARD_GRAPH_PATH='out/governance_quickstart_graph.jsonl'
export MODELKEYGUARD_AUDIT_PATH='out/governance_quickstart_audit.jsonl'
export MODELKEYGUARD_GRAPH_KEY='dev-governance-quickstart-key-32bytes'
export MODELKEYGUARD_DRY_RUN=1

./scripts/register_usage_example.sh
```

Expected result:

- `out/registration_demo_graph.jsonl` exists
- `out/registration_demo_token.txt` exists
- `out/registration_demo_summary.json` exists

If you want a full one-command run (seed + gateway + one request + inspect):

```bash
./scripts/register_and_run_usage_demo.sh
```

## 2. Run gateway with this seeded graph

```bash
export MODELKEYGUARD_GRAPH_PATH='out/registration_demo_graph.jsonl'
export MODELKEYGUARD_GRAPH_KEY='dev-registration-demo-key-change-me'
export MODELKEYGUARD_DRY_RUN=1
./scripts/start_gateway.sh
```

In another terminal:

```bash
export KGW_TOKEN="$(cat out/registration_demo_token.txt)"
./scripts/test_chat.sh
```

Expected result: HTTP 200 synthetic gateway response (dry-run mode).

## 3. Credential split (choose one per operation)

| Operation | Human Operator | Coding Agent |
| --- | --- | --- |
| Review status read | `Authorization: Bearer <oauth token>` or `x-modelkeyguard-admin-secret` | same, usually non-interactive bearer |
| Reviewer model call | `REVIEWER_SAFE_TOKEN` | same |
| Checkpoint write | admin bearer or admin secret | same |

Important: review-status credential and reviewer model-call credential can be
different by design.

## 4. Human Operator Path (GUI + curl/CLI)

GUI pages:

- `http://127.0.0.1:8789/admin/usage`
- `http://127.0.0.1:8789/admin/history`
- `http://127.0.0.1:8789/admin/keys`

Status API with admin secret:

```bash
export MODELKEYGUARD_ADMIN_API_SECRET="$(< ./secrets/modelkeyguard_admin_api_secret)"
curl -sS \
  -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  "http://127.0.0.1:8789/admin/review/status.json" | python -m json.tool
```

Or via CLI:

```bash
modelkeyguard review-status \
  --base-url "http://127.0.0.1:8789" \
  --admin-secret "${MODELKEYGUARD_ADMIN_API_SECRET}"
```

## 5. Coding Agent Path (deterministic governance monitor)

If you are running purely local JSONL without Keycloak, use local reviewer
status computation directly from graph state:

```bash
python -m modelkeyguard review-status \
  --local \
  --policy config/gateway_policy.json
```

If your Keycloak service-account flow is available, you can also run
`usage_analysis_agent.py` with a bearer token minted for usage read:

```bash
export REVIEWER_TOKEN="$(./scripts/get_agent_token.sh modelguard-usage-agent usage-agent-secret)"
python scripts/usage_analysis_agent.py \
  --bearer-token "${REVIEWER_TOKEN}" \
  --principal agent:demo-saas-agent
```

Run deterministic reviewer loop (forced, bounded):

```bash
export REVIEWER_SAFE_TOKEN="$(cat out/registration_demo_token.txt)"
KGW_BASE_URL='http://127.0.0.1:8789' \
KGW_OLLAMA_MODEL='gemma4:e2b' \
python scripts/usage_reviewer_agent.py --force --loop --max-iterations 1
```

Expected output includes:

- `review_status:`
- `review_result:`
- `reviewer_loop_health:`

## 6. Mini API and CLI reference

Core CLI commands:

- `modelkeyguard review-status`
- `python scripts/usage_analysis_agent.py`
- `python scripts/usage_reviewer_agent.py`
- `python -m modelkeyguard registration demo`
- `python -m modelkeyguard registration seed`

Core endpoints:

- `GET /admin/review/status.json`
- `POST /admin/review/checkpoint`
- `GET /admin/usage.json`
- `GET /admin/history.json`

## 7. Projection checks

Check deterministic governance checkpoint/projection state from the same local
graph path:

```bash
python -m modelkeyguard review-status --local --policy config/gateway_policy.json
```

Look for fields related to:

- `checkpoint`
- `window`
- `triggers`
- `summary`

For loop/backoff/breaker semantics:
[`../docs_governance_runtime.md`](../docs_governance_runtime.md).

## 8. Next step

Migrate this deterministic setup to an LLM-driven reviewer with dedicated
reviewer token/quota semantics:

- [`governance_quickstart_llm_migration.md`](governance_quickstart_llm_migration.md)
