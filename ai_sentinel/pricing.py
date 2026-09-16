"""Real dollar-cost accounting: tokens -> USD, using each provider's actual current pricing.

Checked live against platform.claude.com/docs and platform.openai.com on 2026-09-16, not trusted
from training data -- pricing changes, and a stale number here would make the whole "real cost"
pitch dishonest. Re-check before relying on these if it's been a while.
"""

from __future__ import annotations

# USD per million tokens.
_PRICING = {
    "claude-haiku-4-5-20251001": {"input_per_mtok": 1.0, "output_per_mtok": 5.0},
    "gpt-5.6-luna": {"input_per_mtok": 0.20, "output_per_mtok": 1.20},
}
_FREE = {"input_per_mtok": 0.0, "output_per_mtok": 0.0}  # the mock backend costs nothing, genuinely


def estimate_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    price = _PRICING.get(model, _FREE)
    return (tokens_in / 1_000_000) * price["input_per_mtok"] + (tokens_out / 1_000_000) * price["output_per_mtok"]
