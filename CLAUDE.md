# CV3 AI Shopping Engine

**Read Completed.md and PROGRESS.md before believing anything else here.**
Completed.md is what's built and verified end to end. PROGRESS.md is what's
still broken, unbuilt, or open. This file is architecture, invariants, and
practices — it does not track current status.

CommerceV3 runs e-commerce for ~50 mid-market merchants on custom builds,
Magento and Shopware — not Shopify, not demo stores. That fact drives the
"real-client conditions" below: every feature must work at real catalogue
size (40k+ SKUs) and real case volume, not just on the two demo merchants.

## Product Direction

- **Shopper:** help the customer buy better.
- **Merchant:** help the merchant sell better.
- **CV3 Operations:** help CV3 operate multiple merchants better.

Shared loop: Observe → Understand → Decide → Risk-check → Act → Measure → Learn.
New features must use this loop and must not bypass the Decision Engine, Risk
Gate, tenant isolation, payment protections, or auditability.

AI/reasoning proposes. Deterministic systems decide. Risk Gate controls what's
allowed. Adapters execute. Outcomes are measured. A feature that stops after
"reason about the situation" is a report, not a feature — see the value bar.

## Roadmap (high level — detail belongs in PROGRESS.md/Completed.md once selected)

- **Shopper:** advanced discovery, comparison, personalization, mission
  shopping, cart/checkout recovery, bundles/compatibility/return-risk/
  post-purchase, multimodal, proactive/agentic-commerce capabilities.
- **Merchant:** copilot, store diagnosis, recovery radar, catalog/inventory
  intelligence, revenue-at-risk, voice-of-customer, return intelligence,
  promotion/search/recommendation intelligence, policy builder/simulator,
  approval center, automated briefs.
- **Operations:** ops copilot, cross-merchant command center, integration
  health monitoring, incident detection, SLA monitoring, playbooks,
  evaluation/benchmarking, ROI reporting.

Only move an item into PROGRESS.md when it's actually planned or in progress.
A roadmap item's name is not a spec — write one (six value-bar answers, which
real-client conditions apply, which figure moves) with `feature-spec` before
writing code.

**Reply-before-data constraint (blocks Shopper items 1–4 on V1):**
`engine.reasoning.reason()` writes the shopper-facing reply before
`execute_case()` runs (`chat.py`), so any reply describing data fetched
during the turn is written before that data exists and can only be generic.
Do not build Advanced Discovery, Comparison, Personalization, or Mission
Shopping against V1 until this is fixed. V2 fixes this structurally
(`engine/shopper_v2/understanding.py` discards the pre-fetch reply before
retrieval runs) but V2's own retrieval layer has measured gaps of its own —
close those first. See PROGRESS.md's Shopper V2 section for current state.

## Architecture

```
browser (5173) → engine (8000) → adapter → merchant (8001 or 8002)
```

Four stages: **Reasoning** (model proposes actions only) → **Decision**
(ranks proposals, drops what the platform can't do) → **Risk** (eleven-rule
gate: automatic / needs approval / blocked) → **Execution** (adapter carries
it out, tells the shopper).

Two demo merchants prove platform independence: **Northfield Running Co.**
(`conn_demo`, REST, port 8001, no payment recovery) and **Kettle & Bloom
Coffee** (`conn_kettle`, GraphQL, port 8002, supports recovery). Everything
above the adapter works identically for both; behavior differs only by what
each platform declares (capabilities), never by a branch in engine code.

```
engine/reasoning/    the model, prompts, context building
engine/decision/     ranking and capability filtering
engine/risk/         the eleven-rule gate and merchant policy
engine/execution/    carrying actions out
engine/api/          routes, auth, accounts, shopper sessions
engine/db/           models, repository, keys, shopper accounts
engine/session/      conversation memory
engine/shopper_v2/   target conversational architecture (see below)
engine/notify/       order-confirmation mail (fails soft without MAILER_SMTP_HOST)
adapters/            one per platform
shared/              commerce interface and action types
storefront/          React, Vite
```

Two walkthroughs live in `Readme.MD`: "Adding a platform" and "Adding an
action" (six steps; steps 1-2, `ActionType` + `ACTION_RISK_PROPERTIES`, are
the security-critical pair). `scripts/` is gitignored scratch, not tracked.

### Shopper V2

Route `/api/v2/chat` (`engine/api/chat_v2.py`), package `engine/shopper_v2/`.

Shopper V2 is the target shopper architecture. New shopper functionality
should be implemented in V2 unless explicit compatibility or migration work
requires V1 (`engine/api/chat.py`). Current routing/cutover status belongs
in PROGRESS.md and Completed.md, not here.

**V2 owns:** understanding (message → typed `StateDelta`), state
(`ShoppingStateV2`, a typed view over the same `shopping_states` table),
context assembly, retrieval, evidence, response grounding. It can act
(cart proposals, checkout, payment recovery) but every action still only
constructs a candidate (`PROPOSE_CART_CHANGE` / `PREPARE_CHECKOUT` /
`CHECK_ORDER_STATUS` / recovery) — never a direct mutation. A cart only
changes after the shopper's own call to `POST /api/chat/proposals/{id}/accept`.

