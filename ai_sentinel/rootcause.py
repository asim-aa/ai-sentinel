"""Root-cause correlation: given a detected anomaly, which pipeline stage actually caused it?

Compares per-stage p95 latency and error rate (recent window vs. baseline) and picks whichever
stage deviates the most, provided it's a clear enough outlier and not just noise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ai_sentinel import storage
from ai_sentinel.detectors import BASELINE_WINDOW_S, RECENT_WINDOW_S, Anomaly

STAGES = ("auth", "retrieval", "llm_call", "tool_call")
MIN_MEANINGFUL_LATENCY_MS = 50  # ignore ratio noise on stages too fast to matter (e.g. auth)
DEPLOY_CORRELATION_WINDOW_S = 120  # flag an incident as possibly deploy-related within this long of a restart

_STAGE_LABELS = {
    "auth": "authentication",
    "retrieval": "vector DB retrieval",
    "llm_call": "LLM provider call",
    "tool_call": "tool call",
}


@dataclass
class RootCause:
    stage: str | None
    explanation: str
    confidence: float
    evidence: dict = field(default_factory=dict)


def _stage_windows(db_path: str, detector: str) -> tuple[dict, dict]:
    """Same recent-vs-baseline split as detectors._windows, but broken down per pipeline stage
    instead of aggregated — and with the same baseline-exclusion fix, so a root cause diagnosed
    on a later sweep of a sustained fault still compares against a clean baseline."""
    now = time.time()
    recent = storage.stage_breakdown(db_path, RECENT_WINDOW_S, STAGES, end_ts=now)
    periods = storage.excluded_periods(
        db_path, BASELINE_WINDOW_S + RECENT_WINDOW_S, detector, anomaly_lead_s=RECENT_WINDOW_S
    )
    baseline = storage.stage_breakdown(
        db_path, BASELINE_WINDOW_S, STAGES, end_ts=now - RECENT_WINDOW_S, exclude_periods=periods
    )
    return recent, baseline


def _inconclusive(recent: dict, baseline: dict) -> RootCause:
    return RootCause(
        stage=None,
        explanation="Degradation isn't clearly isolated to one stage — recommend manual investigation.",
        confidence=0.3,
        evidence={"recent": recent, "baseline": baseline},
    )


def _deployment_note(db_path: str) -> str | None:
    """If the service restarted recently, say so — a fresh deploy is a common, easily-overlooked
    explanation for a sudden regression, and the root-cause explanation is the natural place to
    surface it since that's what an on-call reader checks first."""
    now = time.time()
    recent = storage.spans_since(db_path, now - 30, now, name="chat_request")
    if not recent:
        return None
    latest = recent[-1]
    started_at = latest["attributes"].get("service_started_at")
    svc_version = latest["attributes"].get("service_version")
    if started_at is None:
        return None
    uptime = now - started_at
    if uptime <= DEPLOY_CORRELATION_WINDOW_S:
        return f"The service restarted {uptime:.0f}s ago (version {svc_version}) — this may be related to that deploy."
    return None


def diagnose(db_path: str, anomaly: Anomaly) -> RootCause:
    cause = _diagnose_inner(db_path, anomaly)
    note = _deployment_note(db_path)
    if note:
        cause.explanation = f"{cause.explanation} {note}"
    return cause


def _diagnose_inner(db_path: str, anomaly: Anomaly) -> RootCause:
    if anomaly.detector == "tool_failure_rate":
        r = storage.stage_breakdown(db_path, RECENT_WINDOW_S, ("tool_call",))["tool_call"]
        return RootCause(
            stage="tool_call",
            explanation=(
                f"Tool-call stage is failing directly ({r['error_rate']:.0%} of calls over the last "
                f"{RECENT_WINDOW_S}s) — likely a downstream tool/integration issue, not the model itself."
            ),
            confidence=0.9,
            evidence={"recent": r},
        )

    if anomaly.detector == "cost_spike":
        # Only the LLM call stage produces tokens here (retrieval/auth/tool don't), so there's no
        # real per-stage attribution question to answer — skip the latency-ratio machinery below,
        # which isn't a meaningful signal for a token-cost anomaly.
        return RootCause(
            stage="llm_call",
            explanation=(
                f"Token usage is elevated on the LLM call stage ({anomaly.summary}). Retrieval, auth, "
                f"and tool calls don't consume tokens in this pipeline, so cost is definitionally "
                f"coming from the LLM call — check for longer prompts/context or verbose responses."
            ),
            confidence=0.85,
            evidence={"anomaly_evidence": anomaly.evidence},
        )

    if anomaly.detector == "invalid_output_rate":
        # Same reasoning as cost_spike above: only the LLM call stage ever produces the response
        # text, so a malformed/invalid answer is definitionally coming from there. The generic
        # per-stage comparison below can't see this at all — malformed output never marks any
        # span's status ERROR, it just corrupts the text, so every stage's error rate reads clean
        # and the comparison always falls through to inconclusive.
        return RootCause(
            stage="llm_call",
            explanation=(
                f"Output is coming back malformed or invalid ({anomaly.summary}). Only the LLM call "
                f"stage produces the response text — retrieval, auth, and tool calls never touch it — "
                f"so the corruption is definitionally happening there, likely a provider-side "
                f"formatting change or a prompt that's confusing the model."
            ),
            confidence=0.85,
            evidence={"anomaly_evidence": anomaly.evidence},
        )

    recent, baseline = _stage_windows(db_path, anomaly.detector)
    is_latency_metric = anomaly.detector == "latency_spike"
    deviations: dict[str, float] = {}

    for stage in STAGES:
        r_stage, b_stage = recent[stage], baseline[stage]
        if r_stage["count"] < 2:
            continue
        if is_latency_metric:
            if r_stage["p95_ms"] < MIN_MEANINGFUL_LATENCY_MS:
                continue
            deviations[stage] = r_stage["p95_ms"] / b_stage["p95_ms"] if b_stage["p95_ms"] > 0 else 3.0
        else:
            # shift error-rate delta onto the same "1.0 = no deviation" scale as the ratio metric
            deviations[stage] = 1.0 + max(0.0, r_stage["error_rate"] - b_stage["error_rate"])

    if not deviations:
        return _inconclusive(recent, baseline)

    ranked = sorted(deviations.items(), key=lambda kv: kv[1], reverse=True)
    top_stage, top_dev = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 1.0

    if top_dev <= 1.3 or top_dev / max(runner_up, 0.01) < 1.3:
        return _inconclusive(recent, baseline)

    confidence = round(min(0.95, 0.5 + (top_dev - 1.0) / 6), 2)
    label = _STAGE_LABELS[top_stage]
    label_leading = label[0].upper() + label[1:]  # capitalize() would mangle "LLM" into "Llm"
    r_stage, b_stage = recent[top_stage], baseline[top_stage]

    if is_latency_metric:
        explanation = (
            f"{label_leading} is the outlier stage: p95 {r_stage['p95_ms']:.0f}ms vs baseline "
            f"{b_stage['p95_ms']:.0f}ms ({top_dev:.1f}x). Other stages are near baseline — likely a "
            f"{label} issue, not the rest of the pipeline."
        )
    else:
        explanation = (
            f"{label_leading} is the outlier stage: {r_stage['error_rate']:.0%} error rate vs "
            f"baseline {b_stage['error_rate']:.0%}. Other stages are stable — likely a {label} issue."
        )

    return RootCause(stage=top_stage, explanation=explanation, confidence=confidence,
                      evidence={"recent": recent, "baseline": baseline, "deviations": deviations})
