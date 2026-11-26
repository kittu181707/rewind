# Rewind

**Off is not undo.**

> Rewind checks whether turning a feature off really restores the old experience without breaking saved user data. With your approval, it rehearses the transition on an isolated test copy of your app using real Flagsmith configuration. If it fails, the agent investigates the evidence and tests up to three configuration-only recovery plans. A separate approval is required before a verified plan is applied to the target environment.

*(Skeleton README — full walkthrough, live proof, and sample report coming on Day 3.)*

## Flagsmith Authentication

* **Standard machines:** Run `flagsmith login` to authenticate via browser into your OS keychain, or export `FLAGSMITH_API_KEY` (a Flagsmith Master API Key) in headless/CI environments.
* **This WSL dev environment:** Uses a headless-keyring wrapper at `/usr/local/bin/flagsmith` backed by `gnome-keyring` so `flagsmith login` credentials persist across subshells. Liveness check: `flagsmith environment list`.

## Prior Work

A pre-event throwaway spike in `~/rewind-spike` verified the Flagsmith CLI v2.0.0 mutation syntax, remote identity evaluation, and edge-convergence behavior before hacking began.
