"""Unit tests for rewind.flagsmith_ops, rewind.approvals, and rewind.snapshot."""

import copy
import json
from pathlib import Path
import subprocess

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


def test_allowlist_and_approval_enforcement(tmp_path: Path, monkeypatch):
    cfg = _make_config(tmp_path, monkeypatch)
    runner = FakeFlagsmithRunner()
    ops = FlagsmithOps(cfg, cli_path="/usr/local/bin/flagsmith", runner=runner)

    with pytest.raises(FlagsmithOpsError, match="ApprovalToken"):
        ops.apply_operation("rehearsal", "checkout_v2", "environment_default", "set_off", approval=None)

    wrong_tok = issue_token("rehearsal", "demo-target")
    with pytest.raises(FlagsmithOpsError, match="ApprovalToken"):
        ops.apply_operation("rehearsal", "checkout_v2", "environment_default", "set_off", approval=wrong_tok)

    tok = issue_token("rehearsal", "rehearsal")
    with pytest.raises(FlagsmithOpsError, match="allowlist"):
        ops.apply_operation("production", "checkout_v2", "environment_default", "set_off", approval=issue_token("prod", "production"))

    with pytest.raises(FlagsmithOpsError, match="allowed.flags"):
        ops.apply_operation("rehearsal", "other_flag", "environment_default", "set_off", approval=tok)

    with pytest.raises(FlagsmithOpsError, match="allowed.scopes"):
        ops.apply_operation("rehearsal", "checkout_v2", "identity:alice", "set_off", approval=tok)

    with pytest.raises(FlagsmithOpsError, match="allowed.operations"):
        ops.apply_operation("rehearsal", "checkout_v2", "environment_default", "create_flag", approval=tok)

    with pytest.raises(FlagsmithOpsError, match="environment_default"):
        ops.apply_operation("rehearsal", "checkout_v2", "environment_default", "delete_override", approval=tok)

    assert runner.calls == []


def test_first_command_log_redacts_newly_returned_credentials(tmp_path: Path, monkeypatch):
    cfg = _make_config(tmp_path, monkeypatch)
    synthetic_secret = "synthetic_returned_cred_987654"
    runner = FakeFlagsmithRunner(first_call_secret=synthetic_secret)
    ops = FlagsmithOps(cfg, cli_path="/usr/local/bin/flagsmith", runner=runner)

    ops.list_segments()
    assert len(ops.command_log) == 1
    assert synthetic_secret not in ops.command_log[0].stdout
    assert "<REDACTED>" in ops.command_log[0].stdout


def test_clear_identity_override_requires_setup_cleanup_authority(tmp_path: Path, monkeypatch):
    cfg = _make_config(tmp_path, monkeypatch)
    runner = FakeFlagsmithRunner()
    runner.id_overrides[("checkout_v2", "alice")] = {"enabled": True, "value": None}
    runner.flags["checkout_v2"]["identity_overrides"] = 1
    ops = FlagsmithOps(cfg, cli_path="/usr/local/bin/flagsmith", runner=runner)

    # Runtime phase tokens must not be permitted to run setup/cleanup identity deletion
    with pytest.raises(FlagsmithOpsError, match="Setup/cleanup authority"):
        ops.clear_identity_override_if_present("rehearsal", "checkout_v2", "alice", issue_token("rehearsal", "rehearsal"))
    with pytest.raises(FlagsmithOpsError, match="Setup/cleanup authority"):
        ops.clear_identity_override_if_present("rehearsal", "checkout_v2", "alice", issue_token("target", "rehearsal"))

    # Unknown user identifier must be rejected even with cleanup authority
    cleanup_tok = issue_token("cleanup", "rehearsal")
    with pytest.raises(FlagsmithOpsError, match="not a configured user"):
        ops.clear_identity_override_if_present("rehearsal", "checkout_v2", "eve", cleanup_tok)

    # Runtime apply_operation still enforces allowed.scopes even with cleanup token
    with pytest.raises(FlagsmithOpsError, match="allowed.scopes"):
        ops.apply_operation("rehearsal", "checkout_v2", "identity:alice", "delete_override", cleanup_tok)

    # Explicit cleanup authority deletes configured user's stale override
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

    # Changing segment membership rules must change snapshot hash
    runner.segments[0]["rules"]["conditions"][0]["value"] = "false"
    snap_rules = take_snapshot(ops, cfg, "rehearsal")
    assert snap_rules.hash != snap1.hash

    # Changing identity override value while keeping count=1 must change snapshot hash
    runner.id_overrides[("checkout_v2", "alice")] = {"enabled": True, "value": None}
    runner.flags["checkout_v2"]["identity_overrides"] = 1
    snap_id_true = take_snapshot(ops, cfg, "rehearsal")

    runner.id_overrides[("checkout_v2", "alice")] = {"enabled": False, "value": None}
    snap_id_false = take_snapshot(ops, cfg, "rehearsal")
    assert snap_id_false.hash != snap_id_true.hash

    # Unconfigured segment or identity override must be explicitly rejected
    runner.flags["checkout_v2"]["identity_overrides"] = 2  # extra unconfigured user
    with pytest.raises(FlagsmithOpsError, match="Unsupported or unconfigured identity override"):
        take_snapshot(ops, cfg, "rehearsal")

    runner.flags["checkout_v2"]["identity_overrides"] = 1
    runner.flags["checkout_v2"]["segment_overrides"] = 1  # unconfigured segment
    with pytest.raises(FlagsmithOpsError, match="Unsupported or unconfigured segment override"):
        take_snapshot(ops, cfg, "rehearsal")
    runner.flags["checkout_v2"]["segment_overrides"] = 0

    tok = issue_token("rehearsal", "rehearsal")
    ops.apply_operation("rehearsal", "cart_compat_reader", "environment_default", "set_on", approval=tok)
    snap3 = take_snapshot(ops, cfg, "rehearsal")
    assert snap3.hash != snap_id_false.hash

    ops.apply_operation("rehearsal", "checkout_v2", "segment:vip_users", "set_on", approval=tok)
    snap4 = take_snapshot(ops, cfg, "rehearsal")
    assert snap4.hash != snap3.hash
    assert snap4.state["flags"]["checkout_v2"]["overrides"]["vip_users"]["enabled"] is True

    for entry in ops.command_log:
        assert "rehearsal_dummy_sdk_key_12345" not in entry.command
        if "--environment" in entry.command:
            assert "<REDACTED>" in entry.command
