# AI Sentinel — diagrams

Eleven hand-drawn diagrams of how the system works. The full text of each section, with the exact files and thresholds, is in [ARCHITECTURE.md](ARCHITECTURE.md). These are static light-theme snapshots of the [rendered version](https://claude.ai/artifact/6CK9V5RV3fi4WhjNauvZnG), which also has each section's "where it lives" block and the write-ups of the bugs found live.

## The core loop

Every section below builds toward one repeating cycle — this is the shape of it:

![The core loop: inject or encounter a fault, detect it against a rolling baseline, diagnose which pipeline stage caused it, recommend a fix, and remediate with one click.](diagrams/00-core-loop.svg)

## 01 · System architecture

*Where everything lives*

Two Python processes, no message broker, no collector. The demo AI service only ever talks to SQLite (it writes spans and never reads them back); your browser only ever talks to the engine, which proxies every control call through to the demo service on your behalf.

![Your browser polls the Sentinel engine on port 8500 every 3 seconds. The engine sends probe and admin calls to the demo AI service on port 8000. The demo service writes spans into a shared SQLite file; the engine reads and writes checks and incidents in the same file.](diagrams/01-system-architecture.svg)

*The demo service never reads the database back — it only writes spans. The engine mediates everything your browser sees.*

Details: [ARCHITECTURE.md §1](ARCHITECTURE.md)

## 02 · Health-check hierarchy

*Three different questions, three different checks*

"Is it healthy?" is really three separate questions, each cheaper and less certain than the last: **is the process alive**, **can it serve traffic right now**, and **does it actually behave correctly end to end**. Only the third one calls a real model through the real pipeline — and it runs on its own clock, independent of whether anyone's looking at the dashboard.

![Three health check lanes. Liveness and readiness are polled by the dashboard every 3 seconds and check process status or recent error rate. The synthetic check runs independently every 60 seconds and sends a real prompt through the full pipeline, verifying the exact expected output.](diagrams/02-health-check-hierarchy.svg)

*Only the synthetic check exercises the real model through the real pipeline — everything else is a cheap proxy for "probably fine."*

Details: [ARCHITECTURE.md §2](ARCHITECTURE.md)

## 03 · Request trace lifecycle

*One request, four spans*

Every `/chat` call opens one root span and four nested child spans — the same shape you'd get instrumenting a real RAG pipeline. The bars below are **real timings from an actual run** of this system, not illustrative numbers.

![Code structure: a chat_request root span wrapping auth, retrieval, llm_call, and tool_call child spans. Below, a proportional waterfall of one real request: auth 9 milliseconds, retrieval 26 milliseconds, llm_call 405 milliseconds dominating the bar, tool_call 29 milliseconds.](diagrams/03-request-trace-lifecycle.svg)

*In steady state the LLM call dominates the budget — which is exactly why the failure detectors and root-cause correlator both treat it as the default suspect.*

Details: [ARCHITECTURE.md §3](ARCHITECTURE.md)

## 04 · Failure-detection pipeline

*From raw spans to a diagnosed, alerted incident*

Six threshold rules compare a short **recent** window against a longer **baseline** window that ends exactly where "recent" begins — so a real regression never gets diluted by mixing it into its own baseline. A cooldown gate stops one sustained problem from spamming the incident feed every 15 seconds.

![Spans split into a recent 90 second window and a baseline 900 second window. Both feed six detector rules. If no rule crosses its threshold, the sweep waits 15 seconds and tries again. If a rule fires but an incident for that detector is already open within the last 180 seconds, it's suppressed and that incident's last-seen time is refreshed. Otherwise the anomaly goes to root-cause diagnosis, then a remediation recommendation, then gets written as an incident and logged as an alert.](diagrams/04-failure-detection-pipeline.svg)

*Two gates, not one: a threshold has to be crossed, *and* the same detector can't already have an open incident from the last three minutes. §07 adds two more gates right after this — correlation across detectors, and a canary check before a fresh fail-over recommendation.*

Details: [ARCHITECTURE.md §4](ARCHITECTURE.md)

## 05 · Remediation / failover flow

*One click, not an auto-pilot*

Every incident carries a recommended action, but nothing executes without you clicking it — this is a co-pilot, not an autonomous fixer. "Fail over" doesn't hardcode a target: it reads whichever backend is currently active and flips to whichever one isn't.

![Sequence: you click Fail Over on an incident. The dashboard posts to the engine's incident action endpoint. The engine reads the demo service's current admin state, then posts the opposite backend to the admin backend endpoint. The engine marks the incident verifying in SQLite, then verified or rolled back automatically once probes confirm whether the metric actually improved. On the next chat request, the llm_call span's backend attribute shows backup.](diagrams/05-remediation-failover-flow.svg)

*Retrieval-stage and tool-stage incidents follow the same shape, toggling `/admin/retrieval` or `/admin/tools` instead.*

Details: [ARCHITECTURE.md §5](ARCHITECTURE.md)

## 06 · Dashboard data flow

*The browser is a viewer, not a worker*

Closing the dashboard tab doesn't pause anything. Detection and diagnosis run as background loops inside the engine process regardless of whether a browser is connected — the page you look at is just polling for what already happened.

![In your browser, a 3 second interval fetches five API endpoints in parallel and renders them to the DOM. On the server, independent of any browser, a synthetic check loop runs every 60 seconds and a detector sweep loop runs every 15 seconds, both writing into SQLite. The browser's fetches only read what these server loops already wrote.](diagrams/06-dashboard-data-flow.svg)

*Started in the FastAPI lifespan as two `asyncio` background tasks — they run for the life of the process, not the life of a connection.*

Details: [ARCHITECTURE.md §6](ARCHITECTURE.md)

## 07 · Correlation, cost, deploy-awareness, and canary

*Four more signals feeding the same loop*

None of this changes **what** remediation does — it changes how well-informed the recommendation is before you click it. Two of the four are new gates inside `engine.py::sweep_once`, picked up right where §04 left off, just after `diagnose()`.

![Continuing from diagnose(): a deployment note is appended if the service restarted within the last 120 seconds. Then a decision: is there already an open incident for the same root-cause stage within the last 60 seconds? If yes, merge this detector into that incident and stop — no new incident is created. If no, get the recommended remediation action. Then a second decision: is the action fail-over, on a brand new incident? If yes, run a canary comparison that probes both backends three times each and restores the original, attaching the result. Either way, the incident is created carrying its stage and canary result, and an alert is delivered.](diagrams/07-correlation-cost-deploy-canary.svg)

*Correlation only ever suppresses a duplicate; canary only ever runs once, on a brand-new fail-over recommendation. Neither one ever executes a remediation on its own.*

Details: [ARCHITECTURE.md §7](ARCHITECTURE.md)

## 08 · Regression memory

*A fix that worked once, staying worked*

Verifying a fix once isn't the same as it staying fixed. The instant an incident is marked `verified`, its failure signature and the action that resolved it are captured as a replayable fixture — so a later code change that quietly breaks a previously-working fix gets caught by a button, not by production.

![Capture happens automatically the instant an incident is marked verified: record_regression captures its detector, stage, and fault mode, and upserts a row into the regressions table keyed by detector and stage. Replay happens later, on demand: Run regression suite loops over every stored regression, injecting the stored fault, probing, applying the stored fix, probing again, then measuring and deciding whether the metric recovered — the same inject, probe, fix, probe, decide shape as the core loop at the top of this page, run now against stored parameters instead of a live anomaly.](diagrams/08-regression-memory.svg)

*Replay mirrors the loop at the top of this page — inject, probe, fix, probe, decide — run now against stored parameters instead of a live anomaly.*

Details: [ARCHITECTURE.md §8](ARCHITECTURE.md)

## 09 · Blind fault-injection eval

*Grading root-cause attribution honestly*

Every other eval in this system checks something mechanical — a string match, a metric recovering. This one checks whether diagnosis is actually *right*: a fault is injected without ever telling detection or diagnosis which one — they see only spans, exactly as they would for a real incident — and only afterward does the eval reveal its own choice to score the result.

![A fault mode is picked from a shuffled pass through all five injectable faults and set via the admin API, hidden from the detection code, then six probes are sent. Below a blind boundary, the real production detect function and rootcause.diagnose are called exactly as a live sweep would call them, producing a diagnosed stage or none. Only then is the actually injected expected stage revealed and compared, scoring the trial as correct, wrong stage, inconclusive, or not detected.](diagrams/09-blind-fault-injection-eval.svg)

*The boundary is real, not cosmetic: detectors.py and rootcause.py run their normal, unmodified code — the only thing withheld is which fault this trial actually injected.*

Details: [ARCHITECTURE.md §9](ARCHITECTURE.md)

## 10 · Quality eval — LLM-as-judge

*The one eval with no ground truth to check against*

Malformed output is already caught mechanically (§04's `invalid_output_rate`). What isn't caught is a response that's well-formed but generic — text that could sit under any question. There's no threshold for that, so a second, independent model judges it: one binary rubric, relevance, `PASS` or `FAIL`.

![One of four fixed test prompts goes through the normal demo chat pipeline, using whichever backend is currently active, and returns a response. That prompt and response together go to a judge model, selected independently of the demo's own primary and backup backends, preferring Anthropic and falling back to OpenAI, with no mock fallback. The judge returns pass or fail with one sentence of reasoning.](diagrams/10-quality-eval-llm-judge.svg)

*The judge is not part of the system it's grading — a separate provider pick, called directly, never routed through the demo service's own backend selection.*

Details: [ARCHITECTURE.md §14](ARCHITECTURE.md)