**V2 must never import or bypass:** the Decision Engine, Risk Gate,
execution's mutation paths, adapters' write methods, or anything cart/
checkout/payment — those stay exactly as authoritative for V2 as for V1.
See `engine/shopper_v2/orchestrator.py`'s docstring for the enforced
boundary.

Technology choices (semantic retrieval, vector search, reranking, Postgres,
LangGraph, etc.) are evidence-gated: adopt only when a measured gap in the
baseline report justifies it, never by default. Do not fix retrieval
correctness through an endless sequence of phrase-specific keyword patches —
use the typed contracts and evaluation corpus instead.

## Security Invariants (never break these)

1. **The AI cannot assert risk.** No field for it — risk properties come
   from a static table keyed by action type.
2. **Money never moves unattended.** `FINANCIAL_ALWAYS_HUMAN` fires before
   any merchant policy; a merchant cannot switch it off.
3. **Payment has no action type.** `PREPARE_CHECKOUT` only reads a cart and
   shows a total; only the shopper's own tap reaches `/api/chat/pay`.
4. **A tap is trusted; the model's guess is not.** `chosen_variant` from a
   button beats `variant_id` from the model.
5. **Raw platform errors never reach shoppers.**
6. **Tenant and shopper isolation.** One merchant's data is never reachable
   with another's key; one shopper's cart/conversation/order is never
   reachable by another shopper holding the same key. Enforced by an
   httpOnly visitor cookie and `db.owners`, checked on every relevant route.
7. **Idempotency.** A cart is claimed in `db.idempotency` at creation, keyed
   on the cart id alone (never per-card or per-attempt). A paid cart cannot
   be charged twice, cannot inherit another cart's paid status after id
   reuse, and cannot be mutated after payment.

## Feature Completion — the value bar

A feature is not done until all six are answered, in a spec, before building:

1. **Whose decision changes?** Name the person, not "the merchant."
2. **What can they do now that they couldn't before?** If it's "see the same
   thing, phrased differently," stop.
3. **What does it cost if this is wrong?**
4. **Where is the action?** Name it and how it's reached in one step, or the
   feature is informational and must be named as such.
5. **What number moves?** Name the figure in the merchant report / ops
   stats.
6. **Does it survive a real client?** See below.

**Real-client conditions** — check all four before calling anything done:

- **Zero data.** New merchant, no history — must render something honest,
  not an empty panel or a confident 0%.
- **Real volume.** 40k-SKU catalogue, six-figure case count — anything that
  reads "all" of something or silently truncates to a fixed page is wrong.
- **A platform that cannot.** Missing capability degrades honestly and says
  why.
- **A platform that is down.** Adapter raises; the turn degrades, doesn't break.

**Named anti-patterns** — recognizing one is grounds to stop and re-spec:

- **Q&A panel** — free-text box answering from data already on screen; never presents as an implementation of a roadmap item.
- **Read-only twin** — the informational version of a feature, marked done.
- **Demo-grade slice** — works on the one walkthrough path, fails real-client conditions.
- **Unreachable claim** — not reachable through the actual UI at real data volume.
- **Generic reply** — text written before the data it describes exists.
- **Unmeasured feature** — ships with no figure that moves.

## Documentation Contract

- **CLAUDE.md** = project rules, architecture, constraints, practices.
- **PROGRESS.md** = current unfinished/partial/open work only.
- **Completed.md** = completed, verified, value-bar-cleared capabilities only.
- ROADMAP ≠ PROGRESS ≠ COMPLETED.

### Documentation Maintenance Standard

PROGRESS.md and Completed.md are concise operational documents, not
historical logs.

PROGRESS.md contains only: current unfinished work, partial work, current
limitations, pending work, honest completion status for major areas.

Completed.md contains only: major implemented capabilities, integrated and
verified functionality, value-bar-cleared work.

Do not add: session history, debugging narratives, commit-by-commit history,
temporary experiments, duplicate entries, repeated verification reports.

When updating an existing feature, modify its existing entry — don't create
another status entry. When a feature becomes complete: update/remove it in
PROGRESS.md, add/update it in Completed.md. Never turn either file into a
changelog. Git history is the historical record. Before editing either file,
read its current contents and preserve this style; if either grows
unnecessarily long, consolidate rather than append.

## Database and Process Safety

`cv3.db` is one SQLite file with exactly one owner (the single engine
process). SQLite assumes a single writer and has no built-in protection
against concurrent opens.

- **Never hardlink or otherwise share `cv3.db` between worktrees/processes.**
  A task needing isolation gets its own `DATABASE_URL` or a fresh DB via
  `mint_keys.py`.
