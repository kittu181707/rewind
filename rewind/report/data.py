"""Public report fields derived from a recorded run."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from datetime import datetime, timezone

from rewind.ledger import autonomy_ledger, ledger_breakdown, ledger_summary


VERDICTS = {"verified": "Verified", "refused": "Refused", "stopped": "Stopped", "approval_denied": "Denied"}
REJECTED_SUGGESTION = "Rejected suggestion"
_AUTH_OR_KEY = re.compile(
    r"https?://[^\s\"'<>]*(?:oauth|authorize|login|auth/|[?&](?:token|key|code)=)[^\s\"'<>]*|"
    r"\b(?:Bearer|Basic)\s+[^\s,;]+|\b(?:sk-|gsk_|AIza|ghp_|github_pat_|ser\.|AQ\.)[A-Za-z0-9_.-]{8,}", re.IGNORECASE,
)
_SECRET_FIELDS = {"sdk_key", "api_key", "environment_key", "flag_smith_key", "token", "token_id", "access_token",
                  "refresh_token", "authorization", "client_secret", "password", "request_headers", "request_body"}
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"\b[\w-]*(?:key|token|secret|password)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", re.IGNORECASE,
)
_TIMESTAMPS = {"timestamp", "issued_at", "closed_at", "started_at", "finished_at"}


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


def _environment_secrets() -> tuple[str, ...]:
    return tuple(sorted({value for name, value in os.environ.items() if len(value) >= 8
                        and any(term in name.upper() for term in ("KEY", "TOKEN", "SECRET", "PASSWORD"))},
                        key=len, reverse=True))


def redact_text(value: str) -> str:
    redacted = _sanitize(value, _environment_secrets())
    redacted = _CREDENTIAL_ASSIGNMENT.sub("<REDACTED>", redacted)
    return " ".join("".join(char for char in redacted if char.isprintable() or char.isspace()).split())


def redact_event(data: dict) -> dict:
    return _sanitize(data, _environment_secrets())


def utc_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        date = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fields(item: dict, *names: str) -> dict:
    return {name: utc_time(item[name]) if name in _TIMESTAMPS else item[name] for name in names if name in item}


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


def _attempt(attempt: dict) -> dict:
    proposal = _proposal(attempt)
    label = "Diagnosis"
    if proposal and attempt.get("status") in {"failed", "invalid"}:
        label = REJECTED_SUGGESTION
    elif attempt.get("status") == "no_fix":
        label = "Why Rewind stopped"
    return {**_fields(attempt, "number", "status", "reason"), "proposal": proposal, "suggestion_label": label,
            "verification": _fields(attempt.get("verification") or {}, "ok", "summary")}


def _observation(step: dict, allowed: frozenset[str]) -> dict:
    return {**_fields(step, "name", "step", "user", "http_status", "executed_version"),
            "effective_flags": {flag: value for flag, value in (step.get("effective_flags") or {}).items() if flag in allowed}}


def _target(run: dict, allowed: frozenset[str]) -> dict:
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
            after = {"observation": last["label"], "user_outcomes": {
                user: _observation(step, allowed) for user, step in last.get("user_outcomes", {}).items()
            }}
    return {"failure_reproduced": run.get("target_failure_reproduced"), "before": before, "after": after}


def _approval(item: dict) -> dict:
    record = _fields(item, "gate", "environment", "approved_by", "approval_mode", "issued_at", "timestamp", "status",
                     "summary", "operations", "snapshot_hash", "max_operations", "closed_at")
    if "uses" in item:
        record["uses"] = [_fields(use, "flag", "scope", "operation", "timestamp") for use in item["uses"]]
    return record


def rollback_failure(events: list[dict]) -> dict | None:
    return next((event for event in events if event.get("ok") is False and event["label"].endswith("verify before recovery")),
                next((event for event in events if event.get("ok") is False), None))


def _people(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def failure_summary(failure: dict | None) -> str:
    if not failure:
        return "No failed rollback was recorded."
    checkouts = [step for step in failure.get("steps", []) if step.get("name") == "checkout"]
    errors: dict[int, list[str]] = {}
    for step in checkouts:
        http = step.get("http_status")
        if isinstance(http, int) and 400 <= http <= 599:
            errors.setdefault(http, []).append(step["user"])
    wrong_version = {item.get("user") for item in failure.get("failed_assertions", [])
                     if item.get("assertion") in {"executed_version_matches_goal", "direct_executed_version_matches_goal"}}
    new_checkout = [step["user"] for step in checkouts if step.get("executed_version") == "v2" and step["user"] in wrong_version]
    observations = [f"{_people(users)} got HTTP {http}" for http, users in errors.items()]
    if new_checkout:
        observations.append(f"{_people(new_checkout)} still ran the new checkout")
    return "; ".join(observations) + "." if observations else failure.get("summary") or "The recorded journey did not pass."


def rehearsal_preview(run: dict, allowed_flags: Iterable[str]) -> dict:
    """Use expected flags already calculated by the planner and checked in rehearsal."""
    proposal = next((attempt.get("proposal") for attempt in reversed(run.get("attempts", []))
                     if attempt.get("status") == "verified"), None)
    if not proposal:
        return {}
    rehearsal = run.get("environments", {}).get("rehearsal")
    events = [event for event in run.get("transitions", []) if event.get("environment") == rehearsal]
    verified_index = next((index for index in reversed(range(len(events))) if events[index].get("ok")
                           and events[index]["label"].endswith("verify candidate")), len(events))
    failure = rollback_failure(list(reversed(events[:verified_index])))
    candidate = events[verified_index] if verified_index < len(events) else {}
    before, after = (failure or {}).get("expected_flags", {}), candidate.get("expected_flags", {})
    flag, scope, operation = (proposal[name] for name in ("flag", "scope", "operation"))
    fix = describe_write(flag, scope, operation) + "."
    if operation == "delete_override":
        kind, _, name = scope.partition(":")
        fix = f"Remove the override for {kind} {name} on {flag} so it inherits the default."
    rows = [{"user": user, "flag": name, "before": before.get(user, {}).get(name), "after": after.get(user, {}).get(name)}
            for user in sorted(before.keys() | after.keys()) for name in allowed_flags]
    return {"problem": failure_summary(failure), "diagnosis": proposal.get("diagnosis"), "fix": fix, "visibility": rows}


def flag_value(value: bool | None) -> str:
    return {True: "ON", False: "OFF", None: "Not recorded"}[value]


def flag_changed(row: dict) -> bool:
    return row["before"] is not None and row["after"] is not None and row["before"] != row["after"]


def _summary(data: dict) -> tuple[tuple[str, str], ...]:
    preview = data["rehearsal_preview"]
    observed = preview.get("problem") or failure_summary(rollback_failure(data["transitions"]))
    attempts = data["attempts"]
    label, found = "Diagnosis", "No diagnosis recorded."
    for attempt in reversed(attempts):
        diagnosis = (attempt.get("proposal") or {}).get("diagnosis")
        if attempt.get("status") == "no_fix":
            diagnosis = attempt.get("reason")
        if diagnosis:
            label = attempt["suggestion_label"]
            found = diagnosis
            break

    changed = "No target flag change was applied."
    if data["status"] == "refused":
        failures = sum(attempt.get("status") == "failed" for attempt in attempts)
        count = f": {failures} {'proposal' if failures == 1 else 'proposals'} rejected by the verifier" if failures else ""
        changed = f"No verified fix found{count}. Target unchanged."
    elif data["status"] != "verified":
        target = data.get("environments", {}).get("target")
        writes = [change for change in data["changes"] if change.get("environment") == target and change.get("kind") == "flag"]
        if writes:
            changed = "Stopped during target writes. See Changes made for the recorded outcomes."

    approvals = []
    for gate in data["approvals"]:
        who = gate.get("approval_mode") or data.get("approval_mode")
        when = gate.get("issued_at") or gate.get("timestamp")
        detail = f"{who or 'unknown mode'} at {when or 'unrecorded time'}"
        decision = gate.get("status")
        if decision == "not reached":
            detail = "not reached"
        else:
            detail = f"{decision or 'approved'} · {detail}"
        approvals.append(f"Gate {gate['gate']}: {detail}")
    request = data.get("request") or {}
    request_line = (("Request", f'{request["text"]} — {request["interpretation"]}'),) if request else ()
    fix_line = (("Verified fix", preview["fix"]),) if preview else ()
    target_line = () if preview and data["status"] == "verified" else (("What changed", changed),)
    return (*request_line, ("Problem found in rehearsal", observed), (label, found), *fix_line, *target_line,
            ("Who approved", "; ".join(approvals) + "."),
            ("Autonomy", f"{data['autonomy_summary']} {data['autonomy_breakdown']}"))




_TOTALS_GOT_RE = re.compile(r"got=\((\d+),\s*(\d+)\)")
_CART_GOT_RE = re.compile(r"got_cart=([^,\s]+)")


def customer_view_rows(transition: dict | None) -> list[dict]:
    """Extract per-user shop view fields strictly from recorded transition steps."""
    if not transition:
        return []
    failed_by_user: dict[str, dict[str, str]] = {}
    for item in transition.get("failed_assertions", []):
        if isinstance(item, dict) and item.get("user") and item.get("assertion"):
            failed_by_user.setdefault(str(item["user"]), {})[str(item["assertion"])] = str(item.get("detail") or "")

    rows: list[dict] = []
    for step in transition.get("steps", []):
        if not isinstance(step, dict) or step.get("name") != "checkout":
            continue
        user = step.get("user")
        if not user:
            continue
        assertions = {
            str(entry["name"]): entry
            for entry in step.get("assertions", [])
            if isinstance(entry, dict) and entry.get("name")
        }
        http_status = step.get("http_status")
        executed_version = step.get("executed_version")

        error_text: str | None = None
        http_assertion = assertions.get("http_status_200")
        http_detail = str(http_assertion.get("detail") or "") if http_assertion else failed_by_user.get(str(user), {}).get("http_status_200", "")
        if "error=" in http_detail:
            candidate_err = http_detail.split("error=", 1)[1].strip()
            if candidate_err:
                error_text = candidate_err
        elif isinstance(http_status, int) and http_status >= 400 and http_detail:
            error_text = http_detail

        cart_id: str | None = None
        cart_detail = str((assertions.get("same_customer_and_cart_id") or {}).get("detail") or "")
        cart_match = _CART_GOT_RE.search(cart_detail)
        if cart_match and cart_match.group(1) != "None":
            cart_id = cart_match.group(1)

        totals: str | None = None
        qty_assertion = assertions.get("latest_quantities_survive")
        cart_intact = qty_assertion.get("pass") if isinstance(qty_assertion, dict) and isinstance(qty_assertion.get("pass"), bool) else None
        if isinstance(qty_assertion, dict):
            qty_detail = str(qty_assertion.get("detail") or "")
            tot_match = _TOTALS_GOT_RE.search(qty_detail)
            if tot_match:
                items_count = int(tot_match.group(1))
                cents = int(tot_match.group(2))
                totals = f"{items_count} {'item' if items_count == 1 else 'items'} · ${cents / 100:.2f}"

        ver_assertion = assertions.get("executed_version_matches_goal")
        if isinstance(ver_assertion, dict) and isinstance(ver_assertion.get("pass"), bool):
            version_ok: bool | None = ver_assertion["pass"]
        elif any(k in failed_by_user.get(str(user), {}) for k in ("executed_version_matches_goal", "direct_executed_version_matches_goal")):
            version_ok = False
        else:
            version_ok = None

        is_vip: bool | None = None
        traits_detail = str((assertions.get("traits_unchanged") or {}).get("detail") or "")
        if "'is_vip': True" in traits_detail or '"is_vip": true' in traits_detail.lower():
            is_vip = True
        elif "'is_vip': False" in traits_detail or '"is_vip": false' in traits_detail.lower():
            is_vip = False

        if http_status == 200:
            order_placed: bool | None = True
        elif isinstance(http_status, int) and http_status >= 400:
            order_placed = False
        else:
            order_placed = None

        rows.append({
            "user": str(user),
            "http_status": http_status,
            "executed_version": executed_version,
            "error": error_text,
            "cart_id": cart_id,
            "totals": totals,
            "cart_intact": cart_intact,
            "order_placed": order_placed,
            "version_ok": version_ok,
            "is_vip": is_vip,
        })
    return rows


def _final_verify_transition(run: dict) -> dict | None:
    transitions = run.get("transitions", [])
    target_name = (run.get("environments") or {}).get("target")
    if target_name:
        target_verify = next(
            (tx for tx in reversed(transitions) if tx.get("environment") == target_name and tx.get("phase") == "verify"),
            None,
        )
        if target_verify is not None:
            return target_verify
    return next((tx for tx in reversed(transitions) if tx.get("phase") == "verify"), None)


def public_report(run: dict, *, captured: bool = False, allowed_flags: Iterable[str] | None = None) -> dict:
    secrets = _environment_secrets()
    metadata = _fields(run, "status", "provider", "model", "approval_mode", "environments", "elapsed_seconds",
                       "started_at", "finished_at", "error")
    approval_log = {item["gate"]: item for item in run.get("approvals", [])}
    if allowed_flags is None:
        allowed_flags = next((snapshot["state"]["flags"] for snapshot in run.get("snapshots", [])
                              if "flags" in snapshot.get("state", {})), ())
    allowed = frozenset(allowed_flags)
    ledger = autonomy_ledger(run)
    data = _sanitize({
        "claim": "Off is not undo.", **metadata, "verdict": VERDICTS[run["status"]],
        "source": "captured run" if captured or run.get("capture") else "recorded run",
        "allowed_flags": sorted(allowed),
        "capture": _fields(run.get("capture") or {}, "label", "case", "scenario", "validated_commit"),
        "request": _fields(run.get("request") or {}, "text", "flag", "direction", "interpretation"),
        "rehearsal_preview": rehearsal_preview(run, sorted(allowed)),
        "autonomy_ledger": ledger, "autonomy_summary": ledger_summary(ledger), "autonomy_breakdown": ledger_breakdown(ledger),
        "transitions": [{
            **_fields(event, "label", "environment", "timestamp", "phase", "summary", "ok"),
            "steps": [_observation(step, allowed) for step in event.get("steps", [])],
            "failed_assertions": [_fields(item, "user", "step", "assertion", "detail")
                                  for item in event.get("failed_assertions", [])],
        } for event in run.get("transitions", [])],
        "attempts": [_attempt(attempt) for attempt in run.get("attempts", [])],
        "approvals": [
            _approval(approval_log[gate])
            if gate in approval_log else {"gate": gate, "status": "not reached"} for gate in (1, 2)
        ],
        "target": _target(run, allowed),
        "changes": [{"status": "recorded", **_fields(change, "kind", "environment", "flag", "scope", "operation",
                     "status", "timestamp", "db_path", "user", "step", "details", "duration_ms")}
                    for change in run.get("changes", [])],
        "customer_views": {
            "before": {"label": (rb := rollback_failure(run.get("transitions", []))) and rb.get("label"), "users": customer_view_rows(rb)},
            "after": {"label": (fv := _final_verify_transition(run)) and fv.get("label"), "users": customer_view_rows(fv)},
        },
        "limits": ["The evidence covers the recorded test users and fixtures, not every production customer.",
                   "Rewind tests configuration-only recovery; it does not undo a database migration or rewrite app code.",
                   "Unreached approvals, missing snapshots, and failed checks do not establish target safety."],
    }, secrets)
    summary = _summary(data)
    data["summary_labels"] = [label for label, _ in summary]
    data["summary_lines"] = [line for _, line in summary]
    return data
