"""Unit tests for the rewind CLI entry points."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from rewind import cli


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def test_cli_seed_rehearsal_uses_setup_token(monkeypatch, capsys) -> None:
    captured = {}

    def fake_seed_case(ops, config, environment: str, case: str) -> dict:
        captured["environment"] = environment
        captured["case"] = case
        return {"environment": environment, "case": case, "hash": "abc123", "state": {}}

    monkeypatch.setenv("FLAGSMITH_ENV_KEY_REHEARSAL", "rehearsal_dummy_sdk_key_12345")
    monkeypatch.setenv("FLAGSMITH_ENV_KEY_TARGET", "target_dummy_sdk_key_67890")
    monkeypatch.setattr("scripts.seed_flagsmith.seed_case", fake_seed_case)

    rc = cli.main(["seed", "--case", "b"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert captured == {"environment": "rehearsal", "case": "b"}
    assert out["case"] == "b"


def test_cli_seed_rejects_target_environment(monkeypatch, capsys) -> None:
    monkeypatch.setenv("FLAGSMITH_ENV_KEY_REHEARSAL", "rehearsal_dummy_sdk_key_12345")
    monkeypatch.setenv("FLAGSMITH_ENV_KEY_TARGET", "target_dummy_sdk_key_67890")
    monkeypatch.setattr(
        "rewind.flagsmith_ops._default_runner",
        lambda args, env: subprocess.CompletedProcess(args, 0, "ok", ""),
    )

    rc = cli.main(["seed", "--case", "a", "--environment", "demo-target"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "cannot mutate target" in err.lower()


def test_cli_check_delegates_and_demo_remains_a_placeholder(tmp_path, monkeypatch, capsys) -> None:
    from rewind.orchestrator import CheckResult

    monkeypatch.chdir(tmp_path)
    calls = []

    def fake_check(config, root):
        calls.append((config.config_path, root))
        recorded = root / "run.json"
        recorded.write_text('{"status":"verified","approval_mode":"scripted"}')
        return CheckResult("verified", recorded)

    monkeypatch.setattr("rewind.orchestrator.run_check", fake_check)
    config = REPO_ROOT / "examples/demo-shop/rewind.config.yaml"
    assert cli.main(["check", "--config", str(config)]) == 0
    assert calls == [(config, tmp_path)]
    assert (tmp_path / "report.html").is_file() and (tmp_path / "report.json").is_file()
    assert "Report:" in capsys.readouterr().out
    assert cli.main(["demo"]) == 0
    assert "not implemented yet" in capsys.readouterr().out
