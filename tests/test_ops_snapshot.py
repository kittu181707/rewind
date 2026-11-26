"""Unit tests for rewind.flagsmith_ops, rewind.approvals, and rewind.snapshot."""

import json
import subprocess
from pathlib import Path
import pytest

from rewind.approvals import issue_token
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
    def __init__(self):
        self.calls: list[list[str]] = []
        self.flags = {
            "checkout_v2": {"enabled": False, "value": None, "segment_overrides": 0, "identity_overrides": 0},
            "cart_compat_reader": {"enabled": False, "value": None, "segment_overrides": 0, "identity_overrides": 0},
        }
        self.overrides: dict[tuple[str, str], dict] = {}

    def __call__(self, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        sub = args[1:3]
        if sub == ["flag", "get"]:
            flag = args[3]
            if "--segment" in args:
                seg = args[args.index("--segment") + 1]
                ov = self.overrides.get((flag, seg))
                if ov is None:
                    return subprocess.CompletedProcess(args, 1, "", "Error: segment override not found")
                return subprocess.CompletedProcess(args, 0, json.dumps(ov), "")
            data = dict(self.flags[flag])
            data["feature"] = flag
            return subprocess.CompletedProcess(args, 0, json.dumps(data), "")
        if sub == ["flag", "update"]:
            flag = args[3]
            enabled = "--enable" in args
            if "--segment" in args:
                seg = args[args.index("--segment") + 1]
                self.overrides[(flag, seg)] = {"feature": flag, "segment": {"name": seg}, "priority": 0, "enabled": enabled, "value": None}
                self.flags[flag]["segment_overrides"] = len([k for k in self.overrides if k[0] == flag])
            else:
                self.flags[flag]["enabled"] = enabled
            return subprocess.CompletedProcess(args, 0, "ok", "")
        if sub == ["flag", "delete"]:
            flag = args[3]
            seg = args[args.index("--segment") + 1]
            self.overrides.pop((flag, seg), None)
            self.flags[flag]["segment_overrides"] = len([k for k in self.overrides if k[0] == flag])
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", "unknown test command")


def test_allowlist_and_approval_enforcement(tmp_path: Path, monkeypatch):
    cfg = _make_config(tmp_path, monkeypatch)
    runner = FakeFlagsmithRunner()
    ops = FlagsmithOps(cfg, cli_path="/usr/local/bin/flagsmith", runner=runner)

    # Missing approval
    with pytest.raises(FlagsmithOpsError, match="ApprovalToken"):
        ops.apply_operation("rehearsal", "checkout_v2", "environment_default", "set_off", approval=None)

    # Mismatched environment approval
    wrong_tok = issue_token("rehearsal", "demo-target")
    with pytest.raises(FlagsmithOpsError, match="ApprovalToken"):
        ops.apply_operation("rehearsal", "checkout_v2", "environment_default", "set_off", approval=wrong_tok)

    tok = issue_token("rehearsal", "rehearsal")
    # Disallowed environment
    with pytest.raises(FlagsmithOpsError, match="allowlist"):
        ops.apply_operation("production", "checkout_v2", "environment_default", "set_off", approval=issue_token("prod", "production"))

    # Disallowed flag
    with pytest.raises(FlagsmithOpsError, match="allowed.flags"):
        ops.apply_operation("rehearsal", "other_flag", "environment_default", "set_off", approval=tok)

    # Disallowed scope (identity:alice is not in allowed.scopes)
    with pytest.raises(FlagsmithOpsError, match="allowed.scopes"):
        ops.apply_operation("rehearsal", "checkout_v2", "identity:alice", "set_off", approval=tok)

    # Disallowed operation
    with pytest.raises(FlagsmithOpsError, match="allowed.operations"):
        ops.apply_operation("rehearsal", "checkout_v2", "environment_default", "create_flag", approval=tok)

    # Cannot delete_override on environment_default
    with pytest.raises(FlagsmithOpsError, match="environment_default"):
        ops.apply_operation("rehearsal", "checkout_v2", "environment_default", "delete_override", approval=tok)

    assert runner.calls == []


def test_snapshot_hash_changes_and_logs_redacted(tmp_path: Path, monkeypatch):
    cfg = _make_config(tmp_path, monkeypatch)
    runner = FakeFlagsmithRunner()
    ops = FlagsmithOps(cfg, cli_path="/usr/local/bin/flagsmith", runner=runner)

    snap1 = take_snapshot(ops, cfg, "rehearsal")
    snap2 = take_snapshot(ops, cfg, "rehearsal")
    assert snap1.hash == snap2.hash

    tok = issue_token("rehearsal", "rehearsal")
    ops.apply_operation("rehearsal", "cart_compat_reader", "environment_default", "set_on", approval=tok)
    snap3 = take_snapshot(ops, cfg, "rehearsal")
    assert snap3.hash != snap1.hash

    ops.apply_operation("rehearsal", "checkout_v2", "segment:vip_users", "set_on", approval=tok)
    snap4 = take_snapshot(ops, cfg, "rehearsal")
    assert snap4.hash != snap3.hash
    assert snap4.state["flags"]["checkout_v2"]["overrides"]["vip_users"]["enabled"] is True

    # Verify redaction of SDK key in all command logs
    for entry in ops.command_log:
        assert "rehearsal_dummy_sdk_key_12345" not in entry.command
        assert "<REDACTED>" in entry.command
