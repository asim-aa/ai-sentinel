import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from demo_service import llm_client


def _clear_provider_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for var in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_TIMEOUT_SECONDS"):
        monkeypatch.delenv(var, raising=False)


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


def _set_compatible_endpoint(monkeypatch, **extra):
    monkeypatch.setenv("LLM_BASE_URL", "http://example.invalid:8000/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def test_make_backends_compatible_endpoint_only_uses_it_for_both(monkeypatch):
    _clear_provider_keys(monkeypatch)
    _set_compatible_endpoint(monkeypatch)

    backends = llm_client.make_backends()

    assert isinstance(backends["primary"], llm_client.OpenAICompatibleBackend)
    assert isinstance(backends["backup"], llm_client.OpenAICompatibleBackend)


def test_make_backends_compatible_endpoint_is_the_backup_behind_claude(monkeypatch):
    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-a")
    _set_compatible_endpoint(monkeypatch)

    backends = llm_client.make_backends()

    assert isinstance(backends["primary"], llm_client.AnthropicBackend)
    assert isinstance(backends["backup"], llm_client.OpenAICompatibleBackend)


def test_make_backends_named_providers_take_priority_over_a_compatible_endpoint(monkeypatch):
    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-a")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-o")
    _set_compatible_endpoint(monkeypatch)

    backends = llm_client.make_backends()

    assert isinstance(backends["primary"], llm_client.AnthropicBackend)
    assert isinstance(backends["backup"], llm_client.OpenAIBackend)


def test_compatible_backend_requires_a_model(monkeypatch):
    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("LLM_BASE_URL", "http://example.invalid:8000/v1")

    with pytest.raises(ValueError, match="LLM_MODEL"):
        llm_client.OpenAICompatibleBackend("primary")


def test_compatible_backend_client_config_defaults(monkeypatch):
    _clear_provider_keys(monkeypatch)
    _set_compatible_endpoint(monkeypatch)

    client = llm_client.OpenAICompatibleBackend("primary")._client

    assert str(client.base_url).startswith("http://example.invalid:8000/v1")
    assert client.api_key == "not-needed"
    assert client.timeout == 120.0
    assert client.max_retries == 0  # this system is the retry/failover layer


def test_compatible_backend_client_config_from_env(monkeypatch):
    _clear_provider_keys(monkeypatch)
    _set_compatible_endpoint(monkeypatch, LLM_API_KEY="dummy-key", LLM_TIMEOUT_SECONDS="30")

    client = llm_client.OpenAICompatibleBackend("primary")._client

    assert client.api_key == "dummy-key"
    assert client.timeout == 30.0


def _compatible_backend(monkeypatch):
    _clear_provider_keys(monkeypatch)
    _set_compatible_endpoint(monkeypatch)
    return llm_client.OpenAICompatibleBackend("primary")


def _chat_response(content, usage="default"):
    if usage == "default":
        usage = SimpleNamespace(prompt_tokens=12, completion_tokens=3)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


def test_compatible_backend_generate_success(monkeypatch):
    backend = _compatible_backend(monkeypatch)
    backend._client.chat.completions.create = AsyncMock(return_value=_chat_response("HEALTHY"))

    result = asyncio.run(backend.generate("Return exactly the word HEALTHY"))

    assert (result.text, result.tokens_in, result.tokens_out, result.error) == ("HEALTHY", 12, 3, None)
    backend._client.chat.completions.create.assert_called_once_with(
        model="test-model", max_tokens=llm_client._COMPATIBLE_MAX_TOKENS,
        messages=[{"role": "user", "content": "Return exactly the word HEALTHY"}],
    )


def test_compatible_backend_tolerates_empty_content_from_a_reasoning_model(monkeypatch):
    """A reasoning model that spends its whole budget thinking returns content=None."""
    backend = _compatible_backend(monkeypatch)
    backend._client.chat.completions.create = AsyncMock(return_value=_chat_response(None))

    result = asyncio.run(backend.generate("hello"))

    assert result.text == ""
    assert result.error is None
    assert result.tokens_out == 3


def test_compatible_backend_handles_api_errors(monkeypatch):
    backend = _compatible_backend(monkeypatch)
    backend._client.chat.completions.create = AsyncMock(side_effect=RuntimeError("Request timed out."))

    result = asyncio.run(backend.generate("hello"))

    assert result.text == ""
    assert result.error == "Request timed out."


def test_compatible_backend_handles_missing_usage(monkeypatch):
    backend = _compatible_backend(monkeypatch)
    backend._client.chat.completions.create = AsyncMock(return_value=_chat_response("ok", usage=None))

    result = asyncio.run(backend.generate("hello"))

    assert (result.text, result.tokens_in, result.tokens_out) == ("ok", 0, 0)
