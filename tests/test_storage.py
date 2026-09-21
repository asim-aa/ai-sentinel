import json
import sqlite3
import time

import pytest

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


def test_create_incident_round_trips_evidence(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    evidence = {
        "recent": {"llm_call": {"count": 5, "error_rate": 0.0, "p50_ms": 640, "p95_ms": 891}},
        "baseline": {"llm_call": {"count": 6, "error_rate": 0.0, "p50_ms": 100, "p95_ms": 104}},
        "deviations": {"llm_call": 8.57},
    }
    incident_id = storage.create_incident(
        db, detector="latency_spike", severity="critical", summary="x",
        root_cause="y", confidence=0.9, recommended_action="z", evidence=evidence,
    )

    incident = storage.get_incident(db, incident_id)
    assert json.loads(incident["evidence"]) == evidence


def test_create_incident_evidence_defaults_to_none(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    incident_id = storage.create_incident(
        db, detector="tool_failure_rate", severity="warning", summary="x",
        root_cause="y", confidence=0.9, recommended_action="z",
    )

    assert storage.get_incident(db, incident_id)["evidence"] is None


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
        evidence={"recent": {"llm_call": {"p95_ms": 900}}},
    )
    incident = storage.get_incident(db, incident_id)
    assert incident["stage"] == "llm_call"
    assert incident["merged_detectors"] == "latency_spike"
    assert incident["evidence"] == '{"recent": {"llm_call": {"p95_ms": 900}}}'

    storage.touch_incident(db, incident_id, ts=123.0)  # last_seen_ts must have been migrated in too
    assert storage.get_incident(db, incident_id)["last_seen_ts"] == 123.0


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


def test_upsert_regression_creates_then_updates_same_signature(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)

    first_id = storage.upsert_regression(
        db, detector="latency_spike", stage="llm_call", fault_mode="slow_llm",
        summary="p95 latency is 3x baseline", root_cause="LLM call is the outlier stage",
        action="fail_over", metric="p95_ms", before_value=3500.0, after_value=420.0,
        source_incident_id=1,
    )
    second_id = storage.upsert_regression(
        db, detector="latency_spike", stage="llm_call", fault_mode="slow_llm",
        summary="p95 latency is 4x baseline", root_cause="LLM call is the outlier stage",
        action="fail_over", metric="p95_ms", before_value=4100.0, after_value=410.0,
        source_incident_id=2,
    )

    assert first_id == second_id  # same (detector, stage) signature -> updated in place, not duplicated
    regressions = storage.list_regressions(db)
    assert len(regressions) == 1
    assert regressions[0]["summary"] == "p95 latency is 4x baseline"
    assert regressions[0]["source_incident_id"] == 2


def test_upsert_regression_distinct_stage_creates_a_separate_row(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)

    storage.upsert_regression(
        db, detector="latency_spike", stage="llm_call", fault_mode="slow_llm",
        summary="x", root_cause="y", action="fail_over", metric="p95_ms",
        before_value=3500.0, after_value=420.0, source_incident_id=1,
    )
    storage.upsert_regression(
        db, detector="latency_spike", stage="retrieval", fault_mode="vector_db_slow",
        summary="x", root_cause="y", action="disable_retrieval", metric="p95_ms",
        before_value=1400.0, after_value=380.0, source_incident_id=2,
    )

    assert len(storage.list_regressions(db)) == 2


def test_record_regression_run_updates_pass_state(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    reg_id = storage.upsert_regression(
        db, detector="latency_spike", stage="llm_call", fault_mode="slow_llm",
        summary="x", root_cause="y", action="fail_over", metric="p95_ms",
        before_value=3500.0, after_value=420.0, source_incident_id=1,
    )

    storage.record_regression_run(db, reg_id, passed=True, detail="3500.00 -> 410.00 (improved)")
    regression = storage.get_regression(db, reg_id)

    assert regression["last_run_passed"] == 1
    assert regression["last_run_at"] is not None
    assert "improved" in regression["last_run_detail"]


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


def test_record_blind_eval_run_round_trips_nested_data(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    trials = [
        {"fault_mode": "tool_failure", "expected_stage": "tool_call", "diagnosed_stage": "tool_call", "outcome": "correct"},
        {"fault_mode": "slow_llm", "expected_stage": "llm_call", "diagnosed_stage": None, "outcome": "not_detected"},
    ]
    counts = {"correct": 1, "wrong_stage": 0, "inconclusive": 0, "not_detected": 1}

    run_id = storage.record_blind_eval_run(
        db, ts=time.time(), trial_count=2, accuracy=0.5, counts=counts, trials=trials,
    )

    runs = storage.list_blind_eval_runs(db)
    assert len(runs) == 1
    assert runs[0]["id"] == run_id
    assert runs[0]["accuracy"] == 0.5
    assert runs[0]["counts"] == counts
    assert runs[0]["trials"] == trials


def test_list_blind_eval_runs_orders_newest_first_and_respects_limit(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    now = time.time()
    for i in range(3):
        storage.record_blind_eval_run(
            db, ts=now + i, trial_count=1, accuracy=1.0,
            counts={"correct": 1, "wrong_stage": 0, "inconclusive": 0, "not_detected": 0},
            trials=[{"fault_mode": "slow_llm", "expected_stage": "llm_call", "diagnosed_stage": "llm_call", "outcome": "correct"}],
        )

    runs = storage.list_blind_eval_runs(db, limit=2)
    assert len(runs) == 2
    assert runs[0]["ts"] > runs[1]["ts"]


def test_list_remediation_runs_carries_incident_context(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    incident_id = storage.create_incident(
        db, detector="latency_spike", severity="critical", summary="p95 latency is 8x baseline",
        root_cause="LLM provider call is the outlier stage", confidence=0.9,
        recommended_action="Fail over to backup backend", stage="llm_call",
    )
    run_id = storage.create_remediation_run(
        db, incident_id=incident_id, action="fail_over", metric="p95_ms",
        before_value=3500.0, started_at=time.time(),
    )
    storage.finish_remediation_run(db, run_id, after_value=420.0, outcome="verified", detail="improved")

    runs = storage.list_remediation_runs(db)
    assert len(runs) == 1
    run = runs[0]
    assert run["id"] == run_id
    assert run["incident_id"] == incident_id
    assert run["action"] == "fail_over"
    assert run["outcome"] == "verified"
    assert run["before_value"] == 3500.0
    assert run["after_value"] == 420.0
    assert run["incident_detector"] == "latency_spike"
    assert run["incident_stage"] == "llm_call"
    assert run["incident_summary"] == "p95 latency is 8x baseline"


def test_list_remediation_runs_orders_newest_first_and_respects_limit(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    incident_id = storage.create_incident(
        db, detector="latency_spike", severity="warning", summary="x",
        root_cause="y", confidence=0.5, recommended_action="z", stage="llm_call",
    )
    now = time.time()
    for i in range(3):
        run_id = storage.create_remediation_run(
            db, incident_id=incident_id, action="fail_over", metric="p95_ms",
            before_value=1000.0, started_at=now + i,
        )
        storage.finish_remediation_run(db, run_id, after_value=200.0, outcome="verified", detail="ok")

    runs = storage.list_remediation_runs(db, limit=2)
    assert len(runs) == 2
    assert runs[0]["started_at"] > runs[1]["started_at"]


def test_reliability_summary_is_all_zero_with_no_history(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)

    summary = storage.reliability_summary(db, window_s=86400)
    assert summary["incident_count"] == 0
    assert summary["verified_count"] == 0
    assert summary["rolled_back_count"] == 0
    assert summary["median_recovery_s"] is None


def test_reliability_summary_counts_outcomes_and_computes_median_recovery(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    now = time.time()

    # incident 1: detected 100s ago, verified 40s ago -> recovered in 60s
    inc1 = storage.create_incident(
        db, detector="latency_spike", severity="critical", summary="x", root_cause="y",
        confidence=0.9, recommended_action="z", ts=now - 100,
    )
    run1 = storage.create_remediation_run(
        db, incident_id=inc1, action="fail_over", metric="p95_ms",
        before_value=4000.0, started_at=now - 95,
    )
    storage.finish_remediation_run(
        db, run1, after_value=400.0, outcome="verified", detail="improved", finished_at=now - 40,
    )

    # incident 2: detected 80s ago, verified 60s ago -> recovered in 20s
    inc2 = storage.create_incident(
        db, detector="tool_failure_rate", severity="warning", summary="x", root_cause="y",
        confidence=0.9, recommended_action="z", ts=now - 80,
    )
    run2 = storage.create_remediation_run(
        db, incident_id=inc2, action="disable_tools", metric="error_rate",
        before_value=0.5, started_at=now - 75,
    )
    storage.finish_remediation_run(
        db, run2, after_value=0.0, outcome="verified", detail="improved", finished_at=now - 60,
    )

    # incident 3: rolled back -- doesn't count toward recovery time
    inc3 = storage.create_incident(
        db, detector="cost_spike", severity="warning", summary="x", root_cause="y",
        confidence=0.85, recommended_action="z", ts=now - 50,
    )
    run3 = storage.create_remediation_run(
        db, incident_id=inc3, action="fail_over", metric="avg_tokens",
        before_value=40.0, started_at=now - 45,
    )
    storage.finish_remediation_run(
        db, run3, after_value=45.0, outcome="rolled_back", detail="did not improve", finished_at=now - 30,
    )

    summary = storage.reliability_summary(db, window_s=86400)
    assert summary["incident_count"] == 3
    assert summary["verified_count"] == 2
    assert summary["rolled_back_count"] == 1
    assert summary["median_recovery_s"] == 20.0  # nearest-rank median of [20, 60] -> the lower one


def test_reliability_summary_respects_the_window(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    now = time.time()

    old_incident = storage.create_incident(
        db, detector="latency_spike", severity="critical", summary="old", root_cause="y",
        confidence=0.9, recommended_action="z", ts=now - 200,
    )
    old_run = storage.create_remediation_run(
        db, incident_id=old_incident, action="fail_over", metric="p95_ms",
        before_value=4000.0, started_at=now - 195,
    )
    storage.finish_remediation_run(
        db, old_run, after_value=400.0, outcome="verified", detail="improved", finished_at=now - 190,
    )

    storage.create_incident(
        db, detector="tool_failure_rate", severity="warning", summary="recent", root_cause="y",
        confidence=0.9, recommended_action="z", ts=now - 10,
    )

    summary = storage.reliability_summary(db, window_s=100)  # only the last 100s
    assert summary["incident_count"] == 1  # the old one (ts=now-200) is outside the window
    assert summary["verified_count"] == 0  # its remediation_run also finished outside the window
    assert summary["median_recovery_s"] is None


def test_record_quality_eval_run_round_trips_results(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    results = [
        {"prompt": "What is the capital of France?", "response": "Paris.", "verdict": "pass",
         "reasoning": "Directly answers the question.", "judge_model": "claude-haiku-4-5-20251001",
         "judge_cost_usd": 0.0001},
        {"prompt": "Summarize the report.", "response": "Based on the retrieved context...",
         "verdict": "fail", "reasoning": "Generic boilerplate, not specific to the prompt.",
         "judge_model": "claude-haiku-4-5-20251001", "judge_cost_usd": 0.0001},
    ]

    run_id = storage.record_quality_eval_run(
        db, ts=time.time(), judge_model="claude-haiku-4-5-20251001", prompt_count=2,
        pass_count=1, total_cost_usd=0.0002, results=results,
    )

    runs = storage.list_quality_eval_runs(db)
    assert len(runs) == 1
    assert runs[0]["id"] == run_id
    assert runs[0]["judge_model"] == "claude-haiku-4-5-20251001"
    assert runs[0]["prompt_count"] == 2
    assert runs[0]["pass_count"] == 1
    assert runs[0]["total_cost_usd"] == 0.0002
    assert runs[0]["results"] == results


def test_list_quality_eval_runs_orders_newest_first_and_respects_limit(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    now = time.time()
    for i in range(3):
        storage.record_quality_eval_run(
            db, ts=now + i, judge_model="claude-haiku-4-5-20251001", prompt_count=1,
            pass_count=1, total_cost_usd=0.0001,
            results=[{"prompt": "x", "response": "y", "verdict": "pass", "reasoning": "z",
                      "judge_model": "claude-haiku-4-5-20251001", "judge_cost_usd": 0.0001}],
        )

    runs = storage.list_quality_eval_runs(db, limit=2)
    assert len(runs) == 2
    assert runs[0]["ts"] > runs[1]["ts"]


def test_touch_incident_records_last_seen_only_for_open_incidents(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    open_id = storage.create_incident(
        db, detector="latency_spike", severity="warning", summary="x",
        root_cause="y", confidence=0.5, recommended_action="z",
    )
    resolved_id = storage.create_incident(
        db, detector="error_rate_spike", severity="warning", summary="x",
        root_cause="y", confidence=0.5, recommended_action="z",
    )
    storage.update_incident_status(db, resolved_id, "verified")

    resolved_last_seen = storage.get_incident(db, resolved_id)["last_seen_ts"]
    storage.touch_incident(db, open_id, ts=500.0)
    storage.touch_incident(db, resolved_id, ts=500.0)

    assert storage.get_incident(db, open_id)["last_seen_ts"] == 500.0
    assert storage.get_incident(db, resolved_id)["last_seen_ts"] == resolved_last_seen  # untouched


def test_excluded_periods_ends_an_open_incident_where_its_fault_was_last_seen(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    now = time.time()
    incident_id = storage.create_incident(
        db, detector="latency_spike", severity="warning", summary="x",
        root_cause="y", confidence=0.5, recommended_action="z", ts=now - 1500,
    )
    storage.touch_incident(db, incident_id, ts=now - 1400)

    periods = storage.excluded_periods(db, lookback_s=990, detector="latency_spike", anomaly_lead_s=90)

    assert periods == [pytest.approx((now - 1500 - 90, now - 1400))]


def test_a_new_incident_that_is_never_re_fired_excludes_only_its_own_trigger_window(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    now = time.time()
    storage.create_incident(
        db, detector="latency_spike", severity="warning", summary="x",
        root_cause="y", confidence=0.5, recommended_action="z", ts=now - 900,
    )

    periods = storage.excluded_periods(db, lookback_s=990, detector="latency_spike", anomaly_lead_s=90)

    assert periods == [pytest.approx((now - 900 - 90, now - 900))]


def test_excluded_periods_falls_back_to_the_capped_range_without_a_last_seen(tmp_path):
    """A row from before the column existed has no evidence about how long the fault lasted ->
    stay conservative, but bounded: now, capped at lookback_s past its own start."""
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    now = time.time()
    storage.create_incident(
        db, detector="latency_spike", severity="warning", summary="x",
        root_cause="y", confidence=0.5, recommended_action="z", ts=now - 2000,
    )
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE incidents SET last_seen_ts = NULL")

    periods = storage.excluded_periods(db, lookback_s=990, detector="latency_spike", anomaly_lead_s=90)

    assert periods == [pytest.approx((now - 2000 - 90, now - 2000 + 990))]


def test_excluded_periods_caps_a_resolved_incident_at_its_last_seen(tmp_path):
    """Resolving an old incident stamps resolved_ts = now. Treating that as when the fault ended
    made it exclude everything since it opened, so a late resolution blinded the detector the
    same way an unresolved incident used to. The fault ended where it was last seen firing."""
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    now = time.time()
    incident_id = storage.create_incident(
        db, detector="latency_spike", severity="warning", summary="x",
        root_cause="y", confidence=0.5, recommended_action="z", ts=now - 2000,
    )
    storage.touch_incident(db, incident_id, ts=now - 1900)
    storage.update_incident_status(db, incident_id, "ignored")  # resolved_ts = now

    periods = storage.excluded_periods(db, lookback_s=990, detector="latency_spike", anomaly_lead_s=90)

    assert periods == [pytest.approx((now - 2000 - 90, now - 1900))]


def test_excluded_periods_keeps_resolved_ts_for_a_resolved_row_without_a_last_seen(tmp_path):
    db = str(tmp_path / "test.db")
    storage.init_db(db)
    now = time.time()
    incident_id = storage.create_incident(
        db, detector="latency_spike", severity="warning", summary="x",
        root_cause="y", confidence=0.5, recommended_action="z", ts=now - 100,
    )
    storage.update_incident_status(db, incident_id, "ignored")
    with sqlite3.connect(db) as conn:  # a row from before the column existed
        conn.execute("UPDATE incidents SET last_seen_ts = NULL")
    resolved_ts = storage.get_incident(db, incident_id)["resolved_ts"]

    periods = storage.excluded_periods(db, lookback_s=990, detector="latency_spike", anomaly_lead_s=90)

    assert periods == [pytest.approx((now - 100 - 90, resolved_ts))]
