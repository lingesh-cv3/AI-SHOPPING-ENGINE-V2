# CV3 AI Shopping Assistant Engine

A platform-independent commerce intelligence and action layer. It assists shoppers,
detects and diagnoses shopping friction, decides what to do about it, applies risk
controls, executes through merchant-specific adapters, and records the outcome.

The engine runs on CV3 infrastructure. A merchant connects to it; they never receive
its code.

---

## The mental model

| Layer | Question it answers |
|---|---|
| **AI Engine** | What should we do? |
| **Decision Engine** | Which valid action should we choose? |
| **Risk / Policy** | Are we allowed to do it automatically? |
| **Adapter** | How do we execute it on this merchant's platform? |

One AI engine. One standard commerce interface. Reusable platform adapters. Isolated
merchant connections.

The separation is the product. The AI proposes and cannot decide; the Decision
Engine selects and cannot classify risk; the Risk Gate classifies and cannot
execute; the adapter executes and cannot choose. Each boundary removes a way the
system could go wrong.

---

## Two invariants, enforced in code

**The AI cannot assert its own risk properties.** `ProposedAction` has no field in
which a model can claim a refund is non-financial. Risk properties are looked up
from a static table keyed by action type
(`shared/models/action.py::ACTION_RISK_PROPERTIES`). An action type with no table
entry fails closed to financial and irreversible, so a forgotten entry degrades to
"requires approval" rather than "silently automated".

**Money-touching actions can never run unattended.** The Risk Gate checks
`FINANCIAL_ALWAYS_HUMAN` before it consults any merchant setting. A merchant who
explicitly permits automatic refunds still gets human approval, and the decision
records which rule overrode them.

---

## Repository layout

```
shared/            contracts - imported by the engine AND every adapter
  models/          normalized Product, Cart, Order, Money, risk table, signals
  interfaces/      StandardCommerceInterface

adapters/          one folder per platform, plugin-style
  framework/       shared HTTP client: retry, timeouts, idempotency headers
  sample/          Northfield adapter (REST)
  kettle/          Kettle & Bloom adapter (GraphQL)

engine/
  api/             FastAPI routes: shop proxy, chat, policy, approvals, simulate
  reasoning/       the ONLY component that touches an LLM
  decision/        candidate filtering, capability registry, ranking
  risk/            deterministic AUTO / HUMAN / BLOCK gate
  execution/       runs approved actions, re-checks capability, records outcomes
  session/         shared memory across the widget and the chat
  db/              SQLAlchemy models, repository, idempotency ledger

sample_merchant/       test client one - REST, integer paise, stock counts
sample_merchant_two/   test client two - GraphQL, decimal money, boolean stock
storefront/            React + TypeScript: shop, operator queue, merchant console
```

### Boundary rules

These are what the directory structure exists to enforce.

- **`shared/` has no dependencies.** No database, no FastAPI, no LLM SDK. Pure
  shape. The moment it imports something, every adapter inherits it.
- **`adapters/` never imports `engine/`.** Only `shared/`. This is what makes a new
  platform a new folder rather than a change everywhere.
- **`engine/` never imports a concrete adapter.** It holds a
  `StandardCommerceInterface` and cannot name a platform.
- **`engine/risk/` never imports `engine/reasoning/`.** This makes it structurally
  impossible for the model to influence risk classification.
- **The sample merchants share nothing with anything.** Their own state, their own
  processes. If either imported from `shared/`, the abstraction would stop being
  tested.

---

## Running it

Four processes. Python 3.11+ and Node 20+.

```bash
python -m venv .venv
.venv/Scripts/activate           # Windows
python -m pip install -e ".[dev]"
```

Create `.env` at the repository root:

```
GROQ_API_KEY=your_key_here
GROQ_MODEL=openai/gpt-oss-120b
```

`.env` is gitignored. The key must never be committed.

Then four terminals:

```bash
python -m uvicorn sample_merchant.api.main:app --port 8001      # test client 1
python -m uvicorn sample_merchant_two.api.main:app --port 8002  # test client 2
python -m uvicorn engine.api.main:app --port 8000               # the engine
cd storefront && npm install && npm run dev                     # port 5173
```

Open `http://localhost:5173`.

**On the model.** Groq's free tier allows roughly 8,000 tokens per minute and a turn
costs around 2,000, so about four turns a minute before throttling. When the model
is unreachable the engine falls back to rule-based proposals and says so rather than
pretending to understand.

---

## The two test merchants

They exist to prove platform independence, so they agree on almost nothing.

| | Northfield Running Co. | Kettle & Bloom Coffee |
|---|---|---|
| Protocol | REST, many endpoints | GraphQL, one endpoint |
| Money | integer paise, no currency | decimal string + currency code |
| Stock | count plus a tri-state flag | boolean only, **no counts** |
| Errors | HTTP 200 with `{"error": ...}` | proper status codes + errors array |
| Timestamps | epoch seconds as a string | ISO 8601 |
| Vocabulary | items, basket, purchase, voucher | products, bag, order, discountCode |
| **Payment recovery** | **none at all** | **supported** |

