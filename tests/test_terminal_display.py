"""Display changes cannot alter the exact values that approval tokens bind."""

from copy import deepcopy
from dataclasses import asdict
from io import StringIO
import json
from pathlib import Path

from rewind import approvals
from rewind.report.terminal import RunConsole, captured_screen
from rewind.verifier import VerificationResult


def test_approval_tokens_match_before_and_after_prettifying_the_prompt(monkeypatch):
    ordered = [
        ("feature", "environment_default", "set_on"),
        ("feature", "environment_default", "set_off"),
        ("feature", "segment:tier", "delete_override"),
    ]
    failure = VerificationResult(False, "verify", 1, "Recorded failure", user_outcomes={
        "customer": {"step": "checkout", "http_status": 200, "executed_version": "v2", "effective_flags": {"feature": True}},
    }, failed_assertions=[{"user": "customer", "step": "checkout", "assertion": "executed_version_matches_goal", "detail": "old path expected"}])
    diff = {"operations": ordered, "stages": ["baseline setup", "rollback", "recovery"], "environment": "staging",
            "starting_state": {"flags": {"feature": {"enabled": False, "overrides": {"tier": {"enabled": True}}}}},
            "db_path": "/tmp/staging-fixture.db", "fixture_writes": ["Reset fixture", "Save test cart", "Write test order"],
            "recovery_index": 2, "rehearsal_failure": failure, "reproduction_flags": ["feature"]}
    before = deepcopy(diff)
    snapshot_hash = "0123456789abcdef" * 4
    summary = "Write fixture data\nRollback: ('feature', 'environment_default', 'set_off'); up to 3 proposals."
    monkeypatch.setattr(approvals, "_REGISTRY", {})
    monkeypatch.setattr(approvals, "_utc_now", lambda: "2026-11-27T01:53:11+00:00")
    monkeypatch.setattr(approvals.secrets, "token_hex", lambda _: "display-fixture-nonce")

    def mint(output):
        rehearsal = approvals.gate1_rehearsal(summary, environment="practice", max_operations=11,
                                             input_fn=lambda _: "yes", output_fn=output)
        target = approvals.gate2_target(diff, snapshot_hash, input_fn=lambda _: "yes", output_fn=output)
        return asdict(rehearsal), asdict(target)

    with monkeypatch.context() as old:
        old.setattr(approvals, "rehearsal_screen", lambda plan, *_: plan)
        old.setattr(approvals, "target_screen", lambda ops, digest, details: approvals._format_diff_display(details, ops, digest))
        old_tokens = mint(lambda _: None)
    output = []
    assert mint(output.append) == old_tokens
    assert diff == before and tuple(ordered) == old_tokens[1]["operations"]
    assert old_tokens[1]["snapshot_hash"] == snapshot_hash and old_tokens[1]["recovery_index"] == 2
    assert snapshot_hash in old_tokens[1]["summary"] and snapshot_hash not in output[1]
    assert snapshot_hash[:12] in output[1] and "Recovery runs only if the failure reproduces." in output[1]
    assert "Rollback: feature: set default OFF" in output[0] and "('feature'" not in output[0]
    assert len(output[1].splitlines()) <= 30


def test_captured_target_preview_hides_repr_and_urls_and_keeps_order():
    root = Path(__file__).resolve().parents[1]
    run = json.loads((root / "docs/captured-runs/case-b/run.json").read_text())
    gate = run["approvals"][1]
    before = deepcopy(gate)
    text = captured_screen(gate, run["capture"]["target_before"], width=80)
    assert gate == before
    assert all(part in text for part in ("Step", "baseline setup", "rollback", "recovery", "Starting state", "Remove override"))
    assert text.index("baseline setup") < text.index("rollback") < text.index("recovery")
    assert "https://" not in text and "{'" not in text and "…" not in text and len(text.splitlines()) <= 30


def test_terminal_change_message_keeps_scope_and_status_without_raw_details():
    output = StringIO()
    console = RunConsole(file=output, width=120, force_terminal=False)
    url = "https://" + "provider.invalid/reference"
    console.print(f"Stopped: source had {{'bad': True}}; see {url}", markup=False)
    console.print("  - " + json.dumps({"kind": "flag", "flag": "feature", "scope": "segment:tier",
        "operation": "delete_override", "environment": "staging", "status": "applied"}), markup=False)
    text = output.getvalue()
    assert "Stopped:" in text and "feature: delete override for segment tier · applied" in text
    assert url not in text and "{" not in text
