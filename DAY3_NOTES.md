# Rewind — Day 3

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
- Through the part 1 validation, the sole post-freeze agent change was the verified refusal-protocol bug. Both historical Day 2 captures remain intact; sample A and Refusal replay those captures. The Case B sample uses the new frozen run. A live report uses the recorded final user observations if no independent after-snapshot was captured. See [the freeze manifest](docs/freeze.md#changes-after-revision-2) for the later changes.
- Browser smoke checks used the existing WSL Playwright installation in the spike environment. The report and replay need no browser driver. Interactive video, benchmark and any extra examples are outside part 1.

## 4. Final build results (items 1–6)

All live approvals here were automated: scripted `yes` answers for the CLI and scripted Chromium clicks for the UI. The UI records `human (ui click)` as the input channel. These checks do not supply the separate human-run evidence listed in the README.

1. **PASS — Prompt-based rollback** ([`9195b7d`](https://github.com/kittu181707/rewind/commit/9195b7d)). `rewind do "roll back checkout_v2" --config examples/demo-shop/rewind.config.yaml` verified live Case A in **115.884 s** and Case B in **147.545 s** (runner time). Each used one Groq interpretation call and two scripted approvals. `rewind do "turn on the new checkout"` refused with exit 1, no evidence run, and identical rehearsal and staging snapshot hashes before and after. The offline tests covered ambiguity, unsupported and disallowed requests, and prompt injection reaching no writes after a declined Gate 1. Local evidence: `.artifacts/run-20261127T071036611772Z/run.json`, `.artifacts/run-20261127T071313320343Z/run.json`, and `.artifacts/final-build-item-1/enable-refusal.log`.
2. **PASS — Gate 2 and report display** ([`aef3b60`](https://github.com/kittu181707/rewind/commit/aef3b60)). All three regenerated terminal replays measured at most **80 columns**. The verified reports show the problem, verbatim diagnosis, verified fix, and effective flags per user before and after the fix. Case B marked exactly the two affected users’ checkout flags. Tests matched the original approval tokens, operation tuples, and full snapshot hashes.
3. **PASS — Autonomy ledger** ([`ae43530`](https://github.com/kittu181707/rewind/commit/ae43530)). The captured runs record **69**, **76**, and **97** autonomous steps for Case A, Case B, and Refusal respectively. The verified cases each reached two approval prompts. Refusal reached one and recorded zero staging writes. The ledger is visible in run JSON, reports, the terminal, and the UI final card.
4. **PASS — Documentation and shop screenshot** ([`3543b3d`](https://github.com/kittu181707/rewind/commit/3543b3d)). The [real Chromium shop screenshot](docs/screens/shop-vip-plain-rollback.png) shows Alice (VIP) with a saved v2 cart and HTTP 500 from legacy checkout after a plain rollback. The screenshot check did not write to staging. Both Flagsmith excerpts were checked against their source pages; the second excerpt ends in an editorial ellipsis.
5. **PASS — Local UI and offline replay** ([`d208555`](https://github.com/kittu181707/rewind/commit/d208555)). The milestone passed **142 offline tests**, including wrong gate IDs, duplicate clicks, bad origins, missing tokens, and timeouts or disconnects without staging writes. Chromium replayed Case A through both prompts by keyboard and Refusal in dark mode at 360 px. Screenshots: [Case A Gate 2](docs/screens/ui-case-a-gate2.png), 141,919 bytes; [mobile Refusal](docs/screens/ui-refusal-360-dark.png), 63,813 bytes. Live `rewind ui --config examples/demo-shop/rewind.config.yaml` verified Case A in **124.309 s** from submission to final card (**122.632 s** runner time) and Case B in **145.855 s** (**144.291 s** runner time). Local evidence: `.artifacts/run-20261127T073514632982Z/run.json` and `.artifacts/run-20261127T073804802399Z/run.json`.
6. **PASS — GitHub Pages; real Codespace blocked** ([`cee53da`](https://github.com/kittu181707/rewind/commit/cee53da)). The Python 3.12 devcontainer and Codespaces badge are in the repo. [The Pages deployment](https://github.com/kittu181707/rewind/actions/runs/45960503542) passed. Chromium completed Case B on the [published replay](https://kittu181707.github.io/rewind/) through both prompts; the report returned HTTP 200 and all replay requests stayed on the Pages site. `gh codespace list --repo kittu181707/rewind` returned HTTP 404 with an explicit missing `codespace` scope, and the browser opened GitHub’s sign-in page. A real Codespace run was not verified.

### Final-candidate SSE recheck

- `python -m pytest -q tests/test_web.py`: **5 passed**. The regression test checks that 15-second keepalives are unnumbered comments and preserve the normal event sequence. The full offline suite (`python -m pytest -q -m 'not live'`) passed **143 tests**.
- A fresh live Case A via `python -m rewind ui --config examples/demo-shop/rewind.config.yaml` verified on staging in **120.399 s** from submission to final card (**119.112 s** runner time). Chromium clicked both approvals and opened the report (HTTP 200). The SSE probe saw the first two `: keepalive` comments at **15.164 s** and **30.301 s** after connecting. Local evidence: `.artifacts/run-20261127T090947111737Z/run.json`. This was another scripted-click run.
