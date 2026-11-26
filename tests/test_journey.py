"""Offline regression tests for raw effective-flag validation in the journey."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def journey(monkeypatch):
    path = Path(__file__).resolve().parent.parent / "examples" / "demo-shop" / "journey.py"
    spec = importlib.util.spec_from_file_location("journey_flag_regression", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Three polling rounds fit before the deterministic deadline. Rejected
    # values time out, while three valid matches converge without real waits.
    clock = iter([0.0, 0.1, 0.2, 1.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    return module


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [(0, False), (1, True), (None, False), ("", False), ("true", True)],
)
def test_journey_rejects_non_boolean_effective_flags(journey, monkeypatch, raw_value, expected):
    monkeypatch.setattr(
        journey,
        "_post",
        lambda *args: (200, {"effective_flags": {"test_flag": raw_value}}),
    )
    converged, _, streak, _ = journey.poll_eval_three_consecutive(
        "http://unused.invalid",
        [{"id": "fixture_user", "traits": {}}],
        {"test_flag": expected},
        timeout_s=0.5,
        interval_s=0,
    )
    assert converged is False
    assert streak == 0


@pytest.mark.parametrize("expected", [False, True])
def test_journey_accepts_three_explicit_boolean_matches(journey, monkeypatch, expected):
    monkeypatch.setattr(
        journey,
        "_post",
        lambda *args: (200, {"effective_flags": {"test_flag": expected}}),
    )
    converged, _, streak, _ = journey.poll_eval_three_consecutive(
        "http://unused.invalid",
        [{"id": "fixture_user", "traits": {}}],
        {"test_flag": expected},
        timeout_s=0.5,
        interval_s=0,
    )
    assert converged is True
    assert streak == 3


def test_journey_rejects_missing_flag_when_expecting_off(journey, monkeypatch):
    monkeypatch.setattr(journey, "_post", lambda *args: (200, {"effective_flags": {}}))
    converged, _, streak, _ = journey.poll_eval_three_consecutive(
        "http://unused.invalid",
        [{"id": "fixture_user", "traits": {}}],
        {"test_flag": False},
        timeout_s=0.5,
        interval_s=0,
    )
    assert converged is False
    assert streak == 0
