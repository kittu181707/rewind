"""Print one complete, compact block for each Rewind run.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _tokens(calls: list[dict], key: str) -> int | None:
    counts = [call[key] for call in calls if type(call.get(key)) is int]
    return sum(counts) if counts or not calls else None


def summarize(run: dict) -> dict:
    calls = run.get("llm_calls", [])
    attempts = []
    for attempt in run.get("attempts", []):
        proposal = attempt.get("proposal")
        if proposal is None:
            # A rejected proposal may only be present in the recorded assistant reply.
            for message in reversed(attempt.get("messages", [])):
                for call in message.get("tool_calls") or []:
                    function = call.get("function", {}) if isinstance(call, dict) else {}
                    if not isinstance(function, dict) or function.get("name") != "propose_operation":
                        continue
                    try:
                        raw = json.loads(function.get("arguments", ""))
                    except (TypeError, ValueError):
                        continue
                    if isinstance(raw, dict):
                        proposal = raw
                        break
                if proposal is not None:
                    break
        attempts.append({
            "number": attempt["number"], "status": attempt["status"], "reason": attempt.get("reason"),
            "diagnosis": proposal.get("diagnosis") if isinstance(proposal, dict) else None,
            "proposal": {k: v for k, v in proposal.items() if k != "diagnosis"} if isinstance(proposal, dict) else None,
        })
    return {
        "status": run["status"], "provider": run.get("provider"), "model": run.get("model"),
        "approval_mode": run.get("approval_mode", "unrecorded"), "attempts": attempts,
        "error": run.get("error"), "target_failure_reproduced": run.get("target_failure_reproduced"),
        "elapsed_seconds": run.get("elapsed_seconds"), "timings": run.get("timings", []),
        "llm_seconds": round(sum(call.get("latency_ms", 0) for call in calls) / 1000, 3),
        "llm_calls": len(calls), "prompt_tokens": _tokens(calls, "prompt_tokens"),
        "completion_tokens": _tokens(calls, "completion_tokens"),
        "calls_without_token_counts": sum(
            call.get("prompt_tokens") is None or call.get("completion_tokens") is None for call in calls
        ),
        "models_used": sorted({f"{call['provider']}/{call['model']}" for call in calls}),
        "flagsmith_command_count": len(run.get("flagsmith_command_log", [])),
        "changes": [{"status": "recorded" if row.get("kind") == "fixture_write" else "unreported", **row}
                    for row in run.get("changes", [])],
    }


def format_summary(summary: dict, label: str) -> str:
    lines = [
        f"=== {label} ===",
        f"Status: {summary['status']}; model: {summary['provider']}/{summary['model']}; "
        f"approval_mode: {summary['approval_mode']}; attempts: {len(summary['attempts'])}",
        f"Target failure reproduced: {summary['target_failure_reproduced']}",
        f"Total: {summary['elapsed_seconds']} s; LLM HTTP time: {summary['llm_seconds']} s "
        f"({summary['llm_calls']} calls); prompt/completion tokens: {summary['prompt_tokens']}/{summary['completion_tokens']} "
        f"({summary['calls_without_token_counts']} calls without full usage)",
        f"Models called: {', '.join(summary['models_used']) or 'none'}; Flagsmith commands: {summary['flagsmith_command_count']}",
    ]
    if summary["error"]:
        lines.append(f"Error: {summary['error']}")
    for attempt in summary["attempts"]:
        lines.extend([
            f"Attempt {attempt['number']} ({attempt['status']}):",
            f"  Diagnosis: {attempt['diagnosis'] if attempt['diagnosis'] is not None else 'not recorded'}",
            f"  Proposal: {json.dumps(attempt['proposal'], ensure_ascii=False)}",
            f"  Reason: {attempt['reason'] if attempt['reason'] is not None else 'not recorded'}",
        ])
    lines.append("Seconds per phase:")
    lines.extend(f"  {phase['phase']}: {phase['seconds']}" for phase in summary["timings"])
    lines.append(f"Changes ({len(summary['changes'])}):")
    lines.extend(f"  {json.dumps(change, ensure_ascii=False, sort_keys=True)}" for change in summary["changes"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", type=Path, nargs="+")
    args = parser.parse_args(argv)
    for path in args.runs:
        print(format_summary(summarize(json.loads(path.read_text(encoding="utf-8"))), str(path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
