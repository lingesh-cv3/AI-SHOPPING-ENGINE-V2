"""Create one pending CartProposal directly, for the browser test suite.

Same reasoning as legacy_account.py: the browser test needs a stored
proposal to exist so it can verify the Suggestions panel and the accept/
reject flow, but creating one the real way costs a Groq model call and its
non-determinism (the model does not always pick PROPOSE_CART_CHANGE for a
given phrasing - see engine/reasoning/prompts.py). Writing the row directly
through the same `engine.db.cart_proposals.create` function the real
PROPOSE_CART_CHANGE execution path calls keeps this a real exercise of the
storage/accept/reject layer, not a fake shortcut around it - the UI and the
accept/reject/list routes have no way to tell the difference between a
proposal that arrived this way and one the model actually produced.

Usage: python scripts/make_test_proposal.py <connection_id> <session_id> <cart_id> <product_id>
Prints a JSON line: {"proposal_id": ...}
"""

import asyncio
import json
import sys

sys.path.insert(0, ".")


async def main() -> None:
    import engine.db as db

    connection_id, session_id, cart_id, product_id = sys.argv[1:5]
    proposal = await db.cart_proposals.create(
        connection_id=connection_id,
        session_id=session_id,
        shopper_id=None,
        cart_id=cart_id,
        change_type="ADD",
        product_id=product_id,
        reason="goes well with what's already in your cart",
        evidence=["declared complementary by the merchant"],
        snapshot_price=None,
    )
    print(json.dumps({"proposal_id": proposal["proposal_id"]}))


asyncio.run(main())
