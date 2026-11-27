"""Offline regression tests for scripts/verify_e2e.py evidence retention and DB write enumeration."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from rewind.approvals import gate1_rehearsal
from rewind.config import load_config
from rewind.flagsmith_ops import FlagsmithOps


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_e2e_module():
    spec = importlib.util.spec_from_file_location(
        "verify_e2e",
        REPO_ROOT / "scripts" / "verify_e2e.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_inspect_db_writes_enumerates_carts_and_orders(tmp_path: Path) -> None:
    mod = _load_e2e_module()
    db = tmp_path / "shop.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE carts (cart_id TEXT PRIMARY KEY, user_id TEXT, schema_version TEXT, payload_json TEXT, status TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE orders (order_id TEXT PRIMARY KEY, cart_id TEXT, user_id TEXT, executed_version TEXT, total_items INTEGER, total_cents INTEGER, items_json TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO carts VALUES ('cart-alice', 'alice', 'v2', '{}', 'open', '2026-11-26T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO orders VALUES ('ord-1234', 'cart-alice', 'alice', 'legacy', 3, 1400, '[]', '2026-11-26T00:01:00Z')"
    )
    conn.commit()
    conn.close()

    entries = mod._inspect_db_writes(db, "Case A")
    assert any("SQLite cart row -> cart-alice" in e and "schema=v2" in e for e in entries)
    assert any("SQLite order insert -> ord-1234" in e and "executed=legacy" in e for e in entries)


def test_flush_evidence_retains_redacted_logs_and_rows(tmp_path: Path, monkeypatch) -> None:
    mod = _load_e2e_module()
    monkeypatch.setenv("FLAGSMITH_ENV_KEY_REHEARSAL", "rehearsal_dummy_sdk_key_12345")
    monkeypatch.setenv("FLAGSMITH_ENV_KEY_TARGET", "target_dummy_sdk_key_67890")
    cfg = load_config(REPO_ROOT / "examples" / "demo-shop" / "rewind.config.yaml")

    def local_runner(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        if args[1:3] == ["segment", "list"]:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps([{"id": 1, "name": "vip_users", "api_token": "synthetic_returned_cred_555666", "rules": {}}]),
                "",
            )
        return subprocess.CompletedProcess(args, 0, "ok", "")

    ops = FlagsmithOps(cfg, cli_path="/usr/local/bin/flagsmith", runner=local_runner)
    ops.list_segments()
    tok = gate1_rehearsal(
        "compat on", environment="rehearsal", max_operations=1, auto_approve=True, output_fn=lambda _: None,
    )
    ops.apply_operation("rehearsal", "cart_compat_reader", "environment_default", "set_on", tok)

    out_dir = tmp_path / "e2e_out"
    row = mod.CaseRow("Case A", "compat environment_default set_on", "PASS", "PASS", "HTTP 200 / legacy", "HTTP 200 / legacy", "HTTP 200 / legacy", True)
    summary_path = mod._flush_evidence(
        out_dir,
        [row],
        ["[demo-target] apply: cart_compat_reader environment_default set_on"],
        ["[Case A] SQLite order insert -> ord-1"],
        [("ops", ops)],
        status="PASS",
    )

    text = summary_path.read_text(encoding="utf-8")
    assert "synthetic_returned_cred_555666" not in text
    assert "rehearsal_dummy_sdk_key_12345" not in text
    data = json.loads(text)
    assert data["status"] == "PASS"
    assert len(data["command_logs"]["ops"]) == 2