That last row is the demonstration. A declined card on Northfield can only be
escalated to a person; on Kettle the engine can offer an alternate payment method
and actually recover the money. Same AI, same rules, same code — the difference
comes entirely from what each platform declared it can do.

The boolean-stock row matters too: `quantity_available` is `None` on Kettle, which
is deliberately distinct from `0`. Northfield never exercises that path.

---

## Seeing it work

Switch merchant with the dropdown in the masthead. **Show engine** reveals the
diagnostic panel — off by default, because a shopper should never see pipeline
stages or rule names.

**A search that finds nothing.** Search `trainers` on Northfield. The assistant
opens itself, reads the catalog, works out this shop says "shoe" and "sneaker", and
suggests a term that actually returns results.

**A declined card, twice.** Check out with card `0002` on each merchant. Northfield
escalates with all three recovery options struck through. Kettle offers an alternate
payment method, and approving it in the Queue recovers the money for real.

**The money guarantee.** In the Merchant console, everything money-touching is
listed as always requiring a person, with no way to promote it. Call `/api/simulate`
with `APPLY_PROMOTION` and the most permissive policy you can set: the verdict is
still `HUMAN`, and `risk_rule` names `FINANCIAL_ALWAYS_HUMAN`.

**Shared memory.** After a declined payment, open the chat and type only "what now".
The assistant already knows — the storefront recorded the friction against the same
session the chat reads.

Test cards: `1111` approves. `0002`, `0003`, `0004` decline on Northfield; `0002`,
`0003`, `0005` decline on Kettle.

---

## What is built

- Platform-independent commerce contract, with two adapters proving it
- AI reasoning on Groq, tool-schema constrained, falling back to rules on failure
- Deterministic risk gate: AUTO / HUMAN / BLOCK, every verdict naming its rule
- Decision engine filtering by declared capability and merchant policy
- Execution with capability re-check, idempotency ledger, and outcome recording
- Real payment recovery on a platform that supports it, with revenue captured
- Persistence for cases, approvals, outcomes, sessions and merchant policy
- Operator approval queue where approving actually executes
- Shopper chat with memory shared across the friction widget and the conversation
- Merchant console showing live platform capabilities and the risk rules

## What is not built

- **Authentication.** Anyone who can reach the engine can call it. Publishable and
  secret keys, per-merchant credential storage, and domain locking are outstanding,
  and no real client can connect until they exist.
- **Widget packaging.** The assistant is a component in our storefront, not a script
  a merchant installs.
- **Webhooks.** Friction reaches the engine only because our own storefront reports
  it. Real platforms push events, and `SupportsWebhooks` has no implementation.
- **Approval expiry.** Expired approvals leave the queue but the case stays
  `PENDING_APPROVAL` and nobody tells the shopper.
- **Learning loop.** Outcomes are recorded and nothing reads them.
- **Semantic search.** Keyword matching only, which is why the AI has to suggest
  better search terms rather than the search simply working.
- **Postgres, hosting, and real tests.** SQLite by connection string, local only,
  and the `verify_*.py` scripts should be pytest.

---

## Design decisions worth knowing

**Raw provider SDK, not LangChain.** The pipeline is fixed and deterministic, and a
framework's abstractions would blur the boundaries between reasoning, decision and
risk that the architecture deliberately keeps apart.

**Groq through the `openai` SDK.** Their API is OpenAI-compatible, so the provider
stays swappable — moving to Anthropic or OpenAI is a base URL and a model name.

**The model ID is configuration, never a constant.** Groq retired
`llama-3.3-70b-versatile` on 16 August 2026. Providers deprecate; the engine should
not need a code change when they do.

**Money is never a float.** `Decimal` plus an explicit currency, everywhere,
including across the wire as a string. A recovery system that loses fractions of a
rupee is not one to run.

**Standard automation mode needs no allowlist.** The gate has already proven a safe
action moves no money, can be undone, and contacts nobody. Requiring a merchant to
also tick a box added no safety and made harmless things queue, which trains people
to approve without reading.

**Ranking is a fixed table, not a scoring model.** A merchant asking why the engine
offered a substitute rather than a discount gets a real answer, and the ordering
encodes a bias toward fixes that cost the merchant nothing.

**Adding to a cart asks which size.** A product with options is not added without
one. The platform would accept it and the shopper would find out at delivery;
asking costs one turn and guessing costs a return.

---

## External systems

No hard dependency on any of these. Each has a seam, not an integration.

- **Webeyez** — would become one more ingester behind the signal intake, producing
  the same normalized event as the widget and merchant webhooks.
- **CRO / GEO** — read from the generalized insights the outcome service records.
  Nothing consumes them yet, by design.
- **CV3 Rev Engine** — an outbound endpoint on the outcome service, if the team
  decides they want one queue rather than two.

## Not in scope

Not a generic chatbot. Not a replacement commerce platform. Not a payment processor.
Not an unrestricted autonomous financial agent.

A genuinely new platform still requires an adapter. The adapter pattern keeps
platform complexity out of the engine; it does not make integration free.