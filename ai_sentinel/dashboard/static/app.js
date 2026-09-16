const COLORS = { "": "#5b9dff", warn: "#f5c344", bad: "#f5556c" };
const history = {};
// Trace waterfalls and incident evidence panels re-render from scratch every poll, so "open"
// state has to live here rather than as a DOM class that the next refresh would wipe out.
const expanded = { traces: new Set(), evidence: new Set() };

const METRIC_TILES = [
  { key: "availability", label: "Availability", fmt: v => (v * 100).toFixed(1) + "%", warnBelow: 0.99, badBelow: 0.95 },
  { key: "error_rate", label: "Error rate", fmt: v => (v * 100).toFixed(1) + "%", warnAbove: 0.05, badAbove: 0.2 },
  { key: "p50_ms", label: "p50 latency", fmt: v => Math.round(v) + "ms" },
  { key: "p95_ms", label: "p95 latency", fmt: v => Math.round(v) + "ms", warnAbove: 1500, badAbove: 3000 },
  { key: "p99_ms", label: "p99 latency", fmt: v => Math.round(v) + "ms" },
  { key: "timeout_rate", label: "Timeout rate", fmt: v => (v * 100).toFixed(1) + "%", warnAbove: 0.05, badAbove: 0.2 },
  { key: "invalid_output_rate", label: "Invalid output", fmt: v => (v * 100).toFixed(1) + "%", warnAbove: 0.05, badAbove: 0.2 },
  { key: "avg_tokens", label: "Avg tokens/req", fmt: v => Math.round(v) },
];

const ACTION_LABELS = {
  "Fail over to backup backend": "fail_over",
  "Disable retrieval": "disable_retrieval",
  "Disable tool calls": "disable_tools",
};

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function fetchJSON(url, opts) {
  const resp = await fetch(url, opts);
  if (!resp.ok) throw new Error(`${url} -> ${resp.status}`);
  return resp.json();
}

