import time

from ai_sentinel import storage


def test_metrics_summary_computes_error_and_invalid_rates(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    now = time.time()
    durations = [100, 100, 100, 100, 500]
    for i, dur in enumerate(durations):
        storage.insert_span(
            db, span_id=f"s{i}", trace_id=f"t{i}", parent_id=None, name="chat_request",
            service_name="demo-ai-service", start_time=now - 10 + i, end_time=now - 10 + i + dur / 1000,
            duration_ms=dur, status="ERROR" if i == 4 else "OK",
            attributes={"invalid_output": i == 0, "tokens_total": 50 + i},
        )

    summary = storage.metrics_summary(db, window_s=60)

    assert summary["count"] == 5
    assert summary["error_rate"] == 0.2
    assert summary["invalid_output_rate"] == 0.2
    assert summary["p50_ms"] == 100
    assert summary["avg_tokens"] == sum(50 + i for i in range(5)) / 5


def test_incident_cooldown_prevents_duplicate_open_incidents(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    storage.create_incident(
        db, detector="latency_spike", severity="warning", summary="x",
        root_cause="y", confidence=0.5, recommended_action="z",
    )

    assert storage.open_incident_for_detector(db, "latency_spike", cooldown_s=120) is not None
    assert storage.open_incident_for_detector(db, "error_rate_spike", cooldown_s=120) is None


def test_update_incident_status_reopens_cleanly(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    incident_id = storage.create_incident(
        db, detector="latency_spike", severity="warning", summary="x",
        root_cause="y", confidence=0.5, recommended_action="z",
    )

    storage.update_incident_status(db, incident_id, "remediated")
    incident = storage.get_incident(db, incident_id)

    assert incident["status"] == "remediated"
    assert incident["resolved_ts"] is not None
    assert storage.open_incident_for_detector(db, "latency_spike") is None
