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
  { key: "avg_cost_usd", label: "Avg cost/req", fmt: v => "$" + v.toFixed(4) },
  { key: "total_cost_usd", label: "Total cost (window)", fmt: v => "$" + v.toFixed(4) },
];

const METRIC_FMT = {
  p95_ms: v => Math.round(v) + "ms",
  error_rate: v => (v * 100).toFixed(0) + "%",
  timeout_rate: v => (v * 100).toFixed(0) + "%",
  invalid_output_rate: v => (v * 100).toFixed(0) + "%",
  avg_tokens: v => Math.round(v),
};
function fmtMetric(metric, value) {
  const fn = METRIC_FMT[metric];
  return fn ? fn(value ?? 0) : String(Math.round((value ?? 0) * 100) / 100);
}

function fmtDuration(s) {
  if (s == null) return "—";
  if (s < 60) return Math.round(s) + "s";
  if (s < 3600) return Math.round(s / 60) + "m";
  return (s / 3600).toFixed(1) + "h";
}

const STAGE_ORDER = ["auth", "retrieval", "llm_call", "tool_call"];

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

function renderReliability(data) {
  const el = document.getElementById("reliability-grid");
  const r = data.requests, s = data.synthetic, inc = data.incidents;
  // No sparklines here on purpose -- these are 24h rolling aggregates, so poll-to-poll they
  // barely move, and a "trend" line built from that would just be misleading noise.
  const tiles = [
    { label: "Availability (24h)", value: (r.availability * 100).toFixed(1) + "%" },
    { label: "Checks passed", value: `${s.passed} / ${s.count}` },
    { label: "Incidents", value: String(inc.incident_count) },
    { label: "Verified recoveries", value: String(inc.verified_count) },
    { label: "Rolled-back actions", value: String(inc.rolled_back_count) },
    { label: "Median recovery time", value: fmtDuration(inc.median_recovery_s) },
  ];
  el.innerHTML = tiles
    .map(t => `<div class="tile">
      <div class="label">${escapeHtml(t.label)}</div>
      <div class="value">${escapeHtml(t.value)}</div>
    </div>`)
    .join("");
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

function renderCanarySummary(canaryResultJson) {
  if (!canaryResultJson) return "";
  let result;
  try {
    result = JSON.parse(canaryResultJson);
  } catch {
    return "";
  }
  const { current, candidate } = result;
  if (!current || !candidate) return "";
  const fmtSide = s =>
    `${escapeHtml(s.backend)}: ${s.avg_latency_ms != null ? Math.round(s.avg_latency_ms) + "ms" : "n/a"} avg, ${Math.round((s.error_rate || 0) * 100)}% err`;
  const better = (candidate.avg_latency_ms ?? Infinity) < (current.avg_latency_ms ?? Infinity)
    && candidate.error_rate <= current.error_rate;
  return `<div class="incident-canary${better ? " favorable" : ""}">
    Expected: ${fmtSide(current)} → ${fmtSide(candidate)} (${better ? "backup looks better" : "no clear improvement"})
  </div>`;
}

function _evidenceRows(inc, ev) {
  // The per-stage comparison path (latency_spike / error_rate_spike / timeout_spike, and the
  // inconclusive fallback) is the only branch with a real baseline-vs-incident number for every
  // stage -- the short-circuit branches below (tool_failure_rate, cost_spike,
  // invalid_output_rate) attribute directly without needing that comparison, so they only ever
  // have a single relevant number to show, not a 4-stage table.
  if (ev.recent && ev.baseline && STAGE_ORDER.some(s => ev.recent[s])) {
    const isLatency = inc.detector === "latency_spike";
    return STAGE_ORDER
      .filter(stage => ev.recent[stage] && ev.recent[stage].count >= 1)
      .map(stage => {
        const r = ev.recent[stage], b = ev.baseline[stage] || {};
        const rVal = isLatency ? (r.p95_ms || 0) : (r.error_rate || 0) * 100;
        const bVal = isLatency ? (b.p95_ms || 0) : (b.error_rate || 0) * 100;
        const fmt = v => (isLatency ? Math.round(v) + "ms" : v.toFixed(0) + "%");
        const delta = bVal > 0 ? Math.round(((rVal - bVal) / bVal) * 100) : (rVal > 0 ? null : 0);
        return {
          label: stage, baseline: fmt(bVal), incident: fmt(rVal),
          delta: delta === null ? "n/a" : (delta >= 0 ? "+" : "") + delta + "%",
          hot: delta !== null && delta >= 50,
        };
      });
  }

  if (ev.recent && ev.recent.error_rate !== undefined && !ev.baseline) {
    const r = ev.recent;
    return [{
      label: "tool_call", baseline: "—",
      incident: `${Math.round(r.error_rate * 100)}% error rate`, delta: `${r.count} calls/90s`, hot: true,
    }];
  }

  if (ev.anomaly_evidence) {
    const a = ev.anomaly_evidence;
    if (a.recent && a.baseline && a.recent.avg_tokens !== undefined) {
      return [{
        label: "llm_call (tokens/req)", baseline: Math.round(a.baseline.avg_tokens),
        incident: Math.round(a.recent.avg_tokens), delta: `${(a.ratio || 0).toFixed(1)}x`, hot: true,
      }];
    }
    if (a.recent && a.recent.invalid_output_rate !== undefined) {
      return [{
        label: "llm_call (output)", baseline: "—",
        incident: `${Math.round(a.recent.invalid_output_rate * 100)}% invalid`,
        delta: `${a.recent.count} samples`, hot: true,
      }];
    }
  }

  return [];
}

function renderEvidence(inc) {
  if (!inc.evidence) return "";
  let ev;
  try {
    ev = JSON.parse(inc.evidence);
  } catch {
    return "";
  }
  const rows = _evidenceRows(inc, ev);
  if (!rows.length) return "";

  const rowsHtml = rows
    .map(row => `<div class="evidence-row${row.hot ? " evidence-hot" : ""}">
      <span>${escapeHtml(String(row.label))}</span><span>${escapeHtml(String(row.baseline))}</span>
      <span>${escapeHtml(String(row.incident))}</span><span>${escapeHtml(String(row.delta))}</span>
    </div>`)
    .join("");
  return `<div class="evidence-table">
    <div class="evidence-row evidence-header"><span>stage</span><span>baseline</span><span>incident</span><span>delta</span></div>
    ${rowsHtml}
  </div>`;
}

function _timelineEvents(inc, regressions) {
  // Everything at inc.ts happened inside one synchronous sweep (detect -> diagnose -> recommend
  // -> canary), so it's one event, not several suspiciously-identical timestamps. Only events
  // this system actually times separately show up here -- no invented sub-second ordering.
  const events = [{
    ts: inc.ts,
    label: `detected ${inc.detector}${inc.stage ? `, diagnosed ${inc.stage}` : ""}`
      + (inc.canary_result ? " (canary compared backends)" : ""),
  }];

  const run = inc.remediation_run;
  if (run) {
    events.push({ ts: run.started_at, label: `you approved "${_actionLabel(run.action)}"` });
    if (run.finished_at) {
      const verdict = run.outcome === "verified" ? "verified — recovered"
        : run.outcome === "rolled_back" ? "rolled back — didn't help" : run.outcome;
      const before = fmtMetric(run.metric, run.before_value);
      const after = fmtMetric(run.metric, run.after_value != null ? run.after_value : run.before_value);
      events.push({ ts: run.finished_at, label: `${verdict} (${run.metric}: ${before} → ${after})` });
    }
  }

  const regression = (regressions || []).find(r => r.source_incident_id === inc.id);
  if (regression) {
    events.push({ ts: regression.created_at, label: "regression saved for future replay" });
  }

  if (inc.status === "ignored" && inc.resolved_ts) {
    events.push({ ts: inc.resolved_ts, label: "ignored" });
  }

  return events.sort((a, b) => a.ts - b.ts);
}

function renderTimeline(inc, regressions) {
  const events = _timelineEvents(inc, regressions);
  if (events.length < 2) return ""; // "detected" alone isn't a timeline worth showing yet
  const rows = events
    .map(e => `<div class="timeline-row">
      <span class="timeline-time">${new Date(e.ts * 1000).toLocaleTimeString()}</span>
      <span>${escapeHtml(e.label)}</span>
    </div>`)
    .join("");
  return `<div class="timeline">${rows}</div>`;
}

function renderIncidents(list, regressions) {
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
      } else if (inc.status === "verifying") {
        actions.push(`<span class="incident-status-label verifying">verifying…</span>`);
      } else {
        actions.push(`<span class="incident-status-label">${inc.status}</span>`);
        actions.push(`<button class="ghost" data-toggle-evidence="${inc.id}">Details</button>`);
      }

      let verificationHtml = "";
      const run = inc.remediation_run;
      if (run) {
        if (inc.status === "verifying") {
          verificationHtml = `<div class="verification pending">
            <span class="spinner"></span>
            <span>Executed "${escapeHtml(run.action)}" — sending test traffic and comparing ${escapeHtml(run.metric)}…</span>
          </div>`;
        } else if (inc.status === "verified" || inc.status === "rolled_back") {
          const before = fmtMetric(run.metric, run.before_value);
          const after = fmtMetric(run.metric, run.after_value != null ? run.after_value : run.before_value);
          const verdict = inc.status === "verified"
            ? `<span class="verdict ok">VERIFIED RECOVERY</span>`
            : `<span class="verdict bad">DIDN'T HELP — ROLLED BACK</span>`;
          verificationHtml = `<div class="verification ${inc.status}">
            ${verdict}
            <span class="metric-compare">${escapeHtml(run.metric)}: <strong>${before}</strong> → <strong>${after}</strong></span>
            <div class="verification-detail">${escapeHtml(run.detail || "")}</div>
          </div>`;
        }
      }

      const signals = (inc.merged_detectors || "").split(",").filter(Boolean);
      const signalsHtml = signals.length > 1
        ? `<div class="incident-signals">Signals: ${signals.map(escapeHtml).join(", ")}</div>`
        : "";

      const canaryHtml = renderCanarySummary(inc.canary_result);

      const evidenceOpen = expanded.evidence.has(String(inc.id)) ? " open" : "";
      const evidenceTableHtml = renderEvidence(inc);
      const timelineHtml = renderTimeline(inc, regressions);
      return `
      <div class="incident-card ${inc.severity} ${inc.status}">
        <div class="incident-top">
          <span class="sev ${inc.severity}">${inc.severity} · ${inc.detector}</span>
          <span class="time">${time}</span>
        </div>
        <div class="incident-summary">${escapeHtml(inc.summary)}</div>
        <div class="incident-cause">${escapeHtml(inc.root_cause || "")} <span class="confidence">(${Math.round((inc.confidence || 0) * 100)}% confidence)</span></div>
        ${signalsHtml}
        ${canaryHtml}
        <div class="incident-actions">${actions.join("")}</div>
        ${verificationHtml}
        <div class="evidence${evidenceOpen}" id="evidence-${inc.id}">
          <div class="evidence-action">recommended action: ${escapeHtml(inc.recommended_action || "none")}</div>
          ${evidenceTableHtml}
          ${timelineHtml}
        </div>
      </div>`;
    })
    .join("");
}

