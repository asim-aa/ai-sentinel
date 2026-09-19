import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

from ai_sentinel import engine, storage


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


def _seed_llm_call_latency_spike(db):
    """Produces a latency_spike anomaly whose root cause attributes to the llm_call stage
    (and so recommends fail_over) — the shared scenario for the sweep_once tests below."""
    for stage, base_ms in [("auth", 10), ("retrieval", 40), ("llm_call", 350), ("tool_call", 15)]:
        _seed(db, stage, 10, base_ms, offset_start=200, spacing=20)
    _seed(db, "chat_request", 10, 10 + 40 + 350 + 15, offset_start=200, spacing=20)

    for stage, base_ms in [("auth", 10), ("retrieval", 40), ("tool_call", 15)]:
        _seed(db, stage, 5, base_ms, offset_start=10, spacing=10)
    _seed(db, "llm_call", 5, 3500, offset_start=10, spacing=10)
    _seed(db, "chat_request", 5, 10 + 40 + 3500 + 15, offset_start=10, spacing=10)


def test_sweep_once_merges_correlated_detector_into_existing_incident(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed_llm_call_latency_spike(db)

    existing_id = storage.create_incident(
        db, detector="error_rate_spike", severity="critical", summary="errors up",
        root_cause="x", confidence=0.9, recommended_action="Fail over to backup backend",
        stage="llm_call",
    )

    with patch("ai_sentinel.alerts.emit_alert", new_callable=AsyncMock):
        created = asyncio.run(engine.sweep_once(db))

    assert created == []  # merged, not a new incident
    incident = storage.get_incident(db, existing_id)
    assert "latency_spike" in incident["merged_detectors"].split(",")
    assert incident["merged_detectors"].split(",")[0] == "error_rate_spike"  # original entry kept


def test_sweep_once_does_not_merge_outside_the_correlation_window(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed_llm_call_latency_spike(db)

    existing_id = storage.create_incident(
        db, detector="error_rate_spike", severity="critical", summary="errors up",
        root_cause="x", confidence=0.9, recommended_action="Fail over to backup backend",
        stage="llm_call", ts=time.time() - engine.CORRELATION_WINDOW_S - 30,
    )

    with patch("ai_sentinel.alerts.emit_alert", new_callable=AsyncMock):
        created = asyncio.run(engine.sweep_once(db))

    assert len(created) == 1  # too old to correlate against -> a fresh incident
    incident = storage.get_incident(db, existing_id)
    assert incident["merged_detectors"] == "error_rate_spike"  # untouched


def test_sweep_once_runs_canary_for_a_new_failover_incident(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed_llm_call_latency_spike(db)

    fake_result = {
        "current": {"backend": "primary", "avg_latency_ms": 3500, "error_rate": 0.0, "samples": 3},
        "candidate": {"backend": "backup", "avg_latency_ms": 400, "error_rate": 0.0, "samples": 3},
    }
    with patch("ai_sentinel.alerts.emit_alert", new_callable=AsyncMock), \
         patch("ai_sentinel.canary.compare_backends", new_callable=AsyncMock, return_value=fake_result) as mock_canary:
        created = asyncio.run(engine.sweep_once(db, demo_url="http://demo"))

    mock_canary.assert_called_once_with("http://demo")
    assert len(created) == 1
    assert json.loads(created[0]["canary_result"]) == fake_result


def test_sweep_once_skips_canary_without_a_demo_url(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed_llm_call_latency_spike(db)

    with patch("ai_sentinel.alerts.emit_alert", new_callable=AsyncMock), \
         patch("ai_sentinel.canary.compare_backends", new_callable=AsyncMock) as mock_canary:
        created = asyncio.run(engine.sweep_once(db))

    mock_canary.assert_not_called()
    assert created[0]["canary_result"] is None


def test_sweep_once_persists_the_diagnosis_evidence(tmp_path):
    """The RCA evidence panel reads this straight off the incident row -- it has to survive the
    trip from rootcause.diagnose()'s in-memory RootCause.evidence into storage."""
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed_llm_call_latency_spike(db)

    with patch("ai_sentinel.alerts.emit_alert", new_callable=AsyncMock):
        created = asyncio.run(engine.sweep_once(db))

    assert len(created) == 1
    evidence = json.loads(created[0]["evidence"])
    assert "recent" in evidence and "baseline" in evidence
    assert evidence["recent"]["llm_call"]["p95_ms"] > evidence["baseline"]["llm_call"]["p95_ms"]


def test_sweep_once_touches_an_open_incident_its_detector_is_still_firing_for(tmp_path):
    """A suppressed re-fire is the only evidence storage.excluded_periods gets that the fault is
    still going, so it has to be recorded on the incident that suppressed it."""
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed_llm_call_latency_spike(db)
    incident_id = storage.create_incident(
        db, detector="latency_spike", severity="critical", summary="p95 up",
        root_cause="x", confidence=0.9, recommended_action="Fail over to backup backend",
        stage="llm_call", ts=time.time() - 30,
    )
    assert storage.get_incident(db, incident_id)["last_seen_ts"] is None

    before = time.time()
    with patch("ai_sentinel.alerts.emit_alert", new_callable=AsyncMock):
        created = asyncio.run(engine.sweep_once(db))

    assert created == []  # suppressed by the cooldown, not a new incident
    assert storage.get_incident(db, incident_id)["last_seen_ts"] >= before
