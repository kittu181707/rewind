"""Step 9 End-to-End Live Verification (No LLM) using seed, demo-shop, verifier, and flagsmith_ops."""

from __future__ import annotations

import argparse
import copy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
import sqlite3
import subprocess
import sys
import time
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


def _reset_db(db_path: Path, label: str, writes_log: list[str]) -> None:
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "examples" / "demo-shop" / "reset.py"), "--db", str(db_path)],
        cwd=str(REPO_ROOT),
        check=True,
        capture_output=True,
        text=True,
    )
    writes_log.append(f"[{label}] SQLite reset -> {db_path} (3 customers, 3 v1 carts)")


def _inspect_db_writes(db_path: Path, label: str) -> list[str]:
    """Enumerate actual SQLite cart mutations and order inserts recorded in db_path."""
    if not db_path.exists():
        return []
    entries: list[str] = []
    conn = sqlite3.connect(str(db_path))
    try:
        carts = conn.execute(
            "SELECT cart_id, user_id, schema_version, updated_at FROM carts ORDER BY user_id"
        ).fetchall()
        for cart_id, uid, schema_ver, updated_at in carts:
            entries.append(f"[{label}] SQLite cart row -> {cart_id} (user={uid}, schema={schema_ver}, updated={updated_at})")
        orders = conn.execute(
            "SELECT order_id, cart_id, user_id, executed_version, total_items, total_cents FROM orders ORDER BY rowid"
        ).fetchall()
        for oid, cid, uid, exec_ver, qty, cents in orders:
            entries.append(
                f"[{label}] SQLite order insert -> {oid} (user={uid}, cart={cid}, executed={exec_ver}, items={qty}, cents={cents})"
            )
    finally:
        conn.close()
    return entries