- **No git worktrees for day-to-day work.** Work directly on `main`; keep it
  runnable at every commit. Don't create a worktree unless explicitly asked.
- **Prefer graceful shutdown over `taskkill /F`** — only forceful when the
  process genuinely has no graceful path (headless Windows console process).
- **Stop the engine before switching branches or resetting state**; confirm
  nothing still holds `cv3.db` open before any destructive DB operation.
- **Back up before anything destructive** to `cv3.db` — a plain file copy is enough.
- **Don't change SQLite's journal mode casually.** WAL mode needs a written
  reason, a named checkpointer, and a kill-safety plan.
- **Diagnose and fix DB problems at the source.** No sleep/retry/poll/
  frontend workaround papers over a backend problem.

## Working Practices

- **Verify against the object, not the file.** Check `Model.model_fields`,
  `inspect.signature`, or run the thing — don't trust a string match against
  a file the patch may have failed to write.
- **Read the signature before calling it.**
- **When the browser disagrees with the source, restart Vite first.**
- **Ask for the exact error before reasoning about the cause.**
- **Walk the product, don't just read the test summary.**
- **Tests must assert against real observed output**, not a guess at what
  the bug produces — confirm by hand against both broken and fixed code.
- **Watch for local variables shadowing module-level state** of the same name.
- **A "recorded once, never revisited" outcome bug is a shape, not a
  one-off** — grep every caller of the same recording function before
  calling the class of bug fixed, not just the instance you were shown.
- **A feature can pass every gate and still not be worth having shipped.**
  invariant-guard catches dangerous; value-auditor catches pointless. Run both.

## Tests

All three suites drive the **running** services over HTTP — start the four
processes first (see Running It).

```
python healthcheck.py     # PASS/FAIL per check; SKIP if provider busy
python fuzz.py             # 20 random shopper sequences; --seed N to replay
python auditroutes.py      # route locking + tenant/shopper scoping
```

`testshopper.py` is the one tracked `test*.py`; every other `test*.py` and
`scripts/` are gitignored scratch — don't trust them as source of truth.

Frontend, from `storefront/`:

```
npm run build    # tsc -b && vite build — the actual typecheck guard; dev does NOT catch type errors
npm run lint
npm run dev       # port 5173
```

**The Groq free tier throttles at ~4 model turns/minute.** `healthcheck.py`
now exceeds what fits in that window, so a full run may not complete
reliably — tapping paths are model-free by design and keep working while
throttled.

## Running It

Four processes from project root, with `.venv\Scripts\Activate.ps1` first:

```
python -m uvicorn sample_merchant.api.main:app --port 8001
python -m uvicorn sample_merchant_two.api.main:app --port 8002
python -m uvicorn engine.api.main:app --port 8000
cd storefront ; npm run dev
```

Then `python demo_reset.py`, and in the browser console `sessionStorage.clear()`
followed by a hard reload. **Restart the engine after any change under
`engine/`.** Restart Vite after any new file or `.env.local`/`vite.config.ts` change.

Routes: `/northfield`, `/kettle` (shops), `/signin`/`/signup` (shopper
accounts), `/merchant` (client console), `/operations` (CV3 queue).

`python mint_keys.py` writes five keys to `.env.keys` (gitignored,
unrecoverable). `python patch_auth_4c.py` copies publishable keys into
`storefront/.env.local`. Deleting `cv3.db` destroys the hashes — rerun both.

## Security

Three key kinds: publishable (browser), secret (per merchant), operator
(CV3). Every route is locked or deliberately public; `connection_id` is
checked against the key so a caller can't claim to be a merchant they aren't.

## Agents

Project subagents live in `.claude/agents/`:

- **adapter-builder** — adds a new merchant platform folder.
- **action-builder** — adds a new `ActionType` through all six pipeline steps.
- **invariant-guard** — read-only check of a diff against the security invariants + idempotency rule.
- **test-runner** — runs healthcheck/fuzz/auditroutes against running services.
- **frontend-verifier** — runs `npm run build` + lint, then walks the feature by hand.
- **progress-scribe** — updates PROGRESS.md/Completed.md per the maintenance standard above.
- **bug-reproducer** — reproduces a bug and records the exact call/response before any fix is written.
- **doc-auditor** — checks CLAUDE.md/Completed.md/PROGRESS.md against actual code for drift.
- **feature-spec** — writes the six value-bar answers before any roadmap code.
- **value-auditor** — checks a finished feature against the value bar, real-client conditions, and anti-patterns.

**Wiring (do this even if not asked):**

- Change under `engine/` → `invariant-guard` + `test-runner`.
- Change under `storefront/` → `frontend-verifier`.
- Roadmap feature start → `feature-spec`. Roadmap feature completion → `value-auditor`.
- Any session touching `engine/`, `storefront/`, `adapters/`, or a roadmap
  item's status → `progress-scribe` before ending.

invariant-guard and value-auditor catch different failures — dangerous vs.
pointless — neither substitutes for the other.
