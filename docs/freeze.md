# Rewind agent freeze

- **Date:** 2026-11-26 UTC
- **Validated code:** `be6590feaabcfd0643450ad904dee84848fca643` (revision 1)
- **Model used:** Groq `openai/gpt-oss-120b`, temperature 0. Gemini `gemini-3.6-flash` was probed with synthetic tool messages; no variant completed. No fallback is configured for the demo.
- **Basis for freeze:** V1–V8 and audits a–f passed. V6 and V7 passed on their allowed full-run retries; their original provider errors are retained. The approvals were scripted. See [Day 2 notes](../DAY2_NOTES.md).

## Revision 1 prompt change

The first live V1 treated the desired legacy checkout as a defect and proposed returning VIP users to v2. The task now states the applied rollback flag and direction, includes the configured goal verbatim, and explicitly rejects moving any user away from that goal. The tool requires successful evidence, flag-state, and source reads before accepting a proposal, with one corrective reply for a premature submission. The model receives compact verifier evidence and flag state. These changes are generic; the task contains no repair hint for a particular case.

## SHA-256

```text
d1d621c015081874280a9e31fb8599c1e3893a555f3860b86101bc5cd7a94f4b  rewind/agent/prompt.py
cb4b54853049ac470bfe17cd2d50bb6a1ad864567f33be0666379f64cdb4faba  rewind/agent/tools.py
76dffd2f9f96bf21b4011ca2377b9660e6728fce0f0826bc47e078c50f535833  rewind/agent/llm.py
671e876b1b2dd7a76c6021a0a4c8acdc0ab1144a26faca712a8d6d7b4c0d8bb8  rewind/orchestrator.py
feeea41402532fecaeac1685343ce05cc882c48ccc0ad436817e6efd25fe8f51  rewind/planning.py
9e4a59ece4f361ee607ad18719786094b5636a3349988aa92ce8e9719686b2e1  rewind/verifier.py
386ceab68b97027ded2e1e8c9d830a0756c1d367db1740ab2aa40636f70f64c9  examples/demo-shop/journey.py
```

After a freeze, change prompt, tools, and decision logic only to fix verified bugs, and list each such change in the README. Case B was not run as part of revision 1.

## Revision 2 — Refusal protocol

- **Date:** 2026-11-27 UTC
- **Tag:** `agent-freeze-2`; `agent-freeze` still identifies revision 1.
- **Validated code:** `ae5cc7611a4dec17c83f4974675e1253afdf4b63`; Groq `openai/gpt-oss-120b`, temperature 0, no configured fallback.
- **Verified bug:** Under revision 1's JSON-answer instruction, Groq twice rejected an attempted call to an undeclared `json` tool with HTTP 400. Revision 2 instructs the model to call the declared `report_no_fix(reason)` tool. It is a terminal call with the existing read prerequisites, one corrective reply, and a required, redacted reason of at most 1,000 characters. The tested legacy plain and fenced JSON parser remains for compatibility. Rollback evaluation, proposal checks, and approval decisions are unchanged.
- **Revalidation:** 114 offline tests, three scripted Case A runs, three scripted Refusal runs, and all six audits passed. Every scenario passed on its first run; zero whole-run retries and zero HTTP 400 responses. All three live Refusals exhausted their three verifier-rejected proposals, so acceptance of `report_no_fix` itself is covered by offline tests. Local evidence: `.artifacts/validation-20261127T013434540822Z/validation.json`.

### Revision 2 SHA-256

```text
cac0455343264f5765319593ac0c1f92e9a5e01be61442804f132c6573ae0502  rewind/agent/prompt.py
cec9d28ef55358214bcc7354b9944b980410ec9e252c19dd7418d0d434bc327e  rewind/agent/tools.py
76dffd2f9f96bf21b4011ca2377b9660e6728fce0f0826bc47e078c50f535833  rewind/agent/llm.py
671e876b1b2dd7a76c6021a0a4c8acdc0ab1144a26faca712a8d6d7b4c0d8bb8  rewind/orchestrator.py
feeea41402532fecaeac1685343ce05cc882c48ccc0ad436817e6efd25fe8f51  rewind/planning.py
9e4a59ece4f361ee607ad18719786094b5636a3349988aa92ce8e9719686b2e1  rewind/verifier.py
386ceab68b97027ded2e1e8c9d830a0756c1d367db1740ab2aa40636f70f64c9  examples/demo-shop/journey.py
```

