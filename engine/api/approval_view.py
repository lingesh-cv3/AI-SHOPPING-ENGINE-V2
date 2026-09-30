"""How a pending approval is presented to the merchant who owns it.

Presentation only. Nothing here decides, executes, or changes what the engine
does with an approval - `POST /api/approvals/{connection}/{id}` is unchanged.

The one thing it adds is honesty about approvals that cannot run. The risk gate
classifies an action by what it *is* (financial, irreversible, contacts a
customer), not by whether the engine can carry it out, so a few action types can
reach the approval queue and then do nothing when approved:

- NOTIFY_BACK_IN_STOCK: execution returns UNSUPPORTED ("needs an email service,
  which is not built").
- ISSUE_REFUND / CANCEL_ORDER: declared in the risk table, never proposed by the
  model, and with no execution path (the dispatcher falls through to UNSUPPORTED).

A merchant shown an Approve button for one of those is being asked to approve
something that cannot happen. The fix belongs at the source (the Decision Engine
should not select what execution cannot run) and is out of scope here; until then
this map lets the console say so.

HAND-MAINTAINED. It does not consult live platform capability, only what the
engine itself can execute. `tests/ops/test_merchant_approvals_api.py` ties each
entry to the real execution behaviour, so the map cannot quietly drift.
"""

from __future__ import annotations

#: action type -> why approving it would run nothing.
_CANNOT_RUN: dict[str, str] = {
    "NOTIFY_BACK_IN_STOCK": (
        "Back-in-stock alerts need an email service, which is not built yet, so "
        "approving this would run nothing."
    ),
    "ISSUE_REFUND": (
        "The engine has no refund path yet, so approving this would run nothing."
    ),
    "CANCEL_ORDER": (
        "The engine has no order-cancellation path yet, so approving this would "
        "run nothing."
    ),
}


def executability(action_type: str) -> dict:
    """{"can_run": bool, "reason": str | None} for one action type."""
    reason = _CANNOT_RUN.get(action_type)
    return {"can_run": reason is None, "reason": reason}


def cannot_run_types() -> frozenset[str]:
    """The action types the map says cannot run. For tests."""
    return frozenset(_CANNOT_RUN)
