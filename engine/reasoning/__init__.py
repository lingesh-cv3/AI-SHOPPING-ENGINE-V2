"""The ONLY component that touches an LLM.

Intent understanding, context assembly, diagnosis, candidate generation.
Proposes actions - never decides, never classifies risk, never executes.

Nothing in engine/risk imports from here, which is what makes it structurally
impossible for the model to influence risk classification.
"""

from .llm import DEFAULT_MODEL, LLMClient, LLMConfig, LLMUnavailable
from .prompts import PROPOSABLE, SYSTEM_PROMPT
from .service import Reasoning, ReasoningService

__all__ = [
    "DEFAULT_MODEL",
    "PROPOSABLE",
    "SYSTEM_PROMPT",
    "LLMClient",
    "LLMConfig",
    "LLMUnavailable",
    "Reasoning",
    "ReasoningService",
]