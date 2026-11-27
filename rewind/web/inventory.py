"""Read flag names and states without exposing credentials or flag values."""

from rewind.config import RewindConfig
from rewind.flagsmith_ops import FlagsmithOps
from rewind.report.data import redact_event


def _override_name(row: dict, kind: str) -> str:
    name = row.get(f"{kind}_name") or row.get("identifier") or row.get(kind) or row.get(f"{kind}_id")
    if isinstance(name, dict):
        name = name.get("name") or name.get("identifier") or name.get("id")
    return str(name) if name is not None else "Name not recorded"


def flag_inventory(config: RewindConfig, ops: FlagsmithOps) -> list[dict]:
    flags: dict[str, dict] = {}
    for env in (config.flagsmith.rehearsal.environment, config.flagsmith.target.environment):
        for row in ops.list_flag_states(env):
            name = row.get("feature")
            if not isinstance(name, str) or not name:
                continue
            entry = flags.setdefault(name, {"name": name, "allowed": name in config.allowed.flags, "environments": []})
            overrides = []
            for kind in ("segment", "identity"):
                if row.get(f"{kind}_overrides"):
                    results = ops.list_flag_states(env, feature=name, identities=kind == "identity")
                    overrides.extend({"kind": kind, "name": _override_name(result, kind), "enabled": result.get("enabled")}
                                     for result in results)
            entry["environments"].append({"name": env, "enabled": row.get("enabled"), "overrides": overrides})
    safe = {"flags": [flags[name] for name in sorted(flags)]}
    return redact_event(safe)["flags"]
