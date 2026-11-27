"""Run one approval-gated rollback rehearsal and, if approved, a staging replay."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

import requests
from rich.console import Console

from rewind.agent.llm import ChatModel, LLMAdapter, LLMError
from rewind.agent.tools import AttemptTools, Proposal, run_attempt
from rewind.approvals import (
    ApprovalError, ApprovalToken, approval_identity, approval_uses, close_rehearsal, gate1_rehearsal, gate2_target,
)
from rewind.config import RewindConfig
from rewind.flagsmith_ops import CommandRecord, FlagsmithOps, FlagsmithOpsError
from rewind.ledger import autonomy_ledger, ledger_breakdown, ledger_summary
from rewind.planning import Operation, PlanningError, configuration, difference, effective_by_user, project
from rewind.report.data import rehearsal_preview
from rewind.request import RollbackRequest
from rewind.snapshot import EnvironmentSnapshot, take_snapshot
from rewind.verifier import VerificationResult, verify_journey

REFUSAL = "No verified configuration-only recovery found. Target unchanged."
_AUTH_URL = re.compile(r"https?://[^\s\"'<>]*(?:oauth|authorize|login|auth/)[^\s\"'<>]*", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class RunError(RuntimeError):
    """The rehearsal or target replay must stop without further writes."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Instance:
    environment: str
    port: int
    db: Path
    state_file: Path

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@dataclass(frozen=True)
class CheckResult:
    status: str
    evidence: Path

    @property
    def exit_code(self) -> int:
        return 0 if self.status == "verified" else 1


class LocalApp:
    """Trusted commands only. The model never controls a subprocess argument."""

    def __init__(self, config: RewindConfig, root: Path) -> None:
        self.config, self.root = config, root

    def _env(self, instance: Instance) -> dict[str, str]:
        env = dict(os.environ)
        for name in ("FLAGSMITH_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY",
                     self.config.flagsmith.rehearsal.key_env, self.config.flagsmith.target.key_env):
            env.pop(name, None)
        if self.config.agent:
            env.pop(self.config.agent.key_env, None)
            if self.config.agent.fallback:
                env.pop(self.config.agent.fallback.key_env, None)
        env.update(FLAGSMITH_ENV_KEY=self.config.env_key_for(instance.environment), DB_PATH=str(instance.db))
        return env

    @staticmethod
    def _command(template: str, instance: Instance) -> list[str]:
        argv = [word.format(port=instance.port, db=instance.db) for word in shlex.split(template)]
        if not argv:
            raise RunError("App command is empty")
        if argv[0] in {"python", "python3"}:
            argv[0] = sys.executable
        return argv

    def reset(self, instance: Instance) -> None:
        argv = self._command(self.config.app.reset, instance)
        proc = subprocess.run(argv, cwd=self.root, env=self._env(instance), capture_output=True,
                              text=True, timeout=30, shell=False)
        if proc.returncode:
            raise RunError(f"{shlex.join(argv)} exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}")

    @contextmanager
    def start(self, instance: Instance):
        argv = self._command(self.config.app.start, instance)
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", instance.port)) == 0:
                raise RunError(f"{shlex.join(argv)}: port {instance.port} is already in use")
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(argv, cwd=self.root, env=self._env(instance), stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=output, shell=False)
            try:
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline and proc.poll() is None:
                    try:
                        health = requests.get(f"{instance.url}/health", timeout=0.5, allow_redirects=False)
                        if health.status_code == 200:
                            body = health.json()
                            if body.get("db_path") and Path(body["db_path"]).resolve() != instance.db:
                                raise RunError(f"{shlex.join(argv)}: /health reported a different fixture database")
                            break
                    except requests.RequestException:
                        pass
                    time.sleep(0.1)
                else:
                    output.seek(0)
                    detail = output.read().decode("utf-8", errors="replace").strip()
                    raise RunError(f"{shlex.join(argv)}: app did not become ready at {instance.url}/health; {detail}")
                yield
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)

    def verify(self, instance: Instance, phase: str, expected: dict[str, dict[str, bool]]) -> VerificationResult:
        def runner(cmd, _env, timeout):
            return subprocess.run(cmd, cwd=self.root, env=self._env(instance), capture_output=True,
                                  shell=False, text=True, timeout=timeout)

        return verify_journey(
            self.config, instance.url, phase, environment=instance.environment, state_file=instance.state_file,
            expected_flags=expected, extra_args=["--expect-json", json.dumps(expected, sort_keys=True)], runner=runner,
        )


