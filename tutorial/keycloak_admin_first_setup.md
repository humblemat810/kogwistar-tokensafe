# Keycloak admin first setup: alice, doc-ingestor, quotas, and reviewer

This walkthrough starts from the fresh deployment state where the compose stack
gives you:

- a Keycloak bootstrap admin console login,
- an empty default Keycloak realm import for end-user accounts,
- a working ModelKeyGuard admin GUI path.

Use this when you want the first real setup to be explicit and repeatable:

```text
create Keycloak alice
register user:alice
register agent:doc-ingestor
set up browser admin login for alice
set up quotas for alice and agent:doc-ingestor
choose a provider key flavor with one variable
set up a reviewer account for usage governance
inspect quota/usage changes as admin
```

If you are starting from scratch on the local compose workflow:

```bash
./scripts/deploy_remote_stack.sh fresh-up --ssh localhost --shape compose
```

If you want the beginner realm seeded with demo user accounts, opt in
explicitly before the deploy:

```bash
MODELKEYGUARD_KEYCLOAK_REALM_IMPORT_FILE=./keycloak/modelguard-realm.beginner.json \
  ./scripts/deploy_remote_stack.sh fresh-up --ssh localhost --shape compose
```

## 1. Open Keycloak admin console

When the deploy finishes, it prints a one-time Keycloak bootstrap admin pair.
Use that pair to open the Keycloak admin console:

```text
http://127.0.0.1:8080/admin/
```

If you are forwarding the remote host through SSH, use the browser-reachable
URL that your tunnel exposes, but keep the same `/admin/` path.

This console login is for Keycloak administration only. It is not the same
thing as a ModelKeyGuard ACL subject.

## 2. Create the first human Keycloak user

Use the Keycloak bootstrap admin only to manage Keycloak. Then switch from the
`master` realm to the `modelguard` realm before creating application users.

In the `modelguard` realm:

1. Open `Users`
2. Click `Add user`
3. Create `alice`
4. Set a password under `Credentials`
5. Open `Role mapping`
6. Assign the OIDC admin role that ModelKeyGuard is configured to require.
   In the bundled setup that role is `model.admin`.

If your OIDC provider uses a different admin role/claim name, set
`MODELKEYGUARD_ADMIN_REQUIRED_ROLE` to match that value and assign that role in
the IdP instead. The default bundled setup uses `model.admin`, but the browser
OIDC gate itself is just checking a configured OIDC role/claim.

For the backend, Alice needs the matching ModelKeyGuard subject too. That means
the admin user is not just a Keycloak login; it should also exist in the graph
as `user:alice` so the gateway can attach policy, quota, and audit records to
her actions.

If you want Alice only as a normal end user, skip the `model.admin` role and
keep her as a plain realm user.

For Alice to be a backend admin, all of these must be true:

- Keycloak username `alice` exists in the trusted app realm
- Alice has the configured OIDC admin role or claim, usually `model.admin`
- ModelKeyGuard has the matching policy subject `user:alice`

That is the real admin path. The Keycloak user and the ModelKeyGuard subject
are separate records, and both must be present.

The browser client used for this login is `modelguard-admin-web`. In Keycloak it
is a public OpenID Connect client with standard flow enabled. It is for humans
using `/admin/oidc/login`; it is not a machine/service-account client.

## 2b. Map the Keycloak admin identity into ModelKeyGuard

The bootstrap admin you used to reach the Keycloak console is still only a
Keycloak identity. If you want that console operator to appear in ModelKeyGuard
policy, quota, and usage records, create a matching ModelKeyGuard user subject
too.

This step is for audit and policy visibility only. It does **not** make Alice a
backend admin, and it does **not** replace the Alice setup above.

The simplest pattern is to take the Keycloak username and prefix it with
`user:` when registering the ModelKeyGuard subject. If the deploy printed a
bootstrap username like `remote-admin-wlzps4253d`, the matching
ModelKeyGuard subject is `user:remote-admin-wlzps4253d`:

```bash
export KEYCLOAK_BOOTSTRAP_USERNAME='remote-admin-wlzps4253d'

modelkeyguard registration register-user \
  --user-id "user:${KEYCLOAK_BOOTSTRAP_USERNAME}" \
  --display-name "Keycloak Admin"
```

