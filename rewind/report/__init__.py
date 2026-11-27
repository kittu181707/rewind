"""Write a self-contained HTML report and its JSON equivalent from run.json."""

from __future__ import annotations

from collections.abc import Iterable
from html import escape
import json
from pathlib import Path

from rewind.report.data import VERDICTS, describe_write, public_report, rollback_failure


class ReportError(ValueError):
    """The recorded run cannot be turned into a report."""


_STYLE = """
:root { color-scheme:light dark; --bg:#f4f5f9; --panel:#fff; --text:#1c2434; --muted:#4c5970;
  --line:#d8deea; --accent:#1548b2; --pass:#075e38; --alert:#92421d; }
@media (prefers-color-scheme:dark) { :root { --bg:#121722; --panel:#1a2231; --text:#eff3fb;
  --muted:#b7c2d7; --line:#37445c; --accent:#aac2ff; --pass:#7edbad; --alert:#ffc496; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:16px/1.5 system-ui,sans-serif; }
main { max-width:68rem; margin:auto; padding:clamp(1rem,3vw,2rem); }
header, section, footer { background:var(--panel); border:1px solid var(--line); border-radius:12px;
  padding:clamp(1rem,3vw,1.7rem); margin-bottom:1.3rem; overflow-wrap:anywhere; }
.eyebrow { font-size:.76rem; font-weight:750; letter-spacing:.12em; text-transform:uppercase; color:var(--accent); margin:0; }
h1 { font-size:clamp(2rem,6vw,3.4rem); line-height:1.08; margin:.35rem 0 .6rem; }
h2 { font-size:1.35rem; margin:0 0 .65rem; }
h3 { margin:.8rem 0 .3rem; font-size:1rem; }
p { margin:.35rem 0 .65rem; }
.verdict { font-size:1.15rem; font-weight:750; color:var(--alert); }
.verdict.verified { color:var(--pass); }
.summary-card { border-left:4px solid var(--accent); }
.summary-card p { margin:.25rem 0 .5rem; }
.facts, .pair { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:.65rem 1rem; }
dl { margin:.6rem 0 0; }
dt, th, .muted { color:var(--muted); }
dt { font-size:.73rem; letter-spacing:.07em; text-transform:uppercase; font-weight:700; }
dd { margin:.1rem 0 0; font-weight:600; }
.scroll { max-width:100%; overflow-x:auto; }
table { width:100%; border-collapse:collapse; text-align:left; font-size:.87rem; }
th,td { border-bottom:1px solid var(--line); padding:.45rem .5rem; vertical-align:top; }
th { font-size:.78rem; }
td { min-width:3.8rem; }
code,pre { font: .81rem/1.5 ui-monospace,monospace; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; background:var(--bg); padding:.7rem; border-radius:6px; }
.attempt,.gate { border-top:1px solid var(--line); padding:.55rem 0 .35rem; }
details { margin:.55rem 0; }
summary { cursor:pointer; font-weight:650; }
@media (max-width:40rem) {
  main { padding:.65rem; }
  header, section, footer { padding:.8rem; margin-bottom:.85rem; }
  .summary-card p { font-size:.875rem; line-height:1.4; margin:.15rem 0 .4rem; }
  .eyebrow { font-size:.65rem; }
  .verdict { font-size:1rem; }
  .facts { gap:.35rem .65rem; font-size:.83rem; line-height:1.35; }
  dt { font-size:.66rem; }
  .scroll table { min-width:33rem; }
  .pair { grid-template-columns:minmax(0,1fr); }
}
"""


def _text(value) -> str:
    return escape("—" if value is None else str(value))


