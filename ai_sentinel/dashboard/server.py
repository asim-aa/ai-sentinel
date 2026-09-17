"""AI Sentinel dashboard: JSON API + static UI, plus the background synthetic-check and
detector-sweep loops that make this a living reliability engine rather than a static page."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ai_sentinel import blind_eval, engine, regression, remediation, storage, synthetic, verification

DB_PATH = os.environ.get(
    "SENTINEL_DB_PATH", str(Path(__file__).resolve().parent.parent.parent / "sentinel.db")
)
DEMO_URL = os.environ.get("DEMO_SERVICE_URL", "http://localhost:8000")
SYNTHETIC_INTERVAL_S = float(os.environ.get("SENTINEL_SYNTHETIC_INTERVAL_S", "60"))
SWEEP_INTERVAL_S = float(os.environ.get("SENTINEL_SWEEP_INTERVAL_S", "15"))
STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.init_db(DB_PATH)
    tasks = [
        asyncio.create_task(synthetic.synthetic_check_loop(DB_PATH, DEMO_URL, SYNTHETIC_INTERVAL_S)),
        asyncio.create_task(engine.sweep_loop(DB_PATH, DEMO_URL, SWEEP_INTERVAL_S)),
    ]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="AI Sentinel", lifespan=lifespan)


async def _verify_then_record_regression(incident_id: int, action: str, demo_url: str, db_path: str) -> None:
    await verification.verify_and_rollback_if_needed(incident_id, action, demo_url, db_path)
    regression.record_regression(db_path, incident_id)  # no-op unless verification just succeeded


class IncidentAction(BaseModel):
    action: str  # "fail_over" | "disable_retrieval" | "disable_tools" | "ignore" | "investigate"


class FaultRequest(BaseModel):
    mode: str


class BackendRequest(BaseModel):
    backend: str


class SendTrafficRequest(BaseModel):
    count: int = 5
    prompt: str = "Summarize the latest quarterly report in one sentence."


@app.get("/api/health")
async def api_health():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{DEMO_URL}/health/ready")
        ready_ok = resp.status_code == 200
        try:
            ready_detail = resp.json()
        except Exception:
            ready_detail = {}
    except Exception as exc:
        ready_ok = False
        ready_detail = {"detail": str(exc)}
    readiness = {"status": "ready" if ready_ok else "unavailable", **ready_detail}

    last_check = storage.last_synthetic_check(DB_PATH)
    if last_check is None:
        synth = {"status": "pending", "detail": "no synthetic check run yet"}
    else:
        age_s = time.time() - last_check["ts"]
        stale = age_s > SYNTHETIC_INTERVAL_S * 3
        synth = {
            "status": "ok" if (last_check["passed"] and not stale) else ("stale" if stale else "failing"),
            "passed": bool(last_check["passed"]),
            "latency_ms": last_check["latency_ms"],
            "age_s": round(age_s, 1),
            "detail": last_check["detail"],
        }

    overall = "healthy" if (ready_ok and synth["status"] == "ok") else "degraded"
    return {"overall": overall, "liveness": {"status": "ok"}, "readiness": readiness, "synthetic": synth}


@app.get("/api/metrics")
def api_metrics(window: int = 300):
    return {
        "requests": storage.metrics_summary(DB_PATH, window),
        "synthetic": storage.checks_stats(DB_PATH, window),
    }


@app.get("/api/traces")
def api_traces(limit: int = 20):
    return storage.recent_traces(DB_PATH, limit)


@app.get("/api/incidents")
def api_incidents(status: str | None = None):
    incidents = storage.list_incidents(DB_PATH, status=status)
    for inc in incidents:
        run = storage.latest_remediation_run(DB_PATH, inc["id"])
        if run:
            inc["remediation_run"] = run
    return incidents


@app.post("/api/incidents/{incident_id}/action")
async def api_incident_action(incident_id: int, req: IncidentAction):
    incident = storage.get_incident(DB_PATH, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="incident not found")

    if req.action == "ignore":
        storage.update_incident_status(DB_PATH, incident_id, "ignored")
        return storage.get_incident(DB_PATH, incident_id)

    if req.action == "investigate":
        return incident

    try:
        result = await remediation.execute(req.action, DEMO_URL)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"remediation failed: {exc}")

    # Don't mark this "done" yet — kick off verification in the background (probing + comparing
    # + possible rollback takes a while) and let the dashboard poll for the outcome.
    storage.update_incident_status(DB_PATH, incident_id, "verifying")
    asyncio.create_task(
        _verify_then_record_regression(incident_id, req.action, DEMO_URL, DB_PATH)
    )
    return {"incident": storage.get_incident(DB_PATH, incident_id), "demo_service_state": result}


@app.get("/api/regressions")
def api_regressions():
    return storage.list_regressions(DB_PATH)


@app.post("/api/regressions/run")
async def api_run_regressions():
    results = await regression.run_regression_suite(DB_PATH, DEMO_URL)
    return {"results": results}


@app.get("/api/blind-eval/runs")
def api_blind_eval_runs(limit: int = 10):
    return storage.list_blind_eval_runs(DB_PATH, limit=limit)


@app.post("/api/blind-eval/run")
async def api_run_blind_eval(trials: int = blind_eval.DEFAULT_TRIAL_COUNT):
    return await blind_eval.run_blind_eval(DB_PATH, DEMO_URL, trial_count=trials)


@app.get("/api/remediation-runs")
def api_remediation_runs(limit: int = 20):
    return storage.list_remediation_runs(DB_PATH, limit=limit)


@app.get("/api/fault-state")
async def api_fault_state():
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{DEMO_URL}/admin/state")
    resp.raise_for_status()
    return resp.json()


@app.post("/api/fault")
async def api_set_fault(req: FaultRequest):
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{DEMO_URL}/admin/fault", json={"mode": req.mode})
    resp.raise_for_status()
    return resp.json()


@app.post("/api/backend")
async def api_set_backend(req: BackendRequest):
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{DEMO_URL}/admin/backend", json={"backend": req.backend})
    resp.raise_for_status()
    return resp.json()


@app.post("/api/send-traffic")
async def api_send_traffic(req: SendTrafficRequest):
    results = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for _ in range(max(1, min(req.count, 20))):
            try:
                resp = await client.post(f"{DEMO_URL}/chat", json={"prompt": req.prompt})
                results.append({"status": resp.status_code})
            except Exception as exc:
                results.append({"status": "error", "detail": str(exc)})
    return {"sent": len(results), "results": results}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
