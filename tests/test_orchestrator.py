"""Scripted model, local fixture outcomes, and in-memory Flagsmith: no network."""

from __future__ import annotations

from contextlib import contextmanager
import io
import json
from pathlib import Path
import subprocess

import pytest
from rich.console import Console

from rewind.agent.llm import LLMCall
from rewind.config import load_config
from rewind.flagsmith_ops import FlagsmithOps
from rewind.orchestrator import REFUSAL, run_check
from rewind.verifier import VerificationResult

ROOT = Path(__file__).resolve().parents[1]
FIX = ("cart_compat_reader", "environment_default", "set_on")
WRONG = ("checkout_v2", "segment:vip_users", "set_on")
ROLLBACK = ("checkout_v2", "environment_default", "set_off")
REHEARSAL = "rehearsal"
TARGET = "demo-target"


def proposal(op=FIX):
    flag, scope, operation = op
    return {"role": "assistant", "tool_calls": [{"type": "function", "id": "fixture-proposal",
        "function": {"name": "propose_operation", "arguments": json.dumps({
            "diagnosis": "The legacy reader cannot read the saved v2 cart without a compatible path.",
            "flag": flag, "scope": scope, "operation": operation,
        })}}]}


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []
        self.calls: list[LLMCall] = []

    def complete(self, messages, tools):
        self.requests.append(json.loads(messages[1]["content"]))
        self.calls.append(LLMCall("fixture-provider", "fixture-model", 1.0, 5, 3, "ok", 200))
        return next(self.replies)


class MemoryFlagsmith:
    def __init__(self, keys):
        self.keys = keys
        self.defaults = {
            REHEARSAL: {"checkout_v2": True, "cart_compat_reader": False},
            TARGET: {"checkout_v2": False, "cart_compat_reader": True},
        }
        self.overrides = {env: {} for env in keys}
        self.writes: list[tuple[str, str, str, str]] = []
        self.environment_reads: list[str] = []

    def __call__(self, argv, _env):
        def result(payload=None, status=0, stdout=None):
            return subprocess.CompletedProcess(argv, status, json.dumps(payload) if stdout is None else stdout, "")

        command = argv[1:3]
        if command == ["environment", "list"]:
            return result([{"name": name, "api_key": key} for name, key in self.keys.items()])
        if command == ["segment", "list"]:
            return result([{"id": 17, "name": "vip_users", "rules": {"type": "ALL", "conditions": [
                {"property": "is_vip", "operator": "EQUAL", "value": "true"},
            ]}}])
        key = argv[argv.index("--environment") + 1]
        name = next(name for name, value in self.keys.items() if value == key)
        flag = argv[3]
        segment = argv[argv.index("--segment") + 1] if "--segment" in argv else None
        overrides = self.overrides[name]
        if command == ["flag", "get"]:
            self.environment_reads.append(name)
            if segment is not None:
                if (flag, segment) not in overrides:
                    return subprocess.CompletedProcess(argv, 1, "", "No override found")
                return result({"enabled": overrides[flag, segment], "priority": 0, "value": None})
            return result({"enabled": self.defaults[name][flag], "value": None, "identity_overrides": 0,
                           "segment_overrides": sum(key[0] == flag for key in overrides)})
        if command == ["flag", "delete"]:
            operation = "delete_override"
            del overrides[flag, segment]
        else:
            assert command == ["flag", "update"]
            value = "--enable" in argv
            operation = "set_on" if value else "set_off"
            if segment:
                overrides[flag, segment] = value
            else:
                self.defaults[name][flag] = value
        self.writes.append((name, flag, f"segment:{segment}" if segment else "environment_default", operation))
        return result(stdout=f"wrote {name}")

    def effective_flags(self, environment, vip):
        flags = dict(self.defaults[environment])
        if vip:
            for (flag, segment), enabled in self.overrides[environment].items():
                assert segment == "vip_users"
                flags[flag] = enabled
        return flags


