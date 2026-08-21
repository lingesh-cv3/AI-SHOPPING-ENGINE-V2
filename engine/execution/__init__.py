"""Executes approved actions: capability re-check, idempotency, adapter dispatch.

Answers no questions. What to do and whether it is allowed are both settled before
anything reaches here.
"""

from .service import NO_EXECUTION, Executed, ExecutionService

__all__ = ["NO_EXECUTION", "Executed", "ExecutionService"]