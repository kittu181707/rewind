"""Idempotent Flagsmith seeder for Rewind cases (a, b, refusal)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

from dotenv import load_dotenv

from rewind.approvals import mint_setup_token
from rewind.config import load_config
from rewind.flagsmith_ops import FlagsmithOps, FlagsmithOpsError
from rewind.snapshot import take_snapshot

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "examples" / "demo-shop" / "rewind.config.yaml"


def test_throwaway_segment_lifecycle(ops: FlagsmithOps) -> None:
    """Prove `flagsmith segment create <name> --rules @file` and clean deletion."""
    throwaway = f"rewind_throwaway_{int(time.time())}"
    rules = {
        "type": "ALL",
        "rules": [],
        "conditions": [{"property": "is_throwaway", "operator": "EQUAL", "value": "true"}],
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        json.dump(rules, tmp)
        rules_path = Path(tmp.name)
    try:
        rec_create = ops.create_segment(
            throwaway,
            rules_path,
            approval=mint_setup_token("seed", "create throwaway segment"),
            description="Throwaway segment CLI test",
        )
        print(f"THROWAWAY_CREATE: exit={rec_create.exit_code} ({rec_create.duration_ms}ms)")
        segs = [s.get("name") for s in ops.list_segments()]
        if throwaway not in segs:
            raise RuntimeError(f"Throwaway segment {throwaway!r} missing from segment list after create")
        rec_del = ops.delete_segment(
            throwaway,
            approval=mint_setup_token("cleanup", "delete throwaway segment"),
        )
        print(f"THROWAWAY_DELETE: exit={rec_del.exit_code} ({rec_del.duration_ms}ms)")
        segs_after = [s.get("name") for s in ops.list_segments()]
        if throwaway in segs_after:
            raise RuntimeError(f"Throwaway segment {throwaway!r} still in segment list after delete")
    finally:
        rules_path.unlink(missing_ok=True)


def seed_case(ops: FlagsmithOps, config, environment: str, case: str) -> dict:
    """Reset the rehearsal environment to the requested case baseline using setup tokens."""
    if environment == config.flagsmith.target.environment:
        raise FlagsmithOpsError(
            f"seed_case uses setup tokens and cannot mutate target environment {environment!r}"
        )

    for flag in config.allowed.flags:
        for scope in config.allowed.scopes:
            if scope.startswith("segment:"):
                seg = scope.split(":", 1)[1]
                ops.clear_segment_override_if_present(
                    environment,
                    flag,
                    seg,
                    approval=mint_setup_token("cleanup", f"clear segment override {flag}/{seg}"),
                )
        for user in config.users:
            ops.clear_identity_override_if_present(
                environment,
                flag,
                user.id,
                approval=mint_setup_token("cleanup", f"clear identity override {flag}/{user.id}"),
            )

    if case in ("a", "refusal"):
        ops.apply_operation(
            environment,
            "checkout_v2",
            "environment_default",
            "set_on",
            approval=mint_setup_token("seed", f"{case}:checkout_v2=on"),
        )
        ops.apply_operation(
            environment,
            "cart_compat_reader",
            "environment_default",
            "set_off",
            approval=mint_setup_token("seed", f"{case}:cart_compat_reader=off"),
        )
    elif case == "b":
        ops.apply_operation(
            environment,
            "checkout_v2",
            "environment_default",
            "set_on",
            approval=mint_setup_token("seed", "b:checkout_v2=on"),
        )
        ops.apply_operation(
            environment,
            "checkout_v2",
            "segment:vip_users",
            "set_on",
            approval=mint_setup_token("seed", "b:vip_users=on"),
        )
        ops.apply_operation(
            environment,
            "cart_compat_reader",
            "environment_default",
            "set_on",
            approval=mint_setup_token("seed", "b:cart_compat_reader=on"),
        )
    else:
        raise ValueError(f"Unknown case {case!r}")

    snap = take_snapshot(ops, config, environment)
    return {"environment": environment, "case": case, "hash": snap.hash, "state": snap.state}


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="Seed Flagsmith baseline for Rewind.")
    parser.add_argument("--case", choices=["a", "b", "refusal"], required=True)
    parser.add_argument("--environment", default=None, help="Target rehearsal environment (default: rehearsal)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--test-segment-create", action="store_true", help="Test throwaway segment create/delete first")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    ops = FlagsmithOps(cfg)
    env = args.environment or cfg.flagsmith.rehearsal.environment

    if args.test_segment_create:
        test_throwaway_segment_lifecycle(ops)

    res = seed_case(ops, cfg, env, args.case)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
