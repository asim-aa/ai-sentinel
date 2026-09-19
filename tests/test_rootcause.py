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


def test_diagnose_invalid_output_shortcircuits_to_llm_call_stage(tmp_path):
    """Only the LLM call stage produces response text, so malformed-output attribution shouldn't
    fall through to the generic per-stage error-rate comparison — which can't see it at all,
    since corrupting the text never marks any span's status ERROR (this was the real gap the
    blind fault-injection eval quantified: malformed_output scored 'inconclusive' every trial
    until this branch existed)."""
    db = str(tmp_path / "t.db")
    storage.init_db(db)

    anomaly = detectors.Anomaly(
        detector="invalid_output_rate", severity="warning",
        summary="invalid/malformed output rate is 40% (via live traffic)",
        evidence={"rate": 0.4},
    )
    cause = rootcause.diagnose(db, anomaly)

    assert cause.stage == "llm_call"
    assert cause.confidence == 0.85
    assert "malformed" in cause.explanation.lower() or "invalid" in cause.explanation.lower()


def test_diagnose_stays_accurate_on_a_later_sweep_of_a_sustained_fault(tmp_path):
    """Root-cause attribution needs the same baseline-exclusion fix as the detector: on a
    second detection of a fault that's been running for minutes, its own earlier history has
    aged into the stage baseline too and would otherwise dilute the deviation ratio."""
    db = str(tmp_path / "t.db")
    storage.init_db(db)

    # genuine clean history, well before the fault started
    for stage, base_ms in [("auth", 10), ("retrieval", 40), ("llm_call", 350), ("tool_call", 15)]:
        _seed(db, stage, 6, base_ms, offset_start=850, spacing=20)
    _seed(db, "chat_request", 6, 10 + 40 + 350 + 15, offset_start=850, spacing=20)

    # the fault's own earlier history, now aged past "recent" into what would be "baseline"
    for stage, base_ms in [("auth", 10), ("retrieval", 40), ("tool_call", 15)]:
        _seed(db, stage, 5, base_ms, offset_start=300, spacing=60)
    _seed(db, "llm_call", 5, 3500, offset_start=300, spacing=60)
    _seed(db, "chat_request", 5, 10 + 40 + 3500 + 15, offset_start=300, spacing=60)

    # an already-fired, still-open incident covering that earlier stretch; the sweeps in between
    # kept re-firing it (that is what "sustained" means), the latest one 15s ago
    incident_id = storage.create_incident(
        db, detector="latency_spike", severity="critical",
        summary="earlier detection", root_cause="x", confidence=0.9,
        recommended_action="Fail over to backup backend",
        ts=time.time() - 690,
    )
    storage.touch_incident(db, incident_id, ts=time.time() - 15)

    # the fault, still ongoing right now
    for stage, base_ms in [("auth", 10), ("retrieval", 40), ("tool_call", 15)]:
        _seed(db, stage, 5, base_ms, offset_start=10, spacing=10)
    _seed(db, "llm_call", 5, 3500, offset_start=10, spacing=10)
    _seed(db, "chat_request", 5, 10 + 40 + 3500 + 15, offset_start=10, spacing=10)

    anomaly = detectors.detect_latency_spike(db)
    assert anomaly is not None, "the detector itself should still fire on this later sweep"

    cause = rootcause.diagnose(db, anomaly)
    assert cause.stage == "llm_call"
    assert cause.confidence > 0.5


def test_diagnose_inconclusive_when_no_stage_dominates(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    # only 1 sample per stage recently -> below the min-sample floor, nothing to compare
    for stage in ("auth", "retrieval", "llm_call", "tool_call"):
        _seed(db, stage, 1, 20, offset_start=10, spacing=10)

    anomaly = detectors.Anomaly(detector="latency_spike", severity="warning", summary="x")
    cause = rootcause.diagnose(db, anomaly)

    assert cause.stage is None


def _seed_chat_request_with_service_attrs(db, started_at, offset_start=5):
    t = time.time() - offset_start
    storage.insert_span(
        db, span_id=f"chat-{offset_start}", trace_id=f"trace-chat-{offset_start}", parent_id=None,
        name="chat_request", service_name="demo-ai-service",
        start_time=t, end_time=t + 0.1, duration_ms=100, status="OK",
        attributes={"service_started_at": started_at, "service_version": "abc1234"},
    )


def test_diagnose_notes_a_recent_restart(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed_chat_request_with_service_attrs(db, started_at=time.time() - 15)

    anomaly = detectors.Anomaly(detector="tool_failure_rate", severity="warning", summary="tools failing")
    cause = rootcause.diagnose(db, anomaly)

    assert "restarted" in cause.explanation
    assert "abc1234" in cause.explanation


def test_diagnose_does_not_note_a_stale_restart(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed_chat_request_with_service_attrs(db, started_at=time.time() - 3600)

    anomaly = detectors.Anomaly(detector="tool_failure_rate", severity="warning", summary="tools failing")
    cause = rootcause.diagnose(db, anomaly)

    assert "restarted" not in cause.explanation


def test_diagnose_does_not_note_when_no_service_attrs_present(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed(db, "tool_call", 5, 15, status="ERROR", offset_start=10, spacing=10)

    anomaly = detectors.Anomaly(detector="tool_failure_rate", severity="warning", summary="tools failing")
    cause = rootcause.diagnose(db, anomaly)

    assert "restarted" not in cause.explanation
