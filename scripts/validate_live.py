"""Run live validation batches with explicitly scripted approvals."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import time

import requests
from dotenv import dotenv_values, load_dotenv
from rich.console import Console

from rewind.agent.llm import LLMAdapter
from rewind.agent.tools import AttemptTools, ToolError
from rewind.approvals import SCRIPTED_APPROVER
from rewind.config import RewindConfig, load_config
from rewind.flagsmith_ops import FlagsmithOps, _default_runner
from rewind.orchestrator import REFUSAL, Instance, LocalApp, run_check
from rewind.planning import configuration, project
from rewind.snapshot import take_snapshot
from scripts.seed_flagsmith import DEFAULT_CONFIG, ROOT, seed_case
from scripts.summarize_run import format_summary, summarize
from scripts.target_hash import AUTH_URL, snapshot_view


ENVIRONMENTS = frozenset({"rehearsal", "demo-target"})
FLAG_NAMES = frozenset({"checkout_v2", "cart_compat_reader"})
REQUIRED = ("GROQ_API_KEY", "GEMINI_API_KEY", "FLAGSMITH_ENV_KEY_REHEARSAL", "FLAGSMITH_ENV_KEY_TARGET")
FLAKY_MODEL_OUTCOMES = {"rate_limited", "server_error", "timeout", "transport_error"}
RUN_INTERVAL_SECONDS = 45
WRITES = ("flagsmith flag update ", "flagsmith flag delete ")
AUTH_URL_BYTES = re.compile(AUTH_URL.pattern.encode(), re.IGNORECASE)


class ValidationError(RuntimeError):
    pass


class FirstProposalModel(LLMAdapter):
    """Verified scenarios stop before another diagnosis if the first proposal failed."""

    def complete(self, messages, tools):
        if len(messages) == 2 and json.loads(messages[1]["content"])["previous_attempts"]:
            raise ValidationError("The first rehearsal proposal failed; review its diagnosis before continuing")
        return super().complete(messages, tools)


@dataclass(frozen=True)
class Scenario:
    name: str
    case: str
    answers: tuple[str, ...]
    status: str


SCENARIOS = (
    Scenario("V1", "a", ("yes", "no"), "approval_denied"),
    Scenario("V2", "a", ("no",), "approval_denied"),
    *(Scenario(f"V{n}", "a", ("yes", "yes"), "verified") for n in range(3, 6)),
    *(Scenario(f"V{n}", "refusal", ("yes",), "refused") for n in range(6, 9)),
)
BATCHES = {
    "baseline": SCENARIOS,
    "refusal-protocol": SCENARIOS[2:],
    "case-b": tuple(Scenario(f"B{n}", "b", ("yes", "yes"), "verified") for n in range(1, 4)),
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def git_bytes(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], input=stdin, capture_output=True, timeout=60, check=False)
    if result.returncode:
        # Git object data and stderr are never echoed by this helper.
        raise ValidationError(f"{shlex.join(['git', *args])}: exit {result.returncode}")
    return result.stdout


def redact(text: str, values: dict[str, str | None]) -> str:
    for value in sorted({value for value in values.values() if value}, key=len, reverse=True):
        text = text.replace(value, "<REDACTED>")
    return AUTH_URL.sub("<REDACTED_AUTH_URL>", text)


@contextmanager
def variable(name: str, value: str | None):
    previous = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


class WriteTrace:
    """Associate redacted CLI records with independently checked environment names."""

    def __init__(self, config: RewindConfig, permitted: set[str], runner=_default_runner) -> None:
        self.target_by_key = {config.env_key_for(name): name for name in config.allowed_environments()}
        self.permitted = permitted
        self.runner = runner
        self.writes: list[dict] = []

    def __call__(self, argv, env):
        command = argv[1:3]
        if command in (["environment", "list"], ["segment", "list"], ["flag", "get"]):
            return self.runner(argv, env)
        require(command in (["flag", "update"], ["flag", "delete"]), "Validation rejected a non-flag CLI write")
        require(argv.count("--environment") == 1 and len(argv) >= 7, "Validation rejected a flag write without one environment")
        key_index = argv.index("--environment") + 1
        require(key_index < len(argv), "Validation rejected a missing environment key")
        environment = self.target_by_key.get(argv[key_index])
        require(environment in self.permitted, "Validation blocked an out-of-scope Flagsmith environment write")
        flag = argv[3]
        require(flag in FLAG_NAMES, "Validation blocked an out-of-scope flag write")

        arguments = argv[4:key_index - 1]
        operation = "delete_override"
        if command == ["flag", "update"]:
            require(bool(arguments) and arguments[0] in {"--enable", "--disable"}, "Validation blocked a flag-value change")
            operation = "set_on" if arguments[0] == "--enable" else "set_off"
            arguments = arguments[1:]
        require(arguments in ([], ["--segment", "vip_users"]) and argv[key_index + 1:] == ["--yes"],
                "Validation blocked an out-of-scope override or unexpected CLI arguments")
        require(operation != "delete_override" or bool(arguments), "Validation blocked deletion of a flag default")
        scope = "segment:vip_users" if arguments else "environment_default"
        safe_command = shlex.join(["flagsmith", *argv[1:key_index], "<REDACTED>", "--yes"])
        write = {"environment": environment, "flag": flag, "scope": scope, "operation": operation,
                 "command": safe_command, "timestamp": timestamp(), "status": "uncertain"}
        self.writes.append(write)
        result = self.runner(argv, env)
        write["status"] = "applied" if result.returncode == 0 else "failed or uncertain"
        write["exit_code"] = result.returncode
        return result


def preflight(config: RewindConfig, values: dict, log, batch: str) -> dict:
    for name in REQUIRED:
        print(f"{name}: {'OK' if values.get(name) else 'MISSING'}", file=log, flush=True)
    require(all(values.get(name) for name in REQUIRED), "Required variable missing in .env; see name-only preflight")
    require(values[REQUIRED[2]] != values[REQUIRED[3]], "Flagsmith SDK keys must be distinct")
    require(config.allowed_environments() == ENVIRONMENTS and set(config.allowed.flags) == FLAG_NAMES
            and set(config.allowed.scopes) == {"environment_default", "segment:vip_users"},
            "Validation only supports the dedicated demo environments, flags and VIP override")
    dirty = git_bytes(ROOT, "status", "--porcelain")
    require(not dirty, f"git status --porcelain: working tree has {len(dirty.splitlines())} change(s)")
    head = git_bytes(ROOT, "rev-parse", "HEAD").decode().strip()
    original_freeze = bool(git_bytes(ROOT, "tag", "--list", "agent-freeze").strip())
    require(original_freeze == (batch != "baseline"), "Unexpected agent-freeze tag state for this batch")
    if batch == "case-b":
        frozen = git_bytes(ROOT, "rev-parse", "agent-freeze-2^{}").decode().strip()
        require(head == frozen, "Case B must run at agent-freeze-2")
    if batch == "refusal-protocol":
        require(not git_bytes(ROOT, "tag", "--list", "agent-freeze-2").strip(), "Revision 2 is already frozen")
    ports = (config.app.rehearsal_port, config.app.target_port)
    require(ports == (8001, 8002), "Validation expects rehearsal port 8001 and target port 8002")
    for port in ports:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            require(probe.connect_ex(("127.0.0.1", port)) != 0, f"Local port {port} is already occupied")
    reads = FlagsmithOps(config, runner=WriteTrace(config, set()))
    environments = reads.list_environments()
    for name in sorted(ENVIRONMENTS):
        matches = [env for env in environments if env.get("name") == name]
        require(len(matches) == 1 and matches[0].get("project") == 55311 and matches[0].get("api_key") == config.env_key_for(name),
                f"flagsmith environment list --json: could not confirm {name} in project 55311")
        snap = take_snapshot(reads, config, name)
        require(not any(flag["identity_overrides_detail"] for flag in snap.state["flags"].values()),
                f"{name} has identity overrides; validation will not clear them")
    require(config.agent is not None and config.agent.provider == "groq" and config.agent.model == "openai/gpt-oss-120b",
            "Validation requires the configured Groq primary model")
    test_env = {key: value for key, value in os.environ.items() if key not in values and key != "FLAGSMITH_API_KEY"}
    test_env.update(TMPDIR="/tmp", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    pytest = [sys.executable, "-B", "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-m", "not live"]
    result = subprocess.run(pytest, cwd=ROOT, env=test_env, capture_output=True, text=True, timeout=120, check=False)
    output = redact((result.stdout + result.stderr).strip(), values)
    print(f"{shlex.join(pytest)}\n{output}", file=log, flush=True)
    require(result.returncode == 0, f"{shlex.join(pytest)}: exited {result.returncode}\n{output}")
    return {"status": "PASS", "commit": head, "git_clean": True,
            "flagsmith_environment_list": sorted(ENVIRONMENTS), "ports_free": list(ports), "keys_distinct": True,
            "env_names": list(REQUIRED), "pytest": output.splitlines()[-1]}


def validate_outcome(scenario: Scenario, row: dict, run: dict, transcript: str) -> None:
    require(run["status"] == scenario.status,
            f"{scenario.name}: rewind check expected {scenario.status}, got {run['status']}: {run.get('error', '')}")
    approval_log = run["approvals"]
    require(run.get("approval_mode") == "scripted" and len(approval_log) == len(scenario.answers),
            f"{scenario.name}: expected {len(scenario.answers)} scripted approval(s)")
    for gate, (approval, answer) in enumerate(zip(approval_log, scenario.answers), 1):
        require(approval["gate"] == gate and approval.get("approval_mode") == "scripted"
                and (approval.get("approved_by") == SCRIPTED_APPROVER if answer == "yes"
                     else approval.get("approved_by") is None and approval.get("status") == "not approved"),
                f"{scenario.name}: Gate {gate} did not record the expected scripted decision")
    require(len(row["writes"]) == sum(record["command"].startswith(WRITES) for record in run["flagsmith_command_log"]),
            f"{scenario.name}: observed Flagsmith writes do not match the command log")
    target_writes = [write for write in row["writes"] if write["environment"] == "demo-target"]
    if scenario.status != "verified":
        require(row["target_before"]["hash"] == row["target_after"]["hash"] and not target_writes
                and not row["target_changes"], f"{scenario.name}: target was changed or drifted")
    if scenario.name == "V2":
        require(row["rehearsal_before"]["hash"] == row["rehearsal_after"]["hash"] and not row["writes"]
                and not run["changes"] and not run["llm_calls"], "V2: denial did not stop all work after seeding")
    if scenario.case == "refusal":
        require(REFUSAL in transcript.splitlines() and "GATE 2:" not in transcript and not target_writes,
                f"{scenario.name}: refusal sentence or no-target invariant did not hold")
    if scenario.status == "verified":
        require([attempt["status"] for attempt in run["attempts"]] == ["verified"],
                f"{scenario.name}: rehearsal required a correction; review the recorded diagnoses before freezing")
        require(run["target_failure_reproduced"] is True and any(
            step["label"] == "Target: verify after recovery" and step["ok"] is True for step in run["transitions"]
        ), f"{scenario.name}: there is no passing target verification after reproducing the failure")
        require(row["target_final_matches_plan"] is True, f"{scenario.name}: final target flag state differs from the approved replay")


def is_flaky(row: dict, run: dict) -> bool:
    if row.get("target_changes") or any(write["environment"] == "demo-target" for write in row["writes"]):
        return False
    if row.get("exception_type") in {"TimeoutExpired", "Timeout", "ReadTimeout", "ConnectTimeout"}:
        return True
    if run.get("status") != "stopped":
        return False
    calls = run.get("llm_calls", [])
    return (run.get("error_kind") == "provider" or (bool(calls) and calls[-1].get("outcome") in FLAKY_MODEL_OUTCOMES)
            or "timed out" in (run.get("error") or "").lower())


def run_scenario(
    scenario: Scenario, number: int, config: RewindConfig, folder: Path, values: dict, *, provider_only_retry: bool,
) -> dict:
    label = f"{scenario.name}.{number}"
    trace_seed = WriteTrace(config, {"rehearsal"})
    trace_run = WriteTrace(config, set(ENVIRONMENTS) if scenario.status == "verified" else {"rehearsal"})
    seed = FlagsmithOps(config, runner=trace_seed)
    ops = FlagsmithOps(config, runner=trace_run)
    reader = FlagsmithOps(config, runner=WriteTrace(config, set()))
    row = {"run": scenario.name, "try": number, "case": scenario.case, "expected": scenario.status,
           "started_at": timestamp(), "outcome": "FAIL", "writes": trace_run.writes, "seed_writes": trace_seed.writes}
    transcript_path = folder / f"{label}.log"
    row["transcript"] = str(transcript_path.relative_to(ROOT))
    replies = iter(scenario.answers)
    run: dict = {}

    with transcript_path.open("w", encoding="utf-8", buffering=1) as transcript:
        def answer(prompt):
            response = next(replies, None)
            require(response is not None, f"{label}: unexpected approval prompt")
            print(f"{prompt}{response} [scripted]", file=transcript)
            return response

        try:
            print(f"{label}: rewind seed --case {scenario.case} (shared seed_case implementation)", file=transcript)
            before = take_snapshot(reader, config, "demo-target")
            row["target_before"] = snapshot_view(before)
            seed_case(seed, config, "rehearsal", scenario.case)
            if scenario.name == "V2":
                row["rehearsal_before"] = snapshot_view(take_snapshot(reader, config, "rehearsal"))
            command = f"rewind check --config {DEFAULT_CONFIG.relative_to(ROOT)}"
            print(f"{label}: {command} (shared run_check implementation; scripted approval input)", file=transcript)
            variant = "no_compat" if scenario.case == "refusal" else None
            model = None
            if scenario.status == "verified":
                assert config.agent is not None
                model = FirstProposalModel(config.agent)
            with variable("APP_VARIANT", variant):
                result = run_check(config, ROOT, ops=ops, model=model,
                                   console=Console(file=transcript, width=135, force_terminal=False),
                                   input_fn=answer, approver=SCRIPTED_APPROVER)
            row["evidence"] = str(result.evidence.relative_to(ROOT))
            run = json.loads(result.evidence.read_text(encoding="utf-8"))
            row["summary"] = summarize(run)
            row["unknown_tool_400s"] = [call["provider_message"] for call in run["llm_calls"]
                if call["http_status"] == 400 and any(phrase in (call.get("provider_message") or "").lower()
                    for phrase in ("unknown tool", "not in request.tools"))]
            row["report_no_fix_used"] = any(
                attempt["status"] == "no_fix" and attempt.get("messages") and any(
                    call.get("function", {}).get("name") == "report_no_fix"
                    for call in attempt["messages"][-1].get("tool_calls", [])
                ) for attempt in run["attempts"]
            )
            row["target_changes"] = [change for change in row["summary"]["changes"] if change["environment"] == "demo-target"]
            require(run["status"] == scenario.status,
                    f"{label}: {command}: expected {scenario.status}, got {run['status']}: {run.get('error') or 'see recorded attempts'}")
            after = take_snapshot(reader, config, "demo-target")
            row["target_after"] = snapshot_view(after)
            if scenario.name == "V2":
                row["rehearsal_after"] = snapshot_view(take_snapshot(reader, config, "rehearsal"))
            if scenario.status == "verified":
                ordered = tuple(tuple(op) for op in run["approvals"][-1]["operations"])
                row["target_final_matches_plan"] = configuration(project(before.state, ordered)) == configuration(after.state)
            validate_outcome(scenario, row, run, transcript_path.read_text(encoding="utf-8"))
            row["outcome"] = "PASS"
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
            row["exception_type"] = type(exc).__name__
            row["error"] = redact(ops.redact(seed.redact(reader.redact(str(exc) or type(exc).__name__))), values)
            row["retryable"] = is_flaky(row, run) and (not provider_only_retry or run.get("error_kind") == "provider")
            print(f"FAIL: {row['error']}", file=transcript)
            if provider_only_retry and "target_before" in row and "target_after" not in row:
                try:
                    row["target_after"] = snapshot_view(take_snapshot(reader, config, "demo-target"))
                except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as snapshot_error:
                    row["target_snapshot_error"] = redact(reader.redact(str(snapshot_error)), values)
        finally:
            for name, client in (("Seed", seed), ("Read-only snapshots", reader)):
                records = [asdict(record) for record in client.command_log]
                print(f"{name} command records:\n{redact(json.dumps(records, indent=2), values)}", file=transcript)
            if "summary" in row:
                print(format_summary(row["summary"], label), file=transcript)
            row["finished_at"] = timestamp()
    return row


def _historical_contents(root: Path):
    entries = git_bytes(root, "rev-list", "--objects", "--all", "--reflog").splitlines()
    ids = []
    for line in entries:
        object_id, _, path = line.partition(b" ")
        if any(part.lower().startswith(b"case_h") for part in path.split(b"/")):
            continue
        ids.append(object_id)
    if not ids:
        return
    batch = git_bytes(root, "cat-file", "--batch", stdin=b"\n".join(dict.fromkeys(ids)) + b"\n")
    offset = 0
    while offset < len(batch):
        end = batch.index(b"\n", offset)
        _, kind, size = batch[offset:end].split()
        offset = end + 1
        finish = offset + int(size)
        if kind in {b"blob", b"commit", b"tag"}:
            yield batch[offset:finish]
        offset = finish + 1


def secrets_audit(root: Path, values: dict[str, str | None], extra: tuple[Path, ...] = ()) -> dict:
    """Count exact dotenv value matches and auth URLs, without revealing content or paths."""
    needles = {name: value.encode() for name, value in values.items() if value}

    def count(contents):
        counts = Counter({name: 0 for name in needles})
        links, checked = 0, 0
        for data in contents:
            checked += 1
            for name, needle in needles.items():
                counts[name] += data.count(needle)
            links += len(AUTH_URL_BYTES.findall(data.replace(b"\\/", b"/")))
        return {"scanned": checked, "value_matches": dict(counts), "auth_url_matches": links}

    paths = set(extra)
    for directory in (root / ".artifacts", root / "artifacts", root / "docs/captured-runs"):
        for path in directory.rglob("*"):
            if any(part.lower().startswith("case_h") for part in path.relative_to(directory).parts):
                continue
            if not path.is_symlink() and path.is_file():
                paths.add(path)
    sources = {"captured_outputs": count(path.read_bytes() for path in sorted(paths)), "git_history": count(_historical_contents(root))}
    return {"status": "PASS" if not any(item["auth_url_matches"] or any(item["value_matches"].values())
                                        for item in sources.values()) else "FAIL", **sources}


def app_environment_audit(config: RewindConfig, folder: Path) -> dict:
    source = folder / "probe_app.py"
    source.write_text('''import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

class Probe(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({name: name in os.environ for name in sys.argv[2:]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

HTTPServer(("127.0.0.1", int(sys.argv[1])), Probe).serve_forever()
''', encoding="utf-8")
    absent = (*REQUIRED, "FLAGSMITH_API_KEY")
    present = ("FLAGSMITH_ENV_KEY", "DB_PATH")
    probe_config = config.model_copy(deep=True)
    probe_config.app.start = f"python {shlex.quote(str(source))} {{port}} {' '.join((*present, *absent))}"
    app = LocalApp(probe_config, ROOT)
    observations = {}
    # Supply a fixture value even when the optional management variable is normally absent.
    with variable("FLAGSMITH_API_KEY", "validation-subprocess-fixture"):
        for environment in sorted(ENVIRONMENTS):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            instance = Instance(environment, port, folder / "probe.db", folder / "probe_state.json")
            with app.start(instance):
                response = requests.get(f"{instance.url}/health", timeout=2)
                response.raise_for_status()
                observations[environment] = response.json()
    expected = {**dict.fromkeys(present, True), **dict.fromkeys(absent, False)}
    return {"status": "PASS" if all(item == expected for item in observations.values()) else "FAIL", "observations": observations}


def read_boundary_audit(config: RewindConfig, folder: Path) -> dict:
    link = folder / "probe_link.py"
    link.symlink_to(folder / "probe_app.py")
    paths = ["journey.py", "verifier.py", ".env", "../..", str(folder / "probe_app.py"), link.name]
    probe_config = config.model_copy(update={"source_paths": paths})
    tools = AttemptTools(probe_config, folder, {}, {}, lambda *_: None, lambda text: text)
    results = []
    for path in paths:
        try:
            tools.read("read_source", {"path": path})
        except ToolError as exc:
            results.append({"path": path, "refused": True, "reason": str(exc)})
        else:
            results.append({"path": path, "refused": False})
    return {"status": "PASS" if all(result["refused"] for result in results) else "FAIL", "probes": results}


def write_audits(rows: list[dict]) -> dict:
    writes = [write for row in rows for field in ("writes", "seed_writes") for write in row[field]]
    forms = sorted({(write["environment"], write["command"]) for write in writes})
    refusal_counts = {row["run"]: sum(write["environment"] == "demo-target" for write in row["writes"])
                      for row in rows if row["case"] == "refusal" and row["outcome"] == "PASS"}
    verified = next(row for row in rows if row["expected"] == "verified" and row["outcome"] == "PASS")
    run = json.loads((ROOT / verified["evidence"]).read_text(encoding="utf-8"))
    gate = run["approvals"][0]
    uses = gate["uses"]
    rehearsal_writes = [item for item in verified["writes"] if item["environment"] == "rehearsal"]
    keys = ("flag", "scope", "operation")
    budget_ok = (0 < len(uses) <= gate["max_operations"] and bool(gate.get("closed_at"))
                 and all(datetime.fromisoformat(use["timestamp"]).tzinfo is not None for use in uses)
                 and [[use[key] for key in keys] for use in uses] == [[write[key] for key in keys] for write in rehearsal_writes])
    audits = {
        "c": {"status": "PASS" if all(name in ENVIRONMENTS for name, _ in forms) else "FAIL", "command_count": len(writes),
              "forms": [{"environment": name, "command": command} for name, command in forms]},
        "f": {"status": "PASS" if budget_ok else "FAIL", "run": verified["run"], "used": len(uses),
              "max_operations": gate["max_operations"], "uses": uses},
    }
    if any(row["case"] == "refusal" for row in rows):
        audits["d"] = {"status": "PASS" if refusal_counts == {"V6": 0, "V7": 0, "V8": 0} else "FAIL",
                       "target_writes": refusal_counts}
    return audits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", choices=BATCHES, default="baseline")
    batch = parser.parse_args(argv).batch
    require(Path.cwd().resolve() == ROOT, "Run from the repository root")
    values = dict(dotenv_values(ROOT / ".env"))
    load_dotenv(ROOT / ".env", override=True)
    folder = ROOT / ".artifacts" / datetime.now(timezone.utc).strftime("validation-%Y%m%dT%H%M%S%fZ")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    config = load_config(DEFAULT_CONFIG)
    record = {"status": "RUNNING", "batch": batch, "approval_mode": "scripted", "started_at": timestamp(), "runs": [], "audits": {}}

    def save() -> None:
        pending = folder / "validation.tmp"
        pending.write_text(redact(json.dumps(record, indent=2), values) + "\n", encoding="utf-8")
        pending.replace(folder / "validation.json")

    print(f"{batch} validation evidence: {folder.relative_to(ROOT)}", flush=True)
    try:
        with variable("FLAGSMITH_NO_INPUT", "1"), (folder / "preflight.log").open("w", encoding="utf-8", buffering=1) as log:
            record["preflight"] = preflight(config, values, log, batch)
            save()
            print(f"PRE-FLIGHT PASS: {record['preflight']['pytest']}; git clean, both ports free, Flagsmith names and keys mapped", flush=True)
            for name in REQUIRED:
                print(f"{name}: OK", flush=True)
            for scenario in BATCHES[batch]:
                for number in (1, 2):
                    if record["runs"]:
                        time.sleep(RUN_INTERVAL_SECONDS)
                    print(f"{scenario.name}.{number}: starting {scenario.case}, scripted approvals", flush=True)
                    row = run_scenario(scenario, number, config, folder, values, provider_only_retry=batch != "baseline")
                    record["runs"].append(row)
                    save()
                    summary = row.get("summary", {})
                    print(f"{scenario.name}.{number}: {row['outcome']} ({summary.get('status', 'no run.json')}); "
                          f"{len(summary.get('attempts', []))} agent attempt(s), {summary.get('elapsed_seconds', 'unknown')} s", flush=True)
                    if row["outcome"] == "PASS":
                        break
                    if number == 1 and row.get("retryable"):
                        print(f"{scenario.name}.{number}: {row['error']}. Retrying this run once.", flush=True)
                        continue
                    raise ValidationError(f"{scenario.name}.{number}: {row['error']}")

        record["audits"].update(write_audits(record["runs"]))
        record["audits"]["b"] = app_environment_audit(config, folder)
        record["audits"]["e"] = read_boundary_audit(config, folder)
        save()
        record["audits"]["a"] = secrets_audit(ROOT, values)
        failed = sorted(name for name, audit in record["audits"].items() if audit["status"] != "PASS")
        require(not failed, f"Safety audit(s) failed: {', '.join(failed)}; see counts in validation.json")
        record["status"] = "PASS"
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError, requests.RequestException, KeyboardInterrupt) as exc:
        record.update(status="STOPPED", error=redact(str(exc) or type(exc).__name__, values))
    finally:
        record["finished_at"] = timestamp()
        save()
    for name, audit in sorted(record["audits"].items()):
        print(f"Audit {name}: {audit['status']}", flush=True)
    if record["status"] == "STOPPED":
        print(record["error"], file=sys.stderr, flush=True)
    print(f"VALIDATION {record['status']}: {folder.relative_to(ROOT) / 'validation.json'}", flush=True)
    return int(record["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
