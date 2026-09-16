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

No `ANTHROPIC_API_KEY` needed — both LLM backends fall back to a deterministic mock with
realistic latency jitter. Set `ANTHROPIC_API_KEY` to route through real `claude-haiku-4-5`
calls instead (both "primary" and "backup" use the same model in that mode — swapping in a
second real provider is a one-line change in `demo_service/llm_client.py`).

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
  llm_client.py       mock backend + real Anthropic backend

ai_sentinel/          the reliability engine
  tracing.py           OpenTelemetry setup + SQLite span exporter
  storage.py           schema + queries (spans, synthetic_checks, incidents)
  synthetic.py         periodic functional check
  detectors.py         6 threshold-based failure detectors
  rootcause.py         per-stage deviation correlator
  remediation.py       action recommendation + execution
  alerts.py            structured logging + optional webhook
  engine.py            ties detect -> diagnose -> recommend -> record -> alert together
  dashboard/           API + static UI

scripts/run_demo.sh   starts both processes together (local/manual use)
deploy/systemd/        user-level systemd units + install script (persistent deployment)
tests/                 pytest suite
docs/ARCHITECTURE.md   the six-diagram architecture write-up
```

## Deliberately out of scope

Real Slack/email alerting, Docker/an OTel Collector/Prometheus export, a second real model
provider, and LLM-as-judge quality evaluation — all reasonable follow-ups, none needed to
demonstrate the core idea. See `docs/ARCHITECTURE.md` for the full reasoning.
