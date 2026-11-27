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

    demo_p = sub.add_parser("demo", help="Replay a captured run offline")
    demo_p.add_argument("--case", choices=["a", "b", "refusal"], default="a", help="Capture to replay (default: Case A)")

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
    if args.command == "check":
        from dotenv import load_dotenv
        from rewind.config import ConfigError, load_config
        from rewind.orchestrator import RunError, run_check
        from rewind.report import ReportError, write_report
        from rewind.report.data import REJECTED_SUGGESTION
        from rewind.report.terminal import RunConsole, attempt_display, plain_output

        root = Path.cwd()
        try:
            load_dotenv(root / ".env", override=False)
            config = load_config(args.config)
            console = RunConsole()
            result = run_check(config, root, console=console)
            report, data = write_report(result.evidence, allowed_flags=config.allowed.flags)
            if data["status"] == "refused":
                for attempt in data["attempts"]:
                    if attempt["suggestion_label"] == REJECTED_SUGGESTION:
                        console.print(attempt_display(attempt), markup=False, highlight=False)
            print(f"Report: {report}")
            return result.exit_code
        except (ConfigError, RunError, ReportError) as exc:
            print(plain_output(f"rewind check: {exc}"), file=sys.stderr)
            return 1
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
    if args.command == "demo":
        from rewind.demo import replay
        from rewind.report import ReportError

        try:
            replay(args.case, REPO_ROOT, Path.cwd())
            return 0
        except ReportError as exc:
            print(f"rewind demo: {exc}", file=sys.stderr)
            return 1
    parser.error("Unknown command")


if __name__ == "__main__":
    sys.exit(main())
