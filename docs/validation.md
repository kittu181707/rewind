# Validation & Agent Revisions

Rewind has been verified across three scenarios (**Case A**, **Case B**, and **Refusal**) on Flagsmith Cloud (`rehearsal` and `demo-target`) and offline unit suites. All live approvals in the automated validation batches below used **scripted approvals** (`yes` replies or scripted browser clicks), with real Groq (`openai/gpt-oss-120b`, temperature `0`) and Flagsmith CLI/SDK calls.

---

## 1. Deterministic Baseline Verification (No LLM)

Verified `scripts/verify_e2e.py` across Case A, Case B (`set_off` and `delete_override`), Refusal (`APP_VARIANT=no_compat`), and Target (`demo-target`) using `seed_flagsmith.py`, `demo-shop`, `verifier.py`, and `flagsmith_ops.py` (15/15 assertions passing).

| Scenario | Action | Expected | Observed | alice | carol | bob | Check |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Case A | Rollback only | FAIL (500 loud) | FAIL | HTTP 500 / legacy | HTTP 500 / legacy | HTTP 200 / legacy | PASS |
| Case A | `identity:alice` compat | FAIL (carol 500) | FAIL | HTTP 200 / legacy | HTTP 500 / legacy | HTTP 200 / legacy | PASS |
| Case A | compat `environment_default` `set_on` | PASS | PASS | HTTP 200 / legacy | HTTP 200 / legacy | HTTP 200 / legacy | PASS |
| Case B | Rollback only (silent) | FAIL (alice/carol v2) | FAIL | HTTP 200 / v2 | HTTP 200 / v2 | HTTP 200 / legacy | PASS |
| Case B | `environment_default` `set_off` | FAIL (still v2) | FAIL | HTTP 200 / v2 | HTTP 200 / v2 | HTTP 200 / legacy | PASS |
| Case B | `identity:alice` `set_off` | FAIL (carol v2) | FAIL | HTTP 200 / legacy | HTTP 200 / v2 | HTTP 200 / legacy | PASS |
| Case B | `segment:vip_users` `set_off` | PASS | PASS | HTTP 200 / legacy | HTTP 200 / legacy | HTTP 200 / legacy | PASS |
| Case B | `segment:vip_users` `delete_override` | PASS | PASS | HTTP 200 / legacy | HTTP 200 / legacy | HTTP 200 / legacy | PASS |
| Refusal | Rollback only (`no_compat`) | FAIL (500) | FAIL | HTTP 500 / legacy | HTTP 500 / legacy | HTTP 200 / legacy | PASS |
| Refusal | `cart_compat_reader` `environment_default` `set_on` | FAIL (`no_compat`) | FAIL | HTTP 500 / legacy | HTTP 500 / legacy | HTTP 200 / legacy | PASS |
| Refusal | `cart_compat_reader` `segment:vip_users` `set_on` | FAIL (`no_compat`) | FAIL | HTTP 500 / legacy | HTTP 500 / legacy | HTTP 200 / legacy | PASS |
| Refusal | `checkout_v2` `segment:vip_users` `set_off` | FAIL (`no_compat`) | FAIL | HTTP 500 / legacy | HTTP 500 / legacy | HTTP 200 / legacy | PASS |
| Refusal | `checkout_v2` `segment:vip_users` `delete_override` | FAIL (`no_compat`) | FAIL | HTTP 500 / legacy | HTTP 500 / legacy | HTTP 200 / legacy | PASS |
| Target | Case A rollback only | FAIL (500 loud) | FAIL | HTTP 500 / legacy | HTTP 500 / legacy | HTTP 200 / legacy | PASS |
| Target | compat `environment_default` `set_on` | PASS | PASS | HTTP 200 / legacy | HTTP 200 / legacy | HTTP 200 / legacy | PASS |

---

## 2. Agent Revision 1 Outcomes & Safety Audits

Commit `be6590f` passed the 104-test offline preflight and all eight live scenarios (**V1–V8**). Times exclude setup, outer snapshot reads, and inter-run spacing (45 s). Model time is HTTP time; retry waits are listed separately. Sanitized captures: [Case A, V4.1](captured-runs/case-a/run.json) and [Refusal, V8.1](captured-runs/refusal/run.json).

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

The provider error on V6.1 and V7.1 was: `HTTP 400: Tool call validation failed: tool call validation failed: attempted to call tool 'json' which was not in request.tools`. Each passed on its one allowed retry. Measured prompt size: **548–3,376 tokens**, with **73 of 74** usage-reporting calls at or below 3,000.

