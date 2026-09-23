"""The Shopper V2 turn orchestrator - public entry point.

As of the LangGraph migration (2026-09, approved architecture review), the
actual turn control flow lives in `graph.py::ShopperTurnGraph` - an explicit
node/edge graph over typed state, replacing this module's former ~700-line
single function whose correctness depended on implicit ordering between
hand-added bypass guards (a shape that produced three real, reproduced bugs
in one session: an explicit `friction` signal silently overridden by the
mission-detection heuristic, then by the cart-intent resolver). See
`graph.py`'s own module docstring for the full argument and the exact
node/edge structure.

This module is kept as the STABLE public import path - `chat_v2.py` and
every test importing `from engine.shopper_v2.orchestrator import run_turn`
continue to work unchanged; `run_turn`'s signature and `TurnResultV2`'s
shape are byte-for-byte identical to before the migration. This is a
control-flow migration, not a behavior change - every underlying capability
module (`cart_proposal.py`, `checkout.py`, `payment_recovery.py`,
`compare.py`, etc.) is called exactly as before, and Decision/Risk/
Execution/Adapter are reached exactly as before, as opaque objects passed
through unchanged.
"""

from __future__ import annotations

from .graph import TurnResultV2, run_turn_via_graph

run_turn = run_turn_via_graph

__all__ = ["TurnResultV2", "run_turn"]
