"""Read-only Flagsmith environment state snapshot and deterministic SHA-256 hash."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from rewind.config import RewindConfig
from rewind.flagsmith_ops import FlagsmithOps


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


def take_snapshot(ops: FlagsmithOps, config: RewindConfig, environment: str) -> EnvironmentSnapshot:
    """Capture all configured flags and segment overrides for an environment."""
    flags_state: dict[str, Any] = {}
    segments = _configured_segments(config)

    for flag_name in sorted(config.allowed.flags):
        default_state = ops.get_flag(environment, flag_name)
        seg_overrides: dict[str, Any] = {}
        if default_state.get("segment_overrides", 0):
            for seg_name in segments:
                override = ops.get_segment_override(environment, flag_name, seg_name)
                if override is not None:
                    seg_overrides[seg_name] = {
                        "enabled": bool(override.get("enabled")),
                        "priority": override.get("priority"),
                        "value": override.get("value"),
                    }
        flags_state[flag_name] = {
            "enabled": bool(default_state.get("enabled")),
            "value": default_state.get("value"),
            "segment_overrides": default_state.get("segment_overrides", 0),
            "identity_overrides": default_state.get("identity_overrides", 0),
            "overrides": seg_overrides,
        }

    canonical_state = {"environment": environment, "flags": flags_state}
    encoded = json.dumps(canonical_state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return EnvironmentSnapshot(environment=environment, state=canonical_state, hash=digest)
