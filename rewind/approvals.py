"""Human-gated rehearsal sessions and single-use target approvals."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import secrets
from threading import Lock
from typing import Any, Callable, Iterable, Literal


class ApprovalError(RuntimeError):
    """Raised when human approval is denied, missing, malformed, or tampered."""


@dataclass(frozen=True)
class ApprovalToken:
    """Immutable approval token registered by approvals.py."""

    token_id: str = field(repr=False)
    phase: Literal["rehearsal", "target", "setup"]
    approved_by: str
    issued_at: str
    summary: str
    operations: tuple[tuple[str, str, str], ...] = ()
    snapshot_hash: str | None = None
    environment: str | None = None
    max_operations: int = 0


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
    lines = [
        f"Snapshot hash: {snapshot_hash}",
        "Exact target operations:",
    ]
    for idx, (flag, scope, op) in enumerate(operations, start=1):
        lines.append(f"  {idx}. flag={flag} scope={scope} operation={op}")
    if isinstance(diff, dict):
        for extra_key in ("db_path", "environment", "note", "fixture_writes"):
            if extra_key in diff and diff[extra_key] is not None:
                lines.append(f"{extra_key}: {diff[extra_key]}")
    return "\n".join(lines)


def _register_token(token: ApprovalToken) -> ApprovalToken:
    with _LOCK:
        _REGISTRY[token.token_id] = _RegistryEntry(token=token)
    return token


def gate1_rehearsal(
    plan_summary: str,
    *,
    environment: str,
    max_operations: int,
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

    output_fn("=== GATE 1: REHEARSAL APPROVAL REQUIRED ===")
    output_fn(f"Environment: {environment}; maximum Flagsmith operations: {max_operations}")
    output_fn(summary_text)
    if auto_approve:
        approved_by = "auto-approved (test)"
    else:
        reply = input_fn('Type "yes" to approve rehearsal changes: ')
        if reply.strip() != "yes":
            raise ApprovalError("Gate 1 rehearsal approval denied")
        approved_by = "human (yes)"

    token = ApprovalToken(
        token_id=secrets.token_hex(12),
        phase="rehearsal",
        approved_by=approved_by,
        issued_at=_utc_now(),
        summary=summary_text,
        environment=environment,
        max_operations=max_operations,
    )
    return _register_token(token)


def gate2_target(
    diff: Any,
    snapshot_hash: str,
    *,
    auto_approve: bool = False,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> ApprovalToken:
    """Gate 2: show the exact target diff and starting snapshot hash and require 'yes'."""
    snap_hash = str(snapshot_hash).strip()
    if not snap_hash:
        raise ApprovalError("gate2_target requires a non-empty snapshot_hash")

    operations = _normalize_operations(diff)
    rendered = _format_diff_display(diff, operations, snap_hash)

    output_fn("=== GATE 2: TARGET APPROVAL REQUIRED ===")
    output_fn(rendered)
    if auto_approve:
        approved_by = "auto-approved (test)"
    else:
        reply = input_fn('Type "yes" to apply this exact plan to target: ')
        if reply.strip() != "yes":
            raise ApprovalError("Gate 2 target approval denied")
        approved_by = "human (yes)"

    token = ApprovalToken(
        token_id=secrets.token_hex(12),
        phase="target",
        approved_by=approved_by,
        issued_at=_utc_now(),
        summary=rendered,
        operations=operations,
        snapshot_hash=snap_hash,
    )
    return _register_token(token)


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


__all__ = [
    "ApprovalError",
    "ApprovalToken",
    "approval_uses",
    "close_rehearsal",
    "gate1_rehearsal",
    "gate2_target",
    "mint_setup_token",
]
