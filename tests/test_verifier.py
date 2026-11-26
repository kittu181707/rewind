"""Unit tests for rewind.verifier with passing, failing, per-user convergence, and wiring fixtures."""

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
    assert any(f["user"] == "alice" for f in res.failed_assertions)
    assert any(f["user"] == "carol" for f in res.failed_assertions)
    assert res.user_outcomes["bob"]["http_status"] == 200


def test_verify_journey_silent_v2_and_identity_patch_rejected() -> None:
    cfg = _load_demo_config()
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


def test_verify_rejects_500_v2_when_assertions_omitted_and_ok_true() -> None:
    cfg = _load_demo_config()
    payload = {
        "ok": True,
        "phase": "verify",
        "steps": [
            {
                "name": "checkout",
                "user": uid,
                "http_status": 500,
                "executed_version": "v2",
                "effective_flags": {"checkout_v2": False},
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
    assert res.ok is False
    assertions = {f["assertion"] for f in res.failed_assertions}
    assert "direct_http_status_200" in assertions
    assert "direct_executed_version_matches_goal" in assertions
    assert "effective_flags_match_executed_version" in assertions
    assert "non_empty_step_assertions" in assertions


def test_verify_rejects_eval_only_evidence_without_checkout() -> None:
    cfg = _load_demo_config()
    payload = {
        "ok": True,
        "phase": "verify",
        "steps": [
            {
                "name": "eval_verify",
                "user": uid,
                "http_status": 200,
                "executed_version": "legacy",
                "effective_flags": {"checkout_v2": False, "cart_compat_reader": True},
                "assertions": [{"name": "eval_converged_3x", "pass": True, "detail": "ok"}],
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
    assert res.ok is False
    assert any(f["assertion"] == "all_users_checked_out" for f in res.failed_assertions)


def test_verify_rejects_empty_assertions_even_on_http_200() -> None:
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
                "assertions": [],
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
    assert res.ok is False
    assert any(f["assertion"] == "non_empty_step_assertions" for f in res.failed_assertions)


def test_poll_eval_convergence_rejects_missing_or_non_bool_flag_for_off() -> None:
    cfg = _load_demo_config()

    def missing_flag_post(url: str, body: dict):
        return 200, {"ok": True, "user_id": body["user_id"], "effective_flags": {"cart_compat_reader": True}}

    conv_missing = poll_eval_convergence(
        base_url="http://127.0.0.1:8001",
        users=cfg.users,
        expected_flags={"checkout_v2": False},
        timeout_s=0.05,
        interval_s=0.0,
        post_fn=missing_flag_post,
    )
    assert conv_missing.converged is False

    def non_bool_post(url: str, body: dict):
        return 200, {"ok": True, "user_id": body["user_id"], "effective_flags": {"checkout_v2": 0}}

    conv_non_bool = poll_eval_convergence(
        base_url="http://127.0.0.1:8001",
        users=cfg.users,
        expected_flags={"checkout_v2": False},
        timeout_s=0.05,
        interval_s=0.0,
        post_fn=non_bool_post,
    )
    assert conv_non_bool.converged is False


def test_poll_eval_convergence_per_user_expectations() -> None:
    cfg = _load_demo_config()
    per_user = {
        "alice": {"checkout_v2": False, "cart_compat_reader": True},
        "carol": {"checkout_v2": False, "cart_compat_reader": True},
        "bob": {"checkout_v2": False, "cart_compat_reader": False},
    }

    def seg_compat_post(url: str, body: dict):
        uid = body["user_id"]
        compat = uid in {"alice", "carol"}
        return 200, {
            "ok": True,
            "user_id": uid,
            "effective_flags": {"checkout_v2": False, "cart_compat_reader": compat},
        }

    conv = poll_eval_convergence(
        base_url="http://127.0.0.1:8001",
        users=cfg.users,
        expected_flags=per_user,
        timeout_s=2.0,
        interval_s=0.0,
        post_fn=seg_compat_post,
    )
    assert conv.converged is True
    assert conv.streak == 3
    assert conv.attempts == 3


def test_verify_journey_passes_config_and_isolated_state_files_per_env() -> None:
    cfg = _load_demo_config()
    seen_cmds: list[list[str]] = []
    passing_payload = {
        "ok": True,
        "phase": "verify",
        "steps": [
            {
                "name": "checkout",
                "user": uid,
                "http_status": 200,
                "executed_version": "legacy",
                "effective_flags": {"checkout_v2": False, "cart_compat_reader": True},
                "assertions": [{"name": "ok", "pass": True, "detail": "ok"}],
            }
            for uid in ("alice", "carol", "bob")
        ],
    }

    def capture_runner(cmd: list[str], env: dict[str, str], timeout: float) -> subprocess.CompletedProcess[str]:
        seen_cmds.append(cmd)
        return _completed(0, json.dumps(passing_payload))

    verify_journey(cfg, base_url="http://127.0.0.1:8001", phase="verify", runner=capture_runner)
    verify_journey(cfg, base_url="http://127.0.0.1:8002", phase="verify", runner=capture_runner)

    assert len(seen_cmds) == 2
    cmd_rehearsal, cmd_target = seen_cmds
    assert "--config" in cmd_rehearsal and "--config" in cmd_target
    cfg_rehearsal = cmd_rehearsal[cmd_rehearsal.index("--config") + 1]
    cfg_target = cmd_target[cmd_target.index("--config") + 1]
    assert Path(cfg_rehearsal) == cfg.config_path
    assert Path(cfg_target) == cfg.config_path

    state_rehearsal = cmd_rehearsal[cmd_rehearsal.index("--state-file") + 1]
    state_target = cmd_target[cmd_target.index("--state-file") + 1]
    assert state_rehearsal != state_target
    assert "rehearsal" in Path(state_rehearsal).name
    assert "demo-target" in Path(state_target).name


def test_parse_journey_output_rejects_malformed() -> None:
    try:
        parse_journey_output("not-json")
        assert False, "expected VerifierError"
    except Exception as exc:
        assert "invalid JSON" in str(exc)


def test_poll_eval_convergence_requires_three_consecutive_matches() -> None:
    cfg = _load_demo_config()
    flap_rounds = [False, False, True, False, False, False]
    call_idx = {"user_calls": 0}

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
