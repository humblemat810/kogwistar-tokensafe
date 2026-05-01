# Glossary

This page is the plain-language map for the terms, tokens, secrets, and env
vars that show up in this repo.

It is written for humans first, but it should also help AI readers avoid the
same mixups that keep coming up in tutorials and support conversations.

## How to read this

- `graph key` means the encryption key for sealed graph state.
- `admin secret` means the shared secret that protects the admin API.
- `safe token` means the short-lived token issued for a principal or user path.
- `bearer token` means a Keycloak/OIDC access token sent in `Authorization: Bearer ...`.
- `gateway` means the ModelKeyGuard HTTP service on `8789` unless noted otherwise.

## If you come from OAuth / OIDC

This repo mixes OAuth/OIDC terms with ModelKeyGuard-specific terms. This table
is the translation layer.

| OAuth / OIDC term | What it means here | Common confusion |
| --- | --- | --- |
| `authorization server` | Keycloak. It mints and validates user and service-account tokens. | People sometimes expect ModelKeyGuard itself to be the auth server. It is not. |
| `client_id` | The Keycloak client identity. Examples include browser clients and service-account clients. | It is not the same as a ModelKeyGuard principal ID or provider key ID. |
| `client_secret` | The Keycloak client secret used by a confidential client or service account helper. | It is not the ModelKeyGuard admin secret and not the graph key. |
| `access token` | A bearer token from Keycloak that can be sent as `Authorization: Bearer ...`. | In this repo, that usually shows up as `ADMIN_TOKEN`. |
| `refresh token` | A Keycloak token that can mint new access tokens. | Most of this repo’s examples do not use refresh tokens at all. |
| `realm` | The Keycloak tenant boundary. | It is not the same as a ModelKeyGuard namespace, though they often line up in tutorials. |
| `scope` | A Keycloak permission claim or an application scope in ModelKeyGuard policy. | The word appears in both systems, but the checks happen in different places. |
| `role` | A Keycloak role claim such as `model.admin` or `model.usage.read`. | The gateway may require a specific role for browser admin access. |
| `service account` | A machine identity backed by a Keycloak client. | This is how the tutorial gets `ADMIN_TOKEN` and some machine agents. |
| `introspection` | The gateway can ask Keycloak whether a token is valid. | This is one auth path, not the only one. Some deployments accept local JWT verification or secret mode too. |
| `OIDC login` | The browser redirect flow at `/admin/oidc/login`. | This is the human-facing login path, not the CLI admin-secret path. |
| `bearer token` | What you paste into `Authorization: Bearer ...`. | In this repo it might be a Keycloak access token, `SAFE_TOKEN`, or another gateway-issued token depending on the route. |

Rule of thumb: if you are in the OAuth world, think of Keycloak as the
identity provider, the gateway as the policy and model gate, and `SAFE_TOKEN`
as the ModelKeyGuard-issued token that a client sends to the model endpoint.

## Core terms

