# AI Sentinel

A reliability engine for AI services: three-level health checks, threshold-based failure
detection, per-stage root-cause diagnosis, and one-click remediation — built around a small
instrumented demo AI service so the whole loop is runnable and demoable, not just described.

**Architecture, in six diagrams:** see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the
full write-up, or the [published diagram set](https://claude.ai/artifact/6CK9V5RV3fi4WhjNauvZnG)
for the rendered version.

## The idea

Detecting that an AI service is unhealthy is the easy part. The harder, less-built layer is
figuring out *why* and what to safely do about it. This system does both:

```
inject a fault → detect it (rolling baseline) → diagnose which pipeline stage caused it
    → recommend a fix → you click Fail Over / Disable Retrieval / Disable Tools
```

Two processes, no external infra:

- **`demo_service/`** (`:8000`) — a small FastAPI "AI service" with a realistic pipeline
  (auth → retrieval → LLM call → tool call), three-level health checks, and a fault-injection
  switch (`slow_llm`, `llm_errors`, `malformed_output`, `vector_db_slow`, `tool_failure`).
- **`ai_sentinel/`** (`:8500`) — the reliability engine: OpenTelemetry tracing into a shared
  SQLite file, a synthetic functional check, six threshold detectors, a root-cause correlator,
  a remediation executor, and the dashboard that ties it all together.

## Running it

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync
./scripts/run_demo.sh
```

This starts the demo service on `:8000` and the dashboard on `:8500`, and wipes
`sentinel.db` on each run so you start from a clean baseline. Open `http://localhost:8500`.

No API keys needed — both LLM backends fall back to a deterministic mock with realistic latency
jitter. Provider selection is availability-driven:

| `ANTHROPIC_API_KEY` | `OPENAI_API_KEY` | primary | backup |
|---|---|---|---|
| — | — | mock | mock |
| set | — | Claude (`claude-haiku-4-5`) | Claude |
| — | set | OpenAI (`gpt-5.6-luna`) | OpenAI |
| set | set | Claude | OpenAI |

Set both and "Fail over to backup backend" is a real cross-provider failover, not just a second
instance of the same model. Adding a third provider is one more entry in the priority list in
`demo_service/llm_client.py`.

## Deploying persistently

For a shared box you don't have root on (e.g. a lab GPU cluster), `deploy/systemd/` installs
both services as **user-level** systemd units — no sudo, nothing touches `/etc`:

```bash
rsync -avz --exclude='.venv' --exclude='.git' --exclude='__pycache__' --exclude='.pytest_cache' \
  --exclude='*.db*' . user@host:~/ai-sentinel/
ssh user@host 'cd ~/ai-sentinel && uv sync && ./deploy/systemd/install.sh'
```

The install script enables `loginctl linger` for your user, which is the part that actually makes
"survives a reboot" true — without it, a user-level systemd service only starts back up once you
next log in, not at boot. Manage it with the usual `systemctl --user` / `journalctl --user`
against `ai-sentinel-demo` and `ai-sentinel-engine`.

## The demo loop

1. Open the dashboard. The health strip should be green within a few seconds (liveness,
   readiness, and the first synthetic check).
2. Click **Send 5 test requests** a couple of times so there's a real baseline in the metrics.
3. Pick a fault mode (e.g. **slow llm**) and click **Send 5 test requests** again.
4. Within ~15s the detector sweep should flag it, with a plain-English root-cause explanation
   naming the specific pipeline stage — and a **Fail over to backup backend** (or
   **Disable retrieval** / **Disable tool calls**, depending on the fault) button.
5. Click it. The demo service's active backend flips immediately; the next request's trace
   shows the change.

Note on timing: detectors compare a 90-second "recent" window against a 90–990-second-ago
"baseline" window. A fault that runs for several minutes would naively age into its own baseline
and lose sensitivity — the system now excludes any period already covered by an open or
recently-resolved incident from that baseline computation (`storage.py::excluded_periods`), so a
sustained fault keeps getting flagged on later sweeps rather than going quiet. See
`docs/ARCHITECTURE.md` §4 for how.

## Configuring alerts

Every incident is always logged. To also deliver it somewhere:

- **`SLACK_WEBHOOK_URL`** — a Slack [incoming webhook](https://api.slack.com/messaging/webhooks)
  URL. Delivers a properly formatted message: a color bar matching the incident's severity (the
  same palette as the dashboard), the summary, the root-cause explanation, and the recommended
  action.
- **`ALERT_WEBHOOK_URL`** — any URL that accepts a POST with a flat JSON body (`detector`,
  `severity`, `summary`, `root_cause`, `confidence`, `recommended_action`) — for anything Slack
  doesn't cover: PagerDuty, a custom endpoint, whatever.
- **`ALERT_MIN_SEVERITY`** — `warning` (default: everything) or `critical`, to cut down on volume
  once you've seen enough `cost_spike` warnings.

Set either, both, or neither — they're independent, and each delivery is wrapped separately so
one failing (bad URL, network blip) doesn't block the other. See `ai_sentinel/alerts.py`.

## Cost, correlation, deploy-awareness, and canaries

Four things the engine does beyond raise-and-recommend:

- **Real dollar cost, not just tokens.** Every LLM call is priced against each provider's actual
  published per-token rate (`ai_sentinel/pricing.py`) and rolled up into `avg_cost_usd` /
  `total_cost_usd` metric tiles. The mock backend genuinely costs `$0` — there's no fake number to
  make the tile look alive.
- **Incident correlation.** Two detectors firing for the same root-cause stage within 60s (e.g.
  `latency_spike` and `error_rate_spike` both landing on `llm_call`) are almost always one
  underlying problem — the second gets merged into the first incident (`Signals: ...` on the
  card) instead of paging on-call twice for it (`storage.py::open_incident_for_stage` /
  `merge_detector_into_incident`, wired in `engine.py::sweep_once`).
- **Deployment correlation.** Each span carries the service's version (git SHA, or a `VERSION`
  file at deploy time) and process-start time. If an incident's root cause is diagnosed shortly
  after a restart, the explanation says so directly — "the service restarted 90s ago (version
  ...) — this may be related to that deploy" — instead of leaving a coincidental redeploy for you
  to notice on your own (`ai_sentinel/rootcause.py::_deployment_note`).
