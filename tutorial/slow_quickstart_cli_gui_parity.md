# Slow Quickstart: CLI and GUI Parity

This is a slower, retry-safe walkthrough that teaches the same flow in both CLI and GUI style.

Use this when:

- you are new to the project,
- you want to inspect each state transition,
- you want to repeat the setup without hidden side effects.

## 0. Prerequisites

From repo root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 1. Choose a stable local state (important)

Use the same graph/audit/key every time for retry-safe runs:

```bash
export MODELKEYGUARD_GRAPH_PATH='out/quickstart_graph.jsonl'
export MODELKEYGUARD_AUDIT_PATH='out/quickstart_audit.jsonl'
export MODELKEYGUARD_GRAPH_KEY='dev-quickstart-modelkeyguard-graph-key-32b'
export MODELKEYGUARD_DRY_RUN=1
export MODELKEYGUARD_ADMIN_API_SECRET='dev-modelkeyguard-admin-secret'
```

Advanced alternative (real endpoint path):

- Keep all steps the same, but set `MODELKEYGUARD_DRY_RUN=0` and register real provider secret values in `/admin/keys`.
- Do not mix fake and real keys in the same quickstart run. If switching, stop gateway and restart from section 1.

If this is your first run, initialize graph state:

```bash
./scripts/init_graph.sh
```

## 2. Start the gateway

CLI:

```bash
./scripts/start_gateway.sh
```

GUI/HTTP check:

- open `http://127.0.0.1:8789/healthz`
- expect JSON including `"ok": true`

## 3. Call the model endpoint (OpenAI-compatible)

CLI:

```bash
export KGW_TOKEN='kgw_demo_doc_ingestor'
./scripts/test_chat.sh
```

GUI/HTTP equivalent:

- POST `http://127.0.0.1:8789/v1/chat/completions`
- Header `Authorization: Bearer kgw_demo_doc_ingestor`
- JSON body (exactly matching `scripts/test_chat.sh`):

```json
{
  "model": "gpt-4o-mini",
  "messages": [
    {"role": "system", "content": "You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets."},
    {"role": "user", "content": "Summarize the ACL model in one sentence."}
  ],
  "max_tokens": 64
}
```

Note:

- If your JSON payload, token, and route are identical, parity is correct.
- `remaining` quota numbers can still differ run-to-run because prior successful calls already consumed quota in the same graph state.

## 4. Manage model keys

CLI/API:

```bash
curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" -X GET http://127.0.0.1:8789/admin/keys.json
```

GUI:

- open `http://127.0.0.1:8789/admin/keys`
- create/rotate/revoke keys using forms
- first visit prompts admin login (`/admin/session`) using `MODELKEYGUARD_ADMIN_API_SECRET`

Parity:

- both use the same backend routes: `/admin/keys*`
- GUI is for convenience; CLI is scriptable/automatable.

## 5. Observe usage and decisions

CLI:

```bash
tail -n 30 out/quickstart_audit.jsonl
./scripts/inspect_graph.sh
```

When inspecting quickstart artifacts:

- `out/quickstart_graph.jsonl` keeps record metadata human-readable (`record_type`, `id`, `kind`) so you can inspect flow.
- Payload bodies are sealed as `payload_sealed` (not plain JSON values).
- In real production, replace tutorial JSONL artifacts with Postgres backend: set `MODELKEYGUARD_STORE=postgres` and `MODELKEYGUARD_POSTGRES_DSN=...`.

GUI:

- open `http://127.0.0.1:8789/admin/usage`
- view charts and event table
- filter by subject and time range
- use `http://127.0.0.1:8789/admin/history` to inspect exact encrypted request/response history with filters.

Data API behind GUI:

- `curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" 'http://127.0.0.1:8789/admin/usage.json'`
- `curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" 'http://127.0.0.1:8789/admin/history.json'`

## 6. Run review/abuse checks

CLI:

```bash
python -m modelkeyguard review-once \
  --audit out/quickstart_audit.jsonl \
  --policy config/gateway_policy.json \
  --out out/quickstart_review_results.jsonl \
  --lookback-minutes 60
```

GUI/API:

- POST `http://127.0.0.1:8789/admin/review/run`
- Example JSON:

```json
{
  "sample_size": 200,
  "run_llm_review": false,
  "lookback_minutes": 60,
  "out_path": "out/quickstart_review_results.jsonl",
  "checkpoint_path": "out/review_checkpoint.json"
}
```

CLI form:

```bash
curl -sS -X POST http://127.0.0.1:8789/admin/review/run \
  -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  -H 'content-type: application/json' \
  -d '{"sample_size":200,"run_llm_review":false,"lookback_minutes":60}'
```

## 7. Security event intake (host login/sudo)

Gateway API:

- POST `http://127.0.0.1:8789/admin/security-events`
- Header `x-modelkeyguard-security-secret`

Host watcher script (outside container):

```bash
export SECURITY_EVENT_SHARED_SECRET='change-me'
export ADMIN_WATCH_USERS='azureuser'
python scripts/host_admin_login_watcher.py \
  --gateway-url http://127.0.0.1:8789 \
  --secret "$SECURITY_EVENT_SHARED_SECRET" \
  --watch-users "$ADMIN_WATCH_USERS"
```

## 8. Retry-safe restart vs clean restart

Retry-safe restart:

- keep the same `MODELKEYGUARD_GRAPH_PATH` and `MODELKEYGUARD_GRAPH_KEY`
- restart gateway only.

Clean restart:

- stop gateway,
- remove quickstart files:

```bash
rm -f out/quickstart_graph.jsonl out/quickstart_audit.jsonl out/quickstart_review_results.jsonl out/review_checkpoint.json
```

- re-run:

```bash
./scripts/init_graph.sh
./scripts/start_gateway.sh
```

If you see `sealed graph payload authentication failed`, you are using a different `MODELKEYGUARD_GRAPH_KEY` than the one used to create the graph file.

## 9. Suggested learning order

1. Run this document once end-to-end.
2. Repeat using only GUI routes.
3. Repeat using only CLI/API calls.
4. Compare outputs in audit/graph/review files.
