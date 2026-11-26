"""Unit tests for rewind.verifier with passing, failing, and convergence fixtures."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from rewind.config import load_config
from rewind.verifier import parse_journey_output, poll_eval_convergence, verify_journey


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_demo_config():
    return load_config(REPO_ROOT / "examples" / "demo-shop" / "rewind.config.yaml")


def _completed(returncode: int, stdout: str, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["journey"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_verify_journey_passing_fixture() -> None:
    cfg = _load_demo_config()
    payload = {
        "ok": True,
        "phase": "verify",
        "steps": [
            {
                "name": "checkout",
                "user": uid,
                "http_status": 200,
                "executed_version": "legacy",
                "effective_flags": {"checkout_v2": False, "cart_compat_reader": True},
                "assertions": [
                    {"name": "http_status_200", "pass": True, "detail": "status=200"},
                    {"name": "executed_version_matches_goal", "pass": True, "detail": "legacy"},
                    {"name": "latest_quantities_survive", "pass": True, "detail": "ok"},
                ],
            }
            for uid in ("alice", "carol", "bob")
        ],
    }

    res = verify_journey(
        cfg,
        base_url="http://127.0.0.1:8001",
        phase="verify",
        runner=lambda cmd, env, timeout: _completed(0, json.dumps(payload)),
    )
    assert res.ok is True
    assert res.exit_code == 0
    assert res.failed_assertions == []
    assert set(res.user_outcomes.keys()) == {"alice", "carol", "bob"}
    assert "PASS" in res.summary


def test_verify_journey_loud_500_fixture() -> None:
    cfg = _load_demo_config()
    payload = {
        "ok": False,
        "phase": "verify",
        "steps": [
            {
                "name": "checkout",
                "user": "alice",
                "http_status": 500,
                "executed_version": "legacy",
                "effective_flags": {"checkout_v2": False, "cart_compat_reader": False},
                "assertions": [
                    {"name": "http_status_200", "pass": False, "detail": "status=500, error=Unrecognized cart schema"},
                ],
            },
            {
                "name": "checkout",
                "user": "carol",
                "http_status": 500,
                "executed_version": "legacy",
                "effective_flags": {"checkout_v2": False, "cart_compat_reader": False},
                "assertions": [
                    {"name": "http_status_200", "pass": False, "detail": "status=500, error=Unrecognized cart schema"},
                ],
            },
            {
                "name": "checkout",
                "user": "bob",
                "http_status": 200,
                "executed_version": "legacy",
                "effective_flags": {"checkout_v2": False, "cart_compat_reader": False},
                "assertions": [{"name": "control_not_harmed", "pass": True, "detail": "ok"}],
            },
        ],
    }

    res = verify_journey(
        cfg,
        base_url="http://127.0.0.1:8001",
        phase="verify",
        runner=lambda cmd, env, timeout: _completed(1, json.dumps(payload)),
    )
    assert res.ok is False
    assert res.exit_code == 1
    assert len(res.failed_assertions) == 2
    assert {f["user"] for f in res.failed_assertions} == {"alice", "carol"}
    assert res.user_outcomes["bob"]["http_status"] == 200


def test_verify_journey_silent_v2_and_identity_patch_rejected() -> None:
    cfg = _load_demo_config()
    # Simulates identity:alice fixed, carol still silently on v2 despite HTTP 200
    payload = {
        "ok": False,
        "phase": "verify",
        "steps": [
            {
                "name": "checkout",
                "user": "alice",
                "http_status": 200,
                "executed_version": "legacy",
                "effective_flags": {"checkout_v2": False, "cart_compat_reader": True},
                "assertions": [{"name": "executed_version_matches_goal", "pass": True, "detail": "legacy"}],
            },
            {
                "name": "checkout",
                "user": "carol",
                "http_status": 200,
                "executed_version": "v2",
                "effective_flags": {"checkout_v2": True, "cart_compat_reader": True},
                "assertions": [
                    {"name": "executed_version_matches_goal", "pass": False, "detail": "expected=legacy, got=v2"},
                ],
            },
            {
                "name": "checkout",
                "user": "bob",
                "http_status": 200,
                "executed_version": "legacy",
                "effective_flags": {"checkout_v2": False, "cart_compat_reader": True},
                "assertions": [{"name": "control_not_harmed", "pass": True, "detail": "ok"}],
            },
        ],
    }

    res = verify_journey(
        cfg,
        base_url="http://127.0.0.1:8001",
        phase="verify",
        runner=lambda cmd, env, timeout: _completed(1, json.dumps(payload)),
    )
    assert res.ok is False
    assert any(f["user"] == "carol" for f in res.failed_assertions)


def test_verify_catches_missing_user_even_if_journey_claims_ok() -> None:
    cfg = _load_demo_config()
    payload = {
        "ok": True,
        "phase": "verify",
        "steps": [
            {
                "name": "checkout",
                "user": "alice",
                "http_status": 200,
                "executed_version": "legacy",
                "effective_flags": {"checkout_v2": False, "cart_compat_reader": True},
                "assertions": [{"name": "http_status_200", "pass": True, "detail": "ok"}],
            }
        ],
    }
    res = verify_journey(
        cfg,
        base_url="http://127.0.0.1:8001",
        phase="verify",
        runner=lambda cmd, env, timeout: _completed(0, json.dumps(payload)),
    )
    assert res.ok is False
    assert any(f["assertion"] == "all_configured_users_covered" for f in res.failed_assertions)


def test_parse_journey_output_rejects_malformed() -> None:
    try:
        parse_journey_output("not-json")
        assert False, "expected VerifierError"
    except Exception as exc:
        assert "invalid JSON" in str(exc)


def test_poll_eval_convergence_requires_three_consecutive_matches() -> None:
    cfg = _load_demo_config()
    # Sequence of rounds: 2 matching, 1 flapping, then 3 consecutive matching -> 6 rounds
    flap_rounds = [False, False, True, False, False, False]
    call_idx = {"round": 0, "user_calls": 0}

    def fake_post(url: str, body: dict):
        round_idx = call_idx["user_calls"] // 3
        call_idx["user_calls"] += 1
        flag_val = flap_rounds[min(round_idx, len(flap_rounds) - 1)]
        return 200, {
            "ok": True,
            "user_id": body["user_id"],
            "effective_flags": {"checkout_v2": flag_val, "cart_compat_reader": True},
        }

    conv = poll_eval_convergence(
        base_url="http://127.0.0.1:8001",
        users=cfg.users,
        expected_flags={"checkout_v2": False, "cart_compat_reader": True},
        timeout_s=5.0,
        interval_s=0.0,
        post_fn=fake_post,
    )
    assert conv.converged is True
    assert conv.streak == 3
    assert conv.attempts == 6