function postJSON(url, body) {
  return fetchJSON(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

function pushHistory(key, value) {
  if (!history[key]) history[key] = [];
  history[key].push(value);
  if (history[key].length > 40) history[key].shift();
}

function sparkline(values, level) {
  const w = 120, h = 24;
  if (!values || values.length < 2) return `<svg viewBox="0 0 ${w} ${h}"></svg>`;
  const max = Math.max(...values);
  const min = Math.min(...values);
  const range = max - min || 1;
  const step = w / (values.length - 1);
  const pts = values
    .map((v, i) => `${(i * step).toFixed(1)},${(h - ((v - min) / range) * h * 0.85 - h * 0.075).toFixed(1)}`)
    .join(" ");
  const color = COLORS[level] || COLORS[""];
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" /></svg>`;
}

function tileLevel(tile, v) {
  if (tile.badAbove !== undefined && v >= tile.badAbove) return "bad";
  if (tile.warnAbove !== undefined && v >= tile.warnAbove) return "warn";
  if (tile.badBelow !== undefined && v <= tile.badBelow) return "bad";
  if (tile.warnBelow !== undefined && v <= tile.warnBelow) return "warn";
  return "";
}

function setBadge(prefix, level, text) {
  document.getElementById(`${prefix}-badge`).className = `badge ${level}`;
  document.getElementById(`${prefix}-text`).textContent = text;
}

function renderHealth(data) {
  const pill = document.getElementById("overall-pill");
  pill.textContent = data.overall;
  pill.className = "status-pill " + data.overall;
  document.getElementById("header-dot").style.background = data.overall === "healthy" ? "#3fd67a" : "#f5556c";

  setBadge("live", "ok", "alive");

  const readyOk = data.readiness.status === "ready";
  const errPart = data.readiness.error_rate !== undefined ? ` (err ${Math.round(data.readiness.error_rate * 100)}%)` : "";
  setBadge("ready", readyOk ? "ok" : "bad", data.readiness.status + errPart);

  const s = data.synthetic;
  let synthBadge = "pending";
  if (s.status === "ok") synthBadge = "ok";
  else if (s.status === "stale") synthBadge = "warn";
  else if (s.status === "failing") synthBadge = "bad";
  const synthText = s.status === "pending" ? "pending" : `${s.status} · ${Math.round(s.age_s)}s ago`;
  setBadge("synth", synthBadge, synthText);
  document.getElementById("synth-detail").textContent =
    s.detail || '"Return exactly the word HEALTHY" every 60s';
}

function renderMetrics(data) {
  const r = data.requests;
  const el = document.getElementById("metrics-grid");
  el.innerHTML = METRIC_TILES.map(tile => {
    const v = r[tile.key] ?? 0;
    pushHistory(tile.key, v);
    const level = tileLevel(tile, v);
    return `<div class="tile">
      <div class="label">${tile.label}</div>
      <div class="value ${level}">${tile.fmt(v)}</div>
      ${sparkline(history[tile.key], level)}
    </div>`;
  }).join("");
}

function renderControls(state) {
  const faultEl = document.getElementById("fault-buttons");
  faultEl.innerHTML = state.available_fault_modes
    .map(m => {
      const active = m === state.fault_mode;
      const cls = active ? (m === "normal" ? "active" : "active danger") : "";
      return `<button data-fault="${m}" class="${cls}">${m.replace(/_/g, " ")}</button>`;
    })
    .join("");

  const backendEl = document.getElementById("backend-buttons");
  backendEl.innerHTML = state.available_backends
    .map(b => `<button data-backend="${b}" class="${b === state.active_backend ? "active" : ""}">${b}</button>`)
    .join("");
}

function renderIncidents(list) {
  const el = document.getElementById("incident-list");
  if (!list.length) {
    el.innerHTML = '<div class="empty">No incidents yet — inject a fault and send some test traffic.</div>';
    return;
  }
  el.innerHTML = list
    .map(inc => {
      const time = new Date(inc.ts * 1000).toLocaleTimeString();
      const actions = [];
      if (inc.status === "open") {
        const actionCode = ACTION_LABELS[inc.recommended_action];
        if (actionCode) {
          actions.push(`<button class="primary" data-incident="${inc.id}" data-action="${actionCode}">${inc.recommended_action}</button>`);
        }
        actions.push(`<button data-incident="${inc.id}" data-action="ignore">Ignore</button>`);
        actions.push(`<button class="ghost" data-toggle-evidence="${inc.id}">Investigate</button>`);
      } else {
        actions.push(`<span class="incident-status-label">${inc.status}</span>`);
        actions.push(`<button class="ghost" data-toggle-evidence="${inc.id}">Details</button>`);
      }
      const evidenceOpen = expanded.evidence.has(String(inc.id)) ? " open" : "";
      return `
      <div class="incident-card ${inc.severity} ${inc.status}">
        <div class="incident-top">
          <span class="sev ${inc.severity}">${inc.severity} · ${inc.detector}</span>
          <span class="time">${time}</span>
        </div>
        <div class="incident-summary">${escapeHtml(inc.summary)}</div>
        <div class="incident-cause">${escapeHtml(inc.root_cause || "")} <span class="confidence">(${Math.round((inc.confidence || 0) * 100)}% confidence)</span></div>
        <div class="incident-actions">${actions.join("")}</div>
        <div class="evidence${evidenceOpen}" id="evidence-${inc.id}">recommended action: ${escapeHtml(inc.recommended_action || "none")}</div>
      </div>`;
    })
    .join("");
}

function renderTraces(list) {
  const el = document.getElementById("trace-list");
  if (!list.length) {
    el.innerHTML = '<div class="empty">No traces yet — send a test request.</div>';
    return;
  }
  el.innerHTML = list
    .map(t => {
      const time = new Date(t.start_time * 1000).toLocaleTimeString();
      const backend = t.attributes.backend || "?";
      const fault = t.attributes.fault_mode || "normal";
      const stages = (t.stages || []).slice().sort((a, b) => a.start_time - b.start_time);
      const totalDur = Math.max(1, t.duration_ms);
      const waterfallRows = stages
        .map(s => {
          const offsetPct = Math.max(0, (((s.start_time - t.start_time) * 1000) / totalDur) * 100);
          const widthPct = Math.max(1, (s.duration_ms / totalDur) * 100);
          return `<div class="waterfall-row">
          <span>${s.name}</span>
          <span class="waterfall-bar-track"><span class="waterfall-bar ${s.status}" style="left:${offsetPct}%;width:${widthPct}%"></span></span>
          <span>${Math.round(s.duration_ms)}ms</span>
        </div>`;
        })
        .join("");
      const waterfallOpen = expanded.traces.has(t.span_id) ? " open" : "";
      return `<div class="trace-row" data-toggle-trace="${t.span_id}">
        <div class="trace-row-top">
          <span class="tid">${t.trace_id.slice(0, 10)}… @ ${time}</span>
          <span>backend=${backend} fault=${fault}</span>
          <span class="status ${t.status}">${t.status} · ${Math.round(t.duration_ms)}ms</span>
        </div>
        <div class="waterfall${waterfallOpen}" id="waterfall-${t.span_id}">${waterfallRows}</div>
      </div>`;
    })
    .join("");
}

async function refresh() {
  try {
    const [health, metrics, incidents, traces, faultState] = await Promise.all([
      fetchJSON("/api/health"),
      fetchJSON("/api/metrics?window=300"),
      fetchJSON("/api/incidents"),
      fetchJSON("/api/traces?limit=15"),
      fetchJSON("/api/fault-state"),
    ]);
    renderHealth(health);
    renderMetrics(metrics);
    renderIncidents(incidents);
    renderTraces(traces);
    renderControls(faultState);
  } catch (err) {
    console.error("refresh failed", err);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  refresh();
  setInterval(refresh, 3000);

  document.addEventListener("click", async e => {
    const t = e.target;
    if (t.matches("[data-fault]")) {
      await postJSON("/api/fault", { mode: t.dataset.fault });
      refresh();
    } else if (t.matches("[data-backend]")) {
      await postJSON("/api/backend", { backend: t.dataset.backend });
      refresh();
    } else if (t.matches("[data-incident]")) {
      await postJSON(`/api/incidents/${t.dataset.incident}/action`, { action: t.dataset.action });
      refresh();
    } else if (t.matches("[data-toggle-evidence]")) {
      const id = t.dataset.toggleEvidence;
      expanded.evidence.has(id) ? expanded.evidence.delete(id) : expanded.evidence.add(id);
      document.getElementById(`evidence-${id}`).classList.toggle("open");
    } else if (t.closest("[data-toggle-trace]")) {
      const id = t.closest("[data-toggle-trace]").dataset.toggleTrace;
      expanded.traces.has(id) ? expanded.traces.delete(id) : expanded.traces.add(id);
      document.getElementById(`waterfall-${id}`).classList.toggle("open");
    } else if (t.id === "send-traffic-btn") {
      t.disabled = true;
      t.textContent = "Sending…";
      await postJSON("/api/send-traffic", { count: 5 });
      t.disabled = false;
      t.textContent = "Send 5 test requests";
      refresh();
    }
  });
});
