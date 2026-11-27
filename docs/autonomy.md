# What Rewind can do on its own

| Rewind does alone | Rewind stops and asks a person |
| --- | --- |
| Read flag state, allowlisted source, and journey evidence. | **Gate 1:** Approve the rehearsal environment, allowed changes, and operation budget. |
| After Gate 1, rehearse the rollback, diagnose failures, propose and apply a permitted rehearsal change, and verify the journey. | **Gate 2:** Approve the exact target changes and fixture replay against the displayed starting snapshot. |
| Stop without touching target if no configuration-only plan passes. | **Drift:** Stop and require a fresh Gate 2 if target settings changed. |
| Record the checks, approvals, and changes made. | **Target failure:** Report what changed and ask before any recovery action. Never revert automatically. |

The autonomy ledger counts successful Flagsmith and agent reads, recorded rehearsal flag and fixture writes, completed journey checks (including failed checks), and model calls (including retries). It counts shown approval prompts and confirmed staging flag and fixture writes separately. An uncertain write stays visible in the change log even though it does not count as a confirmed write.
