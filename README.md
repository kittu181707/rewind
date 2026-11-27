# Rewind

**Off is not undo.**

> Rewind checks whether turning a feature off really restores the old experience without breaking saved user data. With your approval, it rehearses the transition on an isolated test copy of your app using real Flagsmith configuration. If it fails, the agent investigates the evidence and tests up to three configuration-only recovery plans. A separate approval is required before a verified plan is applied to the target environment.

**Claim:** Rewind verifies that the intended users return to legacy behaviour and that their latest saved state still works.

```bash
rewind demo
```

See a captured rollback failure, diagnosis, approvals, and staging verification in about ten seconds, with an offline report.

**Quick start** (Python 3.12; no credentials needed for replay):

```bash
git clone https://github.com/kittu181707/rewind.git && cd rewind
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e . && rewind demo
```

You can also use `python -m rewind demo` from the activated environment.

## Live proof

| Scenario | What happened | Sample report | Captured evidence |
| --- | --- | --- | --- |
| Case A | The old checkout returned HTTP 500 for carts saved by the new checkout. Enabling the existing compatibility reader made the same carts work. | [Open HTML](docs/sample-report-case-a.html) | [run.json](docs/captured-runs/case-a/run.json) |
| Case B | The default was OFF, but the VIP override kept two users on the new checkout. Removing that override returned them to legacy checkout. | [Open HTML](docs/sample-report-case-b.html) | [run.json](docs/captured-runs/case-b/run.json) |
| Refusal | No tested setting restored the old checkout. The verifier rejected three proposals and Rewind left the target unchanged. | [Open HTML](docs/sample-report-refusal.html) | [run.json](docs/captured-runs/refusal/run.json) |

Download the HTML to open it locally. The captures use real model and Flagsmith calls with **scripted approvals**. See the [validation tables and verbatim diagnoses](DAY3_NOTES.md), or replay Case B with `rewind demo --case b` and Refusal with `rewind demo --case refusal`.

**Interactive approval:** The typed `yes` path is covered by tests. Separate human run: _evidence link pending_.

## How it works

```text
Approve rehearsal → save test carts → switch off the feature → observe what broke
     → agent reads evidence and proposes a setting change → verifier checks the same users
     → approve the exact target plan → reproduce the failure on staging → apply and verify
```

The verifier checks which checkout actually ran, whether it completed, and whether cart IDs, quantities, totals, and user traits survived. If no proposal passes, Rewind stops before touching the target.

## What Rewind does alone vs asks a person

| Rewind does alone | Rewind stops and asks a person |
| --- | --- |
| Read flag state, allowlisted source, and journey evidence. | **Gate 1:** Approve the rehearsal environment, allowed changes, and operation budget. |
| After Gate 1, rehearse the rollback, diagnose failures, propose and apply a permitted rehearsal change, and verify the journey. | **Gate 2:** Approve the exact target changes and fixture replay against the displayed starting snapshot. |
| Stop without touching target if no configuration-only plan passes. | **Drift:** Stop and require a fresh Gate 2 if target settings changed. |
| Record the checks, approvals, and changes made. | **Target failure:** Report what changed and ask before any recovery action. Never revert automatically. |

See [the autonomy guide](docs/autonomy.md).

## How it uses Flagsmith

| Feature | What Rewind checks or changes |
| --- | --- |
| Environments | Rehearsal and staging have separate flag settings and separate test databases. |
| Identities, traits, and remote evaluation | Check the flags each test user actually received, then check the checkout that ran. |
| Environment defaults and segment overrides | Turn off the default, then test whether a compatibility setting or group-specific override explains the result. |
| CLI and audit log | Read and write through the CLI. Compare Rewind's recorded commands with the dashboard audit log where available. |

[How Rewind uses Flagsmith](docs/how-rewind-uses-flagsmith.md) explains what would break without each feature and lists the exact redacted commands from the captures.

## Run it for real

You need **Python 3.12+** and the repository checkout. For a live check, also install **Flagsmith CLI v2**, use a Groq API key, and prepare two **dedicated non-production** Flagsmith environments. The demo config expects `rehearsal` and `demo-target`, the Boolean flags `checkout_v2` and `cart_compat_reader`, and the `vip_users` segment with `is_vip` equal to `true`. It starts the shop on loopback ports **8001** and **8002**. Seed sets flag baselines; it does not provision the Flagsmith project.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

| Environment variable | When it is used |
| --- | --- |
| `GROQ_API_KEY` | Required for live diagnosis with Groq `openai/gpt-oss-120b`. |
| `FLAGSMITH_ENV_KEY_REHEARSAL` | SDK key for `rehearsal`, used for flag evaluation and to select the environment in CLI commands. |
| `FLAGSMITH_ENV_KEY_TARGET` | Distinct SDK key for `demo-target`. A staging approval is still required before any target writes. |
| `FLAGSMITH_API_KEY` | Optional Flagsmith management key for headless CLI authentication, in place of `flagsmith login`. This is separate from the two SDK keys. |
| `APP_VARIANT` | Optional demo-shop setting. Use `no_compat` for Refusal; leave it unset for Case A or Case B. |

