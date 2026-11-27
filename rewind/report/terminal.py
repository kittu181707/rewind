"""The same read-only approval screens for live checks and captured replay."""

from __future__ import annotations

import ast
from io import StringIO
import json
import re

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from rewind.report.data import describe_write


_URL = re.compile(r"\b(?:https?|ftp|file)://[^\s\"'<>]+", re.IGNORECASE)
_REPR = re.compile(r"\{[^\n]*\}|\[\s*['\"({][^\n]*\]|\(\s*['\"][^\n]*\)")
_STAGE = re.compile(r"^\s*\d+\. (baseline setup|rollback|recovery):")
_WORDS = {"set_on": "Switch on", "set_off": "Switch off", "delete_override": "Remove override"}


def _change_line(change: dict) -> str:
    if change.get("kind") == "flag" and all(key in change for key in ("flag", "scope", "operation")):
        action = describe_write(change["flag"], change["scope"], change["operation"])
    else:
        action = str(change.get("kind", "change")).replace("_", " ")
        if change.get("user"):
            action += f" for {change['user']}"
    if change.get("db_path"):
        action += f" · {change['db_path']}"
    return f"  - {change.get('environment', 'Environment unrecorded')}: {action} · {change.get('status', 'recorded')}"


def plain_output(text: str) -> str:
    prefix = text.lstrip()
    if prefix.startswith("- {"):
        try:
            change = json.loads(prefix[2:])
        except ValueError:
            pass
        else:
            if isinstance(change, dict):
                text = _change_line(change)
    return _REPR.sub("[details in run.json]", _URL.sub("[URL omitted]", text))


class RunConsole(Console):
    """Leave run evidence alone and clean only what goes to the terminal."""

    def print(self, *objects, **kwargs) -> None:
        super().print(*(plain_output(item) if isinstance(item, str) else item for item in objects), **kwargs)


def _literal(text: str):
    if len(text) <= 32_000:
        try:
            return ast.literal_eval(text)
        except (ValueError, SyntaxError, RecursionError):
            pass
    return None


def _panel(title: str, parts: list, width: int | None) -> str:
    stream = StringIO()
    console = Console(file=stream, width=width or Console().width, color_system=None)
    console.print(Panel(Group(*parts), title=plain_output(title), title_align="left"))
    return stream.getvalue().rstrip()


def _text(text: str) -> Text:
    return Text(plain_output(text))


def _scope(scope: str) -> str:
    kind, _, name = scope.partition(":")
    return "environment default" if kind == "environment_default" else f"{kind} {name}".strip()


def rehearsal_screen(
    summary: str, environment: str, budget: int, *, title: str = "GATE 1: Rehearsal approval", width: int | None = None,
) -> str:
    lines = [f"Environment: {environment}"]
    for line in summary.splitlines():
        if line.startswith("Rollback: "):
            raw, _, rest = line.removeprefix("Rollback: ").partition("; up to ")
            operation = _literal(raw)
            if isinstance(operation, tuple) and len(operation) == 3 and all(isinstance(value, str) for value in operation):
                lines.append(f"Rollback: {describe_write(*operation)}")
            else:
                lines.append("Rollback: see recorded change")
            if rest:
                lines.append(f"Attempts: up to {rest}")
        elif line.startswith("Allowed flags: "):
            for rule in line.split("; "):
                label, _, names = rule.rstrip(".").partition(": ")
                values = names.split(", ")
                if label == "operations":
                    values = [_WORDS.get(value, value) for value in values]
                if label == "scopes":
                    values = [_scope(value) for value in values]
                lines.append(f"{label.capitalize()}: {', '.join(values)}")
        elif line:
            lines.append(line)
    lines.append(f"maximum Flagsmith operations: {budget}")
    return _panel(title, [_text(line) for line in lines], width)


def _table(*headers: str) -> Table:
    table = Table(box=None, padding=(0, 1), pad_edge=False, expand=True)
    for header in headers:
        table.add_column(header, overflow="fold")
    return table


def _operation_text(scope: str, operation: str) -> str:
    if operation == "delete_override":
        return "Remove override; inherit default"
    level = "default" if scope == "environment_default" else "override"
    return f"Set {level} {'ON' if operation == 'set_on' else 'OFF'}"


