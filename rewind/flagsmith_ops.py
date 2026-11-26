"""Allowlisted Flagsmith CLI wrapper. This is the only module that invokes the CLI."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import shutil
import subprocess
import time
from typing import Any, Callable

from rewind.approvals import ApprovalToken
from rewind.config import RewindConfig


class FlagsmithOpsError(RuntimeError):
    """Raised on allowlist violations, missing approvals, or CLI execution failures."""


@dataclass
class CommandRecord:
    command: str
    exit_code: int
    duration_ms: float
    stdout: str
    stderr: str


RunnerFn = Callable[[list[str], dict[str, str]], subprocess.CompletedProcess[str]]


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
    """Executes allowlisted Flagsmith CLI operations with strict redaction."""

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
            if v and any(term in k.upper() for term in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
                self._secrets.add(v)

    def register_secret(self, value: str) -> None:
        if value and len(value) >= 6:
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

        display_cmd = self.redact(" ".join(["flagsmith", *cli_args]))
        stdout_redacted = self.redact(proc.stdout or "")
        stderr_redacted = self.redact(proc.stderr or "")
        record = CommandRecord(
            command=display_cmd,
            exit_code=proc.returncode,
            duration_ms=duration_ms,
            stdout=stdout_redacted.strip(),
            stderr=stderr_redacted.strip(),
        )
        self.command_log.append(record)

        if check and proc.returncode != 0:
            detail = record.stderr or record.stdout or f"exit code {proc.returncode}"
            raise FlagsmithOpsError(f"CLI command failed ({display_cmd}): {detail}")

        parsed: Any = record.stdout
        if "--json" in cli_args and proc.returncode == 0 and record.stdout:
            try:
                parsed = json.loads(proc.stdout)
                self._learn_json_secrets(parsed)
            except json.JSONDecodeError as exc:
                raise FlagsmithOpsError(f"Expected JSON from {display_cmd}: {exc}") from exc
        return parsed, record

    def _learn_json_secrets(self, payload: Any) -> None:
        if isinstance(payload, dict):
            for k, v in payload.items():
                if isinstance(v, str) and any(t in k.lower() for t in ("key", "token", "secret")):
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

    def apply_operation(
        self,
        environment: str,
        flag: str,
        scope: str,
        operation: str,
        approval: ApprovalToken | None,
    ) -> CommandRecord:
        if not isinstance(approval, ApprovalToken) or approval.environment != environment:
            raise FlagsmithOpsError(f"Valid ApprovalToken for {environment!r} is required to mutate Flagsmith")
        self.validate_operation(environment, flag, scope, operation)
        env_key = self._resolve_env_key(environment)

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
        _, record = self._run_cli(args)
        return record
