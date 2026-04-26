function q(name) { return document.getElementById(name); }

function subjectLink(type, value) {
  if (!value) {
    return "";
  }
  const p = new URLSearchParams();
  p.set("subject_type", type);
  p.set("subject_id", value);
  p.set("time_range", q("timeRange").value);
  p.set("bucket", q("bucket").value);
  return `<a href="/admin/usage?${p.toString()}">${value}</a>`;
}

function drawLines(canvasId, series, fields) {
  const c = q(canvasId);
  const ctx = c.getContext("2d");
  ctx.clearRect(0, 0, c.width, c.height);
  if (!series.length) {
    ctx.fillText("no data", 20, 30);
    return;
  }
  const pad = 28;
  const w = c.width - pad * 2;
  const h = c.height - pad * 2;
  const all = [];
  for (const s of series) {
    for (const f of fields) {
      all.push(Number(s[f] || 0));
    }
  }
  const max = Math.max(1, ...all);
  const colors = ["#2563eb", "#16a34a", "#dc2626", "#a855f7", "#d97706"];
  fields.forEach((f, idx) => {
    ctx.beginPath();
    ctx.strokeStyle = colors[idx % colors.length];
    ctx.lineWidth = 2;
    series.forEach((s, i) => {
      const x = pad + (i / Math.max(1, series.length - 1)) * w;
      const y = pad + h - (Number(s[f] || 0) / max) * h;
      if (i === 0) {
        ctx.moveTo(x, y);
      } else {
        ctx.lineTo(x, y);
      }
    });
    ctx.stroke();
    ctx.fillStyle = ctx.strokeStyle;
    ctx.fillRect(pad + idx * 110, 6, 12, 12);
    ctx.fillStyle = "#111";
    ctx.fillText(f, pad + idx * 110 + 16, 16);
  });
  ctx.strokeStyle = "#888";
  ctx.strokeRect(pad, pad, w, h);
}

async function refresh() {
  const p = new URLSearchParams();
  if (q("subjectType").value) {
    p.set("subject_type", q("subjectType").value);
  }
  if (q("subjectId").value) {
    p.set("subject_id", q("subjectId").value);
  }
  p.set("time_range", q("timeRange").value);
  p.set("bucket", q("bucket").value);
  const resp = await fetch("/admin/usage.json?" + p.toString());
  const data = await resp.json();
  const ts = data.charts.time_series || [];
  drawLines("requestsChart", ts, ["requests", "allowed", "denied"]);
  drawLines("tokensChart", ts, ["tokens", "usd", "anomaly_score"]);
  q("topModels").textContent = JSON.stringify(data.charts.top_models || [], null, 2);
  q("topReasons").textContent = JSON.stringify(data.charts.top_deny_reasons || [], null, 2);
  const body = q("eventsBody");
  body.innerHTML = "";
  (data.drilldown.events || []).slice(-100).reverse().forEach((e) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${e.ts || ""}</td><td>${subjectLink("principal", e.principal_id || "")}</td><td>${subjectLink("user", e.on_behalf_of_user_id || "")}</td><td>${subjectLink("key", e.key_id || "")}</td><td>${subjectLink("token", e.token_id || "")}</td><td>${e.model || ""}</td><td>${e.decision || ""}</td><td>${e.reason || ""}</td>`;
    body.appendChild(tr);
  });
}

const initial = new URLSearchParams(window.location.search);
if (initial.get("subject_type")) {
  q("subjectType").value = initial.get("subject_type");
}
if (initial.get("subject_id")) {
  q("subjectId").value = initial.get("subject_id");
}
if (initial.get("time_range")) {
  q("timeRange").value = initial.get("time_range");
}
if (initial.get("bucket")) {
  q("bucket").value = initial.get("bucket");
}
q("refreshBtn").onclick = refresh;
refresh();