### Verbatim Case A Diagnoses (Revision 1)

- **V1:** “Legacy checkout fails on v2 carts because cart_compat_reader is off” — `cart_compat_reader / environment_default / set_on`. Rehearsal passed; Gate 2 was denied.
- **V3:** “VIP users have v2 carts but cart_compat_reader is off, causing legacy checkout to reject the schema.” — `cart_compat_reader / segment:vip_users / set_on`.
- **V4:** “Legacy checkout fails for v2 carts because cart_compat_reader flag is off, causing schema error.” — `cart_compat_reader / environment_default / set_on`.
- **V5:** “Legacy checkout fails for VIP users because cart_compat_reader is off, causing schema errors” — `cart_compat_reader / segment:vip_users / set_on`.

V3–V5 each made four approved flag writes and recorded nine fixture writes on target; post-run target snapshots matched each approved plan.

### Safety Audits (a–f)

- **Audit a — PASS:** 109 captured files and 152 Git-history objects scanned. Zero matches for non-empty `.env` credentials (`GROQ_API_KEY`, `GEMINI_API_KEY`, `FLAGSMITH_ENV_KEY_REHEARSAL`, `FLAGSMITH_ENV_KEY_TARGET`) and zero OAuth/login URL matches.
- **Audit b — PASS:** In rehearsal and target probe subprocesses, only `FLAGSMITH_ENV_KEY` and `DB_PATH` were present; model keys, management key, and separately named environment keys were absent.
- **Audit c — PASS:** All 74 CLI writes were restricted to `rehearsal` and `demo-target`.
- **Audit d — PASS:** V6, V7, and V8 passed with zero target CLI writes and identical before/after target hashes; Gate 2 was never shown.
- **Audit e — PASS:** `read_source` refused `journey.py`, `verifier.py`, `.env`, `../..`, an absolute path, and a symlink.
- **Audit f — PASS:** V3 used 2 of 11 Gate 1 operations and closed cleanly; log entries match its CLI writes (`checkout_v2 / environment_default / set_off` at `2026-11-26T17:21:54.097826+00:00`, `cart_compat_reader / segment:vip_users / set_on` at `2026-11-26T17:22:08.600479+00:00`).

### Historical Pre-Revision-1 Failure & Gemini Probes

- Prior to revision 1, `V1.1` stopped before Gate 2 on commit `a65038b` when Groq diagnosed: “Missing segment override for checkout_v2 on vip_users segment, leaving VIP users on legacy version” and proposed `checkout_v2 / segment:vip_users / set_on` without reading source.
- Synthetic Gemini probes (`gemini-3.6-flash` unmodified timed out after `30.261 s`; schema without `additionalProperties` returned HTTP `503` after `1.051 s`; named-tool-message variant returned HTTP `503` after `5.803 s`). Fallback remains disabled in config for the demo.

---

## 3. Agent Revision 2 — Refusal Protocol Revalidation & Case B

Commit `ae5cc76` (tagged `agent-freeze-2` at `25dfcc1`) passed 114 offline tests, added `report_no_fix(reason)`, and achieved **zero whole-run retries and zero HTTP 400s** across 3/3 Case A and 3/3 Refusal runs (Refusal runs stopped cleanly with unchanged target hashes after three verifier-rejected proposals; direct acceptance of `report_no_fix` is covered by offline tests).

| Run | Scenario | First-run result | Agent attempts | Run (s) | Model HTTP (s) | Prompt / completion tokens |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| V3.1 | Case A, both gates approved | Verified | 1 | 86.101 | 5.150 | 5,718 / 1,181 |
| V4.1 | Case A, both gates approved | Verified | 1 | 78.882 | 5.787 | 5,724 / 881 |
| V5.1 | Case A, both gates approved | Verified | 1 | 78.169 | 6.527 | 5,718 / 936 |
| V6.1 | Refusal | Refused; target unchanged | 3 | 107.421 | 18.887 | 16,894 / 3,362 |
| V7.1 | Refusal | Refused; target unchanged | 3 | 89.894 | 16.525 | 15,582 / 2,792 |
| V8.1 | Refusal | Refused; target unchanged | 3 | 85.197 | 16.375 | 16,799 / 2,993 |

### Case B with the Frozen Agent (`agent-freeze-2`)

