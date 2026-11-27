"""Verify that Customer View renders strictly from recorded run steps with no invented data."""

from __future__ import annotations

import json
from pathlib import Path

from rewind.events import failure_event, final_event
from rewind.report.data import customer_view_rows, public_report
from rewind.web.replay import replay_files


REPO_ROOT = Path(__file__).resolve().parent.parent
CAPTURES = REPO_ROOT / "docs" / "captured-runs"


def test_customer_view_uses_only_recorded_fields_and_invents_nothing() -> None:
    custom_tx = {
        "label": "Custom verify",
        "phase": "verify",
        "steps": [
            {
                "name": "checkout",
                "user": "zoe",
                "http_status": 502,
                "executed_version": "legacy",
                "assertions": [
                    {"name": "http_status_200", "pass": False, "detail": "status=502, error=Custom backend crash 502"},
                    {"name": "same_customer_and_cart_id", "pass": False, "detail": "expected_cart=cart-zoe-9, got_cart=cart-zoe-9"},
                    {"name": "latest_quantities_survive", "pass": False, "detail": "expected=(7, 4250), got=(None, None)"},
                    {"name": "traits_unchanged", "pass": False, "detail": "expected={'is_vip': True}, got=None"},
                ],
            },
            {
                "name": "checkout",
                "user": "max",
                "http_status": 200,
                "executed_version": "legacy",
                "assertions": [
                    {"name": "http_status_200", "pass": True, "detail": "status=200, error="},
                    {"name": "same_customer_and_cart_id", "pass": True, "detail": "expected_cart=cart-max-4, got_cart=cart-max-4"},
                    {"name": "latest_quantities_survive", "pass": True, "detail": "expected=(7, 4250), got=(7, 4250)"},
                    {"name": "executed_version_matches_goal", "pass": True, "detail": "legacy"},
                    {"name": "traits_unchanged", "pass": True, "detail": "expected={'is_vip': False}, got={'is_vip': False}"},
                ],
            },
        ],
        "user_outcomes": {
            "zoe": {"http_status": 502, "executed_version": "legacy"},
            "max": {"http_status": 200, "executed_version": "legacy"},
        },
        "failed_assertions": [],
    }

    rows = customer_view_rows(custom_tx)
    assert len(rows) == 2
    assert rows[0]["user"] == "zoe"
    assert rows[0]["http_status"] == 502
    assert rows[0]["error"] == "Custom backend crash 502"
    assert rows[0]["cart_id"] == "cart-zoe-9"
    assert rows[0]["totals"] is None
    assert rows[0]["order_placed"] is False

    assert rows[1]["user"] == "max"
    assert rows[1]["order_placed"] is True
    assert rows[1]["cart_intact"] is True
    assert rows[1]["totals"] == "7 items · $42.50"
    assert rows[1]["is_vip"] is False

    serialized = json.dumps({"failure": failure_event(custom_tx)})
    for forbidden in ("alice", "carol", "bob", "Unrecognized cart schema", "$14.00", "cart-alice"):
        assert forbidden not in serialized


def test_customer_view_minimal_step_omits_unrecorded_details() -> None:
    minimal_tx = {
        "steps": [
            {"name": "checkout", "user": "solo", "http_status": 200, "executed_version": "legacy"}
        ]
    }
    rows = customer_view_rows(minimal_tx)
    assert rows == [
        {
            "user": "solo",
            "http_status": 200,
            "executed_version": "legacy",
            "error": None,
            "cart_id": None,
            "totals": None,
            "cart_intact": None,
            "order_placed": True,
            "version_ok": None,
            "is_vip": None,
        }
    ]


def test_captured_runs_and_replay_export_include_before_and_after_customer_views() -> None:
    case_a = json.loads((CAPTURES / "case-a" / "run.json").read_text(encoding="utf-8"))
    report_data = public_report(case_a, captured=True)
    before = report_data["customer_views"]["before"]["users"]
    after = report_data["customer_views"]["after"]["users"]
    assert [u["user"] for u in before] == ["alice", "carol", "bob"]
    assert before[0]["error"] == "Unrecognized cart schema"
    assert after[0]["order_placed"] is True and after[0]["cart_intact"] is True and after[0]["totals"] == "3 items · $14.00"

    final = final_event(report_data, "report-case-a.html").data
    assert final["customer_views"] == report_data["customer_views"]

    files = replay_files(REPO_ROOT)
    assert "replay-data.js" in files
    assert "Custom backend" not in files["replay-data.js"]
    assert '"customers"' in files["replay-data.js"] and '"customer_views"' in files["replay-data.js"]


def test_static_app_js_contains_no_hardcoded_customer_data() -> None:
    app_js = (REPO_ROOT / "rewind" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    for literal in ("Unrecognized cart schema", "cart-alice", "cart-carol", "cart-bob", "$14.00", "alice", "carol", "bob"):
        assert literal not in app_js
