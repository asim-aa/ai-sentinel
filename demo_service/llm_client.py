"""LLM backends for the demo AI service.

Two backends are selectable at runtime ("primary" / "backup"). If ANTHROPIC_API_KEY is set,
both use the real Claude API (small Haiku calls); otherwise both fall back to a deterministic
mock with realistic latency jitter, so the whole system works with zero setup.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

_MOCK_RESPONSES = [
    "Based on the retrieved context, the answer is confirmed and consistent with the source material.",
    "Here's a concise summary of the relevant points from the provided context.",
    "The data supports this conclusion; no contradicting evidence was found in retrieval.",
    "This appears to be correct according to the referenced documents.",
]


@dataclass
class LLMResult:
    text: str
    latency_ms: float
    tokens_in: int
    tokens_out: int
    error: str | None = None


class Backend:
    name: str

    async def generate(self, prompt: str) -> LLMResult:
        raise NotImplementedError


class MockBackend(Backend):
    """Deterministic fake model: no network, no cost, realistic latency jitter."""

    def __init__(self, name: str, base_latency_ms: float = 350.0, jitter_ms: float = 60.0):
        self.name = name
        self.base_latency_ms = base_latency_ms
        self.jitter_ms = jitter_ms

    async def generate(self, prompt: str) -> LLMResult:
        start = time.perf_counter()
        latency_s = max(0.02, random.gauss(self.base_latency_ms, self.jitter_ms) / 1000)
        await asyncio.sleep(latency_s)

        if prompt.strip().lower() == "return exactly the word healthy":
            text = "HEALTHY"
        else:
            text = random.choice(_MOCK_RESPONSES)

        elapsed_ms = (time.perf_counter() - start) * 1000
        return LLMResult(
            text=text,
            latency_ms=elapsed_ms,
            tokens_in=max(1, len(prompt) // 4),
            tokens_out=max(1, len(text) // 4),
        )


class AnthropicBackend(Backend):
    """Real Claude API backend, used automatically when ANTHROPIC_API_KEY is set.

    Both "primary" and "backup" currently point at the same model — swapping in a second
    provider/model for backup is a one-line change here. Kept identical so the demo doesn't
    require two separate API keys.
    """

    def __init__(self, name: str, model: str = ANTHROPIC_MODEL):
        import anthropic

        self.name = name
        self.model = model
        self._client = anthropic.AsyncAnthropic()

    async def generate(self, prompt: str) -> LLMResult:
        start = time.perf_counter()
        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a span error by the caller
            elapsed_ms = (time.perf_counter() - start) * 1000
            return LLMResult(text="", latency_ms=elapsed_ms, tokens_in=0, tokens_out=0, error=str(exc))

        elapsed_ms = (time.perf_counter() - start) * 1000
        text = "".join(block.text for block in resp.content if block.type == "text")
        return LLMResult(
            text=text,
            latency_ms=elapsed_ms,
            tokens_in=resp.usage.input_tokens,
            tokens_out=resp.usage.output_tokens,
        )


def make_backends() -> dict[str, Backend]:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return {
            "primary": AnthropicBackend("primary"),
            "backup": AnthropicBackend("backup"),
        }
    return {
        "primary": MockBackend("primary", base_latency_ms=350.0),
        "backup": MockBackend("backup", base_latency_ms=420.0),
    }
