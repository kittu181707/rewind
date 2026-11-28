# Rewind

Off is not undo. Rewind rehearses turning a feature flag off on an isolated test copy, investigates failures, tests up to three setting fixes, and waits for two human approvals before changing staging.

![Rewind web UI showing Flagsmith inventory, failure evidence, and approval gate](docs/screens/ui-case-a-gate2.png)

## Why I built this

TODO: Add why I built this (user answers pending).

## What it does

1. With your first approval, Rewind saves test user carts on the new path and switches the feature flag off in rehearsal.
2. If checkout fails or users stay on the new path, the agent reads allowlisted source and Flagsmith settings to propose one fix.
3. The verifier checks every configured user after three matching Flagsmith reads, rejecting fixes that leave any user broken.
4. With your second approval on the exact operations and starting snapshot, Rewind reproduces the failure on staging, applies the fix, and verifies again.

## Try it

[![Open in Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/kittu181707/rewind?quickstart=1)

Open the [captured browser replay](https://kittu181707.github.io/rewind/) or run `rewind ui --replay` locally (`pip install -e . && rewind ui --replay`) for a captured run with no keys required.

## Run it live

| Variable | Purpose |
| --- | --- |
| `GROQ_API_KEY` | Groq API key for `openai/gpt-oss-120b` diagnosis |
| `FLAGSMITH_ENV_KEY_REHEARSAL` | Client SDK key for the `rehearsal` environment |
| `FLAGSMITH_ENV_KEY_TARGET` | Client SDK key for the `demo-target` staging environment |
| `FLAGSMITH_API_KEY` | Optional CLI management key in place of `flagsmith login` |
| `APP_VARIANT` | Optional shop flag (`no_compat` reproduces the refusal case) |

```bash
flagsmith environment list
rewind seed --case a
rewind check --config examples/demo-shop/rewind.config.yaml
```

## How it uses Flagsmith

| Flagsmith feature | Role in Rewind |
| --- | --- |
| Environments | Separates rehearsal experiments from staging (`demo-target`) using distinct SDK keys. |
| Remote identity eval & traits | Checks per-user effective flags without local SDK cache delays or trait edits. |
| Defaults & segment overrides | Tests compatibility defaults and removes group overrides that block rollbacks. |
| CLI & audit log | Reads snapshots and applies approved changes; see [docs/how-rewind-uses-flagsmith.md](docs/how-rewind-uses-flagsmith.md). |

As noted in [Flagsmith's rollback strategy](https://www.flagsmith.com/blog/rollback-strategy) ("Rehearse the rollback in staging.") and [Flagsmith's automatic rollback guide](https://www.flagsmith.com/blog/automatic-rollback) ("A rollback that touches a database or reopens a fixed bug needs a person..."), Rewind keeps a person in charge at both gates.

## Limits & next steps

- Pass results cover the tested journey and fixture database only, not all production traffic or database migrations.
- Approvals use scripted validation in automated captures; interactive human run evidence pending.
- Approvals are enforced by an in-process approval registry (`approvals.py`) and Flagsmith Change Requests are not supported.
- The planner models Boolean equality rules only and Gemini fallback is disabled in live runs.
- Next steps include recording interactive human approvals, adding another example app, and supporting multi-condition rules.

## Docs

[Validation & checksums](docs/validation.md) | [Connect your app](docs/connect-your-app.md) | [Sample reports](docs/sample-report-case-a.html) ([Case B](docs/sample-report-case-b.html), [Refusal](docs/sample-report-refusal.html))

## AI tools used

TODO: Add AI tools used (user answers pending).

## License

[MIT](LICENSE)
