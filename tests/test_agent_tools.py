"""Offline checks of tool scope, budgets and terminal model replies."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from rewind.agent.tools import AttemptTools, READ_FIRST, ToolError, _source_excerpt, run_attempt
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


READS = (("read_source", {"path": "source:1"}), ("read_evidence", {}), ("read_flag_state", {}))


def proposal_reply():
    return reply(("propose_operation", {"diagnosis": "Legacy rejects the saved format without a compatible reader.",
        "flag": "cart_compat_reader", "scope": "environment_default", "operation": "set_on"}))


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
    model = ScriptedModel(reply(*READS), proposal_reply())
    result = run_attempt(model, tool_set, [])
    assert result.status == "proposed" and result.proposal is not None
    assert result.proposal.operation_tuple == ("cart_compat_reader", "environment_default", "set_on")
    assert {t["function"]["name"] for t in model.requests[0][1]} == {
        "read_source", "read_flag_state", "read_evidence", "propose_operation",
    }
    assert "fixture shop source" in json.dumps(model.requests[1][0])
    assert "fixture shop source" not in json.dumps(result.messages)
    assert "journey.py" not in json.dumps(model.requests[0])
    task = json.loads(model.requests[0][0][1]["content"])
    assert task["rollback_applied"] == "checkout_v2 was set to off"
    assert task["goal"] == load_config(ROOT / "examples/demo-shop/rewind.config.yaml").rollback.goal
    assert task["constraint"] == "A proposal that moves any user away from the goal is wrong."


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
    for name, arguments in READS:
        tool_set.read(name, arguments)
    fields = {"flag": "cart_compat_reader", "scope": "identity:alice", "operation": "set_on"}
    with pytest.raises(ToolError, match="requires diagnosis"):
        tool_set.propose(fields)
    with pytest.raises(ToolError, match="Only one proposal"):
        tool_set.propose({**fields, "diagnosis": "A masked failure."})


def test_premature_proposal_allows_one_correction_and_all_six_reads(tool_set):
    model = ScriptedModel(proposal_reply(), reply(*READS, *READS), proposal_reply())
    result = run_attempt(model, tool_set, [])
    assert result.status == "proposed"
    assert json.loads(model.requests[1][0][-1]["content"]) == {"ok": False, "error": READ_FIRST}
    assert tool_set.reads_remaining == 0
    assert sum(entry.get("error") == READ_FIRST for entry in result.messages) == 1


def test_second_premature_proposal_cannot_pass_with_a_failed_source_read(tool_set):
    model = ScriptedModel(proposal_reply(), reply(("read_source", {"path": ".env"}), *READS[1:]), proposal_reply())
    result = run_attempt(model, tool_set, [])
    assert result.status == "invalid" and result.reason == READ_FIRST
    assert result.proposal is None and len(model.requests) == 3


def test_large_source_prefers_complete_flag_referencing_blocks():
    marker = 'def check(flags):\n    return flags.get("feature")'
    text = "# unrelated source\n" * 340 + marker + "\n# unrelated again\n" * 340
    excerpt = _source_excerpt(Path("shop.py"), text, frozenset({"feature"}))
    assert marker in excerpt and "unrelated source" not in excerpt


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
