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


def _stage_windows(db_path: str) -> tuple[dict, dict]:
    now = time.time()
    recent = storage.stage_breakdown(db_path, RECENT_WINDOW_S, STAGES, end_ts=now)
    baseline = storage.stage_breakdown(db_path, BASELINE_WINDOW_S, STAGES, end_ts=now - RECENT_WINDOW_S)
    return recent, baseline


def _inconclusive(recent: dict, baseline: dict) -> RootCause:
    return RootCause(
        stage=None,
        explanation="Degradation isn't clearly isolated to one stage — recommend manual investigation.",
        confidence=0.3,
        evidence={"recent": recent, "baseline": baseline},
    )


def diagnose(db_path: str, anomaly: Anomaly) -> RootCause:
    recent, baseline = _stage_windows(db_path)

    if anomaly.detector == "tool_failure_rate":
        r = recent["tool_call"]
        return RootCause(
            stage="tool_call",
            explanation=(
                f"Tool-call stage is failing directly ({r['error_rate']:.0%} of calls over the last "
                f"{RECENT_WINDOW_S}s) — likely a downstream tool/integration issue, not the model itself."
            ),
            confidence=0.9,
            evidence={"recent": recent},
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
