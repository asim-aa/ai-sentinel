"""LLM-as-judge quality eval: sends a small, fixed set of representative prompts through the
demo pipeline and has a real model judge whether each response actually addresses what was
asked, as opposed to generic boilerplate that could sit under any question.

Deliberately scoped to one rubric question, not a multi-axis quality score. Correctness and style
aren't judged here -- relevance is, because it's the one quality dimension that (a) isn't already
covered by an existing detector (malformed/corrupted output is already caught mechanically by
invalid_output_rate -- pipeline.py's fault injection sets that flag directly, no judgment needed)
and (b) produces an honest, non-flaky signal even against the mock backend: MockBackend's four
canned responses are deliberately generic and prompt-independent, so a real judge should
consistently fail them for lack of relevance -- which is true, not noise.

The judge model is selected independently of the demo service's primary/backup backends (it's
evaluating the system, not part of it) and has no mock fallback: faking semantic judgment would
be dishonest in a way this project has avoided everywhere else. If neither ANTHROPIC_API_KEY nor
OPENAI_API_KEY is set, running the eval raises clearly rather than pretending to work.
"""

from __future__ import annotations

import os
import time

import httpx

from ai_sentinel import storage
from ai_sentinel.pricing import estimate_cost_usd

JUDGE_MODEL_ANTHROPIC = "claude-haiku-4-5-20251001"
JUDGE_MODEL_OPENAI = "gpt-5.6-luna"

# A small, fixed, non-blind set -- unlike the blind fault-injection eval, there's no ground truth
# to withhold here. "What a good answer looks like" is exactly what the judge is asked to decide.
TEST_PROMPTS = [
    "Summarize the latest quarterly report in one sentence.",
    "What is the capital of France?",
    "List three benefits of automated root-cause analysis.",
    "Explain why the sky appears blue.",
]

RUBRIC_TEMPLATE = """You are grading an AI system's response for relevance, not correctness or style.

The user asked:
{prompt}

The system responded:
{response}

Question: does this response directly and specifically address what was asked, using
information that could only apply to this question -- not generic text that could be pasted
under any question? Answer with exactly one word, PASS or FAIL, on the first line, then a single
sentence of reasoning on the second line."""


def _judge_provider() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return None


async def _call_anthropic(prompt: str) -> tuple[str, int, int]:
    import anthropic

    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model=JUDGE_MODEL_ANTHROPIC, max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    return text, resp.usage.input_tokens, resp.usage.output_tokens


async def _call_openai(prompt: str) -> tuple[str, int, int]:
    import openai

    client = openai.AsyncOpenAI()
    resp = await client.responses.create(model=JUDGE_MODEL_OPENAI, input=prompt)
    usage = getattr(resp, "usage", None)
    tokens_in = getattr(usage, "input_tokens", 0) if usage else 0
    tokens_out = getattr(usage, "output_tokens", 0) if usage else 0
    return resp.output_text, tokens_in, tokens_out


async def _call_judge(prompt: str) -> tuple[str, str, int, int]:
    """Returns (judge_model, response_text, tokens_in, tokens_out)."""
    provider = _judge_provider()
    if provider == "anthropic":
        text, tokens_in, tokens_out = await _call_anthropic(prompt)
        return JUDGE_MODEL_ANTHROPIC, text, tokens_in, tokens_out
    if provider == "openai":
        text, tokens_in, tokens_out = await _call_openai(prompt)
        return JUDGE_MODEL_OPENAI, text, tokens_in, tokens_out
    raise RuntimeError("no judge model configured -- set ANTHROPIC_API_KEY or OPENAI_API_KEY")


def _parse_verdict(judge_text: str) -> tuple[str, str]:
    lines = [line.strip() for line in judge_text.strip().splitlines() if line.strip()]
    if not lines:
        return "fail", "judge returned an empty response"
    verdict = "pass" if "PASS" in lines[0].upper() else "fail"
    reasoning = lines[1] if len(lines) > 1 else ""
    return verdict, reasoning


async def judge_one(demo_url: str, prompt: str) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(f"{demo_url}/chat", json={"prompt": prompt})
    if resp.status_code >= 400:
        answer = f"(request failed: HTTP {resp.status_code})"
    else:
        answer = resp.json().get("answer", "")

    judge_model, judge_text, tokens_in, tokens_out = await _call_judge(
        RUBRIC_TEMPLATE.format(prompt=prompt, response=answer)
    )
    verdict, reasoning = _parse_verdict(judge_text)

    return {
        "prompt": prompt,
        "response": answer,
        "verdict": verdict,
        "reasoning": reasoning,
        "judge_model": judge_model,
        "judge_cost_usd": estimate_cost_usd(judge_model, tokens_in, tokens_out),
    }


async def run_quality_eval(db_path: str, demo_url: str) -> dict:
    if _judge_provider() is None:
        raise RuntimeError("no judge model configured -- set ANTHROPIC_API_KEY or OPENAI_API_KEY")

    results = [await judge_one(demo_url, prompt) for prompt in TEST_PROMPTS]
    pass_count = sum(1 for r in results if r["verdict"] == "pass")
    total_cost_usd = sum(r["judge_cost_usd"] for r in results)
    judge_model = results[0]["judge_model"]
    ts = time.time()

    run_id = storage.record_quality_eval_run(
        db_path, ts=ts, judge_model=judge_model, prompt_count=len(results),
        pass_count=pass_count, total_cost_usd=total_cost_usd, results=results,
    )
    return {
        "id": run_id, "ts": ts, "judge_model": judge_model, "prompt_count": len(results),
        "pass_count": pass_count, "total_cost_usd": total_cost_usd, "results": results,
    }
