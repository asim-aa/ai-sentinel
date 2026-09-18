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

**A second limitation in that same fix, found later by running the blind eval at real scale (§9),
since also fixed:** the exclusion above had no upper bound — an incident that's still `open`
excludes all the way to "now," however long ago it was created. An incident that never gets
resolved (nothing in an unattended run clicks remediate) keeps growing that exclusion forever, and
once it's open longer than the lookback window, the exclusion fully swallows the current baseline
query, dropping `baseline["count"]` below `MIN_SAMPLES` and permanently blinding that detector —
not just to the original fault, but to any later, unrelated occurrence of the same fault. Caught
live on kolmogorov's real database, not just in a seeded test: a 50-trial blind-eval run left four
`latency_spike` incidents open, and every `slow_llm`/`vector_db_slow` trial after roughly the
15-minute mark scored `not_detected` for the rest of the ~90-minute run — 8/10 misses on both fault
types, against a clean 10/10 on the three fault types whose detectors don't read baseline at all.
Fixed by capping the exclusion's end at `min(now, incident_ts + lookback_s)`: a recent, genuinely
ongoing incident behaves exactly as before, but past the cap the excluded range stops growing and
eventually ages out of the baseline window on its own, the same way a resolved incident would.
Verified against kolmogorov's actual stale incidents from that run (not just a fresh seeded test):
baseline sample count went from 0 to 15 for the same detector, same incidents, same live database,
before and after the fix.

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

**`5/5` was one pass, not a statistically powered claim, and running the eval at real scale proved
exactly why that caveat mattered.** A 50-trial run (`trial_count=50`, 10 per fault type instead of
1) scored `68%` overall — `10/10` on the three fault types whose detectors are recent-window-only
(`invalid_output_rate`, `error_rate_spike`, `tool_failure_rate`), but only `2/10` on the two that
depend on baseline (`slow_llm`, `vector_db_slow`, both routed through `detect_latency_spike`). The
pattern wasn't noise: both latency fault types scored correct for their first one or two
occurrences, then missed every single trial from roughly the 15-minute mark onward, for the rest of
the ~90-minute run — the unbounded-exclusion bug in `excluded_periods` described in §4, triggered
here because nothing in an unattended eval run ever resolves the incidents it causes. A run
under ~15 minutes (including the original `n=5` pass) structurally can't hit this, since it needs
an incident older than the baseline lookback to even exist. Fixed in §4; not yet re-confirmed with
a second full-scale run as of this writing.

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

## 12. Incident timeline

