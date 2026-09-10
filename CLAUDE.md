# CV3 AI Shopping Engine

Context for whoever picks this up. Written after a stretch of development that
produced real features and a real mess, and the honest version of both is more use
than a tidy summary.

**Read Completed.md and PROGRESS.md before believing anything else here.**
Completed.md is what's built and fixed, verified end to end - the durable
record. PROGRESS.md is the opposite list: what's still broken, unbuilt, or
open. Both are kept current every session; this file is architecture and
practices, and goes stale the moment it tries to describe current state
instead. The rule that keeps the split honest lives in PROGRESS.md's own
maintenance note - a finished item moves out of PROGRESS.md into Completed.md
once every test suite it needs has actually passed, **and once it clears the
value bar below** - not before.


## Research-Backed Product Roadmap

This section defines the future product direction for the CV3 AI Shopping Engine.

These are proposed features based on AI-commerce, shopper, merchant, and
agentic-commerce research. A roadmap item is NOT an implementation claim.

### Product Direction

The engine should evolve from a basic AI shopping assistant into a
commerce intelligence and action engine:

- Shopper: help the customer buy better.
- Merchant: help the merchant sell better.
- CV3 Operations: help CV3 operate and improve multiple merchants better.

The shared intelligence loop is:

Observe → Understand → Decide → Risk-check → Act → Measure → Learn

New capabilities should use the existing architecture wherever applicable:

browser/storefront
→ engine
→ reasoning
→ decision
→ risk
→ execution
→ adapter
→ merchant
→ outcome/measurement

New features must not bypass the existing Decision Engine, Risk Gate,
tenant isolation, payment protections, or auditability requirements.

---

## Shopper Roadmap

### Phase 1 — Core Shopping Intelligence

**Blocked until the reply-before-data constraint (see Feature Implementation
Rules) is addressed — items 1 through 4 below:**

1. **Advanced Product Discovery**
   - Outcome: a shopper describing what they need in their own words reaches a
     product they would not have found by keyword, and the reply says *why*
     that product fits their stated need, grounded in the record actually
     fetched during the turn — not a generic description written before the
     fetch happens.
   - Moves: dead searches down.

2. **Smart Product Comparison**
   - Outcome: a shopper comparing two named products is told which difference
     actually matters for what they said they wanted, grounded in the real
     fetched values.
   - Structured live fetch is done (Completed.md #27 — both products are
     always fetched fresh, never from anything cached earlier in the
     conversation). The synthesis — explaining meaningful differences rather
     than listing specifications — is not done, and cannot be done properly
     until the reply-before-data constraint is fixed. See PROGRESS.md.

3. **Personalized Recommendations**
   - Outcome: a returning shopper is shown something that visibly reflects
     what they've bought, kept and returned, and can tell that it does — not
     a recommendation that happens to be correct by coincidence.
   - Moves: resolution rate, returns.
   - Recommendations must remain grounded in available merchant data.

