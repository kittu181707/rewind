# Rewind — Day 2 validation

**Revision 1 passed on November 26, 2026 UTC.** Commit `be6590f` passed the 104-test offline preflight and all eight live scenarios. `rewind check` rehearses a rollback, records each attempt, checks for drift before each target write, and applies recovery only after the target reproduces the failure. Nothing reverts automatically. The verdict covers the tested fixture journey on the allowlisted staging target.

- Full local evidence: `.artifacts/validation-20261126T171754693086Z/validation.json` and one transcript per attempt. It includes all run paths, per-call provider metrics, pre/post snapshots, and the target change inventories.
- Sanitized captures: [Case A, V4.1](docs/captured-runs/case-a/run.json) and [Refusal, V8.1](docs/captured-runs/refusal/run.json). Both are **scripted validation runs**, with real Groq and Flagsmith calls.
- [Freeze manifest](docs/freeze.md): checksums and revision history for the tested logic.

## Revision 1 outcomes

Each row is a distinct execution. Times exclude setup, outer snapshot reads, and inter-run spacing. Model time is HTTP time; retry waits are recorded separately. Token totals are the usage returned by the provider, so failed HTTP calls with no usage do not contribute.

| Run | Scenario | Observed result | Agent attempts | Run (s) | Model HTTP (s) | Retry waits (s) | Prompt / completion tokens |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| V1.1 | Case A: Gate 2 denied | PASS; target hash unchanged, zero target changes | 1 | 29.187 | 3.957 | 0 | 4,236 / 666 |
| V2.1 | Case A: Gate 1 denied | PASS; after seeding, both environment hashes stayed unchanged, with no writes or model calls | 0 | 0.001 | 0 | 0 | 0 / 0 |
| V3.1 | Case A: both gates approved | PASS; VIP compatibility override, first proposal verified on target | 1 | 85.132 | 6.762 | 0 | 5,484 / 832 |
| V4.1 | Case A: both gates approved | PASS; environment default compatibility, first proposal verified on target | 1 | 88.310 | 6.403 | 0 | 5,490 / 879 |
| V5.1 | Case A: both gates approved | PASS; VIP compatibility override, first proposal verified on target | 1 | 81.674 | 7.443 | 0 | 5,482 / 992 |
| V6.1 | Refusal | FAIL; Groq HTTP 400 during tool validation, no target writes | 0 completed | 21.296 | 5.699 | 0 | 3,786 / 525 |
| V6.2 | Refusal retry | PASS; exact refusal sentence, Gate 2 absent, target unchanged | 3 | 97.385 | 18.240 | 0 | 17,588 / 2,931 |
| V7.1 | Refusal | FAIL; same Groq HTTP 400, no target writes | 0 completed | 22.488 | 5.697 | 0 | 3,782 / 593 |
| V7.2 | Refusal retry | PASS; exact refusal sentence, Gate 2 absent, target unchanged | 3 | 139.940 | 26.205 | 22 | 24,782 / 3,878 |
| V8.1 | Refusal | PASS; exact refusal sentence, Gate 2 absent, target unchanged | 3 | 110.691 | 22.799 | 3 | 19,997 / 3,785 |

The provider error on V6.1 and V7.1 was: `HTTP 400: Tool call validation failed: tool call validation failed: attempted to call tool 'json' which was not in request.tools`. Each scenario passed on its one allowed retry. In the three passing Refusal runs the verifier rejected three proposals before Rewind refused. The model sometimes repeated the same compatibility operation; these runs demonstrate safe refusal after failed verification, not reliable recognition of the missing code path by the model.

Measured prompt size: **548–3,376 tokens**, with **73 of 74** usage-reporting calls at or below 3,000. Five HTTP calls returned no usage. The highest measured Case A call was 1,705 prompt tokens.

### Case A diagnoses (verbatim)

- **V1:** “Legacy checkout fails on v2 carts because cart_compat_reader is off” — `cart_compat_reader / environment_default / set_on`. Rehearsal passed; Gate 2 was denied.
- **V3:** “VIP users have v2 carts but cart_compat_reader is off, causing legacy checkout to reject the schema.” — `cart_compat_reader / segment:vip_users / set_on`.
- **V4:** “Legacy checkout fails for v2 carts because cart_compat_reader flag is off, causing schema error.” — `cart_compat_reader / environment_default / set_on`.
- **V5:** “Legacy checkout fails for VIP users because cart_compat_reader is off, causing schema errors” — `cart_compat_reader / segment:vip_users / set_on`.

V3–V5 each made four approved flag writes and recorded nine fixture writes on target. The independent post-run flag snapshot matched the approved plan on all three runs. The final staging state after V5 and the Refusal runs was `checkout_v2` OFF, `cart_compat_reader` default OFF with the `vip_users` override ON. No identity overrides remained.

## Safety audits

