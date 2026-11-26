"""Approval token management enforced by flagsmith_ops."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import secrets


@dataclass(frozen=True)
class ApprovalToken:
    """Opaque authorization token required by flagsmith_ops for any mutation."""

    token_id: str
    phase: str
    environment: str
    approved_by: str
    issued_at: str


def issue_token(phase: str, environment: str, approved_by: str = "operator") -> ApprovalToken:
    """Create an explicit approval token for rehearsal, target, or setup mutations."""
    return ApprovalToken(
        token_id=secrets.token_hex(8),
        phase=phase,
        environment=environment,
        approved_by=approved_by,
        issued_at=datetime.now(timezone.utc).isoformat(),
    )
