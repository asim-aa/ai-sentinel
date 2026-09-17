import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from ai_sentinel import quality_eval, storage


# -------------------------------------------------------------- _judge_provider --

def test_judge_provider_prefers_anthropic_when_both_are_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert quality_eval._judge_provider() == "anthropic"


def test_judge_provider_falls_back_to_openai(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert quality_eval._judge_provider() == "openai"


def test_judge_provider_none_when_neither_is_set(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert quality_eval._judge_provider() is None


# --------------------------------------------------------------- _parse_verdict --

def test_parse_verdict_reads_pass_and_reasoning():
    verdict, reasoning = quality_eval._parse_verdict("PASS\nDirectly answers the question.")
    assert verdict == "pass"
    assert reasoning == "Directly answers the question."


def test_parse_verdict_reads_fail():
    verdict, reasoning = quality_eval._parse_verdict("FAIL\nGeneric boilerplate, not specific.")
    assert verdict == "fail"
    assert reasoning == "Generic boilerplate, not specific."


def test_parse_verdict_is_case_insensitive_and_tolerates_extra_words():
    verdict, _ = quality_eval._parse_verdict("Verdict: pass\nsome reasoning")
    assert verdict == "pass"


def test_parse_verdict_defaults_to_fail_on_empty_response():
    verdict, reasoning = quality_eval._parse_verdict("   \n  ")
    assert verdict == "fail"
    assert "empty" in reasoning


def test_parse_verdict_handles_a_missing_reasoning_line():
    verdict, reasoning = quality_eval._parse_verdict("PASS")
    assert verdict == "pass"
    assert reasoning == ""


# ------------------------------------------------------------------ _call_judge --

def test_call_judge_raises_when_no_provider_configured(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    try:
        asyncio.run(quality_eval._call_judge("some prompt"))
        raised = False
    except RuntimeError as exc:
        raised = "no judge model configured" in str(exc)
    assert raised


def test_call_judge_uses_anthropic_when_configured(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    block = MagicMock()
    block.type = "text"
    block.text = "PASS\nDirectly answers the question."
    fake_response = MagicMock()
    fake_response.content = [block]
    fake_response.usage.input_tokens = 50
    fake_response.usage.output_tokens = 10

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=fake_response)
        mock_cls.return_value = mock_client

        model, text, tokens_in, tokens_out = asyncio.run(quality_eval._call_judge("rubric prompt"))

    assert model == quality_eval.JUDGE_MODEL_ANTHROPIC
    assert text == "PASS\nDirectly answers the question."
    assert tokens_in == 50
    assert tokens_out == 10


def test_call_judge_uses_openai_when_anthropic_key_is_absent(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    fake_response = MagicMock()
    fake_response.output_text = "FAIL\nGeneric, not specific."
    fake_response.usage.input_tokens = 40
    fake_response.usage.output_tokens = 8

    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.responses.create = AsyncMock(return_value=fake_response)
        mock_cls.return_value = mock_client

        model, text, tokens_in, tokens_out = asyncio.run(quality_eval._call_judge("rubric prompt"))

    assert model == quality_eval.JUDGE_MODEL_OPENAI
    assert text == "FAIL\nGeneric, not specific."
    assert tokens_in == 40
    assert tokens_out == 8


# --------------------------------------------------------------------- judge_one --

def test_judge_one_combines_the_demo_response_with_the_verdict(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def fake_post(url, json=None, **kwargs):
        resp = AsyncMock()
        resp.status_code = 200
        resp.json = lambda: {"answer": "Paris."}
        return resp

    block = MagicMock()
    block.type = "text"
    block.text = "PASS\nDirectly names the capital."
    fake_response = MagicMock()
    fake_response.content = [block]
    fake_response.usage.input_tokens = 30
    fake_response.usage.output_tokens = 6

    with patch("httpx.AsyncClient.post", side_effect=fake_post), \
         patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=fake_response)
        mock_cls.return_value = mock_client

        result = asyncio.run(quality_eval.judge_one("http://demo", "What is the capital of France?"))

    assert result["prompt"] == "What is the capital of France?"
    assert result["response"] == "Paris."
    assert result["verdict"] == "pass"
    assert result["reasoning"] == "Directly names the capital."
    assert result["judge_model"] == quality_eval.JUDGE_MODEL_ANTHROPIC
    assert result["judge_cost_usd"] > 0


def test_judge_one_handles_a_failed_demo_request(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    async def fake_post(url, json=None, **kwargs):
        resp = AsyncMock()
        resp.status_code = 502
        resp.json = lambda: {"error": "simulated failure"}
        return resp

    block = MagicMock()
    block.type = "text"
    block.text = "FAIL\nThe response is an error message, not an answer."
    fake_response = MagicMock()
    fake_response.content = [block]
    fake_response.usage.input_tokens = 20
    fake_response.usage.output_tokens = 10

    with patch("httpx.AsyncClient.post", side_effect=fake_post), \
         patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=fake_response)
        mock_cls.return_value = mock_client

        result = asyncio.run(quality_eval.judge_one("http://demo", "What is the capital of France?"))

    assert "request failed" in result["response"]
    assert result["verdict"] == "fail"


# -------------------------------------------------------------- run_quality_eval --

def test_run_quality_eval_raises_without_a_configured_judge(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    db = str(tmp_path / "t.db")
    storage.init_db(db)

    raised = False
    try:
        asyncio.run(quality_eval.run_quality_eval(db, "http://demo"))
    except RuntimeError as exc:
        raised = "no judge model configured" in str(exc)
    assert raised


def test_run_quality_eval_aggregates_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    db = str(tmp_path / "t.db")
    storage.init_db(db)

    call_count = []

    async def fake_post(url, json=None, **kwargs):
        resp = AsyncMock()
        resp.status_code = 200
        resp.json = lambda: {"answer": "a generic canned response"}
        return resp

    async def fake_judge(prompt):
        call_count.append(1)
        # alternate pass/fail so aggregation math is actually exercised, not a trivial all-pass
        verdict_text = "PASS\nok" if len(call_count) % 2 else "FAIL\nnot specific"
        return quality_eval.JUDGE_MODEL_ANTHROPIC, verdict_text, 20, 5

    with patch("httpx.AsyncClient.post", side_effect=fake_post), \
         patch("ai_sentinel.quality_eval._call_judge", side_effect=fake_judge):
        result = asyncio.run(quality_eval.run_quality_eval(db, "http://demo"))

    assert result["prompt_count"] == len(quality_eval.TEST_PROMPTS)
    assert result["pass_count"] == sum(1 for i in range(1, len(quality_eval.TEST_PROMPTS) + 1) if i % 2)
    assert result["judge_model"] == quality_eval.JUDGE_MODEL_ANTHROPIC
    assert result["total_cost_usd"] > 0
    assert len(result["results"]) == len(quality_eval.TEST_PROMPTS)

    stored = storage.list_quality_eval_runs(db)
    assert len(stored) == 1
    assert stored[0]["pass_count"] == result["pass_count"]
    assert stored[0]["results"] == result["results"]
