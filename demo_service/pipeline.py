"""The simulated AI request pipeline: auth -> retrieval -> LLM call -> tool call.

Every stage is its own OTel span under one "chat_request" root span. Fault injection lives here,
gated by PipelineState.fault_mode, so the whole chaos surface is in one place.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

from opentelemetry.trace import Status, StatusCode, Tracer

from demo_service.llm_client import Backend

FAULT_MODES = (
    "normal",
    "slow_llm",
    "llm_errors",
    "malformed_output",
    "vector_db_slow",
    "tool_failure",
)

HEALTH_PROBE_PROMPT = "Return exactly the word HEALTHY"


class PipelineError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass
class PipelineState:
    fault_mode: str = "normal"
    active_backend: str = "primary"
    retrieval_enabled: bool = True
    tools_enabled: bool = True


def _corrupt(text: str) -> str:
    cut = max(5, len(text) // 3)
    return text[:cut] + " {UNTERMINATED_FRAGMENT..."


async def run_pipeline(
    prompt: str, state: PipelineState, backends: dict[str, Backend], tracer: Tracer
) -> dict:
    is_probe = prompt.strip() == HEALTH_PROBE_PROMPT
    invalid_output = False
    tokens_total = 0
    answer = ""

    with tracer.start_as_current_span("chat_request") as root:
        trace_id = format(root.get_span_context().trace_id, "032x")

        with tracer.start_as_current_span("auth"):
            await asyncio.sleep(random.uniform(0.005, 0.015))

        context_chunks: list[str] = []
        with tracer.start_as_current_span("retrieval") as span:
            if state.retrieval_enabled:
                delay = random.uniform(0.02, 0.06)
                if state.fault_mode == "vector_db_slow":
                    delay += random.uniform(0.8, 1.5)
                await asyncio.sleep(delay)
                context_chunks = [f"context chunk about: {prompt[:40]!r}"]
                span.set_attribute("chunks_retrieved", len(context_chunks))
            else:
                span.set_attribute("skipped", True)

        try:
            with tracer.start_as_current_span("llm_call") as span:
                backend = backends[state.active_backend]
                span.set_attribute("backend", backend.name)

                # llm_call faults model a problem with the primary provider specifically — they
                # don't apply once traffic has actually moved to backup, so that failing over is a
                # real fix and not just a relabeling of the same broken call.
                fault_active_here = state.active_backend == "primary"

                if fault_active_here and state.fault_mode == "llm_errors" and random.random() < 0.7:
                    err = PipelineError("llm_error", "simulated LLM provider error")
                    span.set_status(Status(StatusCode.ERROR))
                    span.record_exception(err)
                    raise err

                if fault_active_here and state.fault_mode == "slow_llm":
                    await asyncio.sleep(random.uniform(2.0, 4.0))

                full_prompt = prompt
                if context_chunks and not is_probe:
                    full_prompt = "\n\n".join(context_chunks) + f"\n\nInstruction: {prompt}"

                result = await backend.generate(full_prompt)
                if result.error:
                    err = PipelineError("llm_error", result.error)
                    span.set_status(Status(StatusCode.ERROR))
                    span.record_exception(err)
                    raise err

                answer = result.text
                if state.fault_mode == "malformed_output":
                    answer = _corrupt(answer)
                    invalid_output = True

                tokens_total = result.tokens_in + result.tokens_out
                span.set_attribute("tokens_in", result.tokens_in)
                span.set_attribute("tokens_out", result.tokens_out)

            with tracer.start_as_current_span("tool_call") as span:
                if state.tools_enabled:
                    if state.fault_mode == "tool_failure" and random.random() < 0.7:
                        span.set_status(Status(StatusCode.ERROR))
                        span.set_attribute("failed", True)
                    else:
                        await asyncio.sleep(random.uniform(0.01, 0.03))
                        span.set_attribute("tool", "lookup_date")
                else:
                    span.set_attribute("skipped", True)

            root.set_attribute("backend", state.active_backend)
            root.set_attribute("fault_mode", state.fault_mode)
            root.set_attribute("invalid_output", invalid_output)
            root.set_attribute("tokens_total", tokens_total)
            return {
                "answer": answer,
                "backend": state.active_backend,
                "fault_mode": state.fault_mode,
                "trace_id": trace_id,
                "invalid_output": invalid_output,
            }

        except PipelineError as exc:
            root.set_status(Status(StatusCode.ERROR))
            root.set_attribute("backend", state.active_backend)
            root.set_attribute("fault_mode", state.fault_mode)
            root.set_attribute("error_reason", exc.reason)
            raise
