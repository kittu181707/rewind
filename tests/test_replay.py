"""Verify offline browser replay export and captured HTML reports without credentials or networking."""

import json
import os
from pathlib import Path
import socket
from unittest.mock import patch

import pytest

from rewind import cli
from rewind.web.replay import CAPTURE_NAMES, export_replay, replay_files


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_export_replay_offline_with_environment_cleared(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("Replay export must not use network, read an approval, or load credentials")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr("builtins.input", forbidden)
    monkeypatch.setattr("dotenv.load_dotenv", forbidden)

    out_dir = tmp_path / "replay-out"
    with patch.dict(os.environ, {}, clear=True):
        assert cli.main(["ui", "--export", str(out_dir)]) == 0

    output = capsys.readouterr().out
    assert "Browser replay:" in output
    assert (out_dir / "index.html").is_file()
    assert (out_dir / "styles.css").is_file()
    assert (out_dir / "app.js").is_file()
    replay_js = (out_dir / "replay-data.js").read_text(encoding="utf-8")
    assert replay_js.startswith("globalThis.rewindReplays = ")
    payload = json.loads(replay_js.removeprefix("globalThis.rewindReplays = ").removesuffix(";\n"))
    assert set(payload) == set(CAPTURE_NAMES)

    for case, folder in CAPTURE_NAMES.items():
        report_html = (out_dir / f"report-{folder}.html").read_text(encoding="utf-8")
        assert "captured run" in report_html
        events = payload[case]
        types = [event["type"] for event in events]
        assert types[0] == "started"
        assert "inventory" in types
        assert "final" == types[-1]
        gates = [event["data"] for event in events if event["type"] == "gate"]
        attempts = [event["data"] for event in events if event["type"] == "attempt"]
        final = events[-1]["data"]
        if case == "refusal":
            assert len(gates) == 1
            assert len(attempts) == 3
            assert final["status"] == "refused"
        else:
            assert len(gates) == 2
            assert any(attempt.get("status") == "verified" for attempt in attempts)
            assert final["status"] == "verified"
            assert any(event["type"] == "progress" and "Target: verify after recovery" in event["data"]["text"]
                       for event in events)


def test_replay_files_matches_export_and_demo_subcommand_removed(tmp_path):
    files = replay_files(REPO_ROOT)
    assert "replay-data.js" in files
    for folder in CAPTURE_NAMES.values():
        assert f"report-{folder}.html" in files
    export_replay(REPO_ROOT, tmp_path)
    for name, content in files.items():
        assert (tmp_path / name).read_text(encoding="utf-8") == content

    with pytest.raises(SystemExit) as exc:
        cli.main(["demo"])
    assert exc.value.code == 2
