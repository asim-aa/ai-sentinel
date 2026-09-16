import asyncio
from unittest.mock import AsyncMock, patch

from ai_sentinel import alerts, detectors, rootcause, remediation


def _sample():
    anomaly = detectors.Anomaly(
        detector="latency_spike", severity="critical",
        summary="p95 latency is 5.0x baseline (2000ms vs 400ms)",
    )
    cause = rootcause.RootCause(
        stage="llm_call",
        explanation="LLM provider call is the outlier stage: p95 2000ms vs baseline 400ms (5.0x).",
        confidence=0.9,
    )
    action = remediation.Remediation("fail_over", "Fail over to backup backend", "Switch backend.")
    return anomaly, cause, action


def test_no_delivery_when_no_webhooks_configured(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    anomaly, cause, action = _sample()

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        asyncio.run(alerts.emit_alert(anomaly, cause, action))

    mock_post.assert_not_called()


def test_slack_payload_has_severity_color_and_content(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/services/test")
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    anomaly, cause, action = _sample()

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
        asyncio.run(alerts.emit_alert(anomaly, cause, action))

    mock_post.assert_called_once()
    url, kwargs = mock_post.call_args.args[0], mock_post.call_args.kwargs
    assert url == "https://hooks.slack.example/services/test"

    payload = kwargs["json"]
    assert "CRITICAL" in payload["text"]
    attachment = payload["attachments"][0]
    assert attachment["color"] == "#f5556c"  # critical -> red, matches the dashboard palette
    block_text = " ".join(
        b["text"]["text"] for b in attachment["blocks"] if b["type"] == "section"
    )
    assert anomaly.summary in block_text
    assert cause.explanation in block_text


def test_generic_webhook_gets_flat_json(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://example.com/hook")
    anomaly, cause, action = _sample()

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
        asyncio.run(alerts.emit_alert(anomaly, cause, action))

    mock_post.assert_called_once()
    payload = mock_post.call_args.kwargs["json"]
    assert payload == {
        "detector": "latency_spike",
        "severity": "critical",
        "summary": anomaly.summary,
        "root_cause": cause.explanation,
        "confidence": 0.9,
        "recommended_action": "Fail over to backup backend",
    }


def test_both_channels_fire_independently(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/a")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://example.com/b")
    anomaly, cause, action = _sample()

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
        asyncio.run(alerts.emit_alert(anomaly, cause, action))

    assert mock_post.call_count == 2
    called_urls = {c.args[0] for c in mock_post.call_args_list}
    assert called_urls == {"https://hooks.slack.example/a", "https://example.com/b"}


def test_min_severity_filters_out_lower_severity_alerts(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/test")
    monkeypatch.setenv("ALERT_MIN_SEVERITY", "critical")
    anomaly = detectors.Anomaly(detector="cost_spike", severity="warning", summary="tokens up 1.6x")
    cause = rootcause.RootCause(stage="llm_call", explanation="x", confidence=0.8)
    action = remediation.Remediation("fail_over", "Fail over to backup backend", "x")

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        asyncio.run(alerts.emit_alert(anomaly, cause, action))

    mock_post.assert_not_called()


def test_one_channel_failing_does_not_block_the_other(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/broken")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://example.com/ok")
    anomaly, cause, action = _sample()

    ok_resp = AsyncMock()
    ok_resp.status_code = 200

    async def flaky_post(url, json=None):
        if "broken" in url:
            raise ConnectionError("simulated network failure")
        return ok_resp

    with patch("httpx.AsyncClient.post", side_effect=flaky_post) as mock_post:
        asyncio.run(alerts.emit_alert(anomaly, cause, action))

    assert mock_post.call_count == 2  # both were attempted despite the first one failing
