"""Exercise the browser gate boundary with the real runner and in-memory Flagsmith."""

from functools import partial
import json

from fastapi.testclient import TestClient
import pytest

from rewind.orchestrator import run_check
from rewind.request import RollbackRequest
from rewind.web import server
from rewind.web.runs import LiveRun
from test_orchestrator import ROOT, TARGET, ScriptedModel, proposal, workflow


ORIGIN = "http://127.0.0.1:8765"
TOKEN = "synthetic-browser-session"


def app_client(root, config, state=None):
    return TestClient(server.create_app(root, ROOT, TOKEN, 8765, config=config, state=state), base_url=ORIGIN)


def open_page(client):
    assert client.get(f"/open/{TOKEN}").status_code == 200


def send(client, path, body):
    return client.post(path, headers={"Origin": ORIGIN}, json=body)


def test_token_host_origin_and_only_the_allowed_request_fields(tmp_path, workflow):
    _, _, _, apps = workflow
    with app_client(tmp_path, apps.config) as client:
        assert client.get("/").status_code == 403
        assert client.get("/styles.css").status_code == 403
        assert client.get("/open/no").status_code == 403
        open_page(client)
        assert client.get("/", headers={"Host": "untrusted.invalid:8765"}).status_code == 403
        assert client.get("/api/events", headers={"Origin": "https://untrusted.invalid"}).status_code == 403
        assert client.post("/api/run", json={"text": "hello"}).status_code == 403
        assert client.post("/api/decision", headers={"Origin": "https://untrusted.invalid"},
                           json={"gate_id": "test", "decision": "approve"}).status_code == 403
        invalid = send(client, "/api/run", {"text": "roll back", "command": "invalid"})
        assert invalid.status_code == 422 and "command" not in invalid.text


def _start_case(tmp_path, workflow, monkeypatch, timeout):
    _, flags, ops, apps = workflow
    state = LiveRun(timeout)
    monkeypatch.setattr(server, "interpret", lambda *args: RollbackRequest("rollback", "checkout_v2", "off", ()))
    model = ScriptedModel([proposal()])
    monkeypatch.setattr(server, "run_check", partial(run_check, ops=ops, apps=apps, model=model))
    state.subscribe()
    return state, flags, ops, apps.config


def pending_id(state, number):
    with state.lock:
        assert state.lock.wait_for(lambda: any(event.type == "gate" and event.data["gate"] == number
                                               for _, event in state.events), timeout=5)
        return next(event.data["id"] for _, event in state.events if event.type == "gate" and event.data["gate"] == number)


def completed(state):
    with state.lock:
        assert state.lock.wait_for(lambda: not state.active, timeout=5)
        return json.loads((state.report.parent / "run.json").read_text())


def test_wrong_gate_and_double_click_rejected(tmp_path, workflow, monkeypatch):
    state, flags, _, config = _start_case(tmp_path, workflow, monkeypatch, 5)
    with app_client(tmp_path, config, state) as client:
        open_page(client)
        assert send(client, "/api/run", {"text": "rollback"}).status_code == 200
        gate1 = pending_id(state, 1)
        assert send(client, "/api/run", {"text": "another request"}).status_code == 409
        assert send(client, "/api/decision", {"gate_id": "unknown", "decision": "approve"}).status_code == 409
        assert flags.writes == []
        assert send(client, "/api/decision", {"gate_id": gate1, "decision": "approve"}).json() == {"accepted": True}
        assert send(client, "/api/decision", {"gate_id": gate1, "decision": "approve"}).json() == {"accepted": False}
        gate2 = pending_id(state, 2)
        assert gate2 != gate1
        assert send(client, "/api/decision", {"gate_id": gate2, "decision": "decline"}).json() == {"accepted": True}
        run = completed(state)
        assert run["approvals"][0]["approved_by"] == "human (ui click)"
        assert run["approval_mode"] == "human" and run["status"] == "approval_denied"
        assert not any(write[0] == TARGET for write in flags.writes)


@pytest.mark.parametrize("disconnect", [False, True])
def test_timeout_or_closed_page_declines_without_target_writes(tmp_path, workflow, monkeypatch, disconnect):
    state, flags, ops, config = _start_case(tmp_path, workflow, monkeypatch, 5 if disconnect else 0.6)
    with app_client(tmp_path, config, state) as client:
        open_page(client)
        assert send(client, "/api/run", {"text": "rollback"}).status_code == 200
        gate1 = pending_id(state, 1)
        assert send(client, "/api/decision", {"gate_id": gate1, "decision": "approve"}).json() == {"accepted": True}
        pending_id(state, 2)
        marks = len(ops.command_log)
        target_before = flags.defaults[TARGET].copy()
        if disconnect:
            state.disconnect()
        run = completed(state)
        assert run["status"] == "approval_denied" and run["approvals"][1]["status"] == "not approved"
        assert not any(record.command.startswith(("flagsmith flag update", "flagsmith flag delete"))
                       for record in ops.command_log[marks:])
        assert flags.defaults[TARGET] == target_before and not any(write[0] == TARGET for write in flags.writes)
        assert run["autonomy_ledger"]["staging_writes"] == 0
