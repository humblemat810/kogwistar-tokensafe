# LangChain Provider-Native Smoke (Separate Environment)

This guide runs from a separate Python environment from the gateway repo runtime.

Run all commands below from the repository root:

```bash
cd "$(git rev-parse --show-toplevel)"
```

## 0) Start gateway with retry-safe quickstart state

If you previously ran `./scripts/quickstart.sh`, reuse the same graph path + key:

```bash
export MODELKEYGUARD_GRAPH_PATH='out/quickstart_graph.jsonl'
export MODELKEYGUARD_AUDIT_PATH='out/quickstart_audit.jsonl'
export MODELKEYGUARD_GRAPH_KEY='dev-quickstart-modelkeyguard-graph-key-32b'
export MODELKEYGUARD_DRY_RUN=1
./scripts/start_gateway.sh
```

If you want a clean reset, run:

```bash
./scripts/quickstart.sh
```

## 1) Separate client environment

```bash
bash scripts/setup_langchain_smoke_env.sh
source .venv-langchain-smoke/bin/activate
python -m pip install -r scripts/requirements-langchain-smoke.txt
python -c "import langchain_core, langchain_openai, langchain_ollama, langchain_google_genai; print('imports ok')"
```

The smoke script must run with this separate Python interpreter. If you see `ModuleNotFoundError` (for example `langchain_core`), your shell is using a different environment.

## 2) Required client env vars

```bash
export KGW_BASE_URL="http://127.0.0.1:8789"
export KGW_TOKEN="kgw_demo_doc_ingestor"
export KGW_SYSTEM_PROMPT="You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets."
export KGW_OPENAI_MODEL="gpt-4o-mini"
export KGW_AZURE_DEPLOYMENT="azure-mini"
export KGW_AZURE_API_VERSION="2024-10-21"
export KGW_OLLAMA_MODEL="llama3.1"
export KGW_GEMINI_MODEL="gemini-2.0-flash"
export KGW_GEMINI_ENDPOINT="$KGW_BASE_URL"
```

Provider model/deployment defaults used by the script:
- OpenAI native: `KGW_OPENAI_MODEL=gpt-4o-mini`
- Azure native: `KGW_AZURE_DEPLOYMENT=azure-mini`, `KGW_AZURE_API_VERSION=2024-10-21`
- Ollama native: `KGW_OLLAMA_MODEL=llama3.1`
- Gemini native: `KGW_GEMINI_MODEL=gemini-2.0-flash`, `KGW_GEMINI_ENDPOINT=$KGW_BASE_URL`

Important for Azure native mode:
- `KGW_AZURE_DEPLOYMENT` is treated as an Azure deployment identifier in the path.
- If you use a real Azure deployment name, use the same exact value in registration below.

## 3) Register provider-native demo keys (required once per fresh graph)

Native routes enforce provider matching, so these keys must exist before native smoke calls.

```bash
curl -sS -X POST "$KGW_BASE_URL/admin/keys" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "key_id=key:azure:demo" \
  --data-urlencode "provider=azure_openai" \
  --data-urlencode "models=$KGW_AZURE_DEPLOYMENT" \
  --data-urlencode "display_name=Azure demo key" \
  --data-urlencode "provider_secret=fake-real-azure-key"

curl -sS -X POST "$KGW_BASE_URL/admin/keys" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "key_id=key:ollama:demo" \
  --data-urlencode "provider=ollama" \
  --data-urlencode "models=$KGW_OLLAMA_MODEL" \
  --data-urlencode "display_name=Ollama demo key" \
  --data-urlencode "provider_secret=fake-real-ollama-key"

curl -sS -X POST "$KGW_BASE_URL/admin/keys" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "key_id=key:gemini:demo" \
  --data-urlencode "provider=gemini" \
  --data-urlencode "models=$KGW_GEMINI_MODEL" \
  --data-urlencode "display_name=Gemini demo key" \
  --data-urlencode "provider_secret=fake-real-gemini-key"
```

OpenAI note:
- Default quickstart policy already contains OpenAI model keys for `gpt-4o-mini`.
- Register an OpenAI key only if you switch `KGW_OPENAI_MODEL` to another model not in policy.

## 4) Native endpoint calls (stream + non-stream)

OpenAI native:

```bash
python scripts/external_langchain_smoke.py --provider openai --mode native
python scripts/external_langchain_smoke.py --provider openai --mode native --stream
```

Azure OpenAI native:

```bash
python scripts/external_langchain_smoke.py --provider azure_openai --mode native
python scripts/external_langchain_smoke.py --provider azure_openai --mode native --stream
```

Ollama native:

```bash
python scripts/external_langchain_smoke.py --provider ollama --mode native
python scripts/external_langchain_smoke.py --provider ollama --mode native --stream
```

Gemini native:

```bash
python scripts/external_langchain_smoke.py --provider gemini --mode native
python scripts/external_langchain_smoke.py --provider gemini --mode native --stream
```

If Gemini native fails with an endpoint-override error, your `langchain_google_genai` version does not support custom API endpoint routing. Use universal fallback mode below.

## 5) Universal OpenAI adapter fallback (stream + non-stream for each provider model)

OpenAI model via `/v1`:

```bash
python scripts/external_langchain_smoke.py --provider openai --mode universal
python scripts/external_langchain_smoke.py --provider openai --mode universal --stream
```

Azure deployment-model via `/v1`:

```bash
python scripts/external_langchain_smoke.py --provider azure_openai --mode universal
python scripts/external_langchain_smoke.py --provider azure_openai --mode universal --stream
```

Ollama model via `/v1`:

```bash
python scripts/external_langchain_smoke.py --provider ollama --mode universal
python scripts/external_langchain_smoke.py --provider ollama --mode universal --stream
```

Gemini model via `/v1`:

```bash
python scripts/external_langchain_smoke.py --provider gemini --mode universal
python scripts/external_langchain_smoke.py --provider gemini --mode universal --stream
```

## 6) Optional: prove safe-token replacement with mock upstream capture

In gateway terminal:

```bash
export MODELKEYGUARD_DRY_RUN=0
export MODELKEYGUARD_MOCK_UPSTREAM_CAPTURE_PATH=out/mock_upstream_capture.jsonl
./scripts/start_gateway.sh
```

Then run any smoke command above and inspect:

```bash
tail -n 20 out/mock_upstream_capture.jsonl
```

You should see provider-auth headers containing fake real provider secrets, not `kgw_*` safe tokens.

## 7) Troubleshooting

- Error: `No module named ...` when running smoke script:
  - activate the smoke venv and rerun commands from this doc.
- Error: `sealed graph payload authentication failed` on gateway start:
  - you started with a different `MODELKEYGUARD_GRAPH_KEY` than the key used to create that graph file; use the retry-safe block in section 0.
- Error: `403 model_not_registered` for Azure/Ollama/Gemini native:
  - run section 3 registration commands first.
- Error: `403 model_key_provider_mismatch`:
  - model/deployment exists under a different provider key; ensure registration `provider=` matches the native route provider and `models=` matches the exact request model/deployment string.
