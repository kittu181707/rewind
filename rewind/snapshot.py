"""Read-only Flagsmith environment state snapshot and deterministic SHA-256 hash."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from rewind.config import RewindConfig
from rewind.flagsmith_ops import FlagsmithOps, FlagsmithOpsError


@dataclass(frozen=True)
class EnvironmentSnapshot:
    environment: str
    state: dict[str, Any]
    hash: str


def _configured_segments(config: RewindConfig) -> list[str]:
    segments: list[str] = []
    for scope in config.allowed.scopes:
        if scope.startswith("segment:"):
            segments.append(scope.split(":", 1)[1])
    return sorted(set(segments))


def _configured_users(config: RewindConfig) -> list[str]:
    return sorted({u.id for u in config.users})


def take_snapshot(
    ops: FlagsmithOps,
    config: RewindConfig,
    environment: str,
    *,
    strict_overrides: bool = True,
) -> EnvironmentSnapshot:
    """Capture flags, segment overrides/rules, and identity overrides for an environment."""
    segments = _configured_segments(config)
    users = _configured_users(config)

    all_segments = ops.list_segments()
    segments_by_name = {
        str(seg.get("name")): {
            "id": seg.get("id"),
            "rules": seg.get("rules"),
        }
        for seg in all_segments
        if seg.get("name")
    }
    configured_segments_state = {
        seg_name: segments_by_name.get(seg_name)
        for seg_name in segments
    }

    flags_state: dict[str, Any] = {}
    for flag_name in sorted(config.allowed.flags):
        default_state = ops.get_flag(environment, flag_name)
        seg_count = int(default_state.get("segment_overrides") or 0)
        id_count = int(default_state.get("identity_overrides") or 0)

        seg_overrides: dict[str, Any] = {}
        if seg_count > 0:
            for seg_name in segments:
                override = ops.get_segment_override(environment, flag_name, seg_name)
                if override is not None:
                    seg_overrides[seg_name] = {
                        "enabled": bool(override.get("enabled")),
                        "priority": override.get("priority"),
                        "value": override.get("value"),
                    }

        id_overrides: dict[str, Any] = {}
        if id_count > 0:
            for uid in users:
                override = ops.get_identity_override(environment, flag_name, uid)
                if override is not None:
                    id_overrides[uid] = {
                        "enabled": bool(override.get("enabled")),
                        "value": override.get("value"),
                    }

        if strict_overrides:
            if seg_count > len(seg_overrides):
                raise FlagsmithOpsError(
                    f"Unsupported or unconfigured segment override on {flag_name!r} in {environment!r}: "
                    f"total={seg_count}, captured={len(seg_overrides)}"
                )
            if id_count > len(id_overrides):
                raise FlagsmithOpsError(
                    f"Unsupported or unconfigured identity override on {flag_name!r} in {environment!r}: "
                    f"total={id_count}, captured={len(id_overrides)}"
                )

        flags_state[flag_name] = {
            "enabled": bool(default_state.get("enabled")),
            "value": default_state.get("value"),
            "segment_overrides": seg_count,
            "identity_overrides": id_count,
            "overrides": seg_overrides,
            "identity_overrides_detail": id_overrides,
        }

    canonical_state = {
        "environment": environment,
        "segments": configured_segments_state,
        "flags": flags_state,
    }
    encoded = json.dumps(canonical_state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return EnvironmentSnapshot(environment=environment, state=canonical_state, hash=digest)
