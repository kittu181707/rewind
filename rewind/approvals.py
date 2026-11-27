"""Human-gated rehearsal sessions and single-use target approvals."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import secrets
import sys
from threading import Lock
from typing import Any, Callable, Iterable, Literal

from rewind.report.terminal import rehearsal_screen, target_screen

ApprovalMode = Literal["human", "scripted"]
HUMAN_APPROVER = "human (yes)"
SCRIPTED_APPROVER = "scripted (validation harness)"
_TERMINAL_INPUT = input


class ApprovalError(RuntimeError):
    """Raised when human approval is denied, missing, malformed, or tampered."""


@dataclass(frozen=True)
class ApprovalToken:
    """Immutable handle; runtime cursor and authority live in the private registry."""

    token_id: str = field(repr=False)
    phase: Literal["rehearsal", "target", "setup"]
    approved_by: str
    issued_at: str
    summary: str
    operations: tuple[tuple[str, str, str], ...] = ()
    snapshot_hash: str | None = None
    environment: str | None = None
    max_operations: int = 0
    recovery_index: int | None = None
    reproduction_flags: tuple[str, ...] = ()
    reproduction_signature: tuple = ()
    approval_mode: ApprovalMode = "scripted"


@dataclass(frozen=True)
class ApprovalUse:
    flag: str
    scope: str
    operation: str
    timestamp: str


@dataclass
class _RegistryEntry:
    token: ApprovalToken
    used: bool = False
    closed: bool = False
    uses: list[ApprovalUse] = field(default_factory=list)
    cursor: int = 0
    expected_snapshot_hash: str | None = None
    pending_write: bool = False
    reproduced: bool = False


_REGISTRY: dict[str, _RegistryEntry] = {}
_LOCK = Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_operations(diff: Any) -> tuple[tuple[str, str, str], ...]:
    if isinstance(diff, dict) and "operations" in diff:
        raw_ops = diff["operations"]
    elif isinstance(diff, dict) and {"flag", "scope", "operation"} <= set(diff.keys()):
        raw_ops = [diff]
    elif isinstance(diff, Iterable) and not isinstance(diff, (str, bytes)):
        raw_ops = list(diff)
    else:
        raise ApprovalError("gate2_target diff must contain one or more operations")

    normalized: list[tuple[str, str, str]] = []
    for item in raw_ops:
        if isinstance(item, dict):
            try:
                triple = (str(item["flag"]), str(item["scope"]), str(item["operation"]))
            except KeyError as exc:
                raise ApprovalError(f"Operation dict missing key {exc}") from exc
        elif isinstance(item, (tuple, list)) and len(item) == 3:
            triple = (str(item[0]), str(item[1]), str(item[2]))
        else:
            raise ApprovalError(f"Invalid operation specification in diff: {item!r}")
        if not all(part.strip() for part in triple):
            raise ApprovalError(f"Empty field in operation specification: {triple!r}")
        normalized.append(triple)

    if not normalized:
        raise ApprovalError("gate2_target requires at least one operation in diff")
    return tuple(normalized)


def _format_diff_display(diff: Any, operations: tuple[tuple[str, str, str], ...], snapshot_hash: str) -> str:
    lines = [f"Snapshot hash: {snapshot_hash}", "Exact target operations, in order:"]
    details = diff if isinstance(diff, dict) else {}
    stages = details.get("stages", ())
    for idx, (flag, scope, op) in enumerate(operations, start=1):
        what = "delete override (inherit)" if op == "delete_override" else (
            f"set {'default' if scope == 'environment_default' else 'override'} {'ON' if op == 'set_on' else 'OFF'}"
        )
        stage = f"{stages[idx - 1]}: " if idx <= len(stages) else ""
        lines.append(f"  {idx}. {stage}{flag} / {scope}: {what} [{op}]")
    for key, label in (("environment", "Target environment"), ("starting_state", "Starting flag state"),
                       ("db_path", "Target fixture DB"), ("note", "Note")):
        if details.get(key) is not None:
            lines.append(f"{label}: {details[key]}")
    if details.get("fixture_writes"):
        lines.append("Fixture database writes:")
        lines.extend(f"  - {write}" for write in details["fixture_writes"])
    if details.get("recovery_index") is not None:
        lines.append("recovery runs only if the failure reproduces")
    return "\n".join(lines)


def _failure_signature(result: Any, flags: tuple[str, ...]) -> tuple:
    """Compare checkout evidence, not a caller-supplied success/failure bit."""
    from rewind.verifier import VerificationResult

    if (not isinstance(result, VerificationResult) or result.phase != "verify" or result.ok
            or result.exit_code != 1 or not result.user_outcomes or not result.failed_assertions):
        return ()
    signature = []
    for user, outcome in sorted(result.user_outcomes.items()):
        status = outcome.get("http_status")
        version = outcome.get("executed_version")
        effective = outcome.get("effective_flags", {})
        if (outcome.get("step") != "checkout" or type(status) is not int or status < 200
                or not isinstance(version, str) or not isinstance(effective, dict)
                or any(type(effective.get(flag)) is not bool for flag in flags)):
            return ()
        failures = tuple(sorted(
            (str(item.get("assertion")), str(item.get("detail", "")) if item.get("assertion") == "http_status_200" else "")
            for item in result.failed_assertions if item.get("user") == user and item.get("step") == "checkout"
        ))
        signature.append((user, status, version, tuple((flag, effective[flag]) for flag in flags), failures))
    return tuple(signature) if any(row[-1] for row in signature) else ()


def _register_token(token: ApprovalToken) -> ApprovalToken:
    with _LOCK:
        _REGISTRY[token.token_id] = _RegistryEntry(token=token)
    return token


def approval_identity(
    input_fn: Callable[[str], str], approver: str | None, *, auto_approve: bool = False,
) -> tuple[ApprovalMode, str]:
    """A callback or test auto-approval can never claim interactive approval."""
    mode: ApprovalMode = "human" if input_fn is _TERMINAL_INPUT and not auto_approve else "scripted"
    label = HUMAN_APPROVER if mode == "human" else SCRIPTED_APPROVER
    if approver is not None and approver != label:
        raise ApprovalError("Approver label does not match the approval input mode")
    return mode, "auto-approved (test)" if auto_approve and approver is None else label


def _confirm(prompt: str, gate: str, mode: ApprovalMode, input_fn: Callable[[str], str]) -> None:
    if mode == "human" and not sys.stdin.isatty():
        raise ApprovalError(f"{gate} requires an interactive terminal")
    if input_fn(prompt).strip() != "yes":
        raise ApprovalError(f"{gate} approval denied")


def gate1_rehearsal(
    plan_summary: str,
    *,
    environment: str,
    max_operations: int,
    approver: str | None = None,
    auto_approve: bool = False,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> ApprovalToken:
    """Gate 1: approve a budget of operations in one rehearsal environment."""
    summary_text = str(plan_summary).strip()
    if not summary_text:
        raise ApprovalError("gate1_rehearsal requires a non-empty plan_summary")
    if not environment.strip() or type(max_operations) is not int or max_operations < 1:
        raise ApprovalError("Gate 1 requires an environment and a positive operation budget")
    mode, label = approval_identity(input_fn, approver, auto_approve=auto_approve)

    output_fn(rehearsal_screen(summary_text, environment, max_operations))
    if not auto_approve:
        _confirm('Type "yes" to approve rehearsal changes: ', "Gate 1 rehearsal", mode, input_fn)

    token = ApprovalToken(
        token_id=secrets.token_hex(12),
        phase="rehearsal",
        approved_by=label,
        issued_at=_utc_now(),
        summary=summary_text,
        environment=environment,
        max_operations=max_operations,
        approval_mode=mode,
    )
    return _register_token(token)


def gate2_target(
    diff: Any,
    snapshot_hash: str,
    *,
    approver: str | None = None,
    auto_approve: bool = False,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> ApprovalToken:
    """Approve one exact target sequence, with an optional locked replay recovery step."""
    snap_hash = str(snapshot_hash).strip()
    if not snap_hash:
        raise ApprovalError("gate2_target requires a non-empty snapshot_hash")
    operations = _normalize_operations(diff)
    details = diff if isinstance(diff, dict) else {}
    recovery_index = details.get("recovery_index")
    flags: tuple[str, ...] = ()
    signature: tuple = ()
    if recovery_index is not None:
        if type(recovery_index) is not int or not 0 < recovery_index == len(operations) - 1:
            raise ApprovalError("A gated recovery must be the last write and follow the rollback")
        flags = tuple(sorted(details.get("reproduction_flags", ())))
        signature = _failure_signature(details.get("rehearsal_failure"), flags)
        if not signature:
            raise ApprovalError("Gate 2 requires recorded failing rehearsal checkout evidence")
    rendered = _format_diff_display(diff, operations, snap_hash)
    mode, label = approval_identity(input_fn, approver, auto_approve=auto_approve)

    output_fn(target_screen(operations, snap_hash, details))
    if not auto_approve:
        _confirm('Type "yes" to apply this exact plan to target: ', "Gate 2 target", mode, input_fn)

    return _register_token(ApprovalToken(
        token_id=secrets.token_hex(12), phase="target", approved_by=label,
        issued_at=_utc_now(), summary=rendered, operations=operations, snapshot_hash=snap_hash,
        environment=details.get("environment"), recovery_index=recovery_index,
        reproduction_flags=flags, reproduction_signature=signature, approval_mode=mode,
    ))


def mint_setup_token(purpose: Literal["seed", "cleanup"], note: str = "") -> ApprovalToken:
    """Create a single-use setup/cleanup token that can never mutate the target environment."""
    if purpose not in ("seed", "cleanup"):
        raise ApprovalError(f"Invalid setup token purpose: {purpose!r}")
    token = ApprovalToken(
        token_id=secrets.token_hex(12),
        phase="setup",
        approved_by=f"setup ({purpose})",
        issued_at=_utc_now(),
        summary=note or f"setup:{purpose}",
    )
    return _register_token(token)


def _consume_token(
    token: ApprovalToken | None,
    *,
    environment: str,
    rehearsal_env: str,
    target_env: str,
    operations: tuple[tuple[str, str, str], ...],
    allow_setup: bool = False,
) -> ApprovalToken:
    """Authorize the next rehearsal write or consume a single-use approval."""
    with _LOCK:
        return _authorize(
            token, environment=environment, rehearsal_env=rehearsal_env,
            target_env=target_env, operations=operations, allow_setup=allow_setup,
        )


def _registered(token: ApprovalToken | None) -> _RegistryEntry:
    if not isinstance(token, ApprovalToken):
        raise ApprovalError("A registered ApprovalToken is required")
    entry = _REGISTRY.get(token.token_id)
    if entry is None or entry.token is not token:
        raise ApprovalError("ApprovalToken is forged or not present in registry")
    return entry


def approval_uses(token: ApprovalToken) -> tuple[ApprovalUse, ...]:
    """Return immutable audit data, including write attempts whose CLI call failed."""
    with _LOCK:
        return tuple(_registered(token).uses)


def close_rehearsal(token: ApprovalToken) -> None:
    with _LOCK:
        entry = _registered(token)
        if entry.token.phase != "rehearsal":
            raise ApprovalError("Only a rehearsal session can be closed")
        entry.closed = True


def _authorize(
    token: ApprovalToken | None, *, environment: str, rehearsal_env: str,
    target_env: str, operations: tuple[tuple[str, str, str], ...], allow_setup: bool,
) -> ApprovalToken:
    entry = _registered(token)
    token = entry.token
    if entry.used:
        raise ApprovalError("ApprovalToken has already been used")

    if environment == target_env:
        if token.phase == "setup":
            raise ApprovalError("Setup token cannot mutate the target environment")
        if token.phase != "target":
            raise ApprovalError(f"Target environment {target_env!r} requires a Gate 2 target token, got phase={token.phase!r}")
        if not token.snapshot_hash:
            raise ApprovalError("Target ApprovalToken is missing snapshot_hash")
        if token.operations != operations:
            entry.used = True
            raise ApprovalError(
                f"Target operation list does not match Gate 2 approval: approved={token.operations!r}, requested={operations!r}"
            )
        if token.environment is not None and token.environment != environment:
            entry.used = True
            raise ApprovalError("Gate 2 is bound to a different target environment")
        entry.expected_snapshot_hash = token.snapshot_hash
    elif environment == rehearsal_env:
        allowed_phases = {"rehearsal", "setup"} if allow_setup else {"rehearsal"}
        if token.phase not in allowed_phases:
            raise ApprovalError(
                f"Environment {rehearsal_env!r} rejected token with phase={token.phase!r}"
            )
        if token.phase == "rehearsal":
            if token.environment != environment or entry.closed:
                raise ApprovalError("Gate 1 session is closed or belongs to a different environment")
            if len(operations) != 1:
                raise ApprovalError("Authorize each rehearsal write separately")
            if len(entry.uses) >= token.max_operations:
                raise ApprovalError("Gate 1 rehearsal operation budget exhausted")
            entry.uses.append(ApprovalUse(*operations[0], timestamp=_utc_now()))
            return token
    elif environment == "project":
        if not (allow_setup and token.phase == "setup"):
            raise ApprovalError("Project segment management requires a setup token")
    else:
        raise ApprovalError(f"Unsupported approval environment {environment!r}")

    entry.used = True
    return entry.token


class TargetDriftError(ApprovalError):
    """The target snapshot changed between approved steps."""


def _target_entry(token: ApprovalToken) -> _RegistryEntry:
    entry = _registered(token)
    if entry.token.phase != "target" or not entry.used or entry.closed:
        raise ApprovalError("Target plan is not active")
    return entry


def _verify_target_snapshot(token: ApprovalToken, actual_hash: str) -> None:
    with _LOCK:
        entry = _target_entry(token)
        expected = entry.expected_snapshot_hash
        if actual_hash != expected:
            entry.closed = True
            raise TargetDriftError(
                f"Target snapshot drifted (expected={expected}, current={actual_hash}); Gate 2 approval required again"
            )


def _begin_target_write(token: ApprovalToken, operation: tuple[str, str, str], actual_hash: str) -> None:
    with _LOCK:
        entry = _target_entry(token)
        if (entry.pending_write or entry.cursor >= len(token.operations)
                or token.operations[entry.cursor] != operation):
            entry.closed = True
            raise ApprovalError("Write is not the next approved target operation")
        if entry.cursor == token.recovery_index and not entry.reproduced:
            entry.closed = True
            raise ApprovalError("Recovery is locked until the target reproduces the failure")
        if actual_hash != entry.expected_snapshot_hash:
            entry.closed = True
            raise TargetDriftError(
                f"Target snapshot drifted (expected={entry.expected_snapshot_hash}, current={actual_hash}); "
                "Gate 2 approval required again"
            )
        entry.pending_write = True
        entry.uses.append(ApprovalUse(*operation, timestamp=_utc_now()))


def _finish_target_write(token: ApprovalToken, actual_hash: str) -> None:
    with _LOCK:
        entry = _target_entry(token)
        if not entry.pending_write:
            raise ApprovalError("No authorized target write is pending")
        entry.expected_snapshot_hash = actual_hash
        entry.pending_write = False
        entry.cursor += 1


def _record_target_failure(token: ApprovalToken, result: Any) -> None:
    with _LOCK:
        entry = _target_entry(token)
        if (token.recovery_index is None or entry.cursor != token.recovery_index or entry.pending_write
                or entry.reproduced or _failure_signature(result, token.reproduction_flags) != token.reproduction_signature):
            entry.closed = True
            raise ApprovalError("target did not reproduce the failure")
        entry.reproduced = True


def _close_target(token: ApprovalToken, *, require_complete: bool) -> None:
    with _LOCK:
        entry = _registered(token)
        entry.closed = True
        if require_complete and (entry.cursor != len(token.operations) or entry.pending_write):
            raise ApprovalError("Target plan did not complete; no further writes are authorized")


__all__ = [
    "ApprovalError",
    "ApprovalToken",
    "approval_uses",
    "close_rehearsal",
    "gate1_rehearsal",
    "gate2_target",
    "mint_setup_token",
]
