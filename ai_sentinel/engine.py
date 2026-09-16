"""Ties the pieces together: detect -> diagnose -> recommend -> record -> alert.

Runs as a periodic background sweep in the dashboard process. One incident per detector per
cooldown window, so a sustained problem doesn't spam the feed with duplicates.
"""

from __future__ import annotations

import asyncio
import logging

from ai_sentinel import alerts, detectors, remediation, rootcause, storage

log = logging.getLogger("ai_sentinel.engine")

INCIDENT_COOLDOWN_S = 180


async def sweep_once(db_path: str) -> list[dict]:
    created = []
    for anomaly in detectors.run_detectors(db_path):
        if storage.open_incident_for_detector(db_path, anomaly.detector, cooldown_s=INCIDENT_COOLDOWN_S):
            continue

        cause = rootcause.diagnose(db_path, anomaly)
        action = remediation.recommend(cause)
        incident_id = storage.create_incident(
            db_path,
            detector=anomaly.detector,
            severity=anomaly.severity,
            summary=anomaly.summary,
            root_cause=cause.explanation,
            confidence=cause.confidence,
            recommended_action=action.label,
        )
        await alerts.emit_alert(anomaly, cause, action)
        created.append(storage.get_incident(db_path, incident_id))
    return created


async def sweep_loop(db_path: str, interval_s: float = 15.0) -> None:
    while True:
        try:
            await sweep_once(db_path)
        except Exception:
            log.exception("detector sweep error")
        await asyncio.sleep(interval_s)
