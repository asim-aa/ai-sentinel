import asyncio
import time
from unittest.mock import AsyncMock, patch

from ai_sentinel import regression, storage


def _admin_state_response(**overrides):
    state = {"fault_mode": "normal", "active_backend": "primary", "retrieval_enabled": True, "tools_enabled": True}
    state.update(overrides)
    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: state
    return resp


def _generic_response():
    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: {}
    return resp


# --------------------------------------------------------- _infer_fault_mode --

def test_infer_fault_mode_reads_the_closest_span(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    now = time.time()
    storage.insert_span(
        db, span_id="s1", trace_id="t1", parent_id=None, name="chat_request",
        service_name="demo-ai-service", start_time=now - 3, end_time=now - 2.5,
        duration_ms=500, status="OK", attributes={"fault_mode": "slow_llm"},
    )
    assert regression._infer_fault_mode(db, now) == "slow_llm"


def test_infer_fault_mode_ignores_normal(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    now = time.time()
    storage.insert_span(
        db, span_id="s1", trace_id="t1", parent_id=None, name="chat_request",
        service_name="demo-ai-service", start_time=now - 3, end_time=now - 2.5,
        duration_ms=500, status="OK", attributes={"fault_mode": "normal"},
    )
    assert regression._infer_fault_mode(db, now) is None


def test_infer_fault_mode_none_without_spans(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    assert regression._infer_fault_mode(db, time.time()) is None


# ------------------------------------------------------------ record_regression --

def test_record_regression_upserts_after_a_verified_run(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    now = time.time()
    storage.insert_span(
        db, span_id="s1", trace_id="t1", parent_id=None, name="chat_request",
        service_name="demo-ai-service", start_time=now - 5, end_time=now - 4.5,
        duration_ms=3500, status="OK", attributes={"fault_mode": "slow_llm"},
    )
    incident_id = storage.create_incident(
        db, detector="latency_spike", severity="critical", summary="p95 latency is 8x baseline",
        root_cause="LLM provider call is the outlier stage", confidence=0.9,
        recommended_action="Fail over to backup backend", stage="llm_call", ts=now - 5,
    )
    run_id = storage.create_remediation_run(
        db, incident_id=incident_id, action="fail_over", metric="p95_ms",
        before_value=3500.0, started_at=now,
    )
    storage.finish_remediation_run(db, run_id, after_value=420.0, outcome="verified", detail="improved")

    reg_id = regression.record_regression(db, incident_id)

    assert reg_id is not None
    reg = storage.get_regression(db, reg_id)
    assert reg["detector"] == "latency_spike"
    assert reg["stage"] == "llm_call"
    assert reg["fault_mode"] == "slow_llm"
    assert reg["action"] == "fail_over"
    assert reg["source_incident_id"] == incident_id


def test_record_regression_noop_when_not_verified(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    incident_id = storage.create_incident(
        db, detector="latency_spike", severity="critical", summary="x", root_cause="y",
        confidence=0.9, recommended_action="Fail over to backup backend", stage="llm_call",
    )
    run_id = storage.create_remediation_run(
        db, incident_id=incident_id, action="fail_over", metric="p95_ms",
        before_value=3500.0, started_at=time.time(),
    )
    storage.finish_remediation_run(db, run_id, after_value=3800.0, outcome="rolled_back", detail="did not improve")

    assert regression.record_regression(db, incident_id) is None
    assert storage.list_regressions(db) == []


def test_record_regression_noop_without_a_remediation_run(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    incident_id = storage.create_incident(
        db, detector="latency_spike", severity="critical", summary="x", root_cause="y",
        confidence=0.9, recommended_action="Fail over to backup backend", stage="llm_call",
    )
    assert regression.record_regression(db, incident_id) is None


def test_record_regression_noop_for_unknown_incident(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    assert regression.record_regression(db, 999) is None


# ------------------------------------------------------------ replay_regression --

def test_replay_regression_without_fault_mode_is_a_noop(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    reg_id = storage.upsert_regression(
        db, detector="cost_spike", stage="llm_call", fault_mode=None,
        summary="x", root_cause="y", action="fail_over", metric="avg_tokens",
        before_value=50.0, after_value=10.0, source_incident_id=1,
    )
    reg = storage.get_regression(db, reg_id)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        result = asyncio.run(regression.replay_regression(db, "http://demo", reg))

    mock_get.assert_not_called()
    assert result["passed"] is None
    assert "can't replay" in result["detail"]
    stored = storage.get_regression(db, reg_id)
    assert stored["last_run_passed"] is None
    assert stored["last_run_detail"] == result["detail"]


def test_replay_regression_passes_when_the_fix_still_works(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    reg_id = storage.upsert_regression(
        db, detector="latency_spike", stage="llm_call", fault_mode="slow_llm",
        summary="x", root_cause="y", action="fail_over", metric="p95_ms",
        before_value=3500.0, after_value=420.0, source_incident_id=1,
    )
    reg = storage.get_regression(db, reg_id)
    executed = []

    async def fake_get(url, **kwargs):
        return _admin_state_response()

    async def fake_post(url, json=None, **kwargs):
        if url.endswith("/chat"):
            now = time.time()
            duration = 3500 if not executed else 400  # slow before the fix, fast after it
            storage.insert_span(
                db, span_id=f"s-{now}-{id(json)}", trace_id=f"tr-{now}-{id(json)}", parent_id=None,
                name="chat_request", service_name="demo-ai-service",
                start_time=now, end_time=now + duration / 1000, duration_ms=duration,
                status="OK", attributes={},
            )
        return _generic_response()

    async def fake_execute(action, demo_url):
        # Mocked I/O returns near-instantly, so without a real gap here the before- and
        # after-measurement windows (each floored at 0.5s) would overlap and the after reading
        # would still see the slow before-phase spans mixed in. A short real sleep gives the two
        # phases honest separation, the way real remediation latency would in production.
        await asyncio.sleep(0.8)
        executed.append(action)
        return {}

    with patch("httpx.AsyncClient.get", side_effect=fake_get), \
         patch("httpx.AsyncClient.post", side_effect=fake_post), \
         patch("ai_sentinel.remediation.execute", side_effect=fake_execute) as mock_execute:
        result = asyncio.run(regression.replay_regression(db, "http://demo", reg))

    mock_execute.assert_called_once_with("fail_over", "http://demo")
    assert result["passed"] is True
    stored = storage.get_regression(db, reg_id)
    assert stored["last_run_passed"] == 1


def test_replay_regression_fails_when_the_fix_no_longer_helps(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    reg_id = storage.upsert_regression(
        db, detector="latency_spike", stage="llm_call", fault_mode="slow_llm",
        summary="x", root_cause="y", action="fail_over", metric="p95_ms",
        before_value=3500.0, after_value=420.0, source_incident_id=1,
    )
    reg = storage.get_regression(db, reg_id)

    async def fake_get(url, **kwargs):
        return _admin_state_response()

    async def fake_post(url, json=None, **kwargs):
        if url.endswith("/chat"):
            now = time.time()
            storage.insert_span(  # stays slow both times -- the fix no longer helps
                db, span_id=f"s-{now}-{id(json)}", trace_id=f"tr-{now}-{id(json)}", parent_id=None,
                name="chat_request", service_name="demo-ai-service",
                start_time=now, end_time=now + 3.5, duration_ms=3500, status="OK", attributes={},
            )
        return _generic_response()

    with patch("httpx.AsyncClient.get", side_effect=fake_get), \
         patch("httpx.AsyncClient.post", side_effect=fake_post), \
         patch("ai_sentinel.remediation.execute", new_callable=AsyncMock):
        result = asyncio.run(regression.replay_regression(db, "http://demo", reg))

    assert result["passed"] is False
    assert storage.get_regression(db, reg_id)["last_run_passed"] == 0


def test_replay_regression_restores_original_state_even_on_error(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    reg_id = storage.upsert_regression(
        db, detector="latency_spike", stage="llm_call", fault_mode="slow_llm",
        summary="x", root_cause="y", action="fail_over", metric="p95_ms",
        before_value=3500.0, after_value=420.0, source_incident_id=1,
    )
    reg = storage.get_regression(db, reg_id)
    admin_calls = []

    async def fake_get(url, **kwargs):
        return _admin_state_response(active_backend="backup", retrieval_enabled=False)

    async def fake_post(url, json=None, **kwargs):
        if "/admin/" in url:
            admin_calls.append((url, json))
        return _generic_response()

    async def failing_execute(action, demo_url):
        raise RuntimeError("boom")

    with patch("httpx.AsyncClient.get", side_effect=fake_get), \
         patch("httpx.AsyncClient.post", side_effect=fake_post), \
         patch("ai_sentinel.remediation.execute", side_effect=failing_execute):
        raised = False
        try:
            asyncio.run(regression.replay_regression(db, "http://demo", reg))
        except RuntimeError:
            raised = True

    assert raised  # the error isn't swallowed here -- run_regression_suite handles it
    assert ("http://demo/admin/backend", {"backend": "backup"}) in admin_calls
    assert ("http://demo/admin/retrieval", {"enabled": False}) in admin_calls
    assert ("http://demo/admin/tools", {"enabled": True}) in admin_calls
    assert ("http://demo/admin/fault", {"mode": "normal"}) in admin_calls  # restored, not left on slow_llm


# ------------------------------------------------------------- run_regression_suite --

def test_run_regression_suite_aggregates_results_and_survives_a_replay_error(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    storage.upsert_regression(
        db, detector="cost_spike", stage="llm_call", fault_mode=None,
        summary="x", root_cause="y", action="fail_over", metric="avg_tokens",
        before_value=50.0, after_value=10.0, source_incident_id=1,
    )
    storage.upsert_regression(
        db, detector="latency_spike", stage="llm_call", fault_mode="slow_llm",
        summary="x", root_cause="y", action="fail_over", metric="p95_ms",
        before_value=3500.0, after_value=420.0, source_incident_id=2,
    )

    async def fake_get(url, **kwargs):
        return _admin_state_response()

    async def fake_post(url, json=None, **kwargs):
        return _generic_response()

    async def failing_execute(action, demo_url):
        raise RuntimeError("boom")

    with patch("httpx.AsyncClient.get", side_effect=fake_get), \
         patch("httpx.AsyncClient.post", side_effect=fake_post), \
         patch("ai_sentinel.remediation.execute", side_effect=failing_execute):
        results = asyncio.run(regression.run_regression_suite(db, "http://demo"))

    assert len(results) == 2
    no_fault_result = next(r for r in results if r["detector"] == "cost_spike")
    assert no_fault_result["passed"] is None

    latency_result = next(r for r in results if r["detector"] == "latency_spike")
    assert latency_result["passed"] is False
    assert "replay error" in latency_result["detail"]
    assert storage.get_regression(db, latency_result["regression_id"])["last_run_passed"] == 0