You may also create a stable alias such as `user:admin`, but that is only a
ModelKeyGuard alias. It is not an automatic mapping from the Keycloak username.

If you want to track the admin console as an operational principal as well,
you can also register a matching principal such as `agent:keycloak-admin` and
attach admin-only quotas to that subject. That is optional, but it gives you a
clean audit trail when the admin account performs operational work.

```bash
modelkeyguard registration register-principal \
  --principal-id agent:keycloak-admin \
  --kind agent \
  --groups ops-admin \
  --namespace tenant:kogwistar \
  --application-id app:ops-admin
```

## 3. Register Alice in ModelKeyGuard

Back in the shell, register the matching ACL subject. If the Keycloak username
is `alice`, the ModelKeyGuard subject is `user:alice`:

```bash
export ADMIN_TOKEN="$(./scripts/get_agent_token.sh modelguard-admin admin-agent-secret)"

modelkeyguard registration register-user \
  --user-id user:alice \
  --display-name "Alice"
```

`ADMIN_TOKEN` above is not Alice's browser token. It is a Keycloak
client-credentials token for the bundled `modelguard-admin` service-account
client. The local and production runners assign that service account the
`model.admin` role automatically. If you started Keycloak by hand, run this once
after Keycloak is ready:

```bash
./scripts/bootstrap_keycloak_admin_role.sh
```

If you want this walkthrough to write through the running gateway instead of
directly to the local store, add `--admin-base-url` and point it at the
ModelKeyGuard gateway. That base URL is the ModelKeyGuard gateway, not
Keycloak.

Easiest local case: omit `--admin-base-url` entirely and let the command write
directly to the local Kogwistar Postgres-backed store.

Example:

```bash
modelkeyguard registration register-user \
  --user-id user:alice \
  --display-name "Alice"
```

If you want to go through the running local gateway at
`http://127.0.0.1:8789`, use the admin secret by default:

```bash
export MODELKEYGUARD_ADMIN_API_SECRET_FILE='./secrets/modelkeyguard_admin_api_secret'

modelkeyguard \
  --admin-base-url http://127.0.0.1:8789 \
  --admin-secret "$MODELKEYGUARD_ADMIN_API_SECRET" \
  registration register-user \
  --user-id user:alice \
  --display-name "Alice"
```

That secret file is created by `./scripts/bootstrap_secrets.sh` and is the
source of `MODELKEYGUARD_ADMIN_API_SECRET` in local and deploy flows. If you
prefer a shell variable, you can also export it from that file with
`export MODELKEYGUARD_ADMIN_API_SECRET="$(< ./secrets/modelkeyguard_admin_api_secret)"`.

Use `--admin-bearer-token "$ADMIN_TOKEN"` only when the gateway is explicitly
configured with `MODELKEYGUARD_ADMIN_AUTH_MODE=keycloak` or
`secret_or_keycloak`, and the token comes from a Keycloak service account with
the required admin role.

For a true remote deployment, replace `http://127.0.0.1:8789` with the remote
gateway URL and use the same auth mode the remote gateway is configured for.

If Alice will be paired with a specific application principal, register that
too:

```bash
modelkeyguard registration register-principal \
  --principal-id agent:doc-ingestor \
  --kind agent \
  --groups agent-dev \
  --namespace tenant:kogwistar \
  --application-id app:doc-ingestor
```

## 3b. Give the agent and reviewer OIDC machine credentials

ModelKeyGuard accepts two different machine-auth shapes:

- a ModelKeyGuard safe token, issued by `/admin/policy/tokens`, which is the
  preferred path for model calls that need `principal`, `user`, and quota
  accounting;
- a Keycloak service-account access token, minted with the OAuth2
  `client_credentials` grant, for admin/API automation or for clients already
  mapped to a ModelKeyGuard principal.

Do not use the browser client `modelguard-admin-web` for machines.

For a machine-only Keycloak client, use these Keycloak UI settings:

1. Open `Clients`
2. Click `Create client`
3. Set `Client type` to `OpenID Connect`
4. Set `Client ID`, for example `modelguard-usage-agent`
5. In `Capability config`, set `Client authentication` to `On`
6. Keep `Authorization` `Off`
7. Turn `Standard flow` `Off` for a machine-only client
8. Turn `Direct access grants` `Off`
9. Turn `Implicit flow` `Off`
10. Turn `Service accounts roles` `On`
11. Leave browser redirect URI fields blank for a machine-only client
12. Save, then open `Service account roles`
13. Assign the realm role the machine needs

