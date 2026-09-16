import time

from ai_sentinel import detectors, storage


def _seed(db, name, count, duration_ms, status="OK", offset_start=5, spacing=1, attributes=None):
    now = time.time()
    for i in range(count):
        t = now - offset_start - i * spacing
        storage.insert_span(
            db, span_id=f"{name}-{status}-{offset_start}-{i}", trace_id=f"trace-{name}-{offset_start}-{i}",
            parent_id=None, name=name, service_name="demo-ai-service",
            start_time=t, end_time=t + duration_ms / 1000, duration_ms=duration_ms,
            status=status, attributes=attributes or {},
        )


def test_latency_spike_detected(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed(db, "chat_request", 10, 300, offset_start=200, spacing=20)  # baseline ~300ms
    _seed(db, "chat_request", 5, 3000, offset_start=10, spacing=10)  # recent ~3000ms

    names = {a.detector for a in detectors.run_detectors(db)}
    assert "latency_spike" in names


def test_no_false_positive_when_stable(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed(db, "chat_request", 10, 300, offset_start=200, spacing=20)
    _seed(db, "chat_request", 5, 320, offset_start=10, spacing=10)

    assert detectors.run_detectors(db) == []


def test_error_rate_spike_detected(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed(db, "chat_request", 10, 300, status="OK", offset_start=200, spacing=20)
    _seed(db, "chat_request", 3, 300, status="OK", offset_start=10, spacing=10)
    _seed(db, "chat_request", 3, 300, status="ERROR", offset_start=40, spacing=10)

    names = {a.detector for a in detectors.run_detectors(db)}
    assert "error_rate_spike" in names


def test_invalid_output_rate_detected_from_live_traffic(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed(db, "chat_request", 10, 300, offset_start=200, spacing=20)
    _seed(db, "chat_request", 2, 300, offset_start=10, spacing=10, attributes={"invalid_output": False})
    _seed(db, "chat_request", 2, 300, offset_start=40, spacing=10, attributes={"invalid_output": True})

    names = {a.detector for a in detectors.run_detectors(db)}
    assert "invalid_output_rate" in names


def test_tool_failure_rate_detected_independent_of_root_status(tmp_path):
    """Tool failures are non-fatal (the request still succeeds), so this can't rely on
    chat_request-level error status — it has to look at the tool_call stage directly."""
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed(db, "tool_call", 3, 20, status="ERROR", offset_start=10, spacing=10)
    _seed(db, "tool_call", 2, 20, status="OK", offset_start=50, spacing=10)

    names = {a.detector for a in detectors.run_detectors(db)}
    assert "tool_failure_rate" in names


def test_cost_spike_detected(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed(db, "chat_request", 10, 300, offset_start=200, spacing=20, attributes={"tokens_total": 100})
    _seed(db, "chat_request", 5, 300, offset_start=10, spacing=10, attributes={"tokens_total": 400})

    names = {a.detector for a in detectors.run_detectors(db)}
    assert "cost_spike" in names
