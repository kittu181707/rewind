# Rewind — Day 1 Notes

Date: November 26, 2026
Repo: `git@github.com:kittu181707/rewind.git` (WSL path `~/rewind`, branch `main`)

## 1. What Works Today (Steps 1–10)
- **Git & Environment:** SSH push configured with GitHub noreply email; `.env` stores `GROQ_API_KEY`, `GEMINI_API_KEY`, `FLAGSMITH_ENV_KEY_REHEARSAL`, and `FLAGSMITH_ENV_KEY_TARGET` safely outside git.
- **Package Skeleton & Config (`rewind/config.py`):** Loads and validates `rewind.config.yaml`, rejects inline/secret-like literals, enforces uppercase `key_env` names, checks distinct rehearsal/target environments and ports, restricts `source_paths` inside repo root, and normalizes YAML 1.1 booleans (`off`/`on`).
- **Allowlisted Ops & Snapshot (`rewind/flagsmith_ops.py`, `rewind/approvals.py`, `rewind/snapshot.py`):** Executes the `/usr/local/bin/flagsmith` CLI strictly via argument lists (`shell=False`), requires an `ApprovalToken` for mutations, enforces `allowed` flags/scopes/operations/environments, redacts secret tokens/keys in `command_log`, and captures SHA-256 environment state snapshots.
- **Idempotent Seeding (`scripts/seed_flagsmith.py`):** Verified `flagsmith segment create <throwaway> --rules @file` and `delete`. Seeds Case A, Case B, and Refusal cleanly twice in a row with identical snapshot hashes.
- **Demo Shop (`examples/demo-shop/app.py`, `reset.py`, `journey.py`, `index.html`):**
  - FastAPI + SQLite app using remote Flagsmith evaluation (`enable_local_evaluation=False`, `retries=Retry(total=0)`).
  - Returns `executed_version` (`v2` or `legacy`) and `effective_flags` on every response (including HTTP 500 `"Unrecognized cart schema"`).
  - `APP_VARIANT=no_compat` disables compatibility conversion for the Refusal scenario.
  - 3-user deterministic journey (`alice`, `carol` affected VIPs; `bob` control) requires 3 consecutive matching `/eval` reads (30s timeout) and asserts the Section 6 proof contract directly from action responses.
  - Static HTML shop UI (`GET /`) provides user switching (`alice`, `carol`, `bob`), cart view, add/checkout/eval/reset actions, executed version badge, live effective flags panel, and HTTP 500 error display with no framework or build step.
- **Verifier (`rewind/verifier.py`):** Polls `/eval` convergence, executes the configured journey command, parses single-JSON stdout strictly, checks every configured user and action `effective_flags`, and emits structured pass/fail evidence.
- **End-to-End Verification Without LLM (`scripts/verify_day1_e2e.py`):** Passed 15/15 live assertions across Case A, Case B (`set_off` and `delete_override`), Refusal (`no_compat`), and Target (`demo-target`).

## 2. Not Built Yet (Deferred to Day 2 / Day 3)
- LLM diagnostic loop (`rewind/agent/` — Groq `openai/gpt-oss-120b` + Gemini fallback)
- Interactive CLI & Gate 1 / Gate 2 approval workflow
- Rich terminal trace + HTML / JSON evidence report (`rewind/report/`)
- README full architecture/audit polish & demo video
- Optional Case C ("the blocked fix") — cut first if time is constrained

## 3. Exact Commands
```bash
# Run offline unit tests
~/rewind/.venv/bin/pytest -q

# Seed rehearsal environment cleanly (a | b | refusal)
~/rewind/.venv/bin/python ~/rewind/scripts/seed_flagsmith.py --case a --environment rehearsal
~/rewind/.venv/bin/python ~/rewind/scripts/seed_flagsmith.py --case b --environment rehearsal
~/rewind/.venv/bin/python ~/rewind/scripts/seed_flagsmith.py --case refusal --environment rehearsal

# Start demo-shop manually
DB_PATH=/tmp/shop.db FLAGSMITH_ENV_KEY="$FLAGSMITH_ENV_KEY_REHEARSAL" \
  ~/rewind/.venv/bin/python ~/rewind/examples/demo-shop/app.py --port 8001

# Reset DB & run journey phases
~/rewind/.venv/bin/python ~/rewind/examples/demo-shop/reset.py --db /tmp/shop.db
~/rewind/.venv/bin/python ~/rewind/examples/demo-shop/journey.py --base-url http://127.0.0.1:8001 --phase prepare
~/rewind/.venv/bin/python ~/rewind/examples/demo-shop/journey.py --base-url http://127.0.0.1:8001 --phase verify

# Run full no-LLM Step 9 live suite
~/rewind/.venv/bin/python ~/rewind/scripts/verify_day1_e2e.py
```

## 4. Step 9 End-to-End Outcome Table (Observed)

