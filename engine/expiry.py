"""The expiry sweeper.

An approval that nobody actions has to end somewhere. Until this existed it simply
stopped appearing in the queue: the case stayed PENDING_APPROVAL forever, the
shopper who had been told "someone will pick this up" was never told otherwise, and
nothing recorded that a sale had been lost to nobody looking.

That last part is what made it worth building. A recovery system that quietly drops
the cases it fails to action will report a flattering success rate, because the
failures never appear in the denominator.

Three things happen when an approval expires:

The case is closed. State moves to TIMEOUT, so it stops looking like live work.

The shopper is told. They were promised a person. Nobody came, and saying so is
better than leaving them waiting - they can try another card or contact the shop
rather than sitting on a tab that will never update.

An outcome is recorded, marked unresolved. This is the honest part. An expired
recovery is a sale we did not save, and it belongs in the numbers next to the ones
we did.

The sweep runs on a timer inside the engine process. That is fine for one instance
and wrong for several - two instances would both sweep, and the second would find
nothing because the first already claimed the rows, which is harmless but wasteful.
A real deployment wants one scheduled worker.
"""

from __future__ import annotations

import asyncio
import logging

from engine import db
from engine import session as session_store

logger = logging.getLogger(__name__)

#: How often to look. Approval timeouts are measured in minutes, so checking every
#: minute means an approval expires within a minute of when it should. Checking
#: every second would be precision nobody asked for.
SWEEP_SECONDS = 60


def _message(item: dict) -> str:
    """What to tell the shopper.

    Different by friction, because the useful next step differs. Someone whose
    payment failed can try another card; someone whose search found nothing cannot
    do anything with that advice.

    Deliberately not apologetic beyond one clause. The shop failed to answer in
    time, which is worth acknowledging once and then moving past to something they
    can act on.
    """
    if item["order_id"]:
        return (
            "Sorry - nobody at the shop was able to get to order "
            + item["order_id"]
            + " in time. Your items are still held and nothing has been charged. "
            "You can try paying again, or contact the shop directly and they'll "
            "sort it out."
        )
    return (
        "Sorry - nobody at the shop was able to get to that in time. If you still "
        "need a hand, ask me again and I'll pass it on."
    )


async def sweep_once() -> int:
    """Expire everything overdue. Returns how many."""
    try:
        expired = await db.expire_approvals()
    except Exception:  # noqa: BLE001
        logger.exception("expiry sweep failed")
        return 0

    for item in expired:
        # Told first, recorded second. If the process dies between the two, a
        # shopper who has been informed and an outcome we have not counted is a
        # better state than an outcome counted and a shopper still waiting.
        if item["session_id"]:
            try:
                await session_store.add_turn(
                    session_id=item["session_id"],
                    connection_id=item["connection_id"],
                    speaker="assistant",
                    text=_message(item),
                    case_id=item["case_id"],
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "could not tell the shopper about an expired approval"
                )

        try:
            await db.record_outcome(
                connection_id=item["connection_id"],
                case_id=item["case_id"],
                # Unresolved, and counted. An expired recovery is a sale we did not
                # save, and leaving it out of the numbers would flatter them.
                resolved=False,
                final_state="TIMEOUT",
                friction_type=item["friction_type"],
                required_human=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not record an expiry outcome")

    if expired:
        logger.info(
            "expired %d approval(s); longest waited %d minutes",
            len(expired),
            max(item["waited_minutes"] for item in expired),
        )
    return len(expired)


async def run_forever() -> None:
    """The loop. Started at engine startup, cancelled at shutdown."""
    logger.info("approval expiry sweeper started (every %ds)", SWEEP_SECONDS)
    while True:
        try:
            await asyncio.sleep(SWEEP_SECONDS)
            await sweep_once()
        except asyncio.CancelledError:
            logger.info("approval expiry sweeper stopped")
            raise
        except Exception:  # noqa: BLE001
            # Never let one bad sweep kill the loop. The next one may well work, and
            # a sweeper that silently stopped would put us back where we started.
            logger.exception("expiry sweep raised; continuing")