Use these role choices:

- `modelguard-usage-agent` for read-only usage review
- `modelguard-admin` for admin API automation
- `langchain-agent` for the bundled model-invoking service account that maps to
  `agent:doc-ingestor`

Assign `model.usage.read` to a usage reviewer, `model.admin` to an admin API
client, and `model.invoke` to a model-invoking service account.
The usage agent will also invoke llm to analyse the usage, so assign model.invoke.

Important: creating a Keycloak client does not automatically create a
ModelKeyGuard principal mapping. The bundled `langchain-agent` client already
maps to `agent:doc-ingestor` through the default policy. For a new direct-OIDC
model client, add a matching ModelKeyGuard client mapping before relying on the
Keycloak token for model calls. Otherwise use the safe token issued in step 7.

## 4. Register the agent reviewer account

The tutorial flow requires a real reviewer identity in both systems:

- Keycloak service account client: `modelguard-usage-agent`
- ModelKeyGuard principal: `agent:usage-reviewer`

There is no prebuilt `agent:usage-reviewer` subject in ModelKeyGuard, so
register it explicitly before you try the usage-analysis flow. The bundled
`modelguard-usage-agent` client is only the Keycloak-side credential; it does
not create the ModelKeyGuard principal for you.

```bash
modelkeyguard registration register-principal \
  --principal-id agent:usage-reviewer \
  --kind agent \
  --groups review-dev \
  --namespace tenant:kogwistar \
  --application-id app:usage-reviewer
```

Then, in Keycloak, create or reuse the `modelguard-usage-agent` service-account
client and assign it the `model.usage.read` realm role. That lets the reviewer
account inspect usage without being able to mutate policy.

