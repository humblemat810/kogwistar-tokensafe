# Usage Analysis Agent

This tutorial shows the smallest useful Keycloak-native agent flow:

1. get a service-account token from Keycloak,
2. query the gateway usage endpoint,
3. ask for user, principal, or key analytics,
4. keep the same workflow working on one machine or across machines.

Use this when you want a human to follow the steps by hand and an AI agent to
reuse the same library in code.

If you just started the local production rehearsal with
`./scripts/production_compose.sh fresh-up`, this tutorial is the next stop:
the same service account and gateway URLs already exist in that stack.

## 1. What you need

- a running gateway
- a running Keycloak realm
- a service account client with `model.usage.read`
- the reusable Python library: `modelkeyguard.analytics`

In the bundled local realm, the usage-analysis client is:

- client id: `modelguard-usage-agent`
- client secret: `usage-agent-secret`

The debugger-friendly scaffold lives in:

- `modelkeyguard.usage_agent.UsageAnalysisAgent`

If you want lower-level control, the reusable building blocks are still:

- `modelkeyguard.analytics.KeycloakServiceAccount`
- `modelkeyguard.analytics.UsageAnalyticsClient`

## 2. Set the URLs

For a single-machine local run:

```bash
export MODELKEYGUARD_GATEWAY_PUBLIC_URL='http://127.0.0.1:8789'
export KEYCLOAK_URL='http://127.0.0.1:8080'
```

For a distributed deployment, point those at the reachable browser/gateway
URLs instead:

```bash
export MODELKEYGUARD_GATEWAY_PUBLIC_URL='https://gateway.example'
export KEYCLOAK_URL='https://keycloak.example'
```

The gateway talks to `KEYCLOAK_URL` for token exchange and the agent talks to
`MODELKEYGUARD_GATEWAY_PUBLIC_URL` for usage data.

## 3. Quick command-line agent

Run the example script:

```bash
python scripts/usage_analysis_agent.py \
  --client-id modelguard-usage-agent \
  --client-secret usage-agent-secret \
  --user user:alice \
  --principal agent:doc-ingestor \
  --key key:openai:prod
```

That prints three usage datasets, one for each subject lane.

If you already have a bearer token, skip Keycloak minting:

```bash
python scripts/usage_analysis_agent.py \
  --bearer-token "$ADMIN_TOKEN" \
  --principal agent:doc-ingestor
```

## 4. Minimal Python agent

For real code, import the scaffold and let it read its own environment:

```python
from modelkeyguard.usage_agent import UsageAnalysisAgent

agent = UsageAnalysisAgent.from_env()
report = agent.run()
print(report)
```

That object already knows how to:

- acquire a bearer token from `MODELKEYGUARD_BEARER_TOKEN` when present
- mint a service-account token from `KEYCLOAK_URL`, `KEYCLOAK_REALM`,
  `MODELKEYGUARD_OIDC_USAGE_CLIENT_ID`, and
  `MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET`
- read its default subjects from `MODELKEYGUARD_ANALYTICS_SUBJECT_USER`,
  `MODELKEYGUARD_ANALYTICS_SUBJECT_PRINCIPAL`, and
  `MODELKEYGUARD_ANALYTICS_SUBJECT_KEY`
- fall back to the bundled defaults if you have not set those yet

If you want to set everything explicitly:

```bash
export MODELKEYGUARD_GATEWAY_PUBLIC_URL='http://127.0.0.1:8789'
export KEYCLOAK_URL='http://127.0.0.1:8080'
export MODELKEYGUARD_OIDC_USAGE_CLIENT_ID='modelguard-usage-agent'
export MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET='usage-agent-secret'
export MODELKEYGUARD_ANALYTICS_SUBJECT_USER='user:alice'
export MODELKEYGUARD_ANALYTICS_SUBJECT_PRINCIPAL='agent:doc-ingestor'
export MODELKEYGUARD_ANALYTICS_SUBJECT_KEY='key:openai:prod'
python -c 'from modelkeyguard.usage_agent import UsageAnalysisAgent; print(UsageAnalysisAgent.from_env().render())'
```

## 5. What this gives you

- **user analytics**: what one person is doing across requests
- **principal analytics**: what one agent or service account is doing
- **key analytics**: what one provider key is doing

That is the clean split for an admin AI agent:

- use `model.usage.read` to inspect usage,
- use `model.admin` only when you actually need to change policy,
- keep the same code working in single-host and distributed deployments by
  changing only `KEYCLOAK_URL` and `MODELKEYGUARD_GATEWAY_PUBLIC_URL`.
