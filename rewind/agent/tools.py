"""Read-only model tools and one validated, non-executing proposal per attempt."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Callable, Literal

from rewind.agent.llm import ChatModel
from rewind.agent.prompt import SYSTEM_PROMPT
from rewind.config import RewindConfig
from rewind.flagsmith_ops import FlagsmithOpsError


MAX_READ_CALLS = 6
MAX_SOURCE_BYTES = 128 * 1024


class ToolError(ValueError):
    """An invalid read request or terminal model action."""


@dataclass(frozen=True)
class Proposal:
    diagnosis: str
    flag: str
    scope: str
    operation: str

    @property
    def operation_tuple(self) -> tuple[str, str, str]:
        return self.flag, self.scope, self.operation


@dataclass(frozen=True)
class AttemptResult:
    status: Literal["proposed", "invalid", "no_fix"]
    reason: str
    proposal: Proposal | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)


def _function(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False},
    }}


def _text_enum(values: list[str]) -> dict[str, Any]:
    return {"type": "string", "enum": values}


def _source_forbidden(path: Path) -> bool:
    protected = {".git", ".venv", ".artifacts", "artifacts", "tests", "journey.py", "verifier.py",
                 "approvals.py", "flagsmith_ops.py", "config.py", "flagsmith.json"}
    return any(
        part in protected or part.startswith((".env", "test_", "case_h")) or part.endswith("_test.py")
        for part in (piece.lower() for piece in path.parts)
    ) or path.suffix.lower() in {".yaml", ".yml", ".toml"}


class AttemptTools:
    def __init__(
        self, config: RewindConfig, source_root: Path, flag_state: dict[str, Any], evidence: dict[str, Any],
        validate: Callable[[str, str, str], None], redact: Callable[[str], str],
    ) -> None:
        self._root = source_root.resolve()
        self._sources = {f"source:{index}": value for index, value in enumerate(config.source_paths, 1)}
        self._validate = validate
        self._redact = redact
        self._flag_state = self.scrub(flag_state)
        self._evidence = self.scrub(evidence)
        self._reads = 0
        self._proposed = False
        self.schemas = [
            _function("read_source", "Read one allowlisted application source path by its listed alias.", {"path": _text_enum(list(self._sources))}),
            _function("read_flag_state", "Read the flag defaults, relevant overrides and observed effective states.", {}),
            _function("read_evidence", "Read the latest fixed journey and its failed assertions.", {}),
            _function("propose_operation", "Propose one configuration operation. Does not apply anything.", {
                "diagnosis": {"type": "string", "description": "Name the failure mechanism in one or two plain sentences."},
                "flag": _text_enum(list(config.allowed.flags)), "scope": _text_enum(list(config.allowed.scopes)),
                "operation": _text_enum(["set_on", "set_off", "delete_override"]),
            }),
        ]

    def scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, dict):
            return {self._redact(key): self.scrub(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.scrub(item) for item in value]
        return value

    @property
    def reads_remaining(self) -> int:
        return MAX_READ_CALLS - self._reads

    def read(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._reads >= MAX_READ_CALLS:
            raise ToolError("Read budget exhausted (maximum six calls)")
        self._reads += 1
        if name == "read_source" and set(arguments) == {"path"} and isinstance(arguments["path"], str):
            return self.read_source(arguments["path"])
        if name == "read_flag_state" and not arguments:
            return self._flag_state
        if name == "read_evidence" and not arguments:
            return self._evidence
        raise ToolError("Unknown read tool or invalid arguments")

    def read_source(self, path: str) -> str:
        raw = self._sources.get(path, path)
        if raw not in self._sources.values():
            raise ToolError("The requested path is not in the source allowlist")
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts or _source_forbidden(relative):
            raise ToolError("The requested path is protected")
        candidate = self._root / relative
        try:
            if candidate.resolve() != candidate or not candidate.is_file():
                raise ToolError("Allowlisted source must be a regular file with no symlinks")
            with candidate.open("rb") as source:
                data = source.read(MAX_SOURCE_BYTES + 1)
            if len(data) > MAX_SOURCE_BYTES:
                raise ToolError("Allowlisted source is too large")
            return self._redact(data.decode("utf-8"))
        except (OSError, UnicodeError, RuntimeError):
            raise ToolError("Could not read the allowlisted UTF-8 source") from None

    def propose(self, arguments: dict[str, Any]) -> Proposal:
        if self._proposed:
            raise ToolError("Only one proposal is allowed per attempt")
        self._proposed = True
        fields = ("diagnosis", "flag", "scope", "operation")
        if set(arguments) != set(fields) or any(not isinstance(arguments[k], str) or not arguments[k].strip() for k in fields):
            raise ToolError("Proposal requires diagnosis, flag, scope, and operation")
        proposal = Proposal(*(arguments[key].strip() for key in fields))
        if len(proposal.diagnosis) > 1000:
            raise ToolError("Diagnosis exceeds the 1,000 character limit")
        try:
            self._validate(*proposal.operation_tuple)
        except FlagsmithOpsError as exc:
            raise ToolError(self._redact(str(exc))) from None
        return Proposal(self._redact(proposal.diagnosis), *proposal.operation_tuple)


def _tool_call(raw: Any) -> tuple[str, str, dict[str, Any]]:
    try:
        tool_id = raw["id"]
        name = raw["function"]["name"]
        args = json.loads(raw["function"]["arguments"])
        if raw.get("type") != "function" or not isinstance(name, str) or not isinstance(tool_id, str) or not tool_id or not isinstance(args, dict):
            raise TypeError
    except (KeyError, TypeError, ValueError):
        raise ToolError("Malformed function call") from None
    return tool_id, name, args


def run_attempt(model: ChatModel, tools: AttemptTools, previous: list[dict[str, Any]]) -> AttemptResult:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": json.dumps({
        "task": "Diagnose this rollback using the available read tools, then make one terminal decision.",
        "previous_attempts": tools.scrub(previous),
    })}]
    audit: list[dict[str, Any]] = []
    for _ in range(MAX_READ_CALLS + 1):
        raw = model.complete(messages, tools.schemas)
        calls = raw.get("tool_calls")
        content = raw.get("content")
        assistant: dict[str, Any] = {"role": "assistant", "content": content if isinstance(content, str) else None}
        if calls:
            assistant["tool_calls"] = calls
        audit.append(tools.scrub(assistant))
        try:
            if not calls:
                try:
                    answer = json.loads(content) if isinstance(content, str) else None
                except ValueError:
                    answer = None
                if isinstance(answer, dict) and set(answer) == {"no_fix"}:
                    reason = answer["no_fix"]
                    if isinstance(reason, str) and 0 < len(reason.strip()) <= 1000:
                        return AttemptResult("no_fix", tools.scrub(reason.strip()), messages=audit)
                raise ToolError("Expected one proposal or a no_fix answer with a reason")
            if not isinstance(calls, list) or len(calls) > MAX_READ_CALLS:
                raise ToolError("Too many or malformed tool calls")
            parsed = [_tool_call(call) for call in calls]
            if len({call[0] for call in parsed}) != len(parsed):
                raise ToolError("Duplicate tool call IDs")
            if any(name == "propose_operation" for _, name, _ in parsed):
                if len(parsed) != 1:
                    raise ToolError("A proposal must be the only call in its response")
                proposal = tools.propose(parsed[0][2])
                return AttemptResult("proposed", proposal.diagnosis, proposal, audit)
            if len(parsed) > tools.reads_remaining:
                raise ToolError("Read budget exhausted (maximum six calls)")
            messages.append(assistant)
            for tool_id, name, arguments in parsed:
                try:
                    result = {"ok": True, "data": tools.read(name, arguments)}
                except ToolError as exc:
                    result = {"ok": False, "error": str(exc)}
                messages.append({"role": "tool", "tool_call_id": tool_id, "content": json.dumps(result)})
                audit.append({"role": "tool", "name": tools.scrub(name), "ok": result["ok"], **(
                    {"error": result["error"]} if not result["ok"] else {}
                )})
        except ToolError as exc:
            return AttemptResult("invalid", tools.scrub(str(exc)), messages=audit)
    return AttemptResult("invalid", "Model ran out of turns without a terminal decision", messages=audit)
