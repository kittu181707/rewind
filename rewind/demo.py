"""Play back recorded evidence without running the agent or touching Flagsmith."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Callable

from rich.console import Console

from rewind.report import ReportError, write_report
from rewind.report.data import describe_write
from rewind.report.terminal import RunConsole, captured_screen, plain_output


REPLAY_SECONDS = 9.0


def _diagnosis(attempt: dict) -> str:
    proposal = attempt.get("proposal") or {}
    reason = attempt.get("reason")
    diagnosis = proposal.get("diagnosis") or reason or "Not recorded"
    result = f"Attempt {attempt['number']} · Diagnosis: {diagnosis}"
    if proposal:
        operation = describe_write(*(str(proposal.get(field, "—")) for field in ("flag", "scope", "operation")))
        result += f"\nProposed change: {operation}"
    if reason and reason != diagnosis:
        result += f"\nResult: {reason}"
    return result


def _events(data: dict, width: int) -> list[str]:
    target = data.get("environments", {}).get("target")
    attempts = iter(data["attempts"])
    events = [captured_screen(data["approvals"][0], None, width=width)]
    target_events = []
    for transition in data["transitions"]:
        phase = f"{transition['label']} · {transition.get('summary', 'No result recorded')}"
        if transition.get("environment") == target:
            target_events.append(phase)
        else:
            events.append(phase)
            if transition["label"].endswith("verify before recovery"):
                attempt = next(attempts, None)
                if attempt is not None:
                    events.append(_diagnosis(attempt))
    events.extend(_diagnosis(attempt) for attempt in attempts)
    events.append(captured_screen(data["approvals"][1], data["target"]["before"], width=width))
    events.extend(target_events)
    return events


def replay(
    case: str, repository: Path, output_root: Path, *,
    console: Console | None = None, sleep: Callable[[float], None] | None = None,
) -> Path:
    capture_name = {"a": "case-a", "refusal": "refusal"}[case]
    source = repository / "docs" / "captured-runs" / capture_name / "run.json"
    if not source.is_file():
        raise ReportError("Captured run is missing; use the repository checkout with docs/captured-runs")
    output = output_root / ".artifacts" / datetime.now(timezone.utc).strftime("demo-%Y%m%dT%H%M%S%fZ")
    report, data = write_report(source, output, captured=True)
    terminal = console or RunConsole()
    wait = sleep or time.sleep
    terminal.print(f"captured run | Off is not undo. · {capture_name}", style="bold cyan", soft_wrap=True)
    terminal.print("captured run | Playback only. No approvals, network calls, or changes will be made.", soft_wrap=True)
    events = _events(data, terminal.width)
    for event in events:
        line = event if event.startswith("╭") else f"captured run | {event}"
        terminal.print(plain_output(line), markup=False, highlight=False, soft_wrap=True)
        wait(REPLAY_SECONDS / len(events))
    terminal.print(f"captured run | Report: {report.relative_to(output_root)}", markup=False, highlight=False, soft_wrap=True)
    final = {
        "verified": "The tested journey was verified on the allowlisted staging target.",
        "refused": "No verified configuration-only recovery found. Target unchanged.",
    }.get(data["status"], f"Verdict: {data['verdict']}")
    terminal.print(f"captured run | {final}", style="bold", markup=False, highlight=False, soft_wrap=True)
    return report
