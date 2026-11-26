"""One chat/tool interface for Groq and the Gemini fallback."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import os
import re
import time
from typing import Any, Callable, Protocol

import requests

from rewind.config import AgentConfig, FallbackAgentConfig


_ENDPOINTS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
}
TIMEOUT_S = 30
PRIMARY_RETRIES = 2
MAX_RETRY_DELAY_S = 60.0
_DURATION = re.compile(r"(?:\d+(?:\.\d+)?(?:ms|s|m|h|d))+")
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


class LLMError(RuntimeError):
    """Provider errors contain no response bodies, credentials, or headers."""


class _TransientLLMError(LLMError):
    def __init__(self, message: str, *, retry_delay: float = 0.0) -> None:
        super().__init__(message)
        self.retry_delay = retry_delay


def _retry_after_delay(header: str | None) -> float | None:
    if not header:
        return None
    try:
        delay = float(header)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(header)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
        except (OverflowError, TypeError, ValueError):
            return None
    return max(0.0, min(MAX_RETRY_DELAY_S, delay)) if math.isfinite(delay) else None


def _rate_limit_delay(headers) -> float:
    retry_after = _retry_after_delay(headers.get("Retry-After"))
    if retry_after is not None:
        return retry_after
    delays = []
    for name, value in headers.items():
        if not name.lower().startswith("x-ratelimit-reset-"):
            continue
        resource = name.lower().removeprefix("x-ratelimit-reset-")
        remaining = headers.get(f"x-ratelimit-remaining-{resource}")
        if remaining and remaining.isdecimal() and int(remaining) > 0:
            continue
        value = value.strip().lower()
        if _DURATION.fullmatch(value):
            delay = sum(float(part) * _DURATION_UNITS[unit] for part, unit in _DURATION_PART.findall(value))
            delays.append(min(delay, MAX_RETRY_DELAY_S))
        else:
            delay = _retry_after_delay(value)
            if delay is not None:
                delays.append(delay)
    return max(delays, default=1.0)


@dataclass(frozen=True)
class LLMCall:
    provider: str
    model: str
    latency_ms: float
    prompt_tokens: int | None
    completion_tokens: int | None
    outcome: str
    http_status: int | None
    retry_wait_seconds: float = 0.0


class ChatModel(Protocol):
    calls: list[LLMCall]

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]: ...


class LLMAdapter:
    def __init__(
        self, config: AgentConfig, *, post: Callable[..., requests.Response] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._post = post or requests.post
        self._sleep = sleep
        self.calls: list[LLMCall] = []

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        """Retry a transient primary failure twice before trying the configured fallback."""
        for attempt in range(PRIMARY_RETRIES + 1):
            try:
                return self._call(self.config, messages, tools)
            except _TransientLLMError as exc:
                if attempt == PRIMARY_RETRIES:
                    if self.config.fallback is None:
                        raise
                    return self._call(self.config.fallback, messages, tools)
                if exc.retry_delay:
                    self._sleep(exc.retry_delay)
                    self.calls[-1] = replace(self.calls[-1], retry_wait_seconds=exc.retry_delay)
        raise AssertionError("Unreachable retry loop exit")

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
            if status == 429:
                outcome = "rate_limited"
                raise _TransientLLMError(f"{label}: HTTP 429", retry_delay=_rate_limit_delay(response.headers))
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
