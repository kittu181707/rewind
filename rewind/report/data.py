"""Public report fields derived from a recorded run."""

from __future__ import annotations

import json
import os
import re


VERDICTS = {"verified": "Verified", "refused": "Refused", "stopped": "Stopped", "approval_denied": "Denied"}
_AUTH_OR_KEY = re.compile(
    r"https?://[^\s\"'<>]*(?:oauth|authorize|login|auth/|[?&](?:token|key|code)=)[^\s\"'<>]*|"
    r"\b(?:Bearer|Basic)\s+[^\s,;]+|\b(?:sk-|gsk_|AIza|ghp_|github_pat_|ser\.|AQ\.)[A-Za-z0-9_.-]{8,}", re.IGNORECASE,
)
_SECRET_FIELDS = {"sdk_key", "api_key", "environment_key", "flag_smith_key", "token", "token_id", "access_token",
                  "refresh_token", "authorization", "client_secret", "password", "request_headers", "request_body"}


def _sanitize(value, secrets: tuple[str, ...]):
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "<REDACTED>")
        return _AUTH_OR_KEY.sub("<REDACTED>", value.replace("\\/", "/"))
    if isinstance(value, dict):
        return {_sanitize(key, secrets): "<REDACTED>" if key.lower().replace("-", "_") in _SECRET_FIELDS
                else _sanitize(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, secrets) for item in value]
    return value


def _fields(item: dict, *names: str) -> dict:
    return {name: item[name] for name in names if name in item}


def describe_write(flag: str, scope: str, operation: str) -> str:
    kind, _, name = scope.partition(":")
    if operation in ("set_on", "set_off"):
        state = "ON" if operation == "set_on" else "OFF"
        if scope == "environment_default":
            return f"{flag}: set default {state}"
        return f"{flag}: set override {state} for {kind} {name}"
    if operation == "delete_override":
        return f"{flag}: delete override for {kind} {name}"
    return f"{flag} / {scope} / {operation}"


def _proposal(attempt: dict) -> dict | None:
    recorded = attempt.get("proposal")
    if isinstance(recorded, dict):
        return _fields(recorded, "diagnosis", "flag", "scope", "operation")
    # An allowlist rejection can happen before a Proposal is created.
    for message in reversed(attempt.get("messages", [])):
        for call in message.get("tool_calls") or []:
            function = call.get("function", {}) if isinstance(call, dict) else {}
            if not isinstance(function, dict) or function.get("name") != "propose_operation":
                continue
            try:
                arguments = json.loads(function.get("arguments", ""))
            except (TypeError, ValueError):
                continue
            if isinstance(arguments, dict):
                return _fields(arguments, "diagnosis", "flag", "scope", "operation")
    return None


def _target(run: dict) -> dict:
    capture = run.get("capture") or {}
    name = run.get("environments", {}).get("target")
    before = capture.get("target_before") or next((
        _fields(snapshot, "hash", "state") for snapshot in run.get("snapshots", [])
        if snapshot.get("environment") == name and snapshot.get("phase") == "Target starting snapshot"
    ), None)
    after = capture.get("target_after")
    if after is None:
        last = next((event for event in reversed(run.get("transitions", []))
                     if event.get("environment") == name and event.get("phase") == "verify"), None)
        if last is not None:
            after = {"observation": last["label"], "user_outcomes": last.get("user_outcomes", {})}
    return {"failure_reproduced": run.get("target_failure_reproduced"), "before": before, "after": after}


def public_report(run: dict, *, captured: bool = False) -> dict:
    secrets = tuple(sorted({value for name, value in os.environ.items() if value and len(value) >= 8
                           and any(term in name.upper() for term in ("KEY", "TOKEN", "SECRET", "PASSWORD"))},
                           key=len, reverse=True))
    metadata = _fields(run, "status", "provider", "model", "approval_mode", "environments", "elapsed_seconds",
                       "started_at", "finished_at", "error")
    approval_log = {item["gate"]: item for item in run.get("approvals", [])}
    return _sanitize({
        "claim": "Off is not undo.", **metadata, "verdict": VERDICTS[run["status"]],
        "source": "captured run" if captured or run.get("capture") else "recorded run",
        "capture": _fields(run.get("capture") or {}, "label", "case", "scenario", "validated_commit"),
        "transitions": [{
            **_fields(event, "label", "environment", "timestamp", "phase", "summary", "ok"),
            "steps": [_fields(step, "name", "user", "http_status", "executed_version", "effective_flags")
                      for step in event.get("steps", [])],
            "failed_assertions": [_fields(item, "user", "step", "assertion", "detail")
                                  for item in event.get("failed_assertions", [])],
        } for event in run.get("transitions", [])],
        "attempts": [{**_fields(attempt, "number", "status", "reason"), "proposal": _proposal(attempt),
                      "verification": _fields(attempt.get("verification") or {}, "ok", "summary")}
                     for attempt in run.get("attempts", [])],
        "approvals": [
            _fields(approval_log[gate], "gate", "environment", "approved_by", "approval_mode", "issued_at", "timestamp",
                    "status", "summary", "operations", "snapshot_hash", "max_operations", "uses", "closed_at")
            if gate in approval_log else {"gate": gate, "status": "not reached"} for gate in (1, 2)
        ],
        "target": _target(run),
        "changes": [{"status": "recorded", **_fields(change, "kind", "environment", "flag", "scope", "operation",
                     "status", "timestamp", "db_path", "user", "step", "details", "duration_ms")}
                    for change in run.get("changes", [])],
        "limits": ["The evidence covers the recorded test users and fixtures, not every production customer.",
                   "Rewind tests configuration-only recovery; it does not undo a database migration or rewrite app code.",
                   "Unreached approvals, missing snapshots, and failed checks do not establish target safety."],
    }, secrets)
