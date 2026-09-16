import asyncio
import time
from unittest.mock import AsyncMock, patch

from ai_sentinel import storage, verification


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


def test_decide_marks_verified_when_metric_improves():
    outcome, detail = verification.decide(before_value=4000, after_value=500, after_count=5)
    assert outcome == "verified"
    assert "improved" in detail


def test_decide_rolls_back_when_metric_does_not_improve():
    outcome, detail = verification.decide(before_value=4000, after_value=3800, after_count=5)
    assert outcome == "rolled_back"
    assert "did not improve" in detail


def test_decide_verified_when_too_few_after_samples():
    """Not enough post-remediation data to judge -> don't roll back on a guess."""
    outcome, detail = verification.decide(before_value=4000, after_value=100, after_count=1)
    assert outcome == "verified"
    assert "not enough" in detail


def test_decide_verified_when_baseline_already_zero():
    outcome, detail = verification.decide(before_value=0, after_value=0, after_count=5)
    assert outcome == "verified"


def test_decide_boundary_at_improvement_ratio():
    # exactly at the 0.75 threshold counts as improved
    outcome, _ = verification.decide(before_value=100, after_value=75, after_count=5)
    assert outcome == "verified"
    # just past it does not
    outcome, _ = verification.decide(before_value=100, after_value=76, after_count=5)
    assert outcome == "rolled_back"


def test_metric_for_tool_failure_uses_stage_error_rate():
    assert verification._metric_for("tool_failure_rate") == "error_rate"
    assert verification._metric_for("latency_spike") == "p95_ms"
    assert verification._metric_for("cost_spike") == "avg_tokens"
    assert verification._metric_for("some_unknown_detector") == "error_rate"


def test_verify_and_rollback_marks_verified_on_real_improvement(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)

    # "before": the incident's own recent window shows bad latency
    _seed(db, "chat_request", 5, 4000, offset_start=10, spacing=5)

    incident_id = storage.create_incident(
        db, detector="latency_spike", severity="critical", summary="p95 latency is 8x baseline",
        root_cause="LLM provider call is the outlier stage", confidence=0.9,
        recommended_action="Fail over to backup backend",
    )

    async def fake_probe_response(url, json=None):
        # simulate a probe landing a real, fast span, without needing a live demo service
        t = time.time()
        storage.insert_span(
            db, span_id=f"after-{t}-{id(json)}", trace_id=f"trace-after-{t}-{id(json)}", parent_id=None,
            name="chat_request", service_name="demo-ai-service",
            start_time=t, end_time=t + 0.4, duration_ms=400, status="OK", attributes={},
        )
        return AsyncMock(status_code=200)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=fake_probe_response), \
         patch("ai_sentinel.remediation.rollback", new_callable=AsyncMock) as mock_rollback:
        asyncio.run(verification.verify_and_rollback_if_needed(incident_id, "fail_over", "http://demo", db))

    incident = storage.get_incident(db, incident_id)
    assert incident["status"] == "verified"
    mock_rollback.assert_not_called()

    run = storage.latest_remediation_run(db, incident_id)
    assert run["outcome"] == "verified"
    assert run["after_value"] < run["before_value"]


def test_verify_and_rollback_reverts_when_nothing_improved(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)

    _seed(db, "chat_request", 5, 4000, offset_start=10, spacing=5)

    incident_id = storage.create_incident(
        db, detector="latency_spike", severity="critical", summary="p95 latency is 8x baseline",
        root_cause="LLM provider call is the outlier stage", confidence=0.9,
        recommended_action="Fail over to backup backend",
    )

    async def fake_probe_still_slow(url, json=None):
        t = time.time()
        storage.insert_span(
            db, span_id=f"after-slow-{t}-{id(json)}", trace_id=f"trace-after-slow-{t}-{id(json)}", parent_id=None,
            name="chat_request", service_name="demo-ai-service",
            start_time=t, end_time=t + 3.9, duration_ms=3900, status="OK", attributes={},
        )
        return AsyncMock(status_code=200)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=fake_probe_still_slow), \
         patch("ai_sentinel.remediation.rollback", new_callable=AsyncMock) as mock_rollback:
        asyncio.run(verification.verify_and_rollback_if_needed(incident_id, "fail_over", "http://demo", db))

    incident = storage.get_incident(db, incident_id)
    assert incident["status"] == "rolled_back"
    mock_rollback.assert_called_once_with("fail_over", "http://demo")

    run = storage.latest_remediation_run(db, incident_id)
    assert run["outcome"] == "rolled_back"


def test_verify_and_rollback_unknown_incident_is_a_noop(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        asyncio.run(verification.verify_and_rollback_if_needed(999, "fail_over", "http://demo", db))

    mock_post.assert_not_called()