| Term | Meaning | Common confusion |
| --- | --- | --- |
| `MODELKEYGUARD_GRAPH_KEY` | The application key used to seal and open graph state at rest. It protects persisted policy, quota, and secret-reference payloads. | It is not a user password, and it is not the admin API secret. |
| `MODELKEYGUARD_GRAPH_KEY_FILE` | File-based source for the graph key. The app reads the file contents. | People often set both the file and the inline env value and then forget which one the container actually mounts. |
| `MODELKEYGUARD_ADMIN_API_SECRET` | Shared secret for admin API access when the gateway is in secret mode or secret-or-Keycloak mode. | This is not the graph key and not the Keycloak access token. |
| `MODELKEYGUARD_ADMIN_API_SECRET_FILE` | File-based source for the admin secret. | This is the normal production/rehearsal shape when secrets are mounted. |
| `ADMIN_TOKEN` | A Keycloak access token minted for the admin service account/client. | It is not the same as `MODELKEYGUARD_ADMIN_API_SECRET`. |
| `SAFE_TOKEN` | The issued one-time ModelKeyGuard token returned by `/admin/policy/tokens`. | It is the token the client uses for the model request, not the admin login token. |
| `SAFE_TOKEN_ID` | The token ID returned alongside `SAFE_TOKEN`. | Use this for token-lane quota limits and history lookups. |
| `KGW_TOKEN` | A convenient shell variable used in demos for the client token. It usually points to `SAFE_TOKEN` or a demo `kgw_*` token. | It is a display-friendly alias, not a special protocol field. |
| `KGW_SAFE_TOKEN` | Another shell alias used in some tutorials and smoke scripts for a safe token or Keycloak token. | The name suggests “safe token”, but some tutorials use it for any bearer token used by the gateway. |
| `OPENAI_BASE_URL` | The OpenAI-compatible base URL for the client. In this repo it usually points to `http://127.0.0.1:8789/v1`. | It is often the gateway URL, not OpenAI’s public API. |
| `OPENAI_API_KEY` | The bearer token the OpenAI-compatible client sends. In this repo it is often `SAFE_TOKEN`. | It does not have to be an OpenAI key when talking to the gateway. |
| `OPENAI_MODEL` | The model name requested by the client. The gateway uses it to select the matching registered provider key. | It is just the requested model string, not the provider key ID. |
| `KGW_BASE_URL` | Gateway base URL used by LangChain smoke helpers. | Same idea as `OPENAI_BASE_URL`, but named for the gateway-side smoke scripts. |
| `KEYCLOAK_URL` | The Keycloak server URL used by the gateway and token-minting scripts. | This is the IdP/admin URL, not the gateway URL. |
| `KEYCLOAK_REALM` | The Keycloak realm name. | People often assume the tutorial uses the same realm as every other deployment; it does not. |
| `MODELKEYGUARD_GATEWAY_PUBLIC_URL` | The browser-visible gateway URL. | This is the URL users open in a browser, not the internal compose service name. |
| `MODELKEYGUARD_ADMIN_AUTH_MODE` | How `/admin/*` is authenticated: `secret`, `keycloak`, or `secret_or_keycloak`. | A `401` usually means the gateway is not accepting the auth method you used. |
| `MODELKEYGUARD_AUTH_MODE` | How model endpoints are authenticated: `local`, `keycloak`, or `local_or_keycloak` depending on the deployment shape. | This is separate from admin auth mode. |
| `MODELKEYGUARD_DRY_RUN` | When `1`, the gateway can return a synthetic response instead of calling a real provider upstream. | This affects the model call path, not admin registration. |
| `MODELKEYGUARD_STORE` | The backend store: `jsonl`, `postgres`, or `kogwistar_postgres`. | `jsonl` is the toy/demo mode; the postgres-backed modes are serious backends. |

## Gateway paths

| Path | What it does | Typical access |
| --- | --- | --- |
| `/v1/chat/completions` | OpenAI-compatible model request path. | Client code using `OPENAI_BASE_URL` and `OPENAI_API_KEY`. |
| `/v1/responses` | OpenAI-compatible responses path. | OpenAI-style clients that prefer the newer responses API. |
| `/api/chat` | Ollama-shaped provider route exposed by the gateway. This is the route `ChatOllama` hits when pointed at the gateway. | LangChain Ollama helpers or Ollama-shaped clients. |
| `/openai/deployments/{deployment}/chat/completions` | Azure OpenAI chat-completions route exposed by the gateway. | Azure OpenAI-style clients or LangChain Azure smoke helpers. |
| `/openai/responses` | Azure OpenAI responses route exposed by the gateway. | Azure OpenAI-style clients that use the responses API. |
| `/v1beta/models/{model}:generateContent` | Gemini generateContent route exposed by the gateway. | Gemini-style clients or LangChain Gemini helpers. |
| `/v1beta/models/{model}:streamGenerateContent` | Gemini streaming route exposed by the gateway. | Gemini-style clients that stream results. |
| `/admin/keys` | Browser page for creating, listing, rotating, and revoking provider keys. | Admin browser session or admin secret/header auth. |
| `/admin/keys.json` | JSON listing of registered provider keys. | CLI inspection with `curl`. |
| `/admin/policy/tokens` | Issues a one-time safe token. | Admin API or GUI form. |
| `/admin/history` | Browser page for access conversation history. | Admin browser session or admin secret/header auth. |
| `/admin/history.json` | JSON list of access conversation history records. | CLI inspection with `curl`. |
| `/admin/usage.json` | Aggregated usage analytics. | Admin browser session or admin secret/header auth. |

## Provider endpoint map

This section names the gateway routes and the matching upstream-style routes in
plain English, without forcing you to read a wide table.

### OpenAI

- Gateway routes: `/v1/chat/completions`, `/v1/responses`
- Upstream routes: `https://api.openai.com/v1/chat/completions`, `https://api.openai.com/v1/responses`
- Meaning: the gateway keeps the OpenAI-compatible shape.

### Azure OpenAI

- Gateway routes: `/openai/deployments/{deployment}/chat/completions`, `/openai/responses`
- Upstream routes: `https://<resource>.openai.azure.com/openai/deployments/{deployment}/chat/completions`, `https://<resource>.openai.azure.com/openai/responses`
- Meaning: the deployment name is part of the path, so Azure requests are a little more specific than plain OpenAI requests.

