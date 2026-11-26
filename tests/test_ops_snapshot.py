"""Unit tests for rewind.flagsmith_ops, rewind.approvals, and rewind.snapshot."""

import copy
import json
from pathlib import Path
import subprocess

import pytest

from rewind import approvals
from rewind.approvals import ApprovalError, ApprovalToken, gate1_rehearsal, gate2_target, mint_setup_token
from rewind.config import load_config
from rewind.flagsmith_ops import FlagsmithOps, FlagsmithOpsError
from rewind.snapshot import take_snapshot


def _make_config(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FLAGSMITH_ENV_KEY_REHEARSAL", "rehearsal_dummy_sdk_key_12345")
    monkeypatch.setenv("FLAGSMITH_ENV_KEY_TARGET", "target_dummy_sdk_key_67890")
    p = tmp_path / "rewind.config.yaml"
    p.write_text(
        """
project: demo-shop
flagsmith:
  rehearsal: { environment: rehearsal, key_env: FLAGSMITH_ENV_KEY_REHEARSAL }
  target:    { environment: demo-target, key_env: FLAGSMITH_ENV_KEY_TARGET }
app:
  start: "python app.py --port {port}"
  reset: "python reset.py --db {db}"
  journey: "python journey.py --base-url {base_url} --phase {phase}"
  rehearsal_port: 8001
  target_port: 8002
source_paths: ["examples/demo-shop/app.py"]
rollback:
  flag: checkout_v2
  to: off
  goal: "restore legacy"
users:
  - { id: alice, traits: { is_vip: true }, role: affected }
  - { id: bob, traits: { is_vip: false }, role: control }
allowed:
  flags: [checkout_v2, cart_compat_reader]
  scopes: [environment_default, "segment:vip_users"]
  operations: [set_on, set_off, delete_override]
""",
        encoding="utf-8",
    )
    return load_config(p)


class FakeFlagsmithRunner:
    def __init__(self, first_call_secret: str | None = None):
        self.calls: list[list[str]] = []
        self.first_call_secret = first_call_secret
        self.flags = {
            "checkout_v2": {"enabled": False, "value": None, "segment_overrides": 0, "identity_overrides": 0},
            "cart_compat_reader": {"enabled": False, "value": None, "segment_overrides": 0, "identity_overrides": 0},
        }
        self.seg_overrides: dict[tuple[str, str], dict] = {}
        self.id_overrides: dict[tuple[str, str], dict] = {}
        self.segments = [
            {
                "id": 1201561,
                "name": "vip_users",
                "rules": {
                    "type": "ALL",
                    "conditions": [{"property": "is_vip", "operator": "EQUAL", "value": "true"}],
                },
            }
        ]

    def __call__(self, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        sub = args[1:3]
        if sub == ["segment", "list"]:
            payload = copy.deepcopy(self.segments)
            if self.first_call_secret and len(self.calls) == 1:
                payload[0]["api_token"] = self.first_call_secret
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
        if sub == ["flag", "get"]:
            flag = args[3]
            if "--segment" in args:
                seg = args[args.index("--segment") + 1]
                ov = self.seg_overrides.get((flag, seg))
                if ov is None:
                    return subprocess.CompletedProcess(args, 1, "", "Error: segment override not found")
                return subprocess.CompletedProcess(args, 0, json.dumps(ov), "")
            if "--identifier" in args:
                ident = args[args.index("--identifier") + 1]
                ov = self.id_overrides.get((flag, ident))
                if ov is None:
                    return subprocess.CompletedProcess(args, 1, "", "Error: no override for identifier")
                return subprocess.CompletedProcess(args, 0, json.dumps(ov), "")
            data = dict(self.flags[flag])
            data["feature"] = flag
            if self.first_call_secret and len(self.calls) == 1:
                data["service_token"] = self.first_call_secret
            return subprocess.CompletedProcess(args, 0, json.dumps(data), "")
        if sub == ["flag", "update"]:
            flag = args[3]
            enabled = "--enable" in args
            if "--segment" in args:
                seg = args[args.index("--segment") + 1]
                self.seg_overrides[(flag, seg)] = {
                    "feature": flag,
                    "segment": {"name": seg},
                    "priority": 0,
                    "enabled": enabled,
                    "value": None,
                }
                self.flags[flag]["segment_overrides"] = len([k for k in self.seg_overrides if k[0] == flag])
            elif "--identifier" in args:
                ident = args[args.index("--identifier") + 1]
                self.id_overrides[(flag, ident)] = {"feature": flag, "identifier": ident, "enabled": enabled, "value": None}
                self.flags[flag]["identity_overrides"] = len([k for k in self.id_overrides if k[0] == flag])
            else:
                self.flags[flag]["enabled"] = enabled
            return subprocess.CompletedProcess(args, 0, "ok", "")
        if sub == ["flag", "delete"]:
            flag = args[3]
            if "--segment" in args:
                seg = args[args.index("--segment") + 1]
                self.seg_overrides.pop((flag, seg), None)
                self.flags[flag]["segment_overrides"] = len([k for k in self.seg_overrides if k[0] == flag])
            elif "--identifier" in args:
                ident = args[args.index("--identifier") + 1]
                self.id_overrides.pop((flag, ident), None)
                self.flags[flag]["identity_overrides"] = len([k for k in self.id_overrides if k[0] == flag])
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", "unknown test command")


def test_public_issue_token_removed_and_human_prompts_validated():
    assert not hasattr(approvals, "issue_token")

    with pytest.raises(ApprovalError, match="denied"):
        gate1_rehearsal("Rollback checkout_v2", environment="rehearsal", max_operations=2, input_fn=lambda _: "no", output_fn=lambda _: None)
    with pytest.raises(ApprovalError, match="denied"):
        gate2_target([("checkout_v2", "environment_default", "set_off")], "hash123", input_fn=lambda _: "y", output_fn=lambda _: None)

    human_tok = gate1_rehearsal("Rollback checkout_v2", environment="rehearsal", max_operations=2, input_fn=lambda _: "yes", output_fn=lambda _: None)
    assert human_tok.approved_by == "human (yes)"

    auto_tok = gate2_target([("checkout_v2", "environment_default", "set_off")], "hash123", auto_approve=True, output_fn=lambda _: None)
    assert auto_tok.approved_by == "auto-approved (test)"


def test_allowlist_and_approval_enforcement(tmp_path: Path, monkeypatch):
    cfg = _make_config(tmp_path, monkeypatch)
    runner = FakeFlagsmithRunner()
    ops = FlagsmithOps(cfg, cli_path="/usr/local/bin/flagsmith", runner=runner)

    with pytest.raises(FlagsmithOpsError, match="ApprovalToken"):
        ops.apply_operation("rehearsal", "checkout_v2", "environment_default", "set_off", approval=None)

    tok = _rehearsal_token("rehearsal checks")
    with pytest.raises(FlagsmithOpsError, match="allowlist"):
        ops.apply_operation("production", "checkout_v2", "environment_default", "set_off", approval=tok)

    with pytest.raises(FlagsmithOpsError, match="allowed.flags"):
        ops.apply_operation("rehearsal", "other_flag", "environment_default", "set_off", approval=tok)

    with pytest.raises(FlagsmithOpsError, match="allowed.scopes"):
        ops.apply_operation("rehearsal", "checkout_v2", "identity:alice", "set_off", approval=tok)

    with pytest.raises(FlagsmithOpsError, match="allowed.operations"):
        ops.apply_operation("rehearsal", "checkout_v2", "environment_default", "create_flag", approval=tok)

    with pytest.raises(FlagsmithOpsError, match="environment_default"):
        ops.apply_operation("rehearsal", "checkout_v2", "environment_default", "delete_override", approval=tok)

    assert runner.calls == []


def test_forged_wrong_phase_setup_reused_drift_and_changed_ops_all_rejected(tmp_path: Path, monkeypatch):
    cfg = _make_config(tmp_path, monkeypatch)
    runner = FakeFlagsmithRunner()
    ops = FlagsmithOps(cfg, cli_path="/usr/local/bin/flagsmith", runner=runner)

    # 1. Forged token rejected
    forged = ApprovalToken(
        token_id="000000000000000000000000",
        phase="rehearsal",
        approved_by="forger",
        issued_at="2026-11-26T00:00:00Z",
        summary="forged",
    )
    with pytest.raises(FlagsmithOpsError, match="forged"):
        ops.apply_operation("rehearsal", "checkout_v2", "environment_default", "set_off", forged)

    # 2. Wrong phase: rehearsal token cannot mutate target; target token cannot mutate rehearsal
    r_tok = _rehearsal_token("rehearse")
    with pytest.raises(FlagsmithOpsError, match="Gate 2 target token"):
        ops.apply_operation("demo-target", "checkout_v2", "environment_default", "set_off", r_tok)

    snap0 = take_snapshot(ops, cfg, "demo-target")
    t_tok_for_r = gate2_target([("checkout_v2", "environment_default", "set_off")], snap0.hash, auto_approve=True, output_fn=lambda _: None)
    with pytest.raises(FlagsmithOpsError, match="rejected token with phase='target'"):
        ops.apply_operation("rehearsal", "checkout_v2", "environment_default", "set_off", t_tok_for_r)

    # 3. Setup token can never mutate target environment
    setup_tok = mint_setup_token("seed")
    with pytest.raises(FlagsmithOpsError, match="Setup token cannot mutate"):
        ops.apply_operation("demo-target", "checkout_v2", "environment_default", "set_on", setup_tok)

    # 4. Rehearsal has a finite write budget
    single_tok = _rehearsal_token("one op")
    ops.apply_operation("rehearsal", "checkout_v2", "environment_default", "set_on", single_tok)
    with pytest.raises(FlagsmithOpsError, match="budget exhausted"):
        ops.apply_operation("rehearsal", "cart_compat_reader", "environment_default", "set_on", single_tok)

    # 5. Operation list changed after Gate 2 approval rejected without target mutation
    snap_before = take_snapshot(ops, cfg, "demo-target")
    calls_before = len(runner.calls)
    mismatch_tok = gate2_target(
        [("cart_compat_reader", "environment_default", "set_on")],
        snap_before.hash,
        auto_approve=True,
        output_fn=lambda _: None,
    )
    with pytest.raises(FlagsmithOpsError, match="does not match Gate 2 approval"):
        ops.apply_operation("demo-target", "checkout_v2", "environment_default", "set_off", mismatch_tok)
    assert len(runner.calls) == calls_before
    # Even the consumed mismatched token cannot be reused cleanly
    with pytest.raises(FlagsmithOpsError, match="already been used"):
        ops.apply_operation("demo-target", "cart_compat_reader", "environment_default", "set_on", mismatch_tok)

    # 6. Drifted snapshot between Gate 2 approval and apply refuses and requires Gate 2 again
    drift_tok = gate2_target(
        [("cart_compat_reader", "environment_default", "set_on")],
        snap_before.hash,
        auto_approve=True,
        output_fn=lambda _: None,
    )
    # Simulate external drift in demo-target before apply runs (toggle checkout_v2)
    runner.flags["checkout_v2"]["enabled"] = not runner.flags["checkout_v2"]["enabled"]
    with pytest.raises(FlagsmithOpsError, match="Target snapshot drifted"):
        ops.apply_operation("demo-target", "cart_compat_reader", "environment_default", "set_on", drift_tok)
    assert runner.flags["cart_compat_reader"]["enabled"] is False
    with pytest.raises(FlagsmithOpsError, match="already been used"):
        ops.apply_operation("demo-target", "cart_compat_reader", "environment_default", "set_on", drift_tok)

    # Fresh Gate 2 against drifted snapshot succeeds cleanly
    snap_drifted = take_snapshot(ops, cfg, "demo-target")
    fresh_tok = gate2_target(
        [("cart_compat_reader", "environment_default", "set_on")],
        snap_drifted.hash,
        auto_approve=True,
        output_fn=lambda _: None,
    )
    ops.apply_operation("demo-target", "cart_compat_reader", "environment_default", "set_on", fresh_tok)
    assert runner.flags["cart_compat_reader"]["enabled"] is True
    with pytest.raises(FlagsmithOpsError, match="already been used"):
        ops.apply_operation("demo-target", "cart_compat_reader", "environment_default", "set_on", fresh_tok)


def _rehearsal_token(summary: str, max_operations: int = 1):
    return gate1_rehearsal(
        summary, environment="rehearsal", max_operations=max_operations,
        auto_approve=True, output_fn=lambda _: None,
    )


def test_rehearsal_session_budget_use_log_and_close(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path, monkeypatch)
    runner = FakeFlagsmithRunner()
    ops = FlagsmithOps(cfg, runner=runner)
    prompts: list[str] = []
    shown: list[str] = []
    token = gate1_rehearsal(
        "Rollback and test one proposal", environment="rehearsal", max_operations=2,
        input_fn=lambda prompt: prompts.append(prompt) or "yes", output_fn=shown.append,
    )
    rollback = ("checkout_v2", "environment_default", "set_off")
    fix = ("cart_compat_reader", "segment:vip_users", "set_on")
    ops.apply_operation("rehearsal", *rollback, token)
    with pytest.raises(FlagsmithOpsError, match="requires a Gate 2 target token"):
        ops.apply_operation("demo-target", *fix, token)
    ops.apply_operation("rehearsal", *fix, token)
    with pytest.raises(FlagsmithOpsError, match="budget exhausted"):
        ops.apply_operation("rehearsal", *rollback, token)
    uses = approvals.approval_uses(token)
    assert [(u.flag, u.scope, u.operation) for u in uses] == [rollback, fix]
    assert all(use.timestamp.endswith("+00:00") for use in uses)
    assert len(runner.calls) == 2 and len(prompts) == 1
    assert any("maximum Flagsmith operations: 2" in line for line in shown)

    live_budget = _rehearsal_token("another run", max_operations=2)
    ops.apply_operation("rehearsal", *rollback, live_budget)
    approvals.close_rehearsal(live_budget)
    with pytest.raises(FlagsmithOpsError, match="session is closed"):
        ops.apply_operation("rehearsal", *fix, live_budget)
    assert len(approvals.approval_uses(live_budget)) == 1
    assert len(runner.calls) == 3


def test_env_secrets_only_registered_when_length_ge_8_and_first_log_redacted(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SHORT_KEY", "false")
    monkeypatch.setenv("SEVEN_TOKEN", "1234567")
    monkeypatch.setenv("EIGHT_SECRET", "secret12")
    cfg = _make_config(tmp_path, monkeypatch)
    synthetic_secret = "synthetic_returned_cred_987654"
    runner = FakeFlagsmithRunner(first_call_secret=synthetic_secret)
    ops = FlagsmithOps(cfg, cli_path="/usr/local/bin/flagsmith", runner=runner)

    # Short values (< 8) must not be registered or mangle normal text
    assert ops.redact("flag=false count=1234567 val=secret12") == "flag=false count=1234567 val=<REDACTED>"

    ops.list_segments()
    assert len(ops.command_log) == 1
    assert synthetic_secret not in ops.command_log[0].stdout
    assert "<REDACTED>" in ops.command_log[0].stdout


def test_clear_identity_override_requires_setup_authority_and_never_touches_target(tmp_path: Path, monkeypatch):
    cfg = _make_config(tmp_path, monkeypatch)
    runner = FakeFlagsmithRunner()
    runner.id_overrides[("checkout_v2", "alice")] = {"enabled": True, "value": None}
    runner.flags["checkout_v2"]["identity_overrides"] = 1
    ops = FlagsmithOps(cfg, cli_path="/usr/local/bin/flagsmith", runner=runner)

    r_tok = _rehearsal_token("rehearsal")
    with pytest.raises(FlagsmithOpsError, match="Setup"):
        ops.clear_identity_override_if_present("rehearsal", "checkout_v2", "alice", r_tok)

    cleanup_tok = mint_setup_token("cleanup")
    with pytest.raises(FlagsmithOpsError, match="target environment"):
        ops.clear_identity_override_if_present("demo-target", "checkout_v2", "alice", cleanup_tok)

    with pytest.raises(FlagsmithOpsError, match="not a configured user"):
        ops.clear_identity_override_if_present("rehearsal", "checkout_v2", "eve", cleanup_tok)

    removed = ops.clear_identity_override_if_present("rehearsal", "checkout_v2", "alice", cleanup_tok)
    assert removed is True
    assert runner.flags["checkout_v2"]["identity_overrides"] == 0


def test_snapshot_hash_tracks_rules_identity_values_and_rejects_untracked(tmp_path: Path, monkeypatch):
    cfg = _make_config(tmp_path, monkeypatch)
    runner = FakeFlagsmithRunner()
    ops = FlagsmithOps(cfg, cli_path="/usr/local/bin/flagsmith", runner=runner)

    snap1 = take_snapshot(ops, cfg, "rehearsal")
    snap2 = take_snapshot(ops, cfg, "rehearsal")
    assert snap1.hash == snap2.hash

    runner.segments[0]["rules"]["conditions"][0]["value"] = "false"
    snap_rules = take_snapshot(ops, cfg, "rehearsal")
    assert snap_rules.hash != snap1.hash

    runner.id_overrides[("checkout_v2", "alice")] = {"enabled": True, "value": None}
    runner.flags["checkout_v2"]["identity_overrides"] = 1
    snap_id_true = take_snapshot(ops, cfg, "rehearsal")

    runner.id_overrides[("checkout_v2", "alice")] = {"enabled": False, "value": None}
    snap_id_false = take_snapshot(ops, cfg, "rehearsal")
    assert snap_id_false.hash != snap_id_true.hash

    runner.flags["checkout_v2"]["identity_overrides"] = 2
    with pytest.raises(FlagsmithOpsError, match="Unsupported or unconfigured identity override"):
        take_snapshot(ops, cfg, "rehearsal")

    runner.flags["checkout_v2"]["identity_overrides"] = 1
    runner.flags["checkout_v2"]["segment_overrides"] = 1
    with pytest.raises(FlagsmithOpsError, match="Unsupported or unconfigured segment override"):
        take_snapshot(ops, cfg, "rehearsal")
    runner.flags["checkout_v2"]["segment_overrides"] = 0

    tok1 = _rehearsal_token("compat on")
    ops.apply_operation("rehearsal", "cart_compat_reader", "environment_default", "set_on", approval=tok1)
    snap3 = take_snapshot(ops, cfg, "rehearsal")
    assert snap3.hash != snap_id_false.hash

    tok2 = _rehearsal_token("vip on")
    ops.apply_operation("rehearsal", "checkout_v2", "segment:vip_users", "set_on", approval=tok2)
    snap4 = take_snapshot(ops, cfg, "rehearsal")
    assert snap4.hash != snap3.hash
    assert snap4.state["flags"]["checkout_v2"]["overrides"]["vip_users"]["enabled"] is True

    for entry in ops.command_log:
        assert "rehearsal_dummy_sdk_key_12345" not in entry.command
        if "--environment" in entry.command:
            assert "<REDACTED>" in entry.command


_TARGET_ORDER = (
    ("checkout_v2", "environment_default", "set_on"),
    ("checkout_v2", "environment_default", "set_off"),
    ("cart_compat_reader", "environment_default", "set_on"),
)


def _recorded_checkout_failure():
    from rewind.verifier import VerificationResult

    flags = {"checkout_v2": False, "cart_compat_reader": False}
    return VerificationResult(
        False, "verify", 1, "legacy rejects the saved cart",
        user_outcomes={
            "alice": {"step": "checkout", "http_status": 500, "executed_version": "legacy", "effective_flags": flags},
            "bob": {"step": "checkout", "http_status": 200, "executed_version": "legacy", "effective_flags": flags},
        },
        failed_assertions=[{"step": "checkout", "user": "alice", "assertion": "http_status_200", "detail": "Unrecognized cart schema"}],
    )


def _target_plan_fixture(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path, monkeypatch)
    runner = FakeFlagsmithRunner()
    ops = FlagsmithOps(cfg, runner=runner)
    snap = take_snapshot(ops, cfg, "demo-target")
    proof = _recorded_checkout_failure()
    rendered = []
    token = gate2_target({
        "environment": "demo-target", "operations": _TARGET_ORDER, "recovery_index": 2,
        "stages": ["baseline setup", "rollback", "recovery"], "rehearsal_failure": proof,
        "reproduction_flags": cfg.allowed.flags, "starting_state": snap.state,
        "db_path": str(tmp_path / "target.db"), "fixture_writes": ["reset fixture", "save test carts", "record test orders"],
    }, snap.hash, input_fn=lambda _: "yes", output_fn=rendered.append)
    return ops, runner, token, proof, "\n".join(rendered)


def _count_writes(runner):
    return sum(call[1:3] in (["flag", "update"], ["flag", "delete"]) for call in runner.calls)


def test_ordered_gate2_accepts_own_writes_and_real_failure_evidence(tmp_path, monkeypatch):
    ops, runner, token, proof, shown = _target_plan_fixture(tmp_path, monkeypatch)
    assert all(text in shown for text in ("baseline setup", "rollback", "recovery", "target.db", "save test carts",
                                         "Starting flag state", "recovery runs only if the failure reproduces"))
    with ops.approved_target_plan("demo-target", _TARGET_ORDER, token) as replay:
        with pytest.raises(AttributeError):
            token.cursor = 2
        with pytest.raises(AttributeError):
            replay.cursor = 2
        replay.apply_next(*_TARGET_ORDER[0])
        replay.apply_next(*_TARGET_ORDER[1])
        replay.record_failure(proof)
        replay.apply_next(*_TARGET_ORDER[2])
    assert _count_writes(runner) == 3
    assert len(approvals.approval_uses(token)) == 3
    with pytest.raises(FlagsmithOpsError, match="already been used"):
        with ops.approved_target_plan("demo-target", _TARGET_ORDER, token):
            pytest.fail("Completed plan reopened")


@pytest.mark.parametrize("first, second", [(None, 1), (None, 2), (0, 0)])
def test_ordered_gate2_rejects_skipping_reordering_and_repeating(tmp_path, monkeypatch, first, second):
    ops, runner, token, _, _ = _target_plan_fixture(tmp_path, monkeypatch)
    with pytest.raises(FlagsmithOpsError, match="not the next approved target operation"):
        with ops.approved_target_plan("demo-target", _TARGET_ORDER, token) as replay:
            if first is not None:
                replay.apply_next(*_TARGET_ORDER[first])
            replay.apply_next(*_TARGET_ORDER[second])
    assert _count_writes(runner) == (first is not None)


def test_ordered_gate2_recovery_is_locked_without_reproduction(tmp_path, monkeypatch):
    ops, runner, token, _, _ = _target_plan_fixture(tmp_path, monkeypatch)
    with pytest.raises(FlagsmithOpsError, match="Recovery is locked"):
        with ops.approved_target_plan("demo-target", _TARGET_ORDER, token) as replay:
            replay.apply_next(*_TARGET_ORDER[0])
            replay.apply_next(*_TARGET_ORDER[1])
            replay.apply_next(*_TARGET_ORDER[2])
    assert _count_writes(runner) == 2


def test_ordered_gate2_rejects_a_boolean_instead_of_evidence(tmp_path, monkeypatch):
    ops, runner, token, _, _ = _target_plan_fixture(tmp_path, monkeypatch)
    with pytest.raises(FlagsmithOpsError, match="target did not reproduce the failure"):
        with ops.approved_target_plan("demo-target", _TARGET_ORDER, token) as replay:
            replay.apply_next(*_TARGET_ORDER[0])
            replay.apply_next(*_TARGET_ORDER[1])
            replay.record_failure(True)
    assert _count_writes(runner) == 2
    with pytest.raises(FlagsmithOpsError, match="Target plan is not active"):
        replay.apply_next(*_TARGET_ORDER[2])


def test_ordered_gate2_checks_drift_after_its_own_write(tmp_path, monkeypatch):
    ops, runner, token, _, _ = _target_plan_fixture(tmp_path, monkeypatch)
    with pytest.raises(FlagsmithOpsError, match="Target snapshot drifted.*Gate 2 approval required again"):
        with ops.approved_target_plan("demo-target", _TARGET_ORDER, token) as replay:
            replay.apply_next(*_TARGET_ORDER[0])
            runner.flags["cart_compat_reader"]["enabled"] = True  # another operator
            replay.apply_next(*_TARGET_ORDER[1])
    assert _count_writes(runner) == 1
    assert runner.flags["checkout_v2"]["enabled"] is True  # no revert
