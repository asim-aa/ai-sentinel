"""Ties the pieces together: detect -> diagnose -> recommend -> record -> alert.

Runs as a periodic background sweep in the dashboard process. One incident per detector per
cooldown window, so a sustained problem doesn't spam the feed with duplicates.
"""

from __future__ import annotations

import asyncio
import json
import logging

from ai_sentinel import alerts, canary, detectors, remediation, rootcause, storage

log = logging.getLogger("ai_sentinel.engine")

INCIDENT_COOLDOWN_S = 180
# Two detectors firing for the same root-cause stage within this window are almost certainly the
# same underlying problem seen two ways (e.g. latency_spike and error_rate_spike both on llm_call)
# — merge into one incident instead of paging on-call twice for it.
CORRELATION_WINDOW_S = 60


async def sweep_once(db_path: str, demo_url: str | None = None) -> list[dict]:
    created = []
    for anomaly in detectors.run_detectors(db_path):
        if storage.open_incident_for_detector(db_path, anomaly.detector, cooldown_s=INCIDENT_COOLDOWN_S):
            continue

        cause = rootcause.diagnose(db_path, anomaly)

        existing = storage.open_incident_for_stage(db_path, cause.stage, cooldown_s=CORRELATION_WINDOW_S)
        if existing:
            storage.merge_detector_into_incident(db_path, existing["id"], anomaly.detector)
            continue

        action = remediation.recommend(cause)

        # For a fresh fail_over-recommended incident, shadow-test the alternative backend so the
        # recommendation carries real expected-impact numbers, not just "this might help" — kept
        # to new incidents only since this is one comparison, not a per-sweep re-check.
        canary_result = None
        if action.action == "fail_over" and demo_url:
            result = await canary.compare_backends(demo_url)
            if result:
                canary_result = json.dumps(result)

        incident_id = storage.create_incident(
            db_path,
            detector=anomaly.detector,
            severity=anomaly.severity,
            summary=anomaly.summary,
            root_cause=cause.explanation,
            confidence=cause.confidence,
            recommended_action=action.label,
            stage=cause.stage,
            canary_result=canary_result,
            evidence=cause.evidence,
        )
        await alerts.emit_alert(anomaly, cause, action)
        created.append(storage.get_incident(db_path, incident_id))
    return created


async def sweep_loop(db_path: str, demo_url: str | None = None, interval_s: float = 15.0) -> None:
    while True:
        try:
            await sweep_once(db_path, demo_url)
        except Exception:
            log.exception("detector sweep error")
        await asyncio.sleep(interval_s)
