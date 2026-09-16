import asyncio
import time
from unittest.mock import AsyncMock, call, patch

from ai_sentinel import blind_eval, storage


def _generic_response():
    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: {}
    return resp


def _seed_stage_set(db, stage_durations, offset, invalid_output=False):
    """One full chat_request + its child stage spans, `offset` seconds into the past."""
    t = time.time() - offset
    trace = f"trace-{t}-{offset}"
    total = 0
    for stage, dur in stage_durations.items():
        storage.insert_span(
            db, span_id=f"{stage}-{t}-{offset}", trace_id=trace, parent_id=None,
            name=stage, service_name="demo-ai-service",
            start_time=t, end_time=t + dur / 1000, duration_ms=dur,
            status="OK", attributes={},
        )
        total += dur
    storage.insert_span(
        db, span_id=f"chat-{t}-{offset}", trace_id=trace, parent_id=None,
        name="chat_request", service_name="demo-ai-service",
        start_time=t, end_time=t + total / 1000, duration_ms=total,
        status="OK", attributes={"invalid_output": invalid_output},
    )


# ---------------------------------------------------------- _shuffled_fault_cycle --

def test_shuffled_fault_cycle_covers_every_fault_before_repeating():
    cycle = blind_eval._shuffled_fault_cycle()
    first_pass = [next(cycle) for _ in range(len(blind_eval.FAULT_EXPECTATIONS))]
    assert sorted(first_pass) == sorted(blind_eval.FAULT_EXPECTATIONS)

    second_pass = [next(cycle) for _ in range(len(blind_eval.FAULT_EXPECTATIONS))]
    assert sorted(second_pass) == sorted(blind_eval.FAULT_EXPECTATIONS)  # covers again, not stuck


# ------------------------------------------------------------------ run_trial --

def test_run_trial_scores_correct_for_tool_failure(tmp_path):
    """tool_failure_rate short-circuits straight to stage="tool_call" in rootcause.py, so this
    also doubles as the simplest path to prove the "correct" classification works."""
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    fault_calls = []

    async def fake_post(url, json=None, **kwargs):
        if url.endswith("/admin/fault"):
            fault_calls.append(json["mode"])
        elif url.endswith("/chat"):
            t = time.time()
            storage.insert_span(
                db, span_id=f"tool-{t}-{id(json)}", trace_id=f"tr-{t}-{id(json)}", parent_id=None,
                name="tool_call", service_name="demo-ai-service",
                start_time=t, end_time=t + 0.02, duration_ms=20,
                status="ERROR", attributes={"failed": True},
            )
        return _generic_response()

    with patch("httpx.AsyncClient.post", side_effect=fake_post):
        result = asyncio.run(blind_eval.run_trial(db, "http://demo", "tool_failure"))

    assert result["fault_mode"] == "tool_failure"
    assert result["expected_stage"] == "tool_call"
    assert result["diagnosed_stage"] == "tool_call"
    assert result["outcome"] == "correct"
    assert fault_calls == ["tool_failure", "normal"]  # injected, then always restored after


def test_run_trial_scores_inconclusive_for_malformed_output(tmp_path):
    """A known, real gap: malformed_output corrupts response text without marking any span
    ERROR, so rootcause's generic per-stage error-rate comparison finds nothing to point at.
    The eval is supposed to surface this honestly, not hide it."""
    db = str(tmp_path / "t.db")
    storage.init_db(db)

    async def fake_post(url, json=None, **kwargs):
        if url.endswith("/chat"):
            _seed_stage_set(
                db, {"auth": 10, "retrieval": 40, "llm_call": 350, "tool_call": 15},
                offset=1, invalid_output=True,
            )
        return _generic_response()

    with patch("httpx.AsyncClient.post", side_effect=fake_post):
        result = asyncio.run(blind_eval.run_trial(db, "http://demo", "malformed_output"))

    assert result["outcome"] == "inconclusive"
    assert result["diagnosed_stage"] is None


def test_run_trial_scores_not_detected_without_baseline(tmp_path):
    """No baseline at all -> detect_latency_spike bails on the sample-count gate before it ever
    gets to compare anything, regardless of how dramatic the "recent" traffic looks."""
    db = str(tmp_path / "t.db")
    storage.init_db(db)

    async def fake_post(url, json=None, **kwargs):
        if url.endswith("/chat"):
            t = time.time()
            storage.insert_span(
                db, span_id=f"chat-{t}-{id(json)}", trace_id=f"tr-{t}-{id(json)}", parent_id=None,
                name="chat_request", service_name="demo-ai-service",
                start_time=t, end_time=t + 3.5, duration_ms=3500, status="OK", attributes={},
            )
        return _generic_response()

    with patch("httpx.AsyncClient.post", side_effect=fake_post):
        result = asyncio.run(blind_eval.run_trial(db, "http://demo", "slow_llm"))

    assert result["outcome"] == "not_detected"
    assert result["diagnosed_stage"] is None


