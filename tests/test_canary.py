import asyncio
from unittest.mock import AsyncMock, patch

from ai_sentinel import canary


def _mock_response(status_code=200, json_data=None):
    resp = AsyncMock()
    resp.status_code = status_code
    resp.json = lambda: json_data or {}
    return resp


def test_compare_backends_restores_original_backend_after_probing():
    backend_calls = []

    async def fake_get(url, **kwargs):
        return _mock_response(json_data={"active_backend": "primary"})

    async def fake_post(url, json=None, **kwargs):
        if url.endswith("/admin/backend"):
            backend_calls.append(json["backend"])
        return _mock_response()

    with patch("httpx.AsyncClient.get", side_effect=fake_get), \
         patch("httpx.AsyncClient.post", side_effect=fake_post):
        result = asyncio.run(canary.compare_backends("http://demo"))

    assert result is not None
    # flips to backup to probe it, then must restore primary as the last backend switch
    assert backend_calls == ["primary", "backup", "primary"]


def test_compare_backends_reports_both_sides():
    async def fake_get(url, **kwargs):
        return _mock_response(json_data={"active_backend": "primary"})

    async def fake_post(url, json=None, **kwargs):
        return _mock_response()

    with patch("httpx.AsyncClient.get", side_effect=fake_get), \
         patch("httpx.AsyncClient.post", side_effect=fake_post):
        result = asyncio.run(canary.compare_backends("http://demo"))

    assert result["current"]["backend"] == "primary"
    assert result["candidate"]["backend"] == "backup"
    assert result["current"]["samples"] == canary.CANARY_PROBE_COUNT
    assert result["candidate"]["error_rate"] == 0.0


def test_compare_backends_counts_chat_errors_as_candidate_error_rate():
    async def fake_get(url, **kwargs):
        return _mock_response(json_data={"active_backend": "primary"})

    async def fake_post(url, json=None, **kwargs):
        if url.endswith("/chat") and json.get("prompt"):
            return _mock_response(status_code=500)
        return _mock_response()

    with patch("httpx.AsyncClient.get", side_effect=fake_get), \
         patch("httpx.AsyncClient.post", side_effect=fake_post):
        result = asyncio.run(canary.compare_backends("http://demo"))

    assert result["candidate"]["error_rate"] == 1.0


def test_compare_backends_returns_none_on_failure():
    async def fake_get(url, **kwargs):
        raise ConnectionError("simulated network failure")

    with patch("httpx.AsyncClient.get", side_effect=fake_get), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock):
        result = asyncio.run(canary.compare_backends("http://demo"))

    assert result is None
