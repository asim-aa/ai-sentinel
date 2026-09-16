# AI Sentinel — architecture

The rendered version of this document, with diagrams, is published at:
https://claude.ai/artifact/6CK9V5RV3fi4WhjNauvZnG

This file is the text-only companion — same six sections, same specifics, no pictures.

## 1. System architecture

Two Python processes, no message broker, no collector. The demo AI service only ever talks to
SQLite (it writes spans and never reads them back); your browser only ever talks to the engine,
which proxies every control call through to the demo service on your behalf.

- **Demo AI service** (`demo_service/main.py`, `:8000`) — `/health/live`, `/health/ready`,
  `POST /chat`, `POST /admin/{fault,backend,retrieval,tools}`.
- **Sentinel engine** (`ai_sentinel/dashboard/server.py`, `:8500`) — runs the synthetic checker
  and detector sweep as background tasks, and serves the dashboard's API + static UI.
- **`sentinel.db`** (SQLite, WAL mode) — tables `spans`, `synthetic_checks`, `incidents`. Shared
  path via `SENTINEL_DB_PATH` (default `./sentinel.db`). WAL + `busy_timeout=5000` is what makes
  two separate processes safely sharing one file work.

## 2. Health-check hierarchy

"Is it healthy?" is three separate questions of increasing cost and certainty:

| Check | Asks | Where | Cadence |
|---|---|---|---|
| Liveness | Is the process alive? | `demo_service/main.py::liveness` | polled every 3s by the dashboard |
| Readiness | Can it serve traffic right now? | `demo_service/main.py::_readiness_check` — last 30s of `llm_call` spans, ready unless error rate > 50% with ≥3 samples | polled every 3s |
| Synthetic | Does it behave correctly end to end? | `ai_sentinel/synthetic.py::run_once` — sends `HEALTH_PROBE_PROMPT` ("Return exactly the word HEALTHY") through the full `/chat` pipeline | its own loop, every 60s, independent of whether a browser is open |

Only the synthetic check exercises a real model through the real pipeline.

## 3. Request trace lifecycle

Every `/chat` call opens one root span (`chat_request`) and four nested child spans: `auth`,
`retrieval`, `llm_call`, `tool_call` — implemented in `demo_service/pipeline.py::run_pipeline`.

- Root attributes: `backend`, `fault_mode`, `invalid_output`, `tokens_total`
- `llm_call` attributes: `backend`, `tokens_in`, `tokens_out`
- `ai_sentinel/storage.py::recent_traces` joins root + children by `trace_id` for the dashboard's
  waterfall view.

In steady state the LLM call is ~85-90% of total request time — which is why both the detectors
and the root-cause correlator treat it as the default suspect.

## 4. Failure-detection pipeline

Six threshold rules, each comparing a **recent** window (last 90s) against a **baseline** window
(90–990s ago — ending exactly where "recent" begins, so a real regression doesn't get diluted by
mixing into its own baseline):

- `latency_spike` — p95 ratio ≥2.0 warn / ≥3.0 critical
- `error_rate_spike` — error rate ≥0.2 / ≥0.5
- `timeout_spike` — timeout rate ≥0.2 / ≥0.5
- `invalid_output_rate` — from live traffic or the synthetic probe's pass/fail history
- `cost_spike` — avg tokens/request ratio ≥1.5
- `tool_failure_rate` — checked directly against the `tool_call` stage, since tool failures are
  non-fatal (the request still returns an answer) and never show up as a `chat_request`-level error

All six live in `ai_sentinel/detectors.py` (`RECENT_WINDOW_S=90`, `BASELINE_WINDOW_S=900`,
`MIN_SAMPLES=3`). A cooldown (`storage.py::open_incident_for_detector`, 180s) stops one sustained
problem from creating a new incident every 15-second sweep.

On a fresh anomaly, `ai_sentinel/rootcause.py::diagnose` breaks spans down by stage and picks
whichever one deviates most from its own baseline (with a `MIN_MEANINGFUL_LATENCY_MS=50` floor so
noise on a fast stage like `auth` never wins over a genuinely slow one). `cost_spike` and
`tool_failure_rate` skip that comparison and short-circuit directly to `llm_call` / `tool_call`
respectively, since only those stages could plausibly cause them.

**A known limitation, by design, not a bug:** if a fault persists long enough (several minutes),
it eventually ages out of the "recent" window and into "baseline" — at which point the ratio
drops below threshold and the detector stops flagging it, because the fault has effectively
become part of its own baseline. Real systems hit the same failure mode with naive rolling
baselines; a production version would exclude periods with open incidents from baseline
computation. Not fixed here — it's a fair MVP trade-off, and worth knowing if a fault you inject
stops showing up in the incident feed after a few minutes.

## 5. Remediation / failover flow

Every incident carries a recommended action; nothing executes until you click it.

`ai_sentinel/remediation.py::recommend` maps the diagnosed stage to an action:
`llm_call → fail_over`, `retrieval → disable_retrieval`, `tool_call → disable_tools`.

`execute()` doesn't hardcode a target — for `fail_over` it reads the demo service's current
`active_backend` via `GET /admin/state` and flips to whichever one isn't currently active, then
calls `POST /admin/backend`. The dashboard's `POST /api/incidents/{id}/action` is the trigger;
the incident is marked `remediated` in the same request.

## 6. Dashboard data flow

The browser is a pure viewer. `ai_sentinel/dashboard/static/app.js::refresh` runs
`setInterval(refresh, 3000)`, fetching five endpoints in parallel
(`/api/health`, `/api/metrics`, `/api/traces`, `/api/incidents`, `/api/fault-state`) and
re-rendering. Sparklines are hand-rolled inline SVG — no chart library, no CDN dependency.

Separately, `ai_sentinel/dashboard/server.py`'s FastAPI `lifespan` starts the synthetic-check
loop and the detector-sweep loop as `asyncio` background tasks on process boot. They run for the
life of the process, not the life of a browser connection — closing the dashboard tab doesn't
pause detection.
