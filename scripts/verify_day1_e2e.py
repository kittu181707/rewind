"""Step 9 End-to-End Live Verification (No LLM) using seed, demo-shop, verifier, and flagsmith_ops."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rewind.approvals import issue_token
from rewind.config import RewindConfig, load_config
from rewind.flagsmith_ops import FlagsmithOps, FlagsmithOpsError
from rewind.snapshot import take_snapshot
from rewind.verifier import VerificationResult, verify_journey
from scripts.seed_flagsmith import seed_case


@dataclass
class CaseRow:
    scenario: str
    step: str
    expected: str
    observed: str
    alice: str
    carol: str
    bob: str
    passed: bool


def _fmt_user(res: VerificationResult, uid: str) -> str:
    info = res.user_outcomes.get(uid, {})
    return f"HTTP {info.get('http_status', '?')} / {info.get('executed_version', '?')}"


def _start_shop(port: int, db_path: Path, env_key: str, app_variant: str = "default") -> subprocess.Popen[str]:
    env = {
        **os.environ,
        "FLAGSMITH_ENV_KEY": env_key,
        "DB_PATH": str(db_path),
        "APP_VARIANT": app_variant,
    }
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "examples" / "demo-shop" / "app.py"), "--port", str(port)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        if proc.poll() is not None:
            raise RuntimeError(f"demo-shop exited early on port {port}: {proc.stderr.read()}")
        try:
            r = requests.get(f"{base_url}/health", timeout=0.5)
            if r.status_code == 200:
                return proc
        except Exception:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError(f"demo-shop failed health check on port {port}")


def _stop_shop(proc: subprocess.Popen[str] | None) -> None:
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _reset_db(db_path: Path, writes_log: list[str]) -> None:
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "examples" / "demo-shop" / "reset.py"), "--db", str(db_path)],
        cwd=str(REPO_ROOT),
        check=True,
        capture_output=True,
        text=True,
    )
    writes_log.append(f"SQLite fixture reset written: {db_path}")


def _verify(
    cfg: RewindConfig,
    base_url: str,
    phase: str,
    state_file: Path,
    expect_flags: dict[str, bool] | None = None,
    eval_timeout: float = 30.0,
) -> VerificationResult:
    extra = [
        "--config",
        str(REPO_ROOT / "examples" / "demo-shop" / "rewind.config.yaml"),
        "--state-file",
        str(state_file),
        "--eval-timeout",
        str(eval_timeout),
    ]
    if expect_flags is not None:
        extra.extend(["--expect-json", json.dumps(expect_flags)])
    return verify_journey(cfg, base_url=base_url, phase=phase, extra_args=extra, poll_eval=False)


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    cfg = load_config(REPO_ROOT / "examples" / "demo-shop" / "rewind.config.yaml")

    # Config copy permitting identity:alice ONLY for negative proof tests
    cfg_identity = copy.deepcopy(cfg)
    cfg_identity.allowed.scopes.append("identity:alice")

    ops = FlagsmithOps(cfg)
    ops_identity = FlagsmithOps(cfg_identity)

    rehearsal_env = cfg.flagsmith.rehearsal.environment
    target_env = cfg.flagsmith.target.environment
    rehearsal_key = cfg.env_key_for(rehearsal_env)
    target_key = cfg.env_key_for(target_env)
    rehearsal_url = f"http://127.0.0.1:{cfg.app.rehearsal_port}"
    target_url = f"http://127.0.0.1:{cfg.app.target_port}"

    rows: list[CaseRow] = []
    target_flag_changes: list[str] = []
    fixture_writes: list[str] = []

    target_snap_before = take_snapshot(ops, cfg, target_env)

    with tempfile.TemporaryDirectory(prefix="rewind_day1_e2e_") as td:
        td_path = Path(td)
        rehearsal_db = td_path / "rehearsal.db"
        rehearsal_state = td_path / "rehearsal_state.json"
        target_db = td_path / "target.db"
        target_state = td_path / "target_state.json"

        # ------------------------------------------------------------------
        # CASE A (Loud 500) on rehearsal
        # ------------------------------------------------------------------
        proc = None
        try:
            seed_case(ops, cfg, rehearsal_env, "a")
            _reset_db(rehearsal_db, fixture_writes)
            proc = _start_shop(cfg.app.rehearsal_port, rehearsal_db, rehearsal_key)

            prep = _verify(cfg, rehearsal_url, "prepare", rehearsal_state)
            assert prep.ok, f"Case A prepare failed: {prep.summary}"

            tok = issue_token("rehearsal", rehearsal_env)
            ops.apply_operation(rehearsal_env, "checkout_v2", "environment_default", "set_off", tok)
            v_rb = _verify(cfg, rehearsal_url, "verify", rehearsal_state, {"checkout_v2": False})
            a_ok = (
                not v_rb.ok
                and v_rb.user_outcomes["alice"]["http_status"] == 500
                and v_rb.user_outcomes["carol"]["http_status"] == 500
                and v_rb.user_outcomes["bob"]["http_status"] == 200
                and v_rb.user_outcomes["bob"]["executed_version"] == "legacy"
            )
            rows.append(CaseRow("Case A", "Rollback only", "FAIL (500 loud)", "FAIL" if not v_rb.ok else "PASS", _fmt_user(v_rb, "alice"), _fmt_user(v_rb, "carol"), _fmt_user(v_rb, "bob"), a_ok))

            # Confirm default config blocks identity:alice at ops layer
            blocked = False
            try:
                ops.apply_operation(rehearsal_env, "cart_compat_reader", "identity:alice", "set_on", tok)
            except FlagsmithOpsError:
                blocked = True
            assert blocked, "Default config should block identity:alice"

            # Apply identity:alice patch via test-scoped ops_identity -> verifier must STILL fail (carol 500)
            tok_id = issue_token("rehearsal-negative-test", rehearsal_env)
            ops_identity.apply_operation(rehearsal_env, "cart_compat_reader", "identity:alice", "set_on", tok_id)
            try:
                v_id = _verify(cfg, rehearsal_url, "verify", rehearsal_state, {"checkout_v2": False}, eval_timeout=15.0)
                id_ok = (
                    not v_id.ok
                    and v_id.user_outcomes["alice"]["http_status"] == 200
                    and v_id.user_outcomes["carol"]["http_status"] == 500
                )
                rows.append(CaseRow("Case A", "identity:alice compat", "FAIL (carol 500)", "FAIL" if not v_id.ok else "PASS", _fmt_user(v_id, "alice"), _fmt_user(v_id, "carol"), _fmt_user(v_id, "bob"), id_ok))
            finally:
                ops_identity.clear_identity_override_if_present(rehearsal_env, "cart_compat_reader", "alice", tok_id)

            # Apply right fix: cart_compat_reader set_on at environment_default
            ops.apply_operation(rehearsal_env, "cart_compat_reader", "environment_default", "set_on", tok)
            v_fix = _verify(cfg, rehearsal_url, "verify", rehearsal_state, {"checkout_v2": False, "cart_compat_reader": True})
            rows.append(CaseRow("Case A", "compat environment_default set_on", "PASS", "PASS" if v_fix.ok else "FAIL", _fmt_user(v_fix, "alice"), _fmt_user(v_fix, "carol"), _fmt_user(v_fix, "bob"), v_fix.ok))
        finally:
            _stop_shop(proc)

        # ------------------------------------------------------------------
        # CASE B (Silent segment override) on rehearsal
        # ------------------------------------------------------------------
        proc = None
        try:
            seed_case(ops, cfg, rehearsal_env, "b")
            _reset_db(rehearsal_db, fixture_writes)
            proc = _start_shop(cfg.app.rehearsal_port, rehearsal_db, rehearsal_key)

            prep = _verify(cfg, rehearsal_url, "prepare", rehearsal_state)
            assert prep.ok, f"Case B prepare failed: {prep.summary}"

            tok = issue_token("rehearsal", rehearsal_env)
            ops.apply_operation(rehearsal_env, "checkout_v2", "environment_default", "set_off", tok)
            v_silent = _verify(cfg, rehearsal_url, "verify", rehearsal_state, {"checkout_v2": False}, eval_timeout=10.0)
            silent_ok = (
                not v_silent.ok
                and v_silent.user_outcomes["alice"]["http_status"] == 200
                and v_silent.user_outcomes["alice"]["executed_version"] == "v2"
                and v_silent.user_outcomes["carol"]["http_status"] == 200
                and v_silent.user_outcomes["carol"]["executed_version"] == "v2"
                and v_silent.user_outcomes["bob"]["executed_version"] == "legacy"
            )
            rows.append(CaseRow("Case B", "Rollback only (silent)", "FAIL (alice/carol v2)", "FAIL" if not v_silent.ok else "PASS", _fmt_user(v_silent, "alice"), _fmt_user(v_silent, "carol"), _fmt_user(v_silent, "bob"), silent_ok))

            # Re-applying environment default must still FAIL
            ops.apply_operation(rehearsal_env, "checkout_v2", "environment_default", "set_off", tok)
            v_env = _verify(cfg, rehearsal_url, "verify", rehearsal_state, {"checkout_v2": False}, eval_timeout=10.0)
            env_ok = not v_env.ok and v_env.user_outcomes["alice"]["executed_version"] == "v2" and v_env.user_outcomes["carol"]["executed_version"] == "v2"
            rows.append(CaseRow("Case B", "environment_default set_off", "FAIL (still v2)", "FAIL" if not v_env.ok else "PASS", _fmt_user(v_env, "alice"), _fmt_user(v_env, "carol"), _fmt_user(v_env, "bob"), env_ok))

            # identity:alice set_off must FAIL (carol still v2)
            tok_id = issue_token("rehearsal-negative-test", rehearsal_env)
            ops_identity.apply_operation(rehearsal_env, "checkout_v2", "identity:alice", "set_off", tok_id)
            try:
                v_idb = _verify(cfg, rehearsal_url, "verify", rehearsal_state, {"checkout_v2": False}, eval_timeout=10.0)
                idb_ok = (
                    not v_idb.ok
                    and v_idb.user_outcomes["alice"]["executed_version"] == "legacy"
                    and v_idb.user_outcomes["carol"]["executed_version"] == "v2"
                )
                rows.append(CaseRow("Case B", "identity:alice set_off", "FAIL (carol v2)", "FAIL" if not v_idb.ok else "PASS", _fmt_user(v_idb, "alice"), _fmt_user(v_idb, "carol"), _fmt_user(v_idb, "bob"), idb_ok))
            finally:
                ops_identity.clear_identity_override_if_present(rehearsal_env, "checkout_v2", "alice", tok_id)

            # Fix 1: segment:vip_users set_off -> PASS
            ops.apply_operation(rehearsal_env, "checkout_v2", "segment:vip_users", "set_off", tok)
            v_seg_off = _verify(cfg, rehearsal_url, "verify", rehearsal_state, {"checkout_v2": False, "cart_compat_reader": True})
            rows.append(CaseRow("Case B", "segment:vip_users set_off", "PASS", "PASS" if v_seg_off.ok else "FAIL", _fmt_user(v_seg_off, "alice"), _fmt_user(v_seg_off, "carol"), _fmt_user(v_seg_off, "bob"), v_seg_off.ok))

            # Fix 2: re-seed Case B -> segment:vip_users delete_override -> PASS
            seed_case(ops, cfg, rehearsal_env, "b")
            _reset_db(rehearsal_db, fixture_writes)
            prep2 = _verify(cfg, rehearsal_url, "prepare", rehearsal_state)
            assert prep2.ok, f"Case B (del) prepare failed: {prep2.summary}"
            ops.apply_operation(rehearsal_env, "checkout_v2", "environment_default", "set_off", tok)
            ops.apply_operation(rehearsal_env, "checkout_v2", "segment:vip_users", "delete_override", tok)
            v_seg_del = _verify(cfg, rehearsal_url, "verify", rehearsal_state, {"checkout_v2": False, "cart_compat_reader": True})
            rows.append(CaseRow("Case B", "segment:vip_users delete_override", "PASS", "PASS" if v_seg_del.ok else "FAIL", _fmt_user(v_seg_del, "alice"), _fmt_user(v_seg_del, "carol"), _fmt_user(v_seg_del, "bob"), v_seg_del.ok))
        finally:
            _stop_shop(proc)

        # ------------------------------------------------------------------
        # REFUSAL (APP_VARIANT=no_compat) on rehearsal
        # ------------------------------------------------------------------
        proc = None
        try:
            seed_case(ops, cfg, rehearsal_env, "refusal")
            _reset_db(rehearsal_db, fixture_writes)
            proc = _start_shop(cfg.app.rehearsal_port, rehearsal_db, rehearsal_key, app_variant="no_compat")

            prep = _verify(cfg, rehearsal_url, "prepare", rehearsal_state)
            assert prep.ok, f"Refusal prepare failed: {prep.summary}"

            tok = issue_token("rehearsal", rehearsal_env)
            ops.apply_operation(rehearsal_env, "checkout_v2", "environment_default", "set_off", tok)
            v_ref0 = _verify(cfg, rehearsal_url, "verify", rehearsal_state, {"checkout_v2": False})
            rows.append(CaseRow("Refusal", "Rollback only (no_compat)", "FAIL (500)", "FAIL" if not v_ref0.ok else "PASS", _fmt_user(v_ref0, "alice"), _fmt_user(v_ref0, "carol"), _fmt_user(v_ref0, "bob"), not v_ref0.ok))

            # Exhaustively try every allowed non-harmful recovery op on refusal -> ALL MUST FAIL
            ref_ops = [
                ("cart_compat_reader", "environment_default", "set_on", {"checkout_v2": False, "cart_compat_reader": True}),
                ("cart_compat_reader", "segment:vip_users", "set_on", {"checkout_v2": False, "cart_compat_reader": True}),
                ("checkout_v2", "segment:vip_users", "set_off", {"checkout_v2": False}),
                ("checkout_v2", "segment:vip_users", "delete_override", {"checkout_v2": False}),
            ]
            for flag_n, scope_n, op_n, exp_n in ref_ops:
                ops.apply_operation(rehearsal_env, flag_n, scope_n, op_n, tok)
                v_try = _verify(cfg, rehearsal_url, "verify", rehearsal_state, exp_n, eval_timeout=15.0)
                ok_ref = (
                    not v_try.ok
                    and v_try.user_outcomes["alice"]["http_status"] == 500
                    and v_try.user_outcomes["carol"]["http_status"] == 500
                )
                rows.append(CaseRow("Refusal", f"{flag_n} {scope_n} {op_n}", "FAIL (no_compat)", "FAIL" if not v_try.ok else "PASS", _fmt_user(v_try, "alice"), _fmt_user(v_try, "carol"), _fmt_user(v_try, "bob"), ok_ref))
        finally:
            _stop_shop(proc)

        # Verify demo-target stayed untouched during rehearsal/refusal
        target_snap_mid = take_snapshot(ops, cfg, target_env)
        assert target_snap_mid.hash == target_snap_before.hash, "demo-target mutated before Target step!"

        # ------------------------------------------------------------------
        # TARGET (Case A repeated on demo-target, port 8002, own DB)
        # ------------------------------------------------------------------
        proc = None
        try:
            seed_case(ops, cfg, target_env, "a")
            target_flag_changes.append(f"[{target_env}] seed Case A: checkout_v2=ON, cart_compat_reader=OFF, vip_users override cleared")
            _reset_db(target_db, fixture_writes)
            proc = _start_shop(cfg.app.target_port, target_db, target_key)

            prep_t = _verify(cfg, target_url, "prepare", target_state)
            assert prep_t.ok, f"Target prepare failed: {prep_t.summary}"

            tok_t = issue_token("target", target_env)
            ops.apply_operation(target_env, "checkout_v2", "environment_default", "set_off", tok_t)
            target_flag_changes.append(f"[{target_env}] apply: checkout_v2 environment_default set_off")
            v_trb = _verify(cfg, target_url, "verify", target_state, {"checkout_v2": False})
            t_rb_ok = (
                not v_trb.ok
                and v_trb.user_outcomes["alice"]["http_status"] == 500
                and v_trb.user_outcomes["carol"]["http_status"] == 500
                and v_trb.user_outcomes["bob"]["http_status"] == 200
            )
            rows.append(CaseRow("Target", "Case A rollback only", "FAIL (500 loud)", "FAIL" if not v_trb.ok else "PASS", _fmt_user(v_trb, "alice"), _fmt_user(v_trb, "carol"), _fmt_user(v_trb, "bob"), t_rb_ok))

            ops.apply_operation(target_env, "cart_compat_reader", "environment_default", "set_on", tok_t)
            target_flag_changes.append(f"[{target_env}] apply: cart_compat_reader environment_default set_on")
            v_tfix = _verify(cfg, target_url, "verify", target_state, {"checkout_v2": False, "cart_compat_reader": True})
            rows.append(CaseRow("Target", "compat environment_default set_on", "PASS", "PASS" if v_tfix.ok else "FAIL", _fmt_user(v_tfix, "alice"), _fmt_user(v_tfix, "carol"), _fmt_user(v_tfix, "bob"), v_tfix.ok))
        finally:
            _stop_shop(proc)

    print("\n| Scenario | Action | Expected | Observed | alice | carol | bob | Check |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        mark = "PASS" if r.passed else "FAIL"
        print(f"| {r.scenario} | {r.step} | {r.expected} | {r.observed} | {r.alice} | {r.carol} | {r.bob} | {mark} |")

    print("\nTarget flag changes:")
    for item in target_flag_changes:
        print(f"- {item}")

    print("\nFixture writes:")
    for fw in fixture_writes:
        print(f"- {fw}")

    all_passed = all(r.passed for r in rows)
    print(f"\nSTEP9_OVERALL: {'PASS' if all_passed else 'FAIL'} ({sum(r.passed for r in rows)}/{len(rows)} assertions)")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
