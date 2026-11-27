"""Offline tests of the model adapter's retry boundary and usage records."""

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import requests

from rewind.agent.llm import LLMAdapter, LLMError, _rate_limit_delay, _retry_after_delay
from rewind.config import AgentConfig


@pytest.fixture
def model_config(monkeypatch):
    monkeypatch.setenv("GROQ_FIXTURE_KEY", "fixture-groq-only")
    monkeypatch.setenv("GEMINI_FIXTURE_KEY", "fixture-gemini-only")
    return AgentConfig.model_validate({
        "provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "GROQ_FIXTURE_KEY", "max_attempts": 3,
        "fallback": {"provider": "gemini", "model": "gemini-fixture", "key_env": "GEMINI_FIXTURE_KEY"},
    })


def response(status=200, data=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(data if data is not None else {
        "choices": [{"message": {"role": "assistant", "content": "fixture text"}}],
        "usage": {"prompt_tokens": 42, "completion_tokens": 9},
    }).encode()
    return result


def test_retry_then_fallback_on_transport_and_5xx(model_config):
    pending = iter([requests.Timeout("response info withheld"), response(503), response(503), response()])
    endpoints = []

    def fake_post(url, *, headers, json, timeout, allow_redirects):
        endpoints.append((url, json["model"]))
        assert timeout == 30 and json["temperature"] == 0 and not allow_redirects
        result = next(pending)
        if isinstance(result, Exception):
            raise result
        return result

    adapter = LLMAdapter(model_config, post=fake_post)
    assert adapter.complete([], [])["content"] == "fixture text"
    assert [rec.provider for rec in adapter.calls] == ["groq", "groq", "groq", "gemini"]
    assert [rec.outcome for rec in adapter.calls] == ["timeout", "server_error", "server_error", "ok"]
    assert (adapter.calls[-1].prompt_tokens, adapter.calls[-1].completion_tokens) == (42, 9)
    assert all(rec.latency_ms >= 0 for rec in adapter.calls)
    assert endpoints[0] == endpoints[1] == endpoints[2] and endpoints[3][0].endswith("/openai/chat/completions")
    assert "fixture-groq-only" not in repr([asdict(rec) for rec in adapter.calls])


def test_single_interpretation_call_does_not_retry_or_fall_back(model_config):
    tools = [{"type": "function", "function": {"name": "select_rollback"}}]

    def post(url, **kwargs):
        assert url == "https://api.groq.com/openai/v1/chat/completions"
        assert kwargs["json"]["temperature"] == 0 and kwargs["json"]["tools"] == tools
        return response(503)

    adapter = LLMAdapter(model_config, post=post)
    with pytest.raises(LLMError, match="HTTP 503"):
        adapter.complete_once([], tools)
    assert [call.outcome for call in adapter.calls] == ["server_error"]


@pytest.mark.parametrize("status", [400, 401, 403])
def test_nontransient_http_error_never_retries_or_falls_back(model_config, status):
    adapter = LLMAdapter(model_config, post=lambda *args, **kwargs: response(status, {"error": "fixture-groq-only"}))
    with pytest.raises(LLMError, match=f"HTTP {status}") as caught:
        adapter.complete([], [])
    assert "fixture-groq-only" not in str(caught.value)
    assert len(adapter.calls) == 1


def test_provider_error_records_only_redacted_300_character_message(model_config):
    url = "https://" + "provider.invalid/oauth/login?test=1"
    diagnostic = f'Unknown name "additionalProperties". Key fixture-groq-only. URL {url}. ' + "x" * 400
    adapter = LLMAdapter(model_config, post=lambda *args, **kwargs: response(400, {
        "error": {"message": diagnostic, "details": "unretained details"},
    }))
    with pytest.raises(LLMError, match="Unknown name") as exc:
        adapter.complete([], [])
    message = adapter.calls[0].provider_message
    assert message is not None and len(message) == 300 and exc.value.provider_message == message
    assert "additionalProperties" in message and "<REDACTED>" in message
    assert all(part not in str(exc.value) for part in ("fixture-groq-only", url, "unretained details"))


def test_provider_diagnostic_drops_reflected_body_and_headers(model_config):
    adapter = LLMAdapter(model_config, post=lambda *args, **kwargs: response(400, {
        "error": {"message": 'Invalid tools. Request body: {"messages": ["private content"]}'},
    }))
    with pytest.raises(LLMError, match="Invalid tools"):
        adapter.complete([], [])
    assert adapter.calls[0].provider_message == "Invalid tools."


@pytest.mark.parametrize("header, wait", [("2", 2.0), ("75", 60.0), (None, 1.0)])
def test_rate_limit_honors_retry_after_then_falls_back(model_config, header, wait):
    limited = response(429)
    if header is not None:
        limited.headers["Retry-After"] = header
    responses = iter([limited, limited, limited, response()])
    timeline = []

    def post(_url, **kwargs):
        timeline.append(kwargs["json"]["model"])
        return next(responses)

    adapter = LLMAdapter(model_config, post=post, sleep=timeline.append)
    assert adapter.complete([], [])["content"] == "fixture text"
    assert timeline == [model_config.model, wait, model_config.model, wait, model_config.model, model_config.fallback.model]
    assert [record.outcome for record in adapter.calls] == ["rate_limited"] * 3 + ["ok"]
    assert [record.retry_wait_seconds for record in adapter.calls] == [wait, wait, 0.0, 0.0]


def test_success_after_a_rate_limit_does_not_use_gemini(model_config):
    limited = response(429)
    limited.headers["Retry-After"] = "0"
    responses = iter([limited, response()])
    adapter = LLMAdapter(model_config, post=lambda *args, **kwargs: next(responses), sleep=lambda _: pytest.fail("No wait expected"))
    assert adapter.complete([], [])["content"] == "fixture text"
    assert [record.provider for record in adapter.calls] == ["groq", "groq"]


def test_retry_after_http_date_is_supported_and_capped():
    now = datetime.now(timezone.utc)
    assert _retry_after_delay(format_datetime(now + timedelta(minutes=2), usegmt=True)) == 60.0
    assert _retry_after_delay(format_datetime(now - timedelta(minutes=1), usegmt=True)) == 0.0


def test_quota_reset_headers_wait_for_depleted_resource_only():
    headers = requests.structures.CaseInsensitiveDict({
        "x-ratelimit-reset-requests": "1h50m", "x-ratelimit-remaining-requests": "200",
        "x-ratelimit-reset-tokens": "8s250ms", "x-ratelimit-remaining-tokens": "0",
    })
    assert _rate_limit_delay(headers) == 8.25
    headers["x-ratelimit-remaining-requests"] = "0"
    assert _rate_limit_delay(headers) == 60.0
    headers["Retry-After"] = "5"
    assert _rate_limit_delay(headers) == 5.0


def test_bad_proposal_is_not_a_provider_failure(model_config):
    bad = {"role": "assistant", "tool_calls": [{"function": {"name": "propose_operation", "arguments": "broken"}}]}
    adapter = LLMAdapter(model_config, post=lambda *args, **kwargs: response(data={"choices": [{"message": bad}]}))
    assert adapter.complete([], []) == bad
    assert [rec.outcome for rec in adapter.calls] == ["ok"]


def test_malformed_envelope_does_not_trigger_fallback(model_config):
    adapter = LLMAdapter(model_config, post=lambda *args, **kwargs: response(data={"choices": []}))
    with pytest.raises(LLMError, match="invalid response envelope"):
        adapter.complete([], [])
    assert len(adapter.calls) == 1
