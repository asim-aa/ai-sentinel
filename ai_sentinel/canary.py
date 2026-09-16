"""Before committing to a fail-over, shadow-test the alternative backend so the recommendation
comes with real expected-impact numbers instead of just "this might help".

Runs once, only when a *new* (non-merged) incident's recommended action is fail_over: probes the
currently-active backend, then the other one, then restores whichever was active before this
ran — a comparison, not a commitment. The demo service's admin API has no notion of "use backend
X for just this one request", so this necessarily flips the live toggle a couple of times; kept
short (3 probes/side) to bound how long that takes.
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger("ai_sentinel.canary")

CANARY_PROBE_COUNT = 3
CANARY_PROMPT = "Summarize the latest quarterly report in one sentence."


async def _probe_backend(client: httpx.AsyncClient, demo_url: str, backend: str, count: int) -> dict:
    await client.post(f"{demo_url}/admin/backend", json={"backend": backend})
    latencies_ms = []
    errors = 0
    tokens = []
    costs = []
    for _ in range(count):
        start = time.perf_counter()
        try:
            resp = await client.post(f"{demo_url}/chat", json={"prompt": CANARY_PROMPT})
            latencies_ms.append((time.perf_counter() - start) * 1000)
            if resp.status_code >= 400:
                errors += 1
        except Exception:
            errors += 1
    return {
        "backend": backend,
        "avg_latency_ms": (sum(latencies_ms) / len(latencies_ms)) if latencies_ms else None,
        "error_rate": errors / count,
        "samples": count,
    }


async def compare_backends(demo_url: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            state = (await client.get(f"{demo_url}/admin/state")).json()
            original_backend = state["active_backend"]
            other_backend = "backup" if original_backend == "primary" else "primary"

            try:
                current = await _probe_backend(client, demo_url, original_backend, CANARY_PROBE_COUNT)
                candidate = await _probe_backend(client, demo_url, other_backend, CANARY_PROBE_COUNT)
            finally:
                await client.post(f"{demo_url}/admin/backend", json={"backend": original_backend})
    except Exception:
        log.exception("canary comparison failed")
        return None

    return {"current": current, "candidate": candidate}
