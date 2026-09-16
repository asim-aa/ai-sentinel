"""LLM backends for the demo AI service.

Two backends are selectable at runtime ("primary" / "backup"). Provider selection is
availability-driven: with both ANTHROPIC_API_KEY and OPENAI_API_KEY set, primary and backup are
genuinely different providers (Claude + OpenAI) — a real failover, not a same-model toggle. With
only one key set, both point at that one provider. With neither, both fall back to a
deterministic mock with realistic latency jitter, so the whole system works with zero setup.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass
from typing import Callable

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")  # OpenAI's cheapest/fastest tier

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
    """Real Claude API backend, used when ANTHROPIC_API_KEY is set."""

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


class OpenAIBackend(Backend):
    """Real OpenAI API backend, used when OPENAI_API_KEY is set — a genuinely different provider
    from AnthropicBackend, so primary/backup can be real cross-provider failover, not just two
    instances of the same model. Uses the Responses API (the current OpenAI SDK's main entry
    point), not the older Chat Completions API.
    """

    def __init__(self, name: str, model: str = OPENAI_MODEL):
        import openai

        self.name = name
        self.model = model
        self._client = openai.AsyncOpenAI()

    async def generate(self, prompt: str) -> LLMResult:
        start = time.perf_counter()
        try:
            resp = await self._client.responses.create(model=self.model, input=prompt)
        except Exception as exc:  # noqa: BLE001 - surfaced as a span error by the caller
            elapsed_ms = (time.perf_counter() - start) * 1000
            return LLMResult(text="", latency_ms=elapsed_ms, tokens_in=0, tokens_out=0, error=str(exc))

        elapsed_ms = (time.perf_counter() - start) * 1000
        usage = getattr(resp, "usage", None)
        return LLMResult(
            text=resp.output_text,
            latency_ms=elapsed_ms,
            tokens_in=getattr(usage, "input_tokens", 0) if usage else 0,
            tokens_out=getattr(usage, "output_tokens", 0) if usage else 0,
        )


# Priority order: first available key wins "primary", second-available wins "backup". Adding a
# third provider is one more (env var, factory) entry here.
_PROVIDERS: list[tuple[str, Callable[[str], Backend]]] = [
    ("ANTHROPIC_API_KEY", lambda name: AnthropicBackend(name)),
    ("OPENAI_API_KEY", lambda name: OpenAIBackend(name)),
]


def make_backends() -> dict[str, Backend]:
    available = [factory for env_var, factory in _PROVIDERS if os.environ.get(env_var)]

    if len(available) >= 2:
        return {"primary": available[0]("primary"), "backup": available[1]("backup")}
    if len(available) == 1:
        factory = available[0]
        return {"primary": factory("primary"), "backup": factory("backup")}
    return {
        "primary": MockBackend("primary", base_latency_ms=350.0),
        "backup": MockBackend("backup", base_latency_ms=420.0),
    }
