# CV3 AI Shopping Engine

Context for whoever picks this up. Written after a stretch of development that
produced real features and a real mess, and the honest version of both is more use
than a tidy summary.

**Read PROGRESS.md before believing anything else here.** A green test run has
repeatedly meant less than it looked.

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

**Risk** - a ten-rule gate: automatic, needs approval, or blocked.

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
6. **One merchant's data is never reachable with another's key.**

### Layout

```
engine/reasoning/    the model, prompts, context building
engine/decision/     ranking and capability filtering
engine/risk/         the ten-rule gate and merchant policy
engine/execution/    carrying actions out
engine/api/          routes, auth, accounts, shopper sessions
engine/db/           models, repository, keys, shopper accounts
engine/session/      conversation memory
adapters/            one per platform
shared/              the commerce interface and action types
storefront/          React, Vite
```

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

## Current status

See PROGRESS.md for what's built, what's broken, and what's not built yet.
It's updated at the end of every session — read it before assuming anything
about current state.

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

---

## The constraint

The free Groq tier throttles at roughly four model turns a minute. One healthcheck
check cannot run reliably because the suite needs more calls than the tier allows in
the time it takes. Tapping paths are model-free by design and keep working while
throttled - a deliberate response to this, not a coincidence.

A concrete cost rather than a complaint, and a few dollars a month buys it back.