- **a — PASS:** 109 captured files and 152 Git-history objects scanned. Zero matches for the four non-empty `.env` values (`GROQ_API_KEY`, `GEMINI_API_KEY`, `FLAGSMITH_ENV_KEY_REHEARSAL`, `FLAGSMITH_ENV_KEY_TARGET`) and zero OAuth/login URL matches. The two sanitized captures were separately scanned before and after writing.
- **b — PASS:** In actual rehearsal and target probe subprocesses, `FLAGSMITH_ENV_KEY` and `DB_PATH` were present; both model credentials, the management key, and both separately named environment keys were absent.
- **c — PASS:** All 74 CLI writes (including rehearsal seeding) were limited to `rehearsal` and `demo-target`. Every distinct form below ran in both environments; environment keys are redacted.
- **d — PASS:** V6, V7, and V8 passed with zero target CLI writes and equal before/after target hashes. Gate 2 was never shown.
- **e — PASS:** `read_source` refused `journey.py`, `verifier.py`, `.env`, `../..`, an absolute path, and a symlink.
- **f — PASS:** V3 used 2 of 11 Gate 1 operations and closed. The log matches its CLI writes: at `2026-11-26T17:21:54.097826+00:00`, `checkout_v2 / environment_default / set_off`; at `2026-11-26T17:22:08.600479+00:00`, `cart_compat_reader / segment:vip_users / set_on`.

```text
flagsmith flag delete cart_compat_reader --segment vip_users --environment '<REDACTED>' --yes
flagsmith flag update cart_compat_reader --disable --environment '<REDACTED>' --yes
flagsmith flag update cart_compat_reader --enable --environment '<REDACTED>' --yes
flagsmith flag update cart_compat_reader --enable --segment vip_users --environment '<REDACTED>' --yes
flagsmith flag update checkout_v2 --disable --environment '<REDACTED>' --yes
flagsmith flag update checkout_v2 --enable --environment '<REDACTED>' --yes
```

## Historical first validation (before revision 1)

- `V1.1` stopped before Gate 2 on commit `a65038b`. Groq diagnosed: “Missing segment override for checkout_v2 on vip_users segment, leaving VIP users on legacy version”. It proposed `checkout_v2 / segment:vip_users / set_on` after reading the evidence and flag state, without reading source.
- The verifier rejected the v2 checkout, and rehearsal restored its baseline hash before attempting diagnosis again. Groq returned two HTTP 429s; Gemini fallback returned HTTP 400. The exact provider error retained was `gemini/gemini-3.6-flash chat completion: HTTP 400`. The read-only target hash matched before and after.
- Historical evidence: `.artifacts/validation-20261126T165847750593Z/validation.json`, `V1.1.log`, and `.artifacts/run-20261126T165933847806Z/run.json`. This failed attempt is separate from revision 1, which restarted at V1.
- Revision 1's Gemini probe used the real schema structure and synthetic content. The unmodified variant timed out after 30.261 s; the schema without `additionalProperties` returned HTTP 503 after 1.051 s; the named-tool-message variant returned HTTP 503 after 5.803 s. All failed on the initial request; no continuation or token counts were obtained. The demo fallback is disabled.

## Commands and known limits

Commands used for the offline and live checks, from WSL in `~/rewind`:

```bash
env TMPDIR=/tmp PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -B -m pytest -q -x -p no:cacheprovider -m 'not live'
env PATH=/usr/local/bin:/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m scripts.validate_day2
.venv/bin/python -B -m scripts.summarize_run docs/captured-runs/case-a/run.json docs/captured-runs/refusal/run.json
```

Recorded departures from the original `AGENTS.md` plan:

- The authorized validation harness used fixed scripted replies, in-process calls to the same `seed_case` and `run_check` used by the CLI, and 45-second spacing. It reseeded before every scenario and allowed one whole-run retry on a provider failure with no target writes. Actual interactive approvals are still required by the normal CLI.
- The model was given the configured rollback goal and compact evidence; proposals require successful evidence, flag-state, and source reads. A first premature proposal can receive one correction. The verifier still decides which rehearsal passes.
- The demo shipped with Groq only. The original Gemini probe returned HTTP 503; revision 1's three probes did not confirm tool compatibility. No successful fallback is claimed.
- The target replay uses one ordered, snapshot-bound approval and rechecks drift before each write. The approval registry is in-process; the model only has read and proposal tools. `planning.py` models identity precedence, configured segment priority, and defaults for Boolean flags with supported equality-based segment rules. It does not model arbitrary Flagsmith conditions or multivariate rollout values.
- Case B was not run with an agent. The HTML report and `rewind demo` remain Day 3 work.

To run an interactive Case A demonstration:

```bash
cd ~/rewind
.venv/bin/rewind seed --case a
.venv/bin/rewind check --config examples/demo-shop/rewind.config.yaml
```

Seed again before each run; seeding is rehearsal-only. Run `rewind check` from a terminal and review each plan before typing `yes`. The scenario uses dedicated demo environments and a new disposable database per environment and run.

For Refusal, seed `--case refusal` and set `APP_VARIANT=no_compat` for `rewind check`. Use `.venv/bin/python -B -m scripts.target_hash` to compare the target before and after; Rewind's refusal path does not read or write target state.
