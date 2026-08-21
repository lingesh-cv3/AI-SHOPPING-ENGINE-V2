"""Decision Engine. Candidates in, one selected action out.

Checks merchant policy and adapter-declared capabilities. No LLM.

This is the only place capability_verified is set to True. The Risk Gate blocks
anything that reaches it without that flag, so this module is load-bearing rather
than advisory.
"""

from .engine import (
    NO_OPERATION_ACTIONS,
    DecisionEngine,
    DecisionTrace,
    RejectedCandidate,
    is_read_only,
    operation_for,
)
from .ranking import (
    ASSISTANCE_PREFERENCE,
    PREFERENCE,
    explain_preference,
    rank_for,
    rank_of,
)
from .registry import CapabilityRegistry

__all__ = [
    "ASSISTANCE_PREFERENCE",
    "NO_OPERATION_ACTIONS",
    "PREFERENCE",
    "CapabilityRegistry",
    "DecisionEngine",
    "DecisionTrace",
    "RejectedCandidate",
    "explain_preference",
    "is_read_only",
    "operation_for",
    "rank_for",
    "rank_of",
]