"""Verifier for customer journey execution and Flagsmith edge convergence."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from rewind.config import RewindConfig


class VerifierError(RuntimeError):
    """Raised when the verifier cannot invoke or parse a journey run."""


RunnerFn = Callable[[list[str], dict[str, str], float], subprocess.CompletedProcess[str]]
PostFn = Callable[[str, dict[str, Any]], tuple[int, dict[str, Any]]]

EVAL_STEP_NAMES = frozenset({"eval_verify", "eval_prepare", "eval_convergence"})


@dataclass(frozen=True)
class ConvergenceResult:
    """Outcome of polling /eval for 3 consecutive matching reads."""

    converged: bool
    streak: int
    attempts: int
    last_eval: dict[str, dict[str, Any]]
    detail: str


@dataclass(frozen=True)
class VerificationResult:
    """Structured pass/fail report and evidence from a journey verification run."""

    ok: bool
    phase: str
    exit_code: int
    summary: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    failed_assertions: list[dict[str, Any]] = field(default_factory=list)
    user_outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)
    convergence: ConvergenceResult | None = None
    raw_output: str = ""
    stderr: str = ""


def _default_runner(
    cmd: list[str],
    env: dict[str, str],
    timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        shell=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout_s,
        check=False,
    )


def _default_post(url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    resp = requests.post(url, json=payload, timeout=10.0)
    try:
        body = resp.json()
    except Exception:
        body = {"detail": resp.text}
    return resp.status_code, body


def _has_exact_bool_flags(eff: Any, expected_flags: dict[str, bool]) -> bool:
    if not isinstance(eff, dict):
        return False
    for flag_name, want in expected_flags.items():
        if flag_name not in eff:
            return False
        val = eff[flag_name]
        if not isinstance(val, bool) or val is not bool(want):
            return False
    return True


def poll_eval_convergence(
    base_url: str,
    users: list[Any],
    expected_flags: dict[str, bool],
    timeout_s: float = 30.0,
    interval_s: float = 0.5,
    required_streak: int = 3,
    post_fn: PostFn | None = None,
) -> ConvergenceResult:
    """Poll POST /eval for every configured user until required_streak consecutive reads match."""
    if not users:
        raise VerifierError("No users configured for /eval convergence polling")

    poster = post_fn or _default_post
    eval_url = f"{base_url.rstrip('/')}/eval"
    deadline = time.monotonic() + timeout_s
    streak = 0
    attempts = 0
    last_eval: dict[str, dict[str, Any]] = {}

    while True:
        attempts += 1
        round_match = True
        round_results: dict[str, dict[str, Any]] = {}

        for user in users:
            uid = user.id if hasattr(user, "id") else str(user["id"])
            traits = user.traits if hasattr(user, "traits") else user.get("traits", {})
            try:
                status, body = poster(eval_url, {"user_id": uid, "traits": traits})
            except Exception as exc:
                round_match = False
                round_results[uid] = {"http_status": 0, "error": str(exc)}
                continue

            round_results[uid] = {"http_status": status, **(body if isinstance(body, dict) else {})}
            eff = body.get("effective_flags") if isinstance(body, dict) else None
            if status != 200 or not _has_exact_bool_flags(eff, expected_flags):
                round_match = False

        last_eval = round_results
        if round_match:
            streak += 1
        else:
            streak = 0

        if streak >= required_streak:
            return ConvergenceResult(
                converged=True,
                streak=streak,
                attempts=attempts,
                last_eval=last_eval,
                detail=f"{required_streak} consecutive matching /eval reads in {attempts} round(s)",
            )

        if time.monotonic() >= deadline:
            return ConvergenceResult(
                converged=False,
                streak=streak,
                attempts=attempts,
                last_eval=last_eval,
                detail=f"Timed out after {timeout_s:.1f}s ({attempts} rounds, streak={streak})",
            )

        time.sleep(interval_s)


def parse_journey_output(stdout: str) -> dict[str, Any]:
    """Parse the single JSON object printed by the journey command."""
    text = stdout.strip()
    if not text:
        raise VerifierError("Journey command produced empty stdout")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VerifierError(f"Journey command produced invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise VerifierError("Journey JSON root must be an object")
    if "ok" not in parsed or not isinstance(parsed["ok"], bool):
        raise VerifierError("Journey JSON missing boolean 'ok' field")
    steps = parsed.get("steps")
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        raise VerifierError("Journey JSON 'steps' must be a list of objects")
    return parsed


def _build_journey_command(
    config: RewindConfig,
    base_url: str,
    phase: str,
    extra_args: list[str] | None = None,
) -> list[str]:
    rendered = config.app.journey.format(base_url=base_url, phase=phase)
    parts = shlex.split(rendered)
    if not parts:
        raise VerifierError("Rendered app.journey command is empty")
    if parts[0] in {"python", "python3"}:
        parts[0] = sys.executable
    if extra_args:
        parts.extend(extra_args)
    return parts


def verify_journey(
    config: RewindConfig,
    base_url: str,
    phase: str = "verify",
    expected_flags: dict[str, bool] | None = None,
    extra_args: list[str] | None = None,
    timeout_s: float = 90.0,
    eval_timeout_s: float = 30.0,
    eval_interval_s: float = 0.5,
    poll_eval: bool = False,
    runner: RunnerFn | None = None,
    post_fn: PostFn | None = None,
) -> VerificationResult:
    """Optionally wait for /eval convergence, run the journey command, and validate its output."""
    convergence: ConvergenceResult | None = None
    if poll_eval and expected_flags is not None:
        convergence = poll_eval_convergence(
            base_url=base_url,
            users=list(config.users),
            expected_flags=expected_flags,
            timeout_s=eval_timeout_s,
            interval_s=eval_interval_s,
            post_fn=post_fn,
        )

    cmd = _build_journey_command(config, base_url=base_url, phase=phase, extra_args=extra_args)
    env = dict(os.environ)
    run = runner or _default_runner
    try:
        completed = run(cmd, env, timeout_s)
    except subprocess.TimeoutExpired as exc:
        return VerificationResult(
            ok=False,
            phase=phase,
            exit_code=-1,
            summary=f"Journey command timed out after {timeout_s:.1f}s",
            convergence=convergence,
            raw_output=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
        )

    try:
        parsed = parse_journey_output(completed.stdout)
    except VerifierError as exc:
        return VerificationResult(
            ok=False,
            phase=phase,
            exit_code=completed.returncode,
            summary=str(exc),
            convergence=convergence,
            raw_output=completed.stdout,
            stderr=completed.stderr,
        )

    steps = list(parsed["steps"])
    failed_assertions: list[dict[str, Any]] = []
    user_outcomes: dict[str, dict[str, Any]] = {}
    checkout_users: set[str] = set()
    action_users: set[str] = set()

    goal_flag = config.rollback.flag
    goal_bool = config.rollback.to == "on"
    goal_version = "v2" if goal_bool else "legacy"

    for step in steps:
        uid = str(step.get("user", "unknown"))
        eff = step.get("effective_flags")
        exec_ver = step.get("executed_version")
        raw_status = step.get("http_status", 0)
        http_status = raw_status if isinstance(raw_status, int) else 0
        step_name = str(step.get("name", "unknown"))
        is_eval_step = step_name in EVAL_STEP_NAMES

        user_outcomes[uid] = {
            "step": step_name,
            "http_status": http_status,
            "executed_version": exec_ver,
            "effective_flags": eff if isinstance(eff, dict) else {},
        }

        if not is_eval_step:
            action_users.add(uid)
        if step_name == "checkout":
            checkout_users.add(uid)

        # Direct structural checks on verify action/eval steps regardless of journey self-assertions
        if phase == "verify":
            if http_status != 200:
                failed_assertions.append(
                    {
                        "user": uid,
                        "step": step_name,
                        "assertion": "direct_http_status_200",
                        "detail": f"Expected HTTP 200, got {http_status}",
                        "http_status": http_status,
                        "executed_version": exec_ver,
                        "effective_flags": eff if isinstance(eff, dict) else {},
                    }
                )

            if not isinstance(eff, dict) or goal_flag not in eff or not isinstance(eff[goal_flag], bool):
                failed_assertions.append(
                    {
                        "user": uid,
                        "step": step_name,
                        "assertion": "effective_flags_present_in_response",
                        "detail": f"Missing or non-boolean {goal_flag!r} in effective_flags={eff!r}",
                        "http_status": http_status,
                        "executed_version": exec_ver,
                        "effective_flags": eff if isinstance(eff, dict) else {},
                    }
                )
            elif eff[goal_flag] is not goal_bool:
                failed_assertions.append(
                    {
                        "user": uid,
                        "step": step_name,
                        "assertion": "rollback_flag_matches_goal",
                        "detail": f"Expected {goal_flag}={goal_bool}, got {eff[goal_flag]}",
                        "http_status": http_status,
                        "executed_version": exec_ver,
                        "effective_flags": eff,
                    }
                )
            elif not is_eval_step and eff[goal_flag] is not (exec_ver == "v2"):
                failed_assertions.append(
                    {
                        "user": uid,
                        "step": step_name,
                        "assertion": "effective_flags_match_executed_version",
                        "detail": f"{goal_flag}={eff[goal_flag]} conflicts with executed_version={exec_ver!r}",
                        "http_status": http_status,
                        "executed_version": exec_ver,
                        "effective_flags": eff,
                    }
                )

            if not is_eval_step and exec_ver != goal_version:
                failed_assertions.append(
                    {
                        "user": uid,
                        "step": step_name,
                        "assertion": "direct_executed_version_matches_goal",
                        "detail": f"Expected executed_version={goal_version!r}, got {exec_ver!r}",
                        "http_status": http_status,
                        "executed_version": exec_ver,
                        "effective_flags": eff if isinstance(eff, dict) else {},
                    }
                )

        raw_assertions = step.get("assertions")
        if not isinstance(raw_assertions, list) or len(raw_assertions) == 0:
            failed_assertions.append(
                {
                    "user": uid,
                    "step": step_name,
                    "assertion": "non_empty_step_assertions",
                    "detail": f"Step {step_name!r} has missing or empty assertions",
                    "http_status": http_status,
                    "executed_version": exec_ver,
                    "effective_flags": eff if isinstance(eff, dict) else {},
                }
            )
            continue

        for assertion in raw_assertions:
            if not isinstance(assertion, dict) or not isinstance(assertion.get("pass"), bool):
                failed_assertions.append(
                    {
                        "user": uid,
                        "step": step_name,
                        "assertion": "valid_assertion_shape",
                        "detail": f"Malformed assertion entry: {assertion!r}",
                        "http_status": http_status,
                        "executed_version": exec_ver,
                        "effective_flags": eff if isinstance(eff, dict) else {},
                    }
                )
            elif not assertion["pass"]:
                failed_assertions.append(
                    {
                        "user": uid,
                        "step": step_name,
                        "assertion": assertion.get("name", "unnamed"),
                        "detail": assertion.get("detail", ""),
                        "http_status": http_status,
                        "executed_version": exec_ver,
                        "effective_flags": eff if isinstance(eff, dict) else {},
                    }
                )

    configured_users = {u.id for u in config.users}
    seen_users = set(user_outcomes.keys())
    missing_users = sorted(configured_users - seen_users)
    if missing_users:
        failed_assertions.append(
            {
                "user": ",".join(missing_users),
                "step": "coverage",
                "assertion": "all_configured_users_covered",
                "detail": f"Missing user coverage for: {missing_users}",
                "http_status": 0,
                "executed_version": "none",
                "effective_flags": {},
            }
        )

    if phase == "verify":
        missing_checkout = sorted(configured_users - checkout_users)
        if missing_checkout:
            failed_assertions.append(
                {
                    "user": ",".join(missing_checkout),
                    "step": "coverage",
                    "assertion": "all_users_checked_out",
                    "detail": f"Missing checkout step for: {missing_checkout}",
                    "http_status": 0,
                    "executed_version": "none",
                    "effective_flags": {},
                }
            )
    elif phase == "prepare":
        missing_actions = sorted(configured_users - action_users)
        if missing_actions:
            failed_assertions.append(
                {
                    "user": ",".join(missing_actions),
                    "step": "coverage",
                    "assertion": "all_users_prepared",
                    "detail": f"Missing prepare action step for: {missing_actions}",
                    "http_status": 0,
                    "executed_version": "none",
                    "effective_flags": {},
                }
            )

    conv_ok = True if convergence is None else convergence.converged
    overall_ok = (
        completed.returncode == 0
        and parsed["ok"] is True
        and conv_ok
        and len(failed_assertions) == 0
    )

    if overall_ok:
        summary = f"PASS ({phase}): {len(configured_users)} users verified cleanly"
    else:
        first = failed_assertions[0] if failed_assertions else None
        if first:
            summary = (
                f"FAIL ({phase}): {len(failed_assertions)} failed assertion(s); "
                f"first={first['user']}/{first['assertion']} ({first['detail']})"
            )
        elif convergence is not None and not convergence.converged:
            summary = f"FAIL ({phase}): /eval did not converge ({convergence.detail})"
        else:
            summary = f"FAIL ({phase}): exit_code={completed.returncode}, ok={parsed['ok']}"

    return VerificationResult(
        ok=overall_ok,
        phase=phase,
        exit_code=completed.returncode,
        summary=summary,
        steps=steps,
        failed_assertions=failed_assertions,
        user_outcomes=user_outcomes,
        convergence=convergence,
        raw_output=completed.stdout,
        stderr=completed.stderr,
    )
