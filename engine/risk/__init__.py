"""Risk / Policy Service. AUTO, HUMAN, or BLOCK.

Deterministic: same input, same output, always. No LLM.

RULE: imports nothing from engine/reasoning. This makes it structurally
impossible for the model to influence risk classification.
"""

from .gate import RULE_ORDER, RiskGate, explain_rules
from .policy import (
    SUGGESTED_AUTO_ALLOWED,
    AutomationMode,
    PolicyStore,
    RiskPolicy,
    default_policy,
)

__all__ = [
    "RULE_ORDER",
    "SUGGESTED_AUTO_ALLOWED",
    "AutomationMode",
    "PolicyStore",
    "RiskGate",
    "RiskPolicy",
    "default_policy",
    "explain_rules",
]