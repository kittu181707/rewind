"""Read the demo target's Flagsmith snapshot without exposing environment keys."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

from dotenv import load_dotenv

from rewind.config import RewindConfig, load_config
from rewind.flagsmith_ops import FlagsmithOps, FlagsmithOpsError
from rewind.snapshot import EnvironmentSnapshot, take_snapshot
from scripts.seed_flagsmith import DEFAULT_CONFIG, ROOT


AUTH_URL = re.compile(r"https?://[^\s\"'<>]*(?:oauth|authorize|login|auth/)[^\s\"'<>]*", re.IGNORECASE)


def snapshot_view(snapshot: EnvironmentSnapshot) -> dict:
    return {
        "environment": snapshot.environment,
        "hash": snapshot.hash,
        "flags": {
            flag: {
                "enabled": state["enabled"],
                "segment_overrides": sorted(state["overrides"]),
                "identity_overrides": sorted(state["identity_overrides_detail"]),
            }
            for flag, state in snapshot.state["flags"].items()
        },
    }


def read_target(config: RewindConfig, ops: FlagsmithOps) -> dict:
    environment = config.flagsmith.target.environment
    key = config.env_key_for(environment)
    matches = [item for item in ops.list_environments() if item.get("name") == environment]
    if len(matches) != 1 or matches[0].get("api_key") != key:
        raise FlagsmithOpsError("flagsmith environment list: target key mapping could not be verified")
    return snapshot_view(take_snapshot(ops, config, environment))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env", override=False)
    ops = None
    try:
        config = load_config(args.config)
        ops = FlagsmithOps(config)
        print(ops.redact(json.dumps(read_target(config, ops), indent=2)))
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        detail = ops.redact(str(exc)) if ops else str(exc)
        print(f"target_hash: {AUTH_URL.sub('<REDACTED_AUTH_URL>', detail)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
