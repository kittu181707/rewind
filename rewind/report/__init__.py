"""Write a self-contained HTML report and its JSON equivalent from run.json."""

from __future__ import annotations

from html import escape
import json
from pathlib import Path

from rewind.report.data import VERDICTS, describe_write, public_report


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
.facts, .pair { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:.65rem 1rem; }
dl { margin:.6rem 0 0; }
dt, th, .muted { color:var(--muted); }
dt { font-size:.73rem; letter-spacing:.07em; text-transform:uppercase; font-weight:700; }
dd { margin:.1rem 0 0; font-weight:600; }
.scroll { overflow-x:auto; }
table { width:100%; border-collapse:collapse; text-align:left; font-size:.87rem; }
th,td { border-bottom:1px solid var(--line); padding:.45rem .5rem; vertical-align:top; }
th { font-size:.78rem; }
td { min-width:3.8rem; }
code,pre { font: .81rem/1.5 ui-monospace,monospace; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; background:var(--bg); padding:.7rem; border-radius:6px; }
.attempt,.gate { border-top:1px solid var(--line); padding:.55rem 0 .35rem; }
details { margin:.55rem 0; }
summary { cursor:pointer; font-weight:650; }
@media (max-width:40rem) { .scroll table { min-width:33rem; } .pair { grid-template-columns:1fr; } }
"""


def _text(value) -> str:
    return escape("—" if value is None else str(value))


def _table(headers: tuple[str, ...], rows: list[tuple], *, empty: str) -> str:
    if not rows:
        return f'<p class="muted">{_text(empty)}</p>'
    head = "".join(f'<th scope="col">{_text(value)}</th>' for value in headers)
    body = "".join("<tr>" + "".join(f"<td>{_text(value)}</td>" for value in row) + "</tr>" for row in rows)
    return f'<div class="scroll" tabindex="0"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _json_block(value) -> str:
    return f"<pre>{_text(json.dumps(value, indent=2, ensure_ascii=False))}</pre>"


def _flag_states(flags: dict) -> str | None:
    return ", ".join(f"{key}: {'ON' if value is True else 'OFF' if value is False else value}"
                     for key, value in sorted(flags.items())) or None


def _timeline(events: list[dict]) -> str:
    pieces = []
    for event in events:
        rows = [(step.get("user"), step.get("name", "").replace("_", " "), step.get("executed_version"),
                 step.get("http_status"), _flag_states(step.get("effective_flags") or {})) for step in event["steps"]]
        pieces.append(f"<h3>{_text(event['label'])}</h3><p class=\"muted\">{_text(event.get('environment'))} · "
                      f"{_text(event.get('timestamp'))}</p><p>{_text(event.get('summary'))}</p>"
                      + _table(("User", "Action", "Version run", "HTTP", "Effective flags"), rows, empty="No user observations recorded."))
    return "".join(pieces) or '<p class="muted">No journey checks recorded.</p>'


def _failures(events: list[dict]) -> str:
    blocks = []
    for event in events:
        rows = [(item.get("user"), item.get("step"), item.get("assertion", "").replace("_", " "), item.get("detail"))
                for item in event["failed_assertions"]]
        if rows:
            blocks.append(f"<details open><summary>{_text(event['label'])}</summary>"
                          + _table(("User", "Action", "Failed check", "Why it failed"), rows, empty="") + "</details>")
    return "".join(blocks) or '<p class="muted">No failed checks recorded.</p>'


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
        details = f"<pre>{_text(gate['summary'])}</pre>" if gate.get("summary") else ""
        ops = "".join(f"<li>{_text(describe_write(*operation))}</li>" for operation in gate.get("operations", []))
        if ops:
            details += f"<p>Approved writes in order:</p><ol>{ops}</ol>"
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
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Rewind · {_text(data['verdict'])} · {_text(data['source'])}</title><style>{_STYLE}</style></head><body><main>
<header><p class="eyebrow">{_text(eyebrow)}</p><h1>Off is not undo.</h1>
<p class="verdict {status_class}">Verdict: {_text(data['verdict'])}</p><dl class="facts">{facts_html}</dl></header>
{error}
<section><h2>What each user saw</h2>{_timeline(data['transitions'])}</section>
<section><h2>What broke</h2>{_failures(data['transitions'])}</section>
<section><h2>What Rewind tried</h2>{_attempt_cards(data['attempts'])}</section>
<section><h2>Who approved the changes</h2>{_approval_cards(data['approvals'])}</section>
<section><h2>Target before and after</h2><p>Original failure reproduced on target: {_text(reproduction)}</p>
<div class="pair"><div><h3>Before</h3>{_json_block(target['before']) if target['before'] else '<p>No target snapshot recorded.</p>'}</div>
<div><h3>After</h3>{_json_block(target['after']) if target['after'] else '<p>No target replay observation recorded.</p>'}</div></div></section>
<section><h2>Every recorded change</h2>{_table(('When', 'Environment', 'Change', 'Status'), _change_rows(data['changes']), empty='No changes recorded.')}</section>
<footer><h2>What was not verified</h2><ul>{limits}</ul></footer>
</main></body></html>
"""


def write_report(run_path: Path, destination: Path | None = None, *, captured: bool = False) -> tuple[Path, dict]:
    try:
        run = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReportError(f"Could not read run.json ({type(exc).__name__})") from None
    if not isinstance(run, dict) or not isinstance(run.get("status"), str) or run["status"] not in VERDICTS:
        raise ReportError("run.json does not contain a completed Rewind verdict")
    try:
        data = public_report(run, captured=captured)
        html = render_html(data)
        folder = destination or run_path.parent
        folder.mkdir(parents=True, exist_ok=True)
        html_path = folder / "report.html"
        html_path.write_text(html, encoding="utf-8")
        (folder / "report.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ReportError(f"Could not build the evidence report ({type(exc).__name__})") from None
    return html_path, data
