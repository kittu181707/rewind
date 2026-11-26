"""CLI entry points for Rewind."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rewind", description="Rewind feature-flag rollback rehearser")
    sub = parser.add_subparsers(dest="command")

    check_p = sub.add_parser("check", help="Rehearse and verify a rollback plan")
    check_p.add_argument("--config", required=True, help="Path to rewind.config.yaml")

    sub.add_parser("demo", help="Replay a captured run")

    seed_p = sub.add_parser("seed", help="Seed Flagsmith baseline state")
    seed_p.add_argument("--case", choices=["a", "b", "refusal"], required=True)
    seed_p.add_argument("--environment", help="Configured rehearsal environment (the default)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    if args.command == "seed":
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from dotenv import load_dotenv
        from scripts.seed_flagsmith import DEFAULT_CONFIG, ROOT, seed_case

        from rewind.config import ConfigError, load_config
        from rewind.flagsmith_ops import FlagsmithOps, FlagsmithOpsError

        try:
            load_dotenv(ROOT / ".env", override=False)
            config = load_config(DEFAULT_CONFIG)
            environment = args.environment or config.flagsmith.rehearsal.environment
            result = seed_case(FlagsmithOps(config), config, environment, args.case)
        except (ConfigError, FlagsmithOpsError) as exc:
            print(f"rewind seed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0
    print(f"rewind {args.command}: not implemented yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
