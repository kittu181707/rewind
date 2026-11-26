"""Offline coverage of the read-only snapshot and the untruncated run summary."""

import json
from pathlib import Path

from rewind.config import load_config
from scripts.summarize_run import main, summarize
from scripts.target_hash import read_target


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