### Gemini

- Gateway routes: `/v1beta/models/{model}:generateContent`, `/v1beta/models/{model}:streamGenerateContent`
- Upstream routes: `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`, `https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent`
- Meaning: Gemini uses method-style suffixes rather than the OpenAI chat shape.

### Ollama

- Gateway route: `/api/chat`
- Upstream route: `http://<ollama-host>:11434/api/chat`
- Discovery routes on the raw Ollama host: `/api/tags`, `/api/generate`, `/api/show`
- Meaning: LangChain Ollama in this repo goes through the gateway’s Ollama-shaped `/api/chat` route, while `/api/tags` and friends belong to the raw upstream Ollama server.

Rule of thumb: if the docs say “gateway Ollama-shaped route,” they mean the
gateway’s `/api/chat`. If the docs say “raw Ollama host” or “upstream Ollama,”
they mean the external Ollama server and its native endpoints such as `/api/tags`.

## What each token is for

| Token | What it authorizes | Where it shows up |
| --- | --- | --- |
| `ADMIN_TOKEN` | Keycloak-authenticated admin API access. | `Authorization: Bearer ...` on `/admin/*` when the gateway accepts Keycloak admin auth. |
| `MODELKEYGUARD_ADMIN_API_SECRET` | Shared-secret admin API access. | `x-modelkeyguard-admin-secret: ...` and browser admin session bootstrap. |
| `SAFE_TOKEN` | A specific caller’s safe token for model requests. | `OPENAI_API_KEY`, `KGW_TOKEN`, or `KGW_SAFE_TOKEN` in demo clients. |
| `KGW_TOKEN` / `KGW_SAFE_TOKEN` | Shell aliases for a gateway bearer token. | Tutorials, quickstarts, and smoke scripts. |

## Troubleshooting by category

When something fails, the fastest path is usually to ask: which layer is
complaining?

### 1. Authentication and login

| Symptom | Usually means | First thing to check |
| --- | --- | --- |
| `401 admin_auth_required` | The gateway did not accept the admin auth method you used. | Check `MODELKEYGUARD_ADMIN_AUTH_MODE`, the header name, and whether the token is expired. |
| `403 admin_role_required` | The token was accepted, but the caller lacks the required admin role. | Check the Keycloak role mapping and the configured required role/claim. |
| Browser login loops back to login | OIDC login succeeded partially, but the browser client or redirect config is wrong. | Check `/admin/oidc/login`, the browser client ID, realm, and redirect URL. |
| `invalid_token` or `token verification failed` | The gateway could not validate the bearer token. | Check the Keycloak issuer, audience, realm, and whether the token was minted for the right client. |

### 2. Graph state and secrets

| Symptom | Usually means | First thing to check |
| --- | --- | --- |
| `sealed graph payload authentication failed` | The graph key does not match the sealed state on disk or in Postgres. | Check `MODELKEYGUARD_GRAPH_KEY` and `MODELKEYGUARD_GRAPH_KEY_FILE`, then check which data root is active. |
| `graph_key_required` | A serious backend started without a real graph key. | Set a real graph key file or env var; do not rely on the dev fallback. |
| `modelkeyguard registration ...` fails before any HTTP call | The CLI opened the local store directly and could not decrypt it. | Add `--admin-base-url` if you meant to talk to the gateway. |
| `modelkeyguard registration ...` works in one shell but not another | The shells are using different env or different mounted secret files. | Compare `MODELKEYGUARD_GRAPH_KEY_FILE`, `MODELKEYGUARD_STORE`, and the active `secrets/` tree. |

### 3. Provider keys and model routing

| Symptom | Usually means | First thing to check |
| --- | --- | --- |
| `model_key_already_exists` | You tried to create a provider key with a `key_id` that already exists. | List `/admin/keys.json` and confirm the key ID is new before posting again. |
| `provider_secret_required` | `/admin/keys` was called without a provider secret. | Check the `provider_secret` field or the matching secret file. |
| `model_key_ambiguous` | More than one provider key matches the requested model. | Explicitly select the intended key or remove the duplicate registration. |
| `provider_upstream_unreachable` | The gateway could not reach the configured provider URL. | Check the upstream URL, tunnel, firewall, bind address, and provider service. |
| `500 Internal Server Error` on a model call | The request reached the gateway but the upstream or policy path failed. | Read the gateway logs first; the HTTP code alone is not enough here. |

### 4. Prompt policy and request shaping

