import time

from ai_sentinel import detectors, rootcause, storage


def _seed(db, name, count, duration_ms, status="OK", offset_start=5, spacing=1):
    now = time.time()
    for i in range(count):
        t = now - offset_start - i * spacing
        storage.insert_span(
            db, span_id=f"{name}-{status}-{offset_start}-{i}", trace_id=f"trace-{name}-{offset_start}-{i}",
            parent_id=None, name=name, service_name="demo-ai-service",
            start_time=t, end_time=t + duration_ms / 1000, duration_ms=duration_ms,
            status=status, attributes={},
        )


def _seed_baseline_stages(db):
    for stage, base_ms in [("auth", 10), ("retrieval", 40), ("llm_call", 350), ("tool_call", 15)]:
        _seed(db, stage, 10, base_ms, offset_start=200, spacing=20)
    _seed(db, "chat_request", 10, 10 + 40 + 350 + 15, offset_start=200, spacing=20)


def test_diagnose_attributes_latency_spike_to_llm_call(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed_baseline_stages(db)

    _seed(db, "auth", 5, 10, offset_start=10, spacing=10)
    _seed(db, "retrieval", 5, 40, offset_start=10, spacing=10)
    _seed(db, "tool_call", 5, 15, offset_start=10, spacing=10)
    _seed(db, "llm_call", 5, 3500, offset_start=10, spacing=10)
    _seed(db, "chat_request", 5, 10 + 40 + 3500 + 15, offset_start=10, spacing=10)

    anomaly = next(a for a in detectors.run_detectors(db) if a.detector == "latency_spike")
    cause = rootcause.diagnose(db, anomaly)

    assert cause.stage == "llm_call"
    assert cause.confidence > 0.5


def test_diagnose_attributes_latency_spike_to_retrieval(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed_baseline_stages(db)

    _seed(db, "auth", 5, 10, offset_start=10, spacing=10)
    _seed(db, "llm_call", 5, 350, offset_start=10, spacing=10)
    _seed(db, "tool_call", 5, 15, offset_start=10, spacing=10)
    _seed(db, "retrieval", 5, 1400, offset_start=10, spacing=10)
    _seed(db, "chat_request", 5, 10 + 1400 + 350 + 15, offset_start=10, spacing=10)

    anomaly = next(a for a in detectors.run_detectors(db) if a.detector == "latency_spike")
    cause = rootcause.diagnose(db, anomaly)

    assert cause.stage == "retrieval"


def test_diagnose_tool_failure_shortcircuits_to_tool_call_stage(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed(db, "tool_call", 5, 15, status="ERROR", offset_start=10, spacing=10)

    anomaly = detectors.Anomaly(detector="tool_failure_rate", severity="warning", summary="tools failing")
    cause = rootcause.diagnose(db, anomaly)

    assert cause.stage == "tool_call"
    assert cause.confidence == 0.9


def test_diagnose_cost_spike_shortcircuits_to_llm_call_stage(tmp_path):
    """Only the LLM call stage produces tokens, so cost attribution shouldn't run through the
    latency-ratio machinery (which produced confusing "root cause: p95 latency" text for a
    token-cost finding before this was special-cased)."""
    db = str(tmp_path / "t.db")
    storage.init_db(db)

    anomaly = detectors.Anomaly(
        detector="cost_spike", severity="warning",
        summary="average tokens/request is 6.1x baseline (49 vs 8)",
        evidence={"ratio": 6.1},
    )
    cause = rootcause.diagnose(db, anomaly)

    assert cause.stage == "llm_call"
    assert "token" in cause.explanation.lower()
    assert "p95" not in cause.explanation.lower()


def test_diagnose_inconclusive_when_no_stage_dominates(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    # only 1 sample per stage recently -> below the min-sample floor, nothing to compare
    for stage in ("auth", "retrieval", "llm_call", "tool_call"):
        _seed(db, stage, 1, 20, offset_start=10, spacing=10)

    anomaly = detectors.Anomaly(detector="latency_spike", severity="warning", summary="x")
    cause = rootcause.diagnose(db, anomaly)

    assert cause.stage is None
