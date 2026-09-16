"""Alert delivery: always logs; optionally delivers to Slack and/or a generic webhook.

Slack (SLACK_WEBHOOK_URL): a proper Block Kit message with a severity color bar, matching the
dashboard's own color coding. Generic webhook (ALERT_WEBHOOK_URL): raw JSON, for anything else
that can consume a POST. Either, both, or neither can be configured — each delivery path is
independent, so one failing doesn't block the other. ALERT_MIN_SEVERITY (default "warning", i.e.
everything) can be set to "critical" to cut down on notification volume.
"""

from __future__ import annotations

import logging
import os

import httpx

from ai_sentinel.detectors import Anomaly
from ai_sentinel.remediation import Remediation
from ai_sentinel.rootcause import RootCause

log = logging.getLogger("ai_sentinel.alerts")

_SEVERITY_RANK = {"warning": 0, "critical": 1}
_SEVERITY_COLOR = {"warning": "#f5c344", "critical": "#f5556c"}  # matches the dashboard's own palette


def _slack_payload(anomaly: Anomaly, root_cause: RootCause, remediation: Remediation) -> dict:
    return {
        "text": f"[{anomaly.severity.upper()}] {anomaly.detector}: {anomaly.summary}",  # notification fallback
        "attachments": [
            {
                "color": _SEVERITY_COLOR.get(anomaly.severity, "#8b98a9"),
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{anomaly.severity.upper()} · {anomaly.detector}*\n{anomaly.summary}",
                        },
                    },
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": root_cause.explanation},
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": (
                                    f"Confidence: {root_cause.confidence:.0%}  ·  "
                                    f"Recommended: *{remediation.label}*"
                                ),
                            }
                        ],
                    },
                ],
            }
        ],
    }


def _generic_payload(anomaly: Anomaly, root_cause: RootCause, remediation: Remediation) -> dict:
    return {
        "detector": anomaly.detector,
        "severity": anomaly.severity,
        "summary": anomaly.summary,
        "root_cause": root_cause.explanation,
        "confidence": root_cause.confidence,
        "recommended_action": remediation.label,
    }


async def _deliver(client: httpx.AsyncClient, url: str, payload: dict, label: str) -> None:
    try:
        resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            log.warning("%s webhook returned %s: %s", label, resp.status_code, resp.text[:200])
    except Exception:
        log.exception("failed to deliver %s webhook", label)


async def emit_alert(anomaly: Anomaly, root_cause: RootCause, remediation: Remediation) -> None:
    log.warning(
        "INCIDENT [%s/%s] %s | root cause: %s | recommended: %s",
        anomaly.severity, anomaly.detector, anomaly.summary,
        root_cause.explanation, remediation.label,
    )

    min_severity = os.environ.get("ALERT_MIN_SEVERITY", "warning")
    if _SEVERITY_RANK.get(anomaly.severity, 0) < _SEVERITY_RANK.get(min_severity, 0):
        return

    slack_url = os.environ.get("SLACK_WEBHOOK_URL")
    webhook_url = os.environ.get("ALERT_WEBHOOK_URL")
    if not slack_url and not webhook_url:
        return

    async with httpx.AsyncClient(timeout=5.0) as client:
        if slack_url:
            await _deliver(client, slack_url, _slack_payload(anomaly, root_cause, remediation), "Slack")
        if webhook_url:
            await _deliver(client, webhook_url, _generic_payload(anomaly, root_cause, remediation), "generic")
