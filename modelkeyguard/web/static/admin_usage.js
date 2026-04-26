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

function formatTop(items, valueKey, labelKey) {
  if (!items || !items.length) {
    return "(no data)";
  }
  return items.map((item, idx) => `${idx + 1}. ${item[labelKey]}: ${item[valueKey]}`).join("\n");
}

let requestsChart = null;
let tokensChart = null;

function createLineChart(canvasId, labels, datasets, yTitle) {
  if (typeof Chart === "undefined") {
    throw new Error("Chart.js not loaded from /static/vendor/chart.umd.min.js");
  }

  const canvas = q(canvasId);
  return new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets,
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "top" },
      },
      scales: {
        x: {
          ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 8 },
          grid: { color: "rgba(15, 23, 42, 0.08)" },
        },
        y: {
          beginAtZero: true,
          title: { display: true, text: yTitle },
          grid: { color: "rgba(15, 23, 42, 0.08)" },
        },
      },
      elements: {
        line: { tension: 0.2, borderWidth: 2 },
        point: { radius: 1.5, hoverRadius: 4 },
      },
    },
  });
}

function renderCharts(timeSeries) {
  const labels = (timeSeries || []).map((row) => String(row.bucket || "").replace("T", " ").slice(0, 16));
  const requestsData = {
    requests: (timeSeries || []).map((row) => Number(row.requests || 0)),
    allowed: (timeSeries || []).map((row) => Number(row.allowed || 0)),
    denied: (timeSeries || []).map((row) => Number(row.denied || 0)),
  };
  const tokenData = {
    tokens: (timeSeries || []).map((row) => Number(row.tokens || 0)),
    usd: (timeSeries || []).map((row) => Number(row.usd || 0)),
    anomaly_score: (timeSeries || []).map((row) => Number(row.anomaly_score || 0)),
  };

  if (requestsChart) {
    requestsChart.destroy();
  }
  if (tokensChart) {
    tokensChart.destroy();
  }

  requestsChart = createLineChart(
    "requestsChart",
    labels,
    [
      { label: "requests", data: requestsData.requests, borderColor: "#2563eb", backgroundColor: "rgba(37, 99, 235, 0.15)" },
      { label: "allowed", data: requestsData.allowed, borderColor: "#16a34a", backgroundColor: "rgba(22, 163, 74, 0.15)" },
      { label: "denied", data: requestsData.denied, borderColor: "#dc2626", backgroundColor: "rgba(220, 38, 38, 0.15)" },
    ],
    "count"
  );

  tokensChart = createLineChart(
    "tokensChart",
    labels,
    [
      { label: "tokens", data: tokenData.tokens, borderColor: "#2563eb", backgroundColor: "rgba(37, 99, 235, 0.15)" },
      { label: "usd", data: tokenData.usd, borderColor: "#16a34a", backgroundColor: "rgba(22, 163, 74, 0.15)" },
      { label: "anomaly_score", data: tokenData.anomaly_score, borderColor: "#dc2626", backgroundColor: "rgba(220, 38, 38, 0.15)" },
    ],
    "value"
  );
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

  renderCharts(data.charts.time_series || []);

  q("topModels").textContent = formatTop(data.charts.top_models || [], "count", "model");
  q("topReasons").textContent = formatTop(data.charts.top_deny_reasons || [], "count", "reason");

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
