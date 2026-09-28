const q = (id) => document.getElementById(id);

function setStatus(message, kind = "") {
  const node = q("statusMessage");
  node.textContent = message;
  node.className = `status ${kind}`.trim();
}

function addCell(row, value, tag = "td") {
  const cell = document.createElement(tag);
  cell.textContent = value == null ? "" : String(value);
  row.appendChild(cell);
}

function renderTriggers(items) {
  const target = q("triggers");
  target.replaceChildren();
  if (!items.length) { const empty = document.createElement("p"); empty.className = "muted"; empty.textContent = "No trigger data."; target.appendChild(empty); return; }
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = `trigger${item.triggered ? " on" : ""}`;
    const label = document.createElement("span"); label.className = "trigger-label"; label.textContent = item.trigger_id || "unknown";
    const value = document.createElement("span"); value.className = "trigger-value"; value.textContent = `${item.current ?? 0} / ${item.threshold ?? 0}${item.triggered ? " · triggered" : ""}`;
    row.append(label, value); target.appendChild(row);
  });
}

function renderTable(targetId, columns, rows) {
  const target = q(targetId); target.replaceChildren();
  if (!rows.length) { const empty = document.createElement("p"); empty.className = "muted"; empty.textContent = "No records."; target.appendChild(empty); return; }
  const table = document.createElement("table");
  const head = document.createElement("thead"); const headRow = document.createElement("tr");
  columns.forEach((column) => addCell(headRow, column, "th")); head.appendChild(headRow); table.appendChild(head);
  const body = document.createElement("tbody");
  rows.forEach((item) => { const row = document.createElement("tr"); columns.forEach((column) => addCell(row, item[column])); body.appendChild(row); });
  table.appendChild(body); target.appendChild(table);
}

function renderStatus(data) {
  const summary = data.summary || {};
  q("decision").textContent = data.should_review ? "Review required" : "Monitoring";
  q("conversationCount").textContent = summary.conversation_count_since_last_review ?? 0;
  q("tokenCount").textContent = summary.token_used_since_last_review ?? 0;
  q("usdCount").textContent = Number(summary.dollar_used_since_last_review || 0).toFixed(4);
  q("generatedAt").textContent = data.generated_at || "—";
  renderTriggers(Array.isArray(data.triggers) ? data.triggers : []);
  renderTable("historyPreview", ["request_id", "ts", "matched_keywords"], data.history_preview || []);
  renderTable("usagePreview", ["request_id", "ts", "principal_id", "actual_tokens", "actual_cost_usd"], data.usage_preview || []);
}

async function refresh() {
  try { const response = await fetch("/admin/review/status.json", { headers: { Accept: "application/json" } }); if (!response.ok) throw new Error(`status ${response.status}`); renderStatus(await response.json()); setStatus("Status loaded.", "ok"); }
  catch (error) { setStatus(`Unable to load review status: ${error.message}`, "error"); }
}

async function startSnapshot() {
  q("startBtn").disabled = true;
  try {
    const response = await fetch("/admin/review/start", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ requested_by: "admin-ui" }) });
    const data = await response.json(); if (!response.ok) throw new Error(data.error?.message || `status ${response.status}`);
    q("runId").textContent = data.run_id || ""; q("runPanel").hidden = false; setStatus("Evidence snapshot created; send run ID to the controlled reviewer.", "ok"); renderStatus(data.status || {});
  } catch (error) { setStatus(`Unable to create snapshot: ${error.message}`, "error"); }
  finally { q("startBtn").disabled = false; }
}

q("refreshBtn").addEventListener("click", refresh);
q("startBtn").addEventListener("click", startSnapshot);
q("copyRunIdBtn").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(q("runId").textContent);
    setStatus("Run ID copied.", "ok");
  } catch (error) {
    setStatus(`Unable to copy run ID: ${error.message}`, "error");
  }
});
refresh();
