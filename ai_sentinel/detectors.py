"""Threshold-based failure detectors over rolling windows.

Deliberately simple and deterministic (no ML/anomaly-detection) — each rule compares a short
"recent" window against a longer "baseline" window that ends where the recent one begins, so a
real regression doesn't get diluted by mixing it into its own baseline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ai_sentinel import storage

RECENT_WINDOW_S = 90
BASELINE_WINDOW_S = 900
MIN_SAMPLES = 3


@dataclass
class Anomaly:
    detector: str
    severity: str  # "warning" | "critical"
    summary: str
    evidence: dict = field(default_factory=dict)


def _windows(db_path: str) -> tuple[dict, dict]:
    now = time.time()
    recent = storage.metrics_summary(db_path, RECENT_WINDOW_S, end_ts=now)
    baseline = storage.metrics_summary(db_path, BASELINE_WINDOW_S, end_ts=now - RECENT_WINDOW_S)
    return recent, baseline


def detect_latency_spike(db_path: str) -> Anomaly | None:
    recent, baseline = _windows(db_path)
    if recent["count"] < MIN_SAMPLES or baseline["count"] < MIN_SAMPLES or baseline["p95_ms"] <= 0:
        return None
    ratio = recent["p95_ms"] / baseline["p95_ms"]
    if ratio < 2.0:
        return None
    return Anomaly(
        detector="latency_spike",
        severity="critical" if ratio >= 3.0 else "warning",
        summary=f"p95 latency is {ratio:.1f}x baseline ({recent['p95_ms']:.0f}ms vs {baseline['p95_ms']:.0f}ms)",
        evidence={"recent": recent, "baseline": baseline, "ratio": ratio},
    )


def detect_error_rate_spike(db_path: str) -> Anomaly | None:
    recent, baseline = _windows(db_path)
    if recent["count"] < MIN_SAMPLES:
        return None
    if recent["error_rate"] < 0.2:
        return None
    return Anomaly(
        detector="error_rate_spike",
        severity="critical" if recent["error_rate"] >= 0.5 else "warning",
        summary=f"error rate is {recent['error_rate']:.0%} over the last {RECENT_WINDOW_S}s (baseline {baseline['error_rate']:.0%})",
        evidence={"recent": recent, "baseline": baseline},
    )


def detect_timeout_spike(db_path: str) -> Anomaly | None:
    recent, baseline = _windows(db_path)
    if recent["count"] < MIN_SAMPLES or recent["timeout_rate"] < 0.2:
        return None
    return Anomaly(
        detector="timeout_spike",
        severity="critical" if recent["timeout_rate"] >= 0.5 else "warning",
        summary=f"timeout rate is {recent['timeout_rate']:.0%} over the last {RECENT_WINDOW_S}s",
        evidence={"recent": recent, "baseline": baseline},
    )


def detect_invalid_output(db_path: str) -> Anomaly | None:
    recent, baseline = _windows(db_path)
    checks = storage.checks_stats(db_path, 300)
    triggered_by = None
    rate = 0.0
    if recent["count"] >= MIN_SAMPLES and recent["invalid_output_rate"] >= 0.2:
        triggered_by, rate = "live traffic", recent["invalid_output_rate"]
    elif checks["count"] >= 2 and checks["fail_rate"] >= 0.34:
        triggered_by, rate = "synthetic probe", checks["fail_rate"]
    if triggered_by is None:
        return None
    return Anomaly(
        detector="invalid_output_rate",
        severity="critical" if rate >= 0.5 else "warning",
        summary=f"invalid/malformed output rate is {rate:.0%} (via {triggered_by})",
        evidence={"recent": recent, "checks": checks},
    )


def detect_cost_spike(db_path: str) -> Anomaly | None:
    recent, baseline = _windows(db_path)
    if recent["count"] < MIN_SAMPLES or baseline["avg_tokens"] <= 0:
        return None
    ratio = recent["avg_tokens"] / baseline["avg_tokens"]
    if ratio < 1.5:
        return None
    return Anomaly(
        detector="cost_spike",
        severity="warning",
        summary=f"average tokens/request is {ratio:.1f}x baseline ({recent['avg_tokens']:.0f} vs {baseline['avg_tokens']:.0f})",
        evidence={"recent": recent, "baseline": baseline, "ratio": ratio},
    )


def detect_tool_failure_rate(db_path: str) -> Anomaly | None:
    """Tool-call failures are non-fatal (the request still returns an answer), so they never
    show up as a chat_request-level error — check the tool_call stage directly."""
    now = time.time()
    recent = storage.stage_breakdown(db_path, RECENT_WINDOW_S, ("tool_call",), end_ts=now)["tool_call"]
    if recent["count"] < MIN_SAMPLES or recent["error_rate"] < 0.2:
        return None
    return Anomaly(
        detector="tool_failure_rate",
        severity="critical" if recent["error_rate"] >= 0.5 else "warning",
        summary=f"tool-call stage is failing {recent['error_rate']:.0%} of the time over the last {RECENT_WINDOW_S}s",
        evidence={"recent": recent},
    )


DETECTORS = (
    detect_latency_spike,
    detect_error_rate_spike,
    detect_timeout_spike,
    detect_invalid_output,
    detect_cost_spike,
    detect_tool_failure_rate,
)


def run_detectors(db_path: str) -> list[Anomaly]:
    out = []
    for fn in DETECTORS:
        anomaly = fn(db_path)
        if anomaly:
            out.append(anomaly)
    return out
