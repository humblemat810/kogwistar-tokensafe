# Governance Quickstart (LLM Reviewer Migration)

This tutorial continues from:
[`governance_quickstart_deterministic_jsonl.md`](governance_quickstart_deterministic_jsonl.md).

Goal:

1. keep deterministic governance signals,
2. switch reviewer execution to LLM-driven notes,
3. keep reviewer credentials and quotas isolated from application traffic.

Default path stays dry-run/fake-upstream. A real-provider branch is optional at
the end.

## 1. Confirm deterministic baseline is already working

You should already have:

- `out/registration_demo_graph.jsonl`
- running gateway on `http://127.0.0.1:8789`
- a working `REVIEWER_SAFE_TOKEN` (demo token is fine for local rehearsal)

Quick baseline check:

```bash
KGW_BASE_URL='http://127.0.0.1:8789' \
KGW_OLLAMA_MODEL='gemma4:e2b' \
REVIEWER_SAFE_TOKEN="$(cat out/registration_demo_token.txt)" \
python scripts/usage_reviewer_agent.py --force --max-iterations 1 --loop
```

## 2. Reviewer-owned key and quota semantics

Keep reviewer execution independent from app traffic:

- reviewer principal quota (lane: `principal`)
- reviewer token quota (lane: `token`)
- separate model-call token (`REVIEWER_SAFE_TOKEN`)

Example principal quota:

```bash
modelkeyguard registration set-quota \
  --lane principal \
  --subject-id agent:usage-reviewer \
  --quota-name hour \
  --period hour \
  --max-usd 2 \
  --max-tokens 40000 \
  --max-requests 200
```

If you have a reviewer token id, add token-lane cap:

```bash
modelkeyguard registration set-quota \
  --lane token \
  --subject-id "${REVIEWER_SAFE_TOKEN_ID}" \
  --quota-name lifetime \
  --period infinite \
  --max-usd 5 \
  --max-tokens 50000 \
  --max-requests 100
```

## 3. Human Operator Path (GUI + API)

GUI touchpoints:

- `/admin/usage` for usage profile and spend
- `/admin/history` for request/decision context
- `/admin/review/status.json` for trigger/checkpoint state

Read reviewer status via API:

```bash
curl -sS \
  -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  "http://127.0.0.1:8789/admin/review/status.json" | python -m json.tool
```

Advance checkpoint after a reviewed run:

```bash
curl -fsS -X POST \
  "http://127.0.0.1:8789/admin/review/checkpoint" \
  -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  -H "content-type: application/json" \
  -d '{"reviewed_by":"operator","review_summary":"accepted","status":{}}' | python -m json.tool
```

## 4. Coding Agent Path (LLM reviewer loop)

OAuth read token (optional path):

```bash
export REVIEWER_TOKEN="$(./scripts/get_agent_token.sh modelguard-usage-agent usage-agent-secret)"
```

Run LLM reviewer with bounded loop:

```bash
export REVIEWER_SAFE_TOKEN="${REVIEWER_SAFE_TOKEN:-$(cat out/registration_demo_token.txt)}"
export KGW_BASE_URL='http://127.0.0.1:8789'
export KGW_OLLAMA_MODEL='gemma4:e2b'

python scripts/usage_reviewer_agent.py \
  --runtime-mode sync \
  --force \
  --loop \
  --max-iterations 3 \
  --scanner-backoff-initial-seconds 30 \
  --scanner-backoff-max-seconds 300
```

Enable circuit breaker for terminal auth failures:

```bash
python scripts/usage_reviewer_agent.py \
  --force \
  --loop \
  --max-iterations 10 \
  --scanner-breaker-enabled \
  --scanner-breaker-max-failures 3 \
  --scanner-error-family-policy-json '{"auth_denied":"terminal"}'
```

## 5. Auth flow choices (parallel)

| Flow | Use for | Example |
| --- | --- | --- |
| OAuth bearer | review-status read, admin APIs when configured | `Authorization: Bearer ${REVIEWER_TOKEN}` |
| Admin secret | admin API read/write in secret mode | `x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}` |
| Safe token | reviewer model call path | `REVIEWER_SAFE_TOKEN` |

## 6. Mini reference

CLI:

- `python scripts/usage_reviewer_agent.py`
- `python scripts/usage_analysis_agent.py`
- `modelkeyguard review-status`
- `modelkeyguard registration set-quota`

API:

- `GET /admin/review/status.json`
- `POST /admin/review/checkpoint`
- `GET /admin/usage.json`
- `GET /admin/history.json`

More runtime semantics:
[`../docs_governance_runtime.md`](../docs_governance_runtime.md)

Full reviewer walkthrough:
[`usage_reviewer_agent.md`](usage_reviewer_agent.md)

## 7. Optional real-provider branch

Only after local dry-run path is stable:

1. set `MODELKEYGUARD_DRY_RUN=0`
2. register real provider key through admin route
3. keep reviewer token/quota isolation unchanged

Use the same reviewer scripts and checkpoint flow; only upstream execution mode
changes.
