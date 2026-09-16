"""Alert delivery: always logs; optionally POSTs to a webhook if ALERT_WEBHOOK_URL is set.

No real Slack/email integration in this pass — this is the seam where one would plug in later.
"""

from __future__ import annotations

import logging
import os

import httpx

from ai_sentinel.detectors import Anomaly
from ai_sentinel.remediation import Remediation
from ai_sentinel.rootcause import RootCause

log = logging.getLogger("ai_sentinel.alerts")


async def emit_alert(anomaly: Anomaly, root_cause: RootCause, remediation: Remediation) -> None:
    log.warning(
        "INCIDENT [%s/%s] %s | root cause: %s | recommended: %s",
        anomaly.severity, anomaly.detector, anomaly.summary,
        root_cause.explanation, remediation.label,
    )

    webhook_url = os.environ.get("ALERT_WEBHOOK_URL")
    if not webhook_url:
        return

    payload = {
        "detector": anomaly.detector,
        "severity": anomaly.severity,
        "summary": anomaly.summary,
        "root_cause": root_cause.explanation,
        "confidence": root_cause.confidence,
        "recommended_action": remediation.label,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(webhook_url, json=payload)
    except Exception:
        log.exception("failed to deliver alert webhook")
