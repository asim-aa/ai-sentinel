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
calls `POST /admin/backend`. The dashboard's `POST /api/incidents/{id}/action` is the trigger; the
incident moves to `verifying`, then `verified` or `rolled_back` once `ai_sentinel/verification.py`
compares the incident's own metric before vs. after and, if it didn't actually improve, calls
`remediation.rollback()` automatically rather than leaving a false "fixed" on the board.

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

## 7. Correlation, cost, deploy-awareness, and canaries

Four additions to `engine.py::sweep_once`, all still informational or merge-only — none of them
make a new kind of decision on their own, they make the existing decisions better-informed:

- **Cost.** `pipeline.py` tags every `llm_call` span with `model` and `cost_usd`
  (`ai_sentinel/pricing.py`, a verified $/token table per provider). `storage.py::metrics_summary`
  rolls these into `avg_cost_usd`/`total_cost_usd`, surfaced as dashboard tiles.
- **Deployment correlation.** `version.py` resolves `SERVICE_VERSION` (a `VERSION` file at deploy
  time, else a live `git rev-parse`) and `SERVICE_STARTED_AT` once at import time; both are tagged
  onto the root `chat_request` span. `rootcause.py::_deployment_note` checks the most recent span
  against that start time and appends a one-line note to the diagnosis when the restart was
  within the last two minutes — the same explanation string every other detector path already
  returns, just with one more sentence when it's relevant.
- **Incident correlation.** Before creating a new incident, `sweep_once` checks
  `storage.open_incident_for_stage` for an already-open incident diagnosed to the *same* stage
  within the last 60s. If found, the new detector's name is appended to that incident's
  `merged_detectors` (`storage.merge_detector_into_incident`) instead of opening a second card for
  what's really one problem.
- **Canary.** Only for a *brand-new* incident whose recommended action is `fail_over`:
  `canary.py::compare_backends` flips the demo service to the current backend, sends 3 probes,
  flips to the other backend, sends 3 more, then restores whichever was active originally. The
  result (`{current, candidate}` avg latency + error rate) is stored as `canary_result` on the
  incident and rendered as an `Expected: ...` line on the card. This never runs on a merge, and it
  never flips the backend for longer than the probe itself takes — it's a comparison, not a
  commitment, matching the "co-pilot, not autopilot" rule everywhere else in this system.

## 8. Regression memory

The loop up to here ends at `verified` or `rolled_back` (§5) and stops — nothing about *why* a fix
worked outlives the incident. `ai_sentinel/regression.py` closes that gap: the moment
`verification.py` marks an incident `verified`, `regression.record_regression` captures its
failure signature and the fix that resolved it as a row in a new `regressions` table, keyed by
`(detector, stage)` — a later verified fix for the same signature **replaces** the earlier row
(`storage.upsert_regression`, an `ON CONFLICT ... DO UPDATE`), so the corpus reflects the current
best-known fix rather than growing forever on a long-running deployment. `fault_mode` is inferred
after the fact, not passed in explicitly: `_infer_fault_mode` reads the `fault_mode` attribute off
whichever span sits closest to the incident's own timestamp.

**Replay** (`regression.replay_regression`, triggered from the dashboard's "Run regression suite"
button → `POST /api/regressions/run` → `run_regression_suite`, which iterates every stored row) is
a fast, deterministic re-run of just the fault → fix → verify half of the loop — not full live
re-detection, which needs the detectors' rolling baseline to age in naturally over minutes and
would make a "run the suite" button impractical. For each regression: set the recorded
`fault_mode`, send probes, measure "before"; execute the recorded `action`; send probes, measure
"after"; run the *same* `measure()`/`decide()` pair live verification uses, so a stored fix is
judged exactly the way a fresh one would be. The demo service's original state (fault mode,
backend, retrieval/tools toggles) is always restored in a `finally`, even if the fix throws.

One measurement detail worth calling out because it broke on the first live run: unlike live
verification, replay has **no pre-existing organic history** to lean on for either window — both
"before" and "after" are probes sent back-to-back inside the same call. Reusing live verification's
shared `max(5.0, elapsed)` window floor let the "after" window's minimum reach backward into the
still-fresh "before" probes, silently re-including pre-fix latency in the post-fix reading (a fix
that had genuinely worked live came back `failing` on replay). Each phase now gets its own tightly
scoped window (`max(0.5, elapsed_since_that_phase_started)`) instead of sharing one floor — the
kind of bug that only shows up once you actually run the thing, not by reading the diff.

## 9. Blind fault-injection eval

