# Shopper acceptance matrix

Verification record for the committed Shopper AI Assistant (2026-10-25). It records what was RUN and what each result does and does not show; it is not a roadmap and does not
declare anything production-ready. Feature status lives in `Completed.md` / `PROGRESS.md`.

**Test types.** `MF` model-free (deterministic, no provider; isolated database, local demo merchants). `FP` fake-provider (a local OpenAI-compatible fake that streams; proves wiring, not model
quality). `LM` live model (needs a real provider key - NOT RUN, no budget authorised). `RP` real platform (a merchant's real system - NOT RUN, none available). Browser = Playwright against the
isolated stack. Every run used a disposable copy of `cv3_v2verify.db`; the protected databases were byte-identical before and after.

| # | Journey | Steps (abridged) | Expected | Type | Evidence (suite) | Status |
|---|---|---|---|---|---|---|
| 1 | Discovery / category browse | ask for a product class; ask what the shop sells | real catalogue items only, no leakage across merchants, honest no-match | MF, Browser | `test_store_discovery`, `test_multi_product_discovery`, discovery-routing/relevance specs | Pass (model-dependent steps self-skip) |
| 2 | Comparison | compare 2 shown; compare 3+; sold-out member; mixed currency | named products fetched fresh, no cheapest across currencies, sold-out labelled, ratings shown, failed fetch honest | MF | `test_phase3_compare`, `test_superlative_honesty` (added) | Pass for ranking/fallback; the model-written comparison text is LM, not run |
| 3 | Guided mission | plan shoes/socks/energy, size ask, advance, summary | stage order kept, no auto-add, closes cleanly | MF, Browser | `test_phase7_missions`, `test_step14_*`, `test_complete_set_*`, guided-mission specs | Pass |
| 4 | Budgets | basket budget, over-budget, cheaper alternatives | remaining recomputed from the real cart, honest verdict | MF | `test_budget_*` (11 files) | Pass (per-product cap is not a separately labelled case) |
| 5 | Cart management | typed add; ambiguous add asks; sized add asks size; clear; remove | mutation only by accept/typed unambiguous add; one change per command | MF, Browser | `test_explicit_add_executes`, `test_step17_*`, `test_phase6_cart_change_operations`, closeout/proposals specs | Pass |
| 6 | Checkout / payment safety | pay; ambiguous outcome; second card; recover; recycled cart id | UNKNOWN_OUTCOME holds, no double charge, fixed wording, one alert | MF, Browser | `test_payment_*`, payment-hold spec, fuzz | Pass against the DEMO platforms only; RP not run |
| 7 | Order lookup | by id; no id as owner; new conversation; other shopper; other merchant | owner finds own newest; strangers and other merchants find nothing | MF | `test_phase6_order_status`, `test_order_recovery_by_owner` | Pass |
| 8 | SSE stream | events in order; reconnect; replay; timeout; late retry; restart; disconnect | one execution per request id, no duplicate transcript, fixed error text | MF, FP, Browser | `test_chat_stream_endpoint` (7b-7f added), `test_llm_streaming`, `test_turn_stream_sink`, chat-streaming/sse-parser specs | Pass; real-provider streaming is LM, not run |
| 9 | Restart behaviour | resume (Last-Event-ID>0) after the in-memory runs are gone | refuse, never re-execute | MF | `test_chat_stream_endpoint` 7d | **Defect found and fixed** (see below); process restart is simulated by clearing the registry, not by killing the engine |
| 10 | Concurrent turns | streamed + plain turn on one session at once | both finish, nothing lost or duplicated | MF | `test_chat_stream_endpoint` 7e | Pass; transcript is in COMPLETION order (the slower streamed turn lands last) |
| 11 | Memory | list; forget one/all; supersede; other shopper/merchant; guest | own rows only; newer value replaces; guest has none | MF, Browser (mocked) | `test_shopper_memory_control`, shopper-memory spec | Pass; a live turn storing a fact and that fact changing a later ranking are LM, not run |
| 12 | Tenant / shopper isolation | every route family, two cookie jars, cross-merchant keys | refused, 404 for not-yours | MF, Browser | `auditroutes.py`, `fuzz.py`, `test_tenant_isolation_v2`, `*_tenant_isolation`, tenant-isolation spec | Pass |
| 13 | Platform outage | search/add/cart read fail; merchant down | honest reply, no raw error, no half-written cart | MF, Browser | `test_zero_data_platform_down`, `test_budget_platform_failure`, `test_chat_stream_endpoint` 7f (added: add fails mid-turn), cart-restore spec | Pass for add/read; search-down and checkout-down on Kettle are covered only indirectly; service-stopping browser specs NOT RUN |
| 14 | 40k-SKU volume | see below | | MF | `test_*_real_volume` | Structural + one engine-side measurement; no retrieval or platform benchmark |

## Run record (this commit)
- Model-free scripts: 102 files, 1,987 checks, 0 failures; `fuzz.py` 1,200 assertions; `auditroutes.py` clean; HTTP walkthrough 7/7.
- Browser (isolated stack, batches of 1-6 specs, memory checked first): streaming 10 + parser 7, payment-hold 10, chat widget/closeout/proposals 14, mission/discovery/relevance 22, tenant/cart-restore/memory 21,
  checkout Leg A 1, merchant/ops 23 of 25.
- Failed in the merchant batch (not regressions of this work; the isolated database lacks what they read): `merchant-cart-suggestions` "zero friction cases" expects accepted Kettle proposals that only the long-lived demo
  database holds; `return-risk-ui` needs a real discovery answer from a live model.
- NOT RUN: live-model tests (`healthcheck.py` is model-throttled; the tapped-card and real-chat-question specs), real-platform and real-payment tests, service-stopping platform-down specs, checkout Leg B.

## Defects found by this audit
1. **Resume after restart re-ran the turn** (confirmed: a resume with `Last-Event-ID > 0` for an unknown run executed the add again and wrote a second transcript entry). Fixed: such a request is refused `STREAM_RUN_LOST`.
2. A model-free comparison did not label a sold-out product. Fixed.

## Weak or stale tests found (not all fixed)
- Vacuous `check(..., True)` "honestly skipped" passes in `test_phase11_measurement`, `test_phase12_*`, `test_phase13_*`, `test_phase9_tradeoff`, `test_query_intent_consolidation`; `test_phase9_topic_transition:148` asserts nothing. They pass with no model and prove nothing.
  (One in the stream test, `... or True`, was fixed.) `testshopper.py` is a manual script, not a suite.
- Completed.md's "27 files" under `tests/shopper_v2/` was stale (about 100 now).

## 40k-SKU path (separate assessment)
- **Structural (assert call counts / wall time with a 40,000-product fixture):** `test_budget_real_volume`, `test_complete_set_real_volume` (each ~6 s) - no unbounded scan, bounded platform reads.
- **Measured here (synthetic, single thread, engine-side only):** relevance scoring over 40,000 in-memory products took p50 ~1.3 s, p95 ~1.6 s per query; `pick_superlative` ~0.3 s, `sort_products` ~0.4 s; peak extra Python memory ~5 MB.
  This is the cost IF an adapter handed the engine the whole catalogue; adapters return bounded result sets, so it is a ceiling, not a typical turn.
- **Not measured:** retrieval QUALITY at volume (the eval corpus is 20 cases on the demo catalogues), real platform search latency, end-to-end turn latency, concurrency or memory under load, vocabulary sampling at volume.

## Release blockers (unchanged by this audit)
Payment safety is verified only against demo platforms (real idempotency/lookup semantics unknown); no live-model verification of streaming, grounding gates or memory-driven ranking; 40k-SKU quality/latency/concurrency
unmeasured; stream replay state and the duplicate-request guard are in one process's memory (a restart loses them; resumes are now refused, first attempts are not deduplicated across a restart);
no CI gate runs any of this.