function renderRegressions(list) {
  const el = document.getElementById("regression-list");
  const passed = list.filter(r => r.last_run_passed === 1).length;
  const ran = list.filter(r => r.last_run_passed !== null && r.last_run_passed !== undefined).length;
  document.getElementById("regression-summary").textContent = ran ? `${passed} / ${ran} passing` : "";

  if (!list.length) {
    el.innerHTML = '<div class="empty">No regressions recorded yet — a remediation has to verify successfully first.</div>';
    return;
  }
  el.innerHTML = list
    .map(r => {
      const badgeClass = r.last_run_passed === null || r.last_run_passed === undefined
        ? "pending" : r.last_run_passed ? "ok" : "bad";
      const badgeText = r.last_run_passed === null || r.last_run_passed === undefined
        ? "never run" : r.last_run_passed ? "passing" : "failing";
      return `<div class="regression-card">
        <div class="incident-top">
          <span class="sev">${escapeHtml(r.detector)}${r.stage ? " · " + escapeHtml(r.stage) : ""}</span>
          <span class="regression-status"><span class="badge ${badgeClass}"></span>${badgeText}</span>
        </div>
        <div class="incident-summary">${escapeHtml(r.summary || "")}</div>
        <div class="incident-cause">fix: ${escapeHtml(r.action)} · fault: ${escapeHtml(r.fault_mode || "unknown")}</div>
        ${r.last_run_detail ? `<div class="regression-detail">${escapeHtml(r.last_run_detail)}</div>` : ""}
      </div>`;
    })
    .join("");
}

