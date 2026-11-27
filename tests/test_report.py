"""Evidence report tests use captured data and do not launch a live run."""

import json
from copy import deepcopy
from pathlib import Path
import re

import pytest

from rewind.report import ReportError, write_report
from rewind.report.data import public_report, utc_time


CAPTURES = Path(__file__).resolve().parents[1] / "docs/captured-runs"


@pytest.mark.parametrize("case, verdict", [("case-a", "Verified"), ("refusal", "Refused"), ("case-b", "Verified")])
def test_captured_reports_keep_every_transition_user_and_change(tmp_path, case, verdict):
    source = CAPTURES / case / "run.json"
    run = json.loads(source.read_text())
    output, data = write_report(source, tmp_path)
    html = output.read_text()
    assert json.loads(output.with_suffix(".json").read_text()) == data
    summary = html.split('aria-label="Run summary">', 1)[1].split("</section>", 1)[0]
    assert summary.count("<p>") == 5 and "Autonomy:" in summary
    assert html.index('aria-label="Run summary"') < html.index("<header>")
    assert html.count('class="timeline-event" open') == 1
    assert html.count('class="timeline-event"') == len(run["transitions"])
    headings = ["Attempts", "Approvals", "Changes made", "Target before and after", "Full timeline"]
    assert [html.index(f"<h2>{heading}</h2>") for heading in headings] == sorted(html.index(f"<h2>{heading}</h2>") for heading in headings)
    assert all(re.search(r'aria-label="[^"]+"', attrs) for attrs in re.findall(r'<div class="scroll" ([^>]+)>', html))
    header = html.split("<header>", 1)[1].split("</header>", 1)[0]
    for field in ("Off is not undo.", f"Verdict: {verdict}", "openai/gpt-oss-120b", "Approval mode", "scripted",
                  "Rehearsal", "Target", "Total time", "captured run", "scripted validation run"):
        assert field in header
    assert len(data["transitions"]) == len(run["transitions"])
    for recorded, reported in zip(run["transitions"], data["transitions"]):
        assert recorded["label"] == reported["label"]
        assert len(recorded["steps"]) == len(reported["steps"])
        for original, step in zip(recorded["steps"], reported["steps"]):
            assert all(step[key] == original[key] for key in ("user", "http_status", "executed_version"))
            assert step["effective_flags"] == {flag: value for flag, value in original["effective_flags"].items()
                                               if flag in data["allowed_flags"]}
    assert "show_demo_button" not in html and "show_demo_button" not in json.dumps(data)
    assert len(data["changes"]) == len(run["changes"])
    assert len(data["attempts"]) == len(run["attempts"])
    assert data["target"]["before"] == run["capture"]["target_before"]
    assert data["target"]["after"] == run["capture"]["target_after"]
    assert [gate["gate"] for gate in data["approvals"]] == [1, 2]
    assert "What was not verified" in html and "Failed checks" in html
    assert re.search(r"2026-11-\d\d \d\d:\d\d:\d\d UTC", summary)
    assert "prefers-color-scheme:dark" in html and 'name="viewport"' in html
    assert not re.search(r'\b(?:href|src)\s*=\s*["\']https?://', html)
    assert "<script" not in html and "default-src 'none'" in html
    if case == "refusal":
        assert data["approvals"][1]["status"] == "not reached"
        assert "Unrecognized cart schema" in html
        assert "set override ON for segment vip_users" in html
        assert "No verified fix found: 3 proposals rejected by the verifier. Target unchanged." in summary
        assert "Rejected suggestion:" in summary and "What Rewind found" not in html
        assert html.count("Rejected suggestion:") == 4
        assert data["autonomy_ledger"]["approvals_asked"] == 1 and data["autonomy_ledger"]["staging_writes"] == 0
    if case == "case-b":
        assert "delete override for segment vip_users" in html
        assert "alice and carol still ran the new checkout" in summary
        assert "Remove the override for segment vip_users on checkout_v2 so it inherits the default." in summary
        assert summary.count("ON → OFF (changed)") == 2
    if case == "case-a":
        assert "alice and carol got HTTP 500" in summary
        assert summary.count("OFF → ON (changed)") == 3
    if verdict == "Verified":
        assert all(label in summary for label in ("Problem found in rehearsal", "Diagnosis:", "Verified fix:", "Who sees what"))
        before, after = (event["expected_flags"] for event in run["transitions"][1:3])
        visibility = data["rehearsal_preview"]["visibility"]
        assert len(visibility) == len(before) * len(data["allowed_flags"])
        assert all((row["before"], row["after"]) == (before[row["user"]][row["flag"]], after[row["user"]][row["flag"]])
                   for row in visibility)
        assert data["autonomy_ledger"]["approvals_asked"] == 2 and data["autonomy_ledger"]["staging_writes"] > 0
    assert run["attempts"][-1]["proposal"]["diagnosis"] in summary


def test_display_filter_and_utc_format_do_not_mutate_recorded_approvals():
    run = json.loads((CAPTURES / "case-b/run.json").read_text())
    before = deepcopy(run)
    data = public_report(run, allowed_flags=("checkout_v2",))
    assert run == before
    assert data["approvals"][1]["operations"] == before["approvals"][1]["operations"]
    assert data["approvals"][1]["snapshot_hash"] == before["approvals"][1]["snapshot_hash"]
    assert all(set(step["effective_flags"]) <= {"checkout_v2"} for event in data["transitions"] for step in event["steps"])
    assert {row["flag"] for row in data["rehearsal_preview"]["visibility"]} == {"checkout_v2"}
    assert utc_time("2026-11-27T07:23:11.667+05:30") == "2026-11-27 01:53:11 UTC"


def test_rejected_proposal_is_recovered_from_audit_and_escaped(tmp_path, monkeypatch):
    secret = "synthetic-report-key-for-test"
    monkeypatch.setenv("REPORT_TEST_KEY", secret)
    url = "https://" + "provider.invalid/oauth/authorize"
    arguments = {"flag": "f", "scope": "identity:user", "operation": "set_off",
                 "diagnosis": f"<script>FIX</script> {secret} {url}"}
    source = tmp_path / "run.json"
    source.write_text(json.dumps({"status": "stopped", "approval_mode": "human",
        "attempts": [{"number": 1, "status": "invalid", "reason": "Scope not allowed", "messages": [
            {"role": "assistant", "tool_calls": [{"function": {"name": "propose_operation", "arguments": json.dumps(arguments)}}]}
        ]}]}))
    output, data = write_report(source)
    html = output.read_text()
    assert "REJECTED" in html and "Scope not allowed" in html and "set override OFF for identity user" in html
    assert "Rejected suggestion:" in html and "What Rewind found" not in html
    assert "&lt;script&gt;FIX&lt;/script&gt;" in html and "<script>FIX</script>" not in html
    assert data["attempts"][0]["proposal"]["diagnosis"].endswith("<REDACTED> <REDACTED>")
    assert all(value not in html and value not in output.with_suffix(".json").read_text() for value in (secret, url))


def test_denied_verdict_and_missing_snapshots(tmp_path):
    source = tmp_path / "run.json"
    source.write_text('{"status":"approval_denied","approval_mode":"human"}')
    output, _ = write_report(source)
    assert "Verdict: Denied" in output.read_text() and "No target snapshot recorded" in output.read_text()
    source.write_text('{"status":"running"}')
    with pytest.raises(ReportError, match="completed Rewind verdict"):
        write_report(source)
