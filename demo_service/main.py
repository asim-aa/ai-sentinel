"""The demo AI service: the thing AI Sentinel watches.

A small FastAPI app with a realistic-shaped pipeline (auth -> retrieval -> LLM -> tool call),
three-level health checks, and admin endpoints to inject faults / flip backends — everything
AI Sentinel needs to have something real to detect, diagnose, and remediate.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ai_sentinel import storage, tracing
from demo_service.llm_client import make_backends
from demo_service.pipeline import FAULT_MODES, PipelineError, PipelineState, run_pipeline

DB_PATH = os.environ.get(
    "SENTINEL_DB_PATH", str(Path(__file__).resolve().parent.parent / "sentinel.db")
)

app = FastAPI(title="Demo AI Service")
tracer = tracing.init_tracing("demo-ai-service", DB_PATH)
backends = make_backends()
state = PipelineState()


class ChatRequest(BaseModel):
    prompt: str


class FaultRequest(BaseModel):
    mode: str


class BackendRequest(BaseModel):
    backend: str


class ToggleRequest(BaseModel):
    enabled: bool


def _readiness_check() -> tuple[bool, dict]:
    """Ready unless our own recent LLM-call spans show a sustained error burst."""
    rows = storage.spans_since(DB_PATH, time.time() - 30, time.time(), name="llm_call")
    if len(rows) < 3:
        return True, {"llm_call_samples": len(rows)}
    errors = [r for r in rows if r["status"] == "ERROR"]
    error_rate = len(errors) / len(rows)
    return error_rate <= 0.5, {"llm_call_samples": len(rows), "error_rate": round(error_rate, 2)}


@app.get("/health/live")
def liveness():
    return {"status": "ok"}


@app.get("/health/ready")
def readiness():
    ok, detail = _readiness_check()
    if ok:
        return {"status": "ready", **detail}
    raise HTTPException(status_code=503, detail={"status": "unavailable", **detail})


@app.post("/chat")
async def chat(req: ChatRequest):
    try:
        return await run_pipeline(req.prompt, state, backends, tracer)
    except PipelineError as exc:
        raise HTTPException(status_code=502, detail={"error": str(exc), "reason": exc.reason})


@app.get("/admin/state")
def admin_state():
    return {
        "fault_mode": state.fault_mode,
        "active_backend": state.active_backend,
        "retrieval_enabled": state.retrieval_enabled,
        "tools_enabled": state.tools_enabled,
        "available_backends": list(backends.keys()),
        "available_fault_modes": list(FAULT_MODES),
    }


@app.post("/admin/fault")
def set_fault(req: FaultRequest):
    if req.mode not in FAULT_MODES:
        raise HTTPException(status_code=400, detail=f"unknown fault mode: {req.mode}")
    state.fault_mode = req.mode
    return admin_state()


@app.post("/admin/backend")
def set_backend(req: BackendRequest):
    if req.backend not in backends:
        raise HTTPException(status_code=400, detail=f"unknown backend: {req.backend}")
    state.active_backend = req.backend
    return admin_state()


@app.post("/admin/retrieval")
def set_retrieval(req: ToggleRequest):
    state.retrieval_enabled = req.enabled
    return admin_state()


@app.post("/admin/tools")
def set_tools(req: ToggleRequest):
    state.tools_enabled = req.enabled
    return admin_state()
