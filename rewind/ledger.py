"""Count completed work and approval prompts from recorded run evidence."""

_MANAGEMENT_READS = {("environment", "list"), ("feature", "list"), ("flag", "get"), ("segment", "list")}
_AGENT_READS = {"read_evidence", "read_flag_state", "read_source"}


def autonomy_ledger(run: dict) -> dict[str, int]:
    reads = sum(record.get("exit_code") == 0 and tuple(record.get("command", "").split()[1:3]) in _MANAGEMENT_READS
                for record in run.get("flagsmith_command_log", []))
    reads += sum(message.get("role") == "tool" and message.get("name") in _AGENT_READS and message.get("ok") is True
                 for attempt in run.get("attempts", []) for message in attempt.get("messages", []))
    writes = {"rehearsal": 0, "target": 0}
    environments = {name: side for side, name in run.get("environments", {}).items() if name and side in writes}
    for change in run.get("changes", []):
        status = change.get("status")
        side = environments.get(change.get("environment"))
        if side and (change.get("kind") == "fixture_write" or status in {"completed", "applied", "applied; follow-up failed"}):
            writes[side] += 1
    approvals = run.get("approval_requests")
    if approvals is None:
        approvals = [gate for gate in run.get("approvals", []) if gate.get("status") != "not reached"]
    counts = {"reads": reads, "rehearsal_writes": writes["rehearsal"], "verifications": len(run.get("transitions", [])),
              "model_calls": len(run.get("llm_calls", [])) + len(run.get("request_llm_calls", []))}
    return {**counts, "steps_alone": sum(counts.values()), "approvals_asked": len(approvals), "staging_writes": writes["target"]}


def ledger_summary(ledger: dict[str, int]) -> str:
    steps, asked = ledger["steps_alone"], ledger["approvals_asked"]
    unit = "step" if steps == 1 else "steps"
    frequency = {0: "asked for no approvals", 1: "stopped to ask you once", 2: "stopped to ask you twice"}
    return f"Rewind did {steps} {unit} on its own and {frequency.get(asked, f'stopped to ask you {asked} times')}."


def ledger_breakdown(ledger: dict[str, int]) -> str:
    labels = {"reads": ("read", "reads"), "rehearsal_writes": ("rehearsal write", "rehearsal writes"),
              "verifications": ("verification", "verifications"), "model_calls": ("model call", "model calls"),
              "staging_writes": ("staging write", "staging writes")}
    return ", ".join(f"{ledger[key]} {singular if ledger[key] == 1 else plural}"
                     for key, (singular, plural) in labels.items()) + "."