## Changes after revision 2

Compared against `agent-freeze-2` (`25dfcc1`):

- `rewind/orchestrator.py`: [`9195b7d`](https://github.com/kittu181707/rewind/commit/9195b7d) added the optional validated `do` request, its per-run flag and direction override, the interpretation at Gate 1, and redacted request and interpretation-call records. [`aef3b60`](https://github.com/kittu181707/rewind/commit/aef3b60) added a display-only Gate 2 preview from the recorded rehearsal evidence and the planner’s existing per-user flag predictions. [`ae43530`](https://github.com/kittu181707/rewind/commit/ae43530) records displayed approval prompts and derives the autonomy ledger when saving evidence and finishing the terminal run. [`d208555`](https://github.com/kittu181707/rewind/commit/d208555) added an optional observer for sanitized request, progress, failure, attempt, and gate events. Its gate payloads show the ordered plan and the starting snapshot; an observer error does not decide the run. The proposal, retry, drift, and verification decisions are the same as in revision 2.
- `rewind/approvals.py`: [`dc0fef8`](https://github.com/kittu181707/rewind/commit/dc0fef8) switched the displayed Gate 1 and Gate 2 text to the shared terminal renderers while retaining the summaries stored in the tokens. [`d208555`](https://github.com/kittu181707/rewind/commit/d208555) added the `BrowserApproval` input adapter and the `human (ui click)` label. The terminal still requires a TTY; the browser adapter waits for the exact pending gate. Token issuance, operation order, snapshot binding, single-use enforcement, and failure-signature checks are unchanged.
- `rewind/agent/llm.py`: [`9195b7d`](https://github.com/kittu181707/rewind/commit/9195b7d) added `complete_once` for the separate Groq request interpreter: one call at temperature 0 with no retry or fallback. The diagnosis adapter’s existing `complete` path is unchanged.
- `rewind/flagsmith_ops.py`: [`d208555`](https://github.com/kittu181707/rewind/commit/d208555) added read-only `flag list` inventory for configured environments, with feature and identity override selectors. Inventory can display flags outside the write allowlist; write validation and token enforcement are unchanged.

The 15-second SSE keepalive is confined to `rewind/web/server.py` and does not add a run event or change approval handling.

### Final-candidate SHA-256 — changed files

```text
27bc4f4fa52985151caa806b6bca6fcc2b1e0dcc849c21d78e27ace7365082bf  rewind/orchestrator.py
594a815f633d960788fdd9171d2606daba580bc13a6c9320afceb9283ee5a75f  rewind/approvals.py
2a1851dbcb0ca489450751f5db47132753a1280f076b650856053321c0783673  rewind/agent/llm.py
1a3c639c69d466c27140b97a5994d3e002faac8084ab37cc14603fa4465d7f0f  rewind/flagsmith_ops.py
```

### Final-candidate SHA-256 — exact revision 2 matches

The bytes in these five files match the corresponding blobs at `agent-freeze-2` and the revision 2 manifest above.

```text
cac0455343264f5765319593ac0c1f92e9a5e01be61442804f132c6573ae0502  rewind/agent/prompt.py
cec9d28ef55358214bcc7354b9944b980410ec9e252c19dd7418d0d434bc327e  rewind/agent/tools.py
feeea41402532fecaeac1685343ce05cc882c48ccc0ad436817e6efd25fe8f51  rewind/planning.py
9e4a59ece4f361ee607ad18719786094b5636a3349988aa92ce8e9719686b2e1  rewind/verifier.py
386ceab68b97027ded2e1e8c9d830a0756c1d367db1740ab2aa40636f70f64c9  examples/demo-shop/journey.py
```
