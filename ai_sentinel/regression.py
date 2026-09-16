"""Turns a verified remediation into a reusable regression fixture: replaying it later re-injects
the same fault, re-applies the same fix, and re-checks the same metric actually recovers — so a
future code change that quietly breaks a previously-working fix gets caught before it ships.

Deliberately scoped to the deterministic half of the loop only (fault -> fix -> verify), not live
detection: re-deriving a diagnosis needs the detectors' real rolling-window baseline to age in
naturally (minutes), which would make "run the suite" impractical as a dashboard button. Live
detection already has its own coverage — the seeded-span unit tests in test_detectors.py and
test_rootcause.py. A regression here is instead a fast, deterministic check that a fix that used
to work still works.
"""

from __future__ import annotations

import logging
import time

import httpx

from ai_sentinel import remediation, storage
from ai_sentinel.verification import PROBE_COUNT, _send_probes, decide, measure

log = logging.getLogger("ai_sentinel.regression")


def _infer_fault_mode(db_path: str, incident_ts: float) -> str | None:
    """Best-effort: read the fault_mode attribute off whichever chat_request span is closest to
    when the incident was raised, so a replay can reproduce the same failure condition."""
    spans = storage.spans_since(db_path, incident_ts - 60, incident_ts + 5, name="chat_request")
    if not spans:
        return None
    closest = min(spans, key=lambda s: abs(s["start_time"] - incident_ts))
    mode = closest["attributes"].get("fault_mode")
    return mode if mode and mode != "normal" else None


def record_regression(db_path: str, incident_id: int) -> int | None:
    """Call right after an incident is marked 'verified'. No-op for anything else (a rolled-back
    remediation didn't actually fix anything — not something worth replaying as a known-good
    fix)."""
    incident = storage.get_incident(db_path, incident_id)
    if not incident:
        return None
    run = storage.latest_remediation_run(db_path, incident_id)
    if not run or run["outcome"] != "verified":
        return None

    return storage.upsert_regression(
        db_path,
        detector=incident["detector"],
        stage=incident["stage"],
        fault_mode=_infer_fault_mode(db_path, incident["ts"]),
        summary=incident["summary"],
        root_cause=incident["root_cause"],
        action=run["action"],
        metric=run["metric"],
        before_value=run["before_value"],
        after_value=run["after_value"],
        source_incident_id=incident_id,
    )


async def _restore(client: httpx.AsyncClient, demo_url: str, state: dict) -> None:
    await client.post(f"{demo_url}/admin/fault", json={"mode": state["fault_mode"]})
    await client.post(f"{demo_url}/admin/backend", json={"backend": state["active_backend"]})
    await client.post(f"{demo_url}/admin/retrieval", json={"enabled": state["retrieval_enabled"]})
    await client.post(f"{demo_url}/admin/tools", json={"enabled": state["tools_enabled"]})


async def replay_regression(db_path: str, demo_url: str, regression: dict) -> dict:
    """Re-injects the stored fault, re-applies the stored fix, and checks the stored metric
    recovers the same way live verification checks it (reuses the same measure()/decide() pair) —
    then always restores the demo service to whatever state it was in before this ran."""
    base = {
        "regression_id": regression["id"], "detector": regression["detector"],
        "stage": regression["stage"], "action": regression["action"],
    }
    if not regression["fault_mode"]:
        detail = "no fault mode recorded for this signature — can't replay automatically"
        storage.record_regression_run(db_path, regression["id"], passed=None, detail=detail)
        return {**base, "passed": None, "detail": detail}

    async with httpx.AsyncClient(timeout=15.0) as client:
        original_state = (await client.get(f"{demo_url}/admin/state")).json()
        try:
            await client.post(f"{demo_url}/admin/fault", json={"mode": regression["fault_mode"]})

            # Unlike live verification, neither phase here has pre-existing organic history to
            # lean on — both "before" and "after" are probes we just sent, back to back. Window
            # each measurement to only the phase that produced it (not a shared >=5s floor) or the
            # "after" window's minimum reaches backward into the still-fresh "before" probes and
            # silently re-includes the pre-fix latency in the post-fix reading.
            before_started_at = time.time()
            await _send_probes(demo_url, PROBE_COUNT)
            before_window_s = max(0.5, time.time() - before_started_at)
            before_value, _ = measure(
                db_path, regression["detector"], regression["metric"], before_window_s, end_ts=time.time()
            )

            after_started_at = time.time()
            await remediation.execute(regression["action"], demo_url)
            await _send_probes(demo_url, PROBE_COUNT)
            after_window_s = max(0.5, time.time() - after_started_at)
            after_value, after_count = measure(
                db_path, regression["detector"], regression["metric"], after_window_s, end_ts=time.time()
            )

            outcome, detail = decide(before_value, after_value, after_count)
        finally:
            await _restore(client, demo_url, original_state)

    passed = outcome == "verified"
    storage.record_regression_run(db_path, regression["id"], passed=passed, detail=detail)
    return {**base, "passed": passed, "before": before_value, "after": after_value, "detail": detail}


async def run_regression_suite(db_path: str, demo_url: str) -> list[dict]:
    results = []
    for reg in storage.list_regressions(db_path):
        try:
            results.append(await replay_regression(db_path, demo_url, reg))
        except Exception as exc:
            log.exception("regression replay failed for #%s", reg["id"])
            detail = f"replay error: {exc}"
            storage.record_regression_run(db_path, reg["id"], passed=False, detail=detail)
            results.append({
                "regression_id": reg["id"], "detector": reg["detector"], "stage": reg["stage"],
                "action": reg["action"], "passed": False, "detail": detail,
            })
    return results