| Run | Result | Agent attempts | Run (s) | Model HTTP (s) | Prompt / completion tokens | Target hash before / after |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| B1.1 | Verified | 1 | 98.122 | 4.649 | 5,279 / 628 | `141c5a5d...` / `141c5a5d...` |
| B2.1 | Verified | 1 | 124.939 | 4.844 | 5,279 / 643 | `141c5a5d...` / `141c5a5d...` |
| B3.1 | Verified | 1 | 109.494 | 5.450 | 5,279 / 647 | `141c5a5d...` / `141c5a5d...` |

Full before/after SHA-256 on target for B1.1–B3.1: `141c5a5d62978c3215c169d1a7addc717f7528f702cf2731a2beb97d533743c2` (hashes match because target replay temporarily recreated the `vip_users` failure and then returned to the verified clean state; every user journey was independently verified). Captured evidence: [Case B, B1](captured-runs/case-b/run.json).

**Verbatim diagnoses and actual proposal:**

- **B1.1:** “Segment override for vip_users forces checkout_v2 on, overriding the rollback to off.” — `checkout_v2 / segment:vip_users / delete_override`
- **B2.1:** “A segment override for vip_users forces checkout_v2=true, overriding the rollback default.” — `checkout_v2 / segment:vip_users / delete_override`
- **B3.1:** “A segment override for vip_users forces checkout_v2=true, overriding the rollback default.” — `checkout_v2 / segment:vip_users / delete_override`

---

## 4. UI, Replay, and Post-Freeze Feature Validation (`final-candidate`)

Offline samples: [Case A report](sample-report-case-a.html), [Case B report](sample-report-case-b.html), [Refusal report](sample-report-refusal.html). Subprocesses of `rewind demo` with credentials cleared completed in **9.179 s** (Case A) and **9.305 s** (Refusal).

At `final-candidate` (`dd87bf8`), **143 offline tests** passed, with live **scripted** validations confirming:

1. **Prompt-based rollback (`9195b7d`):** `rewind do "roll back checkout_v2"` verified Case A in **115.884 s** and Case B in **147.545 s**; `rewind do "turn on the new checkout"` refused cleanly (exit 1, target unchanged).
2. **Gate 2 preview & terminal width (`aef3b60`):** Terminal replays stayed within 80 columns and highlighted before/after per-user flag visibility.
3. **Autonomy ledger (`ae43530`):** Captured runs record **69** (Case A), **76** (Case B), and **97** (Refusal) autonomous steps against two approval prompts (one in Refusal).
4. **Shop screenshot (`3543b3d`):** Chromium captured Alice's VIP v2 cart returning HTTP 500 under plain rollback (`screens/shop-vip-plain-rollback.png`).
5. **Local web UI & SSE keepalives (`d208555`, `d41eb19`):** Case A verified live via `rewind ui` in **120.399 s** from submission to final card (**119.112 s** runner time) with `: keepalive` SSE comments at **15.164 s** and **30.301 s**; Case B verified in **145.855 s** (**144.291 s** runner time).
6. **GitHub Pages replay (`cee53da`):** Pages deployment succeeded and completed Case B playback in Chromium; Codespaces creation was skipped (`404`, missing `codespace` token scope).

---

## 5. Frozen Files & SHA-256 Checksums

### Revision 1 (`agent-freeze`, `be6590f`)

```text
d1d621c015081874280a9e31fb8599c1e3893a555f3860b86101bc5cd7a94f4b  rewind/agent/prompt.py
cb4b54853049ac470bfe17cd2d50bb6a1ad864567f33be0666379f64cdb4faba  rewind/agent/tools.py
76dffd2f9f96bf21b4011ca2377b9660e6728fce0f0826bc47e078c50f535833  rewind/agent/llm.py
671e876b1b2dd7a76c6021a0a4c8acdc0ab1144a26faca712a8d6d7b4c0d8bb8  rewind/orchestrator.py
feeea41402532fecaeac1685343ce05cc882c48ccc0ad436817e6efd25fe8f51  rewind/planning.py
9e4a59ece4f361ee607ad18719786094b5636a3349988aa92ce8e9719686b2e1  rewind/verifier.py
386ceab68b97027ded2e1e8c9d830a0756c1d367db1740ab2aa40636f70f64c9  examples/demo-shop/journey.py
```

### Revision 2 (`agent-freeze-2`, `25dfcc1`)

