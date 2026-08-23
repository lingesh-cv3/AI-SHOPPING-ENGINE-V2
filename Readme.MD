# CV3 AI Shopping Assistant Engine

An AI assistant for online stores that works on any commerce platform. It helps
shoppers, notices when they get stuck, and can act on their behalf inside limits the
merchant controls.

The engine is hosted by CV3. A merchant connects their store and adds a script to
their site. One engine serves every client regardless of what their store runs on.

```
shopper's browser  →  CV3 engine  →  platform adapter  →  the merchant's store
```

---

## Contents

- [What it does](#what-it-does)
- [Installation](#installation)
- [Core concepts](#core-concepts)
- [Adding a platform](#adding-a-platform)
- [Adding an action](#adding-an-action)
- [The risk rules](#the-risk-rules)
- [Configuration](#configuration)
- [HTTP API](#http-api)
- [Data model](#data-model)
- [Project status](#project-status)

---

## What it does

**Answers shoppers.** A chat assistant that knows the merchant's live catalog,
stock and prices. It can search, check availability, and add or remove things from a
cart.

**Notices friction.** A search that returns nothing, a coupon that gets rejected, a
card that declines. Each one is a moment a shopper usually leaves.

**Diagnoses it.** Not "no results found" but why. A shopper searching for "trainers"
in a shop that says "shoes" gets told which word to use, because the model read the
catalog and worked it out.

**Acts within limits.** Safe things happen immediately. Anything touching money
waits for a person, and no setting changes that.

**Recovers what the platform allows.** On a store whose platform can retry a failed
payment, the engine can do it. On one that cannot, it hands the case to a person
with the diagnosis attached rather than pretending.

**Remembers.** A shopper whose card just failed can open the chat and say "what
now?" without explaining. The friction detector and the chat share one memory.

---

## Installation

Requires Python 3.11+ and Node 20+.

```bash
git clone <repo>
cd cv3-engine
python -m venv .venv
.venv/Scripts/activate          # or: source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Create `.env` in the repo root:

```ini
GROQ_API_KEY=gsk_...
GROQ_MODEL=openai/gpt-oss-120b
# optional
DATABASE_URL=sqlite+aiosqlite:///./cv3.db
SAMPLE_MERCHANT_URL=http://127.0.0.1:8001
KETTLE_MERCHANT_URL=http://127.0.0.1:8002
```

Run four processes:

```bash
python -m uvicorn sample_merchant.api.main:app --port 8001      # demo store, REST
python -m uvicorn sample_merchant_two.api.main:app --port 8002  # demo store, GraphQL
python -m uvicorn engine.api.main:app --port 8000               # the engine
cd storefront && npm install && npm run dev                     # UI on 5173
```

Open http://localhost:5173. Test cards: `1111` succeeds, `0002` declines.

The engine works without `GROQ_API_KEY`. It falls back to rule-based behaviour and
reports that it did, rather than failing.

---

## Core concepts

### The standard commerce interface

Every platform is reached through the same eleven methods, defined in
`shared/interfaces/commerce.py`:

```python
get_capabilities()   search_products()   get_product()     check_inventory()
get_cart()           add_to_cart()       update_cart()     get_order()
apply_promotion()    checkout()          recover_payment()
```

Nothing above the adapter layer knows which platform answered. A GraphQL store
returning `{"price": {"amount": "1450.00", "currencyCode": "INR"}}` and a REST store
returning `{"price_paise": 145000}` both arrive as:

```python
Money(amount=Decimal("1450.00"), currency="INR")
```

Money is `Decimal` and a currency, never a float. It crosses HTTP as a string for
the same reason.

### Capabilities

A platform declares what it can actually do. Not what the vendor supports in
general, but what this specific merchant connection can do right now.

```python
CapabilitySet(
    connection_id="conn_demo",
    platform="sample-merchant",
    operations={
        Operation.RECOVER_PAYMENT: OperationCapability(
            operation=Operation.RECOVER_PAYMENT,
            supported=False,
            reason="platform exposes no payment-recovery or refund endpoint",
        ),
        ...
    },
    payment_recovery_methods=[],
)
```

Anything undeclared counts as unsupported. Nothing downstream assumes a capability,
it looks one up, and an action targeting an unsupported operation is dropped before
it reaches the risk gate.

This is why the same declined card produces different behaviour on different
merchants. It is a data difference, not a code branch.

### Actions and risk properties

The engine has a fixed vocabulary of actions, and each carries intrinsic risk
properties from a static table in `shared/models/action.py`:

| Action | Financial | Reversible |
|---|---|---|
| `RECOMMEND_PRODUCTS` | no | yes |
| `SUGGEST_ALTERNATIVE` | no | yes |
| `ADD_TO_CART` | no | yes |
| `REMOVE_CART_LINE` | no | yes |
| `CHECK_AVAILABILITY` | no | yes |
| `NOTIFY_BACK_IN_STOCK` | no | **no** |
| `APPLY_PROMOTION` | **yes** | yes |
| `RETRY_PAYMENT` | **yes** | no |
| `OFFER_ALTERNATE_PAYMENT` | **yes** | no |
| `SPLIT_PAYMENT` | **yes** | no |
| `ISSUE_REFUND` | **yes** | no |
| `CANCEL_ORDER` | **yes** | no |

These are facts about the action, not judgements about the situation. A refund is
financial and irreversible regardless of amount, circumstance, or how confident the
model is.

`NOTIFY_BACK_IN_STOCK` is worth noting: no money involved, but you cannot unsend an
email, so it needs approval too.

An action type missing from the table is treated as financial and irreversible. A
forgotten entry causes unnecessary approvals rather than an unnoticed automatic
refund.

### The four stages

A shopper turn passes through four components, each able to do only its own job:

```
Reasoning  →  Decision  →  Risk  →  Execution
 proposes     selects      gates    carries out
```

**Reasoning** (`engine/reasoning/`) is the only code that calls an LLM. It sees the
situation, the catalog, the cart and the conversation, and returns one to four
proposals with a diagnosis and a reply. It cannot select, gate, or execute.

**Decision** (`engine/decision/`) drops proposals the platform cannot perform or the
merchant has switched off, then picks the highest-ranked survivor. If nothing
survives, it returns an escalation naming what was unavailable. No LLM.

**Risk** (`engine/risk/`) returns `AUTO`, `HUMAN` or `BLOCK` by walking ten rules in
a fixed order, first match wins. Deterministic: same input, same output. No LLM, and
it cannot import the reasoning package.

**Execution** (`engine/execution/`) re-checks the capability, applies idempotency to
anything financial, calls the adapter, and records an outcome.

### Why the AI cannot escalate its own privileges

The tool schema the model fills has five fields per action: `action_type`,
`rationale`, `search_query`, `product_id`, `code`. There is nowhere to put a claim
about risk.

A model can write "this refund is small and clearly safe to automate" in its
rationale. That string is shown to the human reviewing the case and has no
mechanical effect. Risk properties are looked up from the table after the proposal
arrives, keyed only by action type.

This holds regardless of how the model behaves, because it is a property of the data
structure rather than of the model's obedience.

### Sessions and shared memory

Both the friction detector and the chat write against one `session_id`. A declined
payment creates a `Case` carrying it; a chat message creates a `SessionTurn` carrying
the same one.

When a turn assembles its context it reads both, and labels them differently. The
conversation is what was said. The friction is what was observed. The assistant
knows a payment failed without claiming it discussed it.

---

## Adding a platform

This is the main extension point. A new platform is one folder and no engine
changes.

### 1. Write the mapper

Pure functions, no I/O, so they can be tested without a running server. Their job is
turning the platform's shape into the engine's.

```python
# adapters/yourplatform/mapping.py
from decimal import Decimal
from shared.models import Availability, ErrorCode, Money, Product

ERROR_MAP = {
    "ITEM_MISSING": ErrorCode.PRODUCT_UNAVAILABLE,
    "NO_STOCK": ErrorCode.INVENTORY_INSUFFICIENT,
}

def money(cents: int | None, currency: str) -> Money | None:
    if cents is None:
        return None
    return Money(amount=Decimal(cents) / 100, currency=currency)

def to_product(raw: dict) -> Product:
    return Product(
        product_id=raw["sku"],
        title=raw["name"],
        price=money(raw.get("price_cents"), "USD"),
        availability=(
            Availability.IN_STOCK if raw["available"] else Availability.OUT_OF_STOCK
        ),
        variants=[...],
    )
```

Two rules. Never invent information the platform did not give you: if it reports no
stock count, `quantity_available` is `None`, which is deliberately different from
`0`. And never let a platform-specific error string escape, map it to an
`ErrorCode`.

### 2. Write the adapter

```python
# adapters/yourplatform/adapter.py
from shared.interfaces import StandardCommerceInterface
from shared.models import CapabilitySet, Operation, OperationCapability
from ..framework.http import PlatformClient
from . import mapping


class YourPlatformAdapter(StandardCommerceInterface):
    def __init__(self, *, connection_id: str, base_url: str, api_key: str) -> None:
        self.connection_id = connection_id
        self.platform = "yourplatform"
        self._client = PlatformClient(
            base_url,
            connection_id=connection_id,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def get_capabilities(self) -> CapabilitySet:
        # Declare honestly. Claiming an operation you cannot perform produces a
        # runtime failure inside an approved action, which is the worst place to
        # find out.
        ...

    async def search_products(self, query: str, *, limit: int = 20):
        body = await self._client.request(
            "GET", "/v2/products", params={"q": query, "limit": limit}
        )
        ...
```

Python refuses to instantiate a subclass with any abstract method unimplemented, so
a half-finished adapter fails at construction rather than at runtime.

`PlatformClient` gives you retries with backoff, timeouts, idempotency headers, and
logging that never records credentials or shopper data.

### 3. Register a connection

```python
# engine/api/deps.py
registry.register(
    YourPlatformAdapter(
        connection_id="conn_acme",
        base_url="https://acme.example.com",
        api_key=os.environ["ACME_KEY"],
    )
)
```

That is the whole integration. Nothing in `engine/` changes.

### Testing an adapter

The two demo stores exist as reference implementations and were built to be
awkward on purpose. `sample_merchant/` returns errors with an HTTP 200 status,
stores money as whole paise, and has no payment recovery. `sample_merchant_two/`
speaks GraphQL, uses decimal strings, and reports stock as a bare boolean.

If your adapter handles both patterns you have probably handled a real platform.

---

## Adding an action

1. Add the action to `ActionType` in `shared/models/action.py`.
2. Add its risk properties to `ACTION_RISK_PROPERTIES` in the same file. Skipping
   this means the action defaults to financial and irreversible.
3. Map it to a commerce operation in `operation_for()` in
   `engine/decision/engine.py`, if it needs one.
4. Add it to the preference chain for the relevant friction type in
   `engine/decision/ranking.py`.
5. Add it to `PROPOSABLE` in `engine/reasoning/prompts.py` if the model should be
   able to suggest it.
6. Handle it in `_dispatch()` in `engine/execution/service.py`.

Steps 1 and 2 together are the security-relevant pair. The rest determines whether
the action is reachable and useful.

---

## The risk rules

Ten rules, evaluated in order, first match wins. Every verdict records which rule
produced it.

| # | Rule | Outcome |
|---|---|---|
| 1 | `CAPABILITY_UNVERIFIED` | BLOCK. Reached the gate without a capability check, which is a bug upstream. |
| 2 | `CONNECTION_SUSPENDED` | BLOCK. The connection is paused. |
| 3 | `BLOCKED_BY_MERCHANT_POLICY` | BLOCK. The merchant switched this action off. |
| 4 | `UNKNOWN_ACTION_TYPE` | HUMAN. No risk-table entry, so treated as financial. |
| 5 | `FINANCIAL_ALWAYS_HUMAN` | HUMAN. Touches money. Checked before any merchant setting. |
| 6 | `IRREVERSIBLE_ALWAYS_HUMAN` | HUMAN. Cannot be undone. |
| 7 | `TOUCHES_CUSTOMER_DATA` | HUMAN. Contacts the customer. |
| 8 | `CAUTIOUS_MODE_GATES_ALL` | HUMAN. New connections start here. |
| 9 | `NOT_ON_MERCHANT_ALLOWLIST` | HUMAN. Only if the merchant narrowed the list. |
| 10 | `AUTO_CLEARED` | AUTO. Safe, reversible, permitted. |

Rule 5 sits above every merchant setting deliberately. A merchant who explicitly
enables automatic refunds still gets an approval request, and the audit record names
rule 5 as the reason.

Automation modes: `CAUTIOUS` gates everything, `STANDARD` runs anything the gate
proves safe, `SUSPENDED` runs nothing. In `STANDARD` an empty allowlist means all
safe actions run. Populating it narrows that; it can never widen it.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | none | Omit and the engine runs on rules only. |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Model ID. Deliberately not a constant, providers retire models. |
| `GROQ_BASE_URL` | Groq's endpoint | Point at any OpenAI-compatible provider. |
| `DATABASE_URL` | `sqlite+aiosqlite:///./cv3.db` | Postgres via `postgresql+asyncpg://...` |
| `SQL_ECHO` | off | Log every statement. |
| `SAMPLE_MERCHANT_URL` | `http://127.0.0.1:8001` | Demo store one. |
| `KETTLE_MERCHANT_URL` | `http://127.0.0.1:8002` | Demo store two. |

On rate limits: Groq's free tier allows roughly 8,000 tokens a minute, and a turn
costs around 2,000. Past that the engine falls back to rules and says so. If the
assistant suddenly seems obtuse, check the logs for a 429 before assuming a bug.

---

## HTTP API

Base `http://localhost:8000`.

**Shop operations**, proxied through the adapter so responses are normalized:

```
GET   /api/shop/{connection}/search?q=&dept=&limit=
GET   /api/shop/{connection}/product/{id}
GET   /api/shop/{connection}/departments
POST  /api/shop/{connection}/cart
GET   /api/shop/{connection}/cart/{cart_id}
POST  /api/shop/{connection}/cart/{cart_id}/lines
POST  /api/shop/{connection}/cart/{cart_id}/promotion
POST  /api/shop/{connection}/cart/{cart_id}/checkout
GET   /api/shop/{connection}/order/{order_id}
```

**Assistant:**

```
POST  /api/chat
GET   /api/chat/{connection}/{session_id}
POST  /api/simulate
```

`/api/chat` takes a message and optionally a friction type, and returns the reply,
any products it fetched, and the full pipeline trace. `/api/simulate` runs the
pipeline for a friction type without a conversation.

**Merchant and operator:**

```
GET   /api/connections
GET   /api/connections/{connection}/capabilities
GET   /api/policy/rules
GET   /api/policy/actions
GET   /api/policy/{connection}
PUT   /api/policy/{connection}
GET   /api/approvals/{connection}
POST  /api/approvals/{connection}/{approval_id}
GET   /api/cases/{connection}
GET   /api/stats/{connection}
```

Approving an approval executes the action and returns what happened.

---

## Data model

Six tables in `engine/db/models.py`.

`cases` holds one friction event and everything decided about it: the diagnosis, the
evidence, both replies, what was proposed, what was rejected and why, the selected
action, and the risk verdict. Read as a unit, so the pipeline detail is JSON.

`approvals` is the operator queue. Separate from cases because its lifecycle differs
and the queue should be readable without loading every case payload.

`outcomes` records how a case ended, including revenue recovered where money
actually moved.

`session_turns` is the conversation. The `session_id` also appears on `cases`, which
is what makes memory shared between the chat and the friction detector.

`execution_attempts` is the idempotency ledger. A row is written before the platform
is called, not after, so a crash mid-call leaves a recoverable `IN_FLIGHT` record
rather than losing the fact that anything happened.

`merchant_policies` persists automation settings.

Every table carries `connection_id`, and every repository function requires one.
There is no unfiltered variant, so a route cannot read another merchant's data by
forgetting a filter.

---

## Project status

Working: two platforms behind one interface, AI reasoning with a rules fallback,
the risk gate, the approval queue with real execution, payment recovery on the
platform that supports it, persistence, and shared session memory.

Not built:

**Authentication.** The engine is open to anyone who can reach it. Publishable and
secret keys, per-merchant credential storage, and domain locking are all needed
before a client can connect.

**Installable widget.** The assistant is a component in the demo storefront, not a
script tag.

**Webhooks.** `SupportsWebhooks` is defined and unimplemented. The engine only
learns about friction from our own storefront.

**Approval expiry.** An expired approval leaves the queue and the case stays
`PENDING_APPROVAL` with nobody informed.

**Learning.** Outcomes are recorded and nothing reads them.

**Semantic search.** Keyword matching only, which is why the assistant compensates
by suggesting better search terms.

**Production infrastructure.** SQLite by default, local only, and the `verify_*.py`
scripts should be pytest.

---

## Development

```bash
python verify_risk.py        # risk gate, no servers needed
python verify_adapter.py     # adapter against the REST demo store
python verify_decision.py    # decision and risk together
```

Four import rules hold the architecture together:

- `shared/` imports nothing outside the standard library and Pydantic.
- `adapters/` never imports `engine/`.
- `engine/` never imports a concrete adapter.
- `engine/risk/` never imports `engine/reasoning/`.

The last one is why the model cannot influence risk classification. It is worth a
lint rule before the codebase grows.

## Not in scope

Not a general chatbot, a commerce platform, or a payment processor. Not an
unrestricted autonomous agent.

A new platform still needs an adapter. The pattern keeps platform complexity out of
the engine; it does not make integration free.