class DemoFixtures:
    def __init__(self, config, flags):
        self.config, self.flags = config, flags
        self.events = []
        self.saved = {}
        self.variant = "normal"
        self.expected_flags = []

    @contextmanager
    def start(self, instance):
        self.events.append((instance.environment, "start"))
        try:
            yield
        finally:
            self.events.append((instance.environment, "stop"))

    def reset(self, instance):
        self.saved[instance.environment] = set()
        self.events.append((instance.environment, "reset"))

    def verify(self, instance, phase, expected):
        env = instance.environment
        self.events.append((env, phase))
        self.expected_flags.append((env, phase, expected))
        outcomes, steps, errors = {}, [], []
        for user in self.config.users:
            effective = self.flags.effective_flags(env, user.traits["is_vip"])
            assert effective == expected[user.id], "Prediction must reflect the actual per-user configuration"
            version = "v2" if effective["checkout_v2"] else "legacy"
            step = "checkout" if phase == "verify" else "cart_add"
            if phase == "prepare" and user.role == "affected":
                assert version == "v2"
                self.saved[env].add(user.id)
            affected = user.id in self.saved[env]
            compatible = effective["cart_compat_reader"] and self.variant != "no_compat"
            if env == TARGET and self.variant == "target_passes_before_fix":
                compatible = True
            if env == TARGET and self.variant == "target_fails_after_fix":
                compatible = False
            bad_read = phase == "verify" and version == "legacy" and affected and not compatible
            bad_version = phase == "verify" and version != "legacy"
            status = 500 if bad_read else 200
            outcome = {"step": step, "http_status": status, "executed_version": version, "effective_flags": effective}
            outcomes[user.id] = outcome
            evidence = {**outcome, "name": step, "user": user.id, "assertions": []}
            if phase == "verify" and status == 200:
                evidence["fixture_write"] = {"kind": "order_insert", "cart_id": f"cart-{user.id}"}
            if bad_read or bad_version:
                assertion = "http_status_200" if bad_read else "executed_version_matches_goal"
                detail = "status=500, error=Unrecognized cart schema" if bad_read else "Expected legacy"
                errors.append({"user": user.id, "step": step, "assertion": assertion, "detail": detail})
            steps.append(evidence)
        return VerificationResult(not errors, phase, int(bool(errors)), "fixture failed" if errors else "fixture verified",
                                  steps=steps, user_outcomes=outcomes, failed_assertions=errors)


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    config = load_config(ROOT / "examples/demo-shop/rewind.config.yaml")
    keys = {REHEARSAL: "fixture-rehearsal-key", TARGET: "fixture-target-key"}
    monkeypatch.setenv(config.flagsmith.rehearsal.key_env, keys[REHEARSAL])
    monkeypatch.setenv(config.flagsmith.target.key_env, keys[TARGET])
    flags = MemoryFlagsmith(keys)
    ops = FlagsmithOps(config, runner=flags)
    apps = DemoFixtures(config, flags)

    def run(replies, approvals=("yes", "yes"), variant="normal"):
        model = ScriptedModel(replies)
        answers = iter(approvals)
        prompts = []
        output = io.StringIO()
        apps.variant = variant

        def confirm(prompt):
            prompts.append(prompt)
            return next(answers)

        result = run_check(config, tmp_path, ops=ops, apps=apps, model=model, input_fn=confirm,
                           console=Console(file=output, width=140, force_terminal=False))
        raw = result.evidence.read_text(encoding="utf-8")
        assert all(key not in raw for key in keys.values()) and "token_id" not in raw
        return result, json.loads(raw), output.getvalue(), prompts, model

    return run, flags, ops, apps


def _target_writes(flags):
    return [row[1:] for row in flags.writes if row[0] == TARGET]


def test_correct_proposal_replays_with_one_yes_at_each_gate(workflow):
    run, flags, _, apps = workflow
    result, audit, output, prompts, _ = run([proposal()])
    assert result.status == "verified" and result.exit_code == 0
    assert len(prompts) == 2
    assert audit["target_failure_reproduced"] is True
    assert [attempt["status"] for attempt in audit["attempts"]] == ["verified"]
    assert [approval["approved_by"] for approval in audit["approvals"]] == ["human (yes)", "human (yes)"]
    assert _target_writes(flags) == [
        ("cart_compat_reader", "environment_default", "set_off"),
        ("checkout_v2", "environment_default", "set_on"), ROLLBACK, FIX,
    ]
    assert [phase for env, phase in apps.events if env == TARGET] == ["reset", "start", "prepare", "verify", "verify", "stop"]
    assert "recovery runs only if the failure reproduces" in output
    assert "target.db" in output and "Fixture database writes:" in output
    assert audit["llm_calls"][0]["prompt_tokens"] == 5


