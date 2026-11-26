# Rewind — Day 2 handoff before live validation

- **Step 0, A–C:** Rehearsal session approval, autonomy documentation, provider adapter, and read-only diagnosis tools are on `origin/main` at `faaad77`.
- **D:** `rewind check --config` now runs the rehearsal, records every attempt, and offers one ordered and snapshot-bound Gate 2 plan. It checks for drift before each target write, and only unlocks recovery when the target reproduces the recorded failure. It saves evidence under `.artifacts/run-*/run.json` and never automatically reverts the target.
- **E:** The scripted-model integration tests cover successful recovery, identity rejection, wrong-scope retry and baseline restore, a mixed per-user segment repair, refusal, both denied gates, no reproduction, and post-apply failure. The selected WSL offline suite reported `74 passed`. The recursive source scan was not run as part of this validation.
- **F/G:** No live model or Flagsmith calls were made in D/E. Case B was not run with an agent. The live acceptance runs and `agent-freeze` tag are pending.

## Start the interactive Case A run in WSL

```bash
cd ~/rewind
.venv/bin/rewind seed --case a
.venv/bin/rewind check --config examples/demo-shop/rewind.config.yaml
```

Seed again before each run; seeding is rehearsal-only. Run `rewind check` from a terminal and review each plan before typing `yes`. The scenario uses dedicated demo environments and a new disposable database per environment and run.

For Refusal, seed `--case refusal` and set `APP_VARIANT=no_compat` for `rewind check`. Compare the target snapshot hashes separately before and after each live Refusal, since the refusal path in Rewind does not read or write the target. The built-in plan projection covers Boolean flag operations and equality-based segment conditions, not arbitrary Flagsmith rules or multivariate values.