| Symptom | Usually means | First thing to check |
| --- | --- | --- |
| `system_prompt_signature_mismatch` | The request’s system prompt does not match the policy profile. | Compare the request prompt to the `expected_system_prompt` in `config/gateway_policy.json`. |
| `403 permission_denied` | ACL, namespace, scope, or quota rejected the request. | Check the principal, user, model scope, and quota limits together. |
| `quota_exceeded` | The request would cross a configured quota. | Inspect the subject’s quota policy and current usage. |
| Request works in one helper script but not another | The helper scripts are sending different prompts or different token types. | Check `SAFE_TOKEN`, `KGW_TOKEN`, and the exact system prompt string. |

### 5. Storage, deployment, and lifecycle

| Symptom | Usually means | First thing to check |
| --- | --- | --- |
| Gateway container exits on startup | Startup failed while loading sealed state or config. | Read `docker logs` for the gateway container. |
| `postgres` looks empty after `up` | You expected a fresh reset, but normal `up` reused the active bind mount. | Use `fresh-up` if you want a new rehearsal root. |
| `fresh-up` still seems to reuse old data | The active data root did not change, or you are looking at the wrong checkout. | Check `MODELKEYGUARD_FRESH_ROOT` and the runtime mount path. |
| Keys look identical but one path still fails | The same key is being applied to a different state snapshot or a different store path. | Confirm the active data directory, the current store, and the running process. |
| `Connection refused` on `127.0.0.1:8789` | The gateway is not actually running. | Check `docker ps -a` and gateway startup logs. |

### 6. Quick Q&A

- “I have the right token but still get `401`.”
  Usually the gateway is not in the auth mode you think it is, or the token is stale.

- “I can log into Keycloak but cannot use the browser admin page.”
  Keycloak login alone is not enough. The user also needs the gateway’s required role/claim, and the gateway must accept the browser OIDC path.

- “I created a key, but the next create call says it already exists.”
  The first call probably succeeded. List `/admin/keys.json` before retrying.

- “I changed secrets but the gateway still behaves the old way.”
  The running container may still be using the old mounted files. Recheck the active mount and restart only the relevant service.

- “Why does the tutorial use `SAFE_TOKEN` here and `ADMIN_TOKEN` there?”
  `SAFE_TOKEN` is for model calls. `ADMIN_TOKEN` is for admin API calls when using Keycloak auth.

- “Why does the CLI work in one form and fail in another?”
  The CLI has a local direct-store mode and a gateway mode. Without `--admin-base-url`, it may bypass the gateway entirely.

- “I ran `deploy_remote_stack.sh up --ssh localhost --shape compose`, but the data still looks stale.”
  That path reuses the active remote checkout and its current bind mounts. `up` is not a wipe. Use `fresh-up` if you want a new rehearsal root, and check whether you are looking at the local checkout or `~/token-safe-deploy`.

- “I used `fresh-up`, then `down`, then `up`, and I expected a brand-new database again.”
  `fresh-up` selects the active rehearsal root, but `down`/`up` do not search for a newer folder. They keep using the active root until you intentionally change it again.

- “The local repo and the remote localhost deploy both show the same secrets, so why does one path still fail?”
  Secrets can match while the active data root or store path differs. Check whether the failing path is reading the local checkout, the remote checkout, or a stale direct-store database.

- “Why does `gateway-only` behave differently from `compose`?”
  `gateway-only` is a smaller split-target setup. It still uses the same gateway logic, but it only carries the gateway-facing slice of the deployment instead of the full compose stack.

- “Why does `localhost` in the SSH deploy command still feel remote?”
  Because `localhost` in that command means “the remote host you SSH into,” not your laptop shell. The deploy wrapper runs the stack in the remote checkout on that host.

- “Why does an Ollama example talk about a gateway URL?”
  Because the gateway can expose an Ollama-shaped route. That is different from a raw upstream Ollama host.

## Practical rules of thumb

1. If you are talking to the gateway’s model API, use `SAFE_TOKEN` or a valid Keycloak access token in `Authorization: Bearer ...`.
2. If you are talking to the admin API and the gateway is in secret mode, use `x-modelkeyguard-admin-secret`.
3. If you are registering or reading history from the browser, you are using the admin GUI, not the model client.
4. If you are editing sealed graph state, keep `MODELKEYGUARD_GRAPH_KEY` stable for the whole run.
5. If you see an Ollama example in this repo, read carefully whether it means:
   - the gateway’s Ollama-shaped `/api/chat` route, or
   - a direct upstream Ollama endpoint outside ModelKeyGuard.
