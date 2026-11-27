"""Evidence report tests use captured data and do not launch a live run."""

import json
from pathlib import Path
import re

import pytest

from rewind.report import ReportError, write_report


CAPTURES = Path(__file__).resolve().parents[1] / "docs/captured-runs"


@pytest.mark.parametrize("case, verdict", [("case-a", "Verified"), ("refusal", "Refused"), ("case-b", "Verified")])
def test_captured_reports_keep_every_transition_user_and_change(tmp_path, case, verdict):
    source = CAPTURES / case / "run.json"
    run = json.loads(source.read_text())
    output, data = write_report(source, tmp_path)
    html = output.read_text()
    assert json.loads(output.with_suffix(".json").read_text()) == data
    header = html.split("<header>", 1)[1].split("</header>", 1)[0]
    for field in ("Off is not undo.", f"Verdict: {verdict}", "openai/gpt-oss-120b", "Approval mode", "scripted",
                  "Rehearsal", "Target", "Total time", "captured run", "scripted validation run"):
        assert field in header
    assert len(data["transitions"]) == len(run["transitions"])
    for recorded, reported in zip(run["transitions"], data["transitions"]):
        assert recorded["label"] == reported["label"]
        assert len(recorded["steps"]) == len(reported["steps"])
        for original, step in zip(recorded["steps"], reported["steps"]):
            assert all(step[key] == original[key] for key in ("user", "http_status", "executed_version", "effective_flags"))
    assert len(data["changes"]) == len(run["changes"])
    assert len(data["attempts"]) == len(run["attempts"])
    assert data["target"]["before"] == run["capture"]["target_before"]
    assert data["target"]["after"] == run["capture"]["target_after"]
    assert [gate["gate"] for gate in data["approvals"]] == [1, 2]
    assert "What was not verified" in html and "What broke" in html
    assert "prefers-color-scheme:dark" in html and 'name="viewport"' in html
    assert not re.search(r'\b(?:href|src)\s*=\s*["\']https?://', html)
    assert "<script" not in html and "default-src 'none'" in html
    if case == "refusal":
        assert data["approvals"][1]["status"] == "not reached"
        assert "Unrecognized cart schema" in html
        assert "set override ON for segment vip_users" in html
    if case == "case-b":
        assert "delete override for segment vip_users" in html


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
