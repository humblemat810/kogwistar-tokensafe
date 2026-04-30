# Usage registration tutorial: user + principal + safe token

This tutorial works both from a repo checkout and from an installed wheel or
PyPI package. The default policy graph is bundled into the package, so the
registration CLI can still seed the standard demo path without relying on
`config/gateway_policy.json` being present in the current directory.

If you installed from PyPI and do not want to use any repo scripts, the same
seed path is available as:

```bash
modelkeyguard registration seed \
  --user-id user:customer-123 \
  --user-display-name "Customer 123" \
  --principal-id agent:customer-123-doc-agent \
  --principal-groups agent-dev \
  --namespace tenant:kogwistar \
  --application-id app:customer-123
```

This tutorial registers a SaaS-style model access path in the graph:

```text
application: app:demo-saas
principal:   agent:demo-saas-agent
end user:    user:demo-saas-alice
safe token:  kgw_sk_...  (returned once; graph stores only sha256 hash)
quota:       principal 10s/hour + user hour
```

The OpenAI-compatible client calls the gateway with the safe token as
`Authorization: Bearer ...`. The gateway infers principal, user, application,
namespace, and allowed model key from graph records before replacing the safe
token with the real provider key or dry-run backend.

## 1. Register usage graph records

```bash
./scripts/register_usage_example.sh
```

This creates:

```text
out/registration_demo_graph.jsonl
out/registration_demo_token.txt
out/registration_demo_summary.json
```

The token file contains the only plaintext copy of the safe token. The graph only
stores `safe_token_hash`.

## 2. Start the gateway against that graph

```bash
MODELKEYGUARD_GRAPH_PATH=out/registration_demo_graph.jsonl \
MODELKEYGUARD_GRAPH_KEY=dev-registration-demo-key-change-me \
MODELKEYGUARD_DRY_RUN=1 \
./scripts/start_gateway.sh
```

## 3. Run an OpenAI-compatible client call

```bash
KGW_TOKEN=$(cat out/registration_demo_token.txt) ./scripts/test_chat.sh
```

Or using OpenAI-style env vars:

```bash
OPENAI_BASE_URL=http://127.0.0.1:8789/v1 \
OPENAI_API_KEY=$(cat out/registration_demo_token.txt) \
OPENAI_MODEL=gpt-4o-mini \
python scripts/langchain_user_openai_compatible.py
```

## 4. Inspect graph quota and audit state

```bash
MODELKEYGUARD_GRAPH_PATH=out/registration_demo_graph.jsonl \
MODELKEYGUARD_GRAPH_KEY=dev-registration-demo-key-change-me \
modelkeyguard inspect-graph
```

You should see access conversation events, a strict usage lane for
`user:demo-saas-alice`, and quota projections for principal, user, and key lanes.

## Registration CLI commands

```bash
modelkeyguard registration register-user --user-id user:customer-123 --display-name "Customer 123"
modelkeyguard registration register-principal --principal-id agent:customer-123-doc-agent --kind agent --groups agent-dev --namespace tenant:kogwistar --application-id app:customer-123
modelkeyguard registration set-quota --lane principal --subject-id agent:customer-123-doc-agent --quota-name hour --period hour --max-usd 2.0 --max-tokens 50000 --max-requests 500
modelkeyguard registration set-quota --lane user --subject-id user:customer-123 --quota-name hour --period hour --max-usd 1.0 --max-tokens 20000 --max-requests 100
modelkeyguard registration issue-token --principal-id agent:customer-123-doc-agent --namespace tenant:kogwistar --on-behalf-of-user-id user:customer-123 --application-id app:customer-123
```

If you want the same path as a single beginner-friendly CLI call, use the new
seed command. It creates the user, principal, quotas, and on-behalf-of safe
token in one go while still making the subject semantics explicit:

```bash
modelkeyguard registration seed \
  --user-id user:customer-123 \
  --user-display-name "Customer 123" \
  --principal-id agent:customer-123-doc-agent \
  --principal-groups agent-dev \
  --namespace tenant:kogwistar \
  --application-id app:customer-123
```

The returned JSON includes the raw safe token. If you want to write it to a
file, add `--token-output-file out/customer-123.token`.