def _evidence(result: VerificationResult) -> dict[str, Any]:
    return {"ok": result.ok, "summary": result.summary, "steps": result.steps,
            "failed_assertions": result.failed_assertions, "user_outcomes": result.user_outcomes}


def _model_context(state: dict, result: VerificationResult) -> tuple[dict, dict]:
    flags = state["flags"]
    users = {
        uid: {"status": outcome["http_status"], "executed_version": outcome["executed_version"],
              "effective_flags": {flag: value for flag, value in outcome["effective_flags"].items() if flag in flags}}
        for uid, outcome in result.user_outcomes.items()
    }
    failures = list(dict.fromkeys(
        (item.get("user"), item.get("assertion"), str(item.get("detail", ""))[:160])
        for item in result.failed_assertions
    ))
    evidence = {"summary": result.summary[:300], "user_outcomes": users, "failed_assertions": [
        {"user": user, "name": name, "detail": detail} for user, name, detail in failures[:20]
    ]}
    flag_state = {"flags": {
        name: {"default": setting["enabled"],
               "segment_overrides": {seg: row["enabled"] for seg, row in setting["overrides"].items()},
               "identity_overrides": {uid: row["enabled"] for uid, row in setting["identity_overrides_detail"].items()}}
        for name, setting in flags.items()
    }, "effective_flags": {uid: user["effective_flags"] for uid, user in users.items()}}
    return flag_state, evidence


def _reason(result: VerificationResult, goal_version: str) -> str:
    for uid, outcome in result.user_outcomes.items():
        version, status = outcome["executed_version"], outcome["http_status"]
        if status != 200:
            detail = next((f["detail"] for f in result.failed_assertions
                           if f.get("user") == uid and f.get("assertion") == "http_status_200"), "")
            return f"{uid} checkout returned HTTP {status} ({version}). {detail}".strip()
        if version != goal_version:
            return f"{uid} still executes {version}; expected {goal_version}."
    return result.summary


