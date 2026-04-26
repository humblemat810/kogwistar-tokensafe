# LangChain Provider-Native Smoke (Separate Environment)

This guide runs from a separate Python environment from the gateway repo runtime.

## 1) Separate client environment

```bash
python -m venv ~/kgw-langchain-smoke
source ~/kgw-langchain-smoke/bin/activate
pip install -r /home/azureuser/token-safe/scripts/requirements-langchain-smoke.txt
```

## 2) Required client env vars

```bash
export KGW_BASE_URL="http://127.0.0.1:8789"
export KGW_TOKEN="kgw_demo_doc_ingestor"
export KGW_SYSTEM_PROMPT="You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets."
```

Provider model/deployment defaults used by the script:
- OpenAI native: `KGW_OPENAI_MODEL=gpt-4o-mini`
- Azure native: `KGW_AZURE_DEPLOYMENT=azure-mini`, `KGW_AZURE_API_VERSION=2024-10-21`
- Ollama native: `KGW_OLLAMA_MODEL=llama3.1`
- Gemini native: `KGW_GEMINI_MODEL=gemini-2.0-flash`, `KGW_GEMINI_ENDPOINT=$KGW_BASE_URL`

## 3) Native endpoint calls (stream + non-stream)

OpenAI native:

```bash
python /home/azureuser/token-safe/scripts/external_langchain_smoke.py --provider openai --mode native
python /home/azureuser/token-safe/scripts/external_langchain_smoke.py --provider openai --mode native --stream
```

Azure OpenAI native:

```bash
python /home/azureuser/token-safe/scripts/external_langchain_smoke.py --provider azure_openai --mode native
python /home/azureuser/token-safe/scripts/external_langchain_smoke.py --provider azure_openai --mode native --stream
```

Ollama native:

```bash
python /home/azureuser/token-safe/scripts/external_langchain_smoke.py --provider ollama --mode native
python /home/azureuser/token-safe/scripts/external_langchain_smoke.py --provider ollama --mode native --stream
```

Gemini native:

```bash
python /home/azureuser/token-safe/scripts/external_langchain_smoke.py --provider gemini --mode native
python /home/azureuser/token-safe/scripts/external_langchain_smoke.py --provider gemini --mode native --stream
```

If Gemini native fails with an endpoint-override error, your `langchain_google_genai` version does not support custom API endpoint routing. Use universal fallback mode below.

## 4) Universal OpenAI adapter fallback (stream + non-stream for each provider model)

OpenAI model via `/v1`:

```bash
python /home/azureuser/token-safe/scripts/external_langchain_smoke.py --provider openai --mode universal
python /home/azureuser/token-safe/scripts/external_langchain_smoke.py --provider openai --mode universal --stream
```

Azure deployment-model via `/v1`:

```bash
python /home/azureuser/token-safe/scripts/external_langchain_smoke.py --provider azure_openai --mode universal
python /home/azureuser/token-safe/scripts/external_langchain_smoke.py --provider azure_openai --mode universal --stream
```

Ollama model via `/v1`:

```bash
python /home/azureuser/token-safe/scripts/external_langchain_smoke.py --provider ollama --mode universal
python /home/azureuser/token-safe/scripts/external_langchain_smoke.py --provider ollama --mode universal --stream
```

Gemini model via `/v1`:

```bash
python /home/azureuser/token-safe/scripts/external_langchain_smoke.py --provider gemini --mode universal
python /home/azureuser/token-safe/scripts/external_langchain_smoke.py --provider gemini --mode universal --stream
```

## 5) Optional: prove safe-token replacement with mock upstream capture

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