Regression replay (§8) checks "does a *known* fix still work" — it never questions whether the
original diagnosis was right. `ai_sentinel/blind_eval.py` checks that directly: pick a fault at
random, inject it, and see whether detection + diagnosis name the right pipeline stage — without
either of them ever being told which fault was chosen. `FAULT_EXPECTATIONS` maps each of the five
injectable fault modes to the specific detector function expected to notice it and the stage
`rootcause.diagnose()` should name once it does (`slow_llm`/`llm_errors` → `llm_call`,
`vector_db_slow` → `retrieval`, `tool_failure` → `tool_call`, `malformed_output` → `llm_call`).
Each trial scores one of four outcomes — `correct`, `wrong_stage`, `inconclusive` (detector fired,
diagnosis couldn't attribute a stage), or `not_detected` (detector never fired at all) — persisted
per-run to a new `blind_eval_runs` table (one row per full batch, trials as a JSON list, same
pattern `canary_result` already uses for structured-data-in-a-column).

`malformed_output` stayed in the fault pool from the start even though it initially scored
`inconclusive` on every trial: it corrupts response text without marking any span `ERROR`, so the
generic per-stage error-rate comparison in `rootcause.py`'s `diagnose()` (§4) had no per-stage
signal to compare — excluding it would have inflated the reported accuracy past what the system
actually did. That's exactly how the eval did its job: it made the gap visible and quantified
(`4/5`, not a vague "mostly works") instead of letting it stay invisible. `rootcause.py` now
short-circuits `invalid_output_rate` straight to `stage="llm_call"`, mirroring the existing
`tool_failure_rate`/`cost_spike` branches — only the LLM call stage ever produces the response
text, so the same "no real per-stage attribution question to answer" reasoning applies.
Live-verified: a real `invalid_output_rate` incident now attributes to `llm_call` with 85%
confidence and recommends fail-over, the same as any other LLM-call issue would — and a full
blind-eval re-run scored `5/5`, confirming the fix closes the gap for good, not just for one
hand-picked case.

**Trial isolation is the whole design problem here**, and the first live run caught it the hard
way: `detectors.py`'s recent/baseline windows are fixed, global rolling windows over all
`chat_request` traffic, not scoped to any one trial. Running trials back-to-back left one trial's
traffic still inside the *next* trial's 90-second "recent" window — a completely unrelated,
non-error request mixed into the denominator dilutes a genuine error-rate spike below the
significance threshold. Concretely: a live `llm_errors` trial that should have shown a clean
~70% recent error rate came back diluted to ~22% by two earlier trials' clean traffic still
sitting in the same window, just under the `top_dev <= 1.3` significance floor in `rootcause.py`
→ scored `inconclusive` instead of `correct`, twice in a row.

The fix is **not** a narrower or eval-specific detection window — building one would mean testing
a different code path than the one real incidents actually go through, defeating the eval's
purpose. Instead, `run_blind_eval` spaces trials apart by `INTER_TRIAL_GAP_S`
(`RECENT_WINDOW_S + 10` ≈ 100s) so each trial's own traffic has fully aged out of the *next*
trial's recent window before that one starts, and picks fault modes from a shuffled full pass
(`_shuffled_fault_cycle`) rather than uniform random-with-replacement, so the (now expensive) 5
default trials guarantee coverage of every fault type instead of risking wasted repeats. End to
end this costs a genuine ~10 minutes (a 95s warm-up plus 4 gaps at ~100s each) — slow on purpose,
in exchange for testing the exact same windowed detection a real incident goes through.

## 10. RCA evidence panel

`RootCause.evidence` (§4) was always computed at diagnosis time but never persisted — `engine.py`
only wrote `cause.explanation`, `cause.confidence`, and `cause.stage` into the `incidents` table,
so the actual numbers behind a diagnosis existed for exactly as long as the sweep that produced
them. `storage.create_incident` now takes an `evidence` param (JSON-serialized into a new
`evidence` column, same migration treatment as every other incident column added after the table
already existed on a deployed database — see `_INCIDENT_MIGRATIONS`), and `sweep_once` passes
`cause.evidence` straight through. Clicking **Investigate**/**Details** on an incident card now
renders it as a table, not just a "recommended action" line.

The evidence shape isn't uniform across detectors, and the frontend (`app.js::_evidenceRows`)
branches on what's actually there rather than assuming one shape:

- **Per-stage comparison** (`latency_spike`/`error_rate_spike`/`timeout_spike`, and the
  inconclusive fallback) — `evidence.recent`/`evidence.baseline` are keyed by all four pipeline
  stages, so this renders the full stage / baseline / incident / delta table, using whichever
  metric (`p95_ms` vs `error_rate`) the detector actually compared, and flags the dominant stage's
  row as the outlier.
- **`tool_failure_rate`** — a single-stage `recent` dict with no baseline at all (the detector
  never compares against history, just checks the current tool_call error rate) — renders as one
  row with a `—` in the baseline column rather than a fabricated comparison.
- **`cost_spike` / `invalid_output_rate`** — `evidence.anomaly_evidence` wraps chat-request-level
  (not per-stage) `recent`/`baseline` dicts, since both detectors attribute directly to `llm_call`
  without a real per-stage question to answer (§7, §9) — renders as one row keyed to whichever
  field is present (`avg_tokens` or `invalid_output_rate`).

Live-verified: expanded the evidence panel on a real `latency_spike` incident (full four-stage
table, `llm_call` correctly highlighted as the +883% outlier), a real `cost_spike` incident
(single-row token comparison), and confirmed the `tool_failure_rate` row shape directly against
live incident data — the three structurally distinct branches in `_evidenceRows`, not just one
happy path.

## 11. Remediation audit trail

Every remediation always did get recorded (`remediation_runs`, §5) — what was missing was a way to
*read* that history as its own log, independent of the incident feed. `storage.list_remediation_runs`
joins `remediation_runs` to `incidents` (detector, summary, stage) so each row reads as a
self-contained audit entry without a second lookup, ordered newest-first. `GET
/api/remediation-runs` exposes it; the dashboard's new "Remediation audit trail" section renders
every run ever recorded — not just the latest one per incident, which is all the existing
per-incident verification block (§5) ever showed.

"Initiated by: you, via the dashboard" is a fixed caption on every row, not a stored column —
every remediation in this system is human-clicked by architectural guarantee (§5's whole
co-pilot-not-autopilot point), so recording it per-row would just be a constant repeated forever.
Live-verified against two genuinely different real outcomes on the same running dashboard: a
`cost_spike` fail-over that correctly rolled back (switching providers doesn't fix a token-count
problem) and a `latency_spike` fail-over that also rolled back on this particular run (mock-backend
jitter meant the "after" probes didn't come back meaningfully faster than "before") — both
rendered with their real before/after numbers and rollback detail text, not a contrived
verified-only example.