function renderBlindEvalRuns(runs) {
  const el = document.getElementById("blind-eval-list");
  const summaryEl = document.getElementById("blind-eval-summary");

  if (!runs.length) {
    summaryEl.textContent = "";
    el.innerHTML = '<div class="empty">No blind eval runs yet.</div>';
    return;
  }

  const latest = runs[0];
  summaryEl.textContent = `${Math.round(latest.accuracy * 100)}% correct (latest, ${latest.trial_count} trials)`;

  el.innerHTML = runs
    .map(run => {
      const time = new Date(run.ts * 1000).toLocaleString();
      const trialRows = run.trials
        .map(t => {
          const cls = t.outcome === "correct" ? "ok" : t.outcome === "inconclusive" ? "warn" : "bad";
          const diagnosed = t.diagnosed_stage || "(inconclusive)";
          return `<div class="blind-eval-trial">
            <span class="badge ${cls}"></span>
            <span class="trial-fault">${escapeHtml(t.fault_mode)}</span>
            <span class="trial-arrow">→</span>
            <span>expected ${escapeHtml(t.expected_stage)}, diagnosed ${escapeHtml(diagnosed)}</span>
          </div>`;
        })
        .join("");
      return `<div class="regression-card">
        <div class="incident-top">
          <span class="sev">${time}</span>
          <span class="regression-status">${run.counts.correct}/${run.trial_count} correct (${Math.round(run.accuracy * 100)}%)</span>
        </div>
        ${trialRows}
      </div>`;
    })
    .join("");
}