Pure frontend — no schema change, no new backend query. Every event the timeline shows was
already being fetched by `refresh()` for some other section (§7's incidents, §11's audit trail's
`remediation_runs`, §8's regressions); `app.js::_timelineEvents` just reads the same objects a
second way and lays them out by time instead of by section. `renderIncidents` now takes the
already-fetched regressions list as a second argument purely to look up
`regressions.find(r => r.source_incident_id === inc.id)` — the one cross-reference the timeline
needs that no single section already carried.

This is deliberately the **coarse** timeline, not the fuller one a first pass at the idea sketched
(separate `canary completed` / `human approved` events with independent timestamps). This system
doesn't actually time those as separate events — detection, diagnosis, recommendation, and any
canary comparison all happen synchronously inside one `sweep_once()` sweep (§4, §7), so they share
exactly one timestamp (`incident.ts`) with no real sub-second ordering to report; inventing one
would be fabricated precision, not evidence. What *is* timed separately, and what the timeline
shows, one row per real timestamp this system already tracks:

1. **Detected** (`incident.ts`) — bundles detection + diagnosis + recommendation + canary (if any)
   into one event, since they're genuinely simultaneous; notes "canary compared backends" inline
   when `canary_result` is present rather than fabricating a second timestamp for it.
2. **Approved** (`remediation_runs.started_at`) — the moment you clicked an action; this system has
   no separate "approved" step before "executed," they're the same click.
3. **Verified or rolled back** (`remediation_runs.finished_at`) — with the real before/after metric
   values, same wording §11's audit trail uses.
4. **Regression saved** (`regression.created_at`), only shown when this incident is literally the
   one `regression.record_regression` used to create or refresh a regression row — looked up by
   `source_incident_id`, not assumed.

An incident with only step 1 (freshly detected, not yet acted on) doesn't render a timeline block
at all — one event isn't a timeline, and the evidence table above it already covers "why this
diagnosis." Live-verified against a real `latency_spike` incident that ran the full lifecycle:
all four events rendered in order, correct wording, correct real numbers, confirmed both by calling
`renderTimeline` directly against live-fetched data and by expanding the actual incident card in
the running dashboard.

## 13. 24h reliability card

Same shape as §2's health strip and the 5-minute metrics grid, at a longer window and one new
data source. `storage.reliability_summary` rolls up incidents and remediation outcomes over a
window — incident count, how many remediations actually verified vs. had to roll back, and a
median recovery time (`finished_at - incident.ts` across `verified` runs, via the same
`_percentile` helper the latency tiles already use, not a hand-rolled mean) — and `GET
/api/reliability` composes it with `metrics_summary`/`checks_stats` at the same window (default
86400s), mirroring `/api/metrics`'s existing `{requests, synthetic}` shape plus a new `incidents`
key. The dashboard's new "Reliability (last 24h)" grid reuses the same `.tile` styling as the
5-minute metrics grid above it, deliberately **without** sparklines — those tiles' rolling history
makes sense for a 5-minute window that visibly moves poll to poll; a 24-hour rolling aggregate
barely changes between one 3-second poll and the next, and a "trend line" built from that noise
would be misleading, not informative.

`finish_remediation_run` gained an optional `finished_at` override (defaulting to `time.time()`
exactly as before) purely so `reliability_summary`'s median-recovery computation could be tested
deterministically — the same testability pattern every other time-windowed function in this file
already follows (`ts`, `end_ts`, etc.), just not one this particular function had needed before
this feature required backdating a "recovery" for a test. Live-verified against a real
`tool_failure_rate` incident that ran the full remediate → verify cycle: the card correctly showed
4 incidents, 1 verified recovery, 0 rollbacks, and a 15s median recovery time, all matching what
`/api/reliability` returned directly.

## 14. Quality eval (LLM-as-judge)

Every eval so far in this system has a mechanically checkable ground truth: did a span show
`ERROR`, did the diagnosed stage match, did the metric actually recover. `ai_sentinel/
quality_eval.py` is the first one that doesn't — it asks a real model to judge whether another
model's response is any good, which has no ground truth of its own, only an opinion. That
distinction shaped every design choice here.

**Rubric: one binary question, not a quality score.** `RUBRIC_TEMPLATE` asks the judge exactly
one thing: does this response specifically address what was asked, or is it generic text that
could sit under any question — answered `PASS`/`FAIL`, not a 1–5 scale. This matches the
categorical-outcome idiom every other eval in this system already uses (`verified`/`rolled_back`;
`correct`/`wrong_stage`/`inconclusive`/`not_detected`) rather than introducing a numeric score
whose meaning (what separates a 3 from a 4?) nothing else here has to answer. Correctness and
style aren't judged — relevance is, specifically because it's the one quality axis that (a) isn't
already covered by an existing detector (`invalid_output_rate`, §4/§9, catches malformed/corrupted
text mechanically — `pipeline.py`'s fault injection sets that flag directly, no judgment involved)
and (b) produces an honest, non-flaky signal even against `MockBackend`: its four canned responses
are deliberately generic and prompt-independent, so a real judge should *consistently* fail them
for lack of relevance — a true, reproducible result, not noise, and a built-in sanity check on the
judge itself before trusting it against a real provider's answers.

**Test prompts are fixed and known, not blind.** Unlike `blind_eval.py`, there's no ground truth
being withheld here — "what a relevant answer looks like" is exactly the question put to the
judge, not a secret. `TEST_PROMPTS` is a small, representative, non-random set.

**The judge model is a separate role, not the demo's backup backend, and has no mock fallback.**
`_judge_provider()` picks Anthropic (preferred) or OpenAI independently of `demo_service`'s
primary/backup selection — the judge evaluates the system, it isn't part of it — and deliberately
does **not** fall back to a mock the way the demo backend does: faking semantic judgment would be
dishonest in exactly the way this project has avoided everywhere else (see the blind eval's
"don't fabricate precision" reasoning, §9). If neither `ANTHROPIC_API_KEY` nor `OPENAI_API_KEY`
is set, `run_quality_eval` raises immediately with a clear message; the API layer turns that into
a `503`, and the dashboard surfaces it directly rather than failing silently.

**The judge's own SDK calls are independent of `demo_service/llm_client.py`, not reused from it**,
even though the request shapes are nearly identical (same Anthropic Messages API, same OpenAI
Responses API). `ai_sentinel/` talks to `demo_service/` over HTTP everywhere else in this codebase
— `synthetic.py`, `canary.py`, `regression.py`, `blind_eval.py` all probe it through `/chat` and
`/admin/*`, never by importing its Python modules directly, because in a real deployment the
engine and the watched service are different processes. Importing `AnthropicBackend`/
`OpenAIBackend` directly into the engine package to save a few lines would quietly break that
boundary; `quality_eval.py` owns its own minimal `_call_anthropic`/`_call_openai` instead.

**Cost is tracked like every other LLM call in this system.** The judge's token usage is priced
through the existing `ai_sentinel/pricing.py` table (same model IDs `llm_client.py` already uses,
so no new pricing entries were needed) and reported per-run — a quality eval that hid what it cost
to run would undercut the same "real dollar cost, not just tokens" theme §7 established.

No real `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` was available in-session, so — the same boundary the
OpenAI backend's live call path has always had here — the judge-calling logic is verified via 15
mocked-SDK unit tests (provider selection, verdict parsing, both providers' response shapes,
aggregation, persistence), and the "no judge configured" path is the one verified live end to end:
a real `503` from a running dashboard, parsed correctly by the frontend's error handling, and
surfaced to the user with the exact message `run_quality_eval` raised — confirmed via the browser
console rather than a literal dialog, since automated browsers suppress native `alert()`.