```text
cac0455343264f5765319593ac0c1f92e9a5e01be61442804f132c6573ae0502  rewind/agent/prompt.py
cec9d28ef55358214bcc7354b9944b980410ec9e252c19dd7418d0d434bc327e  rewind/agent/tools.py
76dffd2f9f96bf21b4011ca2377b9660e6728fce0f0826bc47e078c50f535833  rewind/agent/llm.py
671e876b1b2dd7a76c6021a0a4c8acdc0ab1144a26faca712a8d6d7b4c0d8bb8  rewind/orchestrator.py
feeea41402532fecaeac1685343ce05cc882c48ccc0ad436817e6efd25fe8f51  rewind/planning.py
9e4a59ece4f361ee607ad18719786094b5636a3349988aa92ce8e9719686b2e1  rewind/verifier.py
386ceab68b97027ded2e1e8c9d830a0756c1d367db1740ab2aa40636f70f64c9  examples/demo-shop/journey.py
```

### Core Decision & Verification Files Locked at `final-candidate` (`dd87bf8`)

These five files keep exact `agent-freeze-2` bytes:

```text
cac0455343264f5765319593ac0c1f92e9a5e01be61442804f132c6573ae0502  rewind/agent/prompt.py
cec9d28ef55358214bcc7354b9944b980410ec9e252c19dd7418d0d434bc327e  rewind/agent/tools.py
feeea41402532fecaeac1685343ce05cc882c48ccc0ad436817e6efd25fe8f51  rewind/planning.py
9e4a59ece4f361ee607ad18719786094b5636a3349988aa92ce8e9719686b2e1  rewind/verifier.py
386ceab68b97027ded2e1e8c9d830a0756c1d367db1740ab2aa40636f70f64c9  examples/demo-shop/journey.py
```

Additional `final-candidate` files changed after revision 2 (`orchestrator.py` request interpretation, Gate 2 preview, ledger, observer; `approvals.py` browser adapter and shared prompt formatter; `llm.py` `complete_once`; `flagsmith_ops.py` read-only inventory):

```text
27bc4f4fa52985151caa806b6bca6fcc2b1e0dcc849c21d78e27ace7365082bf  rewind/orchestrator.py
594a815f633d960788fdd9171d2606daba580bc13a6c9320afceb9283ee5a75f  rewind/approvals.py
2a1851dbcb0ca489450751f5db47132753a1280f076b650856053321c0783673  rewind/agent/llm.py
1a3c639c69d466c27140b97a5994d3e002faac8084ab37cc14603fa4465d7f0f  rewind/flagsmith_ops.py
```

## Validation Commands

```bash
python -m pytest -q -m 'not live'
python -m rewind demo
python -m rewind demo --case refusal
python -m scripts.validate_live --batch refusal-protocol
python -m scripts.validate_live --batch case-b
python -m scripts.verify_e2e
```

## 6. Presentation, UI, & Final Release Validation (`final-candidate-2`)

All presentation, UI design, customer-view, storefront/report restyling, and asset verification commits stacked on `final-candidate` (`dd87bf8`):

- `47d8ba2 chore(repo): consolidate validation docs, add connect-your-app guide, untrack AGENTS.md, and rename scripts`
- `0e62d5b docs(readme): rewrite concise 70-line README with Flagsmith links and TODO markers`
- `9c64223 style(ui): replace starter CSS with token-based design system and 360px support`
- `e546e58 feat(ui): implement topbar, Flagsmith inventory rows, vertical timeline, and mobile gate layout`
- `6507cd7 feat(ui): render per-user customer view from recorded journey steps in live and replay UI`
- `b2d5bf1 style(shop,report): restyle demo shop storefront and align HTML report styling with UI design tokens`
- `a06f7a7 docs(ui,reports): regenerate replay bundle, sample reports, and verified screenshots`
- `fd568d1 docs(demo): add live port 8003 storefront walkthrough beside Gate 2 replay`

- **Offline suite:** `python -m pytest -q -m 'not live'` -> **147 passed**.
- **Browser verification (Playwright Chromium):** Case A and Case B replayed through both approvals via keyboard (`Enter`); Refusal verified at `360x760` dark with zero overflow; regenerated `docs/screens/*.png` all verified under `300 KB`; live scripted Case A via `rewind ui` verified cleanly.
- **Optional live storefront alongside replay:** Port `8003` (`docs/live-shop-demo.md`) measured a **10.66 s** HTTP 500 window (`measured once`) without touching target drift snapshots or approvals.
- **Frozen hashes & secret scan:** All five frozen files match `agent-freeze-2` SHA-256 bytes verbatim; tree and Git history secret scans recorded zero matches across all `.env` keys and zero auth/launcher tokens.
