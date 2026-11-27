"""Allowlisted Flagsmith CLI wrapper. This is the only module that invokes the CLI."""

from __future__ import annotations

from contextlib import contextmanager

from dataclasses import dataclass
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from rewind.approvals import (
    ApprovalError, ApprovalToken, _begin_target_write, _close_target, _consume_token,
    _finish_target_write, _record_target_failure, _verify_target_snapshot,
)
from rewind.config import RewindConfig


class FlagsmithOpsError(RuntimeError):
    """Raised on allowlist violations, missing approvals, drift, or CLI failures."""


@dataclass
class CommandRecord:
    command: str
    exit_code: int
    duration_ms: float
    stdout: str
    stderr: str


@dataclass(frozen=True)
class TargetReplay:
    """Only the next approved write and a trusted reproduction result are accepted."""

    apply_next: Callable[[str, str, str], CommandRecord]
    record_failure: Callable[[Any], None]


RunnerFn = Callable[[list[str], dict[str, str]], subprocess.CompletedProcess[str]]

SECRET_FIELD_TERMS = ("key", "token", "secret", "password", "credential", "auth")
MIN_ENV_SECRET_LEN = 8


def _default_runner(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
        shell=False,
    )


class FlagsmithOps:
    """Executes allowlisted Flagsmith CLI operations with strict redaction and approvals."""

    def __init__(
        self,
        config: RewindConfig,
        cli_path: str | None = None,
        runner: RunnerFn | None = None,
    ) -> None:
        self.config = config
        self.cli_path = cli_path or shutil.which("flagsmith") or "flagsmith"
        self._runner = runner or _default_runner
        self.command_log: list[CommandRecord] = []
        self._secrets: set[str] = set()
        self._collect_env_secrets()

    def _collect_env_secrets(self) -> None:
        for k, v in os.environ.items():
            if (
                v
                and len(v) >= MIN_ENV_SECRET_LEN
                and any(term in k.upper() for term in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
            ):
                self._secrets.add(v)

    def register_secret(self, value: str) -> None:
        if value and len(value) >= MIN_ENV_SECRET_LEN:
            self._secrets.add(value)

    def redact(self, text: str) -> str:
        out = text or ""
        for secret in sorted(self._secrets, key=len, reverse=True):
            out = out.replace(secret, "<REDACTED>")
        out = re.sub(
            r"\b(sk-[A-Za-z0-9_-]{8,}|gsk_[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{8,}|AQ\.[A-Za-z0-9_-]{8,}|ser\.[A-Za-z0-9]{10,})\b",
            "<REDACTED>",
            out,
        )
        return out

    def _resolve_env_key(self, environment: str) -> str:
        if environment not in self.config.allowed_environments():
            raise FlagsmithOpsError(f"Environment {environment!r} is not in allowlist")
        key = self.config.env_key_for(environment)
        self.register_secret(key)
        return key

    def _run_cli(self, cli_args: list[str], check: bool = True) -> tuple[Any, CommandRecord]:
        cmd = [self.cli_path, *cli_args]
        env = os.environ.copy()
        started = time.perf_counter()
        proc = self._runner(cmd, env)
        duration_ms = round((time.perf_counter() - started) * 1000, 3)

        raw_stdout = proc.stdout or ""
        raw_stderr = proc.stderr or ""
        parsed_json: Any = None
        json_err: json.JSONDecodeError | None = None

        wants_json = "--json" in cli_args
        looks_json = raw_stdout.lstrip().startswith(("{", "["))
        if raw_stdout.strip() and (wants_json or looks_json):
            try:
                parsed_json = json.loads(raw_stdout)
                self._learn_json_secrets(parsed_json)
            except json.JSONDecodeError as exc:
                if wants_json and proc.returncode == 0:
                    json_err = exc

        display_cmd = self.redact(" ".join(["flagsmith", *cli_args]))
        stdout_redacted = self.redact(raw_stdout).strip()
        stderr_redacted = self.redact(raw_stderr).strip()
        record = CommandRecord(
            command=display_cmd,
            exit_code=proc.returncode,
            duration_ms=duration_ms,
            stdout=stdout_redacted,
            stderr=stderr_redacted,
        )
        self.command_log.append(record)

        if check and proc.returncode != 0:
            detail = record.stderr or record.stdout or f"exit code {proc.returncode}"
            raise FlagsmithOpsError(f"CLI command failed ({display_cmd}): {detail}")

        if json_err is not None:
            raise FlagsmithOpsError(f"Expected JSON from {display_cmd}: {json_err}")

        if parsed_json is not None and wants_json and proc.returncode == 0:
            return parsed_json, record
        return record.stdout, record

    def _learn_json_secrets(self, payload: Any) -> None:
        if isinstance(payload, dict):
            for k, v in payload.items():
                if isinstance(v, str) and any(t in k.lower() for t in SECRET_FIELD_TERMS):
                    self.register_secret(v)
                else:
                    self._learn_json_secrets(v)
        elif isinstance(payload, list):
            for item in payload:
                self._learn_json_secrets(item)

    def list_environments(self) -> list[dict[str, Any]]:
        data, _ = self._run_cli(["environment", "list", "--json"])
        return data if isinstance(data, list) else data.get("results", data.get("environments", []))

    def list_segments(self) -> list[dict[str, Any]]:
        data, _ = self._run_cli(["segment", "list", "--json"])
        return data if isinstance(data, list) else data.get("results", data.get("segments", []))

    def list_features(self) -> list[dict[str, Any]]:
        data, _ = self._run_cli(["feature", "list", "--json"])
        return data if isinstance(data, list) else data.get("results", data.get("features", []))

    def list_flag_states(self, environment: str, *, feature: str | None = None,
                         identities: bool = False) -> list[dict[str, Any]]:
        """Read the inventory, including overrides outside the write allowlist."""
        args = ["flag", "list", "--environment", self._resolve_env_key(environment), "--json", "--no-input"]
        if feature is not None:
            args.extend(["--feature", feature])
        if identities:
            args.append("--identities")
        data, _ = self._run_cli(args)
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise FlagsmithOpsError("Unexpected Flagsmith flag inventory format")
        return data

    def get_flag(self, environment: str, flag: str, segment: str | None = None) -> dict[str, Any]:
        if flag not in self.config.allowed.flags:
            raise FlagsmithOpsError(f"Flag {flag!r} is not in allowed.flags")
        env_key = self._resolve_env_key(environment)
        args = ["flag", "get", flag, "--environment", env_key, "--json"]
        if segment is not None:
            args.extend(["--segment", segment])
        data, _ = self._run_cli(args)
        return data

    def get_segment_override(self, environment: str, flag: str, segment: str) -> dict[str, Any] | None:
        if flag not in self.config.allowed.flags:
            raise FlagsmithOpsError(f"Flag {flag!r} is not in allowed.flags")
        env_key = self._resolve_env_key(environment)
        args = ["flag", "get", flag, "--segment", segment, "--environment", env_key, "--json"]
        data, record = self._run_cli(args, check=False)
        if record.exit_code != 0:
            lowered = (record.stderr + " " + record.stdout).lower()
            if "not found" in lowered or "no override" in lowered or "404" in lowered:
                return None
            raise FlagsmithOpsError(f"CLI command failed ({record.command}): {record.stderr or record.stdout}")
        return data

    def get_identity_override(self, environment: str, flag: str, identifier: str) -> dict[str, Any] | None:
        if flag not in self.config.allowed.flags:
            raise FlagsmithOpsError(f"Flag {flag!r} is not in allowed.flags")
        env_key = self._resolve_env_key(environment)
        args = ["flag", "get", flag, "--identifier", identifier, "--environment", env_key, "--json"]
        data, record = self._run_cli(args, check=False)
        if record.exit_code != 0:
            lowered = (record.stderr + " " + record.stdout).lower()
            if "not found" in lowered or "no override" in lowered or "404" in lowered:
                return None
            raise FlagsmithOpsError(f"CLI command failed ({record.command}): {record.stderr or record.stdout}")
        return data

    def validate_operation(self, environment: str, flag: str, scope: str, operation: str) -> None:
        if environment not in self.config.allowed_environments():
            raise FlagsmithOpsError(f"Environment {environment!r} is not in allowlist")
        if flag not in self.config.allowed.flags:
            raise FlagsmithOpsError(f"Flag {flag!r} is not in allowed.flags")
        if scope not in self.config.allowed.scopes:
            raise FlagsmithOpsError(f"Scope {scope!r} is not in allowed.scopes")
        if operation not in self.config.allowed.operations:
            raise FlagsmithOpsError(f"Operation {operation!r} is not in allowed.operations")
        if scope == "environment_default" and operation == "delete_override":
            raise FlagsmithOpsError("Cannot use delete_override on environment_default")

    def _build_flag_args(self, flag: str, scope: str, operation: str, env_key: str) -> list[str]:
        if operation == "delete_override":
            args = ["flag", "delete", flag]
        elif operation == "set_on":
            args = ["flag", "update", flag, "--enable"]
        elif operation == "set_off":
            args = ["flag", "update", flag, "--disable"]
        else:
            raise FlagsmithOpsError(f"Unsupported operation {operation!r}")

        if scope.startswith("segment:"):
            args.extend(["--segment", scope.split(":", 1)[1]])
        elif scope.startswith("identity:"):
            args.extend(["--identifier", scope.split(":", 1)[1]])
        elif scope != "environment_default":
            raise FlagsmithOpsError(f"Unsupported scope {scope!r}")

        args.extend(["--environment", env_key, "--yes"])
        return args

    def apply_operations(
        self,
        environment: str,
        operations: Iterable[tuple[str, str, str]],
        approval: ApprovalToken | None,
    ) -> list[CommandRecord]:
        """Apply a batch, respecting a rehearsal budget or an exact Gate 2 plan."""
        sequence = tuple(operations)
        if not sequence:
            raise FlagsmithOpsError("At least one operation is required")
        for op in sequence:
            self.validate_operation(environment, *op)
        target = self.config.flagsmith.target.environment
        if environment == target:
            with self.approved_target_plan(environment, sequence, approval) as replay:
                return [replay.apply_next(*op) for op in sequence]

        rehearsal = self.config.flagsmith.rehearsal.environment
        session = isinstance(approval, ApprovalToken) and approval.phase == "rehearsal"

        def authorize(writes: tuple[tuple[str, str, str], ...]) -> None:
            try:
                _consume_token(approval, environment=environment, rehearsal_env=rehearsal,
                               target_env=target, operations=writes, allow_setup=True)
            except ApprovalError as exc:
                raise FlagsmithOpsError(str(exc)) from exc

        if not session:
            authorize(sequence)
        env_key = self._resolve_env_key(environment)
        records = []
        for op in sequence:
            if session:
                authorize((op,))
            _, record = self._run_cli(self._build_flag_args(*op, env_key))
            records.append(record)
        return records

    @contextmanager
    def approved_target_plan(
        self,
        environment: str,
        operations: Iterable[tuple[str, str, str]],
        approval: ApprovalToken | None,
    ):
        """Consume Gate 2 once, then execute only its ordered, drift-checked writes."""
        from rewind.snapshot import take_snapshot

        sequence = tuple(operations)
        target = self.config.flagsmith.target.environment
        if environment != target or not sequence:
            raise FlagsmithOpsError("A target plan requires the configured target and at least one operation")
        for op in sequence:
            self.validate_operation(environment, *op)
        try:
            token = _consume_token(
                approval, environment=environment, rehearsal_env=self.config.flagsmith.rehearsal.environment,
                target_env=target, operations=sequence,
            )
        except ApprovalError as exc:
            raise FlagsmithOpsError(str(exc)) from exc

        def snapshot_hash() -> str:
            return take_snapshot(self, self.config, environment).hash

        def apply_next(flag: str, scope: str, operation: str) -> CommandRecord:
            try:
                self.validate_operation(environment, flag, scope, operation)
                _begin_target_write(token, (flag, scope, operation), snapshot_hash())
                _, record = self._run_cli(self._build_flag_args(flag, scope, operation, env_key))
                _finish_target_write(token, snapshot_hash())
                return record
            except ApprovalError as exc:
                _close_target(token, require_complete=False)
                raise FlagsmithOpsError(str(exc)) from exc
            except BaseException:
                _close_target(token, require_complete=False)
                raise

        def record_failure(result: Any) -> None:
            try:
                _record_target_failure(token, result)
            except ApprovalError as exc:
                raise FlagsmithOpsError(str(exc)) from exc

        try:
            env_key = self._resolve_env_key(environment)
            # Also guard before the caller can touch the target fixture DB.
            _verify_target_snapshot(token, snapshot_hash())
            yield TargetReplay(apply_next, record_failure)
            _close_target(token, require_complete=True)
        except ApprovalError as exc:
            raise FlagsmithOpsError(str(exc)) from exc
        finally:
            _close_target(token, require_complete=False)

    def apply_operation(
        self,
        environment: str,
        flag: str,
        scope: str,
        operation: str,
        approval: ApprovalToken | None,
    ) -> CommandRecord:
        """Validate and apply a single Flagsmith operation using a registered approval token."""
        records = self.apply_operations(environment, [(flag, scope, operation)], approval)
        return records[0]

    def create_segment(
        self,
        name: str,
        rules_file: Path | str,
        approval: ApprovalToken | None,
        description: str = "",
    ) -> CommandRecord:
        try:
            _consume_token(
                approval,
                environment="project",
                rehearsal_env=self.config.flagsmith.rehearsal.environment,
                target_env=self.config.flagsmith.target.environment,
                operations=(),
                allow_setup=True,
            )
        except ApprovalError as exc:
            raise FlagsmithOpsError(str(exc)) from exc

        args = ["segment", "create", name, "--rules", f"@{rules_file}"]
        if description:
            args.extend(["--description", description])
        _, record = self._run_cli(args)
        return record

    def delete_segment(self, name: str, approval: ApprovalToken | None) -> CommandRecord:
        try:
            _consume_token(
                approval,
                environment="project",
                rehearsal_env=self.config.flagsmith.rehearsal.environment,
                target_env=self.config.flagsmith.target.environment,
                operations=(),
                allow_setup=True,
            )
        except ApprovalError as exc:
            raise FlagsmithOpsError(str(exc)) from exc

        _, record = self._run_cli(["segment", "delete", name, "--yes"])
        return record

    def clear_segment_override_if_present(
        self,
        environment: str,
        flag: str,
        segment: str,
        approval: ApprovalToken | None,
    ) -> bool:
        """Setup-only helper to clear a segment override in rehearsal using a setup token."""
        if environment == self.config.flagsmith.target.environment:
            raise FlagsmithOpsError("Setup cleanup cannot mutate the target environment")
        self.validate_operation(environment, flag, f"segment:{segment}", "delete_override")
        if self.get_segment_override(environment, flag, segment) is None:
            return False
        if not isinstance(approval, ApprovalToken) or approval.phase != "setup":
            raise FlagsmithOpsError("Setup token (mint_setup_token) is required for setup cleanup")
        self.apply_operation(environment, flag, f"segment:{segment}", "delete_override", approval)
        return True

    def clear_identity_override_if_present(
        self,
        environment: str,
        flag: str,
        identifier: str,
        approval: ApprovalToken | None,
    ) -> bool:
        """Setup-only helper to remove stale overrides for configured users in rehearsal."""
        if environment == self.config.flagsmith.target.environment:
            raise FlagsmithOpsError("Setup cleanup cannot mutate the target environment")
        if environment not in self.config.allowed_environments():
            raise FlagsmithOpsError(f"Environment {environment!r} is not in allowlist")
        if flag not in self.config.allowed.flags:
            raise FlagsmithOpsError(f"Flag {flag!r} is not in allowed.flags")
        if "delete_override" not in self.config.allowed.operations:
            raise FlagsmithOpsError("Operation 'delete_override' is not in allowed.operations")
        configured_users = {u.id for u in self.config.users}
        if identifier not in configured_users:
            raise FlagsmithOpsError(f"Identifier {identifier!r} is not a configured user")

        try:
            _consume_token(
                approval,
                environment=environment,
                rehearsal_env=self.config.flagsmith.rehearsal.environment,
                target_env=self.config.flagsmith.target.environment,
                operations=((flag, f"identity:{identifier}", "delete_override"),),
                allow_setup=True,
            )
        except ApprovalError as exc:
            raise FlagsmithOpsError(str(exc)) from exc

        if not isinstance(approval, ApprovalToken) or approval.phase != "setup":
            raise FlagsmithOpsError("Setup token (mint_setup_token) is required for identity cleanup")

        if self.get_identity_override(environment, flag, identifier) is None:
            return False
        env_key = self._resolve_env_key(environment)
        self._run_cli(["flag", "delete", flag, "--identifier", identifier, "--environment", env_key, "--yes"])
        return True
