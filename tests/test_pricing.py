from ai_sentinel.pricing import estimate_cost_usd


def test_estimate_cost_usd_claude_haiku():
    # 1000 in @ $1/MTok + 1000 out @ $5/MTok
    cost = estimate_cost_usd("claude-haiku-4-5-20251001", 1000, 1000)
    assert round(cost, 6) == round(0.001 + 0.005, 6)


def test_estimate_cost_usd_openai():
    cost = estimate_cost_usd("gpt-5.6-luna", 1_000_000, 1_000_000)
    assert round(cost, 2) == round(0.20 + 1.20, 2)


def test_estimate_cost_usd_unknown_model_is_free():
    assert estimate_cost_usd("mock", 5000, 5000) == 0.0


def test_estimate_cost_usd_scales_with_tokens():
    small = estimate_cost_usd("claude-haiku-4-5-20251001", 100, 100)
    large = estimate_cost_usd("claude-haiku-4-5-20251001", 1000, 1000)
    assert large == small * 10
