"""Shopper V2 - Stage 1 (discovery-only, read-only).

Everything under this package is additive and lives beside the existing
Shopper implementation (`engine/api/chat.py` and friends). It must never be
imported BY anything outside this package plus `engine/api/chat_v2.py` - V1
is completely unaware this package exists.

Stage 1 proves one loop only:

    message -> Understanding -> StateDelta -> ShoppingStateV2 -> Context
             -> Retrieval -> Evidence -> Response

and stops there. It must never import `engine.decision`, `engine.risk`, or
`engine.execution` for anything but the deterministic, read-only relevance
and ranking helpers named in the Stage 1 spec (`engine.execution.relevance`,
`engine.execution.mission_ranking`) - never for proposing, deciding, risk-
classifying, or executing an action. See `orchestrator.py` for the one place
that assembles the whole turn, and its own docstring for the enforced
ordering that keeps the reply-before-data bug from reappearing here.
"""