def test_run_trial_scores_wrong_stage_when_diagnosis_disagrees(tmp_path):
    """Deliberately mismatched fixture: fault claims slow_llm (expects llm_call), but the
    seeded traffic actually has retrieval as the outlier stage -- proving the scoring logic
    correctly flags a disagreement rather than assuming the fault label is always right."""
    db = str(tmp_path / "t.db")
    storage.init_db(db)

    for i in range(6):
        _seed_stage_set(
            db, {"auth": 10, "retrieval": 40, "llm_call": 350, "tool_call": 15}, offset=200 + i * 15,
        )

    async def fake_post(url, json=None, **kwargs):
        if url.endswith("/chat"):
            _seed_stage_set(
                db, {"auth": 10, "retrieval": 1400, "llm_call": 350, "tool_call": 15}, offset=1,
            )
        return _generic_response()

    with patch("httpx.AsyncClient.post", side_effect=fake_post):
        result = asyncio.run(blind_eval.run_trial(db, "http://demo", "slow_llm"))

    assert result["expected_stage"] == "llm_call"
    assert result["diagnosed_stage"] == "retrieval"
    assert result["outcome"] == "wrong_stage"


def test_run_trial_restores_fault_to_normal_on_the_way_out(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    fault_calls = []

    async def fake_post(url, json=None, **kwargs):
        if url.endswith("/admin/fault"):
            fault_calls.append(json["mode"])
        return _generic_response()

    with patch("httpx.AsyncClient.post", side_effect=fake_post):
        asyncio.run(blind_eval.run_trial(db, "http://demo", "llm_errors"))

    assert fault_calls[0] == "llm_errors"
    assert fault_calls[-1] == "normal"


# ------------------------------------------------------------- run_blind_eval --

def test_run_blind_eval_aggregates_and_persists(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)

    fixed_trials = [
        {"fault_mode": "tool_failure", "expected_stage": "tool_call", "diagnosed_stage": "tool_call", "outcome": "correct"},
        {"fault_mode": "malformed_output", "expected_stage": "llm_call", "diagnosed_stage": None, "outcome": "inconclusive"},
        {"fault_mode": "slow_llm", "expected_stage": "llm_call", "diagnosed_stage": "retrieval", "outcome": "wrong_stage"},
        {"fault_mode": "vector_db_slow", "expected_stage": "retrieval", "diagnosed_stage": None, "outcome": "not_detected"},
    ]

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock), \
         patch("ai_sentinel.blind_eval.asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
         patch("ai_sentinel.blind_eval.run_trial", side_effect=fixed_trials) as mock_run_trial:
        result = asyncio.run(blind_eval.run_blind_eval(db, "http://demo", trial_count=4))

    assert mock_run_trial.await_count == 4
    assert result["trial_count"] == 4
    assert result["counts"] == {"correct": 1, "wrong_stage": 1, "inconclusive": 1, "not_detected": 1}
    assert result["accuracy"] == 0.25

    # warm-up settle once, then a full inter-trial gap between each of the 4 trials (3 gaps)
    assert mock_sleep.await_args_list == [
        call(blind_eval.WARMUP_SETTLE_S),
        call(blind_eval.INTER_TRIAL_GAP_S),
        call(blind_eval.INTER_TRIAL_GAP_S),
        call(blind_eval.INTER_TRIAL_GAP_S),
    ]

    stored = storage.list_blind_eval_runs(db)
    assert len(stored) == 1
    assert stored[0]["trial_count"] == 4
    assert stored[0]["accuracy"] == 0.25
    assert stored[0]["counts"] == result["counts"]
    assert stored[0]["trials"] == fixed_trials


def test_run_blind_eval_passes_a_fresh_fault_to_each_trial(tmp_path):
    """Each trial should get a different fault_mode drawn from the shuffled cycle, not the same
    one repeated -- the whole point of spacing trials apart is to cover every fault type."""
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    seen_faults = []

    async def fake_run_trial(db_path, demo_url, fault_mode):
        seen_faults.append(fault_mode)
        return {"fault_mode": fault_mode, "expected_stage": "llm_call", "diagnosed_stage": "llm_call", "outcome": "correct"}

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock), \
         patch("ai_sentinel.blind_eval.asyncio.sleep", new_callable=AsyncMock), \
         patch("ai_sentinel.blind_eval.run_trial", side_effect=fake_run_trial):
        asyncio.run(blind_eval.run_blind_eval(db, "http://demo", trial_count=len(blind_eval.FAULT_EXPECTATIONS)))

    assert sorted(seen_faults) == sorted(blind_eval.FAULT_EXPECTATIONS)


def test_run_blind_eval_accuracy_is_zero_with_no_trials(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock), \
         patch("ai_sentinel.blind_eval.asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
         patch("ai_sentinel.blind_eval.run_trial", new_callable=AsyncMock):
        result = asyncio.run(blind_eval.run_blind_eval(db, "http://demo", trial_count=0))

    assert result["trial_count"] == 0
    assert result["accuracy"] == 0.0
    mock_sleep.assert_awaited_once_with(blind_eval.WARMUP_SETTLE_S)  # warm-up still happens; no trial gaps
