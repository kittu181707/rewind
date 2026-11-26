"""Offline checks of tool scope, budgets and terminal model replies."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from rewind.agent.tools import AttemptTools, ToolError, run_attempt
from rewind.config import load_config
from rewind.flagsmith_ops import FlagsmithOps


ROOT = Path(__file__).resolve().parents[1]


class ScriptedModel:
    def __init__(self, *replies):
        self._replies = iter(replies)
        self.calls = []
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append((deepcopy(messages), deepcopy(tools)))
        return next(self._replies)


def reply(*calls):
    return {"role": "assistant", "tool_calls": [
        {"type": "function", "id": f"test-{i}", "function": {"name": name, "arguments": json.dumps(args)}}
        for i, (name, args) in enumerate(calls)
    ]}


@pytest.fixture
def tool_set(tmp_path):
    cfg = load_config(ROOT / "examples/demo-shop/rewind.config.yaml")
    cfg.source_paths = ["shop.py", ".env", "journey.py", "verifier.py", "tests/fixture.py", "link.py"]
    (tmp_path / "shop.py").write_text("fixture shop source\n", encoding="utf-8")
    (tmp_path / "link.py").symlink_to(tmp_path / "shop.py")
    ops = FlagsmithOps(cfg, runner=lambda *args: pytest.fail("Model tools must not call Flagsmith CLI"))
    return AttemptTools(cfg, tmp_path, {"flags": {"checkout_v2": False}}, {"reason": "cart read failed"},
                        lambda *args: ops.validate_operation(cfg.flagsmith.rehearsal.environment, *args), ops.redact)


def test_read_only_tools_and_one_terminal_proposal(tool_set):
    model = ScriptedModel(
        reply(("read_source", {"path": "source:1"}), ("read_evidence", {})),
        reply(("propose_operation", {"diagnosis": "Legacy rejects the saved format without its compatibility reader.",
                                       "flag": "cart_compat_reader", "scope": "environment_default", "operation": "set_on"})),
    )
    result = run_attempt(model, tool_set, [])
    assert result.status == "proposed" and result.proposal is not None
    assert result.proposal.operation_tuple == ("cart_compat_reader", "environment_default", "set_on")
    assert {t["function"]["name"] for t in model.requests[0][1]} == {
        "read_source", "read_flag_state", "read_evidence", "propose_operation",
    }
    assert "fixture shop source" in json.dumps(model.requests[1][0])
    assert "fixture shop source" not in json.dumps(result.messages)
    assert "journey.py" not in json.dumps(model.requests[0])


@pytest.mark.parametrize("path", ["../shop.py", ".env", "journey.py", "verifier.py", "tests/fixture.py", "link.py"])
def test_protected_and_non_allowlisted_paths_cannot_be_read(tool_set, path):
    with pytest.raises(ToolError):
        tool_set.read_source(path)


def test_rejects_seventh_read_request(tool_set):
    six = reply(*[("read_flag_state", {})] * 6)
    model = ScriptedModel(six, reply(("read_evidence", {})))
    result = run_attempt(model, tool_set, [])
    assert result.status == "invalid" and "maximum six" in result.reason
    assert len([entry for entry in result.messages if entry["role"] == "tool"]) == 6


def test_proposal_requires_diagnosis_and_applies_allowlist(tool_set):
    fields = {"flag": "cart_compat_reader", "scope": "identity:alice", "operation": "set_on"}
    with pytest.raises(ToolError, match="requires diagnosis"):
        tool_set.propose(fields)
    with pytest.raises(ToolError, match="Only one proposal"):
        tool_set.propose({**fields, "diagnosis": "A masked failure."})


@pytest.mark.parametrize("template", ["{}", " \n {} \t ", "\n```json\n{}\n```\n ", "```\r\n{}\r\n```"])
def test_no_fix_accepts_json_with_whitespace_or_one_fence(tool_set, template):
    content = template.format(json.dumps({"no_fix": "The legacy code has no compatibility path."}))
    model = ScriptedModel({"content": content})
    result = run_attempt(model, tool_set, [])
    assert result.status == "no_fix" and result.proposal is None
    assert result.reason == "The legacy code has no compatibility path."


@pytest.mark.parametrize("text", [
    '```json\n{"no_fix":"a reason","extra":true}\n```',
    '{"no_fix":"a reason","no_fix":"another reason"}',
    '{"no_fix":"   "}', '{"no_fix":false}',
    json.dumps({"no_fix": "a" * 1001}),
    'Some prose\n```json\n{"no_fix":"a reason"}\n```',
])
def test_no_fix_still_requires_exact_shape_and_reason_bound(tool_set, text):
    result = run_attempt(ScriptedModel({"content": text}), tool_set, [])
    assert result.status == "invalid" and result.proposal is None
