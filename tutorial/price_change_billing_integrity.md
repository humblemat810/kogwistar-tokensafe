# Price Change Billing Integrity: Pre-Change + Post-Change Cost Accounting

This flow validates that billing totals are not retroactively recomputed when model price changes.

Goal:

- request A before price change uses old price,
- request B after price change uses new price,
- total billing is `cost(A) + cost(B)`,
- old requests are not recalculated with new price.

Prerequisite:

- You already have gateway running with:
  - `MODELKEYGUARD_POLICY_PATH=out/azure_real_billing_policy.json`
  - `MODELKEYGUARD_AUDIT_PATH=out/azure_real_billing_audit.jsonl`
  - valid key + token from the Azure real flow tutorial.

## 1) Run pre-change request (price v1)

Confirm current price in policy (example `0.010`):

```bash
python - <<'PY'
import json
p=json.load(open("out/azure_real_billing_policy.json"))
print("price_v1:", p["model_price_per_1k_tokens_usd"]["gpt-5-mini"])
PY
```

Send request A:

```bash
export KGW_USER_PROMPT='PRICE_PHASE_V1 request'
./scripts/smoke_azure_real_completion.sh
```

## 2) Change model price in policy file

Update to price v2 (example `0.020`):

```bash
python - <<'PY'
import json
from pathlib import Path

path = Path("out/azure_real_billing_policy.json")
p = json.loads(path.read_text())
p.setdefault("model_price_per_1k_tokens_usd", {})["gpt-5-mini"] = 0.020
path.write_text(json.dumps(p, indent=2), encoding="utf-8")
print("price_v2:", p["model_price_per_1k_tokens_usd"]["gpt-5-mini"])
PY
```

Restart gateway so new policy price is loaded:

```bash
pkill -f "python -m modelkeyguard gateway" || true
./scripts/start_gateway.sh
```

## 3) Run post-change request (price v2)

```bash
export KGW_USER_PROMPT='PRICE_PHASE_V2 request'
./scripts/smoke_azure_real_completion.sh
```

## 4) Verify pre/post billing is additive (not retroactive)

Show last two allowed events for the same token:

```bash
python - <<'PY'
import json
from pathlib import Path

rows=[]
for line in Path("out/azure_real_billing_audit.jsonl").read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    r=json.loads(line)
    if r.get("token_id")=="kgw-azure-billing-demo" and r.get("decision")=="ALLOWED":
        rows.append(r)

rows=rows[-2:]
if len(rows)<2:
    raise SystemExit("Need at least 2 allowed events for comparison.")

for i,r in enumerate(rows,1):
    print(f"event_{i}: request_id={r.get('request_id')} estimated_tokens={r.get('estimated_tokens')} estimated_cost_usd={r.get('estimated_cost_usd')}")

total = round(sum(float(r.get("estimated_cost_usd") or 0.0) for r in rows), 6)
print("sum_last_two_estimated_cost_usd:", total)
PY
```

Now compare monitor total (same subject filter):

```bash
curl -sS -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
  'http://127.0.0.1:8789/admin/usage.json?subject_type=principal&subject_id=app:azure-billing-demo&time_range=24h&bucket=5m' \
  | python -m json.tool
```

Interpretation:

- `overview.usd` is derived from stored per-event `estimated_cost_usd`.
- When price changes, only new events use new price.
- Existing historical events keep their original recorded cost.

This behavior is also pinned by regression test `test_083_usage_monitor_totals_are_event_cost_based_not_retroactive_policy_price`.
