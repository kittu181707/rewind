"""Read-only events shared by the UI and the rehearsal runner."""

from dataclasses import dataclass
from typing import Literal

from rewind.report.data import REJECTED_SUGGESTION, customer_view_rows, describe_write, failure_summary

EventType = Literal[
    "started",
    "inventory",
    "interpretation",
    "progress",
    "failure",
    "attempt",
    "gate",
    "decision",
    "final",
]


@dataclass(frozen=True)
class RunEvent:
    type: EventType
    data: dict


def failure_event(transition: dict) -> dict:
    wrong_version = {
        item.get("user")
        for item in transition.get("failed_assertions", [])
        if item.get("assertion") in {"executed_version_matches_goal", "direct_executed_version_matches_goal"}
    }
    rows = [
        {
            "user": user,
            "http_status": result.get("http_status"),
            "version": result.get("executed_version"),
            "version_ok": user not in wrong_version,
        }
        for user, result in transition.get("user_outcomes", {}).items()
    ]
    return {
        "problem": failure_summary(transition),
        "users": rows,
        "customers": customer_view_rows(transition),
    }


def attempt_event(attempt: dict) -> dict:
    proposal = attempt.get("proposal") or {}
    fix = describe_write(proposal["flag"], proposal["scope"], proposal["operation"]) if proposal else None
    return {"number": attempt.get("number"), "status": attempt.get("status"), "fix": fix,
            "label": REJECTED_SUGGESTION if proposal and attempt.get("status") in {"invalid", "failed"} else "Diagnosis",
            "diagnosis": proposal.get("diagnosis") or attempt.get("reason")}


def starting_flags(state: dict) -> list[dict]:
    return [{"flag": flag, "enabled": setting.get("enabled"), "overrides": [
        {"kind": kind, "name": name, "enabled": entry.get("enabled")}
        for kind, key in (("segment", "overrides"), ("identity", "identity_overrides_detail"))
        for name, entry in setting.get(key, {}).items()
    ]} for flag, setting in state.get("flags", {}).items()]


def final_event(data: dict, report: str) -> RunEvent:
    outcome = {
        "verified": "The tested journey is verified on staging.",
        "refused": "No verified configuration-only recovery found. Target unchanged.",
        "approval_denied": "Approval declined. Target unchanged by Rewind.",
        "stopped": "Rewind stopped. See the report for the recorded changes.",
    }
    return RunEvent("final", {"verdict": data["verdict"], "status": data["status"], "text": outcome[data["status"]],
                              "error": data.get("error"), "ledger": data["autonomy_summary"],
                              "ledger_counts": data.get("autonomy_ledger"),
                              "customer_views": data.get("customer_views"),
                              "breakdown": data["autonomy_breakdown"], "report": report})
