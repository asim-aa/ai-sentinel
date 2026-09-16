"""Blind fault-injection accuracy eval: inject a real fault without telling detection or
diagnosis which one, then check whether they notice it and name the right stage -- the most
direct, honest measure of whether root-cause attribution actually works, not just whether it runs.

"Blind" means exactly one thing: the choice of fault is made here and never passed to detectors.py
or rootcause.py -- they only ever see spans, the same as they would for a real, organic incident.
This module reveals its own choice only when scoring the outcome afterward.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

from ai_sentinel import detectors, rootcause, storage
from ai_sentinel.detectors import RECENT_WINDOW_S

log = logging.getLogger("ai_sentinel.blind_eval")

PROBE_PROMPT = "Summarize the latest quarterly report in one sentence."
WARMUP_PROBE_COUNT = 8
WARMUP_SETTLE_S = RECENT_WINDOW_S + 5  # long enough for warmup traffic to age out of "recent"
TRIAL_PROBE_COUNT = 6
# The real detectors.py / rootcause.py windowing is deliberately left untouched here (testing a
# special-cased "faster" version would defeat the point of a *blind*, honest eval) -- which means
# trials genuinely have to be spaced further apart than RECENT_WINDOW_S, or an earlier trial's
# still-fresh (and entirely different) traffic dilutes the current trial's error-rate signal in
# the shared rolling window. Caught live: two back-to-back trials with only ~10-20s between them
# saw llm_errors diluted from a clean ~0.7 recent error-rate down to ~0.2, just under the
# significance threshold, scoring "inconclusive" for a fault the system should clearly catch.
INTER_TRIAL_GAP_S = RECENT_WINDOW_S + 10

# Which detector should notice each injectable fault, and which pipeline stage root-cause should
# name once it does. `malformed_output` is deliberately included even though rootcause.py has no
# per-stage attribution path for it yet (it corrupts output text without ever marking a span
# ERROR, so the generic per-stage error-rate comparison finds nothing to point at) -- excluding it
# would make the reported accuracy look better than the system actually is. See
# docs/ARCHITECTURE.md for the known gap; this eval is what quantifies it honestly.
FAULT_EXPECTATIONS: dict[str, tuple] = {
    "slow_llm": (detectors.detect_latency_spike, "llm_call"),
    "llm_errors": (detectors.detect_error_rate_spike, "llm_call"),
    "malformed_output": (detectors.detect_invalid_output, "llm_call"),
    "vector_db_slow": (detectors.detect_latency_spike, "retrieval"),
    "tool_failure": (detectors.detect_tool_failure_rate, "tool_call"),
}
DEFAULT_TRIAL_COUNT = len(FAULT_EXPECTATIONS)


async def _set_fault(demo_url: str, mode: str) -> None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.post(f"{demo_url}/admin/fault", json={"mode": mode})


async def _send_traffic(demo_url: str, count: int) -> None:
    async with httpx.AsyncClient(timeout=15.0) as client:
        for _ in range(count):
            try:
                await client.post(f"{demo_url}/chat", json={"prompt": PROBE_PROMPT})
            except Exception:
                log.warning("a blind-eval probe failed to reach the demo service")


def _shuffled_fault_cycle():
    """Yields fault modes in randomly-shuffled order, one full pass through every known fault
    before repeating. Each trial now costs real wall-clock time (see INTER_TRIAL_GAP_S), so
    picking uniformly at random with replacement would risk burning that time on redundant
    repeats instead of covering every fault at least once."""
    while True:
        order = list(FAULT_EXPECTATIONS)
        random.shuffle(order)
        yield from order


async def run_trial(db_path: str, demo_url: str, fault_mode: str) -> dict:
    """One blind trial for a given fault: inject it, send traffic, and score whether detection +
    diagnosis correctly named it -- always restoring the fault to normal afterward. `fault_mode`
    is chosen by the caller (run_blind_eval); this function's own logic never sees it as anything
    but "the fault currently active," exactly as detectors.py and rootcause.py would."""
    detect_fn, expected_stage = FAULT_EXPECTATIONS[fault_mode]

    await _set_fault(demo_url, fault_mode)
    try:
        await _send_traffic(demo_url, TRIAL_PROBE_COUNT)
        anomaly = detect_fn(db_path)
        if anomaly is None:
            outcome, diagnosed_stage = "not_detected", None
        else:
            cause = rootcause.diagnose(db_path, anomaly)
            diagnosed_stage = cause.stage
            if diagnosed_stage == expected_stage:
                outcome = "correct"
            elif diagnosed_stage is None:
                outcome = "inconclusive"
            else:
                outcome = "wrong_stage"
    finally:
        await _set_fault(demo_url, "normal")

    return {
        "fault_mode": fault_mode,
        "expected_stage": expected_stage,
        "diagnosed_stage": diagnosed_stage,
        "outcome": outcome,
    }


async def run_blind_eval(db_path: str, demo_url: str, trial_count: int = DEFAULT_TRIAL_COUNT) -> dict:
    """Runs a full blind-eval batch: one baseline warm-up, then `trial_count` independent trials
    (a shuffled full pass through every fault type before any repeat), each separated by a real
    gap so one trial's traffic can't dilute the next's detection signal in the shared rolling
    window. Slow by design -- ~10 minutes for the default 5 trials -- because it exercises the
    exact same windowed detection a real incident would go through, not a special-cased shortcut."""
    await _set_fault(demo_url, "normal")
    await _send_traffic(demo_url, WARMUP_PROBE_COUNT)
    await asyncio.sleep(WARMUP_SETTLE_S)

    fault_cycle = _shuffled_fault_cycle()
    trials = []
    for i in range(trial_count):
        if i > 0:
            await asyncio.sleep(INTER_TRIAL_GAP_S)
        trials.append(await run_trial(db_path, demo_url, next(fault_cycle)))

    counts = {"correct": 0, "wrong_stage": 0, "inconclusive": 0, "not_detected": 0}
    for t in trials:
        counts[t["outcome"]] += 1
    accuracy = counts["correct"] / len(trials) if trials else 0.0
    ts = time.time()

    run_id = storage.record_blind_eval_run(
        db_path, ts=ts, trial_count=len(trials), accuracy=accuracy, counts=counts, trials=trials,
    )
    return {"id": run_id, "ts": ts, "trial_count": len(trials), "accuracy": accuracy, "counts": counts, "trials": trials}
