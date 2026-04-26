from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any


def load_usage_events(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def derive_prompt_heuristics(payload: dict[str, Any], usage_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    usage_profile = usage_profile or {}
    text = "\n".join(_extract_message_texts(payload)).lower()

    observed_intents: set[str] = set()
    if any(tok in text for tok in ("summarize", "summary", "bullet", "tl;dr", "tldr")):
        observed_intents.add("summarization")
    if "?" in text or any(tok in text for tok in ("what ", "why ", "how ", "explain", "answer")):
        observed_intents.add("qna")
    if any(tok in text for tok in ("code", "python", "javascript", "typescript", "refactor", "compile", "function", "class", "bug")):
        observed_intents.add("coding")

    exfiltration_signals = [
        token
        for token in ("api key", "apikey", "token", "secret", "password", "credential", "bearer", "sk-", "private key")
        if token in text
    ]
    token_exfiltration_attempt = bool(exfiltration_signals) and any(tok in text for tok in ("extract", "reveal", "show", "dump", "print", "leak", "exfiltrate"))

    allowed = {str(x).strip().lower() for x in usage_profile.get("allowed_intents", []) if str(x).strip()}
    disallowed = sorted(intent for intent in observed_intents if allowed and intent not in allowed)
    intent_drift = bool(disallowed)

    return {
        "observed_intents": sorted(observed_intents),
        "allowed_intents": sorted(allowed),
        "disallowed_intents": disallowed,
        "intent_drift": intent_drift,
        "token_exfiltration_attempt": token_exfiltration_attempt,
        "token_exfiltration_signals": exfiltration_signals,
    }


def build_usage_monitor_dataset(
    events: list[dict[str, Any]],
    policy: dict[str, Any],
    *,
    subject_type: str | None = None,
    subject_id: str | None = None,
    time_range: str = "24h",
    bucket: str = "hour",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(seconds=_range_seconds(time_range))
    bucket_seconds = _bucket_seconds(bucket)

    filtered = [
        e
        for e in events
        if _parse_ts(e.get("ts")) >= since and _matches_subject(e, subject_type, subject_id)
    ]
    filtered.sort(key=lambda x: _parse_ts(x.get("ts")))

    by_bucket: dict[str, dict[str, Any]] = {}
    model_counts = Counter()
    deny_reasons = Counter()
    totals = {"requests": 0, "allowed": 0, "denied": 0, "tokens": 0, "usd": 0.0}

    for e in filtered:
        ts = _parse_ts(e.get("ts"))
        bkey = _bucket_key(ts, bucket_seconds)
        row = by_bucket.setdefault(
            bkey,
            {"bucket": bkey, "requests": 0, "allowed": 0, "denied": 0, "tokens": 0, "usd": 0.0, "anomaly_score": 0.0, "top_model": None},
        )
        denied = _is_denied(e)
        row["requests"] += 1
        row["allowed"] += 0 if denied else 1
        row["denied"] += 1 if denied else 0
        row["tokens"] += int(e.get("estimated_tokens") or 0)
        row["usd"] += float(e.get("estimated_cost_usd") or 0.0)
        row["anomaly_score"] += _event_anomaly_score(e)

        totals["requests"] += 1
        totals["allowed"] += 0 if denied else 1
        totals["denied"] += 1 if denied else 0
        totals["tokens"] += int(e.get("estimated_tokens") or 0)
        totals["usd"] += float(e.get("estimated_cost_usd") or 0.0)

        model = str(e.get("model") or "")
        if model:
            model_counts[model] += 1
        if denied:
            deny_reasons[str(e.get("reason") or "unknown")] += 1

    # Model-shift anomaly bonus between adjacent buckets.
    ordered = [by_bucket[k] for k in sorted(by_bucket)]
    prev_model: str | None = None
    for row in ordered:
        bucket_events = [e for e in filtered if _bucket_key(_parse_ts(e.get("ts")), bucket_seconds) == row["bucket"]]
        top = Counter(str(e.get("model") or "") for e in bucket_events if e.get("model")).most_common(1)
        row["top_model"] = top[0][0] if top else None
        if prev_model and row["top_model"] and row["top_model"] != prev_model:
            row["anomaly_score"] += 2.0
        prev_model = row["top_model"] or prev_model
        row["usd"] = round(float(row["usd"]), 6)

    return {
        "filters": {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "time_range": time_range,
            "bucket": bucket,
        },
        "overview": {
            "requests": totals["requests"],
            "allowed": totals["allowed"],
            "denied": totals["denied"],
            "tokens": totals["tokens"],
            "usd": round(float(totals["usd"]), 6),
        },
        "charts": {
            "time_series": ordered,
            "top_models": [{"model": m, "count": c} for m, c in model_counts.most_common(10)],
            "top_deny_reasons": [{"reason": r, "count": c} for r, c in deny_reasons.most_common(10)],
        },
        "drilldown": {
            "events": [_compact_event(e) for e in filtered[-200:]],
        },
        "usage_profiles": policy.get("usage_profiles", {}),
    }


def render_usage_monitor_html() -> str:
    return """<!doctype html>
<html>
<head>
  <meta charset='utf-8'>
  <title>ModelKeyGuard Usage Ops</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 1.2rem; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(320px, 1fr)); gap: 1rem; }
    .card { border: 1px solid #ddd; border-radius: 8px; padding: 0.8rem; }
    label { font-size: .9rem; margin-right: .5rem; }
    select,input { margin-right: .8rem; }
    canvas { width: 100%; height: 220px; border: 1px solid #eee; border-radius: 6px; }
    table { width: 100%; border-collapse: collapse; font-size: .85rem; }
    th, td { border: 1px solid #ddd; padding: .25rem .4rem; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  </style>
</head>
<body>
  <h1>Usage Ops Monitor</h1>
  <div class='card'>
    <label>Subject Type</label>
    <select id='subjectType'>
      <option value=''>all</option><option value='principal'>principal</option><option value='user'>user</option><option value='key'>key</option><option value='token'>token</option>
    </select>
    <label>Subject ID</label><input id='subjectId' placeholder='optional subject id'/>
    <label>Range</label>
    <select id='timeRange'><option>1h</option><option selected>24h</option><option>7d</option><option>30d</option></select>
    <label>Bucket</label>
    <select id='bucket'><option>10s</option><option selected>hour</option><option>day</option></select>
    <button id='refreshBtn'>Refresh</button>
  </div>

  <div class='grid'>
    <div class='card'><h3>Requests / Allow / Deny</h3><canvas id='requestsChart' width='560' height='240'></canvas></div>
    <div class='card'><h3>Tokens / USD / Anomaly</h3><canvas id='tokensChart' width='560' height='240'></canvas></div>
    <div class='card'><h3>Top Models</h3><pre id='topModels' class='mono'></pre></div>
    <div class='card'><h3>Top Deny Reasons</h3><pre id='topReasons' class='mono'></pre></div>
  </div>

  <div class='card'>
    <h3>Recent Events</h3>
    <table>
      <thead><tr><th>ts</th><th>principal</th><th>user</th><th>key</th><th>token</th><th>model</th><th>decision</th><th>reason</th></tr></thead>
      <tbody id='eventsBody'></tbody>
    </table>
  </div>

  <script>
  function q(name){ return document.getElementById(name); }
  function subjectLink(type, value){
    if(!value) return '';
    const p = new URLSearchParams();
    p.set('subject_type', type);
    p.set('subject_id', value);
    p.set('time_range', q('timeRange').value);
    p.set('bucket', q('bucket').value);
    return `<a href="/admin/usage?${p.toString()}">${value}</a>`;
  }
  function drawLines(canvasId, series, fields){
    const c=q(canvasId), ctx=c.getContext('2d');
    ctx.clearRect(0,0,c.width,c.height);
    if(!series.length){ ctx.fillText('no data',20,30); return; }
    const pad=28, w=c.width-pad*2, h=c.height-pad*2;
    const all=[];
    for(const s of series){ for(const f of fields){ all.push(Number(s[f]||0)); } }
    const max=Math.max(1,...all);
    const colors=['#2563eb','#16a34a','#dc2626','#a855f7','#d97706'];
    fields.forEach((f,idx)=>{
      ctx.beginPath(); ctx.strokeStyle=colors[idx%colors.length]; ctx.lineWidth=2;
      series.forEach((s,i)=>{
        const x=pad+(i/(Math.max(1,series.length-1)))*w;
        const y=pad+h-(Number(s[f]||0)/max)*h;
        if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
      });
      ctx.stroke();
      ctx.fillStyle=ctx.strokeStyle; ctx.fillRect(pad+idx*110,6,12,12); ctx.fillStyle='#111'; ctx.fillText(f,pad+idx*110+16,16);
    });
    ctx.strokeStyle='#888'; ctx.strokeRect(pad,pad,w,h);
  }
  async function refresh(){
    const p=new URLSearchParams();
    if(q('subjectType').value) p.set('subject_type', q('subjectType').value);
    if(q('subjectId').value) p.set('subject_id', q('subjectId').value);
    p.set('time_range', q('timeRange').value); p.set('bucket', q('bucket').value);
    const resp=await fetch('/admin/usage.json?'+p.toString()); const data=await resp.json();
    const ts=data.charts.time_series||[];
    drawLines('requestsChart', ts, ['requests','allowed','denied']);
    drawLines('tokensChart', ts, ['tokens','usd','anomaly_score']);
    q('topModels').textContent=JSON.stringify(data.charts.top_models||[], null, 2);
    q('topReasons').textContent=JSON.stringify(data.charts.top_deny_reasons||[], null, 2);
    const body=q('eventsBody'); body.innerHTML='';
    (data.drilldown.events||[]).slice(-100).reverse().forEach(e=>{
      const tr=document.createElement('tr');
      tr.innerHTML=`<td>${e.ts||''}</td><td>${subjectLink('principal', e.principal_id||'')}</td><td>${subjectLink('user', e.on_behalf_of_user_id||'')}</td><td>${subjectLink('key', e.key_id||'')}</td><td>${subjectLink('token', e.token_id||'')}</td><td>${e.model||''}</td><td>${e.decision||''}</td><td>${e.reason||''}</td>`;
      body.appendChild(tr);
    });
  }
  const initial = new URLSearchParams(window.location.search);
  if(initial.get('subject_type')) q('subjectType').value = initial.get('subject_type');
  if(initial.get('subject_id')) q('subjectId').value = initial.get('subject_id');
  if(initial.get('time_range')) q('timeRange').value = initial.get('time_range');
  if(initial.get('bucket')) q('bucket').value = initial.get('bucket');
  q('refreshBtn').onclick=refresh; refresh();
  </script>
</body>
</html>
"""


def _extract_message_texts(payload: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for message in payload.get("messages", []) if isinstance(payload.get("messages"), list) else []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and "text" in item:
                    out.append(str(item["text"]))
    return out


def _parse_ts(value: Any) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _range_seconds(time_range: str) -> int:
    return {"1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000}.get(time_range, 86400)


def _bucket_seconds(bucket: str) -> int:
    return {"10s": 10, "hour": 3600, "day": 86400}.get(bucket, 3600)


def _bucket_key(ts: datetime, bucket_seconds: int) -> str:
    epoch = int(ts.timestamp())
    floor_epoch = (epoch // bucket_seconds) * bucket_seconds
    return datetime.fromtimestamp(floor_epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _matches_subject(event: dict[str, Any], subject_type: str | None, subject_id: str | None) -> bool:
    if not subject_type:
        return True
    field = {
        "principal": "principal_id",
        "user": "on_behalf_of_user_id",
        "key": "key_id",
        "token": "token_id",
    }.get(subject_type)
    if not field:
        return True
    if subject_id is None:
        return bool(event.get(field))
    return str(event.get(field) or "") == subject_id


def _is_denied(event: dict[str, Any]) -> bool:
    decision = str(event.get("decision", "")).upper()
    reason = str(event.get("reason", "")).lower()
    return decision in {"BLOCKED", "DENIED"} or "denied" in reason or reason.endswith("exceeded")


def _event_anomaly_score(event: dict[str, Any]) -> float:
    score = 0.0
    if _is_denied(event):
        score += 2.0
    heur = event.get("prompt_heuristics") if isinstance(event.get("prompt_heuristics"), dict) else {}
    if heur.get("token_exfiltration_attempt"):
        score += 5.0
    if heur.get("intent_drift"):
        score += 4.0
    return score


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": event.get("ts"),
        "request_id": event.get("request_id"),
        "principal_id": event.get("principal_id"),
        "on_behalf_of_user_id": event.get("on_behalf_of_user_id"),
        "application_id": event.get("application_id"),
        "key_id": event.get("key_id"),
        "token_id": event.get("token_id"),
        "model": event.get("model"),
        "decision": event.get("decision"),
        "reason": event.get("reason"),
        "estimated_tokens": event.get("estimated_tokens"),
        "estimated_cost_usd": event.get("estimated_cost_usd"),
        "prompt_heuristics": event.get("prompt_heuristics", {}),
    }
