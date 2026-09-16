import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from demo_service import llm_client


def _clear_provider_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_make_backends_no_keys_uses_mock_for_both(monkeypatch):
    _clear_provider_keys(monkeypatch)

    backends = llm_client.make_backends()

    assert isinstance(backends["primary"], llm_client.MockBackend)
    assert isinstance(backends["backup"], llm_client.MockBackend)


def test_make_backends_anthropic_only_uses_it_for_both(monkeypatch):
    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    backends = llm_client.make_backends()

    assert isinstance(backends["primary"], llm_client.AnthropicBackend)
    assert isinstance(backends["backup"], llm_client.AnthropicBackend)


def test_make_backends_openai_only_uses_it_for_both(monkeypatch):
    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    backends = llm_client.make_backends()

    assert isinstance(backends["primary"], llm_client.OpenAIBackend)
    assert isinstance(backends["backup"], llm_client.OpenAIBackend)


def test_make_backends_both_keys_gives_genuine_provider_diversity(monkeypatch):
    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-a")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-o")

    backends = llm_client.make_backends()

    assert isinstance(backends["primary"], llm_client.AnthropicBackend)
    assert isinstance(backends["backup"], llm_client.OpenAIBackend)
    assert type(backends["primary"]) is not type(backends["backup"])


def test_openai_backend_generate_success(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-dummy")
    backend = llm_client.OpenAIBackend("primary")
    fake_response = SimpleNamespace(
        output_text="HEALTHY",
        usage=SimpleNamespace(input_tokens=12, output_tokens=3),
    )
    backend._client.responses.create = AsyncMock(return_value=fake_response)

    result = asyncio.run(backend.generate("Return exactly the word HEALTHY"))

    assert result.text == "HEALTHY"
    assert result.tokens_in == 12
    assert result.tokens_out == 3
    assert result.error is None
    backend._client.responses.create.assert_called_once_with(
        model=llm_client.OPENAI_MODEL, input="Return exactly the word HEALTHY"
    )


def test_openai_backend_generate_handles_api_errors(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-dummy")
    backend = llm_client.OpenAIBackend("backup")
    backend._client.responses.create = AsyncMock(side_effect=RuntimeError("simulated API failure"))

    result = asyncio.run(backend.generate("hello"))

    assert result.text == ""
    assert result.error == "simulated API failure"


def test_openai_backend_handles_missing_usage_gracefully(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-dummy")
    backend = llm_client.OpenAIBackend("primary")
    fake_response = SimpleNamespace(output_text="ok", usage=None)
    backend._client.responses.create = AsyncMock(return_value=fake_response)

    result = asyncio.run(backend.generate("hello"))

    assert result.text == "ok"
    assert result.tokens_in == 0
    assert result.tokens_out == 0
