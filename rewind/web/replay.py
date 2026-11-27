"""Build browser replay assets from the same evidence as the terminal demo."""

from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path

from rewind.demo import CAPTURE_NAMES
from rewind.events import RunEvent, attempt_event, failure_event, final_event, starting_flags
from rewind.report import render_html
from rewind.report.data import describe_write, public_report, redact_event


STATIC = Path(__file__).with_name("static")


def page(mode: str, case: str = "a") -> str:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    if mode == "replay":
        html = html.replace('data-mode="live"', f'data-mode="replay" data-case="{case}"')
        html = html.replace('<script src="app.js"', '<script src="replay-data.js" defer></script>\n  <script src="app.js"')
    return html


def _gate_event(gate: dict, data: dict, start: dict | None = None) -> RunEvent:
    number = gate["gate"]
    summary, _, fixture = (gate.get("summary") or "").partition("Fixture database writes:\n")
    if number == 2:
        summary = "Recorded target approval. Follow the writes in the order shown."
    return RunEvent("gate", {"gate": number, "id": f"replay-{number}", "environment": gate.get("environment"),
                             "summary": summary.strip(), "preview": data["rehearsal_preview"] if number == 2 else {},
                             "starting_flags": starting_flags(start or {}), "snapshot_hash": gate.get("snapshot_hash"),
                             "fixture_writes": [line[4:] for line in fixture.splitlines() if line.startswith("  - ")],
                             "writes": [{"text": describe_write(*op)} for op in gate.get("operations", [])]})


def _replay_inventory(run: dict, data: dict) -> list[dict]:
    allowed = set(data.get("allowed_flags") or [])
    env_order = []
    for key in ("rehearsal", "target"):
        name = (data.get("environments") or {}).get(key)
        if name and name not in env_order:
            env_order.append(name)
    snapshots_by_env: dict[str, dict] = {}
    for snap in run.get("snapshots", []):
        env = snap.get("environment")
        state = snap.get("state") or {}
        if env and env not in snapshots_by_env and "flags" in state:
            snapshots_by_env[env] = state["flags"]
    flags: dict[str, dict] = {}
    for env in env_order or list(snapshots_by_env):
        for name, setting in (snapshots_by_env.get(env) or {}).items():
            entry = flags.setdefault(name, {"name": name, "allowed": name in allowed, "environments": []})
            overrides = [
                {"kind": kind, "name": ov_name, "enabled": ov_entry.get("enabled")}
                for kind, key in (("segment", "overrides"), ("identity", "identity_overrides_detail"))
                for ov_name, ov_entry in (setting.get(key) or {}).items()
            ]
            entry["environments"].append({"name": env, "enabled": setting.get("enabled"), "overrides": overrides})
    return [flags[name] for name in sorted(flags)]


def _elapsed_from_run(started_at: str | None, stamp: str | None) -> float | None:
    if not started_at or not stamp:
        return None
    try:
        start_dt = datetime.fromisoformat(started_at)
        end_dt = datetime.fromisoformat(stamp)
        return round(max(0.0, (end_dt - start_dt).total_seconds()), 1)
    except ValueError:
        return None


def _events(run: dict, data: dict, report: str) -> list[dict]:
    started_at = run.get("started_at")
    envs = data.get("environments") or {}
    events = [RunEvent("started", {
        "rehearsal": envs.get("rehearsal", "rehearsal"),
        "target": envs.get("target", "demo-target"),
        "prompt": (data.get("request") or {}).get("text") or f"Replay {data.get('capture', {}).get('label') or 'captured run'}",
    })]
    inv = _replay_inventory(run, data)
    if inv:
        events.append(RunEvent("inventory", {"flags": inv}))
    if data.get("request"):
        events.append(RunEvent("interpretation", data["request"]))
    events.append(_gate_event(data["approvals"][0], data))
    target = envs.get("target")
    attempts = iter(data["attempts"])
    for event in run["transitions"]:
        if event.get("environment") == target:
            continue
        events.append(RunEvent("progress", {
            "text": event["label"],
            "environment": event.get("environment"),
            "elapsed_s": _elapsed_from_run(started_at, event.get("timestamp")),
        }))
        if event["label"].endswith("verify before recovery") and not event["ok"]:
            events.append(RunEvent("failure", failure_event(event)))
            attempt = next(attempts, None)
            if attempt is not None:
                events.append(RunEvent("attempt", attempt_event(attempt)))
    events.extend(RunEvent("attempt", attempt_event(attempt)) for attempt in attempts)
    if data["approvals"][1].get("status") != "not reached":
        start = next((snapshot["state"] for snapshot in run.get("snapshots", [])
                      if snapshot.get("phase") == "Target starting snapshot"), {})
        events.append(_gate_event(data["approvals"][1], data, start))
    for event in run["transitions"]:
        if event.get("environment") == target:
            events.append(RunEvent("progress", {
                "text": event["label"],
                "environment": event.get("environment"),
                "elapsed_s": _elapsed_from_run(started_at, event.get("timestamp")),
            }))
    events.append(final_event(data, report))
    return [redact_event(asdict(event)) for event in events]


def replay_files(repository: Path) -> dict[str, str]:
    schemes = {}
    files = {}
    for case, folder in CAPTURE_NAMES.items():
        source = repository / "docs/captured-runs" / folder / "run.json"
        run = json.loads(source.read_text(encoding="utf-8"))
        data = public_report(run, captured=True)
        report = f"report-{folder}.html"
        files[report] = render_html(data)
        schemes[case] = _events(run, data, report)
    files["replay-data.js"] = f"globalThis.rewindReplays = {json.dumps(schemes)};\n"
    return files


def export_replay(repository: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    files = {**replay_files(repository), "index.html": page("replay")}
    for asset in ("styles.css", "app.js"):
        files[asset] = (STATIC / asset).read_text(encoding="utf-8")
    for name, text in files.items():
        (output / name).write_text(text, encoding="utf-8")