The reusable Python client for that reviewer is
[`modelkeyguard.analytics`](../README.md#usage-analysis-agent).

## 5. Set quotas for Alice, the admin mapping, and the agents

User quota:

```bash
modelkeyguard registration set-quota \
  --lane user \
  --subject-id user:alice \
  --quota-name month \
  --period month \
  --max-usd 20 \
  --max-tokens 200000 \
  --max-requests 1000
```

Agent quota:

```bash
modelkeyguard \
  --admin-base-url http://127.0.0.1:8789 \
  --admin-secret "$MODELKEYGUARD_ADMIN_API_SECRET" \
  registration set-quota \
  --lane principal \
  --subject-id agent:doc-ingestor \
  --quota-name month \
  --period month \
  --max-usd 10 \
  --max-tokens 50000 \
  --max-requests 500
```

Bootstrap-operator quota, if you registered the bootstrap admin subject above:

```bash
modelkeyguard \
  --admin-base-url http://127.0.0.1:8789 \
  --admin-secret "$MODELKEYGUARD_ADMIN_API_SECRET" \
  registration set-quota \
  --lane user \
  --subject-id "user:${KEYCLOAK_BOOTSTRAP_USERNAME}" \
  --quota-name month \
  --period month \
  --max-usd 5 \
  --max-tokens 50000 \
  --max-requests 200
```

Give that reviewer principal its own quota too. For example, cap it to a
month-level budget:

```bash
modelkeyguard \
  --admin-base-url http://127.0.0.1:8789 \
  --admin-secret "$MODELKEYGUARD_ADMIN_API_SECRET" \
  registration set-quota \
  --lane principal \
  --subject-id agent:usage-reviewer \
  --quota-name month \
  --period month \
  --max-usd 2 \
  --max-tokens 20000 \
  --max-requests 200
```

## 5b. Log in as Alice and inspect the quota pages

If Alice has the configured OIDC admin role, she can use the browser OIDC
login to open the admin pages and see the quota settings you just created.

Open:

```text
http://127.0.0.1:8789/admin/oidc/login?next=/admin/usage
```

That path sends the browser to Keycloak, signs in as `alice`, and then returns
to ModelKeyGuard. Once logged in, Alice can view the quota and usage pages
under `/admin/usage` and `/admin/usage.json`.

If Alice is only a normal end user, keep her out of the admin browser path and
use a separate Keycloak account with the configured admin role for the admin
GUI.

## 6. Choose one provider flavor with a variable

Use one variable so the same tutorial can be repeated for OpenAI, Azure OpenAI,
Ollama, or Gemini.

This compose walkthrough starts the gateway in dry-run mode by default, so the
OpenAI-compatible call below will return a ModelKeyGuard synthetic response
unless you also restart the gateway with `MODELKEYGUARD_DRY_RUN=0` and use real
provider credentials. The same registration order still applies in real mode;
only the upstream forwarding changes.

```bash
export MODELKEYGUARD_SAMPLE_PROVIDER="${MODELKEYGUARD_SAMPLE_PROVIDER:-openai}"
export MODELKEYGUARD_SAMPLE_PROVIDER_SECRET="${MODELKEYGUARD_SAMPLE_PROVIDER_SECRET:-change-me}"

case "$MODELKEYGUARD_SAMPLE_PROVIDER" in
  openai)
    MODELKEYGUARD_SAMPLE_KEY_ID='key:openai:prod'
    MODELKEYGUARD_SAMPLE_MODELS='gpt-4o-mini,gpt-5.3-mini'
    MODELKEYGUARD_SAMPLE_UPSTREAM_URL='https://api.openai.com/v1/chat/completions'
    ;;
  azure_openai)
    MODELKEYGUARD_SAMPLE_KEY_ID='key:azure-openai:prod'
    MODELKEYGUARD_SAMPLE_MODELS='gpt-4o-mini-prod'
    MODELKEYGUARD_SAMPLE_UPSTREAM_URL='https://<resource>.openai.azure.com'
    ;;
  ollama)
    MODELKEYGUARD_SAMPLE_KEY_ID='key:ollama:gemma4-e2b'
    MODELKEYGUARD_SAMPLE_MODELS='gemma4:e2b'
    MODELKEYGUARD_SAMPLE_UPSTREAM_URL='http://127.0.0.1:11434/api/chat'
    ;;
  gemini)
    MODELKEYGUARD_SAMPLE_KEY_ID='key:gemini:prod'
    MODELKEYGUARD_SAMPLE_MODELS='gemini-2.0-flash'
    MODELKEYGUARD_SAMPLE_UPSTREAM_URL='https://generativelanguage.googleapis.com'
    ;;
  *)
    echo "unsupported MODELKEYGUARD_SAMPLE_PROVIDER=$MODELKEYGUARD_SAMPLE_PROVIDER" >&2
    exit 2
    ;;
esac
```

Register the provider key using those variables:

```bash
curl -fsS -X POST 'http://127.0.0.1:8789/admin/keys' \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -F key_id="${MODELKEYGUARD_SAMPLE_KEY_ID}" \
  -F provider="${MODELKEYGUARD_SAMPLE_PROVIDER}" \
  -F models="${MODELKEYGUARD_SAMPLE_MODELS}" \
  -F display_name='First tutorial provider key' \
  -F upstream_url="${MODELKEYGUARD_SAMPLE_UPSTREAM_URL}" \
  -F acl_mode='shared' \
  -F namespace='tenant:kogwistar' \
  -F shared_with_principals='agent:doc-ingestor' \
  -F provider_secret="${MODELKEYGUARD_SAMPLE_PROVIDER_SECRET}" \
  | python -m json.tool
```

That gives you one provider route for the same agent, regardless of the backend
flavor you pick.

If you want the gateway to forward to the real provider instead of returning
the synthetic dry-run payload, restart the compose stack with
`MODELKEYGUARD_DRY_RUN=0` before you run this step. The secure production
compose path already sets that value to `0`; the default tutorial compose stack
keeps it at `1` for a no-secret demo path.

For Ollama, `MODELKEYGUARD_SAMPLE_UPSTREAM_URL` must point at an address the
gateway container can actually reach. If Ollama is running on the host machine,
`http://127.0.0.1:11434/api/chat` usually points back into the gateway
container itself. Use a reachable host IP, a Docker service name, or a
configured `host.docker.internal` entry instead.

## 7. Issue a safe token for the registered model route and cap it

After step 6, ModelKeyGuard has a registered provider key/model route for
`agent:doc-ingestor`. Now issue a safe token for Alice using that agent, then
set a quota on the exact issued token.

```bash
TOKEN_RESPONSE="$(curl -fsS -X POST 'http://127.0.0.1:8789/admin/policy/tokens' \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H 'content-type: application/json' \
  -d '{"principal_id":"agent:doc-ingestor","namespace":"tenant:kogwistar","on_behalf_of_user_id":"user:alice","application_id":"app:doc-ingestor","scopes":["model.invoke"]}')"

SAFE_TOKEN="$(printf '%s' "$TOKEN_RESPONSE" | python -c 'import json,sys; print(json.load(sys.stdin)["safe_token"])')"
SAFE_TOKEN_ID="$(printf '%s' "$TOKEN_RESPONSE" | python -c 'import json,sys; print(json.load(sys.stdin)["token_id"])')"
```

To make that exact safe token stop after a fixed amount on this registered
model path, add a `token` lane quota using the returned token ID:

```bash
curl -fsS -X POST 'http://127.0.0.1:8789/admin/policy/quotas/upsert' \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H 'content-type: application/json' \
  -d "{\"lane\":\"token\",\"subject_id\":\"${SAFE_TOKEN_ID}\",\"quota_name\":\"lifetime\",\"period\":\"infinite\",\"max_usd\":5,\"max_tokens\":50000,\"max_requests\":100}" \
  | python -m json.tool
```

In dry-run mode, the request still exercises auth, ACL, quota, and key
selection, but the returned completion is synthetic. In real mode, with
`MODELKEYGUARD_DRY_RUN=0`, the same token is forwarded to the real upstream
provider.

## 8. Use the browser admin path

If `alice` was assigned `model.admin`, she can now log in through:

```text
http://127.0.0.1:8789/admin/oidc/login?next=/admin/usage
```

That browser login goes:

1. browser to Keycloak
2. Keycloak authenticates `alice`
3. Keycloak redirects back to ModelKeyGuard
4. ModelKeyGuard opens the admin session

If Alice is only a normal end user, do not use her for the admin GUI. Keep the
admin GUI on a separate Keycloak account with `model.admin`.

## 9. Run a request and inspect quota

Use the safe token to make a request. The same OpenAI-compatible client works
for all four provider flavors; only the model name changes to match the key you
registered in step 6.

```bash
OPENAI_BASE_URL='http://127.0.0.1:8789/v1' \
OPENAI_API_KEY="${SAFE_TOKEN}" \
OPENAI_MODEL='gpt-4o-mini' \
python scripts/langchain_user_openai_compatible.py
```

Examples for each provider flavor:

```bash
# OpenAI
OPENAI_BASE_URL='http://127.0.0.1:8789/v1' \
OPENAI_API_KEY="${SAFE_TOKEN}" \
OPENAI_MODEL='gpt-4o-mini' \
python scripts/langchain_user_openai_compatible.py

# Azure OpenAI
OPENAI_BASE_URL='http://127.0.0.1:8789/v1' \
OPENAI_API_KEY="${SAFE_TOKEN}" \
OPENAI_MODEL='gpt-4o-mini-prod' \
python scripts/langchain_user_openai_compatible.py

# Ollama
OPENAI_BASE_URL='http://127.0.0.1:8789/v1' \
OPENAI_API_KEY="${SAFE_TOKEN}" \
OPENAI_MODEL='gemma4:e2b' \
python scripts/langchain_user_openai_compatible.py

If the Ollama key was registered with `http://127.0.0.1:11434/api/chat`, and
Ollama is running on the host rather than inside the gateway container, this
call will fail with `500 Internal Server Error`. Re-register the Ollama key
with a reachable upstream URL before retrying.

# Gemini
OPENAI_BASE_URL='http://127.0.0.1:8789/v1' \
OPENAI_API_KEY="${SAFE_TOKEN}" \
OPENAI_MODEL='gemini-2.0-flash' \
python scripts/langchain_user_openai_compatible.py
```

Then inspect usage as the admin:

```bash
modelkeyguard inspect-graph
```

Or, if you are using the usage-analysis reviewer account, call:

```bash
python scripts/usage_analysis_agent.py \
  --client-id modelguard-usage-agent \
  --client-secret usage-agent-secret \
  --user user:alice \
  --principal agent:doc-ingestor \
  --key "${MODELKEYGUARD_SAMPLE_KEY_ID}"
```

That gives you the end-to-end path:

- Keycloak user creation
- ModelKeyGuard user/principal registration
- provider key selection
- safe-token issue/use
- quota inspection
- reviewer/read-only usage analysis
