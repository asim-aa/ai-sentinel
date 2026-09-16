import asyncio
import time

from ai_sentinel import storage, tracing
from demo_service.llm_client import MockBackend
from demo_service.pipeline import PipelineState, run_pipeline


def test_llm_call_fault_only_applies_to_primary_backend(tmp_path):
    """A fail-over has to actually fix the problem for verified remediation to make sense --
    slow_llm/llm_errors should model a problem with the primary provider, not the llm_call stage
    in general, so switching to backup genuinely avoids it."""
    db = str(tmp_path / "t.db")
    tracer = tracing.init_tracing("test-pipeline-scoping", db)
    backends = {
        "primary": MockBackend("primary", base_latency_ms=10, jitter_ms=1),
        "backup": MockBackend("backup", base_latency_ms=10, jitter_ms=1),
    }

    state_primary = PipelineState(fault_mode="slow_llm", active_backend="primary")
    asyncio.run(run_pipeline("hello", state_primary, backends, tracer))

    state_backup = PipelineState(fault_mode="slow_llm", active_backend="backup")
    asyncio.run(run_pipeline("hello", state_backup, backends, tracer))

    llm_spans = storage.spans_since(db, 0, time.time(), name="llm_call")
    by_backend = {s["attributes"]["backend"]: s["duration_ms"] for s in llm_spans}

    assert by_backend["primary"] >= 1800  # the 2-4s slow_llm sleep applied
    assert by_backend["backup"] < 200  # fault did not apply; just the mock's own ~10ms latency
