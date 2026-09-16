"""Maps a root cause to a recommended (and, for known-safe cases, executable) action."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ai_sentinel.rootcause import RootCause


@dataclass
class Remediation:
    action: str  # "fail_over" | "disable_retrieval" | "disable_tools" | "manual"
    label: str
    detail: str


_STAGE_ACTIONS: dict[str, Remediation] = {
    "llm_call": Remediation(
        "fail_over", "Fail over to backup backend",
        "Switch the active LLM backend from primary to backup.",
    ),
    "retrieval": Remediation(
        "disable_retrieval", "Disable retrieval",
        "Serve without retrieval augmentation until the vector DB recovers.",
    ),
    "tool_call": Remediation(
        "disable_tools", "Disable tool calls",
        "Stop calling tools temporarily until the integration recovers.",
    ),
}

_MANUAL = Remediation("manual", "Investigate manually", "No safe automatic action for this pattern yet.")


def recommend(root_cause: RootCause) -> Remediation:
    if root_cause.stage in _STAGE_ACTIONS:
        return _STAGE_ACTIONS[root_cause.stage]
    return _MANUAL


async def execute(action: str, demo_url: str) -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        if action == "fail_over":
            current = (await client.get(f"{demo_url}/admin/state")).json()
            other = "backup" if current["active_backend"] == "primary" else "primary"
            resp = await client.post(f"{demo_url}/admin/backend", json={"backend": other})
        elif action == "disable_retrieval":
            resp = await client.post(f"{demo_url}/admin/retrieval", json={"enabled": False})
        elif action == "disable_tools":
            resp = await client.post(f"{demo_url}/admin/tools", json={"enabled": False})
        else:
            raise ValueError(f"no executable remediation for action: {action}")
        resp.raise_for_status()
        return resp.json()


async def rollback(action: str, demo_url: str) -> dict:
    """Undoes an executed action when verification finds it didn't help. For fail_over this is
    just calling execute() again — it always toggles to "whichever backend isn't active", so a
    second call flips back to wherever it started, with no need to have remembered that state."""
    if action == "fail_over":
        return await execute("fail_over", demo_url)
    async with httpx.AsyncClient(timeout=5.0) as client:
        if action == "disable_retrieval":
            resp = await client.post(f"{demo_url}/admin/retrieval", json={"enabled": True})
        elif action == "disable_tools":
            resp = await client.post(f"{demo_url}/admin/tools", json={"enabled": True})
        else:
            raise ValueError(f"no rollback defined for action: {action}")
        resp.raise_for_status()
        return resp.json()