def _verify(
    cfg: RewindConfig,
    base_url: str,
    phase: str,
    state_file: Path,
    journey_out_file: Path,
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
    res = verify_journey(cfg, base_url=base_url, phase=phase, extra_args=extra, poll_eval=False)
    journey_out_file.parent.mkdir(parents=True, exist_ok=True)
    journey_out_file.write_text(
        json.dumps(
            {
                "ok": res.ok,
                "phase": res.phase,
                "exit_code": res.exit_code,
                "summary": res.summary,
                "user_outcomes": res.user_outcomes,
                "failed_assertions": res.failed_assertions,
                "steps": res.steps,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return res


def _flush_evidence(
    out_dir: Path,
    rows: list[CaseRow],
    target_flag_changes: list[str],
    fixture_writes: list[str],
    ops_list: list[tuple[str, FlagsmithOps]],
    status: str,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    command_logs: dict[str, list[dict[str, Any]]] = {}
    for label, ops_inst in ops_list:
        command_logs[label] = [asdict(rec) for rec in ops_inst.command_log]

    payload = {
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows": [asdict(r) for r in rows],
        "target_flag_changes": target_flag_changes,
        "fixture_writes": fixture_writes,
        "command_logs": command_logs,
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return summary_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run live no-LLM Step 9 E2E verification.")
    parser.add_argument("--output-dir", default=None, help="Directory for persistent redacted E2E evidence.")
    args = parser.parse_args(argv)

    load_dotenv(REPO_ROOT / ".env")
    cfg = load_config(REPO_ROOT / "examples" / "demo-shop" / "rewind.config.yaml")

    # Test-scoped config copy permitting identity:alice ONLY for negative verification proofs.
    cfg_identity = copy.deepcopy(cfg)
    cfg_identity.allowed.scopes.append("identity:alice")

    ops = FlagsmithOps(cfg)
    ops_identity = FlagsmithOps(cfg_identity)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.output_dir) if args.output_dir else (REPO_ROOT / "artifacts" / "e2e" / run_id)
    journeys_dir = out_dir / "journeys"
    out_dir.mkdir(parents=True, exist_ok=True)

    rehearsal_env = cfg.flagsmith.rehearsal.environment
    target_env = cfg.flagsmith.target.environment
    rehearsal_key = cfg.env_key_for(rehearsal_env)
    target_key = cfg.env_key_for(target_env)
    rehearsal_url = f"http://127.0.0.1:{cfg.app.rehearsal_port}"
    target_url = f"http://127.0.0.1:{cfg.app.target_port}"

    rows: list[CaseRow] = []
    target_flag_changes: list[str] = []
    fixture_writes: list[str] = []
    step_idx = 0

    def record_or_fail(row: CaseRow) -> None:
        rows.append(row)
        if not row.passed:
            summary_path = _flush_evidence(
                out_dir,
                rows,
                target_flag_changes,
                fixture_writes,
                [("ops", ops), ("ops_identity", ops_identity)],
                status="FAILED_FAST",
            )
            raise RuntimeError(
                f"Step 9 fail-fast abort at {row.scenario} / {row.step}: "
                f"expected={row.expected}, observed={row.observed} (alice={row.alice}, carol={row.carol}, bob={row.bob}). "
                f"Redacted evidence saved to {summary_path}"
            )

    def next_journey_path(tag: str) -> Path:
        nonlocal step_idx
        step_idx += 1
        return journeys_dir / f"{step_idx:02d}_{tag}.json"

    target_snap_before = take_snapshot(ops, cfg, target_env)

    rehearsal_db = out_dir / "rehearsal.db"
    rehearsal_state = out_dir / "rehearsal_state.json"
    target_db = out_dir / "target.db"
    target_state = out_dir / "target_state.json"

    # ------------------------------------------------------------------
    # CASE A (Loud 500) on rehearsal
    # ------------------------------------------------------------------
    proc = None
    try:
        seed_case(ops, cfg, rehearsal_env, "a")
        _reset_db(rehearsal_db, "Case A", fixture_writes)
        proc = _start_shop(cfg.app.rehearsal_port, rehearsal_db, rehearsal_key)

        prep = _verify(cfg, rehearsal_url, "prepare", rehearsal_state, next_journey_path("case_a_prepare"))
        assert prep.ok, f"Case A prepare failed: {prep.summary}"

        tok = issue_token("rehearsal", rehearsal_env)
        ops.apply_operation(rehearsal_env, "checkout_v2", "environment_default", "set_off", tok)
        v_rb = _verify(cfg, rehearsal_url, "verify", rehearsal_state, next_journey_path("case_a_rollback"), {"checkout_v2": False})
        record_or_fail(CaseRow(
            "Case A",
            "Rollback only",
            "FAIL (500 loud)",
            "FAIL" if not v_rb.ok else "PASS",
            _fmt_user(v_rb, "alice"),
            _fmt_user(v_rb, "carol"),
            _fmt_user(v_rb, "bob"),
            (
                not v_rb.ok
                and v_rb.user_outcomes["alice"]["http_status"] == 500
                and v_rb.user_outcomes["carol"]["http_status"] == 500
                and v_rb.user_outcomes["bob"]["http_status"] == 200
                and v_rb.user_outcomes["bob"]["executed_version"] == "legacy"
            ),
        ))

        blocked = False
        try:
            ops.apply_operation(rehearsal_env, "cart_compat_reader", "identity:alice", "set_on", tok)
        except FlagsmithOpsError:
            blocked = True
        assert blocked, "Default config should block identity:alice"

        tok_id = issue_token("rehearsal-negative-test", rehearsal_env)
        tok_cleanup = issue_token("cleanup", rehearsal_env)
        ops_identity.apply_operation(rehearsal_env, "cart_compat_reader", "identity:alice", "set_on", tok_id)
        try:
            v_id = _verify(
                cfg,
                rehearsal_url,
                "verify",
                rehearsal_state,
                next_journey_path("case_a_identity_alice"),
                {"checkout_v2": False},
                eval_timeout=15.0,
            )
            record_or_fail(CaseRow(
                "Case A",
                "identity:alice compat",
                "FAIL (carol 500)",
                "FAIL" if not v_id.ok else "PASS",
                _fmt_user(v_id, "alice"),
                _fmt_user(v_id, "carol"),
                _fmt_user(v_id, "bob"),
                (
                    not v_id.ok
                    and v_id.user_outcomes["alice"]["http_status"] == 200
                    and v_id.user_outcomes["carol"]["http_status"] == 500
                ),
            ))
        finally:
            ops_identity.clear_identity_override_if_present(rehearsal_env, "cart_compat_reader", "alice", tok_cleanup)

        ops.apply_operation(rehearsal_env, "cart_compat_reader", "environment_default", "set_on", tok)
        v_fix = _verify(
            cfg,
            rehearsal_url,
            "verify",
            rehearsal_state,
            next_journey_path("case_a_fix"),
            {"checkout_v2": False, "cart_compat_reader": True},
        )
        record_or_fail(CaseRow(
            "Case A",
            "compat environment_default set_on",
            "PASS",
            "PASS" if v_fix.ok else "FAIL",
            _fmt_user(v_fix, "alice"),
            _fmt_user(v_fix, "carol"),
            _fmt_user(v_fix, "bob"),
            v_fix.ok,
        ))
        fixture_writes.extend(_inspect_db_writes(rehearsal_db, "Case A"))
    finally:
        _stop_shop(proc)

    # ------------------------------------------------------------------
    # CASE B (Silent segment override) on rehearsal
    # ------------------------------------------------------------------
    proc = None
    try:
        seed_case(ops, cfg, rehearsal_env, "b")
        _reset_db(rehearsal_db, "Case B (set_off)", fixture_writes)
        proc = _start_shop(cfg.app.rehearsal_port, rehearsal_db, rehearsal_key)

        prep = _verify(cfg, rehearsal_url, "prepare", rehearsal_state, next_journey_path("case_b_prepare"))
        assert prep.ok, f"Case B prepare failed: {prep.summary}"

        tok = issue_token("rehearsal", rehearsal_env)
        ops.apply_operation(rehearsal_env, "checkout_v2", "environment_default", "set_off", tok)
        v_silent = _verify(
            cfg,
            rehearsal_url,
            "verify",
            rehearsal_state,
            next_journey_path("case_b_rollback"),
            {"checkout_v2": False},
            eval_timeout=10.0,
        )
        record_or_fail(CaseRow(
            "Case B",
            "Rollback only (silent)",
            "FAIL (alice/carol v2)",
            "FAIL" if not v_silent.ok else "PASS",
            _fmt_user(v_silent, "alice"),
            _fmt_user(v_silent, "carol"),
            _fmt_user(v_silent, "bob"),
            (
                not v_silent.ok
                and v_silent.user_outcomes["alice"]["http_status"] == 200
                and v_silent.user_outcomes["alice"]["executed_version"] == "v2"
                and v_silent.user_outcomes["carol"]["http_status"] == 200
                and v_silent.user_outcomes["carol"]["executed_version"] == "v2"
                and v_silent.user_outcomes["bob"]["executed_version"] == "legacy"
            ),
        ))

        ops.apply_operation(rehearsal_env, "checkout_v2", "environment_default", "set_off", tok)
        v_env = _verify(
            cfg,
            rehearsal_url,
            "verify",
            rehearsal_state,
            next_journey_path("case_b_env_default"),
            {"checkout_v2": False},
            eval_timeout=10.0,
        )
        record_or_fail(CaseRow(
            "Case B",
            "environment_default set_off",
            "FAIL (still v2)",
            "FAIL" if not v_env.ok else "PASS",
            _fmt_user(v_env, "alice"),
            _fmt_user(v_env, "carol"),
            _fmt_user(v_env, "bob"),
            (
                not v_env.ok
                and v_env.user_outcomes["alice"]["executed_version"] == "v2"
                and v_env.user_outcomes["carol"]["executed_version"] == "v2"
            ),
        ))

        tok_id = issue_token("rehearsal-negative-test", rehearsal_env)
        tok_cleanup = issue_token("cleanup", rehearsal_env)
        ops_identity.apply_operation(rehearsal_env, "checkout_v2", "identity:alice", "set_off", tok_id)
        try:
            v_idb = _verify(
                cfg,
                rehearsal_url,
                "verify",
                rehearsal_state,
                next_journey_path("case_b_identity_alice"),
                {"checkout_v2": False},
                eval_timeout=10.0,
            )
            record_or_fail(CaseRow(
                "Case B",
                "identity:alice set_off",
                "FAIL (carol v2)",
                "FAIL" if not v_idb.ok else "PASS",
                _fmt_user(v_idb, "alice"),
                _fmt_user(v_idb, "carol"),
                _fmt_user(v_idb, "bob"),
                (
                    not v_idb.ok
                    and v_idb.user_outcomes["alice"]["executed_version"] == "legacy"
                    and v_idb.user_outcomes["carol"]["executed_version"] == "v2"
                ),
            ))
        finally:
            ops_identity.clear_identity_override_if_present(rehearsal_env, "checkout_v2", "alice", tok_cleanup)

        ops.apply_operation(rehearsal_env, "checkout_v2", "segment:vip_users", "set_off", tok)
        v_seg_off = _verify(
            cfg,
            rehearsal_url,
            "verify",
            rehearsal_state,
            next_journey_path("case_b_seg_off"),
            {"checkout_v2": False, "cart_compat_reader": True},
        )
        record_or_fail(CaseRow(
            "Case B",
            "segment:vip_users set_off",
            "PASS",
            "PASS" if v_seg_off.ok else "FAIL",
            _fmt_user(v_seg_off, "alice"),
            _fmt_user(v_seg_off, "carol"),
            _fmt_user(v_seg_off, "bob"),
            v_seg_off.ok,
        ))
        fixture_writes.extend(_inspect_db_writes(rehearsal_db, "Case B (set_off)"))

        seed_case(ops, cfg, rehearsal_env, "b")
        _reset_db(rehearsal_db, "Case B (delete_override)", fixture_writes)
        prep2 = _verify(cfg, rehearsal_url, "prepare", rehearsal_state, next_journey_path("case_b_del_prepare"))
        assert prep2.ok, f"Case B (del) prepare failed: {prep2.summary}"
        ops.apply_operation(rehearsal_env, "checkout_v2", "environment_default", "set_off", tok)
        ops.apply_operation(rehearsal_env, "checkout_v2", "segment:vip_users", "delete_override", tok)
        v_seg_del = _verify(
            cfg,
            rehearsal_url,
            "verify",
            rehearsal_state,
            next_journey_path("case_b_seg_del"),
            {"checkout_v2": False, "cart_compat_reader": True},
        )
        record_or_fail(CaseRow(
            "Case B",
            "segment:vip_users delete_override",
            "PASS",
            "PASS" if v_seg_del.ok else "FAIL",
            _fmt_user(v_seg_del, "alice"),
            _fmt_user(v_seg_del, "carol"),
            _fmt_user(v_seg_del, "bob"),
            v_seg_del.ok,
        ))
        fixture_writes.extend(_inspect_db_writes(rehearsal_db, "Case B (delete_override)"))
    finally:
        _stop_shop(proc)

    # ------------------------------------------------------------------
    # REFUSAL (APP_VARIANT=no_compat) on rehearsal
    # ------------------------------------------------------------------
    proc = None
    try:
        seed_case(ops, cfg, rehearsal_env, "refusal")
        _reset_db(rehearsal_db, "Refusal", fixture_writes)
        proc = _start_shop(cfg.app.rehearsal_port, rehearsal_db, rehearsal_key, app_variant="no_compat")

        prep = _verify(cfg, rehearsal_url, "prepare", rehearsal_state, next_journey_path("refusal_prepare"))
        assert prep.ok, f"Refusal prepare failed: {prep.summary}"

        tok = issue_token("rehearsal", rehearsal_env)
        ops.apply_operation(rehearsal_env, "checkout_v2", "environment_default", "set_off", tok)
        v_ref0 = _verify(
            cfg,
            rehearsal_url,
            "verify",
            rehearsal_state,
            next_journey_path("refusal_rollback"),
            {"checkout_v2": False},
        )
        record_or_fail(CaseRow(
            "Refusal",
            "Rollback only (no_compat)",
            "FAIL (500)",
            "FAIL" if not v_ref0.ok else "PASS",
            _fmt_user(v_ref0, "alice"),
            _fmt_user(v_ref0, "carol"),
            _fmt_user(v_ref0, "bob"),
            not v_ref0.ok,
        ))

        ref_ops = [
            ("cart_compat_reader", "environment_default", "set_on", {"checkout_v2": False, "cart_compat_reader": True}),
            ("cart_compat_reader", "segment:vip_users", "set_on", {"checkout_v2": False, "cart_compat_reader": True}),
            ("checkout_v2", "segment:vip_users", "set_off", {"checkout_v2": False}),
            ("checkout_v2", "segment:vip_users", "delete_override", {"checkout_v2": False}),
        ]
        for idx, (flag_n, scope_n, op_n, exp_n) in enumerate(ref_ops, start=1):
            ops.apply_operation(rehearsal_env, flag_n, scope_n, op_n, tok)
            v_try = _verify(
                cfg,
                rehearsal_url,
                "verify",
                rehearsal_state,
                next_journey_path(f"refusal_op_{idx}"),
                exp_n,
                eval_timeout=15.0,
            )
            record_or_fail(CaseRow(
                "Refusal",
                f"{flag_n} {scope_n} {op_n}",
                "FAIL (no_compat)",
                "FAIL" if not v_try.ok else "PASS",
                _fmt_user(v_try, "alice"),
                _fmt_user(v_try, "carol"),
                _fmt_user(v_try, "bob"),
                (
                    not v_try.ok
                    and v_try.user_outcomes["alice"]["http_status"] == 500
                    and v_try.user_outcomes["carol"]["http_status"] == 500
                ),
            ))
        fixture_writes.extend(_inspect_db_writes(rehearsal_db, "Refusal"))
    finally:
        _stop_shop(proc)

    target_snap_mid = take_snapshot(ops, cfg, target_env)
    assert target_snap_mid.hash == target_snap_before.hash, "demo-target mutated before Target step!"

    # ------------------------------------------------------------------
    # TARGET (Case A repeated on demo-target, port 8002, own DB)
    # ------------------------------------------------------------------
    proc = None
    try:
        seed_case(ops, cfg, target_env, "a")
        target_flag_changes.append(f"[{target_env}] seed Case A: checkout_v2=ON, cart_compat_reader=OFF, overrides cleared")
        _reset_db(target_db, "Target", fixture_writes)
        proc = _start_shop(cfg.app.target_port, target_db, target_key)

        prep_t = _verify(cfg, target_url, "prepare", target_state, next_journey_path("target_prepare"))
        assert prep_t.ok, f"Target prepare failed: {prep_t.summary}"

        tok_t = issue_token("target", target_env)
        ops.apply_operation(target_env, "checkout_v2", "environment_default", "set_off", tok_t)
        target_flag_changes.append(f"[{target_env}] apply: checkout_v2 environment_default set_off")
        v_trb = _verify(
            cfg,
            target_url,
            "verify",
            target_state,
            next_journey_path("target_rollback"),
            {"checkout_v2": False},
        )
        record_or_fail(CaseRow(
            "Target",
            "Case A rollback only",
            "FAIL (500 loud)",
            "FAIL" if not v_trb.ok else "PASS",
            _fmt_user(v_trb, "alice"),
            _fmt_user(v_trb, "carol"),
            _fmt_user(v_trb, "bob"),
            (
                not v_trb.ok
                and v_trb.user_outcomes["alice"]["http_status"] == 500
                and v_trb.user_outcomes["carol"]["http_status"] == 500
                and v_trb.user_outcomes["bob"]["http_status"] == 200
            ),
        ))

        ops.apply_operation(target_env, "cart_compat_reader", "environment_default", "set_on", tok_t)
        target_flag_changes.append(f"[{target_env}] apply: cart_compat_reader environment_default set_on")
        v_tfix = _verify(
            cfg,
            target_url,
            "verify",
            target_state,
            next_journey_path("target_fix"),
            {"checkout_v2": False, "cart_compat_reader": True},
        )
        record_or_fail(CaseRow(
            "Target",
            "compat environment_default set_on",
            "PASS",
            "PASS" if v_tfix.ok else "FAIL",
            _fmt_user(v_tfix, "alice"),
            _fmt_user(v_tfix, "carol"),
            _fmt_user(v_tfix, "bob"),
            v_tfix.ok,
        ))
        fixture_writes.extend(_inspect_db_writes(target_db, "Target"))
    finally:
        _stop_shop(proc)

    summary_path = _flush_evidence(
        out_dir,
        rows,
        target_flag_changes,
        fixture_writes,
        [("ops", ops), ("ops_identity", ops_identity)],
        status="PASS",
    )

    print("\n| Scenario | Action | Expected | Observed | alice | carol | bob | Check |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        mark = "PASS" if r.passed else "FAIL"
        print(f"| {r.scenario} | {r.step} | {r.expected} | {r.observed} | {r.alice} | {r.carol} | {r.bob} | {mark} |")

    print("\nTarget flag changes:")
    for item in target_flag_changes:
        print(f"- {item}")

    print("\nFixture & DB writes:")
    for fw in fixture_writes:
        print(f"- {fw}")

    print(f"\nRedacted E2E evidence retained in: {summary_path}")
    print(f"STEP9_OVERALL: PASS ({len(rows)}/{len(rows)} assertions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
