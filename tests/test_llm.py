"""Offline tests of the model adapter's retry boundary and usage records."""

import json
from dataclasses import asdict

import pytest
import requests

from rewind.agent.llm import LLMAdapter, LLMError
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
    pending = iter([requests.Timeout("response info withheld"), response(503), response()])
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
    assert [rec.provider for rec in adapter.calls] == ["groq", "groq", "gemini"]
    assert [rec.outcome for rec in adapter.calls] == ["timeout", "server_error", "ok"]
    assert (adapter.calls[-1].prompt_tokens, adapter.calls[-1].completion_tokens) == (42, 9)
    assert all(rec.latency_ms >= 0 for rec in adapter.calls)
    assert endpoints[0] == endpoints[1] and endpoints[2][0].endswith("/openai/chat/completions")
    assert "fixture-groq-only" not in repr([asdict(rec) for rec in adapter.calls])


@pytest.mark.parametrize("status", [400, 401, 429])
def test_nontransient_http_error_never_retries_or_falls_back(model_config, status):
    adapter = LLMAdapter(model_config, post=lambda *args, **kwargs: response(status, {"error": "fixture-groq-only"}))
    with pytest.raises(LLMError, match=f"HTTP {status}") as caught:
        adapter.complete([], [])
    assert "fixture-groq-only" not in str(caught.value)
    assert len(adapter.calls) == 1


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
