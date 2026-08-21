"""Idempotency.

Until now the execution service carried comments claiming a retry could not
double-charge. It could. execute_case called twice ran twice, and with Kettle &
Bloom able to actually move money, that stopped being theoretical.

This module makes the claim true.

The key is derived from the case, not generated per attempt. A random key per call
would guarantee every retry looked new, which is precisely the failure the mechanism
exists to prevent. Deriving it from the case id means the second attempt carries the
same key as the first, and both we and the platform can recognise it.

Two layers, because one is not enough. The platform's own idempotency is
authoritative where it exists, but not every platform has one and we cannot verify
the ones that claim to. So we keep our own ledger, checked before we call out at
all.

Only money-touching actions are guarded. Running a search twice is harmless and a
ledger row for it would be noise. The guard goes where a repeat has a cost.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from shared.models import ACTION_RISK_PROPERTIES, ActionType

from .models import ExecutionAttempt
from .session import session_scope

#: How long a completed attempt is remembered. Long enough to cover any plausible
#: retry, short enough that the table does not grow without bound.
RETENTION = timedelta(days=7)


def key_for(
    case_id: str, action_type: str, parameters: dict[str, Any] | None = None
) -> str:
    """The idempotency key for one action on one case.

    Deterministic: the same case and action always produce the same key, which is
    what makes a retry recognisable as a retry.

    Parameters are folded in because the same action on the same case with different
    parameters is a different intent. Applying FIRSTBAG and then applying BREWKIT500
    to the same cart are two things, not one thing twice.
    """
    payload = json.dumps(parameters or {}, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{case_id}|{action_type}|{payload}".encode()).hexdigest()
    return f"idem_{digest[:32]}"


def guarded(action_type: ActionType) -> bool:
    """Whether this action needs the guard. Money-touching actions only."""
    props = ACTION_RISK_PROPERTIES.get(action_type)
    return bool(props and props.financial)


async def claim(
    *,
    connection_id: str,
    case_id: str,
    action_type: str,
    idempotency_key: str,
) -> dict | None:
    """Claim the right to execute, or return the previous result.

    None means this is the first attempt and execution should proceed. A dict means
    this key has been seen before and the caller should return that instead.

    The row is written before the platform call, not after. Writing after leaves a
    window where a crash mid-execution loses the record and the retry charges again.
    Writing first leaves an IN_FLIGHT row, which is recoverable.
    """
    async with session_scope() as db:
        existing = await db.get(ExecutionAttempt, idempotency_key)

        if existing is not None:
            if existing.connection_id != connection_id:
                # Same key, different merchant. Should be impossible given the key
                # derivation, but returning another tenant's result would be far
                # worse than refusing.
                return {
                    "state": "CONFLICT",
                    "summary": "that idempotency key belongs to another connection",
                }
            return {
                "state": existing.state,
                "succeeded": existing.succeeded,
                "summary": existing.summary,
                "result": existing.result,
                "first_attempted_at": existing.created_at.isoformat(),
            }

        db.add(
            ExecutionAttempt(
                idempotency_key=idempotency_key,
                connection_id=connection_id,
                case_id=case_id,
                action_type=action_type,
                state="IN_FLIGHT",
            )
        )
    return None


async def complete(
    idempotency_key: str,
    *,
    succeeded: bool,
    summary: str,
    result: dict | None = None,
) -> None:
    """Record how the attempt ended.

    An IN_FLIGHT row that never completes means the process died mid-execution. It
    stays IN_FLIGHT deliberately: a later attempt reads it and knows the platform may
    or may not have acted, which is a situation a person should look at rather than a
    machine guess.
    """
    async with session_scope() as db:
        attempt = await db.get(ExecutionAttempt, idempotency_key)
        if attempt is None:
            return
        attempt.state = "DONE"
        attempt.succeeded = succeeded
        attempt.summary = summary[:500]
        attempt.result = result or {}
        attempt.completed_at = datetime.now(UTC)


async def purge_old() -> int:
    """Drop attempts past the retention window. Returns how many went."""
    cutoff = datetime.now(UTC) - RETENTION
    async with session_scope() as db:
        rows = await db.execute(
            select(ExecutionAttempt).where(ExecutionAttempt.created_at < cutoff)
        )
        stale = list(rows.scalars())
        for row in stale:
            await db.delete(row)
        return len(stale)