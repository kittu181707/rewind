# How Rewind uses Flagsmith

I wired Rewind to Flagsmith using distinct environment SDK keys, remote identity evaluation, and an allowlisted CLI wrapper.

| Flagsmith feature | What Rewind does with it | What breaks without it |
| --- | --- | --- |
| Environments | Binds each configured name to its own SDK key. Rehearsal and target run against different flag settings and different test databases. | A rehearsal write could affect the target, so the separation that Gate 1 promises would not hold. |
| Identities and traits | The shop evaluates flags for each named test user with their stored traits. It records the effective flags alongside the actual checkout path and never changes user traits to mask a failure. | Checking an environment default alone cannot show what an individual user actually received or whether only one of several affected users was repaired. |
| Segments and segment overrides | Case B starts with an override that keeps the VIP segment on the new checkout after the default is off. The frozen agent identifies the override and the verifier checks the affected users and control after its removal. | Case B's failure, and the distinction between fixing an override and flipping the default again, would not be exercised. |
| Environment defaults | The rollback changes the checkout flag's default. A separate compatibility flag can enable an existing reader for carts saved by the new checkout. | Rewind could not reproduce the `off` transition or verify a configuration-only compatibility change. |
| Flagsmith CLI | Reads project environments, segment rules, flag defaults, and overrides for snapshots, and applies exact approved default and override operations. I used the Flagsmith CLI instead of the MCP server because every write is an allowlisted argument list run without a shell. | Rewind could not read the state needed for drift checks or execute the plans it verifies. |
| Audit log | Where available, the Flagsmith dashboard's own audit log is an independent cross-check of flag changes. Rewind records its CLI commands, approval and verification evidence; it does not query the dashboard audit log. | Operators lose the provider-side cross-check. The verifier and Rewind's own run evidence do not depend on that log. |
| Remote evaluation | Each user journey evaluates flags remotely. I disabled local evaluation because the SDK normally polls the environment definition every 60 seconds. Rewind waits for three matching evaluations, then reads flags and the executed path from the actual cart or checkout response. | A stale local snapshot or a default-only check could hide the behavior that follows the approved write. Remote changes still take time to propagate; an earlier matching evaluation alone is not proof. |

## Command evidence

These distinct command strings are from the [Case A](captured-runs/case-a/run.json), [Case B](captured-runs/case-b/run.json), and [Refusal](captured-runs/refusal/run.json) captures. Environment keys are redacted. The `--yes` flag tells the CLI to run without another prompt; Rewind checks its own approvals before runtime writes.

```text
flagsmith environment list --json
flagsmith segment list --json
flagsmith flag get cart_compat_reader --environment <REDACTED> --json
flagsmith flag get cart_compat_reader --segment vip_users --environment <REDACTED> --json
flagsmith flag get checkout_v2 --environment <REDACTED> --json
flagsmith flag get checkout_v2 --segment vip_users --environment <REDACTED> --json
flagsmith flag update cart_compat_reader --disable --environment <REDACTED> --yes
flagsmith flag update cart_compat_reader --enable --environment <REDACTED> --yes
flagsmith flag update cart_compat_reader --enable --segment vip_users --environment <REDACTED> --yes
flagsmith flag update checkout_v2 --disable --environment <REDACTED> --yes
flagsmith flag update checkout_v2 --enable --environment <REDACTED> --yes
flagsmith flag update checkout_v2 --enable --segment vip_users --environment <REDACTED> --yes
flagsmith flag delete cart_compat_reader --segment vip_users --environment <REDACTED> --yes
flagsmith flag delete checkout_v2 --segment vip_users --environment <REDACTED> --yes
```

**Set an override OFF** (`flag update ... --disable --segment ...`) keeps the override, so users in that segment still get OFF if the environment default later changes. **Remove an override** (`flag delete ... --segment ...`) lets those users inherit the ordinary evaluation result. Rewind names the operation in the approval and the report; these two writes are not interchangeable.

**Flagsmith Change Requests are not supported.** Rewind's two approval gates govern its own CLI writes and do not submit or approve Flagsmith Change Requests. Use dedicated staging environments that allow direct writes.
