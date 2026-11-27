# Rewind — Day 3, part 1

**November 27, 2026 UTC.** Revision 2 fixes the refusal protocol. Case B was first run with the agent at the exact `agent-freeze-2` commit, with no subsequent prompt, tool, or decision-logic changes. All live approvals below were **scripted validation**, not interactive approval.

## 1. Refusal protocol revalidation

The code at `ae5cc76` passed 114 offline tests. `report_no_fix` is a terminal tool with the same read prerequisites as a proposal, a required redacted reason, and a 1,000-character limit. The old plain and fenced JSON parser remains covered for backwards compatibility. Revision 2 was tagged at `25dfcc1`; revision 1 remains at `a63272e`.

| Run | Scenario | First-run result | Agent attempts | Run (s) | Model HTTP (s) | Prompt / completion tokens |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| V3.1 | Case A, both gates approved | Verified | 1 | 86.101 | 5.150 | 5,718 / 1,181 |
| V4.1 | Case A, both gates approved | Verified | 1 | 78.882 | 5.787 | 5,724 / 881 |
| V5.1 | Case A, both gates approved | Verified | 1 | 78.169 | 6.527 | 5,718 / 936 |
| V6.1 | Refusal | Refused; target unchanged | 3 | 107.421 | 18.887 | 16,894 / 3,362 |
| V7.1 | Refusal | Refused; target unchanged | 3 | 89.894 | 16.525 | 15,582 / 2,792 |
| V8.1 | Refusal | Refused; target unchanged | 3 | 85.197 | 16.375 | 16,799 / 2,993 |

**Zero whole-run retries and zero HTTP 400s**, including unknown-tool errors. V6 recorded 14 seconds of provider backoff inside the run. All three live Refusals reached the correct refusal after three failing proposals; none used `report_no_fix` directly, so acceptance of that new terminal tool is proven by offline tests. Audits a–f passed. Logs: `.artifacts/validation-20261127T013434540822Z/validation.json` and its six transcripts.

## 2. Case B with the frozen agent

| Run | Result | Agent attempts | Run (s) | Model HTTP (s) | Prompt / completion tokens | Target hash before / after |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| B1.1 | Verified | 1 | 98.122 | 4.649 | 5,279 / 628 | H / H |
| B2.1 | Verified | 1 | 124.939 | 4.844 | 5,279 / 643 | H / H |
| B3.1 | Verified | 1 | 109.494 | 5.450 | 5,279 / 647 | H / H |

`H = 141c5a5d62978c3215c169d1a7addc717f7528f702cf2731a2beb97d533743c2` (full SHA-256). The hashes match because each replay temporarily recreated the failure, then returned to the verified flag state. This did not mean there were no target writes. Each run independently verified its final configuration and the journey for all three users.

**Verbatim diagnoses and actual proposal:**

- **B1.1:** “Segment override for vip_users forces checkout_v2 on, overriding the rollback to off.” Proposal: `checkout_v2 / segment:vip_users / delete_override`.
- **B2.1:** “A segment override for vip_users forces checkout_v2=true, overriding the rollback default.” Proposal: `checkout_v2 / segment:vip_users / delete_override`.
- **B3.1:** “A segment override for vip_users forces checkout_v2=true, overriding the rollback default.” Proposal: `checkout_v2 / segment:vip_users / delete_override`.

All three runs passed on the first proposal. No whole-run retries, no HTTP 400s for unknown tools, and no provider retry waits. Output is in `.artifacts/validation-20261127T015217996947Z/validation.json` and its three transcripts. The secret, environment, scope, source-boundary, and budget audits passed. The refusal-only audit did not apply to this batch. The [B1 capture](docs/captured-runs/case-b/run.json) passed a fresh secret scan before it was saved.

Run times in both tables exclude setup, outer snapshots, and inter-run spacing. The model times are HTTP time, and token totals are the provider-reported usage.

## 3. Report and replay

- `rewind check` now writes `report.html` and `report.json` beside `run.json`. Reports preserve each user observation, failing checks, attempts including rejected proposals, approval evidence, available target observations, and recorded changes.
- Offline samples: [Case A](docs/sample-report-case-a.html), [Refusal](docs/sample-report-refusal.html), [Case B](docs/sample-report-case-b.html). Chromium showed the header fully visible at 360×640 in both color schemes, with no HTTP requests for any sample.
- `rewind demo` and `rewind demo --case refusal` use that report generator. Actual subprocesses with credentials cleared took **9.179 s** (Case A) and **9.305 s** (Refusal). The offline tests also blocked network access, dotenv loading, and approval input. The final offline suite passed **121 tests**.

## Commands and deviations

From WSL in `~/rewind`:

```bash
git pull --ff-only origin main
env TMPDIR=/tmp PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -B -m pytest -q -x -p no:cacheprovider -m 'not live'
env PATH=/usr/local/bin:/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m scripts.validate_day2 --batch refusal-protocol
env PATH=/usr/local/bin:/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m scripts.validate_day2 --batch case-b
env -i PATH=/usr/local/bin:/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 .venv/bin/rewind demo
env -i PATH=/usr/local/bin:/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 .venv/bin/rewind demo --case refusal
```

- The validation harness runs the same seeder and orchestrator in-process to inject fixed, labeled scripted answers. It retains provider errors and only permits a whole-run retry on provider failure before any target writes. The normal CLI still uses interactive gates.
- The sole post-freeze agent change was the verified refusal-protocol bug. Both historical Day 2 captures remain intact; sample A and Refusal replay those captures. The Case B sample uses the new frozen run. A live report uses the recorded final user observations if no independent after-snapshot was captured.
- Browser smoke checks used the existing WSL Playwright installation in the spike environment. The report and replay need no browser driver. Interactive video, benchmark and any extra examples are outside part 1.