| Scenario | Action | Expected | Observed | alice | carol | bob | Check |
|---|---|---|---|---|---|---|---|
| Case A | Rollback only | FAIL (500 loud) | FAIL | HTTP 500 / legacy | HTTP 500 / legacy | HTTP 200 / legacy | PASS |
| Case A | identity:alice compat | FAIL (carol 500) | FAIL | HTTP 200 / legacy | HTTP 500 / legacy | HTTP 200 / legacy | PASS |
| Case A | compat environment_default set_on | PASS | PASS | HTTP 200 / legacy | HTTP 200 / legacy | HTTP 200 / legacy | PASS |
| Case B | Rollback only (silent) | FAIL (alice/carol v2) | FAIL | HTTP 200 / v2 | HTTP 200 / v2 | HTTP 200 / legacy | PASS |
| Case B | environment_default set_off | FAIL (still v2) | FAIL | HTTP 200 / v2 | HTTP 200 / v2 | HTTP 200 / legacy | PASS |
| Case B | identity:alice set_off | FAIL (carol v2) | FAIL | HTTP 200 / legacy | HTTP 200 / v2 | HTTP 200 / legacy | PASS |
| Case B | segment:vip_users set_off | PASS | PASS | HTTP 200 / legacy | HTTP 200 / legacy | HTTP 200 / legacy | PASS |
| Case B | segment:vip_users delete_override | PASS | PASS | HTTP 200 / legacy | HTTP 200 / legacy | HTTP 200 / legacy | PASS |
| Refusal | Rollback only (no_compat) | FAIL (500) | FAIL | HTTP 500 / legacy | HTTP 500 / legacy | HTTP 200 / legacy | PASS |
| Refusal | cart_compat_reader environment_default set_on | FAIL (no_compat) | FAIL | HTTP 500 / legacy | HTTP 500 / legacy | HTTP 200 / legacy | PASS |
| Refusal | cart_compat_reader segment:vip_users set_on | FAIL (no_compat) | FAIL | HTTP 500 / legacy | HTTP 500 / legacy | HTTP 200 / legacy | PASS |
| Refusal | checkout_v2 segment:vip_users set_off | FAIL (no_compat) | FAIL | HTTP 500 / legacy | HTTP 500 / legacy | HTTP 200 / legacy | PASS |
| Refusal | checkout_v2 segment:vip_users delete_override | FAIL (no_compat) | FAIL | HTTP 500 / legacy | HTTP 500 / legacy | HTTP 200 / legacy | PASS |
| Target | Case A rollback only | FAIL (500 loud) | FAIL | HTTP 500 / legacy | HTTP 500 / legacy | HTTP 200 / legacy | PASS |
| Target | compat environment_default set_on | PASS | PASS | HTTP 200 / legacy | HTTP 200 / legacy | HTTP 200 / legacy | PASS |

Target Flagsmith changes logged:
- `[demo-target] seed Case A: checkout_v2=ON, cart_compat_reader=OFF, vip_users override cleared`
- `[demo-target] apply: checkout_v2 environment_default set_off`
- `[demo-target] apply: cart_compat_reader environment_default set_on`

Fixture writes logged:
- Isolated temporary SQLite DBs for `rehearsal.db` (4 clean resets across Case A, Case B x2, Refusal) and `target.db` (1 clean reset for Target).

## 5. Assumptions & Design Choices (Hardened Post-Review)
1. **Control user baseline (`bob`) — spec aligned:** `AGENTS.md` Section 6 originally stated `"all three users save carts in v2 format"` in `Prepare` while expecting `"alice and carol get HTTP 500 ... bob fine"` after rollback. Both `AGENTS.md` §6 and `examples/demo-shop` now state the consistent rule: affected VIP users (`alice`, `carol`) save cart updates in `v2` during `prepare`, while control user `bob` (`is_vip = false`) keeps his pre-rollout `v1` baseline cart (`cart-bob`) so he executes `legacy` (`HTTP 200`) across Case A, Case B, and Refusal.
2. **Verifier strictness (`rewind/verifier.py`):** Requires explicit `bool` values in `effective_flags` (rejecting missing or non-bool keys even when expecting `OFF`), checks `http_status == 200`, `executed_version == goal_version`, and flag-to-execution consistency directly on every verify step, requires non-empty `assertions` on each step, and requires explicit `checkout` coverage for every configured user.
3. **Credential redaction & setup/cleanup separation (`rewind/flagsmith_ops.py`):** `_run_cli` discovers and learns JSON credentials *before* writing `command_log` so even the first CLI response is redacted cleanly. Runtime `apply_operation()` always validates against `allowed.scopes`; setup/cleanup helpers (`clear_identity_override_if_present`, `clear_segment_override_if_present`, `create_segment`, `delete_segment`) require an explicit `phase in {"seed", "cleanup"}` `ApprovalToken`. (`ApprovalToken` remains Day 1 scaffolding; exact-plan binding and drift enforcement happen in Day 2.)
4. **Snapshot targeting fidelity (`rewind/snapshot.py`):** Captures configured segment membership rules and identity-override enabled/value state for configured users, and raises `FlagsmithOpsError` if unconfigured/unsupported overrides exist.
5. **E2E evidence & fail-fast (`scripts/verify_day1_e2e.py`):** Retains redacted command logs, raw journey JSONs, SQLite cart/order write inventories, and fail-fast state in `artifacts/e2e/<run_id>/`. Note: live Case A–Target 15/15 outcomes in §4 above were recorded prior to the Day 1 hardening patch; an explicit authorized live rerun can be triggered with `verify_day1_e2e.py`.

## 6. Day 1 Step Status Summary
- Step 1 (Repo & GitHub SSH): PASS (`07c6e6b`)
- Step 2 (Env Check & Flagsmith auth): PASS
- Step 3 (Package Skeleton & `.venv`): PASS (`5fdafc5`)
- Step 4 (Config Validation): PASS (`6a65448`)
- Step 5 (FlagsmithOps & Snapshot): PASS (`fa015df`)
- Step 6 (Idempotent Seed Script): PASS (`d7264e2`)
- Step 7 (Demo Shop App, Reset, Journey): PASS (`cca22fe`)
- Step 8 (Verifier & Unit Tests): PASS (`da12af4`)
- Step 9 (End-to-End No-LLM Live Suite): PASS (`3f423fe`, 15/15 assertions)
- Step 10 (Static Shop UI): PASS (`7de5831`)