Rewind sets `FLAGSMITH_ENV_KEY` and `DB_PATH` for each app instance and passes the configured port (`PORT` is only a default for starting the shop by hand).

**Validation harness only:** Its preflight still checks for `GEMINI_API_KEY`, and it sets `FLAGSMITH_NO_INPUT` to prevent interactive Flagsmith login. Neither is needed for `rewind demo` or the normal `rewind check`.

Put your live credentials in the ignored `.env` using `.env.example` as a guide, or export them in the shell. Run `flagsmith login` in the checkout using the system keychain, or use `FLAGSMITH_API_KEY`. Confirm `flagsmith environment list` shows both intended environments and the CLI is bound to the right project. The WSL development machine used a headless keyring wrapper; it is not part of Rewind.

```bash
rewind seed --case a
rewind check --config examples/demo-shop/rewind.config.yaml
```

Seed resets the dedicated rehearsal flag settings only. `check` shows a rehearsal approval, the failing checkout, and the proposed repair. If rehearsal passes, Gate 2 lists the ordered staging writes and fixture changes. Read each plan before typing `yes`. If the target changed or the failure does not reproduce there, Rewind stops instead of applying the repair. It writes `run.json`, `report.html`, and `report.json` under `.artifacts/`.

## Connect your own app

Start from the [demo config](examples/demo-shop/rewind.config.yaml) and use a safely isolated app instance for each environment. Use synthetic or otherwise resettable test data; the reset command can replace the disposable fixture database.

- **Config:** Supply the two environment names and the names of their key variables, separate ports, trusted app start/reset/journey commands, allowlisted source paths, the rollback flag/direction/goal, test identities and traits, and allowed flag changes. List the fixture writes the target journey will perform.
- **App and data:** Start on loopback and expose a ready `/health` endpoint. Accept the SDK key and a separate database path for each instance. A reset must restore the fixture before trying a new plan.
- **Journey:** Implement `prepare` and `verify` in a trusted command. It receives the app URL, phase, config path, state file, and expected flags. It emits one JSON result with named user steps, HTTP status, the version that actually ran, effective flags, and pass/fail assertions. After a flag write, wait for three consecutive matching `/eval` reads; use the cart or checkout response itself to verify what actually ran and what state survived. See the [journey contract](AGENTS.md#72-journey-contract-any-language) and the [working example](examples/demo-shop/journey.py).

## Validation results

At [`agent-freeze-2`](https://github.com/kittu181707/rewind/tree/agent-freeze-2), the three Case A revalidations and three Case B runs verified on the first proposal. The three Refusal runs stopped with unchanged target hashes after the **verifier rejected three proposals each**. The model did not identify the missing code path and call `report_no_fix` in those live runs.

- **Initial V1 failed.** The model proposed sending VIP users back to the new checkout. Rewind rejected it; the later model call failed before Gate 2.
- **Revision 1** gave the model the configured rollback goal, required source and evidence reads, and reduced the size of the evidence sent. V1–V8 passed under the retry policy; two Refusal runs initially hit Groq HTTP 400 for an undeclared `json` tool, then passed on retry. See the [Day 2 results](DAY2_NOTES.md#revision-1-outcomes).
- **Revision 2 — post-freeze refusal fix.** Use the declared terminal `report_no_fix(reason)` tool after the required reads. The tested legacy reply parser remains for compatibility. Case A and Refusal revalidation each passed **3/3** with no whole-run retries or HTTP 400s. See the [freeze manifest](docs/freeze.md#revision-2--refusal-protocol) and [Day 3 results](DAY3_NOTES.md#1-refusal-protocol-revalidation).
- **Case B passed 3/3.** Case B was part of the earlier model comparison. The agent code was frozen before these integrated runs and was not changed in response to their results. See [both hashes and the verbatim diagnoses](DAY3_NOTES.md#2-case-b-with-the-frozen-agent).

## Honest limits

- A pass covers the test users, saved fixtures, and staging configuration that were exercised. It does not establish rollback safety for every production customer or undo a database migration.
- Rewind proposes configuration-only changes. It needs an app-specific, trusted journey; it does not edit app code, user traits, or the verifier.
- `planning.py` supports Boolean flags and segment rules based on equality. It does not model arbitrary conditions or multivariate rollouts. Flagsmith **Change Requests** are not supported.
- The approval registry is in-process. The model only has read tools and tools to propose or refuse; it cannot invoke approvals or apply writes. This is not isolation from other Python code running in the same process.
- Gemini fallback is **disabled**. No synthetic probe completed a tool exchange, so a working fallback is untested. Live runs use Groq only.

## Prior work

A throwaway pre-event spike outside this repository verified the Flagsmith command forms and evaluation behaviour.

## AI tools used

TODO: Add the AI tools used during the hackathon.

## License

[MIT](LICENSE).
