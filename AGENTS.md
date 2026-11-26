# REWIND: Project Context for the Coding Agent

> Save this as `AGENTS.md` in the repo root (or give it to your agent whole).
> Read it fully before every task. It is the single source of truth. If something here conflicts with a chat message, ask the human which wins. If a task is not covered here, put the idea in `LATER.md` and do not build it.

**Status when this file was written:** Nov 26, 2026 (amended from the Nov 25 context spec). Everything below marked VERIFIED was proven in a throwaway spike. Everything marked UNTESTED was not.

---

## 1. What we are building

**Rewind** is an AI agent that rehearses a feature-flag rollback before you do it for real. Tagline: **"Off is not undo."**

Turning a feature flag off is supposed to be the safe move. But if the new version saved data the old version cannot read, or a targeting rule keeps some users on the new version, the "rollback" breaks customers. Rewind rehearses the rollback on an isolated test environment with real Flagsmith configuration, investigates why it fails, tries up to three configuration-only recovery plans, and proves a fix with a fixed verifier. Two human approvals are required before anything is changed.

**Pitch (use verbatim in the README):**
> Rewind checks whether turning a feature off really restores the old experience without breaking saved user data. With your approval, it rehearses the transition on an isolated test copy of your app using real Flagsmith configuration. If it fails, the agent investigates the evidence and tests up to three configuration-only recovery plans. A separate approval is required before a verified plan is applied to the target environment.

**The one claim to protect:** Rewind verifies that the intended users actually return to legacy behaviour, and that their latest saved state still works.

**Positioning sentence:** Rewind investigates why a configured user journey fails after a feature rollback and tests whether a configuration-only recovery can restore the intended behaviour.

**Filter for every feature:** does it help prove that Rewind can safely investigate, rehearse and verify a rollback that ordinary feature-flag controls cannot prove safe on their own? If not, it goes in `LATER.md`.

## 2. The hackathon

- **Toggle Wars** (WeMakeDevs x Flagsmith). Online. Hacking starts **Thu Nov 26, 8:00 AM IST**. Submissions close **Sun Nov 29, 8:00 PM IST**. Target submission: Sunday by 2:00 PM IST.
- **Track 02: AI Agents and Feature Flags.** Brief: an agent or tool that works with Flagsmith. Judges look for: use of the Flagsmith **CLI, API or MCP**, **saves real time**, **a human approves changes**, **clear output**.
- **Scorecard (all tracks):** Use of Flagsmith ("is Flagsmith a core part; do the flags or experiments do real work?"; the "delete Flagsmith" test), Impact ("would a developer or team actually use it?"), Creativity, **Demo video (25% of the score)**: clearly show the problem, what we built and how Flagsmith fits in.
- Deliverables: public repo, README, demo video (2-3 minutes). AI tools are allowed, but the human must be able to explain everything built.
- More than 1,000 people registered. Assume strong competition.
- We are **solo**, working in WSL Ubuntu (`~/rewind`) on an ARM64 Windows laptop.
- Experiment launch (Flagsmith Experimentation) is **not** part of the plan. It is a stretch goal only if everything else is done.

## 3. Hard rules (non-negotiable)