def _short_fixture_write(line: str) -> str:
    return (line.replace("Reset target fixture:", "Reset:")
                .replace("with their original v1 carts", "with original v1 carts")
                .replace("clear all fixture orders", "clear orders")
                .replace("Verify before and after recovery:", "Verify both times:")
                .replace("insert one test order per successful checkout (no order for a failed checkout)",
                         "write one test order per successful checkout"))


def target_screen(
    operations, snapshot_hash: str, details: dict, *, title: str = "GATE 2: Target approval", width: int | None = None,
) -> str:
    writes = _table("Step", "Stage", "Flag", "Scope", "Operation")
    writes.columns[1].min_width = len("baseline setup")
    writes.columns[2].min_width = min(20, max((len(flag) for flag, _, _ in operations), default=4))
    stages = details.get("stages") or []
    for step, (flag, scope, operation) in enumerate(operations, 1):
        stage = stages[step - 1] if step <= len(stages) else "—"
        writes.add_row(*(_text(str(value)) for value in (step, stage, flag, _scope(scope), _operation_text(scope, operation))))
    parts = [_text(f"Environment: {details.get('environment') or 'configured target'}"), writes, _text("Starting state")]
    state = details.get("starting_state") or {}
    if state.get("flags"):
        flags = _table("Flag", "Default", "Overrides")
        for flag, setting in state["flags"].items():
            names = []
            for kind, overrides in (("segment", setting.get("overrides", setting.get("segment_overrides"))),
                                    ("identity", setting.get("identity_overrides_detail", setting.get("identity_overrides")))):
                if isinstance(overrides, dict):
                    names.extend(f"{kind} {name}: {'ON' if value.get('enabled') else 'OFF'}" for name, value in overrides.items())
                elif isinstance(overrides, list):
                    names.extend(f"{kind} {name}" for name in overrides)
            flags.add_row(_text(flag), _text("ON" if setting.get("enabled") else "OFF"), _text(", ".join(names) or "None"))
        parts.append(flags)
    else:
        parts.append(_text("No starting flag details recorded."))
    parts.append(_text(f"Snapshot: {snapshot_hash[:12]} (full hash in run.json)"))
    if details.get("db_path"):
        parts.append(_text(f"Fixture database: {details['db_path']}"))
    if details.get("fixture_writes"):
        parts.append(_text("Fixture database writes:"))
        parts.extend(_text(f"  • {_short_fixture_write(line)}") for line in details["fixture_writes"])
    if details.get("recovery_index") is not None:
        parts.append(_text("Recovery runs only if the failure reproduces."))
    return _panel(title, parts, width)


def captured_screen(gate: dict, before: dict | None, *, width: int) -> str:
    number = gate["gate"]
    if gate.get("status") == "not reached":
        return f"Gate {number} — not reached in this captured run"
    mode = "captured, scripted approval" if gate.get("approval_mode") == "scripted" else "captured approval"
    title = f"Gate {number} — {mode} ({gate.get('status', 'approved')})"
    summary = gate.get("summary") or ""
    environment = gate.get("environment")
    if number == 1:
        return rehearsal_screen(summary, environment or "rehearsal environment", gate.get("max_operations", 0), title=title, width=width)
    lines = summary.splitlines()
    recorded_state = next((line.removeprefix("Starting flag state: ") for line in lines if line.startswith("Starting flag state: ")), "")
    state = _literal(recorded_state)
    details = {"environment": environment, "starting_state": state if isinstance(state, dict) else before,
               "db_path": next((line.removeprefix("Target fixture DB: ") for line in lines if line.startswith("Target fixture DB: ")), None),
               "fixture_writes": [line.removeprefix("  - ") for line in lines if line.startswith("  - ")],
               "stages": [match[1] for line in lines if (match := _STAGE.match(line))]}
    if "recovery" in details["stages"]:
        details["recovery_index"] = details["stages"].index("recovery")
    return target_screen(gate.get("operations", []), gate.get("snapshot_hash") or "unrecorded", details, title=title, width=width)
