import sqlite3
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


def test_metrics_summary_computes_cost(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    now = time.time()
    costs = [0.001, 0.002, 0.003]
    for i, cost in enumerate(costs):
        storage.insert_span(
            db, span_id=f"c{i}", trace_id=f"tc{i}", parent_id=None, name="chat_request",
            service_name="demo-ai-service", start_time=now - 10 + i, end_time=now - 10 + i + 0.1,
            duration_ms=100, status="OK", attributes={"cost_usd": cost},
        )

    summary = storage.metrics_summary(db, window_s=60)

    assert round(summary["total_cost_usd"], 6) == round(sum(costs), 6)
    assert round(summary["avg_cost_usd"], 6) == round(sum(costs) / len(costs), 6)


def test_metrics_summary_cost_is_zero_without_cost_attributes(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    now = time.time()
    storage.insert_span(
        db, span_id="s0", trace_id="t0", parent_id=None, name="chat_request",
        service_name="demo-ai-service", start_time=now - 5, end_time=now - 4.9,
        duration_ms=100, status="OK", attributes={},
    )

    summary = storage.metrics_summary(db, window_s=60)
    assert summary["total_cost_usd"] == 0.0
    assert summary["avg_cost_usd"] == 0.0


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


def test_init_db_migrates_an_incidents_table_predating_the_new_columns(tmp_path):
    """Reproduces the real deploy bug this caught: a database created before `stage`,
    `merged_detectors`, and `canary_result` existed has an `incidents` table that
    CREATE TABLE IF NOT EXISTS silently skips, so init_db must ALTER it in instead."""
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE incidents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        detector TEXT NOT NULL,
        severity TEXT NOT NULL,
        summary TEXT NOT NULL,
        root_cause TEXT,
        confidence REAL,
        recommended_action TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        resolved_ts REAL
    )""")
    conn.commit()
    conn.close()

    storage.init_db(db)  # must not raise, and must add the missing columns + index

    incident_id = storage.create_incident(
        db, detector="latency_spike", severity="warning", summary="x",
        root_cause="y", confidence=0.5, recommended_action="z", stage="llm_call",
    )
    incident = storage.get_incident(db, incident_id)
    assert incident["stage"] == "llm_call"
    assert incident["merged_detectors"] == "latency_spike"


def test_open_incident_for_stage_finds_recent_open_incident(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    storage.create_incident(
        db, detector="latency_spike", severity="warning", summary="x",
        root_cause="y", confidence=0.5, recommended_action="z", stage="llm_call",
    )

    assert storage.open_incident_for_stage(db, "llm_call", cooldown_s=60) is not None
    assert storage.open_incident_for_stage(db, "retrieval", cooldown_s=60) is None
    assert storage.open_incident_for_stage(db, None, cooldown_s=60) is None


def test_open_incident_for_stage_ignores_resolved_incidents(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    incident_id = storage.create_incident(
        db, detector="latency_spike", severity="warning", summary="x",
        root_cause="y", confidence=0.5, recommended_action="z", stage="llm_call",
    )
    storage.update_incident_status(db, incident_id, "resolved")

    assert storage.open_incident_for_stage(db, "llm_call", cooldown_s=60) is None


def test_open_incident_for_stage_respects_cooldown_window(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    storage.create_incident(
        db, detector="latency_spike", severity="warning", summary="x",
        root_cause="y", confidence=0.5, recommended_action="z", stage="llm_call",
        ts=time.time() - 120,
    )

    assert storage.open_incident_for_stage(db, "llm_call", cooldown_s=60) is None


def test_merge_detector_into_incident_appends_without_duplicating(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    incident_id = storage.create_incident(
        db, detector="latency_spike", severity="warning", summary="x",
        root_cause="y", confidence=0.5, recommended_action="z", stage="llm_call",
    )

    storage.merge_detector_into_incident(db, incident_id, "error_rate_spike")
    storage.merge_detector_into_incident(db, incident_id, "error_rate_spike")  # idempotent
    storage.merge_detector_into_incident(db, incident_id, "cost_spike")

    incident = storage.get_incident(db, incident_id)
    assert incident["merged_detectors"] == "latency_spike,error_rate_spike,cost_spike"
