"""Captures replay through the real renderer without credentials or networking."""

import json
import os
import socket
from unittest.mock import patch

import pytest

from rewind import cli, demo


@pytest.mark.parametrize("args, case, status", [
    (["demo"], "case-a", "verified"),
    (["demo", "--case", "b"], "case-b", "verified"),
    (["demo", "--case", "refusal"], "refusal", "refused"),
])
def test_demo_offline_with_environment_cleared(tmp_path, monkeypatch, capsys, args, case, status):
    monkeypatch.chdir(tmp_path)
    waits = []
    monkeypatch.setattr(demo.time, "sleep", waits.append)

    def forbidden(*args, **kwargs):
        pytest.fail("Replay must not use network, read an approval, or load credentials")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr("builtins.input", forbidden)
    monkeypatch.setattr("dotenv.load_dotenv", forbidden)
    with patch.dict(os.environ, {}, clear=True):
        assert cli.main(args) == 0
    assert 8 <= sum(waits) <= 11
    output = capsys.readouterr().out
    assert "captured run | Report:" in output
    assert "Gate 1 — captured, scripted approval" in output
    assert "verify before recovery" in output
    assert "steps on its own" in output
    if status == "verified":
        assert "Diagnosis:" in output and "Rejected suggestion:" not in output
        assert "Gate 2 — captured, scripted approval" in output
        assert "Problem found in rehearsal:" in output and "Verified fix:" in output and "Who sees what" in output
        assert "Target: verify after recovery" in output
    else:
        assert output.count("Rejected suggestion:") == 3 and "Diagnosis:" not in output
        assert "Gate 2 — not reached in this captured run" in output
        assert "No verified configuration-only recovery found. Target unchanged." in output
    report = next((tmp_path / ".artifacts").glob("demo-*/report.html"))
    assert "captured run" in report.read_text()
    data = json.loads(report.with_suffix(".json").read_text())
    assert data["status"] == status and data["source"] == "captured run"
    assert data["capture"]["case"] == case
    assert f"captured run | Report: {report.relative_to(tmp_path)}" in output
    assert str(report) not in output and "https://" not in output
