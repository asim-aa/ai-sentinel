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


def test_baseline_pollution_without_exclusion_masks_a_sustained_spike(tmp_path):
    """Sanity check that the scenario below is a meaningful test of the fix: with no incident
    row to exclude the fault's own earlier history, that history pollutes the baseline enough
    that the same ongoing fault stops being detectable. This is the bug found during live
    testing — a fault sustained long enough eventually ages into its own baseline."""
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed(db, "chat_request", 6, 300, offset_start=750, spacing=25)  # genuine clean history
    _seed(db, "chat_request", 6, 3000, offset_start=200, spacing=80)  # the fault's own past
    _seed(db, "chat_request", 5, 3000, offset_start=10, spacing=8)  # the fault, still ongoing

    assert detectors.detect_latency_spike(db) is None


def test_baseline_excludes_periods_covered_by_open_incidents(tmp_path):
    """Same data as above, but with an already-fired, still-open incident covering the fault's
    earlier history — the baseline should exclude that period and detect the ongoing spike."""
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed(db, "chat_request", 6, 300, offset_start=750, spacing=25)
    _seed(db, "chat_request", 6, 3000, offset_start=200, spacing=80)
    _seed(db, "chat_request", 5, 3000, offset_start=10, spacing=8)

    storage.create_incident(
        db, detector="latency_spike", severity="critical",
        summary="earlier detection", root_cause="x", confidence=0.9,
        recommended_action="Fail over to backup backend",
        ts=time.time() - 520,
    )

    anomaly = detectors.detect_latency_spike(db)
    assert anomaly is not None
    assert anomaly.severity == "critical"


def test_open_incident_older_than_the_lookback_no_longer_blinds_the_detector(tmp_path):
    """Companion to the test above: an incident that's still 'open' but has been for far longer
    than the lookback window it's meant to protect must not keep excluding all the way to 'now'
    forever -- that swallows the entire baseline (count drops below MIN_SAMPLES) and permanently
    blinds this detector to any later, unrelated spike, not just the original one. Found live:
    incidents left open by an unattended blind-eval run silently blinded latency_spike detection
    for the rest of a ~90-minute run, on real traffic, not just in a seeded test."""
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    lookback = detectors.BASELINE_WINDOW_S + detectors.RECENT_WINDOW_S
    _seed(db, "chat_request", 6, 300, offset_start=750, spacing=25)  # clean baseline history
    _seed(db, "chat_request", 5, 3000, offset_start=10, spacing=8)  # a fresh, unrelated spike

    storage.create_incident(
        db, detector="latency_spike", severity="critical",
        summary="a much older, never-resolved incident", root_cause="x", confidence=0.9,
        recommended_action="Fail over to backup backend",
        ts=time.time() - (lookback + 1000),
    )

    anomaly = detectors.detect_latency_spike(db)
    assert anomaly is not None


def test_open_incident_whose_fault_ended_stops_excluding_clean_traffic_after_it(tmp_path):
    """The capped version still ended an open incident's exclusion at "now" until it hit the cap,
    which emptied the baseline once the incident was ~12-18 minutes old (all the *clean* traffic
    after the fault was being excluded too) and blinded the detector for ~8 minutes per incident.
    With `last_seen_ts` the exclusion ends where the fault was last seen firing, so the same
    15-minute-old incident leaves the baseline intact. Found by re-running the 50-trial blind eval
    after the cap: 84%, with every remaining miss a latency fault landing in that window."""
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed(db, "chat_request", 18, 300, offset_start=100, spacing=60)  # steady clean traffic, 1/min
    _seed(db, "chat_request", 6, 3000, offset_start=920, spacing=3)  # the fault, ~15 min ago
    _seed(db, "chat_request", 5, 3000, offset_start=10, spacing=8)  # a fresh, unrelated spike

    now = time.time()
    incident_id = storage.create_incident(
        db, detector="latency_spike", severity="critical",
        summary="an incident opened when that fault fired", root_cause="x", confidence=0.9,
        recommended_action="Fail over to backup backend", ts=now - 900,
    )
    storage.touch_incident(db, incident_id, ts=now - 800)  # last seen firing 100s after it opened

    assert detectors.detect_latency_spike(db) is not None


def test_cost_spike_detected(tmp_path):
    db = str(tmp_path / "t.db")
    storage.init_db(db)
    _seed(db, "chat_request", 10, 300, offset_start=200, spacing=20, attributes={"tokens_total": 100})
    _seed(db, "chat_request", 5, 300, offset_start=10, spacing=10, attributes={"tokens_total": 400})

    names = {a.detector for a in detectors.run_detectors(db)}
    assert "cost_spike" in names
