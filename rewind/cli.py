"""CLI entry points for Rewind."""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rewind", description="Rewind feature-flag rollback rehearser")
    sub = parser.add_subparsers(dest="command")

    check_p = sub.add_parser("check", help="Rehearse and verify a rollback plan")
    check_p.add_argument("--config", required=True, help="Path to rewind.config.yaml")

    sub.add_parser("demo", help="Replay a captured run")

    seed_p = sub.add_parser("seed", help="Seed Flagsmith baseline state")
    seed_p.add_argument("--case", choices=["a", "b", "refusal"], required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    print(f"rewind {args.command}: not implemented yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
