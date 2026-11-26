# Rewind — Day 2 handoff before live validation

- **Step 0, A–C:** Rehearsal session approval, autonomy documentation, provider adapter, and read-only diagnosis tools are on `origin/main` at `faaad77`.
- **D:** `rewind check --config` now runs the rehearsal, records every attempt, and offers one ordered and snapshot-bound Gate 2 plan. It checks for drift before each target write, and only unlocks recovery when the target reproduces the recorded failure. It saves evidence under `.artifacts/run-*/run.json` and never automatically reverts the target.
- **E:** The scripted-model integration tests cover successful recovery, identity rejection, wrong-scope retry and baseline restore, a mixed per-user segment repair, refusal, both denied gates, no reproduction, and post-apply failure. The selected WSL offline suite reported `74 passed`. The recursive source scan was not run as part of this validation.
- **F/G:** No live model or Flagsmith calls were made in D/E. Case B was not run with an agent. The live acceptance runs and `agent-freeze` tag are pending.

## Historical first validation (before revision 1)

- `V1.1` stopped before Gate 2 on commit `a65038b`. Groq diagnosed: “Missing segment override for checkout_v2 on vip_users segment, leaving VIP users on legacy version”. It proposed `checkout_v2 / segment:vip_users / set_on` after reading the evidence and flag state, without reading source.
- The verifier rejected the v2 checkout, and rehearsal restored its baseline hash before attempting diagnosis again. Groq returned two HTTP 429s; Gemini fallback returned HTTP 400. The exact provider error retained was `gemini/gemini-3.6-flash chat completion: HTTP 400`. The read-only target hash matched before and after.
- Historical evidence: `.artifacts/validation-20261126T165847750593Z/validation.json`, `V1.1.log`, and `.artifacts/run-20261126T165933847806Z/run.json`. This is a failed validation attempt, not a captured success. All revised results must be measured from V1 again.
- Revision 1's Gemini probe used the real schema structure and synthetic content. The unmodified variant timed out after 30.261 s; the schema without `additionalProperties` returned HTTP 503 after 1.051 s; the named-tool-message variant returned HTTP 503 after 5.803 s. All failed on the initial request; no continuation or token counts were obtained. The demo fallback is disabled.

## Start the interactive Case A run in WSL

```bash
cd ~/rewind
.venv/bin/rewind seed --case a
.venv/bin/rewind check --config examples/demo-shop/rewind.config.yaml
```

Seed again before each run; seeding is rehearsal-only. Run `rewind check` from a terminal and review each plan before typing `yes`. The scenario uses dedicated demo environments and a new disposable database per environment and run.

For Refusal, seed `--case refusal` and set `APP_VARIANT=no_compat` for `rewind check`. Compare the target snapshot hashes separately before and after each live Refusal, since the refusal path in Rewind does not read or write the target. The built-in plan projection covers Boolean flag operations and equality-based segment conditions, not arbitrary Flagsmith rules or multivariate values.