1. **Secrets:** never print, log, echo or commit a key, token or OAuth URL. `.env` is gitignored; `.env.example` has variable NAMES only. Config files reference env-var names, never values. Flagsmith management auth is `flagsmith login` (stored in the OS keyring in WSL via `/usr/local/bin/flagsmith`).
2. **No hardcoded names** (flags, segments, users, environments) anywhere under `rewind/`. They come from `rewind.config.yaml`. `rewind/` never imports `examples/`.
3. **The agent cannot edit** application code, the journey, the verifier, saved data, user traits or assertions. The agent proposes; the fixed verifier decides. Enforce this with tools and code, not with prompt wording alone.
4. **Never run a shell with LLM-produced text.** Run the Flagsmith CLI with an argument list (no `shell=True`) after validating against the allowlist.
5. **WSL only.** Windows Application Control blocks native modules (e.g. Pydantic's) on the Windows side. Never change, disable or bypass any Windows security setting.
6. **Stop on failure:** if a command fails or something is unclear, stop and show the exact command and error. Do not invent results, numbers or screenshots.
7. **Honesty:** labels like "captured run", "measured once", "written by a separate AI session" must be accurate. Never claim a guarantee for every production customer. Success means the tested journey is verified on an allowlisted staging target.
8. **No automatic revert.** If target verification fails, stop, preserve the evidence, list exactly what changed, and request approval for a recovery action.
9. **Prior work:** a throwaway spike proved the idea before the event (in `~/rewind-spike`). You may read its report for exact CLI command forms. Do not copy app code wholesale. If anything is reused, the README says so in one line under "Prior work".
10. **Report format after each task:** short PASS/FAIL list with evidence (command and output excerpt), what is unfinished, and any assumption you made.

## 4. Current state (what is already proven)

**Flagsmith project:** `rewind-demo` (id 55311), Flagsmith Cloud, free plan. Environments: `rehearsal` and `demo-target`. Flags: `checkout_v2`, `cart_compat_reader`. Segment: `vip_users` (rule: trait `is_vip` equals true). The spike left both flags OFF in both environments.

**Spike results (VERIFIED on real Flagsmith):**
- Case A (loud failure) reproduces and recovers via `cart_compat_reader`.
- Case B (silent failure from a segment override) reproduces and recovers. The only signal is the executed version being `v2` while the environment default is OFF, with HTTP 200 and no error.
- Replay on `demo-target` with its own database and environment key works.
- The CLI performed every needed mutation, including segment override set-off and delete. No raw `flagsmith api` fallback was needed.
- Client-side environment keys work for remote identity evaluation.
- **Latency:** CLI command 2.5-4.7 s; edge convergence 0.5-5.5 s; end to end 4.5-8 s per flag change. The verifier must **poll** for the expected effective flags before asserting. Never sleep a fixed time.
- **Set-OFF override and delete-override differ:** after set-OFF, a VIP user stays on legacy even if the environment default is later turned ON. After delete, the user inherits the default. Operations and approvals must name which one is intended.

**UNTESTED:** `flagsmith segment create <name> --rules @file` (the spike reused an existing segment). Test it first in the seed script.

**Auth on this machine:** Flagsmith auth goes through the headless-keyring wrapper at `/usr/local/bin/flagsmith`. Liveness check: `flagsmith environment list`. The README must document auth for other machines (`flagsmith login`, or a management API key set in the environment).

**Environment:** Ubuntu WSL2 (ARM64), Python 3.12.3, Flagsmith CLI v2.0.0, Flagsmith Python SDK 6.7.0, Playwright Chromium launches headless. Runtime LLM chosen by a bake-off: **Groq `openai/gpt-oss-120b`** (primary; fast, valid tool calls 9/9, correct action in A, B and the refusal scenario). Fallback: Gemini `gemini-3.6-flash` (slow, one wrong identity-scope proposal). Do not use `llama-3.3-70b-versatile` (404) or `gemini-4-hydrogen` (503s).
Known model trap: models propose `identity:<user>` overrides because the user is named in the evidence. That masks a broken segment override. The verifier must catch it (see section 8).

## 5. Architecture

```
                     rewind.config.yaml  (journey, flags, envs, users, allowed ops, model)
                                  |
   +------------------------------v------------------------------+
   |                          REWIND CORE (rewind/)              |
   |                                                             |
   |  orchestrator ----> approvals (gate 1, gate 2)              |
   |       |                                                     |
   |       |-- agent loop  (LLM adapter + 4 tools)               |
   |       |      read_source | read_flag_state | read_evidence   |
   |       |      propose_operation(diagnosis, flag, scope, op)  |
   |       |                                                     |
   |       |-- flagsmith_ops  (ONLY place that runs the CLI;     |
   |       |      allowlist + environment allowlist + logging)   |
   |       |-- snapshot       (flag/override state + hash)       |
   |       |-- verifier       (runs journey cmd, polls, parses)  |
   |       '-- report         (rich terminal log + HTML + JSON)  |
   +------+---------------------------------------+--------------+
          | app commands (start/reset/journey)    | flagsmith CLI
          v                                       v
   examples/demo-shop (FastAPI+SQLite)       Flagsmith Cloud project
     rehearsal instance :8001                  environment: rehearsal
     target instance    :8002                  environment: demo-target
     separate SQLite DB per instance
```

**Trust boundaries:**
- The agent sees only: allowlisted source files, flag/override state (read-only), the last verifier evidence, and the `propose_operation` tool. It cannot call the CLI, the app, the DB or the verifier directly.
- Only `flagsmith_ops` mutates Flagsmith, and only with an approval token from `approvals.py`.
- Only the orchestrator runs the verifier, after an operation is applied.

**Flagsmith features used (this is the "how Rewind uses Flagsmith" table in the README; write the "what breaks without it" column):**
- Environments (rehearsal vs target): separate state to rehearse safely.
- Identities and traits: per-user evaluation, so the same user's journey can be tracked.
- Segments and segment overrides: Case B exists only because of them.
- Environment default values: the rollback itself.
- Flagsmith CLI: every read and write.
- Audit log: a real, checkable record of what Rewind changed (screenshot it for the README).

**Flagsmith facts to respect:**
- Remote evaluation, no SDK cache: changes show up after edge convergence (a few seconds). Local evaluation is not used (it polls on an interval, 60 s by default). State this choice in the README.
- Identity overrides win over segment overrides, which win over the environment default.
- Change Requests (Flagsmith's own approval flow) exist only on paid plans. Environments with Change Requests enabled reject direct writes (HTTP 409). Not supported; say so in README limits.
- A running Experiment locks its flag's values. Out of scope.

## 6. The cases

All cases use three users: **alice** and **carol** (`is_vip=true`) are the affected users; **bob** is the control (not VIP). Every journey must cover all three, so an identity-only patch fails verification.

Journey phases (the orchestrator calls them): `prepare` (affected customers save v2 cart changes; control user bob keeps his baseline v1 cart), then Rewind applies the rollback (`checkout_v2` default OFF), then `verify`.

The locked core set is **Case A**, **Case B**, and **Refusal**.

| | Case A (loud) | Case B (silent) | Refusal |
|---|---|---|---|
| Baseline | `checkout_v2` default ON; `cart_compat_reader` OFF; no overrides | `checkout_v2` default ON; segment override ON for `vip_users`; `cart_compat_reader` ON | demo-shop variant where legacy has **no** compat-reader code; reader flag cannot help |
| Prepare | alice and carol (affected) save carts in v2 format; bob (control) keeps v1 baseline cart | same | same as A |
| Rollback | `checkout_v2` default OFF | `checkout_v2` default OFF | same as A |
| Failure signature | alice and carol get HTTP 500 "Unrecognized cart schema"; bob fine | **no error**: alice and carol still execute v2 (HTTP 200), bob executes legacy | HTTP 500 for alice and carol |
| Right fix | `cart_compat_reader`: environment_default, set_on | `checkout_v2`: segment:vip_users, set_off **or** delete_override | none exists |
| Must fail verification | `identity:alice` compat patch (carol still fails) | `identity:alice` set_off (carol still on v2); changing the environment default; touching traits | any proposal |
| Expected output | verified plan, approvals, target applied | verified plan, approvals, target applied | "No verified configuration-only recovery found. Target unchanged." Target is **never** touched. |

**Optional Case C ("the blocked fix"):** a normal development case (cut first if time is short) testing a targeting override blocking the recovery flag itself.

**Proof contract (every attempt, in every case):**
1. Same named customers and same cart IDs before and after.
2. Latest quantities and totals survive.
3. User traits (for example `is_vip`) are unchanged.
4. The legacy checkout path actually executed for every user (backend-reported version), including every user in the affected segment.
5. Checkout completes with HTTP 200.
6. Effective flags reported by the app match the observed execution.
7. Record effective flags and executed version after **every** transition.
8. State persists through a sequence. Reset only between attempts.
9. The control user (bob) is not harmed.

## 7. Contracts

### 7.1 `rewind.config.yaml` (illustrative; keep these ideas; NO secrets)
```yaml
project: demo-shop
flagsmith:
  rehearsal: { environment: rehearsal, key_env: FLAGSMITH_ENV_KEY_REHEARSAL }
  target:    { environment: demo-target, key_env: FLAGSMITH_ENV_KEY_TARGET }
app:
  start:   "python app.py --port {port}"        # receives FLAGSMITH_ENV_KEY, DB_PATH from Rewind
  reset:   "python reset.py --db {db}"
  journey: "python journey.py --base-url {base_url} --phase {phase}"
  rehearsal_port: 8001
  target_port: 8002
source_paths: ["examples/demo-shop/app.py"]    # the only files the agent may read
rollback:
  flag: checkout_v2
  to: off
  goal: "every user executes the legacy checkout and keeps their latest cart"
users:
  - { id: alice, traits: { is_vip: true },  role: affected }
  - { id: carol, traits: { is_vip: true },  role: affected }
  - { id: bob,   traits: { is_vip: false }, role: control }
allowed:
  flags: [checkout_v2, cart_compat_reader]
  scopes: [environment_default, "segment:vip_users"]   # identity scopes off unless a case enables them
  operations: [set_on, set_off, delete_override]
agent:
  provider: groq
  model: openai/gpt-oss-120b
  key_env: GROQ_API_KEY
  max_attempts: 3
  fallback: { provider: gemini, model: gemini-3.6-flash, key_env: GEMINI_API_KEY }
```
Validation must reject values that look like secrets.

### 7.2 Journey contract (any language)
The journey command prints **one JSON object** to stdout and exits 0 or 1:
```json
{"ok": false,
 "steps": [{"name": "...", "user": "alice", "http_status": 500,
            "executed_version": "legacy",
            "effective_flags": {"checkout_v2": false, "cart_compat_reader": false},
            "assertions": [{"name": "latest_quantities_survive", "pass": false, "detail": "..."}]}]}
```
Before asserting, the journey and verifier require **3 consecutive matching `/eval` reads** until the app reports the expected effective flags (timeout 30 s). Assertions must use the `effective_flags` returned in the same response as the cart/checkout action, not only `/eval`. The app exposes `/cart/add`, `/checkout`, `/eval` and returns `executed_version` and `effective_flags` in every response. `reset` restores a clean fixture database.

### 7.3 Flagsmith operations (VERIFIED forms; reuse the exact flag syntax from the spike report)
```
flagsmith flag update <flag> --enable|--disable --environment <ENV> --yes
flagsmith flag update <flag> --disable --segment <name> --environment <ENV> --yes   # set override OFF
flagsmith flag delete <flag> --segment <name> --environment <ENV> --yes             # delete override
flagsmith flag get <flag> [--segment <name>] --json                                  # management read
flagsmith segment list --json
flagsmith environment list --json
```
`flagsmith_ops` is the only module that runs these. It checks every call against `allowed` and the environment allowlist, redacts keys in logs, and returns structured results.

## 8. The agent

**LLM adapter:** provider and model from config; one thin interface; Groq via the OpenAI-compatible API; Gemini as fallback. Name the model used in the README and the report.

**Tools exposed to the model:**
- `read_source(path)`: only files in `source_paths`.
- `read_flag_state()`: snapshot of flags, overrides and effective values (read-only).
- `read_evidence()`: the last verifier JSON.
- `propose_operation(diagnosis, flag, scope, operation)`: `diagnosis` is **required** (one or two plain sentences). Validate arguments against `allowed`. Invalid proposals count as a failed attempt and are reported.

**Loop (per run):**
1. Show what will change in the rehearsal environment. **Approval 1** (see section 9).
2. For attempt 1 to `max_attempts`: reset rehearsal (database and baseline config) between attempts only; run `prepare`; apply the rollback; run `verify` to capture the failing evidence; give the agent the evidence and tools; receive one proposal; validate; apply to rehearsal; run `verify`; if it passes, stop; if it fails, return the structured failure reason (for example "carol still executes v2") and try the next attempt.
3. If no attempt passes (or the agent states that no configuration-only fix exists and makes no proposal): output **"No verified configuration-only recovery found. Target unchanged."** Never touch the target.
4. If one passes: show the target diff and ask **Approval 2**; then replay on the target.

**Prompt guidance:**
- Fix the root cause. Never mask a broken segment override with per-user identity overrides.
- Never change traits, the environment default (unless it is the cause), or anything outside `allowed`.
- If no configuration-only fix exists, say so and make no proposal.
- The agent's diagnosis must name the mechanism (for example "the segment override is enabling v2 although the environment default is OFF").

**Why an LLM and not a combination-testing script (README, honestly):** for two flags a brute-force script could also work. The agent reads evidence, source and configuration, explains a diagnosis, and chooses the next hypothesis, which is what matters as the number of flags and segments grows. This is a rationale to investigate, not a measured capability.

## 9. Approvals and safety

| Approval | What the human approves |
|---|---|
| **1** | Rewind may modify the **rehearsal** environment: this environment, these permitted operations (from `allowed`), up to 3 candidate plans. |
| **2** | Rewind may apply **this exact verified diff** to the target, against **this expected starting state**. Shown first: the target's current config (snapshot), the exact operations with named type ("set override OFF" vs "delete override"), **every target mutation** including the replay's flag changes and fixture writes, and the target database path (a disposable fixture only). |

- Approvals are enforced in code: `flagsmith_ops` mutating functions require an approval token produced by `approvals.py`. The agent cannot create one.
- **Drift check:** snapshot the target at approval; re-snapshot just before applying; if the hash differs, ask for approval again.
- Target replay = the same sequence as the rehearsal (prepare, rollback, verify-before, apply, verify-after), on the target's own database and environment key. The target must **reproduce the failure** before the fix is applied, otherwise it proves nothing.
- If target verification fails: stop, preserve evidence, report every change made, request approval for a recovery action. No automatic revert.
- Environment allowlist: only `rehearsal` and `demo-target` (from config) can be written.

## 10. Outputs

- **Terminal log** (rich): phases, each flag operation with timing, the agent's diagnosis, the verifier result per attempt, approval prompts.
- **Evidence report** (static HTML + JSON): header with the claim; run metadata (model, time, environments); a timeline table of effective flags and executed version after every transition; failing evidence; per-attempt diagnosis, proposed operation (with named type) and verifier result; approvals (what was approved, when); target before/after; a list of every change made; a limits footer. Must be readable in the first screen without running anything.
- **`rewind demo`:** replays a **captured real run** through the real report generator in about 10 seconds, no accounts needed. Label it "captured run".
- **`docs/sample-report.html`:** a captured example committed to the repo.
- **Benchmark (Day 3, measured once):** setup, failure discovery, recovery attempts, verification, total, for the manual rollback test versus Rewind. Include LLM time and approval time. Label it "measured once".
- **Flagsmith audit-log screenshot** showing what Rewind changed.

## 11. Repository layout

```
rewind/            cli.py config.py flagsmith_ops.py snapshot.py verifier.py approvals.py
                   agent/ report/   (never imports examples/)
examples/demo-shop/ app.py reset.py journey.py rewind.config.yaml
examples/second-app/   (Day 3, if the judge path is done)
scripts/seed_flagsmith.py     (idempotent; --case a|b|refusal)
docs/how-rewind-uses-flagsmith.md   docs/sample-report.html
README.md  AGENTS.md  LATER.md  LICENSE (MIT)  .env.example  .gitignore
tests/
```
Python 3.12; FastAPI, uvicorn, PyYAML, rich, pytest, Flagsmith SDK (remote evaluation, no caching). CLI entry points: `rewind check --config <path>`, `rewind demo`, `rewind seed`.

**Tests:** unit tests for config validation, the ops allowlist, snapshot hashing and verifier parsing (passing and failing fixtures). Live case tests are slow and marked as such. Optional CI: unit tests only, no Flagsmith.

## 12. Timeline (solo, IST)

- **Day 1, Thu Nov 26:** repo, config loader, ops wrapper and allowlist, snapshot, seed script (test `segment create` first), demo shop and journey, verifier. Prove Cases A and B recover **without an LLM** and that an identity-only patch fails verification. Write `DAY1_NOTES.md`.
- **Day 2, Fri Nov 27:** agent loop, tools, approvals enforced in code, Case A end to end with the LLM, refusal case. **Freeze prompt, tools and decision logic at the end of the day.**
- **Day 3, Sat Nov 28:** run Case B (and optional Case C if time allows) unchanged; evidence report; `rewind demo`; benchmark; README; **record the demo video this day**. Second example app only if the judge path is done.
- **Day 4, Sun Nov 29:** buffer. Clean-machine run on an x86 box (the dev laptop is ARM64), README run-through, re-record if needed, final checks, **submit by 2:00 PM IST**.

**Cut order if time runs short:** experiment launch, then UI polish. A second example app is protected unless Case B or the refusal slips. Never cut the judge path.

## 13. Judge path (never cut)

1. README first screen: "Off is not undo.", the pitch, the one-line claim, one run command.
2. `rewind demo` and a static sample report.
3. "Live proof" section: the evidence report, the captured run, the Flagsmith audit-log screenshot.
4. "How Rewind uses Flagsmith" table with a "what breaks without it" column.
5. "Connect an existing test app": the app needs Flagsmith integrated, a journey command, resettable test data, an isolated runnable instance. The connection is three commands plus the config. Only call it "in 5 minutes" if measured.
6. Honest limits: configuration changes only, needs a journey, cannot undo a database migration, Change Requests environments not supported, success means the tested journey on a staging target.
7. README lists every environment variable, one real run command, and what should happen when it works. No keys in the repo.
8. Demo video, 2-3 minutes: about 15 seconds of the problem; most of the time on the working demo with **both approvals shown on screen**; a clear look at the Flagsmith side. A long first-screen README is a failure; MAELIN (a previous submission) lost points for jargon, unclear value and no way to connect.

## 14. Out of scope

Automatic recovery or revert; editing application code; undoing database migrations; experiment launch (stretch only); Change Requests support; the Flagsmith MCP server (the CLI is used); multi-tenant or production hardening; a polished UI.

## 15. Working agreements

- Commit small and often. Meaningful messages.
- Update `DAYn_NOTES.md` at the end of each day: what works, what does not, exact commands.
- After each task, report in the section 3 format.
- If a decision is needed that this file does not cover, ask the human; do not guess. Put new ideas in `LATER.md`.