class _Run:
    def __init__(self, config, root: Path, ops, model, apps, console, input_fn, approver: str | None,
                 request: RollbackRequest | None) -> None:
        self.config, self.root, self.ops, self.model, self.apps = config, root, ops, model, apps
        self.console, self.input_fn = console, input_fn
        self.request = request
        self.approval_mode, self.approver = approval_identity(input_fn, approver)
        assert config.agent is not None
        self.max_attempts = config.agent.max_attempts
        now = datetime.now(timezone.utc)
        self.folder = root / ".artifacts" / now.strftime("run-%Y%m%dT%H%M%S%fZ")
        self.folder.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.file = self.folder / "run.json"
        self.rehearsal = Instance(config.flagsmith.rehearsal.environment, config.app.rehearsal_port,
                                  self.folder / "rehearsal.db", self.folder / "rehearsal_state.json")
        self.target = Instance(config.flagsmith.target.environment, config.app.target_port,
                               self.folder / "target.db", self.folder / "target_state.json")
        self.rollback: Operation = (config.rollback.flag, "environment_default", f"set_{config.rollback.to}")
        self.data: dict[str, Any] = {
            "status": "running", "started_at": now.isoformat(), "project": config.project,
            "approval_mode": self.approval_mode,
            "provider": config.agent.provider, "model": config.agent.model,
            "environments": {side: instance.environment for side, instance in (("rehearsal", self.rehearsal), ("target", self.target))},
            "app_variant": os.getenv("APP_VARIANT", "default"), "approvals": [], "approval_requests": [],
            "snapshots": [], "transitions": [],
            "attempts": [], "timings": [], "changes": [], "target_failure_reproduced": None,
        }
        if request is not None:
            self.data["request"] = {"text": request.text, "flag": request.flag, "direction": request.direction,
                                    "interpretation": request.understood}
            self.data["request_llm_calls"] = [asdict(call) for call in request.calls]
        self.started = time.perf_counter()
        self.gate = 1

    def clean(self, value: Any) -> Any:
        if isinstance(value, str):
            return _CONTROL.sub("", _AUTH_URL.sub("<REDACTED_AUTH_URL>", self.ops.redact(value)))
        if isinstance(value, dict):
            return {self.clean(key): self.clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(item) for item in value]
        return value

    def say(self, text: str) -> None:
        self.console.print(self.clean(text), markup=False, highlight=False, overflow="fold")

    def show_approval(self, text: str) -> None:
        self.data["approval_requests"].append({"gate": self.gate, "timestamp": _now()})
        self.say(text)

    def save(self) -> dict[str, int]:
        payload = {**self.data, "elapsed_seconds": round(time.perf_counter() - self.started, 3),
                   "llm_calls": [asdict(call) for call in self.model.calls],
                   "flagsmith_command_log": [asdict(record) for record in self.ops.command_log]}
        ledger = autonomy_ledger(payload)
        payload["autonomy_ledger"] = ledger
        pending = self.file.with_suffix(".tmp")
        pending.write_text(json.dumps(self.clean(payload), indent=2) + "\n", encoding="utf-8")
        pending.replace(self.file)
        return ledger

    @contextmanager
    def time(self, phase: str):
        start = time.perf_counter()
        self.say(f"→ {phase}")
        try:
            yield
        finally:
            elapsed = round(time.perf_counter() - start, 3)
            self.data["timings"].append({"phase": phase, "seconds": elapsed})
            self.say(f"  {elapsed:.2f}s — {phase}")

    def snapshot(self, instance: Instance, phase: str) -> EnvironmentSnapshot:
        with self.time(phase):
            snap = take_snapshot(self.ops, self.config, instance.environment)
            self.data["snapshots"].append({"phase": phase, **asdict(snap)})
        return snap

    def require_environment_binding(self, instance: Instance) -> None:
        with self.time(f"Confirm Flagsmith environment binding: {instance.environment}"):
            key = self.config.env_key_for(instance.environment)
            matches = [env for env in self.ops.list_environments() if env.get("name") == instance.environment]
            if len(matches) != 1 or matches[0].get("api_key") != key:
                raise RunError(f"flagsmith environment list: SDK key does not identify {instance.environment!r}; values withheld")

    def approval(self, token: ApprovalToken, instance: Instance) -> dict[str, Any]:
        record = {"gate": self.gate, "environment": instance.environment, "approved_by": token.approved_by,
                  "approval_mode": token.approval_mode,
                  "issued_at": token.issued_at, "summary": token.summary, "operations": token.operations,
                  "snapshot_hash": token.snapshot_hash, "max_operations": token.max_operations}
        self.data["approvals"].append(record)
        self.save()
        return record

    def write(self, instance: Instance, operation: Operation, apply: Callable[..., CommandRecord]) -> None:
        flag, scope, op = operation
        change = {"kind": "flag", "environment": instance.environment, "flag": flag, "scope": scope,
                  "operation": op, "timestamp": _now(), "status": "not confirmed"}
        start_index = len(self.ops.command_log)
        with self.time(f"{instance.environment}: {flag} / {scope} / {op}"):
            try:
                record = apply(*operation)
            except BaseException as exc:
                after = self.ops.command_log[start_index:]
                writes = [rec for rec in after if rec.command.startswith(("flagsmith flag update ", "flagsmith flag delete "))]
                if writes:
                    change.update(status="applied; follow-up failed" if writes[-1].exit_code == 0 else "failed or uncertain",
                                  command=writes[-1].command)
                elif isinstance(exc, subprocess.TimeoutExpired):
                    change.update(status="timed out; inspect actual state")
                else:
                    change.update(status="blocked before recorded write")
                self.data["changes"].append(change)
                raise
            change.update(status="applied", command=record.command, duration_ms=record.duration_ms)
            self.data["changes"].append(change)
        self.save()

    def reset(self, instance: Instance) -> None:
        change = {"kind": "fixture_reset", "environment": instance.environment, "db_path": str(instance.db),
                  "timestamp": _now(), "status": "incomplete or uncertain"}
        self.data["changes"].append(change)
        with self.time(f"Reset disposable fixture for {instance.environment}"):
            self.apps.reset(instance)
            change["status"] = "completed"

    def verify(self, instance: Instance, phase: str, label: str, state: dict[str, Any]) -> VerificationResult:
        expected = effective_by_user(state, self.config)
        try:
            with self.time(label):
                return self._verify(instance, phase, label, expected)
        except BaseException:
            self.data["changes"].append({"kind": "fixture_journey", "environment": instance.environment,
                "db_path": str(instance.db), "phase": phase, "status": "may contain unreported writes; inspect fixture DB"})
            raise

    def _verify(self, instance: Instance, phase: str, label: str,
                expected: dict[str, dict[str, bool]]) -> VerificationResult:
        result = self.apps.verify(instance, phase, expected)
        self.data["transitions"].append({"label": label, "environment": instance.environment, "timestamp": _now(),
                                         "expected_flags": expected, **asdict(result)})
        for step in result.steps:
            if isinstance(step.get("fixture_write"), dict):
                self.data["changes"].append({"kind": "fixture_write", "environment": instance.environment,
                                             "db_path": str(instance.db), "user": step["user"],
                                             "step": step["name"], "details": step["fixture_write"], "timestamp": _now()})
        self.say(result.summary)
        self.save()
        if phase == "prepare" and not result.ok:
            raise RunError(f"{self.config.app.journey} (prepare): {result.summary}\n{result.stderr}".strip())
        if phase == "verify" and (result.exit_code not in {0, 1}
                or set(result.user_outcomes) != {user.id for user in self.config.users}
                or any(item.get("step") != "checkout" or not item.get("http_status") for item in result.user_outcomes.values())):
            raise RunError(f"{self.config.app.journey} (verify): incomplete journey evidence: {result.summary}\n{result.stderr}".strip())
        return result

    def _rehearse(self) -> tuple[EnvironmentSnapshot, VerificationResult, Proposal] | None:
        instance = self.rehearsal
        max_ops = self.max_attempts * 2 + (self.max_attempts - 1) * 2 + 1
        summary = (f"Write only to {instance.environment} using {instance.db}. Reset and replay the synthetic journey.\n"
                   f"Rollback: {self.rollback}; up to {self.max_attempts} proposed writes and baseline restores between attempts.\n"
                   f"Budget: {self.max_attempts} rollbacks + {self.max_attempts} proposals + "
                   f"{(self.max_attempts - 1) * 2} restoration writes + 1 margin.\n"
                   f"Allowed flags: {', '.join(self.config.allowed.flags)}; scopes: {', '.join(self.config.allowed.scopes)}; "
                   f"operations: {', '.join(self.config.allowed.operations)}.")
        if self.request is not None:
            summary = f"{self.request.understood}\n{summary}"
        self.ops.validate_operation(instance.environment, *self.rollback)
        with self.time("Gate 1 rehearsal approval"):
            token = gate1_rehearsal(summary, environment=instance.environment, max_operations=max_ops,
                                     approver=self.approver, input_fn=self.input_fn, output_fn=self.show_approval)
        try:
            self.approval(token, instance)
            self.require_environment_binding(instance)
            baseline = self.snapshot(instance, "Rehearsal baseline snapshot")
            rolled_state = project(baseline.state, (self.rollback,))
            previous: list[dict[str, Any]] = []
            self.reset(instance)

            def write(*op):
                return self.ops.apply_operation(instance.environment, *op, token)

            goal = "legacy" if self.config.rollback.to == "off" else "v2"
            needs_prepare = True
            with self.apps.start(instance):
                for number in range(1, self.max_attempts + 1):
                    if needs_prepare:
                        self.verify(instance, "prepare", f"Rehearsal {number}: prepare", baseline.state)
                        self.write(instance, self.rollback, write)
                        rolled = self.snapshot(instance, f"Rehearsal {number}: after rollback")
                        if configuration(rolled.state) != configuration(rolled_state):
                            raise RunError("Rehearsal state did not match its rollback plan; stop and inspect drift")
                        failure = self.verify(instance, "verify", f"Rehearsal {number}: verify before recovery", rolled_state)
                        if failure.ok:
                            self.say("The rehearsal rollback passed without a repair; no target changes attempted.")
                            return None

                    flag_state, evidence = _model_context(rolled_state, failure)
                    tools = AttemptTools(self.config, self.root, flag_state, evidence,
                        lambda *op: self.ops.validate_operation(instance.environment, *op), self.clean)
                    with self.time(f"Agent diagnosis {number}"):
                        decision = run_attempt(self.model, tools, previous)
                    proposal = decision.proposal
                    attempt = {"number": number, "status": decision.status, "reason": decision.reason,
                               "proposal": asdict(proposal) if proposal else None, "messages": decision.messages}
                    self.data["attempts"].append(attempt)
                    self.say(f"Attempt {number}: {decision.reason}")
                    self.save()
                    if decision.status == "no_fix":
                        return None
                    needs_prepare = False
                    if proposal:
                        op = proposal.operation_tuple
                        try:
                            candidate = project(rolled_state, (op,))
                            effective_by_user(candidate, self.config)
                            restore = difference(candidate, baseline.state)
                            for inverse in restore:
                                self.ops.validate_operation(instance.environment, *inverse)
                        except (FlagsmithOpsError, PlanningError, ValueError) as exc:
                            attempt.update(status="invalid", reason=str(exc))
                        else:
                            self.write(instance, op, write)
                            observed = self.snapshot(instance, f"Rehearsal {number}: candidate configuration")
                            if configuration(observed.state) != configuration(candidate):
                                raise RunError("Rehearsal config changed unexpectedly; stop and inspect drift")
                            result = self.verify(instance, "verify", f"Rehearsal {number}: verify candidate", candidate)
                            if result.ok:
                                attempt.update(status="verified", reason=result.summary)
                                return baseline, failure, proposal
                            attempt.update(status="failed", reason=_reason(result, goal), verification=_evidence(result))
                            self.say(attempt["reason"])
                            if number < self.max_attempts:
                                for inverse in restore:
                                    self.write(instance, inverse, write)
                                restored = self.snapshot(instance, f"Rehearsal {number}: restored baseline")
                                if restored.hash != baseline.hash:
                                    raise RunError("Rehearsal baseline did not restore exactly; stop and inspect drift")
                                self.reset(instance)
                                needs_prepare = True
                    previous.append({key: attempt[key] for key in ("number", "status", "reason", "proposal")})
            return None
        finally:
            close_rehearsal(token)
            for record in self.data["approvals"]:
                if record["gate"] == 1:
                    record["uses"] = [asdict(use) for use in approval_uses(token)]
                    record["closed_at"] = _now()

    def _replay(self, baseline: EnvironmentSnapshot, failure: VerificationResult, proposal: Proposal) -> None:
        self.gate = 2
        instance = self.target
        self.require_environment_binding(instance)
        start = self.snapshot(instance, "Target starting snapshot")
        setup = difference(start.state, baseline.state)
        operations = (*setup, self.rollback, proposal.operation_tuple)
        for op in operations:
            self.ops.validate_operation(instance.environment, *op)
        target_baseline = project(start.state, setup)
        rolled = project(target_baseline, (self.rollback,))
        fixed = project(rolled, (proposal.operation_tuple,))
        proof = {
            "environment": instance.environment, "starting_state": self.clean(start.state), "operations": operations,
            "stages": ["baseline setup"] * len(setup) + ["rollback", "recovery"], "db_path": str(instance.db),
            "fixture_writes": self.config.app.fixture_writes,
            "recovery_index": len(operations) - 1, "rehearsal_failure": failure,
            "reproduction_flags": self.config.allowed.flags,
            "preview": self.clean(rehearsal_preview(self.data, self.config.allowed.flags)),
        }
        with self.time("Gate 2 exact target approval"):
            token = gate2_target(proof, start.hash, approver=self.approver, input_fn=self.input_fn,
                                 output_fn=self.show_approval)
        audit = self.approval(token, instance)
        try:
            with self.ops.approved_target_plan(instance.environment, operations, token) as replay:
                for op in setup:
                    self.write(instance, op, replay.apply_next)
                self.reset(instance)
                with self.apps.start(instance):
                    self.verify(instance, "prepare", "Target: prepare", target_baseline)
                    self.write(instance, self.rollback, replay.apply_next)
                    try:
                        before = self.verify(instance, "verify", "Target: verify before recovery", rolled)
                        replay.record_failure(before)
                    except RuntimeError as exc:
                        self.data["target_failure_reproduced"] = False
                        if str(exc) == "target did not reproduce the failure":
                            raise
                        raise RunError(f"target did not reproduce the failure: {exc}") from exc
                    self.data["target_failure_reproduced"] = True
                    self.save()
                    self.write(instance, proposal.operation_tuple, replay.apply_next)
                    after = self.verify(instance, "verify", "Target: verify after recovery", fixed)
                    if not after.ok:
                        raise RunError(f"Target verification failed: {after.summary}")
        finally:
            audit["uses"] = [asdict(use) for use in approval_uses(token)]
            audit["closed_at"] = _now()

    def run(self) -> CheckResult:
        try:
            rehearsal_key = self.config.env_key_for(self.rehearsal.environment)
            if rehearsal_key == os.getenv(self.config.flagsmith.target.key_env, "").strip():
                raise RunError("Rehearsal and target use the same SDK key; isolation is not established")
            verified = self._rehearse()
            if verified is None:
                self.data["status"] = "refused"
                self.say(REFUSAL)
            else:
                self._replay(*verified)
                self.data["status"] = "verified"
                self.say("The tested journey is verified on the allowlisted staging target.")
        except (ApprovalError, EOFError) as exc:
            self.data["status"] = "approval_denied"
            self.data["error"] = str(exc) or "Interactive confirmation is required"
            self.data["approvals"].append({"gate": self.gate, "status": "not approved", "timestamp": _now(),
                                           "approval_mode": self.approval_mode})
            self.say(f"Gate {self.gate} not approved: {self.data['error']}. Target unchanged by Rewind.")
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
            self.data["status"] = "stopped"
            self.data["error"] = str(exc) or type(exc).__name__
            if isinstance(exc, LLMError):
                self.data["error_kind"] = "provider"
            self.say(f"Stopped: {self.data['error']}")
            changes = [change for change in self.data["changes"] if change["environment"] == self.target.environment
                       and change.get("status") != "blocked before recorded write"]
            if changes:
                self.say("Target changes and uncertain writes:")
                for change in changes:
                    self.say(f"  - {json.dumps(self.clean(change), sort_keys=True)}")
                self.say("Review the evidence and approve any recovery action separately. No automatic revert was attempted.")
        finally:
            self.data["finished_at"] = _now()
            ledger = self.save()
            self.say(ledger_summary(ledger))
            self.say(ledger_breakdown(ledger))
            self.say(f"Evidence: {self.file}")
        return CheckResult(self.data["status"], self.file)


def run_check(
    config: RewindConfig, root: Path, *, ops: FlagsmithOps | None = None, model: ChatModel | None = None,
    apps: LocalApp | None = None, console: Console | None = None, input_fn: Callable[[str], str] = input,
    approver: str | None = None, request: RollbackRequest | None = None,
) -> CheckResult:
    if config.agent is None or not 1 <= config.agent.max_attempts <= 3:
        raise RunError("Configure an agent with one to three recovery attempts")
    if request is not None:
        config = request.apply(config)
    source_root = root.resolve()
    return _Run(config, source_root, ops or FlagsmithOps(config), model or LLMAdapter(config.agent),
                 apps or LocalApp(config, source_root), console or Console(), input_fn, approver, request).run()
