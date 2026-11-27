"""Read-only model tools and one validated, non-executing proposal per attempt."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Callable, Literal

from rewind.agent.llm import ChatModel
from rewind.agent.prompt import SYSTEM_PROMPT
from rewind.config import RewindConfig
from rewind.flagsmith_ops import FlagsmithOpsError


MAX_READ_CALLS = 6
MAX_SOURCE_BYTES = 128 * 1024
MAX_SOURCE_CHARS = 4_500
REQUIRED_READS = frozenset({"read_evidence", "read_flag_state", "read_source"})
READ_FIRST = "Read evidence, flag state and source first"
TERMINAL_TOOLS = frozenset({"propose_operation", "report_no_fix"})


class ToolError(ValueError):
    """An invalid read request or terminal model action."""


class _ReadsRequired(ToolError):
    pass


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


def _source_excerpt(path: Path, text: str, flags: frozenset[str]) -> str:
    """Prefer complete flag-referencing blocks in large Python source files."""
    if len(text) <= MAX_SOURCE_CHARS:
        return text
    if path.suffix == ".py":
        try:
            nodes = ast.parse(text).body
        except SyntaxError:
            nodes = []
        lines = text.splitlines()
        relevant = ["\n".join(lines[node.lineno - 1:node.end_lineno]) for node in nodes if any(
            isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value in flags
            for value in ast.walk(node)
        )]
        if relevant:
            return "[Source excerpts referencing configured flags; unrelated blocks omitted.]\n" + "\n\n".join(relevant)[:MAX_SOURCE_CHARS]
    half = MAX_SOURCE_CHARS // 2
    return text[:half] + "\n[Middle of source omitted.]\n" + text[-half:]


class AttemptTools:
    def __init__(
        self, config: RewindConfig, source_root: Path, flag_state: dict[str, Any], evidence: dict[str, Any],
        validate: Callable[[str, str, str], None], redact: Callable[[str], str],
    ) -> None:
        self._root = source_root.resolve()
        self._sources = {f"source:{index}": value for index, value in enumerate(config.source_paths, 1)}
        self._flags = frozenset(config.allowed.flags)
        self.task = {"rollback_applied": f"{config.rollback.flag} was set to {config.rollback.to}",
                     "goal": config.rollback.goal}
        self._validate = validate
        self._redact = redact
        self._flag_state = self.scrub(flag_state)
        self._evidence = self.scrub(evidence)
        self._reads = 0
        self._completed_reads: set[str] = set()
        self._terminal_submitted = False
        self.schemas = [
            _function("read_source", "Read one allowlisted application source path by its listed alias.", {"path": _text_enum(list(self._sources))}),
            _function("read_flag_state", "Read the flag defaults, relevant overrides and observed effective states.", {}),
            _function("read_evidence", "Read the latest fixed journey and its failed assertions.", {}),
            _function("propose_operation", "Propose one configuration operation. Does not apply anything.", {
                "diagnosis": {"type": "string", "description": "Name the failure mechanism in one or two plain sentences."},
                "flag": _text_enum(list(config.allowed.flags)), "scope": _text_enum(list(config.allowed.scopes)),
                "operation": _text_enum(["set_on", "set_off", "delete_override"]),
            }),
            _function("report_no_fix", "Finish without proposing a change when no configuration-only fix exists.", {
                "reason": {"type": "string", "description": "Explain why configuration alone cannot fix the failure (maximum 1,000 characters)."},
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
            result = self.read_source(arguments["path"])
        elif name == "read_flag_state" and not arguments:
            result = self._flag_state
        elif name == "read_evidence" and not arguments:
            result = self._evidence
        else:
            raise ToolError("Unknown read tool or invalid arguments")
        self._completed_reads.add(name)
        return result

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
            return self._redact(_source_excerpt(relative, data.decode("utf-8"), self._flags))
        except (OSError, UnicodeError, RuntimeError):
            raise ToolError("Could not read the allowlisted UTF-8 source") from None

    def _submit_terminal(self) -> None:
        if self._terminal_submitted:
            raise ToolError("Only one terminal decision is allowed per attempt")
        if not REQUIRED_READS <= self._completed_reads:
            raise _ReadsRequired(READ_FIRST)
        self._terminal_submitted = True

    def propose(self, arguments: dict[str, Any]) -> Proposal:
        self._submit_terminal()
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

    def report_no_fix(self, arguments: dict[str, Any]) -> str:
        self._submit_terminal()
        reason = arguments.get("reason")
        if set(arguments) != {"reason"} or not isinstance(reason, str) or not 0 < len(reason.strip()) <= 1000:
            raise ToolError("report_no_fix requires a non-empty reason of at most 1,000 characters")
        return self._redact(reason.strip())[:1000]


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


def _no_fix_reason(content: Any) -> str | None:
    """Read older model responses; the current prompt uses report_no_fix."""
    if not isinstance(content, str):
        return None
    text = content.strip()
    fence = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", text, flags=re.IGNORECASE | re.DOTALL)
    if fence is not None:
        text = fence.group(1)

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Duplicate JSON keys")
        return result

    try:
        answer = json.loads(text, object_pairs_hook=unique_object)
    except (ValueError, RecursionError):
        return None
    if not isinstance(answer, dict) or set(answer) != {"no_fix"}:
        return None
    reason = answer["no_fix"]
    if not isinstance(reason, str):
        return None
    reason = reason.strip()
    return reason if 0 < len(reason) <= 1000 else None


def run_attempt(model: ChatModel, tools: AttemptTools, previous: list[dict[str, Any]]) -> AttemptResult:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": json.dumps({
        "task": "Diagnose this rollback using the available read tools, then make one terminal decision.",
        **tools.scrub(tools.task),
        "constraint": "A proposal that moves any user away from the goal is wrong.",
        "previous_attempts": tools.scrub(previous),
    }, separators=(",", ":"))}]
    audit: list[dict[str, Any]] = []
    reminded = False
    for _ in range(MAX_READ_CALLS + 2):
        raw = model.complete(messages, tools.schemas)
        calls = raw.get("tool_calls")
        content = raw.get("content")
        assistant: dict[str, Any] = {"role": "assistant", "content": content if isinstance(content, str) else None}
        if calls:
            assistant["tool_calls"] = calls
        audit.append(tools.scrub(assistant))
        try:
            if not calls:
                reason = _no_fix_reason(content)
                if reason is not None:
                    return AttemptResult("no_fix", tools.scrub(reason), messages=audit)
                raise ToolError("Expected one proposal or a report_no_fix call with a reason")
            if not isinstance(calls, list) or len(calls) > MAX_READ_CALLS:
                raise ToolError("Too many or malformed tool calls")
            parsed = [_tool_call(call) for call in calls]
            if len({call[0] for call in parsed}) != len(parsed):
                raise ToolError("Duplicate tool call IDs")
            if any(name in TERMINAL_TOOLS for _, name, _ in parsed):
                if len(parsed) != 1:
                    raise ToolError("A terminal tool must be the only call in its response")
                tool_id, name, arguments = parsed[0]
                try:
                    if name == "report_no_fix":
                        return AttemptResult("no_fix", tools.report_no_fix(arguments), messages=audit)
                    proposal = tools.propose(arguments)
                except _ReadsRequired:
                    if reminded:
                        raise
                    reminded = True
                    messages.extend([assistant, {"role": "tool", "tool_call_id": tool_id,
                        "content": json.dumps({"ok": False, "error": READ_FIRST})}])
                    audit.append({"role": "tool", "name": name, "ok": False, "error": READ_FIRST})
                    continue
                return AttemptResult("proposed", proposal.diagnosis, proposal, audit)
            if len(parsed) > tools.reads_remaining:
                raise ToolError("Read budget exhausted (maximum six calls)")
            messages.append(assistant)
            for tool_id, name, arguments in parsed:
                try:
                    result = {"ok": True, "data": tools.read(name, arguments)}
                except ToolError as exc:
                    result = {"ok": False, "error": str(exc)}
                messages.append({"role": "tool", "tool_call_id": tool_id,
                                 "content": json.dumps(result, separators=(",", ":"))})
                audit.append({"role": "tool", "name": tools.scrub(name), "ok": result["ok"], **(
                    {"error": result["error"]} if not result["ok"] else {}
                )})
        except ToolError as exc:
            return AttemptResult("invalid", tools.scrub(str(exc)), messages=audit)
    return AttemptResult("invalid", "Model ran out of turns without a terminal decision", messages=audit)