- **Canary comparison before you commit to a fail-over.** When a *new* incident recommends failing
  over, the engine shadow-probes both backends (3 requests each) and shows the comparison —
  `Expected: primary: 441ms avg, 0% err → backup: 500ms avg, 0% err` — right on the incident card,
  before you click anything (`ai_sentinel/canary.py`). This is informational only: nothing
  auto-triggers off it, matching the "co-pilot, not autopilot" stance everywhere else in this
  system — a human still clicks Fail Over.

## Testing

```bash
uv run pytest
```

Unit tests cover the detector thresholds, the root-cause stage-attribution logic, and the
storage layer's metric aggregation — using seeded spans with controlled timestamps rather than
live traffic, so they're deterministic.

## Project layout

```
demo_service/        the AI service being watched
  main.py             FastAPI app: /health/*, /chat, /admin/*
  pipeline.py         auth -> retrieval -> llm_call -> tool_call, fault injection
  llm_client.py       mock + Anthropic + OpenAI backends, availability-driven selection

ai_sentinel/          the reliability engine
  tracing.py           OpenTelemetry setup + SQLite span exporter
  storage.py           schema + queries (spans, synthetic_checks, incidents)
  synthetic.py         periodic functional check
  detectors.py         6 threshold-based failure detectors
  rootcause.py         per-stage deviation correlator + deployment-restart note
  remediation.py       action recommendation + execution
  verification.py      post-remediation verify + auto-rollback
  version.py           resolves the running git SHA/VERSION file + process start time
  pricing.py           per-provider $/token rates -> real cost estimates
  canary.py            shadow-probes both backends before a fail-over recommendation
  alerts.py            structured logging + Slack + generic webhook delivery
  engine.py            ties detect -> diagnose -> correlate -> recommend -> canary -> alert together
  dashboard/           API + static UI

scripts/run_demo.sh   starts both processes together (local/manual use)
deploy/systemd/        user-level systemd units + install script (persistent deployment)
tests/                 pytest suite
docs/ARCHITECTURE.md   the six-diagram architecture write-up
```

## Deliberately out of scope

Real email/SMTP alerting (Slack and generic webhooks are covered — see above), Docker/an OTel
Collector/Prometheus export, and LLM-as-judge quality evaluation — all reasonable follow-ups,
none needed to demonstrate the core idea. See `docs/ARCHITECTURE.md` for the full reasoning.
