"""Offline coverage of the read-only snapshot and the untruncated run summary."""

import json
from pathlib import Path
import subprocess

import pytest

from rewind.config import load_config
from scripts.summarize_run import main, summarize
from scripts.target_hash import read_target
from scripts import validate_day2


CONFIG = Path(__file__).resolve().parents[1] / "examples/demo-shop/rewind.config.yaml"


def test_target_hash_reads_only_target_and_lists_overrides(monkeypatch):
    cfg = load_config(CONFIG)
    mock_key = "snapshot-test-key"
    monkeypatch.setenv(cfg.flagsmith.target.key_env, mock_key)
    reads = []

    class ReadOnlyOps:
        def list_environments(self):
            return [{"name": cfg.flagsmith.target.environment, "api_key": mock_key}]

        def list_segments(self):
            return [{"name": "vip_users", "id": 7, "rules": {}}]

        def get_flag(self, environment, flag):
            reads.append((environment, flag))
            return {"enabled": False, "value": "private fixture value", "segment_overrides": 1, "identity_overrides": 1}

        def get_segment_override(self, environment, flag, segment):
            reads.append((environment, flag))
            return {"enabled": True, "priority": 0}

        def get_identity_override(self, environment, flag, user):
            reads.append((environment, flag))
            return {"enabled": True} if user == "alice" else None

    view = read_target(cfg, ReadOnlyOps())
    assert reads and {environment for environment, _ in reads} == {cfg.flagsmith.target.environment}
    assert len(view["hash"]) == 64
    assert view["flags"]["checkout_v2"] == {"enabled": False, "segment_overrides": ["vip_users"], "identity_overrides": ["alice"]}
    assert mock_key not in json.dumps(view) and "private fixture value" not in json.dumps(view)


def test_run_summary_keeps_complete_diagnoses_and_changes(tmp_path, capsys):
    diagnosis = "The newer cart schema remains saved after the flag changes. " * 8
    run = {
        "status": "verified", "provider": "groq", "model": "fixture-model", "approval_mode": "scripted",
        "target_failure_reproduced": True, "elapsed_seconds": 22.2,
        "attempts": [{"number": 1, "status": "verified", "reason": "All users verified", "proposal": {
            "diagnosis": diagnosis, "flag": "reader", "operation": "set_on", "scope": "environment_default",
        }}],
        "timings": [{"phase": "Target: verify after recovery", "seconds": 5.0}],
        "llm_calls": [{"provider": "groq", "model": "fixture-model", "latency_ms": 3200,
                       "prompt_tokens": 50, "completion_tokens": 4}],
        "flagsmith_command_log": [{}, {}],
        "changes": [{"kind": "flag", "status": "applied", "operation": "set_on"}, {"kind": "fixture_write"}],
    }
    first = tmp_path / "run.json"
    first.write_text(json.dumps(run), encoding="utf-8")
    second = tmp_path / "second.json"
    second.write_text(json.dumps({**run, "attempts": [], "changes": [], "status": "refused"}), encoding="utf-8")
    assert main([str(first), str(second)]) == 0
    text = capsys.readouterr().out
    assert text.count("approval_mode: scripted") == 2 and diagnosis in text
    assert "Target: verify after recovery: 5.0" in text and "prompt/completion tokens: 50/4" in text
    assert '"status": "applied"' in text and '"status": "recorded"' in text
    assert summarize(run)["llm_seconds"] == 3.2


def test_validation_write_trace_guards_scope_without_retaining_keys(monkeypatch):
    cfg = load_config(CONFIG)
    rehearsal_key, target_key = "trace-rehearsal-key", "trace-target-key"
    monkeypatch.setenv(cfg.flagsmith.rehearsal.key_env, rehearsal_key)
    monkeypatch.setenv(cfg.flagsmith.target.key_env, target_key)
    calls = []

    def runner(argv, _env):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    trace = validate_day2.WriteTrace(cfg, {cfg.flagsmith.rehearsal.environment}, runner=runner)
    argv = ["flagsmith", "flag", "update", "checkout_v2", "--disable", "--environment", rehearsal_key, "--yes"]
    trace(argv, {})
    with pytest.raises(validate_day2.ValidationError, match="out-of-scope Flagsmith environment"):
        trace([*argv[:-2], target_key, "--yes"], {})
    with pytest.raises(validate_day2.ValidationError, match="out-of-scope override"):
        trace([*argv[:5], "--identifier", "alice", *argv[5:]], {})
    assert calls == [argv] and len(trace.writes) == 1
    assert trace.writes[0]["environment"] == "rehearsal" and trace.writes[0]["status"] == "applied"
    assert "<REDACTED>" in trace.writes[0]["command"] and rehearsal_key not in json.dumps(trace.writes)


def test_retry_never_follows_a_target_write_or_agent_refusal():
    flaky = {"status": "stopped", "llm_calls": [{"outcome": "rate_limited"}]}
    assert validate_day2.is_flaky({"writes": []}, flaky)
    assert not validate_day2.is_flaky({"writes": [{"environment": "demo-target"}]}, flaky)
    assert not validate_day2.is_flaky({"writes": []}, {**flaky, "status": "refused"})
    assert not validate_day2.is_flaky({"writes": []}, {"status": "stopped", "llm_calls": [{"outcome": "error"}]})
    assert validate_day2.is_flaky({"writes": []}, {"status": "stopped", "error_kind": "provider"})


def test_verified_validation_cases_stop_at_first_rejected_proposal():
    config = load_config(CONFIG)
    assert config.agent
    adapter = validate_day2.FirstProposalModel(config.agent, post=lambda *a, **kw: pytest.fail("Unexpected provider request"))
    messages = [{"role": "system"}, {"role": "user", "content": '{"previous_attempts":[{"status":"failed"}]}'}]
    with pytest.raises(validate_day2.ValidationError, match="first rehearsal proposal failed"):
        adapter.complete(messages, [])


def test_secret_audit_counts_occurrences_without_values_or_urls(tmp_path, monkeypatch):
    marker = "fake-value-only-for-counting"
    url = "https://" + "provider.invalid/oauth/authorize?test=1"
    artifact = tmp_path / ".artifacts" / "run.json"
    artifact.parent.mkdir()
    artifact.write_text(f"{marker} {marker} {url}")
    monkeypatch.setattr(validate_day2, "_historical_contents", lambda root: iter([marker.encode(), b"clean"]))
    audit = validate_day2.secrets_audit(tmp_path, {"SYNTHETIC_KEY": marker, "EMPTY": ""})
    assert audit == {
        "status": "FAIL", "captured_outputs": {"scanned": 1, "value_matches": {"SYNTHETIC_KEY": 2}, "auth_url_matches": 1},
        "git_history": {"scanned": 2, "value_matches": {"SYNTHETIC_KEY": 1}, "auth_url_matches": 0},
    }
    assert marker not in json.dumps(audit) and url not in json.dumps(audit)
