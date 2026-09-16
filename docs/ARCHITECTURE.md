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

**A limitation found during live testing, since fixed:** if a fault persists long enough (several
minutes), it eventually ages out of the "recent" window and into "baseline" — at which point the
ratio drops below threshold and a naive detector would stop flagging it, because the fault has
effectively become part of its own baseline. Real systems hit the same failure mode with naive
rolling baselines. The fix: `storage.py::excluded_periods` looks up any open or recently-resolved
incident for that same detector and excludes its estimated time range (from `RECENT_WINDOW_S`
before it was created through its resolution, or now if still open) from the baseline query —
both the aggregate one in `detectors.py` and the per-stage one in `rootcause.py`. It's a
self-referential design (the system excludes its own detected anomalies from its own baseline),
covered by regression tests in both `tests/test_detectors.py` and `tests/test_rootcause.py` that
seed a polluted baseline and assert detection fails without an incident row and succeeds with one.

Every incident also goes through `ai_sentinel/alerts.py::emit_alert`, which always logs a
structured line and, if configured, delivers to Slack (`SLACK_WEBHOOK_URL` — a proper Block Kit
message with a severity-colored bar matching the dashboard's own palette) and/or a generic
webhook (`ALERT_WEBHOOK_URL` — flat JSON). `ALERT_MIN_SEVERITY` filters which severities get
delivered (logging always happens regardless). The two delivery paths are independent — one
failing doesn't block the other — verified both with mocked-HTTP unit tests
(`tests/test_alerts.py`) and a real local HTTP receiver that the actual `emit_alert()` code path
was pointed at during development.

## 5. Remediation / failover flow

Every incident carries a recommended action; nothing executes until you click it.

`ai_sentinel/remediation.py::recommend` maps the diagnosed stage to an action:
`llm_call → fail_over`, `retrieval → disable_retrieval`, `tool_call → disable_tools`.

`execute()` doesn't hardcode a target — for `fail_over` it reads the demo service's current
`active_backend` via `GET /admin/state` and flips to whichever one isn't currently active, then
calls `POST /admin/backend`. The dashboard's `POST /api/incidents/{id}/action` is the trigger;
the incident is marked `remediated` in the same request.

What "backup" actually *is* depends on what's configured: `demo_service/llm_client.py::make_backends`
picks providers by availability — with both `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` set, primary
is Claude (`claude-haiku-4-5`) and backup is OpenAI (`gpt-5.6-luna`), so a fail-over is a genuine
cross-provider switch, not a same-model toggle. With only one key (or neither), both point at
whatever's available. The priority list is a simple `(env var, factory)` sequence — a third
provider is one more entry, not a restructure.

## 6. Dashboard data flow

The browser is a pure viewer. `ai_sentinel/dashboard/static/app.js::refresh` runs
`setInterval(refresh, 3000)`, fetching five endpoints in parallel
(`/api/health`, `/api/metrics`, `/api/traces`, `/api/incidents`, `/api/fault-state`) and
re-rendering. Sparklines are hand-rolled inline SVG — no chart library, no CDN dependency.

Separately, `ai_sentinel/dashboard/server.py`'s FastAPI `lifespan` starts the synthetic-check
loop and the detector-sweep loop as `asyncio` background tasks on process boot. They run for the
life of the process, not the life of a browser connection — closing the dashboard tab doesn't
pause detection.
