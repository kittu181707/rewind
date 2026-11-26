"""Deterministic 3-user customer journey for demo-shop (prepare & verify)."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import requests
import yaml


DEFAULT_CONFIG_PATH = Path(__file__).with_name("rewind.config.yaml")
DEFAULT_STATE_PATH = Path(__file__).with_name(".journey_state.json")

AFFECTED_ADD_ITEMS: dict[str, list[dict[str, Any]]] = {
    "alice": [
        {"sku": "coffee", "name": "House Coffee", "quantity": 1, "price_cents": 500},
        {"sku": "croissant", "name": "Butter Croissant", "quantity": 1, "price_cents": 400},
    ],
    "carol": [
        {"sku": "tea", "name": "Matcha Tea", "quantity": 2, "price_cents": 400},
        {"sku": "muffin", "name": "Blueberry Muffin", "quantity": 1, "price_cents": 350},
    ],
}


def _load_journey_config(config_path: Path) -> tuple[list[dict[str, Any]], str, str]:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    users = list(raw.get("users", []))
    rollback = raw.get("rollback", {})
    flag_name = str(rollback.get("flag", "checkout_v2"))
    raw_to = rollback.get("to", "off")
    if isinstance(raw_to, bool):
        target_state = "on" if raw_to else "off"
    else:
        target_state = str(raw_to).strip().lower()
    return users, flag_name, target_state


def _canonical_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [
            {
                "sku": str(i["sku"]),
                "qty": int(i["qty"]),
                "price_cents": int(i["price_cents"]),
            }
            for i in items
        ],
        key=lambda x: x["sku"],
    )


def _post(base_url: str, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    try:
        resp = requests.post(f"{base_url.rstrip('/')}{path}", json=payload, timeout=10.0)
    except requests.RequestException as exc:
        return 0, {"detail": str(exc), "executed_version": "unknown", "effective_flags": {}}
    try:
        data = resp.json()
    except Exception:
        data = {"detail": resp.text, "executed_version": "unknown", "effective_flags": {}}
    return resp.status_code, data


def _get(base_url: str, path: str) -> tuple[int, dict[str, Any]]:
    try:
        resp = requests.get(f"{base_url.rstrip('/')}{path}", timeout=10.0)
    except requests.RequestException as exc:
        return 0, {"detail": str(exc), "executed_version": "unknown", "effective_flags": {}}
    try:
        data = resp.json()
    except Exception:
        data = {"detail": resp.text, "executed_version": "unknown", "effective_flags": {}}
    return resp.status_code, data


def _resolve_user_expectations(
    expected_flags: dict[str, Any] | None,
    users: list[dict[str, Any]],
) -> dict[str, dict[str, bool]] | None:
    if expected_flags is None:
        return None
    user_ids = [str(u["id"]) for u in users]
    values = list(expected_flags.values())
    if values and all(isinstance(v, dict) for v in values):
        return {
            uid: {str(k): bool(v) for k, v in dict(expected_flags.get(uid, {})).items()}
            for uid in user_ids
        }
    return {uid: {str(k): bool(v) for k, v in expected_flags.items()} for uid in user_ids}


def poll_eval_three_consecutive(
    base_url: str,
    users: list[dict[str, Any]],
    expected_flags: dict[str, Any] | None,
    timeout_s: float = 30.0,
    interval_s: float = 0.5,
) -> tuple[bool, dict[str, dict[str, Any]], int, str]:
    """Require 3 consecutive matching /eval reads across all users before asserting."""
    per_user_expected = _resolve_user_expectations(expected_flags, users)
    deadline = time.monotonic() + timeout_s
    consecutive = 0
    baseline_snapshot: dict[str, dict[str, bool]] | None = None
    last_eval: dict[str, dict[str, Any]] = {}
    attempts = 0

    while True:
        attempts += 1
        current_eval: dict[str, dict[str, Any]] = {}
        current_flags_only: dict[str, dict[str, bool]] = {}
        all_ok = True
        for user in users:
            uid = user["id"]
            status, body = _post(base_url, "/eval", {"user_id": uid, "traits": user.get("traits")})
            current_eval[uid] = {"http_status": status, **body}
            if status != 200 or not isinstance(body.get("effective_flags"), dict):
                all_ok = False
                continue
            flags = dict(body["effective_flags"])
            if any(not isinstance(value, bool) for value in flags.values()):
                all_ok = False
                continue
            if per_user_expected is not None:
                for k, want in per_user_expected[uid].items():
                    if k not in flags or not isinstance(flags[k], bool) or flags[k] is not bool(want):
                        all_ok = False
            current_flags_only[uid] = flags

        last_eval = current_eval
        if all_ok:
            if per_user_expected is not None:
                consecutive += 1
            else:
                if baseline_snapshot is None or current_flags_only != baseline_snapshot:
                    baseline_snapshot = current_flags_only
                    consecutive = 1
                else:
                    consecutive += 1
        else:
            consecutive = 0

        if consecutive >= 3:
            return True, last_eval, consecutive, f"3 consecutive matching /eval reads after {attempts} round(s)"

        if time.monotonic() >= deadline:
            return False, last_eval, consecutive, (
                f"Timed out after {timeout_s:.1f}s ({attempts} rounds, max consecutive={consecutive})"
            )
        time.sleep(interval_s)


def run_prepare(
    base_url: str,
    users: list[dict[str, Any]],
    rollback_flag: str,
    state_file: Path,
    eval_timeout: float,
) -> dict[str, Any]:
    converged, eval_map, streak, detail = poll_eval_three_consecutive(
        base_url,
        users,
        expected_flags={rollback_flag: True},
        timeout_s=eval_timeout,
    )
    steps: list[dict[str, Any]] = []
    recorded_users: dict[str, Any] = {}
    overall_ok = converged

    for user in users:
        uid = user["id"]
        role = user.get("role", "affected")
        eval_entry = eval_map.get(uid, {})
        eval_flags = eval_entry.get("effective_flags", {})
        steps.append(
            {
                "name": "eval_prepare",
                "user": uid,
                "http_status": int(eval_entry.get("http_status", 500)),
                "executed_version": str(eval_entry.get("executed_version", "unknown")),
                "effective_flags": eval_flags,
                "assertions": [
                    {
                        "name": "eval_converged_3x",
                        "pass": converged and eval_flags.get(rollback_flag) is True,
                        "detail": detail,
                    }
                ],
            }
        )

        if role == "affected":
            adds = AFFECTED_ADD_ITEMS.get(
                uid,
                [{"sku": f"item-{uid}", "name": f"Item for {uid}", "quantity": 2, "price_cents": 750}],
            )
            last_cart_resp: dict[str, Any] = {}
            for idx, item_req in enumerate(adds, start=1):
                status, body = _post(base_url, "/cart/add", {"user_id": uid, **item_req})
                last_cart_resp = body
                eff = body.get("effective_flags", {}) if isinstance(body, dict) else {}
                exec_ver = str(body.get("executed_version", "unknown"))
                step_assertions = [
                    {
                        "name": "http_status_200",
                        "pass": status == 200,
                        "detail": f"status={status}",
                    },
                    {
                        "name": "prepare_executed_v2",
                        "pass": exec_ver == "v2" and eff.get(rollback_flag) is True,
                        "detail": f"executed_version={exec_ver}, effective_flags={eff}",
                    },
                ]
                if not all(a["pass"] for a in step_assertions):
                    overall_ok = False
                steps.append(
                    {
                        "name": f"cart_add_{idx}",
                        "user": uid,
                        "http_status": status,
                        "executed_version": exec_ver,
                        "effective_flags": eff,
                        "assertions": step_assertions,
                    }
                )
            recorded_users[uid] = {
                "role": role,
                "cart_id": last_cart_resp.get("cart_id"),
                "items": _canonical_items(last_cart_resp.get("items", [])),
                "total_items": last_cart_resp.get("total_items"),
                "total_cents": last_cart_resp.get("total_cents"),
                "traits": last_cart_resp.get("traits", user.get("traits", {})),
            }
        else:
            status, cart_body = _get(base_url, f"/cart/{uid}")
            baseline_ok = status == 200 and cart_body.get("schema_version") == "v1"
            if not baseline_ok:
                overall_ok = False
            steps.append(
                {
                    "name": "control_baseline_cart",
                    "user": uid,
                    "http_status": status,
                    "executed_version": str(cart_body.get("executed_version", "none")),
                    "effective_flags": cart_body.get("effective_flags", {}),
                    "assertions": [
                        {
                            "name": "control_v1_baseline_ready",
                            "pass": baseline_ok,
                            "detail": f"status={status}, schema={cart_body.get('schema_version')}",
                        }
                    ],
                }
            )
            recorded_users[uid] = {
                "role": role,
                "cart_id": cart_body.get("cart_id"),
                "items": _canonical_items(cart_body.get("items", [])),
                "total_items": cart_body.get("total_items"),
                "total_cents": cart_body.get("total_cents"),
                "traits": cart_body.get("traits", user.get("traits", {})),
            }

    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"users": recorded_users}, indent=2), encoding="utf-8")
    return {
        "ok": overall_ok,
        "phase": "prepare",
        "eval_streak": streak,
        "steps": steps,
    }


def run_verify(
    base_url: str,
    users: list[dict[str, Any]],
    rollback_flag: str,
    rollback_to: str,
    state_file: Path,
    expected_flags: dict[str, Any] | None,
    eval_timeout: float,
) -> dict[str, Any]:
    if not state_file.exists():
        return {
            "ok": False,
            "phase": "verify",
            "steps": [
                {
                    "name": "load_prepare_state",
                    "user": "system",
                    "http_status": 0,
                    "executed_version": "none",
                    "effective_flags": {},
                    "assertions": [
                        {
                            "name": "state_file_exists",
                            "pass": False,
                            "detail": f"Missing prepare state file: {state_file}",
                        }
                    ],
                }
            ],
        }

    saved = json.loads(state_file.read_text(encoding="utf-8")).get("users", {})
    goal_bool = rollback_to == "on"
    goal_version = "v2" if goal_bool else "legacy"
    raw_expect = expected_flags if expected_flags is not None else {rollback_flag: goal_bool}
    per_user_expect = _resolve_user_expectations(raw_expect, users) or {}

    converged, eval_map, streak, detail = poll_eval_three_consecutive(
        base_url,
        users,
        expected_flags=per_user_expect,
        timeout_s=eval_timeout,
    )

    steps: list[dict[str, Any]] = []
    overall_ok = converged

    for user in users:
        uid = user["id"]
        role = user.get("role", "affected")
        eval_entry = eval_map.get(uid, {})
        eval_flags = eval_entry.get("effective_flags", {})
        eval_ver = str(eval_entry.get("executed_version", "unknown"))
        user_want = per_user_expect.get(uid, {})
        eval_ok = (
            converged
            and int(eval_entry.get("http_status", 500)) == 200
            and isinstance(eval_flags, dict)
            and all(
                k in eval_flags and isinstance(eval_flags[k], bool) and eval_flags[k] is v
                for k, v in user_want.items()
            )
        )
        steps.append(
            {
                "name": "eval_verify",
                "user": uid,
                "http_status": int(eval_entry.get("http_status", 500)),
                "executed_version": eval_ver,
                "effective_flags": eval_flags,
                "assertions": [
                    {
                        "name": "eval_converged_3x",
                        "pass": eval_ok,
                        "detail": detail if eval_ok else f"{detail}; got={eval_flags}",
                    }
                ],
            }
        )

        baseline = saved.get(uid, {})
        status, body = _post(base_url, "/checkout", {"user_id": uid})
        eff = body.get("effective_flags", {}) if isinstance(body, dict) else {}
        exec_ver = str(body.get("executed_version", "unknown"))
        observed_items = _canonical_items(body.get("items", [])) if isinstance(body.get("items"), list) else []

        assertions = [
            {
                "name": "http_status_200",
                "pass": status == 200,
                "detail": f"status={status}, error={body.get('detail', '')}",
            },
            {
                "name": "executed_version_matches_goal",
                "pass": exec_ver == goal_version,
                "detail": f"expected={goal_version}, got={exec_ver}",
            },
            {
                "name": "effective_flags_match_execution",
                "pass": (
                    isinstance(eff, dict)
                    and rollback_flag in eff
                    and isinstance(eff[rollback_flag], bool)
                    and eff[rollback_flag] is (exec_ver == "v2")
                ),
                "detail": f"effective_flags={eff}, executed_version={exec_ver}",
            },
            {
                "name": "same_customer_and_cart_id",
                "pass": status == 200 and body.get("user_id") == uid and body.get("cart_id") == baseline.get("cart_id"),
                "detail": f"expected_cart={baseline.get('cart_id')}, got_cart={body.get('cart_id')}",
            },
            {
                "name": "latest_quantities_survive",
                "pass": (
                    status == 200
                    and observed_items == baseline.get("items")
                    and body.get("total_items") == baseline.get("total_items")
                    and body.get("total_cents") == baseline.get("total_cents")
                ),
                "detail": (
                    f"expected=({baseline.get('total_items')}, {baseline.get('total_cents')}), "
                    f"got=({body.get('total_items')}, {body.get('total_cents')})"
                ),
            },
            {
                "name": "traits_unchanged",
                "pass": status == 200 and body.get("traits") == baseline.get("traits"),
                "detail": f"expected={baseline.get('traits')}, got={body.get('traits')}",
            },
        ]

        for item in assertions:
            if item["name"] == "effective_flags_match_execution":
                flag_matches = (
                    isinstance(eff, dict)
                    and rollback_flag in eff
                    and isinstance(eff[rollback_flag], bool)
                    and (eff[rollback_flag] is (exec_ver == "v2"))
                )
                compat_ok = True
                if status == 200 and exec_ver == "legacy" and body.get("schema_version") == "v2":
                    compat_ok = eff.get("cart_compat_reader") is True
                item["pass"] = bool(flag_matches and compat_ok)

        if role == "control":
            assertions.append(
                {
                    "name": "control_not_harmed",
                    "pass": all(a["pass"] for a in assertions),
                    "detail": f"control user {uid} verified cleanly",
                }
            )

        if not all(a["pass"] for a in assertions):
            overall_ok = False

        steps.append(
            {
                "name": "checkout",
                "user": uid,
                "http_status": status,
                "executed_version": exec_ver,
                "effective_flags": eff,
                "assertions": assertions,
            }
        )

    return {
        "ok": overall_ok,
        "phase": "verify",
        "eval_streak": streak,
        "steps": steps,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic demo-shop customer journey.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--phase", required=True, choices=["prepare", "verify"])
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--expect-json", default=None)
    parser.add_argument("--eval-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    users, rollback_flag, rollback_to = _load_journey_config(Path(args.config))
    state_file = Path(args.state_file)
    expected_flags = None
    if args.expect_json:
        expected_flags = json.loads(args.expect_json)

    if args.phase == "prepare":
        result = run_prepare(args.base_url, users, rollback_flag, state_file, args.eval_timeout)
    else:
        result = run_verify(
            args.base_url,
            users,
            rollback_flag,
            rollback_to,
            state_file,
            expected_flags,
            args.eval_timeout,
        )

    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