4. **Mission-Based Shopping**
   - Outcome: a shopper stating an overall goal ("I need a running setup for
     a marathon") gets a complete, compatible set of items with a reason per
     item, addable in one step — rather than treating every message as an
     isolated product query.
   - Moves: items per order.

**Not blocked:**

5. **Smart Cart and Checkout Recovery**
   - Outcome: a shopper who would have abandoned instead completes the
     purchase, because the specific thing stopping them — an unexpected cost,
     a return policy they couldn't find, uncertainty about a size — was
     answered before they left.
   - Moves: recovered revenue, resolution rate.
   - Preserve existing payment and shopper-confirmation invariants.

### Phase 2 — Deeper Shopping Assistance

6. **Smart Bundles / Complete the Set**
7. **Compatibility Assistant**
8. **Return-Risk Prevention**
9. **Post-Purchase Assistant**
10. **Multimodal Shopping**

These should focus on helping the shopper make a better decision while
avoiding unsupported product or compatibility claims. Write outcome statements
for each (see Feature Implementation Rules) before selecting any of these for
implementation — do not build from the name alone.

### Phase 3 — Advanced / Strategic Shopper Capabilities

11. **Proactive Shopping Assistance**
12. **Gift Assistant**
13. **Voice Shopping**
14. **Multi-Channel Shopper Continuity**
15. **External AI Shopping / Agentic-Commerce Channels**

These are later-stage capabilities and should not be treated as prerequisites
for the core engine.

---

## Merchant Roadmap

### Phase 1 — Merchant Intelligence

1. **CV3 Merchant Copilot**
   - *Partially built — see Completed.md #32, reopened in PROGRESS.md against
     the value bar.* The read-only answering half is done: it answers from
     report, capabilities, policy, rules, actions, unmet demand and catalogue
     alerts. Not done: every answer that identifies a problem must offer the
     action that fixes it, executable from the same panel through the
     existing risk gate. A merchant who asks "what's out of stock" and is
     simply told, with nothing to do about it from that screen, has not
     received a finished feature.
   - Also owed: `_catalog_alerts` calls `search_products("", limit=100)` on
     every request, correct for Northfield's small catalogue and wrong for a
     real client's — the real-volume condition has never been exercised for
     this feature.

2. **AI Store Diagnosis**
   - Outcome: a merchant is told a specific problem, the evidence behind the
     diagnosis, what it's costing them, and can act on it without leaving the
     screen. Not "here are some issues" — a diagnosis with no attached action
     is the read-only twin anti-pattern (see below), whatever it's called.

3. **Recovery Opportunity Radar**
   - Outcome: revenue currently being lost is surfaced with a specific,
     actionable case behind each instance, and a person can act on each one
     from the radar itself.
   - Moves: recovered revenue.

4. **Catalog Intelligence**
   - Outcome: a merchant is shown the specific products whose missing,
     inconsistent or weak information is costing them sales, ranked by what
     it's costing, and this must be demonstrated at real catalogue size, not
     just Northfield's demo catalogue.
   - Moves: dead searches down.

5. **Inventory Intelligence**
   - Outcome: a merchant learns which specific inventory position is about to
     cost them a sale, in time to do something about it, at real catalogue
     size.
   - Moves: dead searches, resolution rate.

### Phase 2 — Merchant Optimization

6. **Revenue-at-Risk Detection**
7. **Voice-of-Customer Intelligence**
8. **Return Intelligence**
9. **Return Prevention**
10. **Promotion / Discount Intelligence**
11. **Product Content Assistant**
12. **Search Intelligence**
13. **Recommendation Quality Monitoring**
14. **Shopper Segmentation**
15. **Customer Lifetime-Value Intelligence**

### Phase 3 — Merchant Decision and Automation Layer

16. **Natural-Language Merchant Policy Builder**
17. **Policy Simulator**
18. **Merchant Approval & Action Center**
19. **Merchant Action Audit Trail**
20. **Automated Merchant Daily Brief**
21. **AI Commerce ROI / Incrementality**
22. **Agent Readiness / Agentic Commerce**

Merchant automation must remain subject to the existing risk model and
merchant/platform capabilities. Financial actions remain human-controlled.

Write outcome statements for Phase 2 and Phase 3 items, following the pattern
above, at the point each is selected for implementation — not in advance and
not from the name alone.

---

## CV3 Operations Roadmap

### Phase 1 — Operations Intelligence

1. **CV3 Operations Copilot**
   - *Partially built — see Completed.md #33, reopened in PROGRESS.md against
     the value bar.* The read-only answering half is done: it answers from
     ops_stats, pending_across, handovers_across and decided_across, resolves
     connection ids to real merchant names, and correctly declines
     merchant-specific questions in favour of the Merchant Copilot. Not done:
     an operator should be able to approve, close, or escalate directly from
     the answer, through the existing routes and risk gate, rather than
     reading the answer and then going to the queue to act on it.

2. **Cross-Merchant Command Center**
   - Outcome: an operator covering ten accounts sees what needs them next
     across all of them, ordered by what it costs to ignore rather than by
     arrival time, and can work it without changing screens.

3. **Integration Health Monitoring**
   - Outcome: a broken adapter or platform connection is detected and named,
     with the specific failing operation identified, before a shopper hits it.

4. **Automatic Incident Detection**
   - Outcome: a systemic problem — one answer or one failure suddenly
     repeating across many sessions — is surfaced while it is happening, not
     in a later report. This is the failure mode that did not exist before
     agents: a single misconfiguration can now propagate identically across
     every conversation at once, invisibly, until someone happens to notice.

### Phase 2 — Operations Automation and Intelligence

5. **Cross-Merchant Revenue-at-Risk Radar**
6. **Incident Diagnosis**
7. **Adaptive Approval Queue**
8. **Operator Copilot**
9. **Cross-Merchant Pattern Detection**
10. **Playbook Engine**
11. **AI-Generated Resolution Playbooks**
12. **SLA Monitoring**
13. **SLA-Breach Prediction**
14. **Operations Daily Brief**
15. **Client Escalation Manager**
16. **Automated Client Performance Reporting**

### Phase 3 — Measurement, Evaluation and Strategic Operations

17. **AI Performance Monitoring**
18. **Decision Engine Monitoring**
19. **Risk Gate Monitoring**
20. **Adapter Performance Monitoring**
21. **AI Evaluation Lab**
22. **Shadow Mode**
23. **Regression Evaluation**
24. **Human-Decision Learning**
25. **Cross-Merchant Benchmarking**
26. **Automated ROI / Business-Impact Reporting**
27. **Client Opportunity Discovery**
28. **CRO / GEO Intelligence Feed**
29. **Agentic-Commerce Monitoring**

Write outcome statements for Phase 2 and Phase 3 items at the point each is
selected for implementation.

---

## Feature Implementation Rules

### The loop is not optional

Every roadmap feature runs the full loop:

1. Observe / retrieve relevant data
2. Reason about the situation
3. Produce candidate actions or recommendations
4. Apply deterministic decision logic
5. Apply the Risk Gate
6. Execute through the appropriate adapter when allowed
7. Record the outcome
8. Measure the result

A feature that stops after step 2 is a report, not a feature. It may still be
worth building — but it must be **named** as informational in its spec, and it
must name the action it leads to and make that action reachable from the same
screen in one step. "The operator can read it and go and do something about it
somewhere else" is not reachable.

The AI proposes. Deterministic systems decide what is allowed.

Existing security invariants always take precedence over new feature
requirements.

### The value bar

A feature is not done until all six of these have written answers. Answer them
in the spec **before** building, and re-check them before moving the item to
Completed.md.

1. **Whose decision changes?** Name the person — a shopper mid-purchase, a
   merchant's one marketer, a CV3 operator with ten accounts. Not "the
   merchant" as an abstraction.
2. **What can they do now that they could not do before?** If the honest
   answer is "see something they could already see, phrased differently",
   stop.
3. **What does it cost them if this is wrong?** A feature that cannot be wrong
   in a way that matters is not doing anything.
4. **Where is the action?** Name the specific action this leads to and how it
   is reached. If there is none, this is informational — see above.
5. **What number moves?** Name the figure in the merchant report or ops stats
   that changes when this works. If no existing figure moves, this feature
   owes one.
6. **Does it survive a real client?** See "Real-client conditions" below.

### Real-client conditions

CV3's clients are around fifty mid-market merchants on custom builds, Magento
and Shopware. Not Shopify, not demo stores. Every feature must be checked
against all four of these before it is called done:

- **Zero data.** A merchant connected this morning. No cases, no orders, no
  history. The feature must render something honest, not an empty panel, a
  divide-by-zero, or a confident 0%.
- **Real volume.** A 40,000-SKU catalogue and six figures of cases. Anything
  that reads "all" of something, or paginates with a fixed limit and presents
  the page as the whole, is wrong at this size. `_catalog_alerts`'s
  `search_products("", limit=100)` is the worked example: correct for
  Northfield, silently wrong for a real Magento client, and nothing in the old
  instructions would have flagged it.
- **A platform that cannot.** The operation this feature needs is not in that
  adapter's capabilities. The feature degrades honestly and says why, the same
  shape Northfield's missing payment recovery already uses.
- **A platform that is down.** The adapter raises. The turn degrades rather
  than breaking.

State in the spec which of the four were actually exercised and how. "It should
handle that" is not an answer; a run against a seeded empty merchant is.

### Named anti-patterns

These have all shipped here or nearly shipped. Recognising one is grounds to
stop and re-spec, not to proceed carefully.

- **The Q&A panel.** A free-text box answering from data already on the
  screen. It re-presents; it does not change anything. Allowed only as a layer
  over a feature that already acts — never as the implementation of a roadmap
  item.
- **The read-only twin.** Building the informational version of a feature and
  marking the roadmap item done. "AI Store Diagnosis" is not done by a panel
  that describes problems; it is done when a diagnosis leads to a fix.
- **The demo-grade slice.** Works for the one path a walkthrough takes. Fails
  the four real-client conditions above.
- **The unreachable claim.** Learned in Completed.md #25: reachable through the
  actual UI on every surface a real user would use. Extend it — also reachable
  at real data volume, not only on a seeded demo row.
- **The generic reply.** A reply written before the data it describes exists.
  See "The reply-before-data constraint" below.
- **The unmeasured feature.** Shipped with no figure that moves. There is now
  no way to tell whether it worked, so nobody will ever remove it either.

### The reply-before-data constraint

`engine.reasoning.reason()` writes the shopper-facing reply **before**
`execute_case()` runs (`chat.py:374` vs `:552`). So any reply describing data
fetched during the turn is written before that data exists, and can only ever
be generic.

This is not a bug in one action. It is a shape that caps the value of every
feature whose worth depends on saying something true about what was just
fetched — Advanced Product Discovery, Smart Product Comparison, Personalized
Recommendations, and Mission-Based Shopping are all limited by it.

**Do not build those four until this is addressed.** Building them against the
current shape produces exactly the generic output this document is trying to
stop. Closing it needs a second reasoning pass over the executed result, or a
different pipeline shape — it is architecture work, and it belongs at the top
of the shopper queue rather than inside a checkbox note. See PROGRESS.md,
Next Steps, item 0.

### Before building anything from the roadmap

Write a short spec first — six value-bar answers, which real-client conditions
apply and how they will be exercised, and which existing figure moves. Show it
before writing code. A roadmap item's **name** is not a spec, and building
straight from the name is how the thin version gets built. Use the
`feature-spec` agent for this.

---

## Roadmap Priority Rule

The roadmap is intentionally larger than the immediate implementation scope.

Do not add every roadmap item to PROGRESS.md.

Only move a feature into PROGRESS.md when implementation of that feature is
actually planned and work has begun or is explicitly queued for the current
development phase.

A feature remains a roadmap item until it is selected for implementation.

A feature becomes completed only when it is implemented end-to-end, relevant
tests pass, the real user-facing behavior has been verified, **and it clears
the value bar above** — a feature can pass every test it has and still not be
done, if what it tests is correctness rather than whether it changed anything
for anybody.

ROADMAP ≠ PROGRESS ≠ COMPLETED.

- CLAUDE.md roadmap = future product direction
- PROGRESS.md = currently unfinished/unbuilt/pending work
- Completed.md = completed, verified, and value-bar-cleared work



---

## Why this exists

CommerceV3 is an agency running e-commerce for around fifty mid-market merchants.
They are not on Shopify - custom builds, Magento, Shopware - which matters more than
it sounds and comes up again below.

### The thing that changed the market

In January 2026 Google and Shopify made catalogue search, cart building and checkout
free platform infrastructure. Every paid Shopify store has an agent-facing endpoint
live by default. So an assistant that answers product questions, recommends things
and adds them to a basket is now a commodity: a merchant on Shopify gets it for
nothing.

That is why an ordinary assistant reads as ordinary. It is not that too little was
built - the platforms started giving away what it does.

### What the research found

Across a dozen-plus on-site assistants and several 2026 consumer studies, one
finding reframes the product:

> **63% of shoppers want AI help while shopping. 4% want it at the point of
> payment.** Same survey, same respondents.

Corroborated elsewhere: 27% trust no organisation to operate a shopping agent, 24%
say they will never delegate a purchase, and comfort drops as soon as the AI acts on
its own. Meanwhile every vendor in the market is racing toward exactly the autonomy
shoppers have refused.

Three more numbers that shaped decisions here:

- **69%** of shoppers who get one irrelevant suggestion give up and search elsewhere
- **71%** want help judging whether a claim is credible, and one in five avoid
  Amazon's assistant believing it is upselling them
- **74%** want comparison - the most wanted capability, and the one a seller's
  assistant is assumed not to do

From the merchant side: serious merchants want proof the AI drove the sale rather
than the customer who would have bought anyway. Slides promising a conversion lift
get nods and no follow-up. **Vendors who cannot answer that lose the deal.**

### The position that follows

Everyone competes on capability. Nobody competes on proof.

Three things here were built for plain correctness reasons and turn out to be what
the research says both audiences want:

1. **Money cannot be automated.** Built so a model could not issue a refund. It is
   the property 96% of shoppers are asking for.
2. **Every decision names the rule that produced it.** Built for debugging. It is
   the audit trail that makes a managed service defensible, and what a shopper needs
   before believing a recommendation.
3. **Only products actually fetched from live inventory are shown.** Built so the
   model could not invent one. 69% abandon after a single bad suggestion.

**CV3's unfair advantage is being an agency with an operations team.** Every
software competitor must be fully autonomous because they have no people. CV3 can
sell "your problem got sorted" rather than "here is a tool" - which makes the
approval queue the product rather than a limitation, and it is the one thing a
software vendor cannot copy.

This is also why the value bar above matters more here than it would for a pure
software vendor: CV3's whole positioning is that a human resolves what the engine
cannot. A feature that only reports and never acts quietly reverts that positioning
back to "here is a tool" - the exact thing this section says CV3 is not selling.

### Business value, concretely

- **Recovered revenue.** A declined payment on a platform supporting recovery is
  offered an alternative, approved by a person, and captured. The merchant console
  reports it in currency, from real outcomes rather than projections.
- **Fewer returns.** The engine refuses to guess a size and asks instead. Returns
  were roughly $850bn in US retail in 2025, near 20% of everything sold online.
- **Merchants nobody else serves.** The large vendors' cost and complexity do not
  work for a team running Magento with one or two marketers. That is CV3's client.
- **Work that reaches a person.** Anything the engine cannot or must not do reaches
  a queue with a waiting clock, and the shopper hears back.

---

## Architecture

```
browser (5173) → engine (8000) → adapter → merchant (8001 or 8002)
```

Four stages:

**Reasoning** - the model proposes actions from a diagnosis. It may only propose.

**Decision** - rank proposals, drop anything the platform cannot do, choose one.

**Risk** - an eleven-rule gate: automatic, needs approval, or blocked.

**Execution** - carry it out through the adapter, and tell the shopper.

Two demo merchants prove platform independence:

- **Northfield Running Co.** (`conn_demo`) - REST, port 8001, no payment recovery
- **Kettle & Bloom Coffee** (`conn_kettle`) - GraphQL, port 8002, supports recovery

The same declined card produces a recovery offer on Kettle and an escalation on
Northfield, decided by what each platform declared rather than by a branch in the
code. That is the core claim and it holds. They also disagree about basics - Kettle
merges same-variant cart lines and Northfield does not - and everything above the
adapter works either way.

### Invariants that must not break

1. **The AI cannot assert risk.** No field for it. Risk properties come from a
   static table keyed by action type.
2. **Money never moves unattended.** `FINANCIAL_ALWAYS_HUMAN` fires before any
   merchant policy, and a merchant cannot switch it off.
3. **Taking payment has no action type.** `PREPARE_CHECKOUT` reads a cart and shows
   a total; only a shopper's own tap reaches `/api/chat/pay`. The guarantee was
   always about not spending other people's money unattended, never about stopping
   a shopper spending their own.
4. **A tap is trusted; the model's guess is not.** `chosen_variant` from a button
   beats `variant_id` from the model, which is a suggestion.
5. **Raw platform errors never reach shoppers.**
6. **One merchant's data is never reachable with another's key, and one shopper's
   cart, conversation and order are never reachable by another shopper holding the
   same key.** Enforced by an httpOnly visitor cookie and an ownership table
   (`db.owners`), checked on every cart/session/order route.

### The payment ledger

A cart is claimed in `db.idempotency` at creation, keyed on the cart id alone - not
on the cart and the card together, and not derived per payment attempt. Load-bearing
for three separate guarantees: a paid cart cannot be charged again on a different
card, a cart id reused after a merchant restart cannot inherit somebody else's paid
status, and a paid cart cannot be mutated afterwards (checked by both the REST cart
routes and the chat/tap execution path before any write). Keying it any other way -
per card, per attempt, per session - reopens a double charge.

### Layout

```
engine/reasoning/    the model, prompts, context building
engine/decision/     ranking and capability filtering
engine/risk/         the eleven-rule gate and merchant policy
engine/execution/    carrying actions out
engine/api/          routes, auth, accounts, shopper sessions
engine/db/           models, repository, keys, shopper accounts
engine/session/      conversation memory
engine/notify/       order-confirmation mail (fails soft; recorded, not
                     delivered, until the MAILER_SMTP_HOST env var is set)
adapters/            one per platform
shared/              the commerce interface and action types
storefront/          React, Vite
```

Two handwritten walkthroughs live in `Readme.MD`, not here, because they are
longer than anything that belongs in a practice file: "Adding a platform" (one
folder, no engine changes) and "Adding an action" (the six steps, of which steps 1-2
- the `ActionType` and its `ACTION_RISK_PROPERTIES` entry - are the security pair).
Reach for those when extending the engine. `scripts/` in the repo root is gitignored
one-off scratch, not part of the tracked suite.

---

## Security

Three key kinds: publishable for the browser, secret per merchant, operator for CV3.
Every route locked or deliberately public, verified by probing the running engine.
`connection_id` is checked against the key, so a caller cannot claim to be a merchant
they are not.

---

## Tests

`healthcheck.py` - one path end to end, growing as bugs are found and fixed. Reports
SKIP rather than FAIL when the provider is busy, and says how many checks never
executed.

`fuzz.py` - random shopper sequences, asserting after every step that the cart
matches what was added and removed, a paid cart is never chargeable again on any
card, and no merchant's session, cart or orders are reachable from another.
Model-free. Prints its seed so a failure is reproducible.

`auditroutes.py` - probes the running engine: refused without a key, accepted with
its own, refused another merchant's, and (since the shopper-scoping fix) refused
another shopper's cart, conversation and order even when both hold the same
publishable key.

`eval.py` - scores the model's judgement across repeated attempts. Unfinished.

### Running the checks

All three suites drive the **running** services over HTTP - they probe what is
actually up, not the source, so they never test code you have not restarted. Start
the three backend processes first (see Running it; the storefront is not needed):

```
python healthcheck.py            # PASS/FAIL per check; names the file to look at
python fuzz.py                  # 20 sequences; --seed N to replay, --sequences N to soak
python auditroutes.py            # probes every route's lock and shopper scoping
```

`testshopper.py` is the one `test*.py` that is tracked (the `.gitignore` explicitly
keeps it); every other `test*.py`, and the contents of `scripts/`, are gitignored
one-off scratch - don't trust them as source of truth.

Frontend, from `storefront/`:

```
npm run build    # tsc -b && vite build - the typecheck guard. npm run dev does NOT
                 # catch type errors, and build did fail for a session the dev server
                 # never flagged (see #14 in Completed.md)
npm run lint     # eslint .
npm run dev      # Vite, port 5173
```

## Current status

See Completed.md for what's built and fixed, verified end to end. See
PROGRESS.md for what's still broken, unbuilt, or open. Both are updated at
the end of every session — read them before assuming anything about current
state.

---

## Running it

Four processes, from the project root, with `.venv\Scripts\Activate.ps1` first:

```
python -m uvicorn sample_merchant.api.main:app --port 8001
python -m uvicorn sample_merchant_two.api.main:app --port 8002
python -m uvicorn engine.api.main:app --port 8000
cd storefront ; npm run dev
```

Then `python demo_reset.py`, and in the browser console `sessionStorage.clear()`
followed by a hard reload.

**Restart the engine after any change under `engine/`.** Restart Vite after any new
file or any change to `.env.local` or `vite.config.ts`.

### Addresses

`/northfield`, `/kettle` - the shops
`/signin`, `/signup` - shopper accounts
`/merchant` - one client's console, their secret key
`/operations` - CV3's queue, the operator key

### Keys

`python mint_keys.py` writes five keys to `.env.keys`, gitignored and unrecoverable.
`python patch_auth_4c.py` copies the publishable ones into `storefront/.env.local`.
Deleting `cv3.db` destroys the hashes, so both must be re-run.

---

## Working practices, learned the hard way

**Verify against the object, not the file.** Several patches printed "applied" while
their replacement had matched nothing, because the check was "is this string in the
file" run against a file the patch had just failed to write. Check
`Model.model_fields`, check `inspect.signature`, run the thing.

**Read the signature before calling it.** More than one failure came from guessing a
method name when the correct call was three lines away in the same file.

**When the browser disagrees with the source, restart Vite first.** Four separate
debugging sessions ended there.

**Ask for the error before reasoning about the cause.** The Console tab named the
problem in one line on several occasions where inference had already burned twenty
minutes.

**Walk the product, do not read the test summary.** Features were committed on the
strength of one narrow test passing, repeatedly, and the demo broke anyway.

**A test written from a guess at the bug's output, not the actual output, can pass
for the wrong reason.** A check asserting a reply excluded words like "added" or
"done" was written before seeing what the model actually says - which turned out to
be "Sure, I'll add the X to your cart," containing none of them. The check would
have passed against the very bug it was meant to catch. Rewritten to assert the
engine's own deterministic replacement text instead, and confirmed by hand against
both the broken and fixed code before trusting it.

**In a test suite, a local variable with the same name as the module-level state it
represents will eventually shadow it.** A new healthcheck block named its response
variable `failed` - the same name as the module-level failure list. The suite
actually passed; the summary then read the response dict as the failures and
reported 31 FAILED (one per field of the reply). Renamed the local. The same class
of bug cost a day on the payment route once (`key` shadowing `key`), so names in
this codebase shadowing their module-scope cousins deserve a second look on sight.

**A "recorded once, never revisited" outcome bug is a shape, not a one-off - fix
the shape, not the instance.** The holdout comparison recorded a declined
payment's outcome as unresolved the instant it happened and never checked
whether the shopper went on to fix it themselves - so `holdout_resolved` read
as a permanent zero regardless of what actually happened next. The first fix
patched only the holdout's own call site. Asked to check for the same mistake
elsewhere rather than trust that one fix, a grep for every caller of
`record_outcome` found the identical gap at two more sites that had copied the
same "record unresolved, never look again" pattern: an operator's rejection
and an expired approval. All three got one shared fix
(`resolve_unresolved_payment_cases_for_cart`) instead of three separate
patches, specifically so a fourth friction type landing in this shape later has
nowhere left to reopen it. When a bug is "X records a fact once and something
later could make that fact stale," grep every other place that records the
same kind of fact before calling the class of bug closed - fixing the one
instance you were shown and stopping there is how the same mistake gets made
again at the next call site.

**A feature can be correct, tested, invariant-clean, and still not worth
having shipped.** Both the Merchant Copilot and the Operations Copilot passed
every existing gate cleanly - invariant-guard found nothing wrong, all three
suites held, browser verification was thorough - and both shipped as read-only
panels answering questions about data the console already displayed. Neither
changes what anyone can do. The gates that existed checked whether the feature
was dangerous; nothing checked whether it was pointless. See the value bar
under Feature Implementation Rules, and use `value-auditor` before trusting
that "verified end to end" means "worth having built."

---

## Agents

Project subagents live in `.claude/agents/`. One line each:

- **adapter-builder** — adds a new merchant platform folder implementing the commerce interface. Use when onboarding a new platform or demo merchant.
- **action-builder** — adds a new `ActionType` through all six pipeline steps. Use when the engine needs to propose/execute a kind of action it can't yet.
- **invariant-guard** — read-only check of a diff against the six hard invariants and the idempotency-key rule. Use after any change to `engine/risk`, `engine/execution`, `engine/api`, `engine/db`, or `shared/`.
- **test-runner** — runs `healthcheck.py` / `fuzz.py` / `auditroutes.py` against the running services and interprets SKIP/FAIL correctly. Use to validate any change to `engine/`.
- **frontend-verifier** — runs the real typecheck guard (`npm run build`, not `dev`) plus lint, then walks the changed feature by hand. Use after any change under `storefront/`.
- **progress-scribe** — updates PROGRESS.md and Completed.md in their existing terse style, cross-checked against real git state. Moves an item from PROGRESS.md to Completed.md only once every test suite it needs has actually passed and it clears the value bar - never mid-task, and never on the strength of "the code is written." Use at the end of a session, not mid-task.
- **bug-reproducer** — reproduces a bug first and records the exact call and response, before any fix is written. Use before starting any bug fix, so the eventual regression test asserts against a real reproduction rather than the fix's own logic.
- **doc-auditor** — reads CLAUDE.md, Completed.md and PROGRESS.md against the actual code and reports contradictions (stale counts, renamed things, drifted claims, or an item described as done in Completed.md that a suite no longer backs). Use periodically or whenever a docs claim looks suspicious.
- **feature-spec** — writes the six value-bar answers, the real-client conditions and the figure that moves, before any code. Use at the start of any roadmap feature, before implementation begins.
- **value-auditor** — read-only check of a finished feature against the value bar, the four real-client conditions and the named anti-patterns. Reports which conditions were actually exercised versus assumed. Use before moving any roadmap item to Completed.md, the same way invariant-guard is used before considering a change to engine/ done.

**Wiring:** after any change under `engine/`, run `invariant-guard` and
`test-runner` before considering the change done. After any change under
`storefront/`, run `frontend-verifier`. For any **roadmap feature**, run
`feature-spec` before starting and `value-auditor` before moving the item to
Completed.md. Do this even if not asked explicitly — it's the equivalent of the
checks a human would run before calling the work finished.

The two feature gates catch different failures and neither substitutes for the
other. invariant-guard catches a feature that is dangerous. value-auditor
catches a feature that is pointless. Both copilots passed invariant-guard
cleanly and would have failed value-auditor.

**Documentation is not optional at session end.** Before ending any session
that touched `engine/`, `storefront/`, `adapters/`, or moved a roadmap item's
status, run `progress-scribe`. This is a trigger, not a suggestion — the same
force as the invariant-guard and value-auditor lines above. Do this even if
not asked explicitly and even if the session felt exploratory rather than
finished.

`progress-scribe` must, every time it runs:

- Move any item out of PROGRESS.md into Completed.md only if every test suite
  it needs has passed **and** `value-auditor` has returned VALUABLE for it —
  never on the strength of "the code is written," and never mid-task.
- Cross-check its own claims against real git state and the actual running
  behaviour, not against what the session intended to do.
- Update PROGRESS.md's Next Steps and Known Issues to match what is actually
  still open, not what was open at the start of the session.

A session that ends without this step is exactly how CLAUDE.md, Completed.md
and PROGRESS.md drift out of sync with the real code — which is the problem
this whole set of instructions exists to stop.

## The constraint

The free Groq tier throttles at roughly four model turns a minute. `healthcheck.py`
has grown past what fits in that window - it needs more model calls than the tier
allows in the time the suite takes, so a full run cannot complete reliably, and this
gets worse as the suite grows rather than better. Tapping paths are model-free by
design and keep working while throttled - a deliberate response to this, not a
coincidence.

A concrete cost rather than a complaint, and a few dollars a month buys it back.