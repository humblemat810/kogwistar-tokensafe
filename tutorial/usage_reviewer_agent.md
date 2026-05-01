# Usage reviewer agent: named projections, triggers, and gateway-backed LangChain/Ollama

This walkthrough comes after [`keycloak_admin_first_setup.md`](keycloak_admin_first_setup.md).
The core reusable module lives in [`modelkeyguard.reviewer_agent`](../modelkeyguard/reviewer_agent.py).
It assumes you already created:

- `user:alice`
- `agent:doc-ingestor`
- `agent:usage-reviewer`
- a provider key for the Ollama-shaped route
- the safe token issued for `agent:doc-ingestor`

The reviewer is split into two parts:

1. a query path that reads rebuildable named projections and history state
2. an execution path that uses LangChain/Ollama through the gateway with the safe token you created earlier

The reviewer is designed to run from a separate machine just like a normal
human or service account would:

- `MODELKEYGUARD_GATEWAY_PUBLIC_URL` points at the gateway
- `KEYCLOAK_URL` points at Keycloak
- `MODELKEYGUARD_OIDC_USAGE_CLIENT_ID` / `MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET`
  mint the reviewer’s OAuth service-account token
- `REVIEWER_SAFE_TOKEN` authorizes the reviewer’s Ollama-shaped model call
- `SAFE_TOKEN` from the first tutorial still belongs to the doc-ingestor demo

This walkthrough assumes you already minted `REVIEWER_SAFE_TOKEN` in
[`keycloak_admin_first_setup.md`](keycloak_admin_first_setup.md).

For the read-only review status query, the reviewer identity must have the
`model.usage.read` role, or the equivalent read claim in your OAuth provider.
In the bundled Keycloak flow, grant that role to the
`modelguard-usage-agent` service account. If you use a different OAuth system,
give the reviewer client/token the matching read permission there too. If you
want to advance the checkpoint projection, keep that as an admin-authenticated
write path.

In Keycloak GUI:

1. Open `modelguard-usage-agent`
2. Go to `Service account roles`
3. Add the `model.usage.read` realm role

If you manage OAuth roles by CLI or API, assign the same read role there. The
review-status query must see the role before it will return data.

## 1. Inspect the review checkpoint

The review checkpoint is a named projection. It is rebuildable and not an
authoritative table.

First mint the reviewer service-account token that can read review status:

```bash
export REVIEWER_TOKEN="$(./scripts/get_agent_token.sh modelguard-usage-agent usage-agent-secret)"
```

Query it from the gateway:

```bash
modelkeyguard review-status \
  --base-url "${MODELKEYGUARD_GATEWAY_PUBLIC_URL:-http://127.0.0.1:8789}" \
  --bearer-token "${REVIEWER_TOKEN}"
```

Or use REST directly:

```bash
curl -sS \
  -H "Authorization: Bearer ${REVIEWER_TOKEN}" \
  "${MODELKEYGUARD_GATEWAY_PUBLIC_URL:-http://127.0.0.1:8789}/admin/review/status.json" \
  | python -m json.tool
```

If your gateway is configured for the shared admin secret instead of a bearer
token, swap the header for:

```bash
-H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}"
```

The status payload shows the current checkpoint and the trigger counters since
that checkpoint:

- token used since last review
- dollar used since last review
- conversation count since last review
- dangerous keyword hits since last review

Those counters come from append-only graph/history data and from the review
checkpoint projection.

## 2. Run the reviewer through the gateway

The runner uses the gateway’s Ollama-shaped route and the reviewer token
minted in the first tutorial. The system prompt stays aligned with the
reviewer policy, while the user message carries the review summary.

```bash
KGW_BASE_URL="${MODELKEYGUARD_GATEWAY_PUBLIC_URL:-http://127.0.0.1:8789}" \
REVIEWER_SAFE_TOKEN="${REVIEWER_SAFE_TOKEN}" \
KGW_OLLAMA_MODEL="${KGW_OLLAMA_MODEL:-gemma4:e2b}" \
python scripts/usage_reviewer_agent.py
```

The script prints:

- the current review status
- whether any trigger threshold fired
- the LangChain/Ollama reviewer note

If you want to force a review note even when the thresholds have not fired yet,
pass `--force`.

## 3. Acknowledge the checkpoint

The checkpoint projection advances when the review has been acknowledged. That
write path is separate from the query path.

If you have admin auth available, you can advance the checkpoint with the
gateway API:

```bash
curl -fsS -X POST \
  "${MODELKEYGUARD_GATEWAY_PUBLIC_URL:-http://127.0.0.1:8789}/admin/review/checkpoint" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "content-type: application/json" \
  -d '{"reviewed_by":"usage_reviewer_agent","review_summary":"checkpoint advanced","status":{}}' \
  | python -m json.tool
```

If you prefer the shared admin secret, use the same endpoint with:

```bash
-H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}"
```

The important bit is not the exact transport. It is that the review checkpoint
is stored as a rebuildable named projection, not as a hidden authoritative
record.

## 4. When the reviewer should run

The default policy now contains review triggers for:

- token usage since the last review
- dollar usage since the last review
- conversation count since the last review
- dangerous keyword hits

Those triggers are policy-driven. If you want to tune them, edit
`config/gateway_policy.json` and then re-run the status query.

## 5. Where to look next

The reviewer status and checkpoint data are queryable through both CLI and REST.
The operational review output is a separate path and should not be treated as
the source of truth. The append-only graph and rebuildable named projections are
the source of truth.
