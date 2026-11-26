"""Boolean flag projections and reversible diffs for the supported test cohort."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from rewind.config import RewindConfig

Operation = tuple[str, str, str]
State = dict[str, Any]


class PlanningError(RuntimeError):
    """The snapshot cannot be reproduced with the allowed Boolean operations."""


def configuration(state: State) -> State:
    return {"flags": state["flags"], "segments": state["segments"]}


def project(state: State, operations: tuple[Operation, ...]) -> State:
    result = deepcopy(state)
    for flag, scope, operation in operations:
        enabled = operation == "set_on"
        current = result["flags"][flag]
        if scope == "environment_default":
            current["enabled"] = enabled
            continue
        kind, name = scope.split(":", 1)
        if kind == "segment" and not result["segments"].get(name):
            raise PlanningError(f"Segment {name!r} is absent from the snapshot")
        field = "overrides" if kind == "segment" else "identity_overrides_detail"
        overrides = current[field]
        if operation == "delete_override":
            if name not in overrides:
                raise PlanningError(f"No override exists to delete for {flag} / {scope}")
            del overrides[name]
        else:
            if name not in overrides:
                if kind == "segment" and overrides:
                    raise PlanningError("Cannot predict precedence when adding a second segment override")
                overrides[name] = {"value": None, **({"priority": 0} if kind == "segment" else {})}
            overrides[name]["enabled"] = enabled
        current["segment_overrides" if kind == "segment" else "identity_overrides"] = len(overrides)
    return result


def _set(enabled: bool) -> str:
    return "set_on" if enabled else "set_off"


def difference(current: State, goal: State) -> tuple[Operation, ...]:
    """Plan only changes that can preserve rules, values and targeting precedence."""
    if current["segments"] != goal["segments"] or current["flags"].keys() != goal["flags"].keys():
        raise PlanningError("Flag set or segment definitions differ; cannot replay this baseline")
    operations: list[Operation] = []
    for flag, expected in sorted(goal["flags"].items()):
        actual = current["flags"][flag]
        if actual["value"] != expected["value"]:
            raise PlanningError(f"Cannot restore a changed flag value for {flag!r}")
        for kind, field in (("segment", "overrides"), ("identity", "identity_overrides_detail")):
            have, want = actual[field], expected[field]
            operations.extend((flag, f"{kind}:{name}", "delete_override") for name in sorted(have.keys() - want.keys()))
            for name, value in sorted(want.items()):
                if name not in have or have[name]["enabled"] != value["enabled"]:
                    operations.append((flag, f"{kind}:{name}", _set(value["enabled"])))
        if actual["enabled"] != expected["enabled"]:
            operations.append((flag, "environment_default", _set(expected["enabled"])))
    plan = tuple(operations)
    if configuration(project(current, plan)) != configuration(goal):
        raise PlanningError("Boolean changes would not recreate the saved override values or precedence")
    return plan


def _trait_text(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def _matches(rule: dict[str, Any], traits: dict[str, Any]) -> bool:
    conditions: list[bool] = []
    for check in rule.get("conditions") or []:
        if check.get("operator") != "EQUAL" or "value" not in check or "property" not in check:
            raise PlanningError("Only equality-based segment conditions are supported for this rehearsal")
        name = check["property"]
        conditions.append(name in traits and _trait_text(traits[name]) == _trait_text(check["value"]))
    conditions.extend(_matches(child, traits) for child in rule.get("rules") or [])
    match rule.get("type"):
        case "ALL":
            return all(conditions)
        case "ANY":
            return any(conditions)
        case "NONE":
            return not any(conditions)
        case _:
            raise PlanningError("Unsupported segment rule grouping")


def effective_by_user(state: State, config: RewindConfig) -> dict[str, dict[str, bool]]:
    values: dict[str, dict[str, bool]] = {}
    for user in config.users:
        values[user.id] = {}
        for flag, setting in state["flags"].items():
            enabled = setting["enabled"]
            identity = setting["identity_overrides_detail"].get(user.id)
            if identity is not None:
                values[user.id][flag] = identity["enabled"]
                continue
            for name, override in sorted(setting["overrides"].items(), key=lambda pair: (pair[1]["priority"], pair[0])):
                segment = state["segments"].get(name)
                if not segment or not isinstance(segment.get("rules"), dict):
                    raise PlanningError(f"Cannot evaluate missing segment rules for {name!r}")
                if _matches(segment["rules"], user.traits):
                    enabled = override["enabled"]
                    break
            values[user.id][flag] = enabled
    return values