def test_identity_trap_is_a_failed_attempt_without_a_write(workflow):
    run, flags, _, _ = workflow
    result, audit, _, _, model = run([proposal(("cart_compat_reader", "identity:alice", "set_on")), proposal()])
    assert result.status == "verified"
    assert [item["status"] for item in audit["attempts"]] == ["invalid", "verified"]
    assert "allowed.scopes" in model.requests[1]["previous_attempts"][0]["reason"]
    assert all(not scope.startswith("identity:") for _, _, scope, _ in flags.writes)


def test_wrong_scope_fails_then_baseline_restores_for_second_attempt(workflow):
    run, flags, _, apps = workflow
    result, audit, _, prompts, model = run([proposal(WRONG), proposal()])
    assert result.status == "verified" and len(prompts) == 2
    assert [attempt["status"] for attempt in audit["attempts"]] == ["failed", "verified"]
    assert "alice still executes v2" in model.requests[1]["previous_attempts"][0]["reason"]
    assert (REHEARSAL, "checkout_v2", "segment:vip_users", "delete_override") in flags.writes
    assert apps.events.count((REHEARSAL, "reset")) == 2


def test_segment_scoped_compatibility_can_pass_with_mixed_flag_values(workflow):
    run, flags, _, apps = workflow
    scoped = ("cart_compat_reader", "segment:vip_users", "set_on")
    result, _, _, _, _ = run([proposal(scoped)])
    assert result.status == "verified" and _target_writes(flags)[-1] == scoped
    _, phase, expected = [row for row in apps.expected_flags if row[0] == TARGET][-1]
    assert phase == "verify"
    assert {user: values["cart_compat_reader"] for user, values in expected.items()} == {"alice": True, "carol": True, "bob": False}


@pytest.mark.parametrize("replies", [
    [{"content": '{"no_fix":"No behind-flag path can read these carts."}'}],
    [proposal(WRONG), proposal(("cart_compat_reader", "environment_default", "set_off")),
     proposal(("checkout_v2", "environment_default", "set_on"))],
], ids=["no_fix", "three_failed_proposals"])
def test_refusal_never_reads_or_mutates_target(workflow, replies):
    run, flags, ops, apps = workflow
    result, audit, output, prompts, _ = run(replies, approvals=("yes",), variant="no_compat")
    assert result.status == "refused" and REFUSAL in output and len(prompts) == 1
    assert len(audit["attempts"]) == len(replies)
    assert _target_writes(flags) == [] and TARGET not in flags.environment_reads
    assert all(record.stdout != "wrote demo-target" for record in ops.command_log)
    assert all(env != TARGET for env, _ in apps.events)


def test_gate1_denied_does_not_mutate_anything(workflow):
    run, flags, ops, apps = workflow
    result, audit, _, prompts, model = run([], approvals=("no",))
    assert result.status == "approval_denied" and len(prompts) == 1
    assert flags.writes == ops.command_log == apps.events == model.calls == []
    assert audit["changes"] == []


def test_gate2_denied_leaves_target_and_its_fixture_untouched(workflow):
    run, flags, _, apps = workflow
    result, audit, _, prompts, _ = run([proposal()], approvals=("yes", "no"))
    assert result.status == "approval_denied" and len(prompts) == 2
    assert _target_writes(flags) == [] and all(env != TARGET for env, _ in apps.events)
    assert [approval["gate"] for approval in audit["approvals"]] == [1, 2]


def test_no_reproduction_closes_target_plan_without_recovery(workflow):
    run, flags, _, _ = workflow
    result, audit, output, _, _ = run([proposal()], variant="target_passes_before_fix")
    assert result.status == "stopped" and audit["target_failure_reproduced"] is False
    assert "target did not reproduce the failure" in output
    assert _target_writes(flags)[-1] == ROLLBACK and FIX not in _target_writes(flags)
    assert audit["approvals"][-1]["closed_at"]


def test_target_verify_after_failure_records_writes_and_does_not_revert(workflow):
    run, flags, _, _ = workflow
    result, audit, output, prompts, _ = run([proposal()], variant="target_fails_after_fix")
    assert result.status == "stopped" and audit["target_failure_reproduced"] is True
    assert "Target verification failed" in output and "approve any recovery action separately" in output
    assert len(prompts) == 2 and _target_writes(flags)[-1] == FIX
    assert flags.defaults[TARGET]["cart_compat_reader"] is True
    target_changes = [row for row in audit["changes"] if row["environment"] == TARGET]
    assert len([row for row in target_changes if row["kind"] == "flag"]) == len(_target_writes(flags))
    assert any(row["kind"] == "fixture_reset" for row in target_changes)
    assert any(row["kind"] == "fixture_write" for row in target_changes)
