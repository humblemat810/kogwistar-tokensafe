function q(id) { return document.getElementById(id); }

function filterParams() {
  const p = new URLSearchParams();
  if (q("subjectType").value) p.set("subject_type", q("subjectType").value);
  if (q("subjectId").value) p.set("subject_id", q("subjectId").value);
  if (q("provider").value) p.set("provider", q("provider").value);
  if (q("model").value) p.set("model", q("model").value);
  if (q("decision").value) p.set("decision", q("decision").value);
  if (q("httpStatus").value) p.set("http_status", q("httpStatus").value);
  p.set("time_range", q("timeRange").value);
  p.set("page", "1");
  p.set("page_size", "100");
  return p;
}

async function loadConfig() {
  const resp = await fetch("/admin/history/config");
  const data = await resp.json();
  q("cfgEnabled").checked = !!data.enabled;
  q("cfgRetentionDays").value = data.retention_days || 30;
  q("cfgMaxActiveRecords").value = data.max_active_records || 10000;
  q("cfgMaxActiveBytes").value = data.max_active_bytes || 52428800;
}

async function saveConfig() {
  const payload = {
    enabled: q("cfgEnabled").checked,
    retention_days: Number(q("cfgRetentionDays").value || 30),
    max_active_records: Number(q("cfgMaxActiveRecords").value || 10000),
    max_active_bytes: Number(q("cfgMaxActiveBytes").value || 52428800),
  };
  const resp = await fetch("/admin/history/config", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    q("cfgStatus").textContent = "save failed";
    return;
  }
  q("cfgStatus").textContent = "saved";
  await refresh();
}

async function showDetail(requestId) {
  const resp = await fetch(`/admin/history/${encodeURIComponent(requestId)}.json`);
  if (!resp.ok) {
    q("detailMeta").textContent = `detail unavailable for ${requestId}`;
    q("requestBody").textContent = "";
    q("responseBody").textContent = "";
    q("streamChunks").textContent = "";
    return;
  }
  const data = await resp.json();
  const meta = data.metadata || {};
  q("detailMeta").textContent = `${meta.ts || ""} request_id=${data.request_id} provider=${meta.provider || ""} status=${meta.http_status || ""}`;
  q("requestBody").textContent = data.request_body_text || "";
  q("responseBody").textContent = data.response_body_text || "";
  q("streamChunks").textContent = (data.stream_chunks || []).join("\n---\n");
}

async function refresh() {
  const resp = await fetch("/admin/history.json?" + filterParams().toString());
  const data = await resp.json();
  const body = q("historyBody");
  body.innerHTML = "";
  for (const row of (data.data || [])) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${row.ts || ""}</td>
      <td><code>${row.request_id || ""}</code></td>
      <td>${row.provider || ""}</td>
      <td>${row.route_family || ""}</td>
      <td>${row.principal_id || ""}</td>
      <td>${row.on_behalf_of_user_id || ""}</td>
      <td>${row.key_id || ""}</td>
      <td>${row.model || ""}</td>
      <td>${row.decision || ""}</td>
      <td>${row.http_status || ""}</td>
      <td><button data-request-id="${row.request_id || ""}">View</button></td>`;
    body.appendChild(tr);
  }
  for (const btn of body.querySelectorAll("button[data-request-id]")) {
    btn.onclick = () => showDetail(btn.getAttribute("data-request-id"));
  }
}

q("refreshBtn").onclick = refresh;
q("saveCfgBtn").onclick = saveConfig;

loadConfig().then(refresh);
