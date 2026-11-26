"""One chat/tool interface for Groq and the Gemini fallback."""

from __future__ import annotations

from dataclasses import dataclass
import os
import time
from typing import Any, Callable, Protocol

import requests

from rewind.config import AgentConfig, FallbackAgentConfig


_ENDPOINTS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
}
TIMEOUT_S = 30


class LLMError(RuntimeError):
    """Provider errors contain no response bodies, credentials, or headers."""


class _TransientLLMError(LLMError):
    pass


@dataclass(frozen=True)
class LLMCall:
    provider: str
    model: str
    latency_ms: float
    prompt_tokens: int | None
    completion_tokens: int | None
    outcome: str
    http_status: int | None


class ChatModel(Protocol):
    calls: list[LLMCall]

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]: ...


class LLMAdapter:
    def __init__(self, config: AgentConfig, *, post: Callable[..., requests.Response] | None = None) -> None:
        self.config = config
        self._post = post or requests.post
        self.calls: list[LLMCall] = []

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        """Retry the primary once on a transient failure, then try the fallback once."""
        try:
            return self._call(self.config, messages, tools)
        except _TransientLLMError:
            try:
                return self._call(self.config, messages, tools)
            except _TransientLLMError:
                if self.config.fallback is None:
                    raise
        return self._call(self.config.fallback, messages, tools)

    def _call(
        self, model: AgentConfig | FallbackAgentConfig,
        messages: list[dict[str, Any]], tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        endpoint = _ENDPOINTS.get(model.provider)
        if endpoint is None:
            raise LLMError("Unsupported model provider; use groq or gemini")
        key = os.getenv(model.key_env, "").strip()
        if not key:
            raise LLMError(f"Missing model credential variable {model.key_env}")
        payload = {"model": model.model, "messages": messages, "tools": tools, "temperature": 0}
        started = time.perf_counter()
        status: int | None = None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        outcome = "error"
        label = f"{model.provider}/{model.model} chat completion"
        try:
            try:
                response = self._post(
                    endpoint,
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload, timeout=TIMEOUT_S, allow_redirects=False,
                )
            except requests.Timeout:
                outcome = "timeout"
                raise _TransientLLMError(f"{label}: timed out (HTTP timeout {TIMEOUT_S}s)") from None
            except (requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
                outcome = "transport_error"
                raise _TransientLLMError(f"{label}: {type(exc).__name__}") from None
            except requests.RequestException as exc:
                raise LLMError(f"{label}: {type(exc).__name__}") from None

            status = response.status_code
            if 500 <= status < 600:
                outcome = "server_error"
                raise _TransientLLMError(f"{label}: HTTP {status}")
            if status != 200:
                raise LLMError(f"{label}: HTTP {status}")

            try:
                data = response.json()
                usage = data.get("usage") or {}
                prompt_tokens = _token_count(usage.get("prompt_tokens"))
                completion_tokens = _token_count(usage.get("completion_tokens"))
                message = data["choices"][0]["message"]
                if not isinstance(message, dict):
                    raise TypeError
            except (ValueError, AttributeError, KeyError, IndexError, TypeError):
                outcome = "invalid_response"
                raise LLMError(f"{label}: invalid response envelope") from None
            outcome = "ok"
            return message
        finally:
            self.calls.append(LLMCall(
                model.provider, model.model, round((time.perf_counter() - started) * 1000, 3),
                prompt_tokens, completion_tokens, outcome, status,
            ))


def _token_count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None