function _actionLabel(code) {
  const entry = Object.entries(ACTION_LABELS).find(([, c]) => c === code);
  return entry ? entry[0] : code;
}

function renderAuditTrail(runs) {
  const el = document.getElementById("audit-list");
  if (!runs.length) {
    el.innerHTML = '<div class="empty">No remediations run yet.</div>';
    return;
  }

  el.innerHTML = runs
    .map(run => {
      const time = new Date(run.started_at * 1000).toLocaleString();
      const outcomeClass = run.outcome === "verified" ? "ok" : run.outcome === "rolled_back" ? "bad" : "pending";
      const before = fmtMetric(run.metric, run.before_value);
      const after = fmtMetric(run.metric, run.after_value != null ? run.after_value : run.before_value);
      return `<div class="audit-card">
        <div class="incident-top">
          <span class="sev">${escapeHtml(run.incident_detector)}${run.incident_stage ? " · " + escapeHtml(run.incident_stage) : ""}</span>
          <span class="time">${time}</span>
        </div>
        <div class="incident-summary">${escapeHtml(run.incident_summary || "")}</div>
        <div class="audit-action">
          action: <strong>${escapeHtml(_actionLabel(run.action))}</strong>
          <span class="audit-initiator">· initiated by: you, via the dashboard</span>
        </div>
        <div class="audit-row">
          <span class="badge ${outcomeClass}"></span>
          <span class="metric-compare">${escapeHtml(run.metric)}: <strong>${before}</strong> → <strong>${after}</strong></span>
          <span class="audit-outcome-label">${escapeHtml(run.outcome)}</span>
        </div>
        ${run.detail ? `<div class="verification-detail">${escapeHtml(run.detail)}</div>` : ""}
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
    const [health, metrics, reliability, incidents, traces, faultState, regressions, blindEvalRuns, auditTrail] = await Promise.all([
      fetchJSON("/api/health"),
      fetchJSON("/api/metrics?window=300"),
      fetchJSON("/api/reliability"),
      fetchJSON("/api/incidents"),
      fetchJSON("/api/traces?limit=15"),
      fetchJSON("/api/fault-state"),
      fetchJSON("/api/regressions"),
      fetchJSON("/api/blind-eval/runs"),
      fetchJSON("/api/remediation-runs"),
    ]);
    renderHealth(health);
    renderMetrics(metrics);
    renderReliability(reliability);
    renderIncidents(incidents, regressions);
    renderTraces(traces);
    renderControls(faultState);
    renderRegressions(regressions);
    renderBlindEvalRuns(blindEvalRuns);
    renderAuditTrail(auditTrail);
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
    } else if (t.id === "run-regressions-btn") {
      t.disabled = true;
      t.textContent = "Running…";
      try {
        await postJSON("/api/regressions/run", {});
      } finally {
        t.disabled = false;
        t.textContent = "Run regression suite";
        refresh();
      }
    } else if (t.id === "run-blind-eval-btn") {
      t.disabled = true;
      const original = t.textContent;
      t.textContent = "Running blind eval… (~10 min: a 95s warm-up, then each fault trial spaced apart on purpose)";
      try {
        await postJSON("/api/blind-eval/run", {});
      } catch (err) {
        console.error("blind eval failed", err);
      } finally {
        t.disabled = false;
        t.textContent = original;
        refresh();
      }
    }
  });
});
