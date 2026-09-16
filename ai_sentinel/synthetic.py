"""The synthetic functional check: a deterministic prompt through the *full* pipeline.

Unlike readiness (which only checks reachability), this proves the whole path actually works:
API -> prompt processing -> model inference -> response generation -> output parsing.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from ai_sentinel import storage
from demo_service.pipeline import HEALTH_PROBE_PROMPT

log = logging.getLogger("ai_sentinel.synthetic")


async def run_once(db_path: str, demo_url: str) -> dict:
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{demo_url}/chat", json={"prompt": HEALTH_PROBE_PROMPT})
        latency_ms = (time.perf_counter() - start) * 1000

        if resp.status_code != 200:
            detail = f"http {resp.status_code}"
            storage.insert_synthetic_check(db_path, passed=False, latency_ms=latency_ms, detail=detail)
            return {"passed": False, "latency_ms": latency_ms, "detail": detail}

        answer = resp.json().get("answer", "")
        passed = answer.strip() == "HEALTHY"
        detail = "" if passed else f"unexpected output: {answer[:80]!r}"
        storage.insert_synthetic_check(db_path, passed=passed, latency_ms=latency_ms, detail=detail)
        return {"passed": passed, "latency_ms": latency_ms, "detail": detail}

    except Exception as exc:  # noqa: BLE001 - a failed probe is a result, not a crash
        latency_ms = (time.perf_counter() - start) * 1000
        storage.insert_synthetic_check(db_path, passed=False, latency_ms=latency_ms, detail=str(exc))
        return {"passed": False, "latency_ms": latency_ms, "detail": str(exc)}


async def synthetic_check_loop(db_path: str, demo_url: str, interval_s: float = 60.0) -> None:
    while True:
        try:
            result = await run_once(db_path, demo_url)
            if not result["passed"]:
                log.warning("synthetic check failed: %s", result["detail"])
        except Exception:
            log.exception("synthetic check loop error")
        await asyncio.sleep(interval_s)