def _table(headers: tuple[str, ...], rows: list[tuple], *, label: str, empty: str = "No entries recorded.") -> str:
    if not rows:
        return f'<p class="muted">{_text(empty)}</p>'
    head = "".join(f'<th scope="col">{_text(value)}</th>' for value in headers)
    body = "".join("<tr>" + "".join(f"<td>{_text(value)}</td>" for value in row) + "</tr>" for row in rows)
    return (f'<div class="scroll" role="region" aria-label="{_text(label)}" tabindex="0">'
            f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')


def _flag_states(flags: dict) -> str | None:
    return ", ".join(f"{key}: {'ON' if value is True else 'OFF' if value is False else value}"
                     for key, value in sorted(flags.items())) or None


def _timeline(events: list[dict]) -> str:
    pieces = []
    failing = rollback_failure(events)
    for event in events:
        label = event["label"]
        rows = [(step.get("user"), step.get("name", "").replace("_", " "), step.get("executed_version"),
                 step.get("http_status"), _flag_states(step.get("effective_flags") or {})) for step in event["steps"]]
        failures = [(item.get("user"), item.get("step"), item.get("assertion", "").replace("_", " "), item.get("detail"))
                for item in event["failed_assertions"]]
        opened = " open" if event is failing else ""
        pieces.append(f'<details class="timeline-event"{opened}><summary>{_text(label)}</summary>'
                      f"<p class=\"muted\">{_text(event.get('environment'))} · {_text(event.get('timestamp'))}</p>"
                      f"<p>{_text(event.get('summary'))}</p>"
                      + _table(("User", "Action", "Version run", "HTTP", "Effective flags"), rows,
                               label=f"{label} user observations", empty="No user observations recorded.")
                      + ("<h3>Failed checks</h3>" + _table(("User", "Action", "Failed check", "Why it failed"),
                         failures, label=f"{label} failed checks") if failures else "") + "</details>")
    return "".join(pieces) or '<p class="muted">No journey checks recorded.</p>'


def _attempt_cards(attempts: list[dict]) -> str:
    titles = {"invalid": "REJECTED", "failed": "Failed verification", "verified": "Verified in rehearsal",
              "proposed": "Proposed; result unavailable", "no_fix": "No fix reported"}
    cards = []
    for attempt in attempts:
        proposal = attempt.get("proposal") or {}
        title = titles.get(attempt.get("status"), attempt.get("status"))
        proposed = describe_write(*(str(proposal.get(key, "—")) for key in ("flag", "scope", "operation"))) if proposal else "No change proposed"
        reason = attempt.get("reason")
        diagnosis = proposal.get("diagnosis") or (reason if attempt.get("status") == "no_fix" else None)
        result = attempt["verification"].get("summary")
        cards.append(f'<article class="attempt"><h3>Attempt {_text(attempt["number"])} · {_text(title)}</h3>'
                     f"<p><strong>Diagnosis:</strong> {_text(diagnosis)}</p><p><strong>Proposed change:</strong> {_text(proposed)}</p>"
                     f"<p><strong>Result:</strong> {_text(reason)}</p>"
                     + (f"<p><strong>Verifier:</strong> {_text(result)}</p>" if result and result != reason else "") + "</article>")
    return "".join(cards) or '<p class="muted">The agent did not submit an attempt.</p>'


def _approval_cards(approvals: list[dict]) -> str:
    cards = []
    for gate in approvals:
        label = "Rehearsal permission" if gate["gate"] == 1 else "Target permission"
        decision = gate.get("status", "approved")
        details = ""
        ops = "".join(f"<li>{_text(describe_write(*operation))}</li>" for operation in gate.get("operations", []))
        if ops:
            details += f"<p>Approved writes in order:</p><ol>{ops}</ol>"
        if gate.get("summary"):
            details += f"<details><summary>Recorded approval details</summary><pre>{_text(gate['summary'])}</pre></details>"
        cards.append(f'<article class="gate"><h3>Gate {_text(gate["gate"])} · {label}</h3>'
                     f"<p>{_text(decision)} · {_text(gate.get('approval_mode'))} · {_text(gate.get('approved_by'))}</p>"
                     f"<p>When: {_text(gate.get('issued_at', gate.get('timestamp')))} · Environment: {_text(gate.get('environment'))}</p>"
                     f"{details}</article>")
    return "".join(cards)


def _change_rows(changes: list[dict]) -> list[tuple]:
    rows = []
    for change in changes:
        if change.get("kind") == "flag":
            what = describe_write(change["flag"], change["scope"], change["operation"])
        else:
            what = str(change.get("kind", "change")).replace("_", " ")
            if change.get("step"):
                what += f" · {change['step']} for {change.get('user', 'unknown user')}"
            if change.get("db_path"):
                what += f" · {change['db_path']}"
            if change.get("details"):
                what += " · " + json.dumps(change["details"], ensure_ascii=False)
        rows.append((change.get("timestamp"), change.get("environment"), what, change.get("status")))
    return rows


def _target_state(snapshot: dict | None, label: str, missing: str) -> str:
    if not snapshot:
        return f"<p>{_text(missing)}</p>"
    if "user_outcomes" in snapshot:
        rows = [(user, step.get("executed_version"), step.get("http_status"), _flag_states(step.get("effective_flags") or {}))
                for user, step in snapshot["user_outcomes"].items()]
        return f"<p>{_text(snapshot.get('observation'))}</p>" + _table(
            ("User", "Version run", "HTTP", "Effective flags"), rows, label=label)
    flags = snapshot.get("flags") or snapshot.get("state", {}).get("flags", {})
    rows = []
    for flag, value in flags.items():
        names = []
        for kind, overrides in (("segment", value.get("overrides", value.get("segment_overrides"))),
                                ("identity", value.get("identity_overrides_detail", value.get("identity_overrides")))):
            if isinstance(overrides, dict):
                names.extend(f"{kind} {name}: {'ON' if entry.get('enabled') else 'OFF'}" for name, entry in overrides.items())
            elif isinstance(overrides, list):
                names.extend(f"{kind} {name}" for name in overrides)
        rows.append((flag, "ON" if value.get("enabled") else "OFF", ", ".join(names) or "None"))
    return f"<p>Snapshot: <code>{_text(snapshot.get('hash'))}</code></p>" + _table(
        ("Flag", "Default", "Overrides"), rows, label=label)


def render_html(data: dict) -> str:
    environments = data.get("environments", {})
    elapsed = data.get("elapsed_seconds")
    facts = (
        ("Model", "/".join(str(data[key]) for key in ("provider", "model") if data.get(key)) or "Not recorded"),
        ("Approval mode", data.get("approval_mode", "Not recorded")),
        ("Rehearsal", environments.get("rehearsal")), ("Target", environments.get("target")),
        ("Total time", f"{elapsed:.3f} s" if isinstance(elapsed, (int, float)) else None),
        ("Evidence source", data["source"]),
    )
    facts_html = "".join(f"<div><dt>{_text(key)}</dt><dd>{_text(value)}</dd></div>" for key, value in facts)
    target = data["target"]
    reproduction = {True: "Yes", False: "No", None: "Not recorded"}[target["failure_reproduced"]]
    limits = "".join(f"<li>{_text(item)}</li>" for item in data["limits"])
    error = f'<p class="verdict">{_text(data["error"])}</p>' if data.get("error") else ""
    capture_label = data["capture"].get("label")
    eyebrow = f"REWIND / {data['source']}" + (f" / {capture_label}" if capture_label else "")
    status_class = "verified" if data["status"] == "verified" else ""
    summary = "".join(f"<p><strong>{_text(label)}:</strong> {_text(line)}</p>" for label, line in zip(
        ("After the rollback", "What Rewind found", "What changed", "Who approved"), data["summary_lines"]))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Rewind · {_text(data['verdict'])} · {_text(data['source'])}</title><style>{_STYLE}</style></head><body><main>
<section class="summary-card" aria-label="Run summary">{summary}</section>
<header><p class="eyebrow">{_text(eyebrow)}</p><h1>Off is not undo.</h1>
<p class="verdict {status_class}">Verdict: {_text(data['verdict'])}</p><dl class="facts">{facts_html}</dl></header>
{error}
<section><h2>Attempts</h2>{_attempt_cards(data['attempts'])}</section>
<section><h2>Approvals</h2>{_approval_cards(data['approvals'])}</section>
<section><h2>Changes made</h2>{_table(('When', 'Environment', 'Change', 'Status'), _change_rows(data['changes']), label='Recorded changes', empty='No changes recorded.')}</section>
<section><h2>Target before and after</h2><p>Original failure reproduced on target: {_text(reproduction)}</p>
<div class="pair"><div><h3>Before</h3>{_target_state(target['before'], 'Target before the replay', 'No target snapshot recorded.')}</div>
<div><h3>After</h3>{_target_state(target['after'], 'Target after the replay', 'No target replay observation recorded.')}</div></div></section>
<section><h2>Full timeline</h2>{_timeline(data['transitions'])}</section>
<footer><h2>What was not verified</h2><ul>{limits}</ul></footer>
</main></body></html>
"""


def write_report(
    run_path: Path, destination: Path | None = None, *, captured: bool = False, allowed_flags: Iterable[str] | None = None,
) -> tuple[Path, dict]:
    try:
        run = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReportError(f"Could not read run.json ({type(exc).__name__})") from None
    if not isinstance(run, dict) or not isinstance(run.get("status"), str) or run["status"] not in VERDICTS:
        raise ReportError("run.json does not contain a completed Rewind verdict")
    try:
        data = public_report(run, captured=captured, allowed_flags=allowed_flags)
        html = render_html(data)
        folder = destination or run_path.parent
        folder.mkdir(parents=True, exist_ok=True)
        html_path = folder / "report.html"
        html_path.write_text(html, encoding="utf-8")
        (folder / "report.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ReportError(f"Could not build the evidence report ({type(exc).__name__})") from None
    return html_path, data
