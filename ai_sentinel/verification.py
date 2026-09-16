"""Verifies that an executed remediation actually helped, and rolls it back if not.

Reuses the same discipline the detectors use — don't trust a single sample, compare recent to a
reference point — just aimed at the action itself: "before" is the incident's own recent window
at the moment you clicked; "after" is everything since, seeded with a burst of active probes so
verification doesn't sit around waiting for organic traffic.
"""

from __future__ import annotations

import logging
import time

import httpx

from ai_sentinel import remediation, storage
from ai_sentinel.detectors import RECENT_WINDOW_S

log = logging.getLogger("ai_sentinel.verification")

PROBE_COUNT = 5
PROBE_PROMPT = "Summarize the latest quarterly report in one sentence."
MIN_AFTER_SAMPLES = 2
IMPROVEMENT_RATIO = 0.75  # "after" must drop to at most 75% of "before" to count as improved

_METRIC_BY_DETECTOR = {
    "latency_spike": "p95_ms",
    "timeout_spike": "timeout_rate",
    "error_rate_spike": "error_rate",
    "cost_spike": "avg_tokens",
    "invalid_output_rate": "invalid_output_rate",
}
_DEFAULT_METRIC = "error_rate"


def _metric_for(detector: str) -> str:
    # tool_failure_rate isn't a chat_request-level metric (tool failures are non-fatal — same
    # reason the detector itself checks the tool_call stage directly, see detectors.py)
    if detector == "tool_failure_rate":
        return "error_rate"
    return _METRIC_BY_DETECTOR.get(detector, _DEFAULT_METRIC)


def measure(db_path: str, detector: str, metric: str, window_s: float, end_ts: float | None = None) -> tuple[float, int]:
    """Current value of `metric` for `detector` over the trailing `window_s`, plus the sample
    count it's based on. Shared by live verification (before/after around a real remediation) and
    regression replay (before/after around a replayed one) so both measure the same way."""
    now = end_ts if end_ts is not None else time.time()
    if detector == "tool_failure_rate":
        stage = storage.stage_breakdown(db_path, window_s, ("tool_call",), end_ts=now)["tool_call"]
        return stage["error_rate"], stage["count"]
    summary = storage.metrics_summary(db_path, window_s, end_ts=now)
    return summary.get(metric, 0.0), summary["count"]


def decide(before_value: float, after_value: float, after_count: int) -> tuple[str, str]:
    """Pure decision logic, kept separate from the I/O around it so it's directly testable."""
    if after_count < MIN_AFTER_SAMPLES:
        return (
            "verified",
            f"only {after_count} post-remediation sample(s) — not enough to fully verify, not rolled back",
        )
    if before_value <= 0:
        return "verified", "baseline was already at zero"
    if after_value <= before_value * IMPROVEMENT_RATIO:
        return "verified", f"{before_value:.2f} -> {after_value:.2f} (improved)"
    return "rolled_back", f"{before_value:.2f} -> {after_value:.2f} (did not improve; reverted)"


async def _send_probes(demo_url: str, count: int) -> None:
    async with httpx.AsyncClient(timeout=15.0) as client:
        for _ in range(count):
            try:
                await client.post(f"{demo_url}/chat", json={"prompt": PROBE_PROMPT})
            except Exception:
                log.warning("a verification probe failed to reach the demo service")


async def verify_and_rollback_if_needed(
    incident_id: int, action: str, demo_url: str, db_path: str
) -> None:
    incident = storage.get_incident(db_path, incident_id)
    if not incident:
        return

    detector = incident["detector"]
    metric = _metric_for(detector)

    before_value, _ = measure(db_path, detector, metric, RECENT_WINDOW_S, end_ts=time.time())

    started_at = time.time()
    run_id = storage.create_remediation_run(
        db_path, incident_id=incident_id, action=action, metric=metric,
        before_value=before_value, started_at=started_at,
    )

    await _send_probes(demo_url, PROBE_COUNT)

    after_window_s = max(5.0, time.time() - started_at)
    after_value, after_count = measure(db_path, detector, metric, after_window_s, end_ts=time.time())

    outcome, detail = decide(before_value, after_value, after_count)

    if outcome == "rolled_back":
        try:
            await remediation.rollback(action, demo_url)
        except Exception:
            log.exception("rollback failed for incident %s", incident_id)
            detail += " (rollback attempt itself failed — check the demo service manually)"

    storage.finish_remediation_run(db_path, run_id, after_value=after_value, outcome=outcome, detail=detail)
    storage.update_incident_status(db_path, incident_id, outcome)
