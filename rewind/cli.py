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

    do_p = sub.add_parser("do", help="Interpret a rollback request and rehearse it")
    do_p.add_argument("request", help="Rollback request in plain words")
    do_p.add_argument("--config", required=True, help="Path to rewind.config.yaml")

    ui_p = sub.add_parser("ui", help="Open the local UI or export captured browser replays")
    mode = ui_p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--config", help="Path to rewind.config.yaml for a live run")
    mode.add_argument("--replay", action="store_true", help="Replay the captures locally without credentials")
    mode.add_argument("--export", type=Path, help="Write a static offline replay site to this folder")
    ui_p.add_argument("--case", choices=["a", "b", "refusal"], help="Starting capture for --replay (default: Case A)")

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
    if args.command in {"check", "do"}:
        from dotenv import load_dotenv
        from rewind.agent.llm import LLMError
        from rewind.config import ConfigError, load_config
        from rewind.orchestrator import RunError, run_check
        from rewind.report import ReportError, write_report
        from rewind.report.data import REJECTED_SUGGESTION
        from rewind.report.terminal import RunConsole, attempt_display, plain_output
        from rewind.request import interpret, refusal

        root = Path.cwd()
        try:
            load_dotenv(root / ".env", override=False)
            config = load_config(args.config)
            console = RunConsole()
            if args.command == "do":
                selection = interpret(config, args.request)
                if selection is None:
                    print(refusal(config))
                    return 1
                result = run_check(config, root, console=console, request=selection)
            else:
                result = run_check(config, root, console=console)
            report, data = write_report(result.evidence, allowed_flags=config.allowed.flags)
            if data["status"] == "refused":
                for attempt in data["attempts"]:
                    if attempt["suggestion_label"] == REJECTED_SUGGESTION:
                        console.print(attempt_display(attempt), markup=False, highlight=False)
            print(f"Report: {report}")
            return result.exit_code
        except (ConfigError, LLMError, RunError, ReportError) as exc:
            print(plain_output(f"rewind {args.command}: {exc}"), file=sys.stderr)
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
    if args.command == "ui":
        from rewind.config import ConfigError, load_config
        from rewind.report.data import redact_text
        from rewind.web import serve
        from rewind.web.replay import export_replay

        if args.case is not None and not args.replay:
            parser.error("--case is only supported with --replay")
        try:
            if args.export is not None:
                export_replay(REPO_ROOT, args.export)
                print(f"Browser replay: {args.export / 'index.html'}")
            else:
                config = None
                if args.config is not None:
                    from dotenv import load_dotenv

                    load_dotenv(Path.cwd() / ".env", override=False)
                    config = load_config(args.config)
                serve(Path.cwd(), REPO_ROOT, config=config, case=args.case or "a")
        except (ConfigError, OSError, ValueError) as exc:
            print(redact_text(f"rewind ui: {exc}"), file=sys.stderr)
            return 1
        return 0
    parser.error("Unknown command")


if __name__ == "__main__":
    sys.exit(main())
