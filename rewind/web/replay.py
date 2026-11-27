"""Build browser replay assets from the same evidence as the terminal demo."""

from dataclasses import asdict
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


def _events(run: dict, data: dict, report: str) -> list[dict]:
    events = [RunEvent("started", {})]
    if data.get("request"):
        events.append(RunEvent("interpretation", data["request"]))
    events.append(_gate_event(data["approvals"][0], data))
    target = data.get("environments", {}).get("target")
    attempts = iter(data["attempts"])
    for event in run["transitions"]:
        if event.get("environment") == target:
            continue
        events.append(RunEvent("progress", {"text": event["label"]}))
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
    events.extend(RunEvent("progress", {"text": event["label"]}) for event in run["transitions"] if event.get("environment") == target)
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